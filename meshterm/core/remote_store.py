# SPDX-License-Identifier: Apache-2.0
"""Persistence for remote-node admin state: last-read settings and CLI history.

Reading a repeater's configuration costs one paced mesh round trip per value, so the
repeater-admin screen never bulk-reads on open — it shows what the *last* read (or the
last applied ``set``) said, stamped with when, and refreshes on demand. That cache lives
here, per node (keyed like the admin passwords, by public key), in a small JSON file in
the config directory (``<config_dir>/remote.json``) alongside each node's remote
command-line history — global machine state, like the other JSON stores, deliberately
outside the per-invocation SQLite database.

The file is read into typed records (:class:`_RemoteNode`, each holding its
:class:`CachedValue` settings) and written back out of them, never round-tripped as the
raw JSON it was read as, so a field no record knows is gone after the next write.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from .admin_store import admin_key
from .atomicwrite import write_atomically
from .models import Contact, utcnow

#: How many command-line entries are kept per node (newest last).
HISTORY_CAP = 100


@dataclass(frozen=True, slots=True)
class CachedValue:
    """One remembered remote setting: what the node last said, and when.

    Attributes:
        value: The value as stored text (empty when unsupported).
        read_at: When it was read from (or written to) the node.
        supported: ``False`` when the node answered the read with an error — its firmware
            has no such setting (a board without a front-end module, a build without a
            bridge). Kept apart from *never read*, which is no entry at all.
        discovered: ``True`` for a setting the catalog hasn't got, learned from a command
            the reader ran on *this node's* command line. Such a row reads ``n/a`` like any
            other when the node stops answering it, and is removed only by hand: its key is
            recorded nowhere else, so a misread reply must not be able to delete it.
    """

    value: str
    read_at: datetime | None
    supported: bool = True
    discovered: bool = False

    #: Fields this record once held and must never hold again under another meaning (see
    #: :data:`meshterm.core.preferences.RETIRED` for why a name is never reused).
    RETIRED: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def read(cls, raw: object) -> CachedValue | None:
        """Build a value from one file entry, or ``None`` when it is malformed.

        A supported entry must carry its value; an unsupported one carries none.
        """
        if not isinstance(raw, dict):
            return None
        supported = not raw.get("unsupported", False)
        if supported and "value" not in raw:
            return None
        return cls(
            value=str(raw.get("value", "")),
            read_at=_as_time(raw.get("read_at")),
            supported=supported,
            discovered=bool(raw.get("discovered", False)),
        )

    def to_json(self) -> dict:
        """The entry as written, in the file's long-standing spelling.

        A supported value carries ``value``; an unsupported one says ``unsupported`` and
        carries no value; ``discovered`` appears only when true.
        """
        entry: dict = {"value": self.value} if self.supported else {"unsupported": True}
        entry["read_at"] = self.read_at.isoformat() if self.read_at is not None else None
        if self.discovered:
            entry["discovered"] = True
        return entry


@dataclass(slots=True)
class _RemoteNode:
    """Everything remembered about one remote node.

    Attributes:
        settings: Its cached settings, keyed by CLI parameter name.
        history: Its remote command-line history, oldest first.
    """

    settings: dict[str, CachedValue] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)

    #: Fields this record once held; none yet (see :attr:`CachedValue.RETIRED`).
    RETIRED: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def read(cls, raw: object) -> _RemoteNode:
        """Build a node's record from its file entry, taking only the fields it knows."""
        record = raw if isinstance(raw, dict) else {}
        settings: dict[str, CachedValue] = {}
        raw_settings = record.get("settings")
        if isinstance(raw_settings, dict):
            for key, entry in raw_settings.items():
                value = CachedValue.read(entry)
                if value is not None:
                    settings[str(key)] = value
        raw_history = record.get("history")
        history = [str(c) for c in raw_history] if isinstance(raw_history, list) else []
        return cls(settings=settings, history=history)

    @property
    def empty(self) -> bool:
        """Whether there is nothing left to remember about this node."""
        return not self.settings and not self.history

    def to_json(self) -> dict:
        """The record as written: its own fields and nothing it happened to be read with."""
        return {
            "settings": {key: value.to_json() for key, value in self.settings.items()},
            "history": list(self.history),
        }


class RemoteStore:
    """Reads and writes per-node remote-admin state (setting cache + CLI history)."""

    def __init__(self, path: Path) -> None:
        """Open the store against a JSON file location.

        Args:
            path: Path to the JSON state file (created lazily on first write).
        """
        self._path = path

    def _load(self) -> dict[str, _RemoteNode]:
        """Read every node's record, keyed by :func:`admin_key`; empty on trouble."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): _RemoteNode.read(raw) for key, raw in data.items()}

    def _node(self, node: Contact) -> _RemoteNode:
        """One node's record, or an empty one when nothing is remembered."""
        return self._load().get(admin_key(node)) or _RemoteNode()

    # -- the settings cache -------------------------------------------------------

    def settings(self, node: Contact) -> dict[str, CachedValue]:
        """Every remembered setting for ``node``, keyed by CLI parameter name."""
        return dict(self._node(node).settings)

    def remember_setting(self, node: Contact, key: str, value: str) -> None:
        """Cache one setting's value for ``node``, stamped now."""
        self._put(node, key, CachedValue(value=value, read_at=utcnow()))

    def remember_discovered(self, node: Contact, key: str, value: str) -> None:
        """Cache a setting the catalog hasn't got, which ``node`` has just proved it has."""
        self._put(node, key, CachedValue(value=value, read_at=utcnow(), discovered=True))

    def remember_unsupported(self, node: Contact, key: str) -> None:
        """Record that ``node`` answered a read of ``key`` with an error, stamped now."""
        self._put(node, key, CachedValue(value="", read_at=utcnow(), supported=False))

    def forget_setting(self, node: Contact, key: str) -> None:
        """Drop one cached setting, so the row reads as never read."""
        records = self._load()
        record = records.get(admin_key(node))
        if record is not None and record.settings.pop(key, None) is not None:
            self._write(records)

    def _put(self, node: Contact, key: str, value: CachedValue) -> None:
        """Store one setting's cache entry for ``node``, keeping it discovered if it was.

        A discovered row is refreshed by the same reads and writes as any other — through
        :meth:`remember_setting` — and that must not quietly demote it to a catalog row it
        has no entry for, which would leave a row nothing draws.
        """
        records = self._load()
        record = records.setdefault(admin_key(node), _RemoteNode())
        previous = record.settings.get(key)
        if previous is not None and previous.discovered:
            value = replace(value, discovered=True)
        record.settings[key] = value
        self._write(records)

    # -- the command-line history ---------------------------------------------------

    def history(self, node: Contact) -> list[str]:
        """The node's remote CLI history, oldest first."""
        return list(self._node(node).history)

    def append_history(self, node: Contact, command: str) -> None:
        """Append one sent command to the node's history (dropping an adjacent dupe)."""
        command = command.strip()
        if not command:
            return
        records = self._load()
        record = records.setdefault(admin_key(node), _RemoteNode())
        if record.history and record.history[-1] == command:
            return  # re-running the last command shouldn't stutter the recall
        record.history.append(command)
        del record.history[:-HISTORY_CAP]
        self._write(records)

    def _write(self, records: dict[str, _RemoteNode]) -> None:
        """Persist ``records`` atomically (crash mid-write keeps the previous file)."""
        data = {key: record.to_json() for key, record in records.items() if not record.empty}
        write_atomically(self._path, json.dumps(data, indent=2))


def _as_time(value: object) -> datetime | None:
    """Parse a stored ISO-8601 timestamp, or ``None`` if absent or corrupt."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
