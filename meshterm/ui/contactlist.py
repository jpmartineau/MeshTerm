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
the ``PLACE`` and ``ARCHIVED`` of the Archived list).

A lane has its own sort ring id, header label, width, and the way to read its value from a
row. Thus to add a lane is a declaration, not a branch: the header, the row builder, the
sort metric, and the calculation of the lane widths all read the same tuple. The tuple
replaces a ``show_traced`` flag. That flag had already caused three of those four places to
say "and if it's the trace picker…".

**A list can show every value in full.** The Contacts list and the Archived list pass
``pan=True``. Their lanes then have the width of their content: the longest name, the
longest place, and the full key. No value is cut to fit the terminal. A row that is wider
than the terminal pans: ←→ slide the column header and all the rows by one shared shift,
with the mark column and the name held still (refer to
:attr:`~meshterm.ui.tui.select.Choice.pans`). The other lists can be floating dialogs (the
Trace picker), so they keep the lanes that fit the terminal.

**A list with the** ``PLACE`` **lane reads its places from the place service.** The caller
gives the service (``places``), and the list fills the place of each row from the key of
the row. The service counts its changes. When a place arrives while the list is open, the
next paint fills the rows again, and the highlight, the sort, the filter, and the pan stay.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from rich.cells import cell_len
from rich.text import Text

from .marks import LOCK_ICON, MANAGED_ICON, SELF_MARK
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

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..services.places import PlaceService

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
            recency heat, ``"count"`` for an integer count drawn muted (a faint ``—``
            when ``None``), or ``"text"`` for a string drawn on the left (a faint ``—``
            when ``None``). A ``text`` lane has no fixed width: the list sizes it to its
            widest value (refer to :func:`_sized`), and :attr:`width` is the minimum.
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

#: How long ago the contact was archived and removed from the device: the last lane of the
#: Archived list. It is drawn exactly as ``HEARD`` is, with the recency heat. It is the same
#: question (how long ago did this occur) about a different event. A visual language of its
#: own is a second grammar that the user must learn, with no benefit.
ARCHIVED_LANE = ContactLane("archived", "ARCHIVED", 8, "archived_at")

#: The place of the node: the named area nearest its location (refer to
#: :class:`~meshterm.core.places.Place`). It is text, so the list sizes it to its widest value.
#: On the Contacts screen it is the last lane, so it is immediately before the key. On the
#: Archived list it is the first lane, after the name (JP, 2026-11-03).
PLACE_LANE = ContactLane("place", "PLACE", 5, "place", kind="text")

#: The middle lanes of the usual contact list: ``NAME · HEARD · PKTS · KEY``.
DEFAULT_LANES: tuple[ContactLane, ...] = (HEARD_LANE, PKTS_LANE)

#: The middle lanes of the Contacts screen: ``NAME · HEARD · PKTS · PLACE · KEY``.
CONTACTS_LANES: tuple[ContactLane, ...] = (HEARD_LANE, PKTS_LANE, PLACE_LANE)

#: The middle lanes of the Trace-target picker, which puts ``TRACED`` before the usual two.
TRACE_LANES: tuple[ContactLane, ...] = (TRACED_LANE, HEARD_LANE, PKTS_LANE)

#: The middle lanes of the Archived list: ``NAME · PLACE · ARCHIVED · KEY``. There is no
#: ``HEARD`` and no ``PKTS``. An archived contact is not on the device, so this screen is not
#: about what the contact still transmits. It is about where the contact is, and when it
#: left. The ``PLACE`` lane is the same as on the Contacts screen.
ARCHIVED_LANES: tuple[ContactLane, ...] = (PLACE_LANE, ARCHIVED_LANE)


def _row_marks(row: ContactRow) -> str:
    """The marks of a row before its type glyph: the padlock, then the key of a managed node."""
    return (glyph(LOCK_ICON) if row.locked else "") + (glyph(MANAGED_ICON) if row.managed else "")


def _lock_lane_width(rows: list[ContactRow]) -> int:
    """The width of the mark column in cells: the widest marks of a row and a gap, or ``0``.

    The column is on the left of the type glyph. It holds the padlock of a locked contact
    and the key of a managed node (:func:`_row_marks`). It is there only while a row has a
    mark, so a list with no marks uses no cells for it. On the 53 cells of the PicoCalc,
    those cells are for a name.
    """
    widest = max((cell_len(_row_marks(row)) for row in rows), default=0)
    return widest + 1 if widest else 0


def _lock_cell(row: ContactRow, lock_w: int) -> str:
    """The mark column of a row: its marks and the gap, or else blanks."""
    if not lock_w:
        return ""
    marks = _row_marks(row)
    return marks + " " * (lock_w - cell_len(marks))


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

#: The sort ring of the Archived list: ``NAME · PLACE · ARCHIVED · KEY``, the same as
#: :data:`ARCHIVED_LANES`. ``archived`` opens *ascending* on the age of the lane, so the
#: most recently archived contacts come first. This screen answers the question "what did
#: that sweep just take?", and the answer must be at the top of the screen. ``place`` sorts
#: as on the Contacts screen (refer to
#: :data:`~meshterm.ui.widgets.CONTACTS_SORT_OPENS_ASCENDING`).
ARCHIVED_SORT_COLUMNS: tuple[str, ...] = ("name", "place", "archived", "hash")
ARCHIVED_SORT_OPENS_ASCENDING: dict[str, bool] = {
    "name": True,
    "place": True,
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
            white ``★``.
        last_seen: When MeshTerm last heard the contact (aware UTC). It fills the heard
            lane, in the colour of the recency heat. ``None`` shows ``never`` in the cold
            style.
        count: The packet count. ``None`` renders a faint ``—`` (MeshTerm never heard the
            node).
        last_traced: When this node was last traced (aware UTC). It fills the ``TRACED``
            lane (:data:`TRACE_LANES`), in the colour of the recency heat. ``None`` shows
            ``never``. Lists that do not declare the lane ignore it.
        place: The place of the node, as its full label (``Villeray, Montréal``). It fills
            the ``PLACE`` lane (:data:`PLACE_LANE`). ``None`` or blank renders a faint ``—``.
            Lists that do not declare the lane ignore it.
        archived_at: When this contact was archived and removed from the device (aware
            UTC). It fills the ``ARCHIVED`` lane (:data:`ARCHIVED_LANES`). Lists without
            that lane ignore it.
        locked: Whether the contact is locked, so that it cannot be archived. A padlock
            then starts the row, on the left of the type glyph. ``False`` draws nothing
            there.
        managed: Whether the contact is a managed node: MeshTerm stores its admin
            password. The ``🔑`` then goes in the same column, after a padlock.
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
    place: str | None = None
    locked: bool = False
    managed: bool = False
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
        # to the left of them. A text lane is the other way: on the left, as its values are.
        # Its label is not padded, so the triangle comes directly after the word, as for
        # ``NAME``. A padded label put the triangle at the far end of a wide lane, where the
        # user did not see it, or past the edge of the screen.
        if lane.kind == "text":
            column(header, lane.label, lane.column, lane.width)
        else:
            column(header, f"{lane.label:>{lane.width}}", lane.column, lane.width)
    column(header, "KEY", "hash", 3, last=True)
    return header


def _pad_cells(text: str, width: int) -> str:
    """Return ``text`` padded on the right with spaces to ``width`` cells (a wide glyph is two)."""
    return text + " " * max(0, width - cell_len(text))


def _sized(lanes: tuple[ContactLane, ...], rows: list[ContactRow]) -> tuple[ContactLane, ...]:
    """Return ``lanes`` with each ``text`` lane as wide as its widest value.

    A text lane cannot have a fixed width, because no width fits every place name. The list
    measures its rows, so that no value is cut. A lane is never narrower than its label. The
    other lanes are the same objects.
    """
    sized = []
    for lane in lanes:
        if lane.kind == "text":
            widest = max(
                (cell_len(str(getattr(row, lane.field, None) or "")) for row in rows),
                default=0,
            )
            lane = dataclasses.replace(lane, width=max(lane.width, cell_len(lane.label), widest))
        sized.append(lane)
    return tuple(sized)


def _lane_cell(row: ContactRow, lane: ContactLane) -> Text:
    """The value of one row in one lane: aligned to the right, with the style of its kind.

    An ``age`` lane draws the relative age in the column form (:func:`format_age`), in the
    colour of the recency heat. Thus a new value is bright and an old value is grey, the
    same for each event that the lane measures. A ``count`` lane draws the count muted, or
    a faint ``—`` where there is nothing to count. It clamps the count, so that a very
    large count cannot make the column wider. A ``text`` lane draws its string on the left,
    padded to the lane, or a faint ``—`` where there is none.
    """
    value = getattr(row, lane.field, None)
    if lane.kind == "text":
        if not value:
            return Text(_pad_cells("—", lane.width), style="faint")
        return Text(_pad_cells(str(value), lane.width))
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
    (white), the name in the pure-white ``you`` style with a muted ``(you)`` tag, and our
    key with its hash lit at the routing width, in the flexible key lane. Our 64-hex key is
    longer than any lane, so it shows as many digits as fit, and then an ellipsis. **Each
    middle lane shows a faint ``—``**, whatever lanes the list declares. MeshTerm never
    hears, traces, or archives our node, so no middle lane has a value to show, and
    each lane says so in the same way. The one exception is a ``text`` lane (the place), which
    shows its value when the row has one.
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
        if lane.kind == "text":
            # The place of our node is a fact about where it is, and MeshTerm can know it.
            text.append_text(_lane_cell(row, lane))
        else:
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
        if lane.kind == "text":
            # A blank value sorts after each text (its first key is larger), like the nodes
            # that were never heard in an age column.
            return (0, str(value).casefold()) if value else (1, "")
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
    ``NAME · PLACE · ARCHIVED · KEY`` of the Archived list. Give the set a ``sort`` whose
    ring covers it.

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

    With ``pan=True`` the list cuts no value (refer to the module docstring). Its rows and
    its column header are one table that ←→ pan, with the marks and the name held still.
    """

    floating = False

    #: The hint sheds this atom first when it is too wide (refer to
    #: :func:`~meshterm.ui.tui.frame.fit_hint`). The ``Left`` and ``Right`` keys are the
    #: first thing that a user tries on a row that runs past the edge, and the other atoms
    #: name keys that nothing else shows. Only a list that pans has the atom.
    spare_hint_atoms = ("←→ scroll",)

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
        key_hint: Callable[[Any], str] | None = None,
        pan: bool = False,
        places: PlaceService | None = None,
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
            key_hint: The hint atoms for the shortcut keys that act on the highlighted row
                (refer to :class:`~meshterm.ui.tui.select.SelectScreen`). ``None`` for a list
                with no such keys.
            pan: Whether the list shows each value in full. The lanes then have the width
                of their content, and ←→ pan the table when it is wider than the terminal
                (refer to the module docstring). It is off for a list that can float as a
                dialog, because the width of a dialog comes from the width of its rows.
            places: The places of the nodes, for a list whose ``lanes`` have the ``PLACE``
                lane. The list fills the place of each row from its key, and fills them
                again when the service counts a change (refer to :meth:`render_body`).
                With ``None``, each row keeps the place that the caller gave it.
        """
        self._places = places if PLACE_LANE in lanes else None
        self._place_rev = self._places.revision if self._places is not None else 0
        rows = self._with_places(rows)
        self._contact_rows = rows
        self._prefix_bytes = prefix_bytes
        self._sort = sort
        self._declared_lanes = tuple(lanes)
        self._lanes = _sized(self._declared_lanes, rows)
        self._pan_table = pan
        #: The cells of the mark column, the type glyph, and the name: the head that stays
        #: still while the table pans. The first paint finds the real value (refer to
        #: :meth:`_pinned_head`).
        self._anchor = 0
        self._lead = list(lead) if lead else []
        self._tail = list(tail) if tail else []
        # Temporary values until the first render finds the true width (refer to
        # render_body).
        self._name_w = _NAME_MIN
        self._hash_w = _HASH_MIN
        if pan:
            # A list that cuts nothing knows its lanes before the first paint.
            self._name_w, self._hash_w = self._lane_widths(0)
        super().__init__(
            title,
            self._compose_items(),
            prompt=prompt,
            footer_hint=footer_hint,
            key_hint=key_hint,
        )

    def _compose_items(self) -> list:
        """The lead rows, the column header with the sort, the contact lanes, the tail rows.

        The rows of our node are first in the lanes and stay first, whatever the sort.
        Only the block below them changes order (refer to :func:`_ordered`). The tail rows
        (the archive actions of the Contacts screen) end the list, after all the contacts,
        whatever the sort.
        """
        items: list = list(self._lead)
        # The text lanes are as wide as the widest value that the rows have now. A change of
        # the rows (a place that was found) can make a lane wider.
        self._lanes = _sized(self._declared_lanes, self._contact_rows)
        # The lane names come before the contacts as their landmark, so they stay pinned at
        # the top while the list scrolls. Thus the user can still read the columns of a row
        # far down in the sort. In a list that pans, the header pans with the rows, and it
        # holds the same head still.
        lock_w = _lock_lane_width(self._contact_rows)
        items.append(
            Separator(
                _header(self._name_w, self._sort, self._lanes, lock_w),
                heading=True,
                pans=self._pan_table,
                hscroll_from=self._anchor,
            )
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
                    pans=self._pan_table,
                    hscroll_from=self._anchor,
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
        self._contact_rows = self._with_places(rows)
        self._rebuild()

    def _with_places(self, rows: list[ContactRow]) -> list[ContactRow]:
        """The rows with the place of each node from the place service, if the list has one.

        The key of the row finds the place. A row without a full key uses the key prefix of
        its contact. A node with no known place gets ``None``, and its lane shows a faint
        ``—``.
        """
        if self._places is None:
            return rows
        filled = []
        for row in rows:
            key = row.key or getattr(row.value, "key_prefix", None)
            place = self._places.get(key) if key else None
            label = (place.label or None) if place is not None else None
            filled.append(dataclasses.replace(row, place=label))
        return filled

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

        A list that pans cuts nothing. Its name lane is the widest name, and its key lane is
        the longest key, whatever the width of the terminal.
        """
        if self._pan_table:
            keys = (len(row.key.lower().removeprefix("0x")) for row in self._contact_rows)
            return self._widest_name(), max(_HASH_MIN, *keys, 0)
        lead = (
            _LEAD
            + _lock_lane_width(self._contact_rows)
            + sum(_GAP + lane.width for lane in _sized(self._declared_lanes, self._contact_rows))
        )
        name_cap = max(_NAME_MIN, width - lead - _HASH_MIN)
        name_w = max(_NAME_MIN, min(self._widest_name(), name_cap))
        hash_w = max(_HASH_MIN, width - lead - name_w)
        return name_w, hash_w

    def _pinned_head(self, name_w: int, width: int) -> int:
        """The cells of a row that stay still while the table pans, for a terminal ``width`` wide.

        The head is the mark column, the type glyph with its space, and the name. They tell
        the user which contact a row is, so they must not slide away. But a very long name
        could leave no room for the lanes that slide. The head is never more than half of
        the row area. The rest of such a name slides with the lanes, and no cell is lost.
        """
        if not self._pan_table:
            return 0
        head = _lock_lane_width(self._contact_rows) + 2 + name_w
        return min(head, max(0, (width - 2) // 2))

    def render_body(self, width: int) -> list[str]:
        """Size the name lane to its content and adjust the key lane, then render the list.

        First, read the places again if the finder or the user changed one. The finder
        works in the background, and a place can arrive while this screen is open. The
        service counts its changes, so the list reads again only after a change. The
        highlight, the sort, the filter, and the pan stay (refer to :meth:`update_rows`).
        """
        if self._places is not None and self._places.revision != self._place_rev:
            self._place_rev = self._places.revision
            self.update_rows(self._contact_rows)
        name_w, hash_w = self._lane_widths(width)
        anchor = self._pinned_head(name_w, width)
        if (name_w, hash_w, anchor) != (self._name_w, self._hash_w, self._anchor):
            self._name_w, self._hash_w, self._anchor = name_w, hash_w, anchor
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
