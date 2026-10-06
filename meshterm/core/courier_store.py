# SPDX-License-Identifier: Apache-2.0
"""The store for the Courier: the store-and-forward outbox.

The Courier (refer to :mod:`meshterm.services.courier`) queues direct messages for
contacts that it cannot reach now. It delivers them when the contact is next heard, or at
a scheduled time. This store is the outbox itself: the queued messages, with their
schedule and attempt history. It also keeps the finished messages (delivered, or given
up), up to a cap. Thus, on the next morning, the user can see what occurred.

This store is global machine state in a small JSON file (``<config_dir>/courier.json``),
the same as the remembered devices (:mod:`meshterm.core.device_store`). It is not in the
SQLite database, which can be different for each run. A queued message must stay after a
restart. Else the full promise ("it'll go out when the contact shows up") is empty.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from .atomicwrite import write_atomically
from .models import utcnow

#: The message states: waiting in the outbox, delivered, or abandoned after the retry
#: budget is spent.
QUEUED = "queued"
DELIVERED = "delivered"
GAVE_UP = "gave-up"

#: How many finished entries (delivered or given up) the store keeps for the history on
#: the screen. This is the only default of the ``courier_history_kept`` preference. The
#: registry refers to this name and does not type the value again
#: (:data:`meshterm.core.preferences.PREFERENCES`).
DONE_CAP = 100


@dataclass(slots=True)
class QueuedMessage:
    """One outbox entry, from the queue to the delivery (or until MeshTerm gives up).

    Attributes:
        ident: A monotonic id (stable across restarts), to address the entry.
        node_key: The canonical 12-hex key prefix of the recipient (the id that
            observations carry). At send time, MeshTerm matches it against the contact
            list. For reachability, MeshTerm matches it against overheard packets.
        node_name: The name of the recipient to show, copied at queue time.
        text: The message body.
        created: When the message was queued.
        not_before: Hold the message until this time (a scheduled send). ``None`` sends
            at the next sign of life instead.
        attempts: The delivery attempts so far. Each attempt is one full chat send, with
            the soft-retry budget of the chat service inside it.
        last_attempt: When the latest attempt ran (the retry backoff uses it).
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

    #: The fields that this record held before. It must never hold them again with a
    #: different meaning (refer to :data:`meshterm.core.preferences.RETIRED` for the
    #: reason that a name is never used again).
    RETIRED: ClassVar[frozenset[str]] = frozenset()


class CourierStore:
    """Reads and writes the outbox, in memory first (read one time, written at each change)."""

    def __init__(self, path: Path) -> None:
        """Open the store on the location of a JSON file.

        Args:
            path: Path to the JSON state file (made only at the first write).
        """
        self._path = path
        self._messages: list[QueuedMessage] | None = None
        self._next_id = 1

    # --- state ---------------------------------------------------------------------

    def _load(self) -> list[QueuedMessage]:
        """Parse the file. A missing or corrupt file gives an empty outbox."""
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
            node_key: The canonical 12-hex key prefix of the recipient.
            node_name: The name of the recipient to show.
            text: The message body.
            not_before: Hold the message until this time. ``None`` sends at the next sign
                of life.

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
        """The waiting messages, oldest first (the first queued is the first delivered)."""
        return [m for m in self._load() if m.status == QUEUED]

    def entries(self) -> list[QueuedMessage]:
        """All the entries (waiting and finished), newest first (the order of the screen)."""
        return sorted(self._load(), key=lambda m: m.created, reverse=True)

    def get(self, ident: int) -> QueuedMessage | None:
        """One entry by its id, or ``None``."""
        return next((m for m in self._load() if m.ident == ident), None)

    def pending_count(self) -> int:
        """How many messages are waiting in the outbox."""
        return len(self.pending())

    def done_count(self) -> int:
        """How many finished entries (delivered or given up) the history holds."""
        return sum(1 for m in self._load() if m.status != QUEUED)

    # --- attempt bookkeeping -----------------------------------------------------------

    def note_attempt(self, ident: int, *, when: datetime | None = None) -> None:
        """Store that a delivery attempt is in progress.

        The store writes this before the send. Thus a crash during the transmission can
        never spend the retry budget two times.
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
        """Finish one entry, and cut the done history to its cap."""
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
        """Remove a waiting entry completely (for finished entries, use :meth:`clear_done`).

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
        """Remove all the finished entries, and do not change the waiting queue."""
        messages = self._load()
        kept = [m for m in messages if m.status == QUEUED]
        if len(kept) != len(messages):
            self._messages = kept
            self._save()

    # --- write to disk ---------------------------------------------------------------

    def _save(self) -> None:
        """Write the full outbox atomically.

        If a crash occurs during the write, the old file stays.
        """
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
    """Convert a stored number to a non-negative int, or return ``default`` if it fails."""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _as_time(value: object) -> datetime | None:
    """Parse a stored ISO-8601 timestamp, or return ``None`` if it is absent or corrupt.

    The function also reads a timestamp without a time zone as corrupt. The store only
    writes aware UTC timestamps, and a naive timestamp makes the schedule arithmetic wrong.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
