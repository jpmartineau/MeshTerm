# SPDX-License-Identifier: Apache-2.0
"""The store for the Watchtower: the watched nodes, their rules, and the alert log.

The Watchtower (refer to :mod:`meshterm.services.watchtower`) is a passive monitor. The
user stars the nodes that are important to the user: for example, the repeater on the
roof, or the gateway on the other side of the town. Then rules watch what the event hub
already hears (the event hub is always on).

For each watched node, this store keeps the rule settings and the last-heard mark from
which the silence rule counts. The key of a watched node is the canonical 12-hex key
prefix that the observations use. The store also keeps the rolling alert log and its
acknowledged state. Thus an alarm that MeshTerm raised in the night is still there in the
morning, also after a restart.

This store is global machine state in a small JSON file (``<config_dir>/watchtower.json``),
the same as the remembered devices (:mod:`meshterm.core.device_store`). It is not in the
SQLite database, which can be different for each run. After the first read, the store
gives all reads from memory, because the header badge reads the unacked count at each
paint. Packets cause the ``note_heard`` writes, so the store throttles them. Thus a busy
mesh does not cause continuous disk writes.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from .models import utcnow

#: The silence-rule choices that the editor offers, in hours (:data:`OFF` turns the rule
#: off).
SILENCE_CHOICES_H = (1, 3, 6, 12, 24, 48)

#: The default silence threshold. Half a day is sufficient for nodes that advertise only
#: approximately one time each day, and it gives no false alarm for a quiet afternoon.
#: This is the default of the code. A node that the user stars gets the value of the
#: ``watch_silence_hours`` preference, and the default of that preference is this value
#: (refer to :mod:`meshterm.core.preferences`).
DEFAULT_SILENCE_HOURS = 12

#: The stored value that means "this rule is off".
OFF = 0

#: How many alerts the log keeps (the store removes the oldest alert first). This is the
#: only default of the ``watch_alerts_kept`` preference. The registry refers to this name
#: and does not type the value again (:data:`meshterm.core.preferences.PREFERENCES`).
#: Thus the two values can never be different.
ALERT_CAP = 200

#: The minimum time between two disk flushes for the last-heard changes that packets
#: cause, in seconds.
_FLUSH_EVERY_S = 60.0


@dataclass(slots=True)
class WatchedNode:
    """One starred node: its rules, and the marks from which the rules count.

    Attributes:
        key: The canonical 12-hex key prefix of the node (the id that observations
            carry).
        name: The label to show. It changes each time the node is heard with a name.
        silence_hours: The hours of silence before the alarm (:data:`OFF` turns the
            alarm off).
        snr_watch: Whether the SNR-sag rule watches the receptions of this node.
        last_heard: When the node was last heard. The store sets it when the user stars
            the node, so that the silence countdown starts immediately.
        silent_since: Set while a silence alarm is active. Thus MeshTerm raises the
            alarm one time, and arms it again only after the node is heard again.
        node_type: The advertised type of the node when the user starred it (a
            ``NODE_TYPE_*`` constant), for the type glyph of the watchlist. ``None`` for
            entries that were starred before this field existed (the screen then uses
            the contact table).
    """

    key: str
    name: str
    silence_hours: int = DEFAULT_SILENCE_HOURS
    snr_watch: bool = True
    last_heard: datetime | None = None
    silent_since: datetime | None = None
    node_type: int | None = None

    #: The fields that this record held before. It must never hold them again with a
    #: different meaning (refer to :data:`meshterm.core.preferences.RETIRED` for the
    #: reason that a name is never used again).
    RETIRED: ClassVar[frozenset[str]] = frozenset()


@dataclass(slots=True)
class Alert:
    """One raised alert. The log keeps it until it goes off the end of the capped log.

    Attributes:
        ident: A monotonic id (stable across restarts) to acknowledge the alert.
        when: When the rule raised the alert.
        kind: ``silence`` / ``recovered`` / ``snr`` / ``new-node``.
        label: The label of the node at that time.
        message: The one-line text for the user.
        acked: Whether the user acknowledged the alert (the header badge uses it).
    """

    ident: int
    when: datetime
    kind: str
    label: str
    message: str
    acked: bool = False

    #: The fields that this record held before. It must never hold them again with a
    #: different meaning (refer to :data:`meshterm.core.preferences.RETIRED` for the
    #: reason that a name is never used again).
    RETIRED: ClassVar[frozenset[str]] = frozenset()


@dataclass(slots=True)
class _State:
    """The in-memory shape of the store (the same shape as the JSON file)."""

    watched: dict[str, WatchedNode] = field(default_factory=dict)
    alerts: list[Alert] = field(default_factory=list)
    known: set[str] = field(default_factory=set)
    new_node_alerts: bool = True
    next_id: int = 1

    #: The fields that this record held before. It must never hold them again with a
    #: different meaning (refer to :data:`meshterm.core.preferences.RETIRED` for the
    #: reason that a name is never used again).
    RETIRED: ClassVar[frozenset[str]] = frozenset()


class WatchStore:
    """Reads and writes the state of the Watchtower, in memory first."""

    def __init__(self, path: Path) -> None:
        """Open the store on the location of a JSON file.

        Args:
            path: Path to the JSON state file (made only at the first write).
        """
        self._path = path
        self._state: _State | None = None
        self._dirty = False
        self._last_flush = 0.0

    # --- state ---------------------------------------------------------------------

    @property
    def state(self) -> _State:
        """The in-memory state, read from disk at the first access."""
        if self._state is None:
            self._state = self._load()
        return self._state

    def _load(self) -> _State:
        """Parse the file into a :class:`_State`, or return a new default at any problem."""
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
        """The watched nodes by canonical id (the live mapping: use it mostly to read)."""
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
        """Star a node with the default rules.

        The silence countdown starts immediately. It counts from the known ``last_seen``
        of the contact when there is one, else from now. Thus a node that was already
        quiet for a day causes an alarm at the next sweep. That is exactly why the user
        starred it.

        Args:
            key: The canonical 12-hex key prefix of the node.
            name: The label to show.
            last_seen: When the node was last heard, if known.
            node_type: The advertised type of the node, if known (for the glyph in the
                watchlist).
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
        """Remove a node from the watchlist (its past alerts stay in the log)."""
        if self.state.watched.pop(key, None) is not None:
            self._save()

    def set_silence(self, key: str, hours: int) -> None:
        """Set the silence threshold of a watched node (:data:`OFF` turns the rule off)."""
        entry = self.state.watched.get(key)
        if entry is not None:
            entry.silence_hours = int(hours)
            self._save()

    def set_snr_watch(self, key: str, on: bool) -> None:
        """Turn the SNR-sag rule on or off for a watched node."""
        entry = self.state.watched.get(key)
        if entry is not None:
            entry.snr_watch = bool(on)
            self._save()

    def note_heard(
        self, key: str, *, when: datetime | None = None, name: str | None = None
    ) -> None:
        """Store that a watched node was heard.

        Packets cause this call, so the writes are throttled.

        Args:
            key: The canonical id of the node.
            when: The reception time (the default is now). If this time is older than
                the known time, the known time stays.
            name: A name that was advertised recently. When it is given, it becomes the
                label to show.
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
        """Latch the active silence alarm of a node: one alarm for each quiet period.

        Packets and sweeps cause this call (the same as :meth:`note_heard`), so the write
        is throttled. The save of the full state is synchronous on the event loop. The
        Watchtower service flushes each pending change when it stops.
        """
        entry = self.state.watched.get(key)
        if entry is not None:
            entry.silent_since = when or utcnow()
            self._dirty = True
            self.flush(only_if_due=True)

    def clear_silent(self, key: str) -> None:
        """Arm the silence alarm of a node again, after the node was heard again.

        Packets cause this call, so it is throttled the same as :meth:`mark_silent`. After
        an outage, a burst of recovery packets must not cause one full-state disk write
        for each packet.
        """
        entry = self.state.watched.get(key)
        if entry is not None and entry.silent_since is not None:
            entry.silent_since = None
            self._dirty = True
            self.flush(only_if_due=True)

    # --- the new-node baseline ---------------------------------------------------------

    @property
    def new_node_alerts(self) -> bool:
        """Whether a node that was never heard before raises an alert when it is heard."""
        return self.state.new_node_alerts

    def set_new_node_alerts(self, on: bool) -> None:
        """Turn the new-node rule on or off."""
        self.state.new_node_alerts = bool(on)
        self._save()

    def known_contains(self, node: str) -> bool:
        """Whether ``node`` is already stored (it is never announced again)."""
        return node in self.state.known

    def remember_known(self, node: str) -> None:
        """Store ``node`` as heard, so that it is never announced as new more than one time."""
        if node not in self.state.known:
            self.state.known.add(node)
            self._dirty = True
            self.flush(only_if_due=True)

    # --- alerts ---------------------------------------------------------------------

    def add_alert(
        self, kind: str, label: str, message: str, *, when: datetime | None = None
    ) -> Alert:
        """Add an alert to the end of the log (capped), and write it with the throttle.

        Alerts come from the packet path. (In a new area of the mesh, MeshTerm can
        announce a node that it never heard before for each packet, for some time.) The
        save serializes the full state synchronously on the event loop. Thus this
        function uses the same throttled flush as :meth:`note_heard`. The in-memory log
        (and the header badge that uses it) changes immediately in all cases. The
        Watchtower service flushes each pending write when it stops.

        Args:
            kind: The rule that raised the alert
                (``silence``/``recovered``/``snr``/``new-node``).
            label: The label of the node to show.
            message: The one-line text for the user.
            when: When the rule raised the alert (the default is now).

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
        """The alert log, newest first (a copy, which is safe to slice)."""
        return list(reversed(self.state.alerts))

    def ack(self, ident: int) -> None:
        """Acknowledge one alert by its id."""
        for alert in self.state.alerts:
            if alert.ident == ident and not alert.acked:
                alert.acked = True
                self._save()
                return

    def ack_all(self) -> None:
        """Acknowledge all the alerts."""
        changed = False
        for alert in self.state.alerts:
            if not alert.acked:
                alert.acked = True
                changed = True
        if changed:
            self._save()

    def clear_acked(self) -> None:
        """Remove the acknowledged alerts from the log, and keep the live alerts."""
        state = self.state
        kept = [a for a in state.alerts if not a.acked]
        if len(kept) != len(state.alerts):
            state.alerts = kept
            self._save()

    def unacked_count(self) -> int:
        """How many alerts wait for the user: the number on the header badge."""
        return sum(1 for a in self.state.alerts if not a.acked)

    # --- write to disk ---------------------------------------------------------------

    def flush(self, *, only_if_due: bool = False) -> None:
        """Write the pending throttled changes to disk.

        Args:
            only_if_due: Do nothing when the last flush was recent (the path that packets
                use). A plain ``flush()`` writes each pending change immediately.
        """
        if not self._dirty:
            return
        if only_if_due and time.monotonic() - self._last_flush < _FLUSH_EVERY_S:
            return
        self._save()

    def _save(self) -> None:
        """Write the full state atomically.

        If a crash occurs during the write, the old file stays.
        """
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
    """Convert a stored number to a non-negative int, or return ``default`` if it fails."""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if number >= 0 else default


def _as_time(value: object) -> datetime | None:
    """Parse a stored ISO-8601 timestamp, or return ``None`` if it is absent or corrupt.

    The function also reads a timestamp without a time zone as corrupt. The store only
    writes aware UTC timestamps, and a naive timestamp makes the silence arithmetic wrong.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
