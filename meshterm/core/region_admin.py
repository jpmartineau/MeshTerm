# SPDX-License-Identifier: Apache-2.0
"""The region table of a repeater as its CLI shows it: the parser, the commands, the edits.

A MeshCore repeater keeps a small table of regions (a maximum of 32) in a tree below the
wildcard ``*``. It relays a scoped flood only when the region of the flood is a region that
it lists as flood-allowed (refer to :mod:`~meshterm.core.regions` for the maths that
connects a flood to its region). The tree is only for organization: a repeater relays only
the regions that it lists as allowed. Thus, when ``lakeside`` is allowed, this tells
nothing about ``lakeside-north`` below it. The flag of the wildcard itself is the unscoped
case: it tells whether the repeater relays plain floods.

The admin reads and edits this table through the remote CLI (firmware
``CommonCLI::handleRegionCmd``, ``RegionMap``). This module holds all the parts of this
work that are not a screen:

* **The dump** (:func:`parse_region_dump`). The ``region`` command replies with the tree,
  with one region on each line. Each line has an indent of one space for each level below
  the wildcard. After the name, ``^`` marks the home region, and `` F`` marks a region
  where floods are allowed::

      *^ F
       lakeside F
        lakeside-north F
        lakeside-south
       harbour F

* **The cut.** The firmware writes each CLI reply into a 160-byte buffer
  (``exportTo(reply, 160)``). The dump stops where the buffer ends: in the middle of a line
  or of a name, and nothing tells that it stopped. A table of 32 regions with real names
  does not fit. Thus MeshTerm reads a dump near the cap as possibly cut
  (:data:`REPLY_CAP_BYTES`, :func:`parse_region_dump`). It removes the incomplete last
  line, and does not show it as a region with half a name. Then it tells the user. The
  flat lists (``region list allowed`` / ``region list denied``) let MeshTerm get the
  remaining regions (:func:`parse_name_list`, :func:`RegionTable.with_lists`). These lists
  give no parent, so MeshTerm shows these regions after the cut, with no position in the
  tree. The lists also have a cap. A list skips a name that does not fit, and does not cut
  it. Thus MeshTerm also flags a list that is so near the cap that it possibly skipped a
  name (:func:`list_may_be_incomplete`). No part of this module claims to hold the full
  table when it cannot know.

* **The commands** (:func:`allow_command` and the related functions). Each edit is one
  remote command, and this module spells each command only once. On the firmware,
  ``allowf``/``denyf``/``home`` find a name by its prefix, and an exact match has priority.
  MeshTerm sends only names that the repeater itself listed. Thus the exact match always
  wins.

* **The edits** (:meth:`RegionTable.after`). This part applies the effect of a command that
  the repeater accepted to the table. Thus the editor draws the table again from the reply,
  and does not use one more round trip to read the table again. On the firmware,
  ``region put`` with a name that exists already moves that region. For this reason, the
  editor refuses a name that it can see is in use.

* **The save.** Each edit stays in the RAM of the repeater until ``region save``. A reboot
  cancels each edit that was not saved. The only exception is ``region default`` (firmware
  1.15+), which saves the full table itself. Its reply (``default scope is now …``) shows
  that the command also saved the unsaved edits (:func:`saves_table`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from .regions import MAX_NAME_BYTES, WILDCARD, RegionNameError, validate

#: The size of the CLI reply buffer of the firmware, in bytes. ``BufStream`` stops one
#: byte before the end (for the NUL). Thus a dump that has no more space arrives with a
#: length of 159 bytes.
REPLY_CAP_BYTES = 160

#: How near the cap a dump must be for MeshTerm to read it as possibly cut. A cut can
#: occur in a multi-byte UTF-8 name, and the decode before the dump gets here can make it
#: some bytes shorter. Also, the transport can remove a trailing newline. Thus the test has
#: a margin for these two effects, and does not ask for exactly 159.
_CUT_SLACK = 4

#: The command that dumps the tree.
DUMP_COMMAND = "region"
#: The command that asks which region is the default scope (firmware 1.15+).
DEFAULT_QUERY = "region default"
#: The two flat lists: all the flood-allowed names and all the flood-denied names, ``*``
#: included.
LIST_ALLOWED = "region list allowed"
LIST_DENIED = "region list denied"
#: The command that writes the table to flash. No other data stays after a reboot.
SAVE_COMMAND = "region save"

#: The token that ``region default`` takes to clear the default scope.
NULL_DEFAULT = "<null>"

#: The text that the repeater shows for an empty flat list.
_EMPTY_LIST = "-none-"


# -- the table -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegionRow:
    """One region in the table of a repeater.

    Attributes:
        name: The bare region name (``*`` for the wildcard).
        parent: The name of the parent. It is ``*`` for a top-level region. It is ``None``
            for the wildcard itself, and for a region that MeshTerm got from a flat list,
            whose position is not known.
        depth: The number of levels below the wildcard (0 for the wildcard, 1 for a
            top-level region).
        flood: Whether the repeater relays floods scoped to this region (``F``). For the
            wildcard, whether it relays unscoped floods.
        home: Whether it is the home region of the repeater (``^``).
        placed: ``False`` for a region that MeshTerm knows only from a flat list. The
            region is in the table, but the dump was cut before it. Thus its parent and
            depth are not known.
    """

    name: str
    parent: str | None
    depth: int
    flood: bool
    home: bool = False
    placed: bool = True

    @property
    def wildcard(self) -> bool:
        """Whether this row is the wildcard: the unscoped case, not a region."""
        return self.name == WILDCARD


@dataclass(frozen=True, slots=True)
class RegionTable:
    """What MeshTerm knows about the region table of one repeater.

    Attributes:
        rows: The wildcard first (when the dump got to it). Then each region in dump order
            (depth-first, as the firmware prints the tree). Then the regions that MeshTerm
            got from the flat lists after a cut (``placed=False``).
        cut: Whether the dump was possibly cut at the reply cap. If so, there can be rows
            after ``cut_after`` that the tree does not show.
        cut_after: The last region that the dump showed complete (``None`` when it showed
            no region).
        lists_read: Whether MeshTerm read the flat lists to get the regions that the cut
            hid.
        lists_partial: Whether one of the flat lists came so near its own cap that it
            possibly skipped a name. If so, the set that MeshTerm got can still be
            incomplete.
        default: The default-scope region, or ``None`` if there is none. If MeshTerm does
            not know the default, :attr:`default_known` is ``False`` (older firmware does
            not have this query).
        default_known: Whether MeshTerm read ``default`` from the repeater.
    """

    rows: tuple[RegionRow, ...] = ()
    cut: bool = False
    cut_after: str | None = None
    lists_read: bool = False
    lists_partial: bool = False
    default: str | None = None
    default_known: bool = False

    # -- read the table -----------------------------------------------------------

    def get(self, name: str) -> RegionRow | None:
        """The row for ``name``, or ``None`` when the known table has no such region."""
        return next((row for row in self.rows if row.name == name), None)

    @property
    def wildcard(self) -> RegionRow | None:
        """The row of the wildcard, or ``None`` when the dump did not get to it."""
        return self.get(WILDCARD)

    def regions(self) -> list[RegionRow]:
        """All the region rows, without the wildcard."""
        return [row for row in self.rows if not row.wildcard]

    def children(self, name: str) -> list[RegionRow]:
        """The regions directly below ``name`` (only placed rows: no other row has a parent)."""
        return [row for row in self.rows if row.parent == name and not row.wildcard]

    @property
    def home(self) -> str | None:
        """The name of the home region (``*`` when none was set), or ``None`` when not shown."""
        return next((row.name for row in self.rows if row.home), None)

    @property
    def complete(self) -> bool:
        """Whether each region that the repeater holds is here (as far as MeshTerm can tell).

        A clean dump is complete. A cut dump is complete again after MeshTerm read the two
        flat lists, if neither list came near its own cap. Then some parts of the tree are
        still not placed, but no name is missing.
        """
        return not self.cut or (self.lists_read and not self.lists_partial)

    def carried(self) -> list[str]:
        """The scopes for which the repeater relays floods, as the regions request lists them.

        ``*`` comes first when the repeater relays unscoped floods. Then each flood-allowed
        region follows. This is the exact shape that
        :meth:`~meshterm.core.region_store.RegionStore.learn_carried` takes.
        """
        wild = self.wildcard
        head = [WILDCARD] if wild is not None and wild.flood else []
        return head + [row.name for row in self.regions() if row.flood]

    # -- edit the table -----------------------------------------------------------

    def after(self, command: str, reply: str) -> RegionTable:
        """The table after the repeater accepted ``command``.

        The method understands only the verbs that this module spells. For all other
        commands, and for a refused reply (refer to :func:`region_refused`), it returns the
        table with no change. Thus a caller can send each accepted reply through this
        method, and does not have to find first what the reply was.

        Args:
            command: The command that was sent, as this module built it.
            reply: The reply of the repeater to the command.

        Returns:
            The updated table (a new object, because tables are immutable).
        """
        if region_refused(reply):
            return self
        parts = command.split()
        if len(parts) < 2 or parts[0] != "region":
            return self
        verb, args = parts[1], parts[2:]
        if verb in ("allowf", "denyf") and args:
            return self._with(args[0], flood=verb == "allowf")
        if verb == "home" and args:
            if self.get(args[0]) is None:
                return self
            rows = tuple(replace(row, home=row.name == args[0]) for row in self.rows)
            return replace(self, rows=rows)
        if verb == "default" and args:
            if args[0] == NULL_DEFAULT:
                return replace(self, default=None, default_known=True)
            # The firmware sets flood on for the default region. If the region does not
            # exist, the firmware makes it at the top level.
            table = self if self.get(args[0]) else self._put(args[0], WILDCARD, flood=True)
            return replace(table._with(args[0], flood=True), default=args[0], default_known=True)
        if verb == "put" and args:
            parent = args[1] if len(args) > 1 else WILDCARD
            # Firmware 1.15+ replies "OK - (flood allowed)". Older firmware made it denied.
            return self._put(args[0], parent, flood="flood allowed" in reply.lower())
        if verb == "remove" and args:
            if self.children(args[0]):
                return self  # the firmware refuses this, thus a reply that says otherwise is wrong
            rows = tuple(row for row in self.rows if row.name != args[0])
            default = None if self.default == args[0] else self.default
            return replace(self, rows=rows, default=default)
        return self

    def _with(self, name: str, **changes: object) -> RegionTable:
        """The table with changed fields in one row (no change when the row is not here)."""
        rows = tuple(replace(row, **changes) if row.name == name else row for row in self.rows)
        return replace(self, rows=rows)

    def _put(self, name: str, parent: str, *, flood: bool) -> RegionTable:
        """The table with a new region placed as the last child of ``parent``.

        The dump order is depth-first. Thus the new row goes after the last descendant of
        the parent. This is exactly where the next ``region`` dump will print it.
        """
        if self.get(name) is not None:
            return self
        above = self.get(parent)
        depth = (above.depth + 1) if above is not None and above.placed else 1
        row = RegionRow(name=name, parent=parent, depth=depth, flood=flood)
        rows = list(self.rows)
        at = len(rows)
        if above is not None and above.placed:
            at = rows.index(above) + 1
            while at < len(rows) and rows[at].placed and rows[at].depth > above.depth:
                at += 1
        else:
            # A parent that is not placed has no subtree for the new row. Keep the placed
            # rows together, and put the new row before the rows that are not placed.
            at = next((i for i, r in enumerate(rows) if not r.placed), len(rows))
        rows.insert(at, row)
        return replace(self, rows=tuple(rows))

    def with_lists(self, allowed: str | None, denied: str | None) -> RegionTable:
        """Add the two flat lists to a cut table, to get back the regions that the cut hid.

        If the tree did not show a name that is in a list, the name goes into the table as
        not placed, with the flood flag that its list gives. A name that the tree has
        already keeps its tree row, because the tree has the parent that the list does not
        have. The flag of the wildcard comes from the lists when the tree did not get to the
        wildcard. (The tree always gets to it, because it is the first line, unless the
        reply was empty.)

        Args:
            allowed: The reply to :data:`LIST_ALLOWED` (``None`` when it did not come).
            denied: The reply to :data:`LIST_DENIED` (``None`` when it did not come).

        Returns:
            The table, with the rows that MeshTerm got back added at the end.
            ``lists_read`` is set only when the two lists replied. ``lists_partial`` is set
            when one of the lists possibly skipped a name.
        """
        rows = list(self.rows)
        partial = False
        for reply, flood in ((allowed, True), (denied, False)):
            if reply is None or region_refused(reply):
                continue
            partial = partial or list_may_be_incomplete(reply)
            for name in parse_name_list(reply):
                if any(row.name == name for row in rows):
                    continue
                if name == WILDCARD:
                    rows.insert(0, RegionRow(name=WILDCARD, parent=None, depth=0, flood=flood))
                else:
                    rows.append(
                        RegionRow(name=name, parent=None, depth=1, flood=flood, placed=False)
                    )
        both = (
            allowed is not None
            and denied is not None
            and not region_refused(allowed)
            and not region_refused(denied)
        )
        return replace(self, rows=tuple(rows), lists_read=both, lists_partial=partial)

    def with_default(self, reply: str | None) -> RegionTable:
        """The table with the default scope read from a :data:`DEFAULT_QUERY` reply.

        The table does not change (and ``default_known`` stays ``False``) when there was no
        reply, or when the firmware does not know the query. Firmware before 1.15 replies to
        it with its general error, ``Err - ??``.
        """
        parsed = parse_default_reply(reply)
        if parsed is None:
            return self
        return replace(self, default=parsed or None, default_known=True)


# -- the parser ------------------------------------------------------------------------


def dump_may_be_cut(text: str) -> bool:
    """Whether a reply came so near the 160-byte cap that it was possibly cut."""
    return len(text.encode("utf-8")) >= REPLY_CAP_BYTES - 1 - _CUT_SLACK


def parse_region_dump(text: str | None) -> RegionTable:
    """Parse a ``region`` dump into a table, and remove a line that the reply cap cut in half.

    Each line that is not blank is one region. Its leading spaces give its depth. Then come
    the name, an optional ``^`` (home), and an optional ``F`` (flood allowed) after a space.
    The parent of a line is the nearest line above it at one level up. Depth 0 is the
    wildcard. If a line does not agree with the grammar, or if its indent skips a level,
    the parser does not guess. The tree ends at that line, and the table is marked as cut,
    because a dump that does not make sense from that point is no longer the dump.

    Args:
        text: The reply text (``None`` or an empty text gives an empty table that is not
            cut).

    Returns:
        The table. When the reply came near the cap, the parser removes its last line,
        unless the reply ended with a newline (then the line is known to be complete).
        Also, ``cut`` is set, and ``cut_after`` names the last region shown.
    """
    if not text:
        return RegionTable()
    raw = text.replace("\r", "").replace("\x00", "")
    cut = dump_may_be_cut(raw)
    lines = raw.split("\n")
    if cut and not raw.endswith("\n") and lines:
        lines = lines[:-1]  # the buffer ended in this line, so a name can be half a name
    rows: list[RegionRow] = []
    stack: list[str] = []  # the open ancestor at each depth
    for line in lines:
        if not line.strip():
            continue
        depth = len(line) - len(line.lstrip(" "))
        row = _parse_line(line.strip(), depth, stack)
        if row is None:
            cut = True
            break
        del stack[depth:]
        stack.append(row.name)
        rows.append(row)
    cut_after = next((row.name for row in reversed(rows)), None) if cut else None
    return RegionTable(rows=tuple(rows), cut=cut, cut_after=cut_after)


def _parse_line(body: str, depth: int, stack: list[str]) -> RegionRow | None:
    """One dump line, or ``None`` if it breaks the grammar (refer to :func:`parse_region_dump`)."""
    tokens = body.split()
    if not tokens or len(tokens) > 2 or (len(tokens) == 2 and tokens[1] != "F"):
        return None
    name = tokens[0]
    home = name.endswith("^")
    name = name.removesuffix("^")
    if not name or depth > len(stack):
        return None
    if depth == 0 and name != WILDCARD:
        return None
    if depth > 0 and name == WILDCARD:
        return None
    parent = stack[depth - 1] if depth > 0 else None
    return RegionRow(name=name, parent=parent, depth=depth, flood=len(tokens) == 2, home=home)


def parse_name_list(text: str | None) -> list[str]:
    """The names in a ``region list allowed|denied`` reply (``-none-`` gives an empty list)."""
    if not text or text.strip() == _EMPTY_LIST:
        return []
    names: list[str] = []
    for part in text.replace("\x00", "").split(","):
        name = part.strip().removeprefix("#")
        if name and name not in names:
            names.append(name)
    return names


def list_may_be_incomplete(text: str) -> bool:
    """Whether a flat list came so near its cap that it possibly skipped a name.

    The firmware adds a name only while ``length + len(name) + 2 < 160``, and skips a name
    that does not fit. Then it continues with the next name, so a short name that comes
    later can still go in. Thus a skip occurs only when the list has a minimum length of
    ``158 - len(name)`` bytes, for a name with a maximum length of
    :data:`~meshterm.core.regions.MAX_NAME_BYTES`. A list shorter than that skipped no name.
    """
    return len(text.encode("utf-8")) >= REPLY_CAP_BYTES - 2 - MAX_NAME_BYTES


def parse_default_reply(text: str | None) -> str | None:
    """The default scope that a ``region default`` reply names.

    Returns:
        The region name, ``""`` for no scope (``<null>``), or ``None`` when the reply is
        missing, refused, or not a reply about the default scope.
    """
    if not text or region_refused(text):
        return None
    marker = "default scope is"
    lowered = text.lower()
    if marker not in lowered:
        return None
    rest = text[lowered.index(marker) + len(marker) :].strip()
    rest = rest.removeprefix("now").strip()
    name = rest.split()[0] if rest.split() else ""
    return "" if name in ("", NULL_DEFAULT) else name.removeprefix("#")


def region_refused(reply: str | None) -> bool:
    """Whether a reply to a region command shows that the repeater refused it.

    The region verbs reply ``Err - …`` (``unknown region``, ``not empty``,
    ``unable to put``, ``save failed``, and ``??`` for a verb that the firmware does not
    have). The error test of the settings catalog does not recognize these replies. Older
    firmware that has no ``region`` command replies with its general unknown-command text.
    """
    if reply is None:
        return True
    lowered = reply.strip().lower()
    return lowered.startswith(("err", "??", "unknown command", "error"))


def saves_table(command: str, reply: str | None) -> bool:
    """Whether an accepted command wrote the full table to flash by itself.

    ``region save`` does this, by definition. ``region default`` also does this on
    firmware 1.15+, which is the firmware that replies to it with
    ``default scope is now …``.
    """
    if reply is None or region_refused(reply):
        return False
    if command == SAVE_COMMAND:
        return True
    return command.startswith("region default ") and "default scope is now" in reply.lower()


# -- the commands ------------------------------------------------------------------


def allow_command(name: str) -> str:
    """Relay floods scoped to ``name`` (``*``: relay unscoped floods)."""
    return f"region allowf {name}"


def deny_command(name: str) -> str:
    """Stop the relay of floods scoped to ``name`` (``*``: stop the relay of unscoped floods)."""
    return f"region denyf {name}"


def home_command(name: str) -> str:
    """Make ``name`` the home region of the repeater (``*`` clears it, back to none)."""
    return f"region home {name}"


def default_command(name: str | None) -> str:
    """Make ``name`` the default scope for the floods of the repeater, or clear it (``None``)."""
    return f"region default {name or NULL_DEFAULT}"


def put_command(name: str, parent: str = WILDCARD) -> str:
    """Add ``name`` below ``parent`` (at the top level for ``*``). New regions are flood-allowed."""
    return f"region put {name}" if parent == WILDCARD else f"region put {name} {parent}"


def remove_command(name: str) -> str:
    """Remove ``name``. The firmware refuses this while the region still has sub-regions."""
    return f"region remove {name}"


def _is_name_char(ch: str) -> bool:
    """Whether ``RegionMap::is_name_char`` in the firmware accepts ``ch`` in a region name."""
    return ch in "-$#" or ch.isdigit() or ord(ch) >= ord("A")


def validate_new_name(name: str, taken: Iterable[str] = ()) -> str:
    """Check a name for ``region put``: the rules of the firmware, and one rule of MeshTerm.

    In addition to :func:`~meshterm.core.regions.validate` (length, no wildcard, no
    private ``$`` names), the firmware refuses a name with punctuation other than ``-``
    (its ``is_name_char``). Also, ``region put`` with a name that exists moves that region,
    and does not add a region. Thus this function refuses a name that is already in the
    table.

    Args:
        name: The name as the user typed it.
        taken: The names that are already in the table.

    Returns:
        The bare, valid name.

    Raises:
        RegionNameError: With the reason, in words that tell the user what to do.
    """
    bare = validate(name)
    bad = sorted({ch for ch in bare if not _is_name_char(ch)})
    if bad:
        raise RegionNameError(
            f"a repeater refuses {' '.join(bad)} in a region name — letters, digits, and - only"
        )
    if bare in set(taken):
        raise RegionNameError(f"{bare} is already a region here")
    return bare
