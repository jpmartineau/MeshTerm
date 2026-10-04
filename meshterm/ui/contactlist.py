# SPDX-License-Identifier: Apache-2.0
"""The shared contact list: one sortable, filterable lane layout for every screenful of contacts.

Extracted from the Time Machine's subject picker so the app has exactly one way to draw "a
full-screen list of contacts": aligned lanes under a sort-aware column header, our own node
pinned first, the sort riding the Ctrl+arrows (plain arrows keep the highlight, letters keep
type-to-filter), and the name lane sized to its content so the columns anchor left while the
key lane soaks up the rest of the terminal. The Contacts screen, the Time Machine picker, the
Archived list and the courier recipient list all build on :class:`ContactListScreen`; each
hands its rows over as :class:`ContactRow` values — however it learned them (stored history of
*discovered* contacts, the device's *added* contact table, the contacts a sweep archived) — so
the lists render, sort, and steer identically without sharing a data source.

**Name and key are fixtures; everything between them is a lane set.** Every list opens on the
name and closes on the key — those two are what a contact *is*, and the flexing widths are
built around them — and declares the middle lanes it wants as a tuple of :class:`ContactLane`
(:data:`DEFAULT_LANES` for ``NAME · HEARD · PKTS · KEY``, :data:`TRACE_LANES` for the Trace
picker's extra ``TRACED``, :data:`ARCHIVED_LANES` for the Archived list's single ``ARCHIVED``).
A lane carries its own sort-ring id, header label, width, and how to read its value off a row,
so adding one is a declaration rather than a branch: the header, the row builder, the sort
metric and the lane-width arithmetic all read the same tuple. It replaces a ``show_traced``
flag that had already made three of those four places say "and if it's the trace picker…".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rich.cells import cell_len
from rich.text import Text

from .marks import SELF_MARK
from .menus import fit_cells
from .theme import glyph, name_style
from .tui.select import Choice, SelectScreen, Separator
from .widgets import (
    DEFAULT_GLYPH,
    NODE_GLYPHS,
    ContactsSort,
    _recency_style,
    age_seconds,
    format_age,
    highlighted_hash,
)

#: The narrowest the name lane shrinks to (a very narrow terminal), so the header's ``NAME``
#: label and its sort triangle always have somewhere to sit.
_NAME_MIN = 6

#: The key lane's floor in cells — four leading bytes plus an ellipsis, still a recognisable
#: prefix on a very narrow terminal. The lane otherwise flexes to fill whatever width the
#: (content-sized) name lane leaves, so a longer key — our own 64-hex node key, or a full key
#: resolved from a contact — shows as many whole bytes as fit, ellipsized past that (see
#: :func:`~meshterm.ui.widgets.highlighted_hash`).
_HASH_MIN = 8

#: Cells between adjacent lanes. Wide enough that the sort triangle the header draws in a
#: column's trailing gap keeps a clear space either side and never abuts the next column's
#: label — a 2-cell gap (just the triangle and its one leading space) left the arrow touching
#: whatever followed. Both the header and the value lanes gap by this, so the two stay aligned.
_GAP = 3
_GAP_S = " " * _GAP

#: Everything in a row besides the name lane, the key lane, and the middle lanes: the select
#: pointer (2), the type glyph and its space (2), and the gap before the key. Each declared
#: :class:`ContactLane` adds its own gapped width on top (see
#: :meth:`ContactListScreen._lane_widths`); the name lane is content-sized and the key lane
#: takes whatever is left.
_LEAD = 2 + 2 + _GAP


@dataclass(frozen=True)
class ContactLane:
    """One of the fixed lanes drawn between a row's name and its key.

    A lane is a *declaration*, not a branch: it names the sort-ring column it answers to, the
    header label over it, its width in cells, and how to read its value off a
    :class:`ContactRow`. The header builder, the row builder, the sort metric and the
    lane-width arithmetic all read the same tuple, so a new lane is added in one place.

    Attributes:
        column: The sort-ring id this lane sorts under (see :data:`SORT_COLUMNS`).
        label: The header label. Never wider than :attr:`width`, or the header would
            outrun the value lane under it and shift every column after it.
        width: The lane's field width in cells; values are right-aligned into it.
        field: The :class:`ContactRow` attribute holding the lane's value.
        kind: ``"age"`` for a datetime drawn as a recency-heat-coloured relative age, or
            ``"count"`` for an integer tally drawn muted (a faint ``—`` when ``None``).
    """

    column: str
    label: str
    width: int
    field: str
    kind: str = "age"


#: How long the node was last heard ago — the lane every list of *live* contacts carries.
HEARD_LANE = ContactLane("heard", "HEARD", 5, "last_seen")

#: How many transmissions MeshTerm has overheard from the node.
PKTS_LANE = ContactLane("packets", "PKTS", 5, "count", kind="count")

#: How long ago the node was last traced — the Trace-target picker's extra lane.
TRACED_LANE = ContactLane("traced", "TRACED", 6, "last_traced")

#: How long ago the contact was archived off the device — the Archived list's only lane.
#: Drawn exactly as ``HEARD`` is, recency heat and all: it is the same question (how long
#: ago did this happen) about a different event, and giving it its own visual language would
#: have been a second grammar for the reader to learn for no gain.
ARCHIVED_LANE = ContactLane("archived", "ARCHIVED", 8, "archived_at")

#: The ordinary contact list's middle lanes: ``NAME · HEARD · PKTS · KEY``.
DEFAULT_LANES: tuple[ContactLane, ...] = (HEARD_LANE, PKTS_LANE)

#: The Trace-target picker's, which inserts ``TRACED`` ahead of the usual two.
TRACE_LANES: tuple[ContactLane, ...] = (TRACED_LANE, HEARD_LANE, PKTS_LANE)

#: The Archived list's: ``NAME · ARCHIVED · KEY``. No ``HEARD`` and no ``PKTS`` — an archived
#: contact is off the device, so what it is still doing on the air is not what this screen is
#: about; when it left is.
ARCHIVED_LANES: tuple[ContactLane, ...] = (ARCHIVED_LANE,)

#: The padlock a locked contact's row leads with (see :attr:`ContactRow.locked`) — the
#: private-channel ``🔒`` of the icon lexicon, which is the same claim (this one is closed to
#: something), and on the PicoCalc the font's own hand-drawn padlock ``⚿`` through
#: :func:`~meshterm.ui.theme.glyph`. An unlocked contact draws nothing: the mark is an
#: exception worth noticing, and a lane of open padlocks would bury the few that aren't.
_LOCK_ICON = "🔒"


def _lock_lane_width(rows: list[ContactRow]) -> int:
    """The lock column's width in cells: this platform's padlock and its gap, or ``0``.

    The column sits left of the type glyph and exists only while some row is locked, so a
    list with no locks spends no cells on it — on the PicoCalc's 53 those are a name's cells.
    """
    if not any(row.locked for row in rows):
        return 0
    return cell_len(glyph(_LOCK_ICON)) + 1


def _lock_cell(row: ContactRow, lock_w: int) -> str:
    """A row's lock column: the padlock and its gap when locked, blank otherwise."""
    if not lock_w:
        return ""
    if not row.locked:
        return " " * lock_w
    mark = glyph(_LOCK_ICON)
    return mark + " " * (lock_w - cell_len(mark))


#: The muted ``(you)`` tag on the own-node lane (see :func:`_lane`), its width folded into
#: the name lane's content sizing so the tag never truncates.
_YOU_TAG = "  (you)"

#: The sort ring and each column's natural opening direction — name A→Z, most-recently-heard
#: first, most packets first, and ``hash`` on the contact's key (ascending = ``0`` → ``f``).
#: The ``hash`` ring id predates the lexicon and stays for saved-sort compatibility; its
#: column header reads ``KEY`` — the lane shows the key, with the hash lit inside it.
#: Callers build their :class:`~meshterm.ui.widgets.ContactsSort` over these so the Ctrl+arrows
#: walk the same four columns on every contact list.
SORT_COLUMNS: tuple[str, ...] = ("name", "heard", "packets", "hash")
SORT_OPENS_ASCENDING: dict[str, bool] = {
    "name": True,
    "heard": True,
    "packets": False,
    "hash": True,
}

#: The Trace-target picker's wider sort ring. It adds a ``traced`` column — how long ago the
#: node was last traced — between name and heard, and opens on it *ascending*, which is what
#: every age lane's natural open is: the metric is an **age**, so ascending is freshest-first
#: and never-traced rows carry ``+inf`` and gather at the bottom under either direction (see
#: :func:`_ordered`). It read ``False`` while ``traced`` alone sorted on a raw timestamp; the
#: lane spec made that special case unnecessary, and one age column now sorts like the rest.
#: The picker builds its :class:`~meshterm.ui.widgets.ContactsSort` over these, so the
#: Ctrl+arrows walk all five columns; a list without the ``traced`` lane keeps to
#: :data:`SORT_COLUMNS`.
TRACE_SORT_COLUMNS: tuple[str, ...] = ("name", "traced", "heard", "packets", "hash")
TRACE_SORT_OPENS_ASCENDING: dict[str, bool] = {
    "name": True,
    "traced": True,
    "heard": True,
    "packets": False,
    "hash": True,
}

#: The Archived list's ring — ``NAME · ARCHIVED · KEY``, matching :data:`ARCHIVED_LANES`.
#: ``archived`` opens *ascending* on the lane's age, so the most recently archived contacts
#: come first: the question this screen answers is "what did that sweep just take?", and the
#: answer should be at the top of it.
ARCHIVED_SORT_COLUMNS: tuple[str, ...] = ("name", "archived", "hash")
ARCHIVED_SORT_OPENS_ASCENDING: dict[str, bool] = {
    "name": True,
    "archived": True,
    "hash": True,
}

#: What the active sort column and its triangle are lit in, matching the static Contacts
#: table's header (see :func:`~meshterm.ui.widgets._sort_header`) so sort cues read
#: identically everywhere. The app's ``cursor`` white — the same ink the highlighted row
#: wears, because this is the same claim one lane over: *this* is the one you picked. It
#: used to be a bare cyan, which put a selection cue back on the node spectrum (and, on
#: the console, straight onto the wordmark's slot).
_SORT_ACTIVE = "cursor"

#: The default footer: navigation, the Ctrl+arrow sort, filtering, then Esc last.
_HINT = "↑↓ move · ^←→↑↓ sort · type to filter · Enter open · Esc back"


@dataclass
class ContactRow:
    """One contact's lane data, however the caller learned it.

    Attributes:
        value: What the row's :class:`~meshterm.ui.tui.select.Choice` resolves with —
            also the identity a re-sort uses to keep the highlight on its contact, so it
            should be unique across the list.
        name: The display name, drawn in the contact's hash-derived palette hue; ``None``
            renders a muted ``unknown`` (an own-node row falls back to a bare ``you``
            instead).
        key: The hex the key lane shows — as full a key as the caller could resolve;
            also the seed of the name's palette hue. Empty renders a muted ``?``.
        node_type: The contact's node type for the leading glyph (``None`` = the plain-node
            ``●``; ignored on the own-node row, which always leads with the yellow ``★``).
        last_seen: When the contact was last heard (aware UTC) — fills the heard lane,
            coloured by recency heat; ``None`` reads ``never`` in the cold style.
        count: The packet tally; ``None`` renders a faint ``—`` (never overheard).
        last_traced: When this node was last traced (aware UTC) — fills the ``TRACED`` lane
            (:data:`TRACE_LANES`), coloured by recency heat; ``None`` reads ``never``.
            Ignored by lists that don't declare the lane.
        archived_at: When this contact was archived off the device (aware UTC) — fills the
            ``ARCHIVED`` lane (:data:`ARCHIVED_LANES`). Ignored by lists without it.
        locked: Whether the contact is locked against archiving. Leads the row with a
            padlock, left of the type glyph; ``False`` draws nothing there.
        you: Whether this is our own node: the ``★`` marker, the pure-white ``you`` name
            style with a muted ``(you)`` tag, faint ``—`` heard/packet lanes (we never
            overhear ourselves), pinned above the sorted block whatever the sort.
    """

    value: object
    name: str | None
    key: str = ""
    node_type: int | None = None
    last_seen: datetime | None = None
    count: int | None = None
    last_traced: datetime | None = None
    archived_at: datetime | None = None
    locked: bool = False
    you: bool = False


def _header(
    name_w: int,
    sort: ContactsSort,
    lanes: tuple[ContactLane, ...] = DEFAULT_LANES,
    lock_w: int = 0,
) -> Text:
    """Column labels over the contact lanes (see :func:`_lane`).

    The lanes read ``NAME``, then each declared :class:`ContactLane` in order, then ``KEY``
    — matching the row builder, which walks the same tuple. The four leading spaces cover
    the select screen's pointer column (2 cells) plus the one-cell type glyph and its gap,
    so each label lands over its lane.

    Every column is sortable, so any one can be the active sort. The active column's label
    *and* its direction triangle (``▲`` ascending, ``▼`` descending) are lit together in the
    app's ``cursor`` white — the same cue the static Contacts table's
    :func:`~meshterm.ui.widgets._sort_header` lights. The triangle lands in the column's
    trailing :data:`_GAP`, flanked by a space on each side so it never abuts the next
    column's label, and every column reserves that same gap whether or not it holds a
    triangle — so switching the sort never widens a lane and shifts the rest of the row.
    Returned as a :class:`~rich.text.Text` (not a plain string) so just the active column
    carries the colour while the rest stays muted.
    """

    def column(header: Text, label: str, key: str, field_w: int, *, last: bool = False) -> None:
        """Append one column: its label, the sort triangle when active, then the lane gap.

        The label fills ``field_w`` cells (its value lane's width); the two-cell triangle
        mark and the clear space after it live in the trailing :data:`_GAP`, so the arrow
        keeps a space either side. The last column (KEY) takes no trailing gap.
        """
        active = key == sort.column
        mark = (" " + ("▲" if sort.ascending else "▼")) if active else "  "
        # Label and mark go down as one span so the active lane's highlight covers both.
        header.append(label + mark, style=_SORT_ACTIVE if active else "muted")
        # Pad the label out to its lane width (the flexing name lane needs it), then the
        # inter-column gap less the two cells the mark already spent.
        header.append(" " * max(0, field_w - cell_len(label)), style="muted")
        if not last:
            header.append(" " * (_GAP - 2), style="muted")

    # The pointer (2), the lock column when the list has one, the type glyph and its gap (2).
    header = Text(" " * (4 + lock_w), style="muted")
    column(header, "NAME", "name", name_w)
    for lane in lanes:
        # Right-aligned label over a right-aligned value, so a narrow field's header sits
        # where its digits will (``PKTS`` over a 5-cell count) rather than off to the left.
        column(header, f"{lane.label:>{lane.width}}", lane.column, lane.width)
    column(header, "KEY", "hash", 3, last=True)
    return header


def _lane_cell(row: ContactRow, lane: ContactLane) -> Text:
    """One row's value for one lane, right-aligned into the lane's width and styled by kind.

    An ``age`` lane draws the relative age in the column form (:func:`format_age`) under
    recency heat, so a fresh value glows and a cold one greys — the same reading whatever
    the event the lane times. A ``count`` lane draws the tally muted, or a faint ``—`` where
    there is nothing to count, and clamps so a runaway tally can't widen the column.
    """
    value = getattr(row, lane.field, None)
    if lane.kind == "count":
        if value is None:
            return Text(f"{'—':>{lane.width}}", style="faint")
        return Text(f"{min(value, 99999):>{lane.width}}", style="muted")
    secs = age_seconds(value)
    return Text(f"{format_age(secs):>{lane.width}}", style=_recency_style(secs))


def _you_lane(
    row: ContactRow,
    name_w: int,
    prefix_bytes: int,
    hash_w: int,
    lanes: tuple[ContactLane, ...] = DEFAULT_LANES,
    lock_w: int = 0,
) -> Text:
    """Our own node's lane — laid out exactly like :func:`_lane`'s regular rows.

    Drawn like the map and the static table draw us: the ``★`` self marker (yellow), the
    name in the pure-white ``you`` style with a muted ``(you)`` tag, and our key with its
    hash lit at the routing width, filling the flexing key lane — our 64-hex key is longer
    than any lane, so it shows as many digits as fit and ellipsizes. **Every middle lane
    reads a faint ``—``**, whatever the list declares: we never overhear, trace, or archive
    ourselves, so none of those has a value to show and each says so identically.
    """
    text = Text(" " * lock_w, no_wrap=True, overflow="ellipsis")  # we are never locked
    text.append(SELF_MARK[0], style=SELF_MARK[1])
    text.append(" ")
    # The name lane, exactly name_w cells: the name (white) with a snug muted "(you)" tag —
    # padded out to fill the lane — or, when the name alone would crowd out the tag, the
    # name fit to the lane with the tag dropped. The lane is sized to hold the tag (see
    # ContactListScreen._widest_name), so the drop is a very-long-name guard.
    used = cell_len(row.name) + cell_len(_YOU_TAG) if row.name else 0
    if row.name and used <= name_w:
        text.append(row.name, style="you")
        text.append(_YOU_TAG, style="muted")
        text.append(" " * (name_w - used))
    else:
        text.append(fit_cells(row.name or "you", name_w), style="you")
    for lane in lanes:
        text.append(_GAP_S)
        text.append(f"{'—':>{lane.width}}", style="faint")
    text.append(_GAP_S)  # the lane gap the header's KEY lane keeps (see _header)
    if row.key:
        text.append_text(highlighted_hash(row.key, prefix_bytes, width=hash_w))
    else:
        text.append("?", style="muted")
    return text


def _lane(
    row: ContactRow,
    name_w: int,
    prefix_bytes: int,
    hash_w: int,
    lanes: tuple[ContactLane, ...] = DEFAULT_LANES,
    lock_w: int = 0,
) -> Text:
    """One contact as fixed, colour-coded lanes under :func:`_header`'s columns.

    A locked contact's padlock leads, in a column of its own (``lock_w`` cells, zero when
    no row in the list is locked). The type glyph follows (the app's shared marker palette),
    so the mark reads ``▲`` for a repeater, ``■`` for a room, ``◉`` for a sensor, ``●`` for a
    plain node. The name takes
    the contact's hash-derived palette hue (a nameless contact's ``unknown`` placeholder
    stays muted — colour marks a name, and the hash lane already carries the identity) and
    the flexing name lane (``name_w`` cells). Each declared lane follows in order, drawn by
    :func:`_lane_cell`; the key closes the row in the shared key widget, its hash lit at the
    device's routing width, or a muted ``?`` when no key is known at all. An own-node row
    (:attr:`ContactRow.you`) takes its own drawing — see :func:`_you_lane`.

    Args:
        row: The contact's lane data.
        name_w: The name lane's width in cells (content-sized across the whole list).
        prefix_bytes: The hash width in bytes to light at the head of the key.
        hash_w: The flexing key lane's width in cells (a short key pads out to it; see
            :func:`~meshterm.ui.widgets.highlighted_hash`).
        lanes: The middle lanes to draw between the name and the key.
        lock_w: The lock column's width in cells (see :func:`_lock_lane_width`).
    """
    if row.you:
        return _you_lane(row, name_w, prefix_bytes, hash_w, lanes, lock_w)
    mark, mark_style = NODE_GLYPHS.get(row.node_type, DEFAULT_GLYPH)
    text = Text(_lock_cell(row, lock_w), no_wrap=True, overflow="ellipsis")
    text.append(mark, style=mark_style)
    text.append(" ")
    text.append(
        fit_cells(row.name or "unknown", name_w),
        style=name_style(row.name, row.key) if row.name else "muted",
    )
    for lane in lanes:
        text.append(_GAP_S)
        text.append_text(_lane_cell(row, lane))
    text.append(_GAP_S)  # the lane gap the header's KEY lane keeps (see _header)
    if row.key:
        # A nameless row is an unidentified node: its key lane greys whole (prefix
        # unlit) like every unknown-node hash in the app, the name lane's muted
        # ``unknown`` beside it making the same claim.
        text.append_text(
            highlighted_hash(row.key, prefix_bytes, width=hash_w, known=bool(row.name))
        )
    else:
        text.append("?", style="muted")
    return text


def _ordered(
    rows: list[ContactRow],
    sort: ContactsSort,
    lanes: tuple[ContactLane, ...] = DEFAULT_LANES,
) -> list[ContactRow]:
    """Order the sortable rows by the active sort (own-node rows are pinned elsewhere).

    The two fixture columns sort on themselves — name A→Z, ``hash`` by the displayed key
    (ascending = ``0`` → ``f``). Every other column is a declared :class:`ContactLane`, and
    sorts by the lane's own metric: a ``count`` lane by its tally, an ``age`` lane by *age in
    seconds*, so ascending is freshest-first and a row with no value at all gathers at the
    old end whichever direction is in force. That last property is what makes ``heard``,
    ``traced`` and ``archived`` behave identically without one line of code naming any of
    them.

    A name-ascending pre-sort is the stable tiebreak, so two contacts sharing a metric keep
    an A→Z order under both directions rather than flipping with the primary key.
    """
    by_column = {lane.column: lane for lane in lanes}

    def key_name(row: ContactRow) -> str:
        return (row.name or "unknown").casefold()

    def metric(row: ContactRow):  # noqa: ANN202 - homogeneous per sort
        if sort.column == "name":
            return key_name(row)
        if sort.column == "hash":
            return row.key
        lane = by_column.get(sort.column)
        if lane is None:
            # A sort ring wider than the lanes on screen (a saved sort from another list):
            # nothing to order by, so the stable name pre-sort stands.
            return 0
        value = getattr(row, lane.field, None)
        if lane.kind == "count":
            return value or 0
        secs = age_seconds(value)
        return secs if secs is not None else float("inf")

    ordered = sorted(rows, key=key_name)
    ordered.sort(key=metric, reverse=not sort.ascending)
    return ordered


class ContactListScreen(SelectScreen):
    """A full-screen, sortable, filterable contact list in the shared lane layout.

    The rows run in aligned lanes — the name (in the contact's hash-derived palette hue),
    then whatever middle lanes the list declared, then the key — own-node rows first, then
    the rest in the active sort order. ``lanes`` picks the set: :data:`DEFAULT_LANES` for
    the usual ``NAME · HEARD · PKTS · KEY``, :data:`TRACE_LANES` for the Trace picker's
    extra ``TRACED``, :data:`ARCHIVED_LANES` for the Archived list's ``NAME · ARCHIVED ·
    KEY``. Pair the set with a ``sort`` whose ring spans it. The lanes anchor to the left:
    the name lane is sized to its widest name (not the terminal), so the columns stay put
    as the window widens and the freed width flows to the key lane, which shows each key as
    fully as it fits (see :meth:`_lane_widths`). It is a full-screen list — a place — by
    default; the one instance that is a *question* (the trace target picker) sets
    ``floating`` on itself and is drawn as a popup over the menu.

    The sort rides the Ctrl+arrows, leaving the plain arrows for the highlight and the
    letters for type-to-filter: **Ctrl+←/→** pick the column (each adopting its natural
    direction) and **Ctrl+↑/↓** force ascending/descending. Every change re-sorts the
    contact block in place — the lead rows and headers stay pinned, the highlight rides its
    contact, and any active filter holds — the same
    :class:`~meshterm.ui.widgets.ContactsSort` model and white column cue the static
    Contacts table uses, only its keys moved off the plain arrows that a filterable list
    already spends.
    """

    floating = False

    @property
    def picocalc_lyra_lane(self):
        """The select list's lane, with the whole sort added on F3/F8.

        Four columns is a short ring — walking it backwards saves at most two presses, so
        it never earns the Shift companion. That slot takes the sort's *other* half
        instead: the direction. F3 steps the column forward (each column opening in its
        natural direction) and F8 flips the one in force.

        The flip chip names the direction it would *give* you, not the one already in
        force — ``Sort ▼`` while the list reads ascending — in the same triangle the header
        lights over the active column, so the chip and the column cue can never read as
        the same claim.

        Built on ``super()``'s lane rather than the bare default, so a contact list that
        carries section headings (the Time Machine's whole-mesh lead, the Contacts screen's
        archive tail) keeps the section jumps the select list promotes onto F1/F2.
        """
        from .tui.fkeys import FPair

        lane = list(super().picocalc_lyra_lane)
        descend = self._sort.ascending  # ascending now, so the flip lands descending
        lane[2] = FPair(
            "Sort →",
            "ctrl_right",
            "Sort ▼" if descend else "Sort ▲",
            "ctrl_down" if descend else "ctrl_up",
        )
        return lane

    def __init__(
        self,
        title: str,
        *,
        rows: list[ContactRow],
        prefix_bytes: int,
        sort: ContactsSort,
        prompt: str = "",
        lead: list | None = None,
        tail: list | None = None,
        footer_hint: str = _HINT,
        lanes: tuple[ContactLane, ...] = DEFAULT_LANES,
    ) -> None:
        """Build the list over already-resolved rows.

        Args:
            title: Short heading shown in the border.
            rows: Every contact's lane data; :attr:`ContactRow.you` rows are pinned first (in
                the order given), the rest re-sorted here per ``sort``.
            prefix_bytes: The hash width in bytes to light at the head of each key.
            sort: The sort state, mutated in place by the Ctrl+arrows — pass the same
                instance across re-opens so the chosen order persists. Its ring should span
                the ring matching ``lanes`` (:data:`SORT_COLUMNS`,
                :data:`TRACE_SORT_COLUMNS` or :data:`ARCHIVED_SORT_COLUMNS`), so every
                column on screen is reachable and no column off it is.
            prompt: An optional instruction shown above the list.
            lead: Rows (choices/separators) drawn above the column header — the Time
                Machine's whole-mesh row and its section heading; ``None`` for none.
            tail: Rows (choices/separators) drawn *below* the sorted contacts — the
                Contacts screen's maintenance actions. They sit past every contact whatever
                the sort, and (being ordinary choices) fall out of view while a
                type-to-filter narrows the list, exactly like the Trophy case's delete rows.
                ``None`` for a pure pick-list.
            footer_hint: Footer key hint; the default advertises the full grammar.
            lanes: The fixed lanes drawn between each row's name and its key. See
                :data:`DEFAULT_LANES`, :data:`TRACE_LANES` and :data:`ARCHIVED_LANES`.
        """
        self._contact_rows = rows
        self._prefix_bytes = prefix_bytes
        self._sort = sort
        self._lanes = tuple(lanes)
        self._lead = list(lead) if lead else []
        self._tail = list(tail) if tail else []
        # Provisional until the first render learns the true width (see render_body).
        self._name_w = _NAME_MIN
        self._hash_w = _HASH_MIN
        super().__init__(
            title,
            self._compose_items(),
            prompt=prompt,
            footer_hint=footer_hint,
        )

    def _compose_items(self) -> list:
        """The lead rows, the sort-aware column header, the contact lanes, then any tail rows.

        Own-node rows lead the lanes and stay first whatever the sort: only the block
        below them reorders (see :func:`_ordered`). Tail rows (an archive action, the exit
        group) close the list, past every contact whatever the sort.
        """
        items: list = list(self._lead)
        # The lane names lead the contacts as their landmark, so they pin overhead while the
        # list scrolls — a row deep in the sort can still be read off its columns.
        lock_w = _lock_lane_width(self._contact_rows)
        items.append(
            Separator(_header(self._name_w, self._sort, self._lanes, lock_w), heading=True)
        )
        pinned = [row for row in self._contact_rows if row.you]
        rest = [row for row in self._contact_rows if not row.you]
        for row in (*pinned, *_ordered(rest, self._sort, self._lanes)):
            items.append(
                Choice(
                    _lane(
                        row,
                        self._name_w,
                        self._prefix_bytes,
                        self._hash_w,
                        self._lanes,
                        lock_w,
                    ),
                    row.value,
                )
            )
        items.extend(self._tail)
        return items

    def update_rows(self, rows: list[ContactRow]) -> None:
        """Swap the contact lanes in place, keeping the reader's place.

        The :meth:`~meshterm.ui.tui.select.SelectScreen.replace_items` counterpart for a
        list whose *lanes* are data: the Trace-target picker's ``TRACED`` column ages the
        moment a trace it launched comes back, and the list is sorted by that column. The
        rows are recomposed and re-sorted, and the highlight lands back on the same contact
        wherever the new order put it — so the node just traced rises to the top with the
        cursor riding it. The typed filter and the sort ride along untouched.

        Use it for a lane that changed; a contact that is *gone* is the rebuild case (see
        :func:`~meshterm.ui.contacts_screen.open_contacts`), because the row the list was
        holding a place in no longer exists.

        Args:
            rows: Every contact's lane data, as :meth:`__init__` takes it.
        """
        self._contact_rows = rows
        self._rebuild()

    def _rebuild(self) -> None:
        """Recompose the rows for the current sort/width, keeping the highlight on its contact."""
        current = self._current_choice()
        keep = current.value if current is not None else None
        self._items = self._compose_items()
        self._reselect(keep)

    def _reselect(self, value: object) -> None:
        """Move the highlight back onto the choice with ``value`` (else clamp it in range)."""
        choices = self._choices()
        for i, choice in enumerate(choices):
            if choice.value == value:
                self._index = i
                return
        self._index = max(0, min(self._index, len(choices) - 1)) if choices else 0

    def _widest_name(self) -> int:
        """The widest rendered name in cells across every lane the list draws.

        The name lane is sized to its content, not the terminal, so the columns anchor to
        the left instead of drifting apart as the window widens. The measure spans the
        regular rows (``unknown`` for the nameless, as the row renders them) and any
        own-node row's name plus its ``(you)`` tag, so the tag always fits.
        """
        widths = [cell_len("unknown")]
        for row in self._contact_rows:
            if row.you:
                widths.append(
                    cell_len(row.name) + cell_len(_YOU_TAG) if row.name else cell_len("you")
                )
            else:
                widths.append(cell_len(row.name or "unknown"))
        return max(widths)

    def _lane_widths(self, width: int) -> tuple[int, int]:
        """The name and key lane widths for a terminal ``width`` cells wide.

        The name lane is content-sized (see :meth:`_widest_name`) so it stays put as the
        window grows; it only yields when a very long name would starve the key lane past
        its :data:`_HASH_MIN` floor. The key lane then takes all the width the fixed lanes
        and the name lane leave, so keys show as fully as they fit — a heard id's 12 hex
        digits with room to spare, a 64-hex key ellipsized to the lane. Each declared lane
        adds its gapped width to the fixed lead, so a list that drops two lanes (the
        Archived one) hands both back to the key.
        """
        lead = (
            _LEAD
            + _lock_lane_width(self._contact_rows)
            + sum(_GAP + lane.width for lane in self._lanes)
        )
        name_cap = max(_NAME_MIN, width - lead - _HASH_MIN)
        name_w = max(_NAME_MIN, min(self._widest_name(), name_cap))
        hash_w = max(_HASH_MIN, width - lead - name_w)
        return name_w, hash_w

    def render_body(self, width: int) -> list[str]:
        """Size the name lane to content and flex the key lane, then render the list."""
        name_w, hash_w = self._lane_widths(width)
        if (name_w, hash_w) != (self._name_w, self._hash_w):
            self._name_w, self._hash_w = name_w, hash_w
            self._rebuild()
        return super().render_body(width)

    def handle(self, action: str, data: str = "") -> None:
        """Steer the sort with the Ctrl+arrows; everything else is the base list's."""
        if action == "ctrl_left":
            self._sort.move(-1)
            self._rebuild()
        elif action == "ctrl_right":
            self._sort.move(1)
            self._rebuild()
        elif action == "ctrl_up":
            if not self._sort.ascending:
                self._sort.ascending = True
                self._rebuild()
        elif action == "ctrl_down":
            if self._sort.ascending:
                self._sort.ascending = False
                self._rebuild()
        else:
            super().handle(action, data)
