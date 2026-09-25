# SPDX-License-Identifier: Apache-2.0
"""A repeater's region table as its CLI shows it: the parser, the commands, the edits.

A MeshCore repeater keeps a small table of regions (at most 32) in a tree under the
wildcard ``*``, and relays a *scoped* flood only when the flood's region is one it lists as
flood-allowed (see :mod:`~meshterm.core.regions` for the maths that ties a flood to its
region). The tree is **organisational only**: a repeater relays exactly the regions it
lists as allowed, so allowing ``lakeside`` says nothing about ``lakeside-north`` beneath it.
The wildcard's own flag is the unscoped case — whether it relays plain floods at all.

The admin reads and edits that table over the remote CLI (firmware ``CommonCLI::
handleRegionCmd``, ``RegionMap``), and this module is everything about it that is not a
screen:

* **The dump** (:func:`parse_region_dump`). ``region`` answers with the tree, one region per
  line, each line indented one space per level below the wildcard, the name followed by
  ``^`` on the home region and `` F`` where floods are allowed::

      *^ F
       lakeside F
        lakeside-north F
        lakeside-south
       harbour F

* **The cut.** Every CLI reply is written into a 160-byte buffer (``exportTo(reply, 160)``),
  and the dump simply stops where the buffer ends — mid-line, mid-name, with nothing to say
  it did. A table of 32 regions with real names does not fit, so a dump near the cap is read
  as *possibly cut* (:data:`REPLY_CAP_BYTES`, :func:`parse_region_dump`), its unfinished last
  line is dropped rather than shown as a region with half a name, and the reader is told.
  The flat lists (``region list allowed`` / ``region list denied``) are how the rest is
  recovered (:func:`parse_name_list`, :func:`RegionTable.with_lists`) — they carry no parent,
  so those regions are shown *beyond the cut*, placed nowhere. The lists have a cap of their
  own and skip a name that would not fit rather than cutting it, so a list close enough to
  the cap to have skipped one is flagged too (:func:`list_may_be_incomplete`). Nothing here
  ever claims to hold the whole table when it cannot know.

* **The commands** (:func:`allow_command` and siblings). Every edit is one remote command,
  and each is spelled here exactly once. ``allowf``/``denyf``/``home`` match a name by
  *prefix* on the firmware, preferring an exact match — MeshTerm only ever sends names the
  repeater itself listed, so the exact match always wins.

* **The edits** (:meth:`RegionTable.after`). What a command the repeater accepted does to the
  table, so the editor redraws from the reply rather than spending another round trip on a
  re-read. ``region put`` of a name that already exists *moves* it on the firmware, which is
  why the editor refuses a name it can see is taken.

* **Persistence.** Every edit lives in the repeater's RAM until ``region save``; a reboot
  undoes whatever was not saved. The one exception is ``region default`` (firmware 1.15+),
  which saves the whole table itself — its reply (``default scope is now …``) is how an edit
  says it took the unsaved ones with it (:func:`saves_table`).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from .regions import MAX_NAME_BYTES, WILDCARD, RegionNameError, validate

#: The firmware's CLI reply buffer, in bytes. ``BufStream`` stops one short of it (the NUL),
#: so a dump that ran out of room arrives 159 bytes long.
REPLY_CAP_BYTES = 160

#: How close to the cap a dump must come to be read as possibly cut. A cut can land inside
#: a multi-byte UTF-8 name, which the decode on the way here may shorten by a few bytes, and
#: the transport may strip a trailing newline — so the test leaves room for both rather
#: than asking for exactly 159.
_CUT_SLACK = 4

#: The command that dumps the tree.
DUMP_COMMAND = "region"
#: The command that asks which region is the default scope (firmware 1.15+).
DEFAULT_QUERY = "region default"
#: The two flat lists — every flood-allowed and every flood-denied name, ``*`` included.
LIST_ALLOWED = "region list allowed"
LIST_DENIED = "region list denied"
#: The command that writes the table to flash. Nothing else survives a reboot.
SAVE_COMMAND = "region save"

#: The token ``region default`` takes to clear the default scope.
NULL_DEFAULT = "<null>"

#: What an empty flat list reads as on the repeater.
_EMPTY_LIST = "-none-"


# -- the table -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegionRow:
    """One region in a repeater's table.

    Attributes:
        name: The bare region name (``*`` for the wildcard).
        parent: The parent's name — ``*`` for a top-level region, ``None`` for the wildcard
            itself and for a region recovered from a flat list, whose place is unknown.
        depth: Levels below the wildcard (0 for the wildcard, 1 for a top-level region).
        flood: Whether the repeater relays floods scoped to it (``F``) — for the wildcard,
            whether it relays unscoped floods.
        home: Whether it is the repeater's home region (``^``).
        placed: ``False`` for a region known only from a flat list: it is in the table,
            but the dump was cut before it, so its parent and depth are not known.
    """

    name: str
    parent: str | None
    depth: int
    flood: bool
    home: bool = False
    placed: bool = True

    @property
    def wildcard(self) -> bool:
        """Whether this row is the wildcard — the unscoped case, not a region."""
        return self.name == WILDCARD


@dataclass(frozen=True, slots=True)
class RegionTable:
    """What MeshTerm knows of one repeater's region table.

    Attributes:
        rows: The wildcard first (when the dump reached it), then every region in dump order
            (depth-first, as the firmware prints the tree), then any recovered from the flat
            lists after a cut (``placed=False``).
        cut: Whether the dump may have been cut at the reply cap — rows after ``cut_after``
            may exist that the tree does not show.
        cut_after: The last region the dump showed whole (``None`` when it showed none).
        lists_read: Whether the flat lists were read to recover what the cut hid.
        lists_partial: Whether either flat list came close enough to its own cap that a
            name may have been skipped — the recovered set may still be short.
        default: The default-scope region, ``None`` where there is none, and absent
            knowledge is :attr:`default_known` ``False`` (older firmware has no such query).
        default_known: Whether ``default`` was actually read.
    """

    rows: tuple[RegionRow, ...] = ()
    cut: bool = False
    cut_after: str | None = None
    lists_read: bool = False
    lists_partial: bool = False
    default: str | None = None
    default_known: bool = False

    # -- reading it ---------------------------------------------------------------

    def get(self, name: str) -> RegionRow | None:
        """The row for ``name``, or ``None`` when the table (as known) has no such region."""
        return next((row for row in self.rows if row.name == name), None)

    @property
    def wildcard(self) -> RegionRow | None:
        """The wildcard's row, or ``None`` when the dump never reached it."""
        return self.get(WILDCARD)

    def regions(self) -> list[RegionRow]:
        """Every region row, the wildcard left out."""
        return [row for row in self.rows if not row.wildcard]

    def children(self, name: str) -> list[RegionRow]:
        """The regions directly under ``name`` (placed rows only — nothing else has a parent)."""
        return [row for row in self.rows if row.parent == name and not row.wildcard]

    @property
    def home(self) -> str | None:
        """The home region's name (``*`` when none was set), or ``None`` when not seen."""
        return next((row.name for row in self.rows if row.home), None)

    @property
    def complete(self) -> bool:
        """Whether every region the repeater holds is (to the best of our reading) here.

        A clean dump is complete. A cut one is complete again once both flat lists were read
        and neither came near its own cap — the tree is still partly unplaced, but no name
        is missing.
        """
        return not self.cut or (self.lists_read and not self.lists_partial)

    def carried(self) -> list[str]:
        """What the repeater relays floods for, as the regions request would list it.

        ``*`` first when it relays unscoped floods, then every flood-allowed region — the
        exact shape :meth:`~meshterm.core.region_store.RegionStore.learn_carried` takes.
        """
        wild = self.wildcard
        head = [WILDCARD] if wild is not None and wild.flood else []
        return head + [row.name for row in self.regions() if row.flood]

    # -- editing it ---------------------------------------------------------------

    def after(self, command: str, reply: str) -> RegionTable:
        """The table as it stands once the repeater accepted ``command``.

        Only the verbs this module spells are understood; anything else (or a refused
        reply — see :func:`region_refused`) returns the table unchanged, so a caller may
        fold every accepted reply through here without first asking what it was.

        Args:
            command: The command sent, as built by this module.
            reply: The repeater's reply to it.

        Returns:
            The updated table (a new object; tables are immutable).
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
            # The firmware forces flood on for the default region, and creates it at the top
            # level if it did not exist.
            table = self if self.get(args[0]) else self._put(args[0], WILDCARD, flood=True)
            return replace(table._with(args[0], flood=True), default=args[0], default_known=True)
        if verb == "put" and args:
            parent = args[1] if len(args) > 1 else WILDCARD
            # 1.15+ answers "OK - (flood allowed)"; older firmware created it denied.
            return self._put(args[0], parent, flood="flood allowed" in reply.lower())
        if verb == "remove" and args:
            if self.children(args[0]):
                return self  # the firmware refuses this; a reply claiming otherwise is noise
            rows = tuple(row for row in self.rows if row.name != args[0])
            default = None if self.default == args[0] else self.default
            return replace(self, rows=rows, default=default)
        return self

    def _with(self, name: str, **changes: object) -> RegionTable:
        """The table with one row's fields changed (unchanged when the row is not here)."""
        rows = tuple(replace(row, **changes) if row.name == name else row for row in self.rows)
        return replace(self, rows=rows)

    def _put(self, name: str, parent: str, *, flood: bool) -> RegionTable:
        """The table with a new region placed as the last child of ``parent``.

        Dump order is depth-first, so the new row goes after the parent's last descendant —
        exactly where the next ``region`` dump would print it.
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
            # An unplaced parent has no subtree to append to: keep placed rows together and
            # put the new one ahead of the unplaced tail.
            at = next((i for i, r in enumerate(rows) if not r.placed), len(rows))
        rows.insert(at, row)
        return replace(self, rows=tuple(rows))

    def with_lists(self, allowed: str | None, denied: str | None) -> RegionTable:
        """Fold the two flat lists into a cut table, recovering the regions the cut hid.

        A name in a list that the tree did not show joins it unplaced, flooded as its list
        says. Names the tree already has keep their tree row — the tree has the parent the
        list lacks — and the wildcard's flag is taken from the lists when the tree never
        reached it (it always does, being the first line, unless the reply was empty).

        Args:
            allowed: The reply to :data:`LIST_ALLOWED` (``None`` when it never came).
            denied: The reply to :data:`LIST_DENIED` (``None`` when it never came).

        Returns:
            The table with the recovered rows appended; ``lists_read`` is set only when both
            lists answered, and ``lists_partial`` when either may have skipped a name.
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

        Unchanged (``default_known`` left ``False``) when there was no reply or the firmware
        does not know the query — pre-1.15 answers it with its catch-all ``Err - ??``.
        """
        parsed = parse_default_reply(reply)
        if parsed is None:
            return self
        return replace(self, default=parsed or None, default_known=True)


# -- parsing ---------------------------------------------------------------------------


def dump_may_be_cut(text: str) -> bool:
    """Whether a reply came close enough to the 160-byte cap to have been cut short."""
    return len(text.encode("utf-8")) >= REPLY_CAP_BYTES - 1 - _CUT_SLACK


def parse_region_dump(text: str | None) -> RegionTable:
    """Parse a ``region`` dump into a table, dropping a line the reply cap cut in half.

    Each non-blank line is one region: its leading spaces are its depth, then the name, an
    optional ``^`` (home), and an optional ``F`` (flood allowed) after a space. A line's
    parent is the nearest line above it one level up; depth 0 is the wildcard. A line that
    does not fit the grammar, or whose indentation skips a level, is not guessed at — it
    ends the tree there and the table is marked cut, since a dump that stops making sense
    has stopped being the dump.

    Args:
        text: The reply text (``None`` or empty reads as an empty, uncut table).

    Returns:
        The table. When the reply came within reach of the cap its last line is dropped
        unless the reply ended on a newline (so the line is known whole), ``cut`` is set,
        and ``cut_after`` names the last region shown.
    """
    if not text:
        return RegionTable()
    raw = text.replace("\r", "").replace("\x00", "")
    cut = dump_may_be_cut(raw)
    lines = raw.split("\n")
    if cut and not raw.endswith("\n") and lines:
        lines = lines[:-1]  # the line the buffer ran out inside — a name may be half a name
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
    """One dump line, or ``None`` when it breaks the grammar (see :func:`parse_region_dump`)."""
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
    """The names in a ``region list allowed|denied`` reply (``-none-`` reads as empty)."""
    if not text or text.strip() == _EMPTY_LIST:
        return []
    names: list[str] = []
    for part in text.replace("\x00", "").split(","):
        name = part.strip().removeprefix("#")
        if name and name not in names:
            names.append(name)
    return names


def list_may_be_incomplete(text: str) -> bool:
    """Whether a flat list came near enough its cap that a name may have been skipped.

    The firmware appends a name only while ``length + len(name) + 2 < 160`` and skips one
    that would not fit — then carries on with the next, so a later short name can still get
    in. A skip therefore needs the list to have reached at least ``158 - len(name)`` bytes
    for some name of at most :data:`~meshterm.core.regions.MAX_NAME_BYTES`, and a list
    shorter than that skipped nothing.
    """
    return len(text.encode("utf-8")) >= REPLY_CAP_BYTES - 2 - MAX_NAME_BYTES


def parse_default_reply(text: str | None) -> str | None:
    """The default scope named by a ``region default`` reply.

    Returns:
        The region name, ``""`` for none (``<null>``), or ``None`` when the reply is
        missing, refused, or not a default-scope answer at all.
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
    """Whether a reply to a region command is the repeater refusing it.

    The region verbs answer ``Err - …`` (``unknown region``, ``not empty``, ``unable to
    put``, ``save failed``, and ``??`` for a verb the firmware lacks), which the settings
    catalog's own error test does not recognise. Older firmware with no ``region`` command
    at all answers its generic unknown-command text.
    """
    if reply is None:
        return True
    lowered = reply.strip().lower()
    return lowered.startswith(("err", "??", "unknown command", "error"))


def saves_table(command: str, reply: str | None) -> bool:
    """Whether an accepted command wrote the whole table to flash on its own.

    ``region save`` does, by definition; ``region default`` does too on firmware 1.15+,
    which is the firmware that answers it with ``default scope is now …``.
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
    """Stop relaying floods scoped to ``name`` (``*``: stop relaying unscoped floods)."""
    return f"region denyf {name}"


def home_command(name: str) -> str:
    """Make ``name`` the repeater's home region (``*`` clears it back to none)."""
    return f"region home {name}"


def default_command(name: str | None) -> str:
    """Make ``name`` the default scope for the repeater's own floods, or clear it (``None``)."""
    return f"region default {name or NULL_DEFAULT}"


def put_command(name: str, parent: str = WILDCARD) -> str:
    """Add ``name`` under ``parent`` (top level for ``*``). New regions are flood-allowed."""
    return f"region put {name}" if parent == WILDCARD else f"region put {name} {parent}"


def remove_command(name: str) -> str:
    """Remove ``name`` — refused by the firmware while it still has sub-regions."""
    return f"region remove {name}"


def _is_name_char(ch: str) -> bool:
    """Whether the firmware's ``RegionMap::is_name_char`` accepts ``ch`` in a region name."""
    return ch in "-$#" or ch.isdigit() or ord(ch) >= ord("A")


def validate_new_name(name: str, taken: Iterable[str] = ()) -> str:
    """Check a name for ``region put``, the firmware's own rules and one of MeshTerm's.

    On top of :func:`~meshterm.core.regions.validate` (length, no wildcard, no private
    ``$`` names), the firmware refuses a name with any punctuation but ``-`` — its
    ``is_name_char`` — and ``region put`` of a name that exists *moves* that region rather
    than adding one, so a name already in the table is refused here instead.

    Args:
        name: The name as typed.
        taken: Names already in the table.

    Returns:
        The bare, valid name.

    Raises:
        RegionNameError: With the reason, in words a reader can act on.
    """
    bare = validate(name)
    bad = sorted({ch for ch in bare if not _is_name_char(ch)})
    if bad:
        raise RegionNameError(
            f"a repeater refuses {' '.join(bad)} in a region name — letters, digits and - only"
        )
    if bare in set(taken):
        raise RegionNameError(f"{bare} is already a region here")
    return bare
