# SPDX-License-Identifier: Apache-2.0
"""The places that the user typed for nodes.

MeshTerm finds the place of a node from its location (refer to :mod:`meshterm.core.places`).
But the user can know better: a repeater on the roof of a known building, or a node whose
location is far from where it really is. The node page lets the user type a place for the
node, and this store keeps it. A typed place wins over each found place, on each screen.

The store finds a place by the key prefix of the node (12 hex digits, the same id as
``observations.node``). A place belongs to a node and not to a contact: a repeater in a path
is often no contact of the device. The file is ``<config_dir>/places.json``, global machine
state like the other choices of the user. After the first read, the store answers reads from
memory, because the Contacts list and each path line read it at each paint. A write occurs
only when the user changes a place, and the write is atomic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .atomicwrite import write_atomically


def _norm(node: str) -> str:
    """The 12-hex id of a node, from a key prefix or a full public key."""
    return node.lower().removeprefix("0x")[:12]


@dataclass(frozen=True, slots=True)
class TypedPlace:
    """The place that the user typed for one node.

    Attributes:
        name: The place, as the user typed it.
        set_at: When the user typed it (seconds since the epoch).
    """

    name: str
    set_at: int

    #: The names that left this record. A retired name is never used again with a new
    #: meaning (refer to :data:`meshterm.core.preferences.RETIRED` for the reason).
    RETIRED: ClassVar[frozenset[str]] = frozenset()


class PlaceStore:
    """Reads and writes the places that the user typed, from memory first."""

    def __init__(self, path: Path) -> None:
        """Open the store on the location of a JSON file.

        Args:
            path: The path to the JSON state file. The store makes the file at the first
                typed place.
        """
        self._path = path
        self._places: dict[str, TypedPlace] | None = None

    @property
    def _state(self) -> dict[str, TypedPlace]:
        """The typed places by node id, read from disk at the first access."""
        if self._places is None:
            self._places = self._load()
        return self._places

    def _load(self) -> dict[str, TypedPlace]:
        """Parse the file into typed records (empty for a bad or missing file)."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        entries = data.get("places") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return {}
        places: dict[str, TypedPlace] = {}
        for node, entry in entries.items():
            if not isinstance(node, str) or not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            try:
                set_at = int(entry.get("set_at") or 0)
            except (TypeError, ValueError):
                set_at = 0
            places[_norm(node)] = TypedPlace(name.strip(), set_at)
        return places

    def get(self, node: str | None) -> str | None:
        """The place that the user typed for a node, or ``None``.

        Args:
            node: The key prefix or the full public key of the node.
        """
        if not node:
            return None
        typed = self._state.get(_norm(node))
        return typed.name if typed else None

    def all(self) -> dict[str, str]:
        """Each typed place by node id, as a copy that the caller can keep."""
        return {node: typed.name for node, typed in self._state.items()}

    def set(self, node: str, name: str | None, *, when: int) -> None:
        """Type a place for a node, or clear it with ``None`` or an empty name.

        Args:
            node: The key prefix or the full public key of the node.
            name: The place. ``None`` or blank text clears the typed place, and the found
                place shows again.
            when: The time of the change (seconds since the epoch).
        """
        key = _norm(node)
        name = (name or "").strip()
        current = self._state.get(key)
        if not name:
            if current is None:
                return
            del self._state[key]
        else:
            if current is not None and current.name == name:
                return
            self._state[key] = TypedPlace(name, when)
        self._save()

    def _save(self) -> None:
        """Store all the typed places atomically, from the records."""
        data = {
            "places": {
                node: {"name": typed.name, "set_at": typed.set_at}
                for node, typed in sorted(self._state.items())
            }
        }
        write_atomically(self._path, json.dumps(data, indent=2, ensure_ascii=False))
