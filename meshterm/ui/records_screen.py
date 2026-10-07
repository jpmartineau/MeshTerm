# SPDX-License-Identifier: Apache-2.0
"""The Trophy case: the walks that set records, found by the trace tools.

A read-only board over the ``discovered_paths`` table (:meth:`Repository.discoveries`).
It opens from the main menu. MeshTerm scores each successful trace (a *Trace target*
boomerang or a *Trace path* walk) against each discipline, and offers it to their boards
(refer to :mod:`~meshterm.services.records`). This screen shows the records that the
boards kept.

* The browser groups the disciplines. Each discipline has its heading, with a one-line
  description of the game under it (wrapped when necessary). The heading and the
  description pin together at the top as one block while that board scrolls. Thus a
  row far down in a discipline still shows which game it wins and what that game
  scores. Then come the records of the discipline, best first: the day, the score in
  the unit of the discipline, and the walk itself on the only path widget, with the two
  ends bare. (Each record is a boomerang, thus the cells go to the hops.) If a
  discipline has records at more than one hash width, each row shows its width, because
  the widths are really different games.
* When the user opens a record, MeshTerm pushes :class:`RecordScreen`, a full-screen
  page. The page shows each stat by which the walk was measured (the far point with the
  node that it reached, the longest leg with the link that it spanned). It shows the
  walk on the only route graph (the shape of the Message paths dialog, with our node at
  the two ends, drawn no taller than one graph lane needs). It shows the full route
  (the path widget again, with no label, across the full width of the page, wrapped at
  hop boundaries), the spec, and when and by which app version the record was set. The
  page scrolls (PgUp/PgDn/Home/End) when it is taller than the terminal. From there,
  *Trace this path* opens Trace path again, with the route of the record already filled
  in. Thus the user can test a claim again with one press of Enter. Trace path opens
  above the trophy case. Thus when that screen closes, the user is back on the trophy
  case, and ^W unwinds the full stack to the main menu.
* There are three levels of deletion, each behind a red dialog for data loss. One
  record is behind a Cancel/Delete confirm. The two bulk levels are one discipline (all
  widths) or all the records. To do a bulk deletion, the user must type ``delete``.

Nothing here transmits: this screen reads the boards that the trace tools filled.
"""

from __future__ import annotations

import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.text import Text

from ..core.geo import haversine_km
from ..persistence.repository import DiscoveredPath
from ..services import trace_runner
from ..services.records import CATEGORIES, CATEGORY_BY_ID, Category, local_xy
from .mapcanvas import RGB, MapCanvas
from .menus import fit_cells, icon_lane, marked_label, section_heading
from .pathgraph import PathLayer, render_path_graph
from .pathline import path_line
from .theme import glyph, name_style, snr_style
from .tui.render import render_hanging, render_lines, render_to_ansi
from .tui.screen import Screen
from .widgets import (
    NodeResolver,
    TypeOf,
    name_rgb,
    node_marker,
    node_type_legend,
    route_graph_style,
    self_marker,
    short_frame,
    tab_air,
    tab_strip,
)

if TYPE_CHECKING:
    from ..context import AppContext

#: The width at which the descriptions of the disciplines wrap. It is well inside 72
#: cells, so that the text is the same at each terminal width.
_DESC_WRAP = 64

#: The colour of the single walk of a record on the route graph. It is the only path on
#: the canvas, so it does not need a colour to make it different. White means only "the
#: walk".
_WALK_EDGE = (255, 255, 255)

#: The canvas rows in which the route graph of a record draws. One walk is one graph
#: lane, thus the graph is flat. It has only its marker row, with one row on each side
#: for the labels. The default of the shared widget (5) puts two rows of blank canvas
#: around a scored walk. A fan grows past this number by itself, but nothing here fans.
_GRAPH_ROWS = 3

#: The one body tone of the area drawing: a plain muted slate, for the enclosed region
#: and for its loop. It is one shade on purpose (no dimmer fill inside a brighter edge).
#: An even-odd fill shades only the enclosed side, and the coloured node pins give the
#: colour. Thus the shape shows as one flat silhouette, not as two brightnesses.
_AREA_TONE: RGB = (148, 163, 184)

#: The cell box of the area drawing, which is the full stage of the Area tab. Its width
#: is the width of the page. (The proportions are kept, thus a wider page gives a bigger
#: shape, never a padded one.) Its height is all the rows that the viewport leaves under
#: the strip and above the caption. The rows have a minimum: the first paint draws at
#: this height, before the frame gives the height of the viewport. The width also has a
#: minimum: below it, the drawing is not drawn, because a squeezed drawing is not
#: legible. The rows that a flat shape leaves blank are trimmed (:func:`_drawn_rows`).
#: The drawing was once next to the stats, in one third of the width. That left no space
#: for a label on any pin.
_AREA_MIN_W = 16
_AREA_MIN_ROWS = 8

#: The tabs of the page, in strip order: Info and Route, as Info and Routes on the node
#: page, and then the ground. The Area tab is offered only for a walk with a shape that
#: can be drawn.
_TAB_INFO = "Info"
_TAB_ROUTE = "Route"
_TAB_AREA = "Area"
#: The dots kept clear inside the edges of the box. The padding is wider on the sides
#: than above and below. Thus a pin at the east or west extreme still has the cells for
#: its hash-byte label.
_AREA_PAD_X = 8.0
_AREA_PAD_Y = 2.0

#: The number of cells of the label lane on the record page. Each row with a label on the
#: page (the stat lanes, the hanging indent of the spec row, the recorded line) uses this
#: one column. Thus the values align, whatever the label says.
_LABEL_W = 12


def discipline_label(category: Category, lane: int) -> str:
    """The mark of one discipline, padded to ``lane`` cells, then its title.

    This is the only way to write the name of a board. Thus the seven headings (and the
    seven rows of the list that selects a discipline to delete) start their titles in one
    column, not in two. The marks do not all have the same width. ``🛣`` and ``🕸`` are
    not in Emoji_Presentation, and they are one cell wide, while the other five are two
    cells wide. Because of this, ``🛣 Longest distance`` was one column away from
    ``🎯 Farthest node``. Its gap also looked closed on a terminal that paints the road
    wider than it advances (JP, 2026-09-09).

    This is the same rule as the icon column of a command row
    (:func:`~meshterm.ui.menus.icon_lane`), but on purpose it is not that function. A
    command row removes its icon where the platform draws no icon lane. But the mark of a
    board is part of its name on the two platforms. Thus the lane is measured over the
    glyphs that :func:`~meshterm.ui.theme.glyph` will draw here, and the marks stay.

    Args:
        category: The discipline to name.
        lane: The width of the mark column, from :func:`discipline_lane`.

    Returns:
        ``"<mark><pad><title>"``. The pad includes the separator space after the mark.
    """
    mark = glyph(category.icon)
    return f"{mark}{' ' * max(1, lane - cell_len(mark) + 1)}{category.title}"


def discipline_lane() -> int:
    """The width in cells of the widest discipline mark on this platform.

    Refer to :func:`discipline_label`. The width is measured, never written as a constant.
    The marks change with the platform. If a substitution becomes narrower later, each
    heading becomes one cell narrower, and nothing else moves.
    """
    return max(cell_len(glyph(category.icon)) for category in CATEGORIES)


def _ordinal(n: int) -> str:
    """``1 → "1st"``, ``2 → "2nd"``, ``11 → "11th"``, … for the standing of a record."""
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n if n < 20 else n % 10, "th")
    return f"{n}{suffix}"


def record_score(category: Category, record: DiscoveredPath) -> str:
    """The score of a record in the unit of its discipline, as a bound if the walk was partial.

    This is the only spelling of the metric, at each place where it is written (the score
    lane of the board, and the header of the record page). Thus a partial Longest-distance
    walk shows ``≥`` in the two places.
    """
    score = category.format_score(record.score)
    if category.id == "long_haul" and not record.stats.get("km_complete", True):
        score = "≥ " + score
    return score


def _drawn_rows(lines: list[str]) -> list[str]:
    """``lines`` without the rows at each end on which nothing was drawn.

    The page has two drawn blocks. Each block is rendered on a canvas with the size for
    the worst case, and is then filled. The area drawing has all the rows that the
    viewport leaves (at least :data:`_AREA_MIN_ROWS`), for a tall shape. The route graph
    pads one row on each side of its markers for the labels. When a shape is flat, or all
    the labels are on one side, those rows stay empty. But an empty row still uses a line
    of the page, and these rows make a gap that the layout did not intend. (Once, there
    were three blank lines between the stats and the graph, where one was intended.) The
    trim occurs after the drawing, thus nothing that was placed is lost. Only the rows
    that stayed blank are removed.
    """
    kept = list(lines)
    while kept and not Text.from_ansi(kept[-1]).plain.strip():
        kept.pop()
    while kept and not Text.from_ansi(kept[0]).plain.strip():
        kept.pop(0)
    return kept


@dataclass(frozen=True, slots=True)
class WalkVertex:
    """One positioned point of the circuit of a walk, projected onto the local plane in km.

    Our node is at the origin, and each hop is placed relative to it (kilometres east and
    north). Thus the vertices draw the same shape over which the area was scored. Each
    vertex has its own map marker, thus the drawing pins the nodes in the shared palette.

    Attributes:
        x: Kilometres east of our node.
        y: Kilometres north of our node.
        glyph: The node-type marker to pin at this point.
        color: The truecolour of the marker.
        is_self: ``True`` if this vertex is our node (the yellow star at the origin).
        label: The hash byte of the hop, written next to its pin. It is the same label
            that the route graph gives the node, thus the user can compare the two
            drawings. Empty for our node: the star already shows which node it is.
    """

    x: float
    y: float
    glyph: str
    color: RGB
    is_self: bool
    label: str = ""


class RecordScreen(Screen):
    """The full story of one record: a full-screen page pushed over the trophy case.

    It is a place, not a dialog. The user is in the record (to read it, scroll it, or
    start a trace from it). Thus it fills the frame, the same as the browser below it,
    and Esc means back. It once floated as a card of 62 columns, which looked like a
    question over the list, not like the page that it is.

    Its organization is the same as the node page. A one-line **header** is above the
    strip: the mark of the discipline and the metric of the record (the score in the unit
    of the discipline). Then comes a **stage with tabs**
    (:func:`~meshterm.ui.widgets.tab_strip`, ``Tab``/``Shift+Tab`` to change the tab):

    - **Info** shows the stats of the record, with the action rows of the page at its
      bottom.
    - **Route** shows the walk in two ways (the only route graph, above the route line),
      with *Trace this path* under it. Thus Enter there walks the route that is visible.
    - **Area** shows the ground of the walk. It is drawn across the full stage that the
      viewport leaves under the strip. Thus the shape gets each row and column of the
      page, not only the corner that a stacked layout could give it. The Area tab is
      offered only when there is a shape to draw.

    The page shows:

    - each stat by which the walk was measured: a reliability from the trace history of
      the far node, the far point with the node that it reached, and the longest leg,
      drawn as the link that it spanned (our end is the app-wide ★)
    - the walk on the only route graph (our node at the two ends, and relays with their
      map marker, above a legend of node types)
    - the full route on the only path widget: no label lane, the full width of the page,
      wrapped at hop boundaries and never truncated
    - the provenance of the record: when it was set, and by which app version.

    The page scrolls (PgUp/PgDn/Home/End) when it is taller than the frame, while the
    arrow keys move between the actions. There are two actions. *Trace this path* opens
    Trace path again with the route of the record already filled in (propagation changes,
    and a record is a claim that is worth a new test). *Delete…* removes this one record,
    behind a red Cancel/Delete confirm for data loss.
    """

    floating = False

    def __init__(
        self,
        record: DiscoveredPath,
        category: Category,
        rank: int,
        *,
        resolve: NodeResolver,
        device_label: str,
        device_hash: str | None,
        far_label: str | None = None,
        far_id: str | None = None,
        shape: Sequence[WalkVertex] | None = None,
        reliability: tuple[float, int, int] | None = None,
        type_of: TypeOf | None = None,
    ) -> None:
        """Build the page for one stored record.

        Args:
            record: The record to show.
            category: Its category (for the unit and the title of the score).
            rank: Its standing on the board (1 = the record holder).
            resolve: Changes node ids to friendly names.
            device_label: The name of our node, at the two ends of the route.
            device_hash: Our public key, shown at the width of the record.
            far_label: The name of the farthest node, shown next to its distance.
                ``None`` when no positioned hop has a name.
            far_id: The id of the farthest node, which gives the name its key-derived
                hue. With ``None``, the label is drawn in ``node.unknown``, the grey for
                a node that no key identifies.
            shape: The positioned circuit of the walk, projected for the area drawing.
                ``None`` when the walk has fewer than the three points that a polygon
                must have.
            reliability: ``(rate, successes, total)`` of the traces to the far node of
                the walk, or ``None`` when that node was never a trace target.
            type_of: Changes a route hash to its node type, so that relays draw their
                map marker (``▲`` repeater, …) on the graph. With ``None``, the relays
                are generic dots.
        """
        super().__init__()
        # The title bar tells the type of page. The header line under it names the record:
        # its discipline, its standing, and its metric.
        self.title = "Record"
        self._rank = rank
        self._record = record
        self._category = category
        self._resolve = resolve
        self._device_label = device_label
        self._device_hash = device_hash
        self._far_label = far_label
        self._far_id = far_id
        self._shape = list(shape) if shape else None
        self._reliability = reliability
        self._type_of = type_of
        self._index = 0
        self._cursor: int | None = None
        self._tabs = [_TAB_INFO, _TAB_ROUTE] + ([_TAB_AREA] if self._shape else [])
        self._tab_index = 0
        # The composed stage of each tab above its action rows, for each width, and the
        # drawing of the Area tab for each (width, rows). All are pure functions of the
        # frozen record.
        self._info_cache: tuple[int, list[str]] | None = None
        self._route_cache: tuple[tuple[int, bool], list[str]] | None = None
        self._area_cache: tuple[tuple[int, int], list[str]] | None = None
        # The page opens at the top. The arrow keys move (and follow) the highlight on the
        # actions. PgUp/PgDn/Home/End scroll the body independently of the highlight
        # (refer to cursor_line).
        self._follow = False

    @property
    def _tab(self) -> str:
        """The tab that fills the stage."""
        return self._tabs[self._tab_index]

    @property
    def _actions(self) -> tuple[str, ...]:
        """The action rows of the active tab, in the order of the highlight.

        Info has the actions of the page, and the highlight opens on the safe action. Route
        has only *Trace this path*, so Enter there walks the route that is visible. This is
        the rule of the node page, where Enter on a route starts its trace. Area is a
        picture and has no actions.
        """
        if self._tab == _TAB_INFO:
            return ("trace", "delete")
        if self._tab == _TAB_ROUTE:
            return ("trace",)
        return ()

    @property
    def picocalc_lyra_lane(self):
        """The shared pager, dim where nothing scrolls, and the tab change on F3.

        The chip names the tab to which it goes, never the tab that is active. This is the
        rule of the node page (refer to
        :attr:`~meshterm.ui.node_detail_screen.NodeDetailScreen.picocalc_lyra_lane`).
        The Area tab is a picture with the size of the viewport, so the pager is dim there.
        """
        from .tui.fkeys import FPair, default_lane

        lane = list(default_lane(nav=self._tab != _TAB_AREA and self.content_overflows))
        lane[2] = FPair(self._tabs[(self._tab_index + 1) % len(self._tabs)], "tab")
        return lane

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The hint line: the tab change, then the move, scroll, and select atoms, Esc last.

        Each atom shows only when its key has something to do. With one action row, ↑↓
        has nothing to move between. The Area tab has no highlight and never scrolls, so
        its hint names only the tab change and Esc.
        """
        parts = ["Tab/⇧Tab switch"]
        actions = self._actions
        if len(actions) > 1:
            parts.append("↑↓ move")
        if actions and self.content_overflows:
            parts.append("PgUp/PgDn scroll")
        if actions:
            parts.append("Enter select")
        parts.append("Esc back")
        return " · ".join(parts)

    def _switch_tab(self, delta: int) -> None:
        """Change the active tab: its page opens at the top, with the highlight on row one."""
        self._tab_index = (self._tab_index + delta) % len(self._tabs)
        self._index = 0
        self._follow = False
        self.scroll_to_top()

    def handle(self, action: str, data: str = "") -> None:
        """Change the tab, move the highlight, scroll the page, select the action, or leave.

        The Area tab is a picture with the size of the viewport. Nothing there moves,
        scrolls, or commits. Thus on that tab, each key does nothing, except the tab change
        and Esc.
        """
        if action == "tab":
            self._switch_tab(1)
        elif action == "shift_tab":
            self._switch_tab(-1)
        elif action == "escape":
            self.resolve(None)
        elif self._tab == _TAB_AREA:
            return
        elif action == "up":
            self._follow = True
            # The two ends clamp, they do not wrap. This is the rule of the app for a
            # highlight: a highlight that jumps from one end to the other moves the
            # viewport with it.
            self._index = max(0, self._index - 1)
        elif action == "down":
            self._follow = True
            self._index = min(len(self._actions) - 1, self._index + 1)
        elif action in ("pageup", "ctrl_pageup"):
            self._follow = False
            self.scroll_pages(-1)
        elif action in ("pagedown", "space", "ctrl_pagedown"):
            self._follow = False
            self.scroll_pages(1)
        elif action in ("home", "ctrl_home"):
            self._follow = False
            self.scroll_to_top()
        elif action in ("end", "ctrl_end"):
            self._follow = False
            self.scroll_to_bottom()
        elif action == "enter":
            self.resolve(self._actions[self._index])

    def cursor_line(self) -> int | None:
        """Keep the selected action visible after an arrow key, but not after a page key."""
        return self._cursor if self._follow else None

    def _header(self) -> Text:
        """The one-line identity above the strip: the discipline, the standing, the metric.

        The record page has this line where the node page has its identity header. It
        reads as one sentence: ``🛣 Longest distance — 5th place: 112.0 km``. The
        discipline is written the same as its board heading writes it
        (:func:`discipline_label`: the mark is padded to the measured lane, so that ``🛣``
        and ``🎯`` start their titles in the same cell). The standing is the rank of the
        record on that board. The metric is the score in the unit of the discipline
        (:func:`record_score`). The title bar says only ``Record``: this line names the
        record.
        """
        line = Text(discipline_label(self._category, discipline_lane()))
        line.append(f" — {_ordinal(self._rank)} place: ", style="muted")
        line.append(record_score(self._category, self._record), style="accent bold")
        return line

    @staticmethod
    def _label(label: str) -> Text:
        """The muted label lane of the page, padded to :data:`_LABEL_W` cells."""
        return Text(f"{label:<{_LABEL_W}}", style="muted")

    def _lane(self, label: str, value: Text) -> Text:
        """One stat lane: a label and a value. The label lane is fixed, so the values align."""
        row = self._label(label)
        row.append_text(value)
        return row

    def _graph_lines(self, width: int) -> list[str]:
        """Draw the walked route as one path on the only route graph, our node at the two ends.

        A scored walk leaves home and comes back. Thus it draws our node (left) → its
        relays → our node (right) as one white path, through the shared route-graph widget
        (:func:`~meshterm.ui.pathgraph.render_path_graph`). This is the same shape on which
        the Message paths dialog draws a delivery. Relays show their own map marker (``▲``
        repeater, …) when the type is known. When the walk went through a node more than
        once (the mirrored return leg of a boomerang), the node shows only at its first
        position. The route line still shows each hop, with the revisits.

        This collapse is a choice that we made on purpose here, not a limit of the widget.
        A graph can give each visit its own marker (``allow_duplicate_nodes``, which the
        surfaces for observed packets turn on), but this graph does not. A trophy record
        scores a path that **we composed**. Thus a second walk through one node must never
        make the shape look bigger than the ground that it covered: one node, one marker,
        whatever the spec asked for. An overheard via chain has the opposite rule: nothing
        there is ours to make bigger, and a repeat is evidence.

        One walk is one graph lane. Thus the graph draws in :data:`_GRAPH_ROWS` (the marker
        row, and one row on each side for the labels), instead of the default of the
        widget. The default keeps space for a fan that this canvas never has, and that
        space becomes blank rows. If one of the two label rows stays empty, it is removed
        (:func:`_drawn_rows`). Thus the graph starts directly under the tab strip and its
        space, and the caption follows the graph directly.
        """
        seen: set[str] = set()
        hops: list[str] = []
        for node in self._record.route:
            if node not in seen:
                seen.add(node)
                hops.append(node)
        glyph_of, label_of, label_rgb_of = route_graph_style(
            resolve=self._resolve,
            self_name=self._device_label,
            source=self._device_label,
            type_of=self._type_of,
        )
        return _drawn_rows(
            render_path_graph(
                [PathLayer(hops=tuple(hops), color=_WALK_EDGE, priority=3)],
                width,
                glyph_of=glyph_of,
                label_of=label_of,
                label_rgb_of=label_rgb_of,
                min_rows=_GRAPH_ROWS,
            )
        )

    def _stat_lanes(self) -> list[Text]:
        """The measured stats of the walk, as label and value lanes (reliability → round trip).

        The lanes that show change with the discipline. (A walk with no positions has no
        distance, far point, or area.) Thus callers use the size of the returned list, not
        a fixed count. The far-point lane has the name of the node that the walk reached,
        when the name is known. The longest-leg lane has the link that the leg spanned. A
        record that was set before a stat existed has no lane for that stat.
        """
        record = self._record
        stats = record.stats
        lanes: list[Text] = []
        # There is no score lane: the metric is the header of the page, above the strip
        # (refer to _header).

        if self._reliability is not None:
            rate, ok, total = self._reliability
            value = Text(f"{rate:.0%}", style=snr_style(20 * rate - 10))
            value.append(f"  · {ok}/{total} traces to far node", style="muted")
            lanes.append(self._lane("reliability", value))

        hops = stats.get("hop_count", len(record.route))
        distinct = stats.get("distinct_nodes", len(set(record.route)))
        walk = Text(f"{hops} hop{'s' if hops != 1 else ''}")
        walk.append(f" · {distinct} distinct node{'s' if distinct != 1 else ''}", style="muted")
        if stats.get("repeats"):
            walk.append(" · revisits", style="muted")
        lanes.append(self._lane("walk", walk))

        km = stats.get("km_travelled")
        if km:
            value = Text(f"{'≥ ' if not stats.get('km_complete', True) else ''}{km:.1f} km")
            lanes.append(self._lane("distance", value))
        far = stats.get("far_km")
        if far is not None:
            value = Text(f"{far:.1f} km")
            if self._far_label:
                value.append("  ")
                value.append(self._far_label, style=name_style(self._far_label, self._far_id))
            lanes.append(self._lane("far point", value))
        leg = stats.get("leg_km")
        if leg is not None:
            value = Text(f"{leg:.1f} km")
            link = stats.get("leg_link")
            if link and len(link) == 2:
                # The link itself, on the only path widget. The two ends are named the
                # same as the route line names them: our end is the app-wide ★, because a
                # leg that starts or ends at home is the most usual type of leg.
                value.append("  ")
                value.append_text(
                    path_line(
                        list(link),
                        self._resolve,
                        self_name=self._device_label,
                        bare_self=True,
                        dim_self=False,
                        show_hash=False,
                    ).text()
                )
            lanes.append(self._lane("longest leg", value))
        area = stats.get("area_km2")
        if area is not None:
            lanes.append(self._lane("area", Text(f"{area:.1f} km²")))
        snr = stats.get("min_snr")
        if snr is not None:
            lanes.append(self._lane("weakest", Text(f"{snr:+.1f} dB", style=snr_style(snr))))
        rtt = stats.get("rtt_ms")
        if rtt is not None:
            lanes.append(self._lane("round trip", Text(f"{rtt:.0f} ms")))
        return lanes

    def _area_stage(self, width: int, budget: int) -> list[str]:
        """The Area tab: the ground of the walk, drawn to fill the rows that the strip leaves.

        ``budget`` is the number of rows that the viewport leaves under the strip. The
        caption uses one row, and the drawing uses the others. The drawing has a minimum of
        :data:`_AREA_MIN_ROWS` rows. This is also the height of the first paint, before the
        frame gives the height of the viewport. Then the drawing is trimmed to the rows on
        which something was drawn (:func:`_drawn_rows`). If the page is too narrow to draw
        on, it says so. It does not squeeze the shape until the shape is not legible.
        """
        if width < _AREA_MIN_W:
            note = Text("no room to draw the walk — widen the terminal", style="muted")
            return [render_to_ansi(note, width, no_wrap=True)]
        rows = max(_AREA_MIN_ROWS, budget - 1)
        cached = self._area_cache
        if cached is None or cached[0] != (width, rows):
            cached = ((width, rows), _drawn_rows(self._shape_lines(width, rows)))
            self._area_cache = cached
        lines = list(cached[1])
        caption = Text("north up · labels = hash byte", style="faint")
        lines.append(render_to_ansi(caption, width, no_wrap=True))
        return lines

    def _shape_lines(self, cell_w: int, cell_h: int) -> list[str]:
        """Draw the enclosed area of the walk on a braille canvas: fill, loop, labelled pins.

        The projected circuit (our node at the origin, each positioned hop around it) is
        scaled to the cell box, and the true proportions are kept. Braille dots are square,
        thus the same scale for x and y keeps the geography correct. Then the circuit is
        filled as a shaded region, drawn as the walked loop, and pinned with the map marker
        of each node. The shape is against the left edge of the box, and is centred
        vertically. (The text above it is aligned left, and a shape centred in a box as
        wide as the page floats far from that text.) The pin of each hop has its hash
        byte. The byte is placed the same as the map places the name of a node: next to
        the pin, clear of the drawn lines where a free position exists, and over them where
        none exists. A label is removed instead of printed over another pin or label.
        """
        verts = self._shape or []
        canvas = MapCanvas(cell_w, cell_h)
        xs = [v.x for v in verts]
        ys = [v.y for v in verts]
        span_x = (max(xs) - min(xs)) or 1e-6
        span_y = (max(ys) - min(ys)) or 1e-6
        avail_w = max(1.0, canvas.dot_w - 1 - 2 * _AREA_PAD_X)
        avail_h = max(1.0, canvas.dot_h - 1 - 2 * _AREA_PAD_Y)
        scale = min(avail_w / span_x, avail_h / span_y)
        origin_x = _AREA_PAD_X
        origin_y = _AREA_PAD_Y + (avail_h - span_y * scale) / 2
        min_x, max_y = min(xs), max(ys)
        # Reverse y so that north is up: the point farthest north is on the top dot row.
        ring = [(origin_x + (v.x - min_x) * scale, origin_y + (max_y - v.y) * scale) for v in verts]
        closed = ring + ring[:1]
        # One flat tone for the enclosed side and its loop. Even-odd shades only the odd
        # (enclosed) region: where a walk crosses itself, the crossing stays unshaded.
        # Nothing is drawn darker than the rest, so there is no dimmer sub-region.
        canvas.fill_polygon([closed], _AREA_TONE, priority=0)
        canvas.draw_line(closed, _AREA_TONE, priority=0)
        # First the hops, then our node last, so that the star always gets its cell. A hop
        # that is projected onto the same cell can never hide our node.
        pins = [
            (v, int(round(dx)), int(round(dy))) for v, (dx, dy) in zip(verts, ring, strict=True)
        ]
        for v, px, py in pins:
            if not v.is_self:
                canvas.marker(px, py, v.glyph, v.color)
        for v, px, py in pins:
            if v.is_self:
                canvas.marker(px, py, v.glyph, v.color)
        # Place the labels after all the pins. Thus no label is written over a pin that
        # comes later. There are two passes, the same as on the map: first a position
        # clear of the dots, then any free position.
        unlabelled = [(v, px, py) for v, px, py in pins if v.label and not v.is_self]
        for avoid_dots in (True, False):
            unlabelled = [
                (v, px, py)
                for v, px, py in unlabelled
                if not canvas.marker_label(px, py, v.label, v.color, avoid_dots=avoid_dots)
            ]
        return canvas.to_ansi_lines()

    def render_body(self, width: int) -> list[str]:
        """The tab strip, then the stage of the active tab.

        Info is the stat lanes, and Route is the graph above the route line. Each ends with
        its action rows, and scrolls if it is taller than the viewport. The Area tab is the
        ground drawing, with the size of the rows that the strip leaves, so it never
        scrolls. All the content above the action rows is a pure function of the frozen
        record and the width (and, for the drawing, the rows). Thus it is composed once and
        cached. Only the action rows, which have the highlight, render again at each paint.
        """
        # The pinned chrome above each tab: the metric line, then the strip with its space
        # (refer to widgets.tab_air). This is the same as the identity header and the strip
        # of the node page.
        lines: list[str] = [render_to_ansi(self._header(), width, no_wrap=True)]
        lines.extend([""] * tab_air())
        strip = tab_strip(
            self._tabs, self._tab_index, width, compact=short_frame(self._scroll_viewport)
        )
        lines.extend(render_lines(strip, width))
        lines.extend([""] * tab_air())
        if self._tab == _TAB_AREA:
            self._cursor = None
            lines.extend(self._area_stage(width, self._scroll_viewport - len(lines)))
            self._scroll_total = max(1, len(lines))
            return lines

        if self._tab == _TAB_INFO:
            cached = self._info_cache
            if cached is None or cached[0] != width:
                cached = (width, self._info_lines(width))
                self._info_cache = cached
        else:
            cached = self._route_cache
            short = short_frame(self._scroll_viewport)
            if cached is None or cached[0] != (width, short):
                cached = ((width, short), self._route_lines(width, short=short))
                self._route_cache = cached
        lines.extend(cached[1])

        lines.append("")
        self._cursor = None
        # One measured icon column for the two action rows. 🗑 is one cell wide and 👣 is
        # two. When each mark was measured alone, "Delete record…" started one column to the
        # left of "Trace this path". The column is empty where the platform draws no icons,
        # and the labels use those cells.
        lane = icon_lane(("👣", "🗑"))
        for i, key in enumerate(self._actions):
            selected = i == self._index
            row = Text("❯ " if selected else "  ", style="cursor" if selected else "")
            if key == "trace":
                row.append_text(
                    marked_label(
                        "👣", "Trace this path — reopen in Trace path", "accent", lane=lane
                    )
                )
            else:
                row.append_text(marked_label("🗑", "Delete record…", "err", lane=lane))
            if selected:
                row.style = "cursor"
                self._cursor = len(lines)
            row.no_wrap = True
            row.truncate(width, overflow="ellipsis")
            lines.append(render_to_ansi(row, width))
        self._scroll_total = max(1, len(lines))
        return lines

    def _info_lines(self, width: int) -> list[str]:
        """The Info tab above its action rows: the identity of the record, then its stats."""
        record = self._record
        lines: list[str] = []
        # The identity of the record comes before its stats: the spec that was walked, and
        # when it was set. Then come the measurements of the walk.
        spec = Text(record.spec, style="brand")
        spec.append(f"  ({record.width_bytes}-byte hops)", style="muted")
        lines.extend(render_hanging(self._label("spec"), spec, width, indent=_LABEL_W))
        when = Text(record.discovered_at.astimezone().strftime("%b %d %Y %H:%M"))
        when.append(f" · MeshTerm {record.app_version}", style="muted")
        lines.append(render_to_ansi(self._lane("recorded", when), width, no_wrap=True))
        lines.extend(render_to_ansi(lane, width, no_wrap=True) for lane in self._stat_lanes())
        return lines

    def _route_lines(self, width: int, *, short: bool = False) -> list[str]:
        """The Route tab above its action row: the only route graph, its legend, the route line.

        On a short frame, the legend of node types is not shown (refer to
        :func:`~meshterm.ui.widgets.short_frame`), the same as under each route graph. The
        rows go to the route.
        """
        record = self._record
        lines = self._graph_lines(width)
        caption = Text("you → … → you · labels = hash byte", style="faint")
        lines.append(render_to_ansi(caption, width, no_wrap=True))
        if not short:
            lines.extend(render_lines(node_type_legend(width=width), width, no_wrap=True))

        # The walk itself, on the only path widget, across the full width of the page. There
        # is no ``route`` label lane. The line under a route graph with the caption
        # "you → … → you" is the route, and the twelve cells of a label are hops that the
        # user came here to see. The line wraps at hop boundaries (never in a name, never in
        # a chip), instead of a hanging indent under a lane. Our two ends are the app-wide ★,
        # not our name and key again. The graph above already marks our node with the same
        # star at the two ends, and the legend under it says that the star is "you". The
        # stars keep the ``you`` white, because nothing about a walk that is already done is
        # ours to compose.
        route = path_line(
            [None, *record.route, None],
            self._resolve,
            prefix_bytes=record.width_bytes,
            self_name=self._device_label,
            show_hash=True,
            hash_bytes=record.width_bytes,
            device_hash=self._device_hash,
            bare_self=True,
            dim_self=False,
        )
        lines.extend(render_to_ansi(line, width, no_wrap=True) for line in route.wrapped(width))
        return lines


async def open_records(ctx: AppContext) -> dict:
    """Open the Trophy case browser and run it until the user leaves it.

    This function connects the browser to the database and to the observed contacts. The
    records come directly from :meth:`Repository.discoveries`. The hop hashes resolve to
    friendly names through the same resolver that the trace screens use. *Trace this
    path* opens the live Trace path screen, with the route of the record already filled
    in. That screen opens above the browser, which stays pushed. Thus when that screen
    closes, the user is back on the browser.

    Args:
        ctx: The shared application context (it must run the interactive TUI).

    Returns:
        A summary dict for the run row of the tool (the number of records shown).

    Raises:
        RuntimeError: If it is called outside the interactive menu.
    """
    from .surface import TuiUi
    from .trace_screen import open_trace_path
    from .tui import CANCEL, Choice, SelectScreen, Separator

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - the menu caller prevents this
        raise RuntimeError("the Trophy case screen is only available in the menu")
    session = ctx.ui.session

    contacts = await ctx.devstate.contacts()
    self_info = await ctx.devstate.self_info()
    resolve = trace_runner.make_node_resolver(contacts, ctx.repo.node_names())
    device_label = str(self_info.get("name") or "us")
    device_hash = str(self_info.get("public_key") or "") or None

    def _as_float(value) -> float | None:  # noqa: ANN001
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    lat, lon = _as_float(self_info.get("adv_lat")), _as_float(self_info.get("adv_lon"))
    self_pos = (lat, lon) if lat is not None and lon is not None and (lat or lon) else None

    # The trace results for each target, for the reliability of a record (the success rate
    # of the traces to its far node). They are read once here. For each record, the far
    # node is matched against these keys.
    target_counts = ctx.repo.target_trace_counts()

    # The mark column after which each board heading writes its title (refer to
    # discipline_label).
    mark_lane = discipline_lane()

    # The node positions and types for the area drawing of a record. They are collected
    # live, the same as the trace tools collect them to score a walk: the adverts that we
    # heard, with the contacts put over them. A record stores canonical ids, so match them
    # the same as the resolver does (a prefix on either side).
    node_entries: list[tuple[str, tuple[float, float] | None, int | None]] = []
    for heard in ctx.repo.heard_nodes():
        if not heard.node:
            continue
        pos = (heard.lat, heard.lon) if heard.has_location else None
        node_entries.append((heard.node.lower().removeprefix("0x"), pos, heard.node_type))
    for contact in contacts:
        ident = (contact.public_key or contact.key_prefix or "").lower().removeprefix("0x")
        if not ident:
            continue
        pos = (
            (contact.lat, contact.lon)
            if (contact.has_location and (contact.lat or contact.lon))
            else None
        )
        node_entries.append((ident, pos, contact.node_type))

    # Cached: the boards ask for the same route nodes many times (each hop of each record,
    # and records that share hops). The entry list does not change while the screen is
    # open. Thus the prefix scan occurs only once for each different id.
    geo_memo: dict[str, tuple[tuple[float, float] | None, int | None]] = {}

    def node_geo(node_id: str) -> tuple[tuple[float, float] | None, int | None]:
        """The best-known position and type of a route node, from the heard and contact data."""
        cached = geo_memo.get(node_id)
        if cached is not None:
            return cached
        needle = node_id.lower().removeprefix("0x")
        pos: tuple[float, float] | None = None
        ntype: int | None = None
        for ident, epos, etype in node_entries:
            if not (ident.startswith(needle) or needle.startswith(ident)):
                continue
            if pos is None:
                pos = epos
            if ntype is None:
                ntype = etype
            if pos is not None and ntype is not None:
                break
        geo_memo[node_id] = (pos, ntype)
        return pos, ntype

    def walk_drawing(
        record: DiscoveredPath,
    ) -> tuple[str | None, str | None, list[WalkVertex] | None]:
        """The farthest node (name and id), and the projected polygon of the walk.

        This function uses the same geometry as the scoring (refer to
        :mod:`~meshterm.services.records`). Our node is at the origin of the plane. Each
        positioned hop is projected onto the same local plane in km, in walk order, and the
        farthest hop is named. That farthest hop is the natural target of the walk, so its
        id is returned for the reliability lookup. A polygon must have three points (our
        node and two positioned hops). Thus a walk with fewer points gives no drawing, but
        it still names its far point.
        """
        if self_pos is None:
            return None, None, None
        glyph, color = self_marker()
        verts = [WalkVertex(0.0, 0.0, glyph, color, True)]
        far_label: str | None = None
        far_id: str | None = None
        far_dist = -1.0
        for node_id in record.route:
            pos, ntype = node_geo(node_id)
            if pos is None:
                continue
            east, north = local_xy(self_pos, pos)
            # The node-type glyph in the hue of the node name. Thus the pin shows that mesh
            # name, the same as the route line and the graph labels colour it. A hop with no
            # name keeps the neutral tone of the shape, so that it never looks like a
            # coloured name.
            name = resolve(node_id)
            hop_glyph = node_marker(ntype)[0]
            hop_color = name_rgb(name, node_id) if name and name != node_id else _AREA_TONE
            # The label of the pin is the first hash byte of the hop. The route graph gives
            # the same label to the same node, so the user can match a pin and a graph node
            # by eye.
            byte = node_id.lower().removeprefix("0x")[:2]
            verts.append(WalkVertex(east, north, hop_glyph, hop_color, False, label=byte))
            dist = haversine_km(self_pos[0], self_pos[1], pos[0], pos[1])
            if dist > far_dist:
                far_dist = dist
                far_id = node_id
                far_label = name if name and name != node_id else None
        return far_label, far_id, (verts if len(verts) >= 3 else None)

    def walk_reliability(
        far_id: str | None, far_label: str | None
    ) -> tuple[float, int, int] | None:
        """The observed reliability of a record: the success rate of traces to its far node.

        The route of a walk cannot be counted from the history, because a trace that timed
        out stores no path. But its farthest node is the target of the boomerang, and each
        trace stores its target, also the failed traces. Thus the success rate of the far
        node (matched against the stored targets by name or by hex hash) is a correct
        delivery figure. The sample is usually small (the count shows next to the rate).
        ``None`` when the node was never a target.
        """
        if not far_id:
            return None
        needle = far_id.lower().removeprefix("0x")
        wanted = far_label.lower() if far_label else None
        ok = total = 0
        for target, (t_ok, t_n) in target_counts.items():
            key = target.lower().removeprefix("0x")
            is_hex = bool(key) and all(c in "0123456789abcdef" for c in key)
            if (wanted is not None and key == wanted) or (
                is_hex and (key.startswith(needle) or needle.startswith(key))
            ):
                ok += t_ok
                total += t_n
        return (ok / total, ok, total) if total else None

    def ranked(category: Category) -> list[DiscoveredPath]:
        """The records of one discipline, best first, across all hash widths."""
        rows = ctx.repo.discoveries(category.id)
        rows.sort(key=lambda r: r.score, reverse=not category.ascending)
        return rows

    def describe(category: Category) -> list:
        """The heading and its wrapped description, as rows that the user cannot select."""
        rows: list = [section_heading(discipline_label(category, mark_lane))]
        desc = category.description
        if category.needs_positions and self_pos is None:
            desc += " — needs your location (set it in Config) to score"
        for line in textwrap.wrap(desc, _DESC_WRAP):
            rows.append(Separator(f"   {line}", style="muted"))
        return rows

    def browser_lanes(
        rank: int,
        category: Category,
        record: DiscoveredPath,
        *,
        show_width: bool,
        score_w: int,
    ) -> Text:
        """The fixed lanes of a record row: rank, date, score, and, if necessary, the width.

        The lanes are only as wide as their content: the day on which the record was set
        (the record page shows the time), and a score lane with the width of the widest
        score on this board, not a fixed twelve. Each cell that they do not use is a hop
        that the route can show. They are composed without a width, because nothing here
        fits itself to the terminal. This block is the same on each platform and at each
        size. That is also why it is the anchor of the horizontal scroll of the row (refer
        to :func:`browser_row`).
        """
        row = Text(f"#{rank} ", style="muted")
        row.append(record.discovered_at.astimezone().strftime("%b %d"), style="muted")
        row.append("  ")
        row.append(fit_cells(record_score(category, record), score_w), style="accent")
        if show_width:
            row.append(f" {record.width_bytes} B", style="muted")
        row.append("  ")
        return row

    def browser_row(lanes: Text, record: DiscoveredPath) -> Text:
        """One record row: its fixed lanes (:func:`browser_lanes`), then the full walk.

        The walk draws on the only path widget, with the two ends bare
        (:data:`~meshterm.ui.pathline.SELF_GLYPH`). A record is a boomerang by
        construction. To name our node two times in each row uses more cells than the full
        score lane, and tells the user what each other row already told. These stars keep
        the ``you`` white (``dim_self=False``). The dim colour means "not yours to
        compose", and nothing is composed on a board of walks that are already done.

        The line is composed at its **natural** length and is given whole. Thus each row is
        cut in the same way as the highlighted row: anchored on its first hop, and cracked
        at the edge of the lane (:func:`~meshterm.ui.pathline.cut_to`, which the list
        applies). On purpose, the line is not middle-elided to the width. That rescue keeps
        the two endpoints of a route when the right end is truncated. But on this board, the
        two endpoints are the same ``★`` on each row, by construction. A ``⋯`` uses three
        cells and a hop, only to show the one thing that the user already knows. The front
        of the walk is what changes from row to row, and each cell goes to it.

        To read a row that is longer than the lane, the user slides it. The highlight keeps
        the same line, and it scrolls horizontally under ←→ from the width of the lanes
        (``Choice.hscroll_from``). Thus the walk is the only thing on the row that moves.
        A scroll that moves the rank and the score off to the left loses the place of the
        user in the board, and gives the walk no cells that it does not already get. Thus a
        selected row and a row that is not selected are different in only one thing: the
        shift.
        """
        row = lanes.copy()
        walk = path_line(
            [None, *record.route, None],
            resolve,
            prefix_bytes=record.width_bytes,
            hash_bytes=record.width_bytes,
            self_name=device_label,
            bare_self=True,
            dim_self=False,
        )
        row.append_text(walk.text())
        return row

    async def delete_category_flow() -> None:
        """Select a discipline, confirm, and delete its records (all widths)."""
        rows: list = []
        lane = discipline_lane()
        for category in CATEGORIES:
            count = len(ctx.repo.discoveries(category.id))
            rows.append(
                Choice(
                    title=Text.assemble(
                        (f"{discipline_label(category, lane)}  ", ""),
                        (f"{count} record{'s' if count != 1 else ''}, all widths", "muted"),
                    ),
                    value=category.id,
                )
            )
        picked = await session.run_screen(
            SelectScreen(
                "Delete a discipline's records",
                rows,
                footer_hint="↑↓ move · Enter select · Esc back",
                filterable=False,
            )
        )
        if picked is CANCEL or picked is None:  # Esc
            return None
        category = CATEGORY_BY_ID[str(picked)]
        count = len(ctx.repo.discoveries(category.id))
        if await session.typed_confirm(
            f"This deletes all {count} {category.title} "
            f"record{'s' if count != 1 else ''} — every hash width. "
            "They can only be re-earned by walking them again.",
            "delete",
            title="Delete discipline records",
        ):
            ctx.repo.delete_discoveries(category.id)

    while True:
        items: list = []
        for category in CATEGORIES:
            items.extend(describe(category))
            board = ranked(category)
            if not board:
                items.append(Separator("   no records yet", style="muted"))
            show_width = len({r.width_bytes for r in board}) > 1
            # The score lane has the width of the widest score on this board. "3 nodes" and
            # "+6.0 dB" have different widths. A lane with the worst-case width on each board
            # puts the difference as padding in front of each route.
            score_w = max((cell_len(record_score(category, r)) for r in board), default=0)
            for rank, record in enumerate(board, start=1):
                lanes = browser_lanes(
                    rank,
                    category,
                    record,
                    show_width=show_width,
                    score_w=score_w,
                )
                items.append(
                    Choice(
                        # A finished line, not a callable that knows the width. The row does
                        # not fit itself to a width, so the list cuts each row (highlighted or
                        # not) in the same way (refer to browser_row). Records are frozen, so
                        # the line is composed once each time the screen opens, not at each
                        # paint.
                        title=browser_row(lanes, record),
                        value=("open", category, rank, record),
                        # ←→ slide only the walk. The lanes in front of it do not move
                        # (browser_row).
                        hscroll_from=lanes.cell_len,
                    )
                )
        total = len(ctx.repo.discoveries())
        if total:
            items.append(Separator(" "))
            items.append(
                Choice(
                    title=marked_label("🗑", "Delete a discipline's records…", "err"),
                    value=("del_cat", None, 0, None),
                )
            )
            items.append(
                Choice(
                    title=marked_label("🗑", "Delete all records…", "err"),
                    value=("del_all", None, 0, None),
                )
            )
        browser = SelectScreen(
            "Trophy case",
            items,
            footer_hint="↑↓ move · Enter open · Esc back",
            hscroll=True,  # a long walk slides under ←→, and is not cut at the edge
        )
        # A place, not a question: the trophy case is the page of the tool itself. Thus it
        # fills the frame from each entry point. From the menu, it is the only screen, and
        # it is drawn full-frame in all cases. From a trace, it opens over the trace screen,
        # which is still pushed. There it once showed as a box with the size of its content.
        browser.floating = False
        # The browser stays pushed for the full visit. Thus each dialog that belongs over the
        # trophy case (the discipline list, the delete confirms) already has the browser
        # drawn full-frame behind it. When the page of a record (also full-frame) pops, the
        # user is back on the browser, with the highlight still on the record that they
        # read. The loop leaves (and the list is built again, which loses that place) only
        # when the set of records changed.
        async with session.stay(browser) as visit:
            while True:
                picked = await visit.result()
                if picked is CANCEL or picked is None:  # Esc
                    return {"records": total}
                verb = picked[0]
                if verb == "del_cat":
                    await delete_category_flow()
                elif verb == "del_all":
                    if total and await session.typed_confirm(
                        f"This deletes all {total} records — every discipline, every width. "
                        "They can only be re-earned by walking them again.",
                        "delete",
                        title="Delete all records",
                    ):
                        ctx.repo.delete_discoveries()
                else:
                    _verb, category, rank, record = picked
                    far_label, far_id, shape = walk_drawing(record)
                    action = await session.run_screen(
                        RecordScreen(
                            record,
                            category,
                            rank,
                            resolve=resolve,
                            device_label=device_label,
                            device_hash=device_hash,
                            far_label=far_label,
                            far_id=far_id,
                            shape=shape,
                            reliability=walk_reliability(far_id, far_label),
                            type_of=lambda node_id: node_geo(node_id)[1],
                        )
                    )
                    if action == "trace":
                        # A walk of the path of a record opens the Trace screen above the
                        # trophy case, the same as each other sub-view. Esc from the trace goes
                        # back one step, to the trophy case, with the highlight on the record
                        # from which the trace started. ^W leaves the full excursion. (This
                        # flow once flattened the stack first and put the user on the main
                        # menu, because no key could leave a deep stack in one press. Now ^W
                        # does that.)
                        await open_trace_path(ctx, spec=record.spec)
                    elif action == "delete":
                        sure = await ctx.ui.dialog(
                            f"Delete this {category.title} record?",
                            [("Cancel", False), ("Delete", True)],
                            title="Delete record",
                            default=1,
                            destructive=True,
                        )
                        if sure:
                            ctx.repo.delete_discovery(record.id)
                # A trace can set a record, and each delete can remove some records. Thus the
                # board is built again only when the number of records changed. Only this
                # change is worth the loss of the highlight position.
                if len(ctx.repo.discoveries()) != total:
                    break
