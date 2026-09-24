# SPDX-License-Identifier: Apache-2.0
"""Persistence for the Watchtower: watched nodes, their rules, and the alert log.

The Watchtower (see :mod:`meshterm.services.watchtower`) is a passive sentinel: the user
stars the nodes they care about — the repeater on the roof, the gateway across town —
and rules watch what the always-on event hub already hears. This store remembers, per
watched node (keyed by the 12-hex canonical id observations use), the rule settings and
the last-heard mark the silence rule counts from, plus the rolling alert log and its
acknowledged state, so an alarm raised overnight is still waiting in the morning even
across a restart.

Like the remembered devices (:mod:`meshterm.core.device_store`), this is global machine
state in a small JSON file (``<config_dir>/watchtower.json``) rather than the
per-invocation SQLite database. Reads are served from memory after the first load — the
header badge reads the unacked count on every repaint — and the packet-driven
``note_heard`` writes are throttled so a busy mesh doesn't grind the disk.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from .models import utcnow

#: Silence-rule choices offered in the editor, in hours (:data:`OFF` disables it).
SILENCE_CHOICES_H = (1, 3, 6, 12, 24, 48)

#: Default silence threshold: half a day covers nodes that only advertise daily-ish
#: without crying wolf over a quiet afternoon. It is the *code's* default; what a newly
#: starred node actually gets is the ``watch_silence_hours`` preference, which defaults
#: to this (see :mod:`meshterm.core.preferences`).
DEFAULT_SILENCE_HOURS = 12

#: The stored value meaning "this rule is off".
OFF = 0

#: How many alerts the log retains (oldest dropped first) — *the* default behind the
#: ``watch_alerts_kept`` preference, which the registry names rather than re-types
#: (:data:`meshterm.core.preferences.PREFERENCES`), so the two can never disagree.
ALERT_CAP = 200

#: Minimum seconds between disk flushes for the packet-driven last-heard updates.
_FLUSH_EVERY_S = 60.0


@dataclass(slots=True)
class WatchedNode:
    """One starred node: its rules and the marks the rules count from.

    Attributes:
        key: The node's canonical 12-hex id (what observations carry).
        name: Display label, refreshed whenever the node is heard with a name.
        silence_hours: Hours of silence before the alarm (:data:`OFF` disables it).
        snr_watch: Whether the SNR-sag rule watches this node's receptions.
        last_heard: When the node was last heard (seeded at watch time so the
            silence countdown is armed immediately).
        silent_since: Set while a silence alarm is active, so it fires once and
            re-arms only after the node is heard again.
        node_type: The node's advertised type at star time (a ``NODE_TYPE_*``
            constant), for the watchlist's type glyph; ``None`` for entries starred
            before the field existed (the screen falls back to the contact table).
    """

    key: str
    name: str
    silence_hours: int = DEFAULT_SILENCE_HOURS
    snr_watch: bool = True
    last_heard: datetime | None = None
    silent_since: datetime | None = None
    node_type: int | None = None

    #: Fields this record once held and must never hold again under another meaning (see
    #: :data:`meshterm.core.preferences.RETIRED` for why a name is never reused).
    RETIRED: ClassVar[frozenset[str]] = frozenset()


@dataclass(slots=True)
class Alert:
    """One raised alert, kept until it scrolls off the capped log.

    Attributes:
        ident: Monotonic id (stable across restarts) used to acknowledge it.
        when: When the rule tripped.
        kind: ``silence`` / ``recovered`` / ``snr`` / ``new-node``.
        label: The node's display label at the time.
        message: The human-readable one-liner.
        acked: Whether the user has acknowledged it (feeds the header badge).
    """

    ident: int
    when: datetime
    kind: str
    label: str
    message: str
    acked: bool = False

    #: Fields this record once held and must never hold again under another meaning (see
    #: :data:`meshterm.core.preferences.RETIRED` for why a name is never reused).
    RETIRED: ClassVar[frozenset[str]] = frozenset()


@dataclass(slots=True)
class _State:
    """The store's in-memory shape (mirrors the JSON file)."""

    watched: dict[str, WatchedNode] = field(default_factory=dict)
    alerts: list[Alert] = field(default_factory=list)
    known: set[str] = field(default_factory=set)
    new_node_alerts: bool = True
    next_id: int = 1

    #: Fields this record once held and must never hold again under another meaning (see
    #: :data:`meshterm.core.preferences.RETIRED` for why a name is never reused).
    RETIRED: ClassVar[frozenset[str]] = frozenset()


class WatchStore:
    """Reads and writes the Watchtower's state, memory-first."""

    def __init__(self, path: Path) -> None:
        """Open the store against a JSON file location.

        Args:
            path: Path to the JSON state file (created lazily on first write).
        """
        self._path = path
        self._state: _State | None = None
        self._dirty = False
        self._last_flush = 0.0

    # --- state ---------------------------------------------------------------------

    @property
    def state(self) -> _State:
        """The in-memory state, loaded from disk on first access."""
        if self._state is None:
            self._state = self._load()
        return self._state

    def _load(self) -> _State:
        """Parse the file into a :class:`_State`, or a fresh default on any trouble."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        state = _State(
            new_node_alerts=bool(data.get("new_node_alerts", True)),
            next_id=max(1, _as_int(data.get("next_id"), 1)),
            known={str(k) for k in data.get("known", []) if isinstance(k, str)},
        )
        watched = data.get("watched")
        if isinstance(watched, dict):
            for key, record in watched.items():
                if not isinstance(record, dict):
                    continue
                raw_type = record.get("node_type")
                state.watched[str(key)] = WatchedNode(
                    key=str(key),
                    name=str(record.get("name") or key),
                    silence_hours=_as_int(record.get("silence_hours"), DEFAULT_SILENCE_HOURS),
                    snr_watch=bool(record.get("snr_watch", True)),
                    last_heard=_as_time(record.get("last_heard")),
                    silent_since=_as_time(record.get("silent_since")),
                    node_type=raw_type if isinstance(raw_type, int) and raw_type >= 0 else None,
                )
        for record in data.get("alerts", []):
            if not isinstance(record, dict):
                continue
            when = _as_time(record.get("when"))
            if when is None:
                continue
            state.alerts.append(
                Alert(
                    ident=_as_int(record.get("ident"), 0),
                    when=when,
                    kind=str(record.get("kind") or "alert"),
                    label=str(record.get("label") or "?"),
                    message=str(record.get("message") or ""),
                    acked=bool(record.get("acked", False)),
                )
            )
        return state

    # --- watched nodes ---------------------------------------------------------------

    def watched(self) -> dict[str, WatchedNode]:
        """The watched nodes by canonical id (the live mapping — treat as read-mostly)."""
        return self.state.watched

    def is_watched(self, key: str) -> bool:
        """Whether ``key`` is on the watchlist."""
        return key in self.state.watched

    def watch(
        self,
        key: str,
        name: str,
        *,
        last_seen: datetime | None = None,
        node_type: int | None = None,
    ) -> None:
        """Star a node with default rules.

        The silence countdown arms immediately: it counts from the contact's known
        ``last_seen`` when there is one (so a node that has already been quiet for a
        day alarms on the next sweep, which is exactly why it was starred), else from
        now.

        Args:
            key: The node's canonical 12-hex id.
            name: Display label.
            last_seen: When the node was last heard, if known.
            node_type: The node's advertised type, if known (for the watchlist glyph).
        """
        from .preferences import current as current_preferences

        self.state.watched[key] = WatchedNode(
            key=key,
            name=name,
            silence_hours=current_preferences().watch_silence_hours,
            last_heard=last_seen or utcnow(),
            node_type=node_type,
        )
        self._save()

    def unwatch(self, key: str) -> None:
        """Drop a node from the watchlist (its past alerts stay in the log)."""
        if self.state.watched.pop(key, None) is not None:
            self._save()

    def set_silence(self, key: str, hours: int) -> None:
        """Set a watched node's silence threshold (:data:`OFF` disables the rule)."""
        entry = self.state.watched.get(key)
        if entry is not None:
            entry.silence_hours = int(hours)
            self._save()

    def set_snr_watch(self, key: str, on: bool) -> None:
        """Enable/disable the SNR-sag rule for a watched node."""
        entry = self.state.watched.get(key)
        if entry is not None:
            entry.snr_watch = bool(on)
            self._save()

    def note_heard(
        self, key: str, *, when: datetime | None = None, name: str | None = None
    ) -> None:
        """Record that a watched node was heard (packet-driven; writes throttled).

        Args:
            key: The node's canonical id.
            when: Reception time (defaults to now); older-than-known stamps are kept.
            name: A freshly advertised name, adopted as the display label when given.
        """
        entry = self.state.watched.get(key)
        if entry is None:
            return
        stamp = when or utcnow()
        if entry.last_heard is None or stamp > entry.last_heard:
            entry.last_heard = stamp
        if name:
            entry.name = name
        self._dirty = True
        self.flush(only_if_due=True)

    def mark_silent(self, key: str, when: datetime | None = None) -> None:
        """Latch a node's active silence alarm so it fires once per quiet spell.

        Packet/sweep-driven (like :meth:`note_heard`), so the write is throttled — the
        full-state save is synchronous on the event loop, and the sentinel flushes any
        pending change on stop.
        """
        entry = self.state.watched.get(key)
        if entry is not None:
            entry.silent_since = when or utcnow()
            self._dirty = True
            self.flush(only_if_due=True)

    def clear_silent(self, key: str) -> None:
        """Re-arm a node's silence alarm after it has been heard again.

        Packet-driven, so throttled like :meth:`mark_silent` — a recovery burst after an
        outage must not land one full-state disk write per packet.
        """
        entry = self.state.watched.get(key)
        if entry is not None and entry.silent_since is not None:
            entry.silent_since = None
            self._dirty = True
            self.flush(only_if_due=True)

    # --- the new-node baseline ---------------------------------------------------------

    @property
    def new_node_alerts(self) -> bool:
        """Whether hearing a never-before-seen node raises an alert."""
        return self.state.new_node_alerts

    def set_new_node_alerts(self, on: bool) -> None:
        """Toggle the new-node rule."""
        self.state.new_node_alerts = bool(on)
        self._save()

    def known_contains(self, node: str) -> bool:
        """Whether ``node`` has already been recorded (never re-announced)."""
        return node in self.state.known

    def remember_known(self, node: str) -> None:
        """Record ``node`` as seen so it is announced as new at most once, ever."""
        if node not in self.state.known:
            self.state.known.add(node)
            self._dirty = True
            self.flush(only_if_due=True)

    # --- alerts ---------------------------------------------------------------------

    def add_alert(
        self, kind: str, label: str, message: str, *, when: datetime | None = None
    ) -> Alert:
        """Append an alert to the log (capped) and persist, throttled.

        Alerts fire from the packet path (a new mesh region can announce a never-seen
        node per packet for a while), and the save serializes the whole state
        synchronously on the event loop — so this batches through the same throttled
        flush as :meth:`note_heard`. The in-memory log (and the header badge it feeds)
        updates immediately either way; the sentinel flushes any pending write on stop.

        Args:
            kind: The rule that tripped (``silence``/``recovered``/``snr``/``new-node``).
            label: The node's display label.
            message: The human-readable one-liner.
            when: When it tripped (defaults to now).

        Returns:
            The stored :class:`Alert`.
        """
        state = self.state
        alert = Alert(
            ident=state.next_id,
            when=when or utcnow(),
            kind=kind,
            label=label,
            message=message,
        )
        state.next_id += 1
        state.alerts.append(alert)
        from .preferences import current as current_preferences

        cap = current_preferences().watch_alerts_kept
        if len(state.alerts) > cap:
            del state.alerts[: len(state.alerts) - cap]
        self._dirty = True
        self.flush(only_if_due=True)
        return alert

    def alerts(self) -> list[Alert]:
        """The alert log, newest first (a copy, safe to slice)."""
        return list(reversed(self.state.alerts))

    def ack(self, ident: int) -> None:
        """Acknowledge one alert by id."""
        for alert in self.state.alerts:
            if alert.ident == ident and not alert.acked:
                alert.acked = True
                self._save()
                return

    def ack_all(self) -> None:
        """Acknowledge every alert."""
        changed = False
        for alert in self.state.alerts:
            if not alert.acked:
                alert.acked = True
                changed = True
        if changed:
            self._save()

    def clear_acked(self) -> None:
        """Drop acknowledged alerts from the log, keeping the live ones."""
        state = self.state
        kept = [a for a in state.alerts if not a.acked]
        if len(kept) != len(state.alerts):
            state.alerts = kept
            self._save()

    def unacked_count(self) -> int:
        """How many alerts are waiting — the header badge's number."""
        return sum(1 for a in self.state.alerts if not a.acked)

    # --- persistence -----------------------------------------------------------------

    def flush(self, *, only_if_due: bool = False) -> None:
        """Write pending throttled changes to disk.

        Args:
            only_if_due: Skip when the last flush was recent (the packet-driven
                path); a plain ``flush()`` writes any pending change immediately.
        """
        if not self._dirty:
            return
        if only_if_due and time.monotonic() - self._last_flush < _FLUSH_EVERY_S:
            return
        self._save()

    def _save(self) -> None:
        """Persist the whole state atomically (crash mid-write keeps the old file)."""
        state = self.state
        data = {
            "new_node_alerts": state.new_node_alerts,
            "next_id": state.next_id,
            "known": sorted(state.known),
            "watched": {
                key: {
                    "name": e.name,
                    "silence_hours": e.silence_hours,
                    "snr_watch": e.snr_watch,
                    "last_heard": e.last_heard.isoformat() if e.last_heard else None,
                    "silent_since": e.silent_since.isoformat() if e.silent_since else None,
                    "node_type": e.node_type,
                }
                for key, e in state.watched.items()
            },
            "alerts": [
                {
                    "ident": a.ident,
                    "when": a.when.isoformat(),
                    "kind": a.kind,
                    "label": a.label,
                    "message": a.message,
                    "acked": a.acked,
                }
                for a in state.alerts
            ],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._path)
        self._dirty = False
        self._last_flush = time.monotonic()


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
    aware UTC stamps, and a naive one would poison the silence arithmetic.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
