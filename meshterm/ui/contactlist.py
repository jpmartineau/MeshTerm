# SPDX-License-Identifier: Apache-2.0
"""The shared contact list: one lane layout, with sort and filter, for each screen of contacts.

We took this module from the subject picker of the Time Machine, so that the app has exactly
one way to draw "a full-screen list of contacts". It draws aligned lanes under a column
header that shows the sort, with our node pinned first. The Ctrl+arrow keys change the sort,
so the plain arrow keys stay with the highlight, and the letters stay with type-to-filter.
The name lane has the width of its content. Thus the columns stay at the left, and the key
lane uses the width of the terminal that remains.

The Contacts screen, the Time Machine picker, the Archived list, and the courier recipient
list all build on :class:`ContactListScreen`. Each one gives its rows as :class:`ContactRow`
values, from any source (the stored history of *discovered* contacts, the table of *added*
contacts on the device, the contacts that a sweep archived). Thus the lists render, sort,
and move in the same way, but they do not share a data source.

**The name and the key are fixed. All the lanes between them are a lane set.** Each list
starts with the name and ends with the key. These two lanes are what a contact *is*, and the
flexible widths are built around them. Each list declares the middle lanes that it wants as
a tuple of :class:`ContactLane` (:data:`DEFAULT_LANES` for ``NAME · HEARD · PKTS · KEY``,
:data:`TRACE_LANES` for the extra ``TRACED`` of the Trace picker, :data:`ARCHIVED_LANES` for
the single ``ARCHIVED`` of the Archived list).

A lane has its own sort ring id, header label, width, and the way to read its value from a
row. Thus to add a lane is a declaration, not a branch: the header, the row builder, the
sort metric, and the calculation of the lane widths all read the same tuple. The tuple
replaces a ``show_traced`` flag. That flag had already caused three of those four places to
say "and if it's the trace picker…".
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

#: The minimum width of the name lane (on a very narrow terminal), so that the ``NAME``
#: label of the header and its sort triangle always have space.
_NAME_MIN = 6

#: The minimum width of the key lane in cells: three leading bytes and an ellipsis, which is
#: still a recognizable prefix on a very narrow terminal. In other cases, the lane expands to
#: fill the width that the name lane (sized to its content) leaves. Thus a longer key (the
#: 64-hex key of our node, or a full key resolved from a contact) shows as many full bytes as
#: fit, with an ellipsis after them (refer to :func:`~meshterm.ui.widgets.highlighted_hash`).
_HASH_MIN = 8

#: The number of cells between two lanes that are next to each other. The header draws the
#: sort triangle in the trailing gap of a column. This gap is wide enough that the triangle
#: has a clear space on each side and never touches the label of the next column. A gap of
#: 2 cells (only the triangle and its one leading space) let the arrow touch the text after
#: it. The header and the value lanes both use this gap, so the two stay aligned.
_GAP = 3
_GAP_S = " " * _GAP

#: All the parts of a row other than the name lane, the key lane, and the middle lanes: the
#: select pointer (2), the type glyph and its space (2), and the gap before the key. Each
#: declared :class:`ContactLane` adds its own width and gap to this (refer to
#: :meth:`ContactListScreen._lane_widths`). The name lane has the width of its content, and
#: the key lane gets the width that remains.
_LEAD = 2 + 2 + _GAP


@dataclass(frozen=True)
class ContactLane:
    """One of the fixed lanes drawn between the name and the key of a row.

    A lane is a *declaration*, not a branch. It names the column of the sort ring that it
    belongs to, the header label above it, its width in cells, and how to read its value
    from a :class:`ContactRow`. The header builder, the row builder, the sort metric, and
    the calculation of the lane widths all read the same tuple. Thus you add a new lane in
    one place.

    Attributes:
        column: The sort ring id under which this lane sorts (refer to
            :data:`SORT_COLUMNS`).
        label: The header label. It is never wider than :attr:`width`. If it is wider, the
            header goes past the value lane under it and moves each column after it.
        width: The field width of the lane in cells. The values align to the right in it.
        field: The :class:`ContactRow` attribute that holds the value of the lane.
        kind: ``"age"`` for a datetime drawn as a relative age in the colour of the
            recency heat, or ``"count"`` for an integer count drawn muted (a faint ``—``
            when ``None``).
    """

    column: str
    label: str
    width: int
    field: str
    kind: str = "age"


#: How long ago MeshTerm last heard the node: the lane that each list of *live* contacts has.
HEARD_LANE = ContactLane("heard", "HEARD", 5, "last_seen")

#: The number of transmissions from the node that MeshTerm heard.
PKTS_LANE = ContactLane("packets", "PKTS", 5, "count", kind="count")

#: How long ago the node was last traced: the extra lane of the Trace-target picker.
TRACED_LANE = ContactLane("traced", "TRACED", 6, "last_traced")

#: How long ago the contact was archived and removed from the device: the only lane of the
#: Archived list. It is drawn exactly as ``HEARD`` is, with the recency heat. It is the same
#: question (how long ago did this occur) about a different event. A visual language of its
#: own is a second grammar that the user must learn, with no benefit.
ARCHIVED_LANE = ContactLane("archived", "ARCHIVED", 8, "archived_at")

#: The middle lanes of the usual contact list: ``NAME · HEARD · PKTS · KEY``.
DEFAULT_LANES: tuple[ContactLane, ...] = (HEARD_LANE, PKTS_LANE)

#: The middle lanes of the Trace-target picker, which puts ``TRACED`` before the usual two.
TRACE_LANES: tuple[ContactLane, ...] = (TRACED_LANE, HEARD_LANE, PKTS_LANE)

#: The middle lanes of the Archived list: ``NAME · ARCHIVED · KEY``. There is no ``HEARD``
#: and no ``PKTS``. An archived contact is not on the device, so this screen is not about
#: what the contact still transmits. It is about when the contact left.
ARCHIVED_LANES: tuple[ContactLane, ...] = (ARCHIVED_LANE,)

#: The padlock at the start of the row of a locked contact (refer to
#: :attr:`ContactRow.locked`). It is the private-channel ``🔒`` of the icon lexicon, because
#: it means the same thing (this item is closed to something). On the PicoCalc,
#: :func:`~meshterm.ui.theme.glyph` gives the hand-drawn padlock ``⚿`` of the font. An
#: unlocked contact draws nothing. The mark is an exception that the user must notice, and a
#: lane of open padlocks makes the few closed padlocks hard to see.
_LOCK_ICON = "🔒"


def _lock_lane_width(rows: list[ContactRow]) -> int:
    """The width of the lock column in cells: the platform padlock and its gap, or ``0``.

    The column is on the left of the type glyph. It is there only while a row is locked, so
    a list with no locks uses no cells for it. On the 53 cells of the PicoCalc, those cells
    are for a name.
    """
    if not any(row.locked for row in rows):
        return 0
    return cell_len(glyph(_LOCK_ICON)) + 1


def _lock_cell(row: ContactRow, lock_w: int) -> str:
    """The lock column of a row: the padlock and its gap when locked, or else blanks."""
    if not lock_w:
        return ""
    if not row.locked:
        return " " * lock_w
    mark = glyph(_LOCK_ICON)
    return mark + " " * (lock_w - cell_len(mark))


#: The muted ``(you)`` tag on the lane of our node (refer to :func:`_lane`). Its width is part
#: of the content size of the name lane, so the tag is never cut.
_YOU_TAG = "  (you)"

#: The sort ring, and the natural first direction of each column: name A→Z, the most
#: recently heard first, the most packets first, and ``hash`` on the key of the contact
#: (ascending = ``0`` → ``f``). The ``hash`` ring id is older than the lexicon. It stays for
#: compatibility with saved sorts. Its column header shows ``KEY``, because the lane shows
#: the key with the hash lit in it. Callers build their
#: :class:`~meshterm.ui.widgets.ContactsSort` over these values. Thus the Ctrl+arrow keys go
#: through the same four columns on each contact list.
SORT_COLUMNS: tuple[str, ...] = ("name", "heard", "packets", "hash")
SORT_OPENS_ASCENDING: dict[str, bool] = {
    "name": True,
    "heard": True,
    "packets": False,
    "hash": True,
}

#: The wider sort ring of the Trace-target picker. It adds a ``traced`` column (how long ago
#: the node was last traced) between name and heard. It opens on that column *ascending*,
#: which is the natural first direction of each age lane. The metric is an **age**, so
#: ascending puts the newest first. Rows that were never traced have ``+inf``, and they
#: collect at the old end in both directions: at the bottom when ascending, and at the top
#: when descending (refer to :func:`_ordered`).
#:
#: The value was ``False`` while only ``traced`` sorted on a raw timestamp. The lane spec
#: made that special case unnecessary, and now this age column sorts like the others. The
#: picker builds its :class:`~meshterm.ui.widgets.ContactsSort` over these values, so the
#: Ctrl+arrow keys go through all five columns. A list without the ``traced`` lane uses
#: :data:`SORT_COLUMNS`.
TRACE_SORT_COLUMNS: tuple[str, ...] = ("name", "traced", "heard", "packets", "hash")
TRACE_SORT_OPENS_ASCENDING: dict[str, bool] = {
    "name": True,
    "traced": True,
    "heard": True,
    "packets": False,
    "hash": True,
}

#: The sort ring of the Archived list: ``NAME · ARCHIVED · KEY``, the same as
#: :data:`ARCHIVED_LANES`. ``archived`` opens *ascending* on the age of the lane, so the
#: most recently archived contacts come first. This screen answers the question "what did
#: that sweep just take?", and the answer must be at the top of the screen.
ARCHIVED_SORT_COLUMNS: tuple[str, ...] = ("name", "archived", "hash")
ARCHIVED_SORT_OPENS_ASCENDING: dict[str, bool] = {
    "name": True,
    "archived": True,
    "hash": True,
}

#: The style of the active sort column and its triangle. It is the same as in the header of
#: the static Contacts table (refer to :func:`~meshterm.ui.widgets._sort_header`), so that
#: sort cues look the same everywhere. It is the ``cursor`` white of the app, the same
#: colour as the highlighted row, because it says the same thing in a different lane:
#: *this* is the one that you selected. Before, it was a plain cyan. That colour put a
#: selection cue back on the node spectrum (and, on the console, directly on the slot of
#: the wordmark).
_SORT_ACTIVE = "cursor"

#: The default footer: navigation, the Ctrl+arrow sort, the filter, and then Esc at the end.
_HINT = "↑↓ move · ^←→↑↓ sort · type to filter · Enter open · Esc back"


@dataclass
class ContactRow:
    """The lane data of one contact, from any source that the caller used.

    Attributes:
        value: The value that the :class:`~meshterm.ui.tui.select.Choice` of the row
            resolves with. A new sort also uses it as the identity to keep the highlight
            on its contact, so it must be unique in the list.
        name: The display name, drawn in the hue of the contact. ``None`` renders a muted
            ``unknown`` (the row of our node shows a plain ``you`` instead).
        key: The hex that the key lane shows: the most complete key that the caller could
            resolve. It is also the source of the hue of the name. An empty value renders
            a muted ``?``.
        node_type: The node type of the contact, for the leading glyph (``None`` = the
            plain node ``●``). The row of our node ignores it, and always starts with the
            yellow ``★``.
        last_seen: When MeshTerm last heard the contact (aware UTC). It fills the heard
            lane, in the colour of the recency heat. ``None`` shows ``never`` in the cold
            style.
        count: The packet count. ``None`` renders a faint ``—`` (MeshTerm never heard the
            node).
        last_traced: When this node was last traced (aware UTC). It fills the ``TRACED``
            lane (:data:`TRACE_LANES`), in the colour of the recency heat. ``None`` shows
            ``never``. Lists that do not declare the lane ignore it.
        archived_at: When this contact was archived and removed from the device (aware
            UTC). It fills the ``ARCHIVED`` lane (:data:`ARCHIVED_LANES`). Lists without
            that lane ignore it.
        locked: Whether the contact is locked, so that it cannot be archived. A padlock
            then starts the row, on the left of the type glyph. ``False`` draws nothing
            there.
        you: Whether this is our node. Our node gets the ``★`` marker, the pure-white
            ``you`` name style with a muted ``(you)`` tag, and faint ``—`` heard and packet
            lanes (MeshTerm never hears our node). Its row is pinned above the sorted
            block, whatever the sort.
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
    """The column labels above the contact lanes (refer to :func:`_lane`).

    The lanes show ``NAME``, then each declared :class:`ContactLane` in order, then
    ``KEY``. This is the same order as in the row builder, which goes through the same
    tuple. The four leading spaces are for the pointer column of the select screen (2
    cells), and for the one-cell type glyph and its gap. Thus each label is above its lane.

    Each column can sort, so any column can be the active sort. The label of the active
    column *and* its direction triangle (``▲`` ascending, ``▼`` descending) both get the
    ``cursor`` white of the app. :func:`~meshterm.ui.widgets._sort_header` of the static
    Contacts table uses the same cue. The triangle is in the trailing :data:`_GAP` of the
    column, with a space on each side, so that it never touches the label of the next
    column. Each column keeps that same gap, also when it has no triangle. Thus a change of
    the sort never makes a lane wider or moves the rest of the row.

    The function returns a :class:`~rich.text.Text` (not a plain string), so that only the
    active column has the colour, and the other columns stay muted.
    """

    def column(header: Text, label: str, key: str, field_w: int, *, last: bool = False) -> None:
        """Add one column: its label, the sort triangle if active, then the lane gap.

        The label fills ``field_w`` cells (the width of its value lane). The two-cell
        triangle mark and the clear space after it are in the trailing :data:`_GAP`, so
        that the arrow has a space on each side. The last column (KEY) has no trailing gap.
        """
        active = key == sort.column
        mark = (" " + ("▲" if sort.ascending else "▼")) if active else "  "
        # The label and the mark are one span, so that the colour of the active lane
        # covers both.
        header.append(label + mark, style=_SORT_ACTIVE if active else "muted")
        # Pad the label to the width of its lane (the flexible name lane must have this).
        # Then add the gap between columns, minus the two cells that the mark already used.
        header.append(" " * max(0, field_w - cell_len(label)), style="muted")
        if not last:
            header.append(" " * (_GAP - 2), style="muted")

    # The pointer (2), the lock column if the list has one, and the type glyph and its gap (2).
    header = Text(" " * (4 + lock_w), style="muted")
    column(header, "NAME", "name", name_w)
    for lane in lanes:
        # A label aligned to the right, above a value aligned to the right. Thus the header
        # of a narrow field is where its digits will be (``PKTS`` above a 5-cell count), not
        # to the left of them.
        column(header, f"{lane.label:>{lane.width}}", lane.column, lane.width)
    column(header, "KEY", "hash", 3, last=True)
    return header


def _lane_cell(row: ContactRow, lane: ContactLane) -> Text:
    """The value of one row in one lane: aligned to the right, with the style of its kind.

    An ``age`` lane draws the relative age in the column form (:func:`format_age`), in the
    colour of the recency heat. Thus a new value is bright and an old value is grey, the
    same for each event that the lane measures. A ``count`` lane draws the count muted, or
    a faint ``—`` where there is nothing to count. It clamps the count, so that a very
    large count cannot make the column wider.
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
    """The lane of our node, with the same layout as the usual rows of :func:`_lane`.

    It is drawn as the map and the static table draw our node: the ``★`` self marker
    (yellow), the name in the pure-white ``you`` style with a muted ``(you)`` tag, and our
    key with its hash lit at the routing width, in the flexible key lane. Our 64-hex key is
    longer than any lane, so it shows as many digits as fit, and then an ellipsis. **Each
    middle lane shows a faint ``—``**, whatever lanes the list declares. MeshTerm never
    hears, traces, or archives our node, so no middle lane has a value to show, and
    each lane says so in the same way.
    """
    text = Text(" " * lock_w, no_wrap=True, overflow="ellipsis")  # our node is never locked
    text.append(SELF_MARK[0], style=SELF_MARK[1])
    text.append(" ")
    # The name lane, exactly name_w cells. It has the name (white) with a muted "(you)" tag
    # close after it, padded to fill the lane. If the name alone leaves no space for the
    # tag, the name is fitted to the lane and the tag is removed. The lane is sized to hold
    # the tag (refer to ContactListScreen._widest_name), so the removal is only a guard for
    # a very long name.
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
    text.append(_GAP_S)  # the same gap as before KEY in the header (refer to _header)
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
    """One contact as fixed lanes with colour codes, under the columns of :func:`_header`.

    The padlock of a locked contact comes first, in a column of its own (``lock_w`` cells,
    zero when no row in the list is locked). The type glyph follows (from the shared marker
    palette of the app). Thus the mark is ``▲`` for a repeater, ``■`` for a room server,
    ``◉`` for a sensor, and ``●`` for a plain node. The name gets the hue of the contact and
    the flexible name lane (``name_w`` cells). The ``unknown`` placeholder of a contact
    without a name stays muted, because colour marks a name, and the key lane already shows
    the identity.

    Each declared lane follows in order, drawn by :func:`_lane_cell`. The key ends the row
    in the shared key widget, with its hash lit at the routing width of the device. When no
    key is known at all, the row ends with a muted ``?``. The row of our node
    (:attr:`ContactRow.you`) has its own drawing (refer to :func:`_you_lane`).

    Args:
        row: The lane data of the contact.
        name_w: The width of the name lane in cells (sized to the content of the full
            list).
        prefix_bytes: The width of the hash in bytes, to light at the start of the key.
        hash_w: The width of the flexible key lane in cells
            (:func:`~meshterm.ui.widgets.highlighted_hash` pads a short key to this width).
        lanes: The middle lanes to draw between the name and the key.
        lock_w: The width of the lock column in cells (refer to :func:`_lock_lane_width`).
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
    text.append(_GAP_S)  # the same gap as before KEY in the header (refer to _header)
    if row.key:
        # A row without a name is an unidentified node. All of its key lane is grey (the
        # prefix is not lit), like each hash of an unknown node in the app. The muted
        # ``unknown`` in the name lane next to it says the same thing.
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
    """Order the sortable rows by the active sort (our node is pinned in a different place).

    The two fixed columns sort on their own values: name A→Z, and ``hash`` by the displayed
    key (ascending = ``0`` → ``f``). Each other column is a declared :class:`ContactLane`,
    and sorts by the metric of the lane. A ``count`` lane sorts by its count, and an ``age``
    lane sorts by *age in seconds*. Thus ascending puts the newest first, and a row with no
    value at all collects at the old end in both directions. Because of this property,
    ``heard``, ``traced``, and ``archived`` behave the same, and no line of code names any
    of them.

    A pre-sort by name, ascending, is the stable tiebreak. Thus two contacts with the same
    metric keep an A→Z order in both directions, and do not change places with the primary
    key.
    """
    by_column = {lane.column: lane for lane in lanes}

    def key_name(row: ContactRow) -> str:
        return (row.name or "unknown").casefold()

    def metric(row: ContactRow):  # noqa: ANN202 - the same type for each sort
        if sort.column == "name":
            return key_name(row)
        if sort.column == "hash":
            return row.key
        lane = by_column.get(sort.column)
        if lane is None:
            # A sort ring that is wider than the lanes on the screen (a saved sort from a
            # different list). There is nothing to sort by, so the stable pre-sort by name
            # stays.
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
    """A full-screen contact list, with sort and filter, in the shared lane layout.

    The rows are in aligned lanes: the name (in the hue of the contact), then the middle
    lanes that the list declared, then the key. The rows of our node are first, then the
    other rows in the active sort order. ``lanes`` selects the set: :data:`DEFAULT_LANES`
    for the usual ``NAME · HEARD · PKTS · KEY``, :data:`TRACE_LANES` for the extra
    ``TRACED`` of the Trace picker, and :data:`ARCHIVED_LANES` for the
    ``NAME · ARCHIVED · KEY`` of the Archived list. Give the set a ``sort`` whose ring
    covers it.

    The lanes stay at the left. The name lane has the width of its widest name (not of the
    terminal). Thus the columns do not move when the window becomes wider, and the free
    width goes to the key lane, which shows each key as fully as it fits (refer to
    :meth:`_lane_widths`). By default, it is a full-screen list, that is, a place. The only
    instance that is a *question* (the trace target picker) sets ``floating`` on itself,
    and it is drawn as a dialog over the menu.

    The sort uses the Ctrl+arrow keys, so the plain arrow keys stay with the highlight and
    the letters stay with type-to-filter. **Ctrl+←/→** select the column (each column
    starts in its natural direction), and **Ctrl+↑/↓** force ascending or descending. Each
    change sorts the contact block again in place. The lead rows and the headers stay
    pinned, the highlight stays on its contact, and an active filter stays.

    This is the same :class:`~meshterm.ui.widgets.ContactsSort` model and white column cue
    that the static Contacts table uses. Only the keyboard keys are different, because a
    list with a filter already uses the plain arrow keys.
    """

    floating = False

    @property
    def picocalc_lyra_lane(self):
        """The F-key lane of the select list, with the full sort added on F3/F8.

        Four columns is a short ring. To go through it backward saves a maximum of two key
        presses, so it is not worth the Shift companion. Thus that slot gets the *other*
        half of the sort instead: the direction. F3 moves to the next column (each column
        opens in its natural direction), and F8 reverses the direction that is in force.

        The reverse chip names the direction that it *gives* you, not the direction
        already in force (``Sort ▼`` while the list is ascending). It uses the same
        triangle that the header shows above the active column. Thus the chip and the
        column cue can never seem to say the same thing.

        The lane is built on the lane of ``super()``, not on the plain default. Thus a
        contact list that has section headings (the whole-mesh lead of the Time Machine,
        the archive tail of the Contacts screen) keeps the section jumps that the select
        list puts on F1/F2.
        """
        from .tui.fkeys import FPair

        lane = list(super().picocalc_lyra_lane)
        descend = self._sort.ascending  # ascending now, so the reverse makes it descending
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
        """Build the list over rows that are already resolved.

        Args:
            title: The short heading in the border.
            rows: The lane data of each contact. The :attr:`ContactRow.you` rows are
                pinned first (in the given order), and this method sorts the other rows by
                ``sort``.
            prefix_bytes: The width of the hash in bytes, to light at the start of each key.
            sort: The sort state. The Ctrl+arrow keys change it in place. Give the same
                instance each time that the list opens again, so that the chosen order
                stays. Its ring must cover the ring that matches ``lanes``
                (:data:`SORT_COLUMNS`, :data:`TRACE_SORT_COLUMNS`, or
                :data:`ARCHIVED_SORT_COLUMNS`). Thus the user can get to each column on the
                screen, and to no column that is not on the screen.
            prompt: An optional instruction above the list.
            lead: Rows (choices and separators) drawn above the column header: the
                whole-mesh row of the Time Machine and its section heading. ``None`` for no
                rows.
            tail: Rows (choices and separators) drawn *below* the sorted contacts: the
                maintenance actions of the Contacts screen. They are after all the
                contacts, whatever the sort. They are usual choices, so they are not
                visible while type-to-filter makes the list shorter, exactly like the
                delete rows of the Trophy case. ``None`` for a list that is only for
                selection.
            footer_hint: The key hint of the footer. The default shows the full grammar.
            lanes: The fixed lanes drawn between the name and the key of each row. Refer
                to :data:`DEFAULT_LANES`, :data:`TRACE_LANES`, and :data:`ARCHIVED_LANES`.
        """
        self._contact_rows = rows
        self._prefix_bytes = prefix_bytes
        self._sort = sort
        self._lanes = tuple(lanes)
        self._lead = list(lead) if lead else []
        self._tail = list(tail) if tail else []
        # Temporary values until the first render finds the true width (refer to
        # render_body).
        self._name_w = _NAME_MIN
        self._hash_w = _HASH_MIN
        super().__init__(
            title,
            self._compose_items(),
            prompt=prompt,
            footer_hint=footer_hint,
        )

    def _compose_items(self) -> list:
        """The lead rows, the column header with the sort, the contact lanes, the tail rows.

        The rows of our node are first in the lanes and stay first, whatever the sort.
        Only the block below them changes order (refer to :func:`_ordered`). The tail rows
        (the archive actions of the Contacts screen) end the list, after all the contacts,
        whatever the sort.
        """
        items: list = list(self._lead)
        # The lane names come before the contacts as their landmark, so they stay pinned at
        # the top while the list scrolls. Thus the user can still read the columns of a row
        # far down in the sort.
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
        """Replace the contact lanes in place, and keep the position of the user.

        This is the counterpart of
        :meth:`~meshterm.ui.tui.select.SelectScreen.replace_items` for a list whose *lanes*
        are data. The ``TRACED`` column of the Trace-target picker changes its age when a
        trace that the picker started comes back, and the list is sorted by that column.
        This method composes and sorts the rows again, and puts the highlight back on the
        same contact, wherever the new order put it. Thus the node that was traced last
        goes to the top, and the highlight goes with it. The typed filter and the sort do
        not change.

        Use it for a lane that changed. A contact that is *gone* is the case for a rebuild
        (refer to :func:`~meshterm.ui.contacts_screen.open_contacts`), because the row that
        the list kept a position in no longer exists.

        Args:
            rows: The lane data of each contact, as :meth:`__init__` accepts it.
        """
        self._contact_rows = rows
        self._rebuild()

    def _rebuild(self) -> None:
        """Rebuild the rows for the current sort and width, with the highlight on its contact."""
        current = self._current_choice()
        keep = current.value if current is not None else None
        self._items = self._compose_items()
        self._reselect(keep)

    def _reselect(self, value: object) -> None:
        """Move the highlight back to the choice with ``value`` (or else clamp it in range)."""
        choices = self._choices()
        for i, choice in enumerate(choices):
            if choice.value == value:
                self._index = i
                return
        self._index = max(0, min(self._index, len(choices) - 1)) if choices else 0

    def _widest_name(self) -> int:
        """The width in cells of the widest rendered name, in all the lanes of the list.

        The name lane has the width of its content, not of the terminal. Thus the columns
        stay at the left, and do not move apart when the window becomes wider. The
        measurement includes the usual rows (``unknown`` for a row without a name, as the
        row renders it), and the name of each row of our node with its ``(you)`` tag, so
        that the tag always fits.
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
        """The widths of the name lane and the key lane, for a terminal ``width`` cells wide.

        The name lane has the width of its content (refer to :meth:`_widest_name`), so it
        does not move when the window becomes wider. It becomes narrower only when a very
        long name makes the key lane narrower than its :data:`_HASH_MIN` minimum. The key
        lane then gets all the width that the fixed lanes and the name lane leave. Thus
        keys show as fully as they fit: the 12 hex digits of a key prefix from stored
        history with free space, and a 64-hex key cut with an ellipsis to fit the lane.

        Each declared lane adds its width and gap to the fixed lead. Thus a list without
        two of the lanes (the Archived list) gives both widths back to the key.
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
        """Size the name lane to its content and adjust the key lane, then render the list."""
        name_w, hash_w = self._lane_widths(width)
        if (name_w, hash_w) != (self._name_w, self._hash_w):
            self._name_w, self._hash_w = name_w, hash_w
            self._rebuild()
        return super().render_body(width)

    def handle(self, action: str, data: str = "") -> None:
        """Change the sort with the Ctrl+arrow keys. The base list handles all other actions."""
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
