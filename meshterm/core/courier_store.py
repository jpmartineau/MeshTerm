# SPDX-License-Identifier: Apache-2.0
"""Persistence for the Courier: the store-and-forward outbox.

The Courier (see :mod:`meshterm.services.courier`) queues direct messages for contacts
that aren't reachable right now and delivers them when the contact is next heard — or at
a scheduled time. This store is the outbox itself: queued messages with their schedule
and attempt history, plus the finished ones (delivered or given-up) kept around, capped,
so the morning after tells the story.

Like the remembered devices (:mod:`meshterm.core.device_store`), this is global machine
state in a small JSON file (``<config_dir>/courier.json``) rather than the
per-invocation SQLite database — a queued message must survive restarts, or the whole
promise ("it'll go out when the contact shows up") is hollow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from .atomicwrite import write_atomically
from .models import utcnow

#: Message states: waiting in the outbox, landed, or abandoned after the retry budget.
QUEUED = "queued"
DELIVERED = "delivered"
GAVE_UP = "gave-up"

#: How many finished (delivered / given-up) entries are kept for the screen's history —
#: *the* default behind the ``courier_history_kept`` preference, which the registry names
#: rather than re-types (:data:`meshterm.core.preferences.PREFERENCES`).
DONE_CAP = 100


@dataclass(slots=True)
class QueuedMessage:
    """One outbox entry, from queueing through delivery (or defeat).

    Attributes:
        ident: Monotonic id (stable across restarts), used to address the entry.
        node_key: The recipient's canonical 12-hex id (what observations carry),
            matched against the contact list at send time and against overheard
            packets for reachability.
        node_name: The recipient's display name, snapshotted at queue time.
        text: The message body.
        created: When it was queued.
        not_before: Hold until this time (a scheduled send); ``None`` sends on the
            next sign of life instead.
        attempts: Delivery attempts made so far (each is one full chat send, with
            the chat service's own soft-retry budget inside it).
        last_attempt: When the latest attempt ran (drives the retry backoff).
        status: :data:`QUEUED`, :data:`DELIVERED`, or :data:`GAVE_UP`.
        finished: When the entry left the queue (delivered or given up).
    """

    ident: int
    node_key: str
    node_name: str
    text: str
    created: datetime
    not_before: datetime | None = None
    attempts: int = 0
    last_attempt: datetime | None = None
    status: str = QUEUED
    finished: datetime | None = None

    #: Fields this record once held and must never hold again under another meaning (see
    #: :data:`meshterm.core.preferences.RETIRED` for why a name is never reused).
    RETIRED: ClassVar[frozenset[str]] = frozenset()


class CourierStore:
    """Reads and writes the outbox, memory-first (loaded once, persisted on change)."""

    def __init__(self, path: Path) -> None:
        """Open the store against a JSON file location.

        Args:
            path: Path to the JSON state file (created lazily on first write).
        """
        self._path = path
        self._messages: list[QueuedMessage] | None = None
        self._next_id = 1

    # --- state ---------------------------------------------------------------------

    def _load(self) -> list[QueuedMessage]:
        """Parse the file, tolerating absence and corruption (an empty outbox)."""
        if self._messages is not None:
            return self._messages
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        self._next_id = max(1, _as_int(data.get("next_id"), 1))
        messages: list[QueuedMessage] = []
        for record in data.get("messages", []):
            if not isinstance(record, dict):
                continue
            created = _as_time(record.get("created"))
            if created is None:
                continue
            messages.append(
                QueuedMessage(
                    ident=_as_int(record.get("ident"), 0),
                    node_key=str(record.get("node_key") or ""),
                    node_name=str(record.get("node_name") or "?"),
                    text=str(record.get("text") or ""),
                    created=created,
                    not_before=_as_time(record.get("not_before")),
                    attempts=_as_int(record.get("attempts"), 0),
                    last_attempt=_as_time(record.get("last_attempt")),
                    status=str(record.get("status") or QUEUED),
                    finished=_as_time(record.get("finished")),
                )
            )
        self._messages = messages
        return messages

    # --- the queue -----------------------------------------------------------------

    def queue(
        self,
        node_key: str,
        node_name: str,
        text: str,
        *,
        not_before: datetime | None = None,
    ) -> QueuedMessage:
        """Add a message to the outbox.

        Args:
            node_key: The recipient's canonical 12-hex id.
            node_name: The recipient's display name.
            text: The message body.
            not_before: Hold until this time; ``None`` sends on next sign of life.

        Returns:
            The stored :class:`QueuedMessage`.
        """
        messages = self._load()
        message = QueuedMessage(
            ident=self._next_id,
            node_key=node_key,
            node_name=node_name,
            text=text,
            created=utcnow(),
            not_before=not_before,
        )
        self._next_id += 1
        messages.append(message)
        self._save()
        return message

    def pending(self) -> list[QueuedMessage]:
        """The waiting messages, oldest first (first queued, first delivered)."""
        return [m for m in self._load() if m.status == QUEUED]

    def entries(self) -> list[QueuedMessage]:
        """Every entry — waiting and finished — newest first (the screen's order)."""
        return sorted(self._load(), key=lambda m: m.created, reverse=True)

    def get(self, ident: int) -> QueuedMessage | None:
        """One entry by id, or ``None``."""
        return next((m for m in self._load() if m.ident == ident), None)

    def pending_count(self) -> int:
        """How many messages are waiting in the outbox."""
        return len(self.pending())

    def done_count(self) -> int:
        """How many finished (delivered / given-up) entries the history holds."""
        return sum(1 for m in self._load() if m.status != QUEUED)

    # --- attempt bookkeeping -----------------------------------------------------------

    def note_attempt(self, ident: int, *, when: datetime | None = None) -> None:
        """Record that a delivery attempt is being made.

        Written *before* the send, so a crash mid-transmission can never spend the
        retry budget twice.
        """
        message = self.get(ident)
        if message is not None:
            message.attempts += 1
            message.last_attempt = when or utcnow()
            self._save()

    def mark_delivered(self, ident: int, *, when: datetime | None = None) -> None:
        """Move an entry to :data:`DELIVERED`."""
        self._finish(ident, DELIVERED, when)

    def mark_gave_up(self, ident: int, *, when: datetime | None = None) -> None:
        """Move an entry to :data:`GAVE_UP` (the retry budget is spent)."""
        self._finish(ident, GAVE_UP, when)

    def _finish(self, ident: int, status: str, when: datetime | None) -> None:
        """Finish one entry and trim the done history to its cap."""
        message = self.get(ident)
        if message is None:
            return
        message.status = status
        message.finished = when or utcnow()
        messages = self._load()
        done = [m for m in messages if m.status != QUEUED]
        from .preferences import current as current_preferences

        cap = current_preferences().courier_history_kept
        if len(done) > cap:
            done.sort(key=lambda m: m.finished or m.created)
            drop = {m.ident for m in done[: len(done) - cap]}
            self._messages = [m for m in messages if m.ident not in drop]
        self._save()

    def cancel(self, ident: int) -> bool:
        """Remove a *waiting* entry outright (finished ones use :meth:`clear_done`).

        Returns:
            Whether an entry was removed.
        """
        messages = self._load()
        kept = [m for m in messages if not (m.ident == ident and m.status == QUEUED)]
        if len(kept) == len(messages):
            return False
        self._messages = kept
        self._save()
        return True

    def clear_done(self) -> None:
        """Drop every finished entry, leaving the waiting queue untouched."""
        messages = self._load()
        kept = [m for m in messages if m.status == QUEUED]
        if len(kept) != len(messages):
            self._messages = kept
            self._save()

    # --- persistence -----------------------------------------------------------------

    def _save(self) -> None:
        """Persist the whole outbox atomically (crash mid-write keeps the old file)."""
        data = {
            "next_id": self._next_id,
            "messages": [
                {
                    "ident": m.ident,
                    "node_key": m.node_key,
                    "node_name": m.node_name,
                    "text": m.text,
                    "created": m.created.isoformat(),
                    "not_before": m.not_before.isoformat() if m.not_before else None,
                    "attempts": m.attempts,
                    "last_attempt": m.last_attempt.isoformat() if m.last_attempt else None,
                    "status": m.status,
                    "finished": m.finished.isoformat() if m.finished else None,
                }
                for m in self._load()
            ],
        }
        write_atomically(self._path, json.dumps(data, indent=2))


def _as_int(value: object, default: int) -> int:
    """Coerce a stored number to a non-negative int, falling back to ``default``."""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _as_time(value: object) -> datetime | None:
    """Parse a stored ISO-8601 timestamp, or ``None`` if absent/corrupt.

    A timestamp without a zone is treated as corrupt too: the store only ever writes
    aware UTC stamps, and a naive one would poison the schedule arithmetic.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
