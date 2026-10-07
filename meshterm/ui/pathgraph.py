# SPDX-License-Identifier: Apache-2.0
"""The only route-graph widget: hop sequences drawn as a flow of paths from left to right.

This widget was extracted from the Message paths dialog, so that each screen that draws
walked (or planned) routes draws them the same way. A *layer* is one path: its relay hops,
an edge colour, and a draw priority. The widget puts each distinct path between a shared
left endpoint marker and a shared right endpoint marker.

All paths share their two endpoints (an origin on the left, our node on the right), and
each path is walked from left to right. Thus the picture is a *flow*: routes **diverge**
from the origin, go on their own course, and **converge** back into our node. They share a
relay where their walks agree.

Earlier versions tried two other drawings. The first was a vertical *bus* that the lanes
joined at right angles. It looked like a metro map with no direction, and the bare 90°
turns hid that A flows to B. The second had oblique branches directly from each marker,
which made each relay off the lane a pointed peak or valley. The widget now draws the fan
as a **multilane highway**. A node is level in its lane, and a route changes lane only
between nodes, with one gentle shift, the same as a car that moves over one lane and then
goes straight. In detail:

* **Lanes**: the path with the highest *priority* (the spine, for example the route with
  the best evidence) holds the centre of the flow, with its relays in a straight run. The
  alternatives fan out above and below it, a fixed pitch of text rows apart. The priority
  sets the geometry. A separate *emphasis* rank selects which route is drawn emphasized (on
  top, so it wins any shared cell), and it moves no marker. Thus a caller can give the
  emphasis to a different route, and the layout of the picture does not change.

  The widget does not give each route a full-width lane of its own, because then the band
  is as tall as the number of routes, also where the routes almost do not overlap.
  Instead, the lanes are *packed by column*. The spine keeps its own relays, and each other
  node moves to the innermost free row above or below the spine, in its own column. Thus a
  column with one node off the spine uses one flanking row, however many routes cross the
  graph. Only a column where routes really stack uses the deeper rows.

  Then the widget *balances* which flank an alternative takes. Routes that share a column
  split above and below, instead of two deep on one flank while the other flank is empty
  (the band is only as tall as its deepest flank plus the other flank). Thus a fan of five
  routes, with a maximum of two nodes in each column, draws three lanes deep (the spine
  plus one flank on each side), not five.

  The two endpoints are at the vertical centre of the packed band, so the spine goes
  through them. The spine is fully straight when the flanks are equal and the number of
  lanes is odd. Otherwise, it leans gently to the centre. When the number of lanes is
  even, the widget opens one more padding row between the two central lanes, so that the
  endpoints are on an exact centred row between them.
* **Columns** (x): each node goes to a column by its *balanced* rank. This rank is its
  distance from the origin, divided by the sum of that distance and the remaining distance
  to our node. Thus the relays of a path spread evenly between the two ends, however long
  the other paths are, and a shared relay is in one place.
* **Level seating**: the line enters and leaves each node on a *level tangent*. Thus a
  relay that is off the lane of its neighbours looks like a gentle rise and settle, never a
  pointed peak or valley. Between two nodes on different lanes, the shift leaves the first
  node level, moves across, and arrives level at the second node. The only bends are the
  soft level→curve→level eases, never a bare right angle in open canvas. The curve spans the
  full gap, not a centred part with flat platforms on its two sides, because a corner
  between a platform and the curve draws a heavy braille *knee*. Thus the stroke goes
  continuously from node to node and stays an even, thin arc (refer to :data:`_CURVE_SPAN`).
* **Shared relays** draw as one marker (a route that uses a hop again is not a new node).
  The marker is on the lane of its path with the highest priority. A path with a lower
  priority that also goes through it leans off its lane to meet the marker, and then goes
  back. This shape shows the alternative as a branch through the shared node, which is
  exactly what the evidence tells.
* **Revisits**: that merge is correct across paths (two routes went through one relay), but
  it is wrong in one path. When one walk touches the same hop two times, that hop is not a
  node that two routes share. It is a **cycle**, and a flow from left to right has no place
  for a cycle. On a set of edges with a cycle, the relaxation of the balanced x never
  settles. Thus the members of the loop go to almost the same columns: markers one cell
  apart, labels on top of each other, and the back edge as a bare vertical line. Also, the
  relays outside the loop are pushed against the ends.

  Instead, ``allow_duplicate_nodes`` gives each revisit its own marker (refer to
  :func:`_split_revisits`). This keeps the flow without cycles, and draws the walk in its
  true order. The cost is that one node can show two times, so a caller that turns it on
  must tell the user so on the surface (:func:`~meshterm.ui.widgets.revisit_note`). It is
  opt-in, because the caller must make this choice. A path that MeshTerm *composed* (the
  scored walk of the Trophy case) collapses a revisit on purpose. But an **observed** via
  chain must be drawn as heard: its hops are named by a one-byte hash, so a repeat is as
  probably two nodes with the same hash byte as a real loop.
* **Detours**: a heard route that is a sibling route plus one inserted relay is that
  sibling with a detour. The widget measures the full fan against the row budget before it
  sets the shape. While there are enough rows, the detour **nests**: its inserted relay goes
  to the lane directly outside the lane of its sibling, on the same flank. Thus the straight
  run of the sibling clearly skips the relay, and the detour shows as the wider arc through
  the relay. Only a viewport that is too short for that extra lane **folds** the relay onto
  the lane of the sibling (the weakest detours first, with a new measurement after each
  fold). There, the run of the sibling passes over the relay. This fold is the last resort,
  which is easy to read but loses information. It is never the default (refer to
  :func:`_layout_lanes`).
* **Bypasses**: the opposite of a detour. Next to the ``A → B → C`` of the spine, there is
  a shorter route that skips ``B``. When ``A`` and ``C`` are on one lane, its ``A→C`` edge
  is a level run directly through the marker of the one node that it does not go through.
  If it is drawn that way, it looks like a route through ``B``, which the evidence excludes.
  When the column of the skipped node has space (a free lane next to it, or a lane that the
  row budget can open), the edge bends around instead. It goes in an arc through an
  unmarked *virtual waypoint* in that column, the same wider arc that a nested detour
  draws. Thus a skip looks like a skip (refer to :func:`_bypass_vias`). Only a graph with
  really no space left keeps the level pass-over.
* **Diverge and converge**: the fan at each end is a flow, not a switchboard. Routes leave
  the origin on top of each other on the centre line, and then go off one at a time to
  their lanes (the divergence). On the right, they do the opposite into our node (the
  convergence). Each edge draws exactly one time, however many routes share it. The only
  edge that draws as a bare vertical line is a pair of nodes walked in both directions.
  That is a real two-way hop, and the only place where a straight vertical line is true.
* **Emphasis dims the rest**: when one layer has more emphasis than the others, it is the
  only path in colour. Each node that it does not go through draws its marker and its
  label in one dark grey (:data:`_OFF_ROUTE`), the same dim colour as its unused lines. A
  fan is one route that the user compares with its alternatives, and markers in full hue
  along the grey lines contradicted what the emphasis told. The endpoints are never dimmed
  (all paths go through them), and the glyphs keep their shapes: a repeater off the route
  is still ``▲``, and only its colour is dim. When there is no emphasis to compare (one
  path, or a fan with all layers equal), nothing is dim, and the colours of the caller
  stay as they are.
* **Labels** are directly above or below their marker, on the side away from the middle of
  the graph, and a place with no drawn lines is preferred. An endpoint moves its label
  inward from the edge of the canvas, so that a long name is still next to its marker. A
  caller that wants a node without a label returns ``None`` for it.

All the drawing renders onto a :class:`~meshterm.ui.mapcanvas.MapCanvas`. The caller
supplies the callbacks for the glyph, the label, and the colour of each node. Thus the
widget does not have to know about the contact list or the theme.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise, permutations, product
from math import ceil

from ..services.topology import is_path_hash
from .mapcanvas import MapCanvas
from .marks import (  # noqa: F401 - the canonical home, re-exported for existing importers
    DST_NODE,
    RGB,
    SRC_NODE,
    GlyphOf,
    LabelOf,
    LabelRgbOf,
    parse_hex,
)
from .theme import mark_rgb

#: Joins a hop id to its occurrence index when ``allow_duplicate_nodes`` gives each revisit
#: of a path its own marker. It uses the same guarantee as the endpoint sentinels (NUL never
#: occurs in a hex hop id). Thus a qualified id can never be the same as a real id, and
#: :func:`_base_node` changes it back before any callback of the caller sees it.
_OCCURRENCE_SEP = "\x00#"

#: The text rows between the markers of two lanes that are next to each other: the pitch of
#: one lane. At the default, two blank rows pad each gap (space for the label of a lane and
#: the label of its neighbour). An even number of lanes opens one more row between the two
#: central lanes, so that the endpoints are on an exact centred row (refer to the block for
#: the vertical size). A graph with more lanes than the row budget can hold makes the pitch
#: smaller to fit. A graph with few lanes never makes the pitch larger than this value,
#: because more distance between the lanes only brings back the empty space that the layout
#: must prevent. Thus a sparse graph draws compact, not spread out.
_LANE_PITCH_ROWS = 3

#: The dots that are kept free past the outermost lane at each end of the graph: a label
#: row for the markers of that lane, plus a small space.
_GRAPH_END_DOTS = 8

#: The dot row in a character cell at which a horizontal edge line aims: the upper middle
#: of the 2×4 pixel grid of the cell (rows 0..3 from the top). There, a line one dot thick
#: looks like it goes through the glyph, not along its bottom edge. The y of each lane snaps
#: onto this row (refer to :func:`_mid_row`), so that a level run stays level and the
#: markers in the same column align.
_CELL_MID_DOT = 2

#: The margin, in dots, between the endpoint markers and the edges of the canvas.
_GRAPH_PAD_DOTS = 6

#: The part of the horizontal column gap of a lane change that the eased curve uses. If the
#: value is smaller, the rest splits into two flat platforms, one on each side of the curve.
#: The value is the full gap: the curve goes continuously from one node to the next, with
#: no separate flat segment. A shorter curve leaves flat platforms, but it brings back a
#: **corner** where a platform (slope 0) meets the rising curve. That corner makes a heavy
#: braille *knee* (a cell with 3 or 4 dots filled) that looks thick, and more so when the
#: end tangent of the curve is shallower. When the curve spans the full gap, there is no
#: corner, so the stroke stays an even, thin arc from end to end. The nodes are still
#: level, because the end tangents of the curve stay horizontal (refer to :data:`_BEND_K`).
#: Thus each marker has a flat point, without a platform run. (In a very narrow column, a
#: shift of a full lane is necessarily steep up to the node, so its marker is on a gentle
#: slope, not fully level. That is the true cost of a thin stroke there.)
_CURVE_SPAN = 1.0

#: The shortest horizontal run that a lane change gets, also for a hop of one lane. Thus a
#: narrow column gap still bends across some dots, and does not jump in one abrupt step.
_MIN_SHIFT_DOTS = 4

#: The distance of the bezier control points in from each end, as a fraction of the
#: horizontal span of the shift. Each control point is level with its own end, so the curve
#: leaves and enters each node horizontally. Now that the curve spans the full gap
#: (:data:`_CURVE_SPAN`) with no separate platform, this flat end tangent is what keeps a
#: node level. The value is a balance with thinness. A higher value keeps the tangent flat
#: for longer (a better-seated node), but makes the middle of the curve steeper to
#: compensate (a thicker centre). A lower value spreads the drop more evenly (thinner), but
#: tilts the tangent at the node (a marker on a slope). The value is tuned to keep the
#: marker almost level while the stroke stays an even, thin arc between nodes.
_BEND_K = 0.4

#: The distance in dots from the flow arrow to our marker, on its left: one cell. Thus the
#: arrow is part of the trunk as ``▶★``, and it shows the direction node → our node with
#: enough space for the endpoint.
_ARROW_GAP_DOTS = 2

#: The dim colour of each node off the emphasized path, for its marker and for its label,
#: in the two encodings that the callbacks use. The purpose of a fan is to compare one route
#: with its alternatives. The picture shows which route that is only if all the rest is
#: dim. The unused lines already draw grey, and a marker in full hue with a label in full
#: hue on such a line is the most visible thing in the frame (JP, 2026-09-04). Each value in
#: this range folds to the dark-grey slot of the console, so the rule stays the same after
#: the 16-colour quantizer.
_OFF_ROUTE: RGB = (110, 110, 110)
_OFF_ROUTE_HEX = "#{:02x}{:02x}{:02x}".format(*_OFF_ROUTE)

#: The maximum number of distinct paths for which an exhaustive search optimizes the lane
#: order. Past this number, the search space (``(n-1)!`` orders of the lanes that are not
#: central) is too large, so a barycentre heuristic seats the lanes instead. The real inputs
#: of the widget are far below this number.
_MAX_EXACT_LANES = 8

#: The number of sweeps of the barycentre heuristic for the lane order. It is used past
#: :data:`_MAX_EXACT_LANES`.
_ORDER_SWEEPS = 8

#: The maximum number of routes (other than the best route) that the side balancer examines
#: by exhaustive 2-colouring (refer to :func:`_balance_sides`). Past this number, there are
#: too many ``2**m`` side assignments, so the routes keep the side that the jog order gave
#: them. The real inputs of the widget are far below this number.
_MAX_BALANCE_ROUTES = 16

#: The glyph of the flow arrow in the trunk directly before our node, so that the full flow
#: reads better as a directed run (node → our node), not as a map with no direction.
# _ARROW_GLYPH = "▶"
_ARROW_GLYPH = ""

#: How much the emphasis of a layer outranks its layout priority when edges compete for a
#: cell. The emphasis applies at draw time: the selected path draws on top and wins any
#: shared cell. It must be larger than any range of priority values, so it is scaled far
#: past the small priorities that the widget gets. The layout geometry ignores the emphasis
#: fully. Only the colour and the z-order of the drawn edges follow it (refer to
#: :func:`_draw_rank`). Thus when a different path gets the emphasis, the same picture is
#: drawn again in new colours, and its layout does not change.
_EMPHASIS_BOOST = 1_000_000


@dataclass(frozen=True)
class PathLayer:
    """One path drawn on the route graph.

    Attributes:
        hops: The relay node ids between the endpoints, in walk order. If it is empty, the
            path goes straight across from endpoint to endpoint.
        color: The colour of the edges of the path.
        priority: The **layout** rank, which selects the spine. The path with the highest
            priority takes the straight centre lane and owns each relay that it shares
            (weaker paths jog to meet it). Thus it sets the column and the lane of each
            node. The geometry depends only on this value, never on :attr:`emphasis`. A
            caller that wants the drawn picture to stay the same while it moves the emphasis
            keeps the priority of each path constant.
        emphasis: The **draw** rank: the emphasis, on top of the fixed geometry. Where edges
            share a cell (or cross), the path with more emphasis gets the colour and draws
            on top. It moves no marker. The default is ``0`` (all paths are equal, so the
            colour comes from :attr:`priority` as before). A caller that shows the selection
            by emphasis changes this value, not the priority. Thus the layout stays the
            same, and only the colours change.
    """

    hops: tuple[str, ...]
    color: RGB
    priority: int
    emphasis: int = 0


def _mid_row(y_dot: float) -> int:
    """Snap a dot row onto the upper-middle dot of its character cell.

    A braille cell is four dot rows tall. A horizontal line on the top row or on the bottom
    row is at the edge of the glyph, and looks too high or too low. When the y of each lane
    snaps to :data:`_CELL_MID_DOT`, the markers (and the level runs along a lane) stay
    centred in the pixel space of the cell.
    """
    return round((y_dot - _CELL_MID_DOT) / 4) * 4 + _CELL_MID_DOT


def _collapse(layers: Sequence[PathLayer]) -> list[PathLayer]:
    """Fold layers with identical hop sequences onto one drawn path.

    When a known route is walked again, the route gets the emphasis, and it does not draw
    two times. The geometry keeps the strongest ``priority`` of the folded copies (so a
    shared route is where its best copy would be). The emphasis (the drawn colour and the
    order on top) follows the copy with the most emphasis, and that emphasis goes to the
    folded path. Thus two routes that are different only in an order inside a cluster,
    which the caller removed, are on one line. A selection of either route gives that line
    the emphasis, also when a copy with a higher priority owns the layout.
    """
    drawn: list[PathLayer] = []
    by_hops: dict[tuple[str, ...], int] = {}
    for layer in layers:
        at = by_hops.get(layer.hops)
        if at is None:
            by_hops[layer.hops] = len(drawn)
            drawn.append(layer)
        else:
            cur = drawn[at]
            top = layer if _draw_rank(layer) > _draw_rank(cur) else cur
            drawn[at] = PathLayer(
                cur.hops,
                top.color,
                max(layer.priority, cur.priority),
                max(layer.emphasis, cur.emphasis),
            )
    return drawn


def _coalesce_prefixes(layers: Sequence[PathLayer]) -> list[PathLayer]:
    """Fold a hop with too few bytes into the only longer id that it can be, across the layers.

    A relay can come into the graph at different hash widths in different paths: a 1-byte
    trace hop (``be``) next to a wider id of the same node (``be1d1c1dbc4b``). If they are
    drawn as they are, the node shows two times: two markers a column apart, often with the
    same resolved name. The graph of the observed topology coalesces what it can, but only
    where a hash has one meaning in the full contact list. A hash that starts two contacts
    (``be`` also starts ``bedd2b``) comes through to this point, although in these paths it
    can only be the one wide node that is present.

    Thus the widget closes the gap locally, with only the ids that the layers hold. It
    rewrites a short id to the longer id of which it is a strict prefix, when exactly one
    such id is present (and it follows a chain to its longest end). A short id that is a
    prefix of two distinct nodes that are present is really ambiguous, and stays as it is.
    Then the callbacks for each node see one id for each node, and any paths that the
    rewrite made identical collapse together later.
    """
    ids = {hop for layer in layers for hop in layer.hops}
    remap: dict[str, str] = {}
    remaining = set(ids)
    while True:
        pair = _prefix_merge(remaining)
        if pair is None:
            break
        short, long = pair
        remap[short] = long
        remaining.discard(short)
    if not remap:
        return list(layers)

    def resolved(hop: str) -> str:
        for _ in range(len(remap) + 1):  # follow a rewrite chain, with a guard against cycles
            if hop not in remap:
                break
            hop = remap[hop]
        return hop

    return [
        PathLayer(
            tuple(resolved(hop) for hop in layer.hops),
            layer.color,
            layer.priority,
            layer.emphasis,
        )
        for layer in layers
    ]


def _prefix_merge(nodes: set[str]) -> tuple[str, str] | None:
    """The next ``(short, long)`` id pair to fold among ``nodes``, else ``None``.

    A short hex id (less than the full width of 6 bytes) folds when all the longer present
    ids that extend it are on one prefix chain (the longest id starts with each of the
    others). Then the short id can only name that one node. The shortest ids come first, so
    that a chain collapses from its end.
    """
    for short in sorted(nodes, key=len):
        if len(short) >= 12 or not is_path_hash(short):
            continue
        exts = [
            other
            for other in nodes
            if other != short
            and len(other) > len(short)
            and is_path_hash(other)
            and other.startswith(short)
        ]
        if not exts:
            continue
        longest = max(exts, key=len)
        if all(longest.startswith(ext) for ext in exts):
            return short, longest
    return None


def revisited_hops(hops: Sequence[str]) -> tuple[str, ...]:
    """The hops that one path touches more than one time, in the order of first appearance.

    A caller applies this test before it decides to draw with ``allow_duplicate_nodes``.
    Then the caller names these hops in its warning (:func:`~meshterm.ui.widgets.revisit_note`),
    because a graph that draws one node two times must tell the user at least that. The test
    uses the ids as given. An observed via chain addresses its hops with one byte. At that
    width, a repeat is as probably two different nodes with the same hash byte as a packet
    that really walked a loop. Neither the path nor this widget can tell them apart, and
    that is exactly what the warning says.
    """
    counts: dict[str, int] = {}
    for hop in hops:
        if hop:
            counts[hop] = counts.get(hop, 0) + 1
    return tuple(hop for hop, seen in counts.items() if seen > 1)


def _split_revisits(layers: Sequence[PathLayer]) -> list[PathLayer]:
    r"""Give each revisit in a path its own node id, so that no walk folds into a cycle.

    The k-th occurrence of a hop in one layer becomes ``hop\\x00#k`` (the first occurrence
    keeps the bare id). Thus the flow has no cycle, and the balanced rank can spread the walk
    evenly again. The count is done for each layer, but the qualifier is positional. Thus
    occurrence *k* of a hop is the same id in each layer that goes that far. Two routes that
    go through one relay one time still merge on the bare id. The diverge and converge
    picture, which is the purpose of the widget, does not change, and only a real repeat in
    one path splits.
    """
    split: list[PathLayer] = []
    for layer in layers:
        seen: dict[str, int] = {}
        hops: list[str] = []
        for hop in layer.hops:
            nth = seen.get(hop, 0)
            seen[hop] = nth + 1
            hops.append(hop if nth == 0 else f"{hop}{_OCCURRENCE_SEP}{nth}")
        split.append(PathLayer(tuple(hops), layer.color, layer.priority, layer.emphasis))
    return split


def _base_node(node: str) -> str:
    """Change an internal id with an occurrence qualifier back to the node id of the caller.

    An id without a qualifier does not change.
    """
    return node.split(_OCCURRENCE_SEP, 1)[0]


def _unqualified(
    glyph_of: GlyphOf, label_of: LabelOf, label_rgb_of: LabelRgbOf
) -> tuple[GlyphOf, LabelOf, LabelRgbOf]:
    """Wrap the callbacks for each node, so that they get a split revisit as its real node.

    The occurrence qualifiers are private data of the widget. A caller supplies callbacks
    keyed on its own hop ids, and gets a marker, a label, and a hue for each node. Thus the
    two markers of a revisited hop draw identically (the same glyph, the same name, the same
    colour). The picture says "here twice", and does not make up a second identity.
    """

    def glyph(node: str) -> tuple[str, str]:
        return glyph_of(_base_node(node))

    def label(node: str) -> str | None:
        return label_of(_base_node(node))

    def label_rgb(node: str) -> RGB:
        return label_rgb_of(_base_node(node))

    return glyph, label, label_rgb


def _draw_rank(layer: PathLayer) -> int:
    """The draw rank for the edges of a layer: first the emphasis, then the layout priority.

    It controls only which colour wins a shared cell and which edge draws on top, never the
    placement of a node (only :attr:`PathLayer.priority` sets that). Thus a caller can give
    the emphasis to a different path (it increases its :attr:`~PathLayer.emphasis`). Then the
    graph is drawn again in new colours over the same layout, and the layout does not change
    because of a new spine.
    """
    return layer.priority + layer.emphasis * _EMPHASIS_BOOST


def _highlighted(drawn: Sequence[PathLayer], seqs: Sequence[tuple[str, ...]]) -> set[str] | None:
    """The nodes on the one emphasized path, or ``None`` when no single path has the emphasis.

    An emphasis is a *comparison*, so at least two paths and exactly one winner are
    necessary. One path alone has nothing to compare with. A fan whose layers all have the
    same emphasis (the default: a picture with no selection) has no single route, so nothing
    is dimmed. In both cases, the function returns ``None``, and then the colours of the
    caller are used exactly as given.

    The returned set has the endpoints, because all paths go through them. A selection never
    makes the origin or our node different.
    """
    if len(drawn) < 2:
        return None
    top = max(layer.emphasis for layer in drawn)
    lit = [i for i, layer in enumerate(drawn) if layer.emphasis == top]
    return set(seqs[lit[0]]) if len(lit) == 1 else None


def _dim_off_route(
    glyph_of: GlyphOf, label_rgb_of: LabelRgbOf, lit: set[str]
) -> tuple[GlyphOf, LabelRgbOf]:
    """Wrap the marker and label colour callbacks to draw all nodes outside ``lit`` grey.

    The callbacks of the caller are still asked about each node on the emphasized path, so a
    node there keeps the hue that its screen gives it. Off the path, ``label_rgb_of`` is not
    called, and the colour that ``glyph_of`` returns is not used. The glyph does not change:
    a repeater off the route is still ``▲``, and only its colour is dim.
    """

    def glyph(node: str) -> tuple[str, str]:
        mark, colour = glyph_of(node)
        return mark, (colour if node in lit else _OFF_ROUTE_HEX)

    def rgb(node: str) -> RGB:
        return label_rgb_of(node) if node in lit else _OFF_ROUTE

    return glyph, rgb


def render_path_graph(
    layers: Sequence[PathLayer],
    width: int,
    *,
    glyph_of: GlyphOf,
    label_of: LabelOf,
    label_rgb_of: LabelRgbOf,
    min_rows: int = 5,
    max_rows: int = 15,
    lane_pitch: int = _LANE_PITCH_ROWS,
    allow_duplicate_nodes: bool = False,
) -> list[str]:
    """Draw the diverge/converge route-flow graph and return its ANSI lines.

    Args:
        layers: The paths to draw. The highest ``priority`` takes the straight centre lane and
            sets the geometry. A separate ``emphasis`` (default ``0``) decides which path is
            drawn with emphasis (on top, so it wins any shared cell), and it moves no marker.
            Thus when a different path gets the emphasis, the same layout is drawn again in
            new colours. Layers with identical hop sequences collapse to one drawn path,
            which the highest priority among them owns.
        width: The width of the canvas, in cells.
        glyph_of: The marker glyph and its colour for each node id (the endpoints use the
            keys :data:`SRC_NODE` and :data:`DST_NODE`). The colour is any value that
            :func:`~meshterm.ui.theme.mark_rgb` accepts: a literal ``#rrggbb`` or the name of
            a theme style.
        label_of: The label text for each node id (``None`` or ``""`` gives a bare marker).
            A label stays complete, but if it is wider than the canvas, it is ellipsized to
            fit.
        label_rgb_of: The label colour for each node id.
        min_rows: The minimum number of canvas rows to draw, however few lanes there are.
        max_rows: The maximum number of canvas rows to use. A graph with more lanes than can
            fit makes the spacing of its lanes smaller, instead of growing past this number.
        lane_pitch: The text rows between the markers of lanes that are next to each other
            (thus ``lane_pitch - 1`` blank rows pad each gap). An even number of lanes opens
            one more row between the two central lanes, so that the endpoints are on an
            exact centred row. The row budget makes the pitch smaller when there are many
            lanes, and never makes it larger when there are few. The default is
            :data:`_LANE_PITCH_ROWS`.
        allow_duplicate_nodes: Draw a hop that a path touches two times as two markers,
            instead of one folded marker. It is off by default, because the fold is correct
            for a path that the caller composed. Turn it on for an **observed** walk. For
            such a walk, the fold makes a cycle that the flow from left to right cannot
            seat, and the layout collapses (refer to the module docstring). With this
            argument, the walk draws in its true order. The cost is that one node can show
            two times, and the caller must tell the user so on the surface
            (:func:`~meshterm.ui.widgets.revisit_note`, with the result of
            :func:`revisited_hops`). Relays that two paths share merge in both cases.

    Returns:
        One ANSI string for each canvas row (an empty list when there are no layers to
        draw).
    """
    if not layers:
        return []

    drawn = _collapse(_coalesce_prefixes(layers))
    if allow_duplicate_nodes:
        # After the prefix fold and the identical-path fold, so that the count of revisits uses
        # the ids that are drawn. A hop that only looks repeated at two hash widths first
        # coalesces to one id, and then it is correctly counted as one visit.
        drawn = _split_revisits(drawn)
        glyph_of, label_of, label_rgb_of = _unqualified(glyph_of, label_of, label_rgb_of)
    seqs = [(SRC_NODE, *layer.hops, DST_NODE) for layer in drawn]

    # When one path has more emphasis than the rest, all that is not on it is dim. The nodes
    # of the emphasized path keep their hue, and each other marker and label draws in the
    # :data:`_OFF_ROUTE` grey. The membership test uses the graph's own id space (after
    # the prefix coalesce, the identical-path collapse, and the revisit split). Thus a relay
    # that the selected route reaches by a short hash still counts as on the route, after it
    # is folded into the wide marker that is drawn for it.
    lit = _highlighted(drawn, seqs)
    if lit is not None:
        glyph_of, label_rgb_of = _dim_off_route(glyph_of, label_rgb_of, lit)

    # The order of first appearance for each node, so that the layout is identical at each
    # paint. A set of string ids with a hash seed iterates in an order that changes from run
    # to run. Then the passes settle differently each time, and the graph jumps between
    # frames.
    ordered_nodes: list[str] = []
    seen: set[str] = set()
    edges: set[tuple[str, str]] = set()
    for seq in seqs:
        for node in seq:
            if node not in seen:
                seen.add(node)
                ordered_nodes.append(node)
        edges.update(pairwise(seq))

    xfrac = _balanced_x(ordered_nodes, edges)

    # A node draws on the path with the highest priority that owns it: the first path (by
    # priority, then by appearance) that goes through it. Thus a shared relay is drawn one
    # time, on the strongest route through it, and weaker routes jog to meet it. A path that
    # adds no node of its own (a stronger route already owns each of its hops) is *subsumed*:
    # it gets no lane, and it goes through the markers that other paths placed. Thus it costs
    # no empty band.
    best = max(range(len(drawn)), key=lambda j: drawn[j].priority)
    owner: dict[str, int] = {}
    for i in sorted(range(len(drawn)), key=lambda j: -drawn[j].priority):
        for node in seqs[i]:
            owner.setdefault(node, i)
    span = width * 2 - 2 * _GRAPH_PAD_DOTS

    def col_of(node: str) -> int:
        return (_GRAPH_PAD_DOTS + round(xfrac[node] * span)) >> 1

    # Seat each node on a signed lane. Measure the full fan against the row budget before the
    # shape is set: detour routes nest outside their sibling while there are enough rows, and
    # fold onto the lane of the sibling only as the last resort. Refer to :func:`_layout_lanes`.
    signed = _layout_lanes(drawn, seqs, ordered_nodes, owner, best, col_of, max_rows, lane_pitch)

    # A pair walked in both directions draws as the one true vertical line (the edge pass
    # below). It also never bends around anything.
    bidir = {frozenset((u, v)) for (u, v) in edges if (v, u) in edges}
    # An edge that would go level directly through a marker that it skips bends around the
    # marker instead, through a virtual waypoint in the column of the skipped node, when the
    # column has space (refer to :func:`_bypass_vias`). The lanes of the waypoints
    # (``via_lanes``) are part of the band extent below, so the vertical size includes each
    # lane that a bypass opens.
    vias = _bypass_vias(seqs, bidir, signed, col_of, max_rows, lane_pitch)
    via_lanes = [lane for hops in vias.values() for _m, lane in hops]

    # The endpoints are at the vertical centre of the compressed lane band, where the
    # strongest route goes through them as the spine of the graph. The alternatives fan out
    # above and below. When the flanks are equal, the lane of the spine is that centre, and
    # the spine is fully straight. When the band is not balanced (or has an even number of
    # lanes), the centre is between lanes. Then the best path eases gently to reach the
    # endpoints, instead of a full graph that is off the centre. The balancer keeps this lean
    # small, because it makes the flanks equal first.
    low = min([*signed.values(), *via_lanes], default=0)
    high = max([*signed.values(), *via_lanes], default=0)
    max_lane = high - low
    centre_lane = max_lane / 2.0
    node_lane: dict[str, float] = {
        node: (centre_lane if node in (SRC_NODE, DST_NODE) else float(signed[node] - low))
        for node in ordered_nodes
    }

    # -- Vertical size. Each lane is on its own text row, ``lane_pitch`` rows apart (thus
    # ``lane_pitch - 1`` blank rows pad each gap). For an even number of lanes, the two
    # central lanes are one more row apart. Thus the endpoints, which are pinned to the
    # centre of the band, are on the exact middle row between them, not on a fractional row
    # that snaps off it. For an odd number, a central lane is already there for the
    # endpoints. A graph that is too tall for ``max_rows`` makes each row proportionally
    # smaller (never larger), so that it stays compact.
    even_lanes = max_lane % 2 == 1  # N = max_lane + 1 lanes, so even ⟺ max_lane odd
    lower_centre = max_lane // 2 + 1  # the first lane below the centre (used only when even)

    def slot(lane: float) -> float:
        """The unscaled text row of a lane index, with the centre gap for an even count."""
        rows_out = lane * lane_pitch
        if even_lanes and lane > lower_centre - 1:
            # the lower central lane and all lanes below it move one row down. The half lane
            # of the endpoints takes half of that row, so the endpoints are in the middle of
            # the wider gap.
            rows_out += min(1.0, lane - (lower_centre - 1))
        return rows_out

    if max_lane <= 0:
        rows = min_rows
        lane_rows = 0.0
        scale = 0.0
    else:
        lane_rows = slot(float(max_lane))  # the total lane span, in rows
        ideal = lane_rows * 4 + 2 * _GRAPH_END_DOTS
        rows = max(min_rows, min(max_rows, ceil(ideal / 4)))
        avail = rows * 4 - 2 * _GRAPH_END_DOTS
        scale = min(1.0, avail / (lane_rows * 4)) if lane_rows else 0.0

    canvas = MapCanvas(width, rows)
    band = lane_rows * 4 * scale
    # Centre the band, with its anchor on a row at the middle of a cell, so that the
    # (unscaled) integer lane rows are exactly on their cells. Thus there is no snap drift
    # for each lane that can move a centred endpoint off its middle.
    top = float(_mid_row((rows * 4 - band) / 2))

    def x_of(node: str) -> int:
        return _GRAPH_PAD_DOTS + round(xfrac[node] * span)

    pos: dict[str, tuple[int, int]] = {
        node: (x_of(node), _mid_row(top + slot(node_lane[node]) * 4 * scale))
        for node in ordered_nodes
    }
    # Each bypass waypoint becomes a dot point at the exact x of the skipped marker, on its
    # own lane row, through the same slot and snap as the real nodes. Thus the level peak of
    # the arc is exactly over the node that it goes around.
    via_pts: dict[frozenset[str], list[tuple[float, float]]] = {
        key: [
            (float(pos[m][0]), float(_mid_row(top + slot(float(lane - low)) * 4 * scale)))
            for m, lane in hops
        ]
        for key, hops in vias.items()
    }
    # -- Edges. Collect each edge one time, keyed by its unordered node pair. An edge that two
    # routes share (or a pair walked in both directions) must draw only one time. If not, it
    # becomes a double line, one dot off itself (the Bresenham runs of two routes never go on
    # exactly the same dots). Each pair keeps the colour and the draw rank of the strongest
    # route through it. The strength is the draw rank, so that the emphasized route wins a
    # shared edge over a spine that only has a higher priority. Thus the emphasis colours the
    # full selected route, and does not stop where it overlaps another route. A two-way pair
    # (``bidir``, above) draws as the one true vertical line, not as a lane change.
    edge_style: dict[frozenset[str], tuple[int, RGB]] = {}
    for layer, seq in sorted(zip(drawn, seqs, strict=True), key=lambda ls: _draw_rank(ls[0])):
        rank = _draw_rank(layer)
        for u, v in pairwise(seq):
            key = frozenset((u, v))
            prev = edge_style.get(key)
            if prev is None or rank > prev[0]:
                edge_style[key] = (rank, layer.color)
    # Draw in ascending order of draw rank, so that the colour of the strongest route (or the
    # route with the most emphasis) wins any cell that two edges share, and is on top.
    for key, (rank, color) in sorted(edge_style.items(), key=lambda kv: kv[1][0]):
        u, v = tuple(key)
        canvas.draw_line(_route(u, v, pos, key in bidir, via_pts.get(key, ())), color, rank)

    # -- An arrow in the trunk directly before our node, so that the full flow reads as a
    # directed run (node → our node), not as a map with no direction. It is one glyph in the
    # colour of the spine. It reserves its cell, so the endpoint label goes around it and does
    # not collide with stray dots.
    ax, ay = pos[DST_NODE]
    canvas.marker(ax - _ARROW_GAP_DOTS, ay, _ARROW_GLYPH, drawn[best].color)

    # -- Markers for each node (each endpoint and each relay draws one time).
    for node in ordered_nodes:
        glyph, colour = glyph_of(node)
        canvas.marker(*pos[node], glyph, mark_rgb(colour))

    _place_labels(canvas, ordered_nodes, pos, node_lane, width, rows * 2, label_of, label_rgb_of)
    return canvas.to_ansi_lines()


def _route(
    u: str,
    v: str,
    pos: dict[str, tuple[int, int]],
    bidir: bool,
    vias: Sequence[tuple[float, float]] = (),
) -> list[tuple[float, float]]:
    """The point chain for one edge (in dot coordinates), drawn as a multilane-highway flow.

    Two nodes on the same lane join with a level run. Two nodes on different lanes join with
    one smooth **shift**: a bezier S that leaves the first marker level, moves across the
    lanes between them, and arrives level at the second marker (refer to :func:`_sbend`).
    Thus there is no corner, only the eased level→curve→level of the shift.

    The shift spans the full column gap (:data:`_CURVE_SPAN`), not a centred part with flat
    platforms on its two sides. A platform meets the rising curve at a corner, and that
    corner draws a heavy braille *knee*. Thus the curve goes continuously from node to node,
    and the marker is on the level end tangent of the curve. The endpoints are at the centre
    of the lane band, so the branches that diverge from the origin and the merges that
    converge into our node come from this one rule, with no special case for the endpoints.

    ``vias`` are the bypass waypoints of an edge (:func:`_bypass_vias`), between the two
    markers in x order. The run applies the same level-or-shift rule from anchor to anchor
    (marker to waypoint to waypoint to marker). Thus a bypass eases out, is level for an
    instant exactly over the marker that it goes around, and eases back in. It never cuts
    through the marker. The only exception is ``bidir``: a pair walked in both directions
    draws as one straight segment between the markers (almost vertical when the layout
    stacks them). That is the only place where a vertical line is the true picture, and it
    is never bent.
    """
    (xu, yu), (xv, yv) = pos[u], pos[v]
    if bidir:
        return [(xu, yu), (xv, yv)]
    if xu > xv:  # orient the trapezium left→right (balanced rank can only tie, never invert)
        (xu, yu), (xv, yv) = (xv, yv), (xu, yu)
    anchors: list[tuple[float, float]] = [(xu, yu), *sorted(vias), (xv, yv)]
    pts: list[tuple[float, float]] = [anchors[0]]
    for (xa, ya), (xb, yb) in pairwise(anchors):
        if ya == yb:
            pts.append((xb, yb))
            continue
        dx = xb - xa
        shift = min(dx, max(_MIN_SHIFT_DOTS, round(dx * _CURVE_SPAN)))
        stub = (dx - shift) // 2
        # A bezier S across the full gap. It has level tangents at the two ends, so it eases
        # out of and back into each anchor with no corner, and with no platform corner that
        # makes a heavy knee.
        pts.extend([*_sbend(xa + stub, ya, xb - stub, yb), (xb, yb)])
    return pts


def _sbend(x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, float]]:
    """Sample a cubic-bezier S-curve from ``(x0, y0)`` to ``(x1, y1)``, level at the two ends.

    Each control point is level with its own endpoint (:data:`_BEND_K` of the span in from
    each side). Thus the tangent of the curve is horizontal where it meets the platforms: a
    smooth lane change that moves across and settles, instead of a hard diagonal. The samples
    are dense enough that the polyline rasterizes as a continuous curve. The convex hull
    keeps the curve inside the ``(x0, y0)–(x1, y1)`` box, so it never goes past its lane or
    column.
    """
    cx0 = x0 + _BEND_K * (x1 - x0)
    cx1 = x1 - _BEND_K * (x1 - x0)
    samples = max(4, int(abs(x1 - x0) + abs(y1 - y0)))
    pts: list[tuple[float, float]] = []
    for i in range(samples + 1):
        t = i / samples
        mt = 1.0 - t
        a, b, c, d = mt * mt * mt, 3 * mt * mt * t, 3 * mt * t * t, t * t * t
        pts.append((a * x0 + b * cx0 + c * cx1 + d * x1, a * y0 + b * y0 + c * y1 + d * y1))
    return pts


def _balanced_x(ordered_nodes: list[str], edges: set[tuple[str, str]]) -> dict[str, float]:
    """The x of each node in ``0..1``: the balanced rank from the origin to our node.

    The fraction of a node is its longest-path distance from the origin, divided by the sum
    of that distance and its longest remaining distance to our node. Thus the origin is at
    ``0``, our node is at ``1``, and each other node is between them, in proportion to how
    far along its route it is. The relays of a path spread evenly between the two ends,
    however many hops the other paths have. For example, one relay on a route of one hop
    goes to the middle of the canvas, not against the origin with a long edge in an arc
    across to our node.

    A relay that routes of different lengths share still gets one x. Each edge increases
    the distance from the origin and decreases the distance to our node. Thus the fraction
    increases strictly along each path: edges always go from left to right.

    A pair walked in both directions is a 2-cycle in the edge set. Without a merge, the
    longest-path relaxation loops through it, increases each downstream depth up to the
    limit of the node count, and pushes other relays hard against the ends. Thus the rank
    runs over the graph with each such pair (transitively) merged into one representative.
    That is the flow without cycles that the picture really is. The pair draws as one
    vertical line, so it correctly shares an x in any case.
    """
    rep = _merge_bidir_pairs(ordered_nodes, edges)
    reps: list[str] = []
    seen: set[str] = set()
    for node in ordered_nodes:
        if rep[node] not in seen:
            seen.add(rep[node])
            reps.append(rep[node])
    rep_edges = {(rep[u], rep[v]) for u, v in edges if rep[u] != rep[v]}
    up = _longest_paths(reps, rep_edges)
    down = _longest_paths(reps, {(v, u) for u, v in rep_edges})
    frac = {r: (up[r] / (up[r] + down[r])) if (up[r] + down[r]) else 0.0 for r in reps}
    return {node: frac[rep[node]] for node in ordered_nodes}


def _merge_bidir_pairs(ordered_nodes: list[str], edges: set[tuple[str, str]]) -> dict[str, str]:
    """Map each node to a representative, and unite each two nodes that a two-way edge joins.

    Two nodes walked in both directions make a 2-cycle. The function unites them
    transitively, so a chain of such pairs folds into one group. Thus the balanced rank can
    treat the flow as the run without cycles that it is in all other respects. A node in no
    such pair maps to itself. It is not important which member is the representative: each
    member gets the one fraction of the group, and the rank of the group is structural.
    """
    parent = {node: node for node in ordered_nodes}

    def find(node: str) -> str:
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:  # compress the path
            parent[node], node = root, parent[node]
        return root

    for u, v in edges:
        if (v, u) in edges:
            ru, rv = find(u), find(v)
            if ru != rv:
                parent[ru] = rv
    return {node: find(node) for node in ordered_nodes}


def bidir_clusters(sequences: Sequence[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """The groups of three or more nodes that make a bidirectional cluster (a flow SCC).

    Two nodes walked in both directions are a 2-cycle, which the graph draws as one tidy
    vertical pair. That is good by itself. Three or more nodes linked to each other in that
    way are a strongly connected knot that the flow from left to right cannot put in order.
    :func:`_merge_bidir_pairs` collapses them all onto one column, where their markers and
    labels go on top of each other and nobody can read them. A caller can give its path
    sequences (with the endpoints) to this function, to find those knots and contract each
    knot to one super-node before it draws. Thus the cluster shows as one marker, not as a
    jam. This is the only true way to seat a cycle in a DAG layout.

    Returns the member ids of each cluster in the order of first appearance (without the
    endpoints). One node alone, or the tidy pair of two nodes, is not a cluster, and the
    function does not return it.
    """
    edges = {pair for seq in sequences for pair in pairwise(seq)}
    ordered = list(dict.fromkeys(node for seq in sequences for node in seq))
    rep = _merge_bidir_pairs(ordered, edges)
    groups: dict[str, list[str]] = {}
    for node in ordered:
        if node in (SRC_NODE, DST_NODE):
            continue
        groups.setdefault(rep[node], []).append(node)
    return [tuple(members) for members in groups.values() if len(members) >= 3]


def _longest_paths(ordered_nodes: list[str], edges: set[tuple[str, str]]) -> dict[str, int]:
    """The longest-path depth of each node over ``edges`` (relaxed to a fixed point).

    The number of passes has the node count as its limit, so that a pathological cycle in
    the id set cannot make it loop forever. The graphs are fans without cycles, so it
    settles in a few passes.
    """
    depth = {node: 0 for node in ordered_nodes}
    for _ in range(len(ordered_nodes)):
        changed = False
        for u, v in edges:
            if depth[v] < depth[u] + 1:
                depth[v] = depth[u] + 1
                changed = True
        if not changed:
            break
    return depth


def _detour_nests(
    drawn: Sequence[PathLayer],
    seqs: Sequence[tuple[str, ...]],
    owner: dict[str, int],
) -> dict[int, int]:
    """Map each *detour* path to the sibling route that it branches off (detection only).

    A heard route that is another route plus one or two inserted relays (the same
    convergence into our node, with one more hop on the way) is that sibling with a detour.
    It is not an independent track. Such a path owns some relays. Its other relays are
    exactly the relays of a sibling with a higher or equal priority, and that sibling really
    owns them (it is their lane).

    The layout decides what to do with the pair (:func:`_layout_lanes`). While the row
    budget lets it, the detour nests directly outside the lane of its sibling
    (:func:`_compress_lanes`). Only a budget that is too tight for that folds the detour onto
    the lane of the sibling (:func:`_fold_detour`). Returns ``{detour_index: sibling_index}``,
    with the weakest detours first in iteration order. ``owner`` does not change.
    """
    relays = [frozenset(n for n in seq if n not in (SRC_NODE, DST_NODE)) for seq in seqs]
    nests: dict[int, int] = {}
    # Weakest first, so that a marginal detour pairs with its stronger sibling, never the
    # opposite.
    for i in sorted(range(len(drawn)), key=lambda j: drawn[j].priority):
        own_i = {n for n in seqs[i] if owner[n] == i}
        residual = relays[i] - own_i
        if not own_i or not residual:
            continue
        for q in range(len(drawn)):
            if q == i or drawn[q].priority < drawn[i].priority or relays[q] != residual:
                continue
            if not all(owner[n] == q for n in relays[q]):  # q must own (be the lane of) them
                continue
            nests[i] = q
            break
    return nests


def _fold_detour(
    i: int,
    q: int,
    seqs: Sequence[tuple[str, ...]],
    owner: dict[str, int],
    col_of: Callable[[str], int],
) -> None:
    """Fold the extra relays of detour ``i`` onto the lane of sibling ``q``: the last resort.

    This is the one-lane picture that a viewport that is too short falls back to. The relays
    that the detour owns get the sibling as their new owner. They go on its lane as
    waypoints, through which the branch dips. The cost is that the straight run of the
    sibling passes over them.

    The fold occurs only if none of these relays shares a cell column with a node that is
    already on that lane. This includes a detour that was folded there before, so that two
    siblings that insert a relay at the same column do not print over each other (the
    refused detour keeps its own lane). Then the folded path owns nothing, gets no lane, and
    costs no band. This function changes ``owner`` in place.
    """
    own_i = {n for n in seqs[i] if owner[n] == i}
    q_cols = {col_of(n) for n, o in owner.items() if o == q and n not in (SRC_NODE, DST_NODE)}
    new_cols = {col_of(n) for n in own_i}
    if len(new_cols) == len(own_i) and q_cols.isdisjoint(new_cols):
        for n in own_i:
            owner[n] = q


def _band_rows(n_lanes: int, lane_pitch: int) -> int:
    """The canvas rows that are necessary for a band of ``n_lanes`` at full pitch.

    It does the same calculation as the vertical size block of ``render_path_graph`` (its
    ``slot`` span plus the end margins). Thus the lane layout can compare a candidate shape
    with ``max_rows`` before it sets that shape, instead of a compressed drawing that shows
    the problem only after the fact.
    """
    max_lane = n_lanes - 1
    if max_lane <= 0:
        return 0
    lane_rows = max_lane * lane_pitch + (1 if max_lane % 2 == 1 else 0)
    return ceil((lane_rows * 4 + 2 * _GRAPH_END_DOTS) / 4)


def _layout_lanes(
    drawn: Sequence[PathLayer],
    seqs: Sequence[tuple[str, ...]],
    ordered_nodes: list[str],
    owner: dict[str, int],
    best: int,
    col_of: Callable[[str], int],
    max_rows: int,
    lane_pitch: int,
) -> dict[str, int]:
    """Seat each relay on a signed lane, and use rows for detours before they fold.

    The function measures the full fan against the row budget before it sets the shape.
    First, the detour routes (:func:`_detour_nests`) *nest*: each keeps its own lane
    directly outside the sibling that it branches off (:func:`_compress_lanes`). Thus the
    straight run of the sibling clearly skips the inserted relay, and does not pass over its
    marker. Only when that band does not fit ``max_rows`` at full pitch does a detour *fold*
    onto the lane of its sibling (:func:`_fold_detour`). The weakest detour folds first, one
    at a time, and the function lays out and measures again until the band fits or no
    detours remain. Thus the picture with all the relays on one lane is the last resort,
    never the default.

    What is still too tall after all the folds is the true minimum, and the pitch
    compression of the renderer handles it. The function changes ``owner`` in place where it
    folds.

    Returns ``{node: signed_lane}`` for each relay (``0`` is the spine, ``<0`` is above,
    ``>0`` is below), as :func:`_compress_lanes` returns it.
    """
    nests = _detour_nests(drawn, seqs, owner)
    while True:
        lane_of_path = _assign_lanes(drawn, seqs, owner, best)
        signed = _compress_lanes(ordered_nodes, lane_of_path, owner, best, col_of, nests)
        if not nests:
            return signed
        lanes = max(signed.values(), default=0) - min(signed.values(), default=0) + 1
        if _band_rows(lanes, lane_pitch) <= max_rows:
            return signed
        # Too tall for the viewport: fold the weakest detour onto the lane of its sibling, and
        # try again. A fold that the column guard refuses still removes the detour from the
        # nest map (the route goes back to a plain lane of its own). Thus the loop always runs
        # out of detours and stops.
        victim = min(nests, key=lambda i: drawn[i].priority)
        _fold_detour(victim, nests.pop(victim), seqs, owner, col_of)


def _bypass_vias(
    seqs: Sequence[tuple[str, ...]],
    bidir: set[frozenset[str]],
    signed: dict[str, int],
    col_of: Callable[[str], int],
    max_rows: int,
    lane_pitch: int,
) -> dict[frozenset[str], list[tuple[str, int]]]:
    """Bend each edge that goes level through a skipped marker, where the column has space.

    A subset pair is the trigger. Next to an ``A → B → C → D`` route, there is the shorter
    ``A → C → D``. When ``A`` and ``C`` are on one lane, the ``A→C`` edge of the shorter
    route is a level run directly through the cell of ``B``. If it is drawn that way, it
    looks like a route through B, which is the one thing that the evidence excludes. With
    emphasis, it is worse: the emphasis of the subset colours the run of the spine again, and
    the skipped relay looks selected.

    Each marker between the ends of an edge on their shared lane is, by construction, a node
    that the edge skips. If the route went through it, the walk would have ``A→B`` and
    ``B→C``, never ``A→C``. Thus each such edge gets a *virtual waypoint*: an unmarked point
    in the column of the skipped node, on the innermost lane above or below it that is
    really free. The edge goes in an arc through that waypoint instead: out, level for an
    instant next to the skipped marker, and back. This is the same wider arc that a nested
    detour draws, so a skip looks like a skip.

    The space is measured, never assumed. A lane at that column is free when no marker is
    there and no route goes level through it across that column (a waypoint on such a lane
    has its peak on the line of that route, and it looks like it touches that line). Also, a
    waypoint can open a lane outside the current band only while the larger band still fits
    ``max_rows`` at full pitch (:func:`_band_rows`). This is the same budget that the detour
    fold uses. The innermost free lane wins. If two lanes have the same depth, the side that
    does not grow the band wins (and above wins if they are fully equal).

    An edge whose skipped column really has no space (all the lanes are taken, and the band
    cannot grow) keeps the plain level pass-over, which is the true last resort. The
    function visits the edges in walk order, so that the picture is identical at each paint.
    A set of string pairs iterates in an order from the hash seed, and then two runs can
    give a contested lane to different edges.

    Returns ``{edge pair: [(skipped node, via signed lane), …]}``, with the waypoints from
    left to right, in the signed-lane space of ``signed``. The caller adds the waypoint lanes
    to the band extent, so that the vertical size includes each lane that a bypass opened.
    The caller seats each waypoint at the exact x of the skipped node, on the row of that
    lane. Bidirectional pairs draw as the one true vertical line, and never bend.
    """
    if not signed:
        return {}
    low = min(signed.values())
    high = max(signed.values())
    centre = (low + high) / 2.0  # the endpoint lane (an integer only when it is a real lane)

    def lane_of(node: str) -> float:
        return centre if node in (SRC_NODE, DST_NODE) else float(signed[node])

    cols = {node: col_of(node) for node in (*signed, SRC_NODE, DST_NODE)}

    # Each drawn edge one time, in walk order. The level edges keep their lane and column span.
    level: list[tuple[frozenset[str], float, int, int]] = []
    seen: set[frozenset[str]] = set()
    for seq in seqs:
        for u, v in pairwise(seq):
            key = frozenset((u, v))
            if key in seen or key in bidir:
                continue
            seen.add(key)
            if lane_of(u) == lane_of(v):
                c0, c1 = sorted((cols[u], cols[v]))
                level.append((key, lane_of(u), c0, c1))

    # Where a waypoint must not go: each seated marker, and each column that a level run
    # crosses on its own lane (a waypoint next to the straight run of another route looks
    # like it touches that run).
    taken: set[tuple[int, float]] = {(cols[n], float(seat)) for n, seat in signed.items()}
    taken.add((cols[SRC_NODE], centre))
    taken.add((cols[DST_NODE], centre))
    for _key, lane, c0, c1 in level:
        for col in range(c0 + 1, c1):
            taken.add((col, lane))

    vias: dict[frozenset[str], list[tuple[str, int]]] = {}
    for key, lane, c0, c1 in level:
        skipped = sorted(
            (n for n, seat in signed.items() if float(seat) == lane and c0 < cols[n] < c1),
            key=lambda n: cols[n],
        )
        for m in skipped:
            # The innermost free lane on each side of the skipped node, then the better of
            # the two: the shallower lane first, and on a depth tie, the side that keeps the
            # height of the band.
            pick: tuple[int, int, int, int] | None = None
            for side, sign in ((0, -1), (1, 1)):
                for depth in range(1, high - low + 3):
                    cand = signed[m] + sign * depth
                    if (cols[m], float(cand)) in taken:
                        continue
                    grows = int(cand < low or cand > high)
                    if grows:
                        n_lanes = max(high, cand) - min(low, cand) + 1
                        if _band_rows(n_lanes, lane_pitch) > max_rows:
                            break  # deeper on this side only grows more, so stop on this side
                    if pick is None or (depth, grows, side) < pick[:3]:
                        pick = (depth, grows, side, cand)
                    break  # the innermost free lane on this side is found
            if pick is None:
                continue  # no space on either side: the level pass-over stays
            via_lane = pick[3]
            taken.add((cols[m], float(via_lane)))
            low, high = min(low, via_lane), max(high, via_lane)
            vias.setdefault(key, []).append((m, via_lane))
    return vias


def _assign_lanes(
    drawn: Sequence[PathLayer],
    seqs: Sequence[tuple[str, ...]],
    owner: dict[str, int],
    best: int,
) -> dict[int, int]:
    """Seat each lane-bearing path on a horizontal lane.

    The best path is in the centre, and routes that share hops are near each other. Returns
    ``{path_index: lane}`` only for the paths that own at least one node (a subsumed path
    goes through the markers of other paths, and has no lane of its own). The lanes count
    from ``0`` (the top) up.

    The best path is pinned to the centre lane, where it goes straight through the two
    endpoints as the spine of the graph. The other paths are in the order that minimizes
    the total *jog*: the vertical distance that a path travels to reach a relay that another
    path owns. Thus routes that share hops are near each other, and a shared relay costs the
    shortest detour. Small graphs (``≤`` :data:`_MAX_EXACT_LANES` lanes) get the exact best
    order by a search. Larger graphs use a barycentre heuristic instead.
    """
    bearing = [i for i in range(len(drawn)) if any(owner[node] == i for node in seqs[i])]
    n = len(bearing)
    if n == 1:
        return {bearing[0]: 0}

    # The jog partners of each bearing path: the owner lanes of the hops that it borrows from
    # other paths.
    shared_owners = {i: [owner[node] for node in seqs[i] if owner[node] != i] for i in bearing}

    def jog(lane: dict[int, int]) -> int:
        return sum(abs(lane[i] - lane[o]) for i in bearing for o in shared_owners[i])

    centre_slot = (n - 1) // 2
    others = [i for i in bearing if i != best]

    if n - 1 <= _MAX_EXACT_LANES:
        best_order: tuple[int, ...] | None = None
        best_cost: int | None = None
        for perm in permutations(others):
            slots = list(perm)
            slots.insert(centre_slot, best)
            cost = jog({p: k for k, p in enumerate(slots)})
            if best_cost is None or cost < best_cost:
                best_cost, best_order = cost, tuple(slots)
        assert best_order is not None
        return {p: k for k, p in enumerate(best_order)}

    return _barycentre_lanes(bearing, best, centre_slot, shared_owners)


def _barycentre_lanes(
    bearing: list[int],
    best: int,
    centre_slot: int,
    shared_owners: dict[int, list[int]],
) -> dict[int, int]:
    """A heuristic lane order for a graph that is too large to search: barycentre sweeps.

    Each sweep seats each path again at the average lane of the paths with which it shares
    relays, and the best path stays pinned to the centre lane. A small number of sweeps puts
    routes that share relays next to each other. This is the low-cost crossing minimizer, at
    the coarser grain of whole lanes.
    """
    lane = {p: float(k) for k, p in enumerate(bearing)}
    lane[best] = float(centre_slot)
    others = [i for i in bearing if i != best]
    for _ in range(_ORDER_SWEEPS):
        for i in others:
            if shared_owners[i]:
                lane[i] = sum(lane[o] for o in shared_owners[i]) / len(shared_owners[i])
        order = sorted(others, key=lambda i: lane[i])
        order.insert(centre_slot, best)
        lane = {p: float(k) for k, p in enumerate(order)}
    return {p: int(lane[p]) for p in order}


def _compress_lanes(
    ordered_nodes: list[str],
    lane_of_path: dict[int, int],
    owner: dict[str, int],
    best: int,
    col_of: Callable[[str], int],
    nests: dict[int, int] | None = None,
) -> dict[str, int]:
    """Compress the lanes of the paths onto the fewest rows, one node at a time in each column.

    :func:`_assign_lanes` seats each path on a lane of its own, so the band is as tall as
    the number of routes in the graph, also where the routes only go one or two side by
    side. But a lane is a full-width row. Two routes must have distinct rows only in the
    columns where they both have a node, and from endpoint to endpoint, each route already
    shares the origin and our node. Thus this pass keeps the relays of the best path on the
    spine (offset ``0``), and moves each other node to the innermost free lane above or below
    the spine, in its own column. A column with one node above the spine uses the first row
    above, however many routes fan past it. Only a column where several routes really stack
    uses the deeper rows.

    The **side** that each alternative takes does not come from its jog-order lane. That
    order seats routes that share a relay next to each other. Thus it puts routes that share
    a column onto the same flank, and the band is as tall as the deepest stack of one flank
    plus that of the other flank, also when no column holds more than two nodes.

    Instead, :func:`_balance_sides` 2-colours the alternatives to make the deeper flank
    flatter. (The spine can lean off the exact centre to reach the middle of the band, and
    that is acceptable.) Thus a fan of five routes, with a maximum of two nodes in each
    column, draws three lanes (the spine plus one flank on each side), not five. The relays
    of the best path still hold the straight spine. The returned lanes are signed offsets
    from it (``<0`` above, ``>0`` below), which the caller shifts to a band that starts at
    ``0``.

    A *nested* detour (``nests``, from :func:`_detour_nests`) stays a full lane **outside**
    the flank sibling that it branches off. Its route is pinned to the side of the sibling,
    and its own relays get a depth *floor* one more than the floor of the sibling. The
    straight run of the sibling crosses the column of the detour relay on its own lane.
    Because of the floor, that run never passes over the marker of the relay. Then the
    detour shows as the wider arc that it is: out past the sibling, through its inserted
    relay, and back in to the shared node. (A floor is not necessary for a detour off the
    spine, because the first flank row is already outside lane ``0``.)

    Returns ``{node: signed_lane}`` for each relay (the caller puts the endpoints on the
    centre of the band, not this function). A node that is alone off the spine in its
    column always goes on ``±1`` (or on ``±2`` when it belongs to a nested detour). Thus a
    sparse graph with many routes collapses to what it really is: three lanes, with the
    spine and two flanks.
    """
    nests = nests or {}
    best_lane = lane_of_path[best]
    relays = [node for node in ordered_nodes if node not in (SRC_NODE, DST_NODE)]
    spine = {node for node in relays if owner[node] == best}
    off_spine = [node for node in relays if node not in spine]
    # The bearing routes other than the best route, in the jog order that _assign_lanes
    # settled (each route keyed by its signed lane relative to the best route). Thus a tie
    # goes to the arrangement that is already clean.
    routes = sorted(
        {owner[node] for node in off_spine},
        key=lambda r: (lane_of_path[r] - best_lane, r),
    )
    cols_of_route = {r: {col_of(node) for node in off_spine if owner[node] == r} for r in routes}
    # The relays of a nested detour are one lane outside the relays of its sibling (the
    # floor), on the same flank (the tie). A detour whose sibling is the spine is an ordinary
    # flank route.
    floor = {r: 1 for r in routes}
    tie: dict[int, int] = {}
    for d, s in nests.items():
        if d in floor and s in floor:
            floor[d] = floor[s] + 1
            tie[d] = s
    orig_side = {r: (1 if lane_of_path[r] - best_lane > 0 else -1) for r in routes}
    side = _balance_sides(routes, cols_of_route, orig_side, floor, tie)

    rank = {r: i for i, r in enumerate(routes)}  # nearest to the spine first, in a flank
    columns: dict[int, list[str]] = {}
    for node in off_spine:
        columns.setdefault(col_of(node), []).append(node)

    signed: dict[str, int] = {node: 0 for node in spine}
    for members in columns.values():
        above = sorted(
            (n for n in members if side[owner[n]] < 0),
            key=lambda n: (floor[owner[n]], rank[owner[n]]),
        )
        below = sorted(
            (n for n in members if side[owner[n]] > 0),
            key=lambda n: (floor[owner[n]], rank[owner[n]]),
        )
        depth = 0
        for node in above:
            depth = max(depth + 1, floor[owner[node]])
            signed[node] = -depth
        depth = 0
        for node in below:
            depth = max(depth + 1, floor[owner[node]])
            signed[node] = depth
    return signed


def _balance_sides(
    routes: list[int],
    cols_of_route: dict[int, set[int]],
    orig_side: dict[int, int],
    floor: dict[int, int],
    tie: dict[int, int],
) -> dict[int, int]:
    """Choose a flank (``-1`` above / ``+1`` below the spine) for each alternative route.

    The band that a :func:`_compress_lanes` pack draws is ``(deepest stack above) + (deepest
    stack below) + 1``. Two alternatives must have distinct rows only where they share a
    column, so a flank is only as deep as its most crowded column. The jog order seats routes
    that share relays next to each other, and thus puts them on the same flank. Then one
    flank can stack two deep while the other flank is empty, and the band is taller than
    necessary. Thus the routes are 2-coloured, by exhaustive search while there are few of
    them (:data:`_MAX_BALANCE_ROUTES`, past which the jog-order sides stay):

    * A nested detour is pinned to the flank of its sibling (``tie``). The purpose is to
      nest outside the sibling, so a colouring that puts the pair on different flanks is not
      a candidate. The stack of a column is measured with the depth *floor* of the detour,
      so its outside row counts.
    * Never above the **ceiling** that the jog order itself draws. The balance can make a
      band flatter, but never taller. A column that really stacks three nodes makes one
      flank two deep with any colouring. A colouring that fills the other flank to the same
      depth (better to look at, but taller) is refused.
    * Below that ceiling, **make the deeper flank flatter**. Thus a fan that is two deep on
      one side, while the other side is empty, splits across the two sides, and the band
      becomes smaller.
    * Then make the two flanks equal, and then keep the jog-order side. Thus a graph that
      the jog order already draws flat stays exactly as it is. An alternative that puts its
      routes on one side gives a lower band, but it has the same deepest flank. Thus the
      balance tie-break keeps the open shape, instead of a lopsided shape that is one row
      shorter. Only a graph with a flank that is deeper than necessary changes.

    Returns ``{route_index: side}``.
    """
    if not routes:
        return {}
    # The jog-order baseline, with each nested detour moved onto the flank of its sibling.
    # This shape obeys the ties, and the ceiling and the agreement tie-break are both
    # measured against it (it is also the one assignment that is sure to pass its own
    # ceiling).
    forced = dict(orig_side)
    for d, s in tie.items():
        forced[d] = forced[s]
    if len(routes) > _MAX_BALANCE_ROUTES:
        return forced

    def flanks(assign: dict[int, int]) -> tuple[int, int]:
        deep = {-1: 0, 1: 0}
        for sign in (-1, 1):
            cols: dict[int, list[int]] = {}
            for r in routes:
                if assign[r] == sign:
                    for col in cols_of_route[r]:
                        cols.setdefault(col, []).append(r)
            for members in cols.values():
                # The same stack order from the inside out as the pack uses: floors push the
                # row of a nested detour outward, also where the column holds nothing else.
                depth = 0
                for f in sorted(floor[r] for r in members):
                    depth = max(depth + 1, f)
                deep[sign] = max(deep[sign], depth)
        return deep[-1], deep[1]

    orig_above, orig_below = flanks(forced)
    ceiling = orig_above + orig_below  # the band height of the jog order − 1, never taller

    best_assign: dict[int, int] | None = None
    best_key: tuple[int, int, int] | None = None
    for combo in product((-1, 1), repeat=len(routes)):
        assign = dict(zip(routes, combo, strict=True))
        if any(assign[d] != assign[s] for d, s in tie.items()):  # a nest split off its sibling
            continue
        deep_above, deep_below = flanks(assign)
        if deep_above + deep_below > ceiling:  # taller than the jog order draws, so reject it
            continue
        agree = sum(1 for r in routes if assign[r] == forced[r])
        key = (max(deep_above, deep_below), abs(deep_above - deep_below), -agree)
        if best_key is None or key < best_key:
            best_key, best_assign = key, assign
    assert best_assign is not None  # `forced` obeys the ties and meets its own ceiling
    return best_assign


def _place_labels(
    canvas: MapCanvas,
    ordered_nodes: list[str],
    pos: dict[str, tuple[int, int]],
    node_lane: dict[str, float],
    width: int,
    mid_y: int,
    label_of: LabelOf,
    label_rgb_of: LabelRgbOf,
) -> None:
    """Write the label of each node above or below its marker, away from the lines if possible.

    The endpoints come first, then the relays down the lanes, so that the named ends win any
    contest for a cell. The function tries each label first on the side away from the middle
    of the graph (to spread the text outward, off the busy centre). It prefers a row that the
    drawn lines do not already use. A node at a canvas edge moves its centre anchor inward
    far enough for the full name to fit. Such a node is an endpoint by construction, or a
    relay that a balanced rank pins near the origin or our node.

    A centred label that goes past the edge places nothing, and then the marker has no
    label, with no warning. When the two stacked rows are blocked, the label goes next to
    its marker instead. A name is shortened only when it is wider than the full canvas.
    """
    endpoints = [n for n in (DST_NODE, SRC_NODE) if n in pos]
    relays = sorted(
        (n for n in ordered_nodes if n not in (SRC_NODE, DST_NODE)),
        key=lambda n: (node_lane[n], pos[n][0]),
    )
    for node in [*endpoints, *relays]:
        label = label_of(node)
        if not label:
            continue
        if len(label) > width:
            label = label[: max(1, width - 1)] + "…"
        rgb = label_rgb_of(node)
        x, y = pos[node]
        # Clamp the centre cell so that the full label fits between the canvas edges. Without
        # the clamp, a marker near an edge centres its name off the canvas, places nothing,
        # and loses its label. A marker with enough space keeps its true centre (there, the
        # clamp does nothing).
        half = len(label) // 2
        cx = min(max(x >> 1, half), max(half, width - (len(label) - half)))
        anchor_x = cx * 2
        rows_out = (y - 4, y + 4) if y <= mid_y else (y + 4, y - 4)
        if any(
            canvas.place_label(anchor_x, sy, label, rgb, bold=True, avoid_dots=True)
            for sy in rows_out
        ):
            continue
        if any(canvas.place_label(anchor_x, sy, label, rgb, bold=True) for sy in rows_out):
            continue
        # The two rows are blocked: put the label next to the marker (away from the drawn lines
        # if possible, else over them), instead of no label.
        if not canvas.marker_label(x, y, label, rgb, avoid_dots=True):
            canvas.marker_label(x, y, label, rgb)
