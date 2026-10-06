# SPDX-License-Identifier: Apache-2.0
"""The region table and the region CLI of a repeater, as the simulator gives them.

The :class:`~meshterm.core.connection.MockDevice` answers ``region …`` commands and the
anonymous regions request from one of these maps for each simulated repeater. Thus you can
use the node page, the region editor, and the ``regions`` command under ``--mock``.

This module is a copy of the firmware, not an idea of it: ``RegionMap`` and
``CommonCLI::handleRegionCmd`` as of MeshCore 1.15/1.16. The parser that reads its replies
(:mod:`~meshterm.core.region_admin`) is for the real firmware. If a simulator answers more
kindly than a repeater, a bug in the parser can go through without detection. Thus this
module keeps the difficult edge cases of the firmware on purpose:

- the 160-byte reply buffer, which cuts a long dump in the middle of a name,
- the flat lists, which skip a name that does not fit,
- prefix matching on ``allowf``/``denyf``/``home``/``get``,
- ``put`` of a name that exists, which moves that name,
- ``remove``, which refuses a region with children,
- new regions, which allow floods,
- ``default``, which makes a missing region and saves the table,
- all the other edits, which stay in RAM until ``save``
  (:meth:`SimulatedRegionMap.reboot` puts the saved table back).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

from .region_admin import REPLY_CAP_BYTES
from .regions import WILDCARD

#: The size of the table in the firmware (``MAX_REGION_ENTRIES``).
MAX_REGIONS = 32


@dataclass(slots=True)
class _Entry:
    """One ``RegionEntry``: an id, the id of its parent, the deny-flood flag, and the name."""

    id: int
    parent: int
    deny_flood: bool
    name: str


class SimulatedRegionMap:
    """The region table of one simulated repeater, and the CLI verbs that edit it."""

    def __init__(self, tree: list[tuple[str, str, bool]] | None = None) -> None:
        """Make a table, already saved, from ``(name, parent, flood)`` triples in order.

        Args:
            tree: The regions, each after its parent (``*`` for the top level). ``None`` is
                an empty table: a stock repeater that relays unscoped floods and nothing
                else.
        """
        self._wild_deny = False
        self._entries: list[_Entry] = []
        self._next_id = 1
        self._home = 0
        self._default = 0
        for name, parent, flood in tree or ():
            parent_id = 0 if parent == WILDCARD else self._by_name(parent).id  # type: ignore[union-attr]
            entry = self._put(name, parent_id)
            assert entry is not None
            entry.deny_flood = not flood
        self._saved = self._snapshot()

    # -- the RegionMap ------------------------------------------------------------

    def _snapshot(self) -> tuple:
        return copy.deepcopy(
            (self._wild_deny, self._entries, self._next_id, self._home, self._default)
        )

    def reboot(self) -> None:
        """Remove all the unsaved edits, as a power cycle does."""
        (self._wild_deny, self._entries, self._next_id, self._home, self._default) = copy.deepcopy(
            self._saved
        )

    def _by_name(self, name: str) -> _Entry | None:
        """``findByName``: an exact match (``None`` for the wildcard ``*``)."""
        name = name.removeprefix("#")
        return next((e for e in self._entries if e.name.removeprefix("#") == name), None)

    def _by_prefix(self, prefix: str) -> _Entry | str | None:
        """``findByNamePrefix``: an exact match, else the last prefix match (``*`` → ``"*"``)."""
        if prefix == WILDCARD:
            return WILDCARD
        prefix = prefix.removeprefix("#")
        partial = None
        for entry in self._entries:
            bare = entry.name.removeprefix("#")
            if bare == prefix:
                return entry
            if bare.startswith(prefix):
                partial = entry
        return partial

    def _put(self, name: str, parent_id: int) -> _Entry | None:
        """``putRegion``: refuse bad characters, move a name that exists, else add at the end."""
        if not all(ch in "-$#" or ch.isdigit() or ord(ch) >= ord("A") for ch in name):
            return None
        entry = self._by_name(name)
        if entry is not None:
            if entry.id == parent_id:
                return None
            entry.parent = parent_id
            return entry
        if len(self._entries) >= MAX_REGIONS:
            return None
        entry = _Entry(id=self._next_id, parent=parent_id, deny_flood=True, name=name)
        self._next_id += 1
        self._entries.append(entry)
        return entry

    def _dump(self) -> str:
        """``exportTo(reply, 160)``: the tree, cut where the buffer is full."""
        out: list[str] = []

        def visit(indent: int, entry_id: int, name: str, deny: bool) -> None:
            home = "^" if entry_id == self._home else ""
            out.append(
                " " * indent + f"{name.removeprefix('#')}{home}" + ("" if deny else " F") + "\n"
            )
            for child in self._entries:
                if child.parent == entry_id:
                    visit(indent + 1, child.id, child.name, child.deny_flood)

        visit(0, 0, WILDCARD, self._wild_deny)
        return _capped("".join(out))

    def _names(self, *, allowed: bool) -> str:
        """``exportNamesTo(reply, 160, …)``: each name that fits, with commas between."""
        parts: list[str] = []
        length = 0
        if self._wild_deny != allowed:
            parts.append(WILDCARD)
            length = 2
        for entry in self._entries:
            if entry.deny_flood == allowed:
                continue
            bare = entry.name.removeprefix("#")
            size = len(bare.encode("utf-8"))
            if length + size + 2 < REPLY_CAP_BYTES:
                parts.append(bare)
                length += size + 1
        return ",".join(parts)

    def allowed_names(self) -> str:
        """The answer to the anonymous regions request (``*`` first if ``*`` allows floods)."""
        return self._names(allowed=True)

    # -- the CLI ------------------------------------------------------------------

    def command(self, text: str) -> str:
        """Answer one ``region …`` command as ``handleRegionCmd`` does."""
        parts = text.split()
        n = len(parts)
        if n == 1:
            return self._dump()
        verb = parts[1]
        if verb == "save":
            self._saved = self._snapshot()
            return "OK"
        if verb in ("allowf", "denyf") and n >= 3:
            found = self._by_prefix(parts[2])
            if found is None:
                return "Err - unknown region"
            if found == WILDCARD:
                self._wild_deny = verb == "denyf"
            else:
                found.deny_flood = verb == "denyf"  # type: ignore[union-attr]
            return "OK"
        if verb == "get" and n >= 3:
            found = self._by_prefix(parts[2])
            if found is None:
                return "Err - unknown region"
            if found == WILDCARD:
                return f" * {'' if self._wild_deny else 'F'}"
            parent = next((e for e in self._entries if e.id == found.parent), None)  # type: ignore[union-attr]
            flag = "" if found.deny_flood else "F"  # type: ignore[union-attr]
            if parent is not None:
                return f" {found.name} ({parent.name}) {flag}"  # type: ignore[union-attr]
            return f" {found.name} {flag}"  # type: ignore[union-attr]
        if verb == "home":
            if n == 2:
                home = next((e.name for e in self._entries if e.id == self._home), WILDCARD)
                return f" home is {home}"
            found = self._by_prefix(parts[2])
            if found is None:
                return "Err - unknown region"
            self._home = 0 if found == WILDCARD else found.id  # type: ignore[union-attr]
            return f" home is now {WILDCARD if found == WILDCARD else found.name}"  # type: ignore[union-attr]
        if verb == "default":
            if n == 2:
                current = next((e.name for e in self._entries if e.id == self._default), None)
                return f" default scope is {current or '<null>'}"
            if parts[2] == "<null>":
                self._default = 0
                self._saved = self._snapshot()
                return " default scope is now <null>"
            found = self._by_prefix(parts[2])
            if found == WILDCARD:
                self._wild_deny = False
                self._default = 0
                self._saved = self._snapshot()
                return " default scope is now *"
            if found is None:
                found = self._put(parts[2], 0)
            if found is None:
                return "Err - region table full"
            found.deny_flood = False  # type: ignore[union-attr]
            self._default = found.id  # type: ignore[union-attr]
            self._saved = self._snapshot()
            return f" default scope is now {found.name}"  # type: ignore[union-attr]
        if verb == "put" and n >= 3:
            if n >= 4:
                parent = self._by_prefix(parts[3])
                if parent is None:
                    return "Err - unknown parent"
                parent_id = 0 if parent == WILDCARD else parent.id  # type: ignore[union-attr]
            else:
                parent_id = 0
            entry = self._put(parts[2], parent_id)
            if entry is None:
                return "Err - unable to put"
            entry.deny_flood = False
            return "OK - (flood allowed)"
        if verb == "remove" and n >= 3:
            entry = self._by_name(parts[2])
            if entry is None:
                return "Err - not found"
            if any(e.parent == entry.id for e in self._entries):
                return "Err - not empty"
            self._entries.remove(entry)
            return "OK"
        if verb == "list" and n >= 3:
            if parts[2] not in ("allowed", "denied"):
                return "Err - use 'allowed' or 'denied'"
            return self._names(allowed=parts[2] == "allowed") or "-none-"
        return "Err - ??"


def _capped(text: str) -> str:
    """``BufStream``: remove the bytes after the 159th, whatever the cut divides."""
    raw = text.encode("utf-8")[: REPLY_CAP_BYTES - 1]
    return raw.decode("utf-8", errors="ignore")
