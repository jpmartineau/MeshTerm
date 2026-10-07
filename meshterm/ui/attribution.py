# SPDX-License-Identifier: Apache-2.0
"""The credit of the basemap: the name of OpenStreetMap, in the corner of the frame of the map.

MeshTerm renders the street map from OpenStreetMap data. OpenFreeMap serves the data as
vector tiles on the unmodified OpenMapTiles schema. All three must be named. The
*Attribution Guideline* of the OpenStreetMap Foundation (adopted 2021-06-25) says where and
how to name OpenStreetMap, in enough detail to build against:

    "For a browsable map (e.g., embedded in a web page or application), the credit
    should typically appear in a corner of the map. While the lower right corner is
    traditional, any corner of the map is acceptable."

    "Attribution must be to 'OpenStreetMap'." … "The historical forms of attribution
    '© OpenStreetMap contributors' or '© OpenStreetMap' are acceptable."

    "The attribution format should not require individuals to interact with the map or
    produced work to see the attribution."

    "You may use a mechanism to fade/collapse the attribution under certain conditions:
    … automatically on map interaction such as panning, clicking, or zooming …"

    "If the attribution has been collapsed, the user must still be able to find the
    licence information if they look for it, for example from an '(i)' button in the
    corner of the map or an 'About' option in a menu."

OpenFreeMap asks for one line of its own: *"OpenFreeMap © OpenMapTiles Data from
OpenStreetMap"*. It adds: *"You do not need to display the OpenFreeMap part, but it is
nice if you do."* Thus :data:`CREDIT_FULL` is that line without its optional half, and
:data:`CREDIT_SHORT` is the remnant to which the credit collapses.

This is the whole design. It costs the map almost nothing:

* **The credit is in the frame of the map, and never on a line of its own.** It is not a
  title atom, not a footer character, and not a body row. The title is a status line that
  already has four atoms, and the footer hint uses its whole budget of 72 cells. A credit
  that is added to either of them would cost space on each screen of each visit. *Where*
  the credit is in the frame is the one thing that is different on each platform, and
  only because the frames of the platforms are different. A bordered frame has a **bottom
  border rule**. The credit sits in this rule as a title sits in the top rule: right-
  justified, muted, and one rule cell before the corner. The drawing is not changed
  (JP, 2026-09-13: *"that way it's not in the map"*). The frame of the PicoCalc has no
  bottom rule. It has a borderless title bar above, the F-key lane below, and body rows
  between them. Thus there MeshTerm **stamps the credit over the right end of the bottom
  row of the drawing**. This is still the corner that the guideline calls traditional. It
  costs the map fifteen cells of ground, which a pan can move away from under the credit.
  We read that form on the panel of the device itself, and JP approved it as it is
  (JP, 2026-09-13). Thus its glyphs, its position, and its collapse are settled: do not
  change them. :func:`rule_caption` and :func:`stamp` are the two forms. :func:`map_body`
  picks one.
* **It arrives whole and becomes shorter after the first use.** A map that the user has
  not touched shows :data:`CREDIT_FULL`. Thus the user never must interact with the map to
  see the attribution. The first pan, zoom, reframe, or find key press collapses it to
  :data:`CREDIT_SHORT`, which is one of the two forms that OSMF names as acceptable. Thus
  MeshTerm uses the permission to collapse only on the half of the line for OpenMapTiles,
  and the OpenStreetMap credit never goes away.
* **The licence is on the About page.** The guideline gives this as its own example of
  where a user can find the licence information of a collapsed credit: an ``About`` option
  in a menu. ``meshterm/assets/pages/about.md`` has the full line, the ODbL, and the
  ``openstreetmap.org/copyright`` URL written out, because nothing on a framebuffer
  console is clickable. The user can reach that page from the main menu at all times.
  Thus the map needs no key of its own to show the licence. This is worth more here than a
  key would be. The map binds no plain letters (the find filter uses all of them). On the
  PicoCalc, a key that is not advertised cannot be found, so a key to show the licence
  would need a chip on an F-key lane that has five slots and is already full.

Where MeshTerm *does* stamp the credit on the drawing (the map of the PicoCalc, and the
location preview on both platforms), the mark **wins the cells that it covers**. The code
splices it over the rendered row. Thus a street, a braille dot, or a node label under it is
overprinted, and not moved. The guideline requires the attribution to be "legible and
understandable". A credit that a label can destroy is not legible. Also, the user can pan
the map, so ground that is hidden under fifteen cells of one row is one key press away. The
credit must not be hidden in this way.

Its style is ``muted``, because it is chrome that is drawn on the map and not content on
the map. The user must not take it for a node. Thus it does not use the ``you`` white or
a node hue that comes from a key. It is the one lane in the app where grey is the correct
claim.
"""

from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text

from ..platforms import Platform, on_platform
from .tui.render import crop_cells, render_to_ansi

#: The whole credit. MeshTerm shows it until the user first touches the map. It is the line
#: that OpenFreeMap requires, with its optional "OpenFreeMap" half removed. It has 40 cells,
#: which fits inside the readable widths of both platforms (72 regular, 53 PicoCalc), with
#: room to spare.
CREDIT_FULL = "© OpenMapTiles · Data from OpenStreetMap"

#: The credit to which it collapses after the map is used. It is also the only form that the
#: small static preview ever draws. It has 15 cells. It is a complete OpenStreetMap
#: attribution by itself ("The historical forms … '© OpenStreetMap' are acceptable"). Thus
#: the OSM credit does not depend on the permission to collapse. Only the half for
#: OpenMapTiles depends on it.
CREDIT_SHORT = "© OpenStreetMap"

#: The blank cells between what the map drew and the start of the mark. Thus the credit
#: never looks like the end of a street name that it lands beside.
_GAP = 1

#: Whether the frame of this platform has a bottom border rule in which the credit sits.
#: If it does not, the map must give up cells of its own bottom row. The code binds this
#: value one time at each platform switch (:func:`_bind`). Thus no paint ever asks the
#: platform this question.
_HAS_RULE = True


@on_platform
def _bind(platform: Platform) -> None:
    """Set where the credit goes when the platform is chosen, and not when a frame is drawn."""
    global _HAS_RULE
    _HAS_RULE = platform.frame_border


def credit(*, full: bool) -> Text:
    """The credit as a styled run: :data:`CREDIT_FULL` or :data:`CREDIT_SHORT`.

    Args:
        full: Whether to return the whole line (a map that the user did not touch) instead
            of the remnant.

    Returns:
        The mark, ``muted`` and ``no_wrap``.
    """
    return Text(CREDIT_FULL if full else CREDIT_SHORT, style="muted", no_wrap=True)


def stamp(lines: list[str], width: int, *, full: bool, left: Text | None = None) -> list[str]:
    """The map rows, with the credit stamped into the right end of the last row.

    The rows arrive as raw ANSI directly from
    :meth:`~meshterm.ui.mapcanvas.MapCanvas.to_ansi_lines`. Thus the function parses the
    bottom row again (:meth:`rich.text.Text.from_ansi`). It cuts the row to the cells that
    the mark leaves (:func:`~meshterm.ui.tui.render.crop_cells`, which measures in display
    cells and so never cuts a braille glyph in half). Then it renders the row again with the
    mark added. The frame does the same parse for each body line when the line goes into the
    panel. Thus the ground stays unchanged through it on both platforms, and this includes
    the colours that the PicoCalc already quantized.

    ``left`` replaces the content of the row and does not crop it. One caller has something
    else to put there. On a platform whose footer is the F-key lane, the map shows its live
    find query over this same row (refer to
    :meth:`~meshterm.ui.map_screen.MapScreen._query_echo`). The two share the row: the
    query is at the left, the credit is pinned at the right, and the code crops the query if
    it is long. The credit is the one item that the code must not cut.

    If a row is too narrow to hold even :data:`CREDIT_SHORT` beside a blank cell, the row
    gets no mark at all. A surface that small has no room to be legible, and the About page
    still carries the full statement.

    The function changes nothing in its input. A caller can pass a frame that it caches (the
    map uses one raster for many paints) and get a new list back. Thus the code never
    stamps the credit into ground that the next paint will stamp over again.

    Args:
        lines: The rendered map rows.
        width: The row width in cells.
        full: Whether the credit is still in its whole form (refer to :func:`credit`).
        left: The content for the rest of the row. It replaces what was drawn there.

    Returns:
        A new list of rows: the rows of the input, with the last row written again.
    """
    if not lines or width <= 0:
        return lines
    mark = credit(full=full)
    span = cell_len(mark.plain)
    if span + _GAP > width:  # the whole line does not fit here, so try the remnant
        mark = credit(full=False)
        span = cell_len(mark.plain)
        if span + _GAP > width:
            return lines
    keep = width - span
    row = crop_cells(left if left is not None else Text.from_ansi(lines[-1]), 0, keep - _GAP)
    row.pad_right(max(0, keep - cell_len(row.plain)))
    row.append_text(mark)
    return [*lines[:-1], render_to_ansi(row, width, no_wrap=True)]


def rule_caption(*, full: bool) -> str:
    """The credit for the bottom border rule of the frame, or ``""`` where the frame has none.

    A bordered frame already has a rule under the body, and the rule only closes the box. A
    caption in it is the cheapest place for the credit. It uses no cell of the drawing and
    no cell of a line that the app used for other content. The frame renders the caption
    right-justified through the own subtitle machinery of Rich
    (:func:`~meshterm.ui.tui.frame._panel_box`). Thus it lands as
    ``──── © OpenStreetMap ─╯``: one rule cell before the corner, as a title sits in the top
    rule.

    The result is empty on the PicoCalc, because its frame has no bottom rule for the credit.
    There the code stamps the credit on the drawing instead (:func:`stamp`). Both callers
    ask without a condition, and one of them gets nothing. Thus neither caller must know
    which platform it is on.

    Args:
        full: Whether the credit is still in its whole form (refer to :func:`credit`).

    Returns:
        The caption, or ``""`` where the frame of this platform has no rule for it.
    """
    return (CREDIT_FULL if full else CREDIT_SHORT) if _HAS_RULE else ""


def map_body(lines: list[str], width: int, *, full: bool, left: Text | None = None) -> list[str]:
    """The rows of the full-screen map, with the credit where the frame of this platform cannot.

    This function is the counterpart of :func:`rule_caption`. Because of the two functions,
    the ``render_body`` of the map needs no platform test of its own. Exactly one of the two
    marks the map. This function is the one that draws nothing where the bottom rule of the
    frame carries the credit.

    The function draws ``left`` (the echo of the find query) in both cases, because it
    belongs to the map and not to the credit. In practice only the platform without a rule
    asks for it. That platform has the F-key lane as its footer, so it has no other place
    for the query.

    Args:
        lines: The rendered map rows.
        width: The row width in cells.
        full: Whether the credit is still in its whole form (refer to :func:`credit`).
        left: The echo of the find query for the bottom row, or ``None``.

    Returns:
        A new list of rows (the list of the input itself where there is nothing to draw).
    """
    if not _HAS_RULE:
        return stamp(lines, width, full=full, left=left)
    if left is None or not lines or width <= 0:
        return lines
    return [*lines[:-1], render_to_ansi(crop_cells(left, 0, width), width, no_wrap=True)]
