# SPDX-License-Identifier: Apache-2.0
"""The store for the admin state of remote nodes: the last-read settings and the CLI history.

A read of the settings of a repeater costs one paced round trip on the mesh for each value.
Thus the repeater-admin screen never reads all the values when it opens. It shows what the
last read (or the last applied ``set``) said, with its time, and it reads again when the
user asks.

That cache is here, for each node, in a small JSON file in the config directory
(``<config_dir>/remote.json``). The key is the public key of the node, the same as for the
admin passwords. The same file holds the remote command-line history of each node. This is
global machine state, the same as the other JSON stores. It is intentionally outside the
SQLite database, which can be different for each run.

The file is read into typed records (:class:`_RemoteNode`, each with its
:class:`CachedValue` settings) and written back from them. The store never writes back the
raw JSON that it read. Thus a field that no record knows is removed after the next write.
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

#: How many command-line entries the store keeps for each node (newest last).
HISTORY_CAP = 100


@dataclass(frozen=True, slots=True)
class CachedValue:
    """One remembered remote setting: what the node said last, and when.

    Attributes:
        value: The value as stored text (empty when the node does not support the
            setting).
        read_at: When it was read from the node (or written to it).
        supported: ``False`` when the node answered the read with an error: its firmware
            has no such setting (a board without a front-end module, a build without a
            bridge). This is separate from "never read", which is no entry at all.
        discovered: ``True`` for a setting that the catalog does not have. MeshTerm
            learned it from a command that the user ran on the command line of this node.
            When the node no longer answers it, such a row reads ``n/a``, the same as each
            other row. Only the user can remove it, by hand, because its key is stored
            nowhere else. Thus a reply that MeshTerm reads incorrectly must not be able to
            delete it.
    """

    value: str
    read_at: datetime | None
    supported: bool = True
    discovered: bool = False

    #: The fields that this record held before. It must never hold them again with a
    #: different meaning (refer to :data:`meshterm.core.preferences.RETIRED` for the
    #: reason that a name is never used again).
    RETIRED: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def read(cls, raw: object) -> CachedValue | None:
        """Build a value from one file entry, or ``None`` when the entry is malformed.

        A supported entry must have its value. An unsupported entry has no value.
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
        """The entry as written, in the spelling that the file always used.

        A supported value has ``value``. An unsupported value has ``unsupported`` and no
        value. ``discovered`` is present only when it is true.
        """
        entry: dict = {"value": self.value} if self.supported else {"unsupported": True}
        entry["read_at"] = self.read_at.isoformat() if self.read_at is not None else None
        if self.discovered:
            entry["discovered"] = True
        return entry


@dataclass(slots=True)
class _RemoteNode:
    """All that MeshTerm remembers about one remote node.

    Attributes:
        settings: Its cached settings, with the CLI parameter name as the key.
        history: Its remote command-line history, oldest first.
    """

    settings: dict[str, CachedValue] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)

    #: The fields that this record held before. There are none yet (refer to
    #: :attr:`CachedValue.RETIRED`).
    RETIRED: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def read(cls, raw: object) -> _RemoteNode:
        """Build the record of a node from its file entry, with only the fields it knows."""
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
        """Whether nothing remains to remember about this node."""
        return not self.settings and not self.history

    def to_json(self) -> dict:
        """The record as written: its own fields only, not the other fields of the read entry."""
        return {
            "settings": {key: value.to_json() for key, value in self.settings.items()},
            "history": list(self.history),
        }


class RemoteStore:
    """Reads and writes the remote-admin state of each node (setting cache and CLI history)."""

    def __init__(self, path: Path) -> None:
        """Open the store on the location of a JSON file.

        Args:
            path: Path to the JSON state file (made only at the first write).
        """
        self._path = path

    def _load(self) -> dict[str, _RemoteNode]:
        """Read the records of all the nodes by :func:`admin_key` (empty at a problem)."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): _RemoteNode.read(raw) for key, raw in data.items()}

    def _node(self, node: Contact) -> _RemoteNode:
        """The record of one node, or an empty record when nothing is remembered."""
        return self._load().get(admin_key(node)) or _RemoteNode()

    # -- the settings cache -------------------------------------------------------

    def settings(self, node: Contact) -> dict[str, CachedValue]:
        """All the remembered settings for ``node``, with the CLI parameter name as the key."""
        return dict(self._node(node).settings)

    def remember_setting(self, node: Contact, key: str, value: str) -> None:
        """Cache the value of one setting for ``node``, with the time now."""
        self._put(node, key, CachedValue(value=value, read_at=utcnow()))

    def remember_discovered(self, node: Contact, key: str, value: str) -> None:
        """Cache a setting that is not in the catalog, which ``node`` proved that it has."""
        self._put(node, key, CachedValue(value=value, read_at=utcnow(), discovered=True))

    def remember_unsupported(self, node: Contact, key: str) -> None:
        """Store that ``node`` answered a read of ``key`` with an error, with the time now."""
        self._put(node, key, CachedValue(value="", read_at=utcnow(), supported=False))

    def forget_setting(self, node: Contact, key: str) -> None:
        """Remove one cached setting, so that the row reads as never read."""
        records = self._load()
        record = records.get(admin_key(node))
        if record is not None and record.settings.pop(key, None) is not None:
            self._write(records)

    def _put(self, node: Contact, key: str, value: CachedValue) -> None:
        """Store the cache entry of one setting for ``node``, and keep the discovered flag.

        The same reads and writes as for each other row change a discovered row (through
        :meth:`remember_setting`). That change must not quietly make it a catalog row,
        because the catalog has no entry for it. If that occurs, nothing draws the row.
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
        """The remote CLI history of the node, oldest first."""
        return list(self._node(node).history)

    def append_history(self, node: Contact, command: str) -> None:
        """Add one sent command to the history of the node (not a repeat of the last command)."""
        command = command.strip()
        if not command:
            return
        records = self._load()
        record = records.setdefault(admin_key(node), _RemoteNode())
        if record.history and record.history[-1] == command:
            return  # a repeat of the last command must not add a duplicate to the recall
        record.history.append(command)
        del record.history[:-HISTORY_CAP]
        self._write(records)

    def _write(self, records: dict[str, _RemoteNode]) -> None:
        """Write ``records`` atomically.

        If a crash occurs during the write, the previous file stays.
        """
        data = {key: record.to_json() for key, record in records.items() if not record.empty}
        write_atomically(self._path, json.dumps(data, indent=2))


def _as_time(value: object) -> datetime | None:
    """Parse a stored ISO-8601 timestamp, or return ``None`` if it is absent or corrupt."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
