# SPDX-License-Identifier: Apache-2.0
"""Compose a rendered map frame: basemap geometry, labels, and mesh nodes.

Given a :class:`~meshterm.core.geo.Viewport` and the decoded vector tiles covering it, this
projects every street, river, water body and place label onto a :class:`MapCanvas`, then
overlays the mesh nodes with their names on top. It is pure and synchronous — tile *fetching*
happens elsewhere (:mod:`meshterm.services.basemap`) — so both the interactive screen and
the one-shot CLI render call the same code.

Composing a frame is most of a second of pure Python, far more than a pan keystroke can
wait for, so a frame can also be drawn on the ground of the *last* one, reprojected onto
the view that has since moved (:class:`Ghost`) — which is what the interactive map paints
while the real raster is being drawn behind it.

Colours target a dark terminal (the app theme): warm roads, grey minor streets, blue water
and rivers, faint green parks. Because a cell shows one colour, draw priorities keep the
important feature visible where things overlap (rivers over water, major roads over minor).
The features whose hue *is* the information — water, its watercourses, parks, highways —
name a ``map.*`` theme style instead of a hex so the 16-slot console chooses its own shade
(see :data:`~meshterm.ui.theme.MESH_THEME_16`); everything else is grey either way.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from operator import itemgetter

from ..core.geo import Viewport
from ..core.models import MapMarker
from ..core.mvt import GEOM_LINE, GEOM_POLYGON, Layer
from ..platforms import Platform, on_platform
from .mapcanvas import MapCanvas, Raster
from .marks import NODE_MARK, REPEATER_MARK, RGB, SELF_MARK, UNKNOWN_MARK, parse_hex
from .theme import mark_rgb

# Marker palette, shared across the whole app — canonical tuples in ui.marks. These are
# local shorthand for the renderer's own hot paths; anything else imports from ui.marks.
_SELF = SELF_MARK
_REPEATER = REPEATER_MARK
_NODE = NODE_MARK
_UNKNOWN = UNKNOWN_MARK


# -- basemap styling ----------------------------------------------------------

# Every colour here is whatever :func:`~meshterm.ui.theme.mark_rgb` takes: a literal
# ``#rrggbb``, or a theme style name where the hue carries meaning and the platform must
# pick its own shade (the ``map.*`` entries — see the theme, which explains why water,
# parks and highways cannot survive a naive downsample to 16 slots).

# Road class -> (colour, priority). Higher priority wins a shared cell.
_ROAD_STYLE: dict[str, tuple[str, int]] = {
    "motorway": ("map.highway", 27),
    "trunk": ("map.highway", 26),
    "primary": ("#e8b24a", 25),
    "secondary": ("#d7c257", 24),
    "tertiary": ("#a7adb8", 23),
    "minor": ("#828894", 21),
    "unclassified": ("#828894", 21),
    "residential": ("#828894", 21),
    "living_street": ("#767c88", 20),
    "service": ("#5f646e", 19),
    "pedestrian": ("#5a5f69", 15),
    "path": ("#4b5560", 12),
    "track": ("#4b5560", 12),
    "footway": ("#454e58", 11),
    "cycleway": ("#455864", 12),
}
_ROAD_DEFAULT = ("#5f646e", 18)
_RAIL = ("#8a8f98", 22)

# Waterway class -> (colour, priority).
_WATERWAY_STYLE: dict[str, tuple[str, int]] = {
    "river": ("map.river", 30),
    "canal": ("map.river", 30),
    "stream": ("map.stream", 29),
    "ditch": ("map.ditch", 28),
    "drain": ("map.ditch", 28),
}
_WATER_FILL = ("map.water", 6)
_GREEN_FILL = ("map.park", 4)
_GREEN_CLASSES = {"wood", "forest", "grass", "park", "meadow", "scrub", "wetland", "cemetery"}

# Label styling per place class: (colour, bold, rank, min_display_zoom). Lower rank places
# claim screen space first; min zoom hides the fine detail until you zoom in.
_PLACE_STYLE: dict[str, tuple[str, bool, int, int]] = {
    "city": ("#f8fafc", True, 0, 0),
    "town": ("#e6ebf2", True, 1, 9),
    "village": ("#cdd6e2", False, 2, 12),
    "suburb": ("#9fb0c4", False, 3, 12),
    "quarter": ("#94a5ba", False, 4, 13),
    "neighbourhood": ("#8496ab", False, 5, 13),
    "hamlet": ("#8496ab", False, 6, 14),
}
_WATER_LABEL = ("#7dd3fc", False, 2)
_STREET_LABEL = ("#9aa0aa", False, 7)

#: The zoom street names appear at. Below it a canvas 53 cells wide is nowhere near
#: holding one name per street, and the few that fit label a grid too coarse to tell which
#: line they belong to — the place names are what orients you at that scale.
#:
#: Read **twice**, and it has to be: :func:`_compose` will not place a label under its own
#: ``min_zoom``, and :func:`_draw_tile` will not build one it knows cannot be placed. The
#: second is the load-bearing one. Queuing a street label is not cheap — the line is
#: clipped to the canvas segment by segment, each surviving piece measured, and the pieces
#: sorted (see :func:`_add_line_label`) — and a central tile carries 270 of them, which
#: measured 16.8 ms of a 46 ms frame on the desktop and so upwards of a third of a second
#: on the PicoCalc, at the two zooms the map most often sits at, for text that was
#: discarded at placement every time.
_STREET_LABEL_MIN_ZOOM = 15

#: Polygon layers drawn as fills, in painting order (later wins the shared cell).
_FILL_LAYERS = ("water", "landcover", "landuse", "park")

#: Buildings: colour, cell priority, stipple density, and the zoom they appear at.
#: They sit above the ground fills and below every road, and they are *stippled* rather
#: than solid because a braille dot is one bit: a solid fill does not shade a region, it
#: fills every cell it touches and takes the street grid down with it. Below the gate a
#: typical footprint is about two dots across — under half a cell — so the whole layer
#: collapses to a haze that costs the streets their legibility and says nothing the
#: street pattern didn't already say.
_BUILDING_FILL = ("#4a4f5a", 8)
_BUILDING_STIPPLE = 2
_BUILDING_MIN_ZOOM = 15

#: Every basemap layer :func:`_draw_tile` looks at — and so the only geometry the map
#: has any use for. A planet tile also ships ``housenumber``, ``poi``, ``mountain_peak``
#: and the aero layers, which together are a large share of its features and none of its
#: pixels: decoding them cost ~40% of every tile until this set was handed to
#: :func:`~meshterm.core.mvt.decode_tile` (measured on the Lyra — a 153 KB tile went
#: 1236 ms → 281 ms). Passed to the tile source at construction
#: (:attr:`meshterm.context.AppContext.basemap_source`); the on-disk cache still holds
#: whole tiles, so widening this set costs a re-decode, never a re-download.
DRAWN_LAYERS: frozenset[str] = frozenset(
    _FILL_LAYERS
    + (
        "building",
        "waterway",
        "transportation",
        "boundary",
        "transportation_name",
        "place",
        "water_name",
    )
)

#: Marks "this feature class has not been styled yet" in :func:`_draw_tile`'s per-tile style
#: memos, where ``None`` is a real answer meaning "a class we deliberately don't draw".
_UNRESOLVED = object()

#: Lowest drawing priority a **coarse** pass keeps among the road/rail lines — see
#: :func:`render_ground`. Tertiary and up: the through-roads whose pattern says *where you
#: are*, against the residential mesh that says only *town*, and above rail
#: (:data:`_RAIL`) and the unrecognised-class default. Stated as the priority the styles
#: already carry rather than as a second list of class names, so a reclassified road
#: cannot end up in one pass and not the other. At the PicoCalc's zoom 13 it keeps 2 522
#: lines of 5 416 — under half of them for nearly all of the legibility.
_COARSE_ROAD_PRIORITY = 23


@dataclass(slots=True)
class _Label:
    """A pending label placement candidate.

    Attributes:
        alts: Further anchors to try, in order, when the preferred one is already taken.
            A line feature offers these along the stretch of itself that is on screen, so
            a street whose middle is under a node marker still gets named further along.
    """

    rank: int
    x: float
    y: float
    text: str
    color: RGB
    bold: bool
    min_zoom: int = 0
    alts: tuple[tuple[float, float], ...] = ()


@dataclass(slots=True)
class _Frame:
    """Working state while composing one frame.

    Attributes:
        coarse: Whether this is the quick first pass — ground and through-roads only,
            no buildings, no back streets, no text (see :func:`render_ground`).
    """

    canvas: MapCanvas
    viewport: Viewport
    labels: list[_Label] = field(default_factory=list)
    coarse: bool = False


# -- the stale ground ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ghost:
    """A finished frame's ground, and the view it was drawn for.

    Rasterizing a view is most of a second of pure Python, so a pan moves the view long
    before its picture can exist. Rather than answer the keystroke with an empty canvas —
    the map blanking to black on every step and flashing back when the frame lands — the
    paint reprojects *this*: the streets as they were, shifted to where they now belong,
    under fresh markers. It is the ground alone (see :class:`~meshterm.ui.mapcanvas.
    Raster`); labels and node markers are redrawn at their real places on top.

    Attributes:
        raster: The braille layer of the frame it came from.
        viewport: The view that frame was drawn for, which is what says where its dots
            have since moved to.
    """

    raster: Raster
    viewport: Viewport


#: How far the reused ground dims while its replacement is being drawn — enough to read as
#: provisional (and to keep the not-yet-drawn edge the view is panning onto from looking
#: like real empty ground) without losing the shape of the streets.
#:
#: Bound at platform-switch time, and *off* where there is no truecolour: on the 16-slot
#: console a colour does not dim, it lands in a different slot, so a fade there is a
#: recolouring — the faint road classes drop to black and the rest muddle together. The
#: title's ``drawing…`` carries the same news on both platforms.
_GHOST_FADE = 0.6

#: Zoom steps of difference beyond which the ghost is dropped rather than scaled. What it
#: shows is true at any distance — the dots are reprojected, not the glyphs (see
#: :func:`~meshterm.ui.mapcanvas._magnified`) — but a dot enlarged past two steps is eight
#: cells across, and a mosaic that coarse orients nobody: there is nothing left of the
#: street pattern to recognise, and the markers alone are the better picture.
_GHOST_MAX_STEPS = 2


@on_platform
def _bind(platform: Platform) -> None:
    """Bind the ghost's fade to what the platform's palette can actually express."""
    global _GHOST_FADE
    _GHOST_FADE = 0.6 if platform.truecolor else 1.0


def _ghost_axis(
    cells: int,
    dots: int,
    origin: float,
    src_origin: float,
    scale: float,
    src_cells: int,
    magnify: int,
) -> list[tuple[int, int]]:
    """Source cell and sub-cell per canvas cell along one axis (``(-1, 0)`` for none).

    Both viewports are windows in Web Mercator, so one maps onto the other by an affine
    scale-and-shift on each axis independently — a dot at world position ``d + origin``
    sits at ``(d + origin) * scale - src_origin`` in the older view — and the whole
    reprojection collapses to two little index tables the paste then reads off.

    Where the view has zoomed *in*, one source cell spans ``magnify`` cells here, so the
    whole number of that division is only half the answer: the remainder says which of
    those cells this is, and hence which share of the source's dots it should show
    enlarged (see :meth:`~meshterm.ui.mapcanvas.MapCanvas.paste_raster`). At ``magnify``
    of 1 the remainder is always zero and this is the plain cell-index table it was.

    Args:
        cells: Canvas size along this axis, in cells.
        dots: Dots per cell on this axis (2 across, 4 down).
        origin: The canvas viewport's world-pixel origin on this axis.
        src_origin: The ghost viewport's world-pixel origin, at *its* zoom.
        scale: World pixels of the ghost's zoom per world pixel of ours.
        src_cells: The ghost raster's size along this axis, for the bounds test.
        magnify: How many cells here one source cell covers.
    """
    out: list[tuple[int, int]] = []
    for c in range(cells):
        centre = c * dots + dots / 2  # sample each cell at its middle dot
        exact = ((centre + origin) * scale - src_origin) / dots
        src = math.floor(exact)
        if not 0 <= src < src_cells:
            out.append((-1, 0))
            continue
        # Which magnified sub-cell the middle of this cell falls in. Floating point can
        # land the fraction a hair short of 1.0, so the last sub-cell is clamped rather
        # than trusted to divide.
        out.append((src, min(int((exact - src) * magnify), magnify - 1)))
    return out


def _paste_ghost(canvas: MapCanvas, viewport: Viewport, ghost: Ghost) -> None:
    """Lay ``ghost``'s ground onto ``canvas``, reprojected to ``viewport``.

    A no-op when the two views have drifted too far apart in zoom, or when the pan has
    carried the view clear off the old frame's ground — in both cases there is nothing
    left to stand in, and the markers alone are the honest picture.
    """
    src = ghost.viewport
    steps = viewport.zoom - src.zoom
    if abs(steps) > _GHOST_MAX_STEPS:
        return
    scale = 2.0**-steps
    magnify = 1 << steps if steps > 0 else 1  # a view zoomed *out* keeps whole glyphs
    ox, oy = viewport.origin_world
    sx, sy = src.origin_world
    cols = _ghost_axis(canvas.cell_w, 2, ox, sx, scale, ghost.raster.cell_w, magnify)
    if not any(c >= 0 for c, _ in cols):
        return
    rows = _ghost_axis(canvas.cell_h, 4, oy, sy, scale, ghost.raster.cell_h, magnify)
    if not any(r >= 0 for r, _ in rows):
        return
    canvas.paste_raster(ghost.raster, cols, rows, magnify=magnify, fade=_GHOST_FADE)


# -- composing a frame --------------------------------------------------------


def render_map(
    viewport: Viewport,
    tiles: dict[tuple[int, int, int], list[Layer] | None],
    markers: list[MapMarker],
    *,
    max_labels: int = 80,
    find: str = "",
    ghost: Ghost | None = None,
) -> list[str]:
    """Render a full map frame to truecolour ANSI lines.

    Args:
        viewport: The view to render (its dot size sets the canvas size).
        tiles: Decoded layers keyed by ``(z, x, y)``; ``None`` values are pending/absent.
        markers: Mesh nodes to overlay.
        max_labels: Cap on basemap labels placed, to keep the map readable.
        find: A live node-name filter: markers whose label contains it
            (case-insensitively) draw with bright white labels while the rest dim to
            unlabelled context. Empty draws every node normally.
        ghost: Ground from an earlier frame to lay down first, reprojected onto this view
            (see :class:`Ghost`). It is a stand-in for ground that isn't drawn yet, so it
            goes with the frames that have no ``tiles`` of their own; a frame drawing the
            real thing has no use for one.

    Returns:
        One ANSI string per row, ready for the TUI frame or the console.
    """
    return _compose(
        viewport, tiles, markers, max_labels=max_labels, find=find, ghost=ghost
    ).to_ansi_lines()


def render_ground(
    viewport: Viewport,
    tiles: dict[tuple[int, int, int], list[Layer] | None],
    markers: list[MapMarker],
    *,
    max_labels: int = 80,
    find: str = "",
    coarse: bool = False,
) -> tuple[list[str], Ghost]:
    """Render a frame and keep its ground, for the next moved view to stand on.

    The same work as :func:`render_map`, plus the :class:`Ghost` the frame leaves behind —
    which is why the interactive map calls this one for the real (background) raster and
    :func:`render_map` for the immediate paints in between.

    ``coarse`` draws the **first pass**: the ground fills, the watercourses, the admin
    boundaries and the through-roads (:data:`_COARSE_ROAD_PRIORITY`) — no buildings, no back
    streets, no text at all. It is the same picture at a lower resolution of detail, and
    it is a quarter of the work: on the PicoCalc, 249 ms against 962 at zoom 13, 120 ms
    against 529 at zoom 16. Drawn before the full frame it gives a view that has run past
    its last ground *something* true within a blink, and gives a pan being held a unit of
    work small enough to abandon — which is what the map asks for when
    :data:`~meshterm.ui.map_screen._COARSE_PREVIEW` is on (see
    :meth:`meshterm.ui.map_screen.MapScreen._draw_ground`).

    Args:
        viewport: The view to render.
        tiles: Decoded layers keyed by ``(z, x, y)``.
        markers: Mesh nodes to overlay.
        max_labels: Cap on basemap labels placed.
        find: A live node-name filter (see :func:`render_map`).
        coarse: Draw the quick first pass rather than the finished frame.

    Returns:
        The ANSI lines, and the ground they leave behind.
    """
    canvas = _compose(viewport, tiles, markers, max_labels=max_labels, find=find, coarse=coarse)
    return canvas.to_ansi_lines(), Ghost(canvas.raster(), viewport)


def _compose(
    viewport: Viewport,
    tiles: dict[tuple[int, int, int], list[Layer] | None],
    markers: list[MapMarker],
    *,
    max_labels: int = 80,
    find: str = "",
    ghost: Ghost | None = None,
    coarse: bool = False,
) -> MapCanvas:
    """Draw one frame onto a fresh canvas — see :func:`render_map` for the arguments."""
    canvas = MapCanvas(viewport.dot_w // 2, viewport.dot_h // 4)
    frame = _Frame(canvas=canvas, viewport=viewport, coarse=coarse)

    if ghost is not None:
        _paste_ghost(canvas, viewport, ghost)

    for (z, x, y), layers in tiles.items():
        if layers:
            _draw_tile(frame, layers, z, x, y)

    # Nodes reserve their cells first so basemap labels route around them.
    _draw_nodes(canvas, viewport, markers, find=find)

    # Then place basemap labels by importance, honouring the zoom gate and collisions.
    # OpenStreetMap splits a long street into several named features, so one name can
    # arrive many times over; on a terminal's worth of columns the second copy is only
    # ever taking space from a street that has none, so a name is drawn once per frame.
    placed = 0
    named: set[str] = set()
    for label in sorted(frame.labels, key=lambda lab: lab.rank):
        if placed >= max_labels:
            break
        if viewport.zoom < label.min_zoom or label.text in named:
            continue
        for ax, ay in ((label.x, label.y), *label.alts):
            if canvas.place_label(ax, ay, label.text, label.color, bold=label.bold):
                placed += 1
                named.add(label.text)
                break

    return canvas


#: A point's y, for ``min``/``max`` to read in C (see ``on_view`` in :func:`_draw_tile`).
_Y = itemgetter(1)


def _draw_tile(frame: _Frame, layers: list[Layer], z: int, x: int, y: int) -> None:
    """Draw one decoded tile's geometry and collect its label candidates."""
    vp = frame.canvas
    by_name = {layer.name: layer for layer in layers}

    # The tile-local -> dot projection is affine, and every layer in a tile shares the
    # same extent in practice, so its three coefficients are resolved once per extent and
    # the per-vertex work is spelled out inline below (see Viewport.tile_transform): a
    # downtown frame projects ~50k vertices, and at that count even the function call per
    # vertex is worth removing.
    transforms: dict[int, tuple[float, float, float]] = {}

    def transform(extent: int) -> tuple[float, float, float]:
        t = transforms.get(extent)
        if t is None:
            t = transforms[extent] = frame.viewport.tile_transform(x, y, z, extent)
        return t

    def project(ring: list[tuple[int, int]], extent: int) -> list[tuple[float, float]]:
        bx, by, step = transform(extent)
        return [(bx + lx * step, by + ly * step) for lx, ly in ring]

    # The canvas as a window in this tile's own units, a dot wider all round, so a ring can
    # be tested before a single point of it is projected. A zoomed-out view sees a sliver
    # of tiles whose coastlines and land cover run to tens of thousands of points, and
    # projecting all of them was half of a z7 frame. Skipping a ring wholly outside is
    # exact, never an approximation: a line there lights nothing, and a closed ring's
    # crossings on every scanline come in pairs off the canvas, so the even-odd fill of
    # whatever it shares a polygon with is unchanged (see MapCanvas.fill_polygon).
    windows: dict[int, tuple[float, float, float, float]] = {}

    def on_view(ring: list[tuple[int, int]], extent: int) -> bool:
        w = windows.get(extent)
        if w is None:
            bx, by, step = transform(extent)
            w = windows[extent] = (
                (-1 - bx) / step,
                (vp.dot_w + 1 - bx) / step,
                (-1 - by) / step,
                (vp.dot_h + 1 - by) / step,
            )
        lo_x, hi_x, lo_y, hi_y = w
        # min/max over the tuples run in C: the first element decides, so they are x.
        return not (
            max(ring)[0] < lo_x
            or min(ring)[0] > hi_x
            or max(ring, key=_Y)[1] < lo_y
            or min(ring, key=_Y)[1] > hi_y
        )

    # A tile's palette is a handful of colours shared by tens of thousands of features, so
    # every one of them is resolved once here rather than per feature. mark_rgb memoizes,
    # but at ~30 000 calls a frame even a dict hit behind a function call is real time on
    # the device — and the styles below are constants, so the answer never varies within a
    # tile. Per-class styles (roads, waterways, places) memoize into the dicts alongside.
    water_rgb, water_prio = mark_rgb(_WATER_FILL[0]), _WATER_FILL[1]
    green_rgb, green_prio = mark_rgb(_GREEN_FILL[0]), _GREEN_FILL[1]
    boundary_rgb = mark_rgb("#6d5f88")
    road_styles: dict[str | None, tuple[tuple[int, int, int], int]] = {}
    water_styles: dict[str | None, tuple[tuple[int, int, int], int] | None] = {}

    # Fills first (water, green space) so lines and labels sit on top.
    for name in _FILL_LAYERS:
        layer = by_name.get(name)
        if layer is None:
            continue
        is_water = name == "water"
        for feat in layer.features:
            if feat.geom_type != GEOM_POLYGON:
                continue
            if is_water:
                rgb, prio = water_rgb, water_prio
            elif str(feat.tags.get("class") or feat.tags.get("subclass")) in _GREEN_CLASSES:
                rgb, prio = green_rgb, green_prio
            else:
                continue
            ext = layer.extent
            vp.fill_polygon([project(r, ext) for r in feat.rings if on_view(r, ext)], rgb, prio)

    # Buildings, as a stippled texture under the streets.
    building = by_name.get("building")
    if building is not None and frame.viewport.zoom >= _BUILDING_MIN_ZOOM and not frame.coarse:
        rgb = mark_rgb(_BUILDING_FILL[0])
        # A downtown tile holds thousands of footprints and a zoomed-in view shows a few
        # dozen of them, so each is rejected on its own bounds before it is projected.
        ext = building.extent
        for feat in building.features:
            if feat.geom_type != GEOM_POLYGON:
                continue
            keep = [ring for ring in feat.rings if len(ring) >= 4 and on_view(ring, ext)]
            if keep:
                vp.fill_polygon(
                    [project(r, ext) for r in keep],
                    rgb,
                    _BUILDING_FILL[1],
                    stipple=_BUILDING_STIPPLE,
                )

    # Waterways (rivers/streams) as lines.
    waterway = by_name.get("waterway")
    if waterway is not None:
        for feat in waterway.features:
            cls = feat.tags.get("class")
            style = water_styles.get(cls, _UNRESOLVED)
            if style is _UNRESOLVED:
                named = _WATERWAY_STYLE.get(str(cls))
                style = None if named is None else (mark_rgb(named[0]), named[1])
                water_styles[cls] = style
            if style is None or feat.geom_type != GEOM_LINE:
                continue
            rgb, prio = style
            for ring in feat.rings:
                if len(ring) >= 2 and on_view(ring, waterway.extent):
                    vp.draw_line(project(ring, waterway.extent), rgb, prio)
            if feat.name and not frame.coarse:
                _add_line_label(
                    frame, feat.rings, waterway.extent, z, x, y, feat.name, _WATER_LABEL
                )

    # Roads / rail.
    transportation = by_name.get("transportation")
    if transportation is not None:
        for feat in transportation.features:
            if feat.geom_type != GEOM_LINE:
                continue
            cls = feat.tags.get("class")
            style = road_styles.get(cls)
            if style is None:
                name = str(cls or "")
                named = (
                    _RAIL if name in ("rail", "transit") else _ROAD_STYLE.get(name, _ROAD_DEFAULT)
                )
                style = road_styles[cls] = (mark_rgb(named[0]), named[1])
            rgb, prio = style
            if frame.coarse and prio < _COARSE_ROAD_PRIORITY:
                continue  # the back streets are the bulk of the lines and the last to matter
            for ring in feat.rings:
                if len(ring) >= 2 and on_view(ring, transportation.extent):
                    vp.draw_line(project(ring, transportation.extent), rgb, prio)

    # Boundaries (admin) as a faint hint.
    boundary = by_name.get("boundary")
    if boundary is not None:
        for feat in boundary.features:
            if feat.geom_type != GEOM_LINE:
                continue
            try:
                if int(feat.tags.get("admin_level", 99)) > 6:
                    continue
            except (TypeError, ValueError):
                continue
            for ring in feat.rings:
                if len(ring) >= 2 and on_view(ring, boundary.extent):
                    vp.draw_line(project(ring, boundary.extent), boundary_rgb, 14)

    if frame.coarse:
        return  # everything past here is text, and text is what the second pass is for

    # Street-name labels, once the streets are far enough apart to carry a name each.
    # Gated here as well as at placement (:data:`_STREET_LABEL_MIN_ZOOM`): the label is
    # the expensive half of a named road, and below the gate every one of them was built
    # and then dropped. The *display* zoom decides, not the tile's — an overzoomed view
    # reads its ground off a lower tile but is still zoomed in, and still wants names.
    tname = by_name.get("transportation_name")
    if tname is not None and frame.viewport.zoom >= _STREET_LABEL_MIN_ZOOM:
        color, bold, rank = _STREET_LABEL
        for feat in tname.features:
            if feat.name and feat.rings:
                _add_line_label(
                    frame,
                    feat.rings,
                    tname.extent,
                    z,
                    x,
                    y,
                    feat.name,
                    (color, bold, rank),
                    min_zoom=_STREET_LABEL_MIN_ZOOM,
                )

    # Place labels.
    place = by_name.get("place")
    if place is not None:
        for feat in place.features:
            if not feat.name or not feat.rings or not feat.rings[0]:
                continue
            style = _PLACE_STYLE.get(str(feat.tags.get("class")))
            if style is None:
                continue
            color, bold, rank, min_zoom = style
            lx, ly = feat.rings[0][0]
            dx, dy = frame.viewport.feature_to_dot(x, y, z, place.extent, lx, ly)
            frame.labels.append(_Label(rank, dx, dy, feat.name, mark_rgb(color), bold, min_zoom))

    # Water-body names.
    water_name = by_name.get("water_name")
    if water_name is not None:
        color, bold, rank = _WATER_LABEL
        label_rgb = mark_rgb(color)
        for feat in water_name.features:
            if feat.name and feat.rings and feat.rings[0]:
                lx, ly = feat.rings[0][0]
                dx, dy = frame.viewport.feature_to_dot(x, y, z, water_name.extent, lx, ly)
                frame.labels.append(_Label(rank, dx, dy, feat.name, label_rgb, bold, 8))


def _clip_to_canvas(
    x0: float, y0: float, x1: float, y1: float, w: float, h: float
) -> tuple[float, float, float, float] | None:
    """Trim a segment to the ``0..w`` by ``0..h`` canvas (Liang-Barsky).

    Returns:
        The part of the segment inside the canvas, or ``None`` if none of it is. A street
        that crosses the view with both of its endpoints beyond the edges still yields the
        stretch you can see, which is the whole point of clipping rather than testing the
        endpoints.
    """
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0), (dx, w - x0), (-dy, y0), (dy, h - y0)):
        if p == 0:
            if q < 0:
                return None  # parallel to this edge and wholly outside it
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    return (x0 + t0 * dx, y0 + t0 * dy, x0 + t1 * dx, y0 + t1 * dy)


def _add_line_label(
    frame: _Frame,
    rings: list[list[tuple[int, int]]],
    extent: int,
    z: int,
    x: int,
    y: int,
    text: str,
    style: tuple[str, bool, int],
    *,
    min_zoom: int = 0,
) -> None:
    """Queue a label on the longest stretch of a line feature that is actually on screen.

    Anchoring to the feature's own midpoint — the middle of the street as the *tile* drew
    it — pins the label to a fixed geographic point rather than to your view, and the
    tighter you zoom the less likely that point is to still be on screen. It made street
    names get rarer the further in you went: of 265 named streets in a central Montréal
    tile, 37 anchors landed on a 53x26 canvas at zoom 14 and only 6 at zoom 16.

    So the line is clipped to the canvas first and the label goes on the longest piece
    that survives, with the next-longest pieces kept as alternates for when that spot is
    already spoken for. Nothing is queued for a feature that is wholly off screen.
    """
    vp = frame.viewport
    w, h = float(vp.dot_w), float(vp.dot_h)
    pieces: list[tuple[float, float, float]] = []  # (length, mid x, mid y)
    for ring in rings:
        if len(ring) < 2:
            continue
        prev = vp.feature_to_dot(x, y, z, extent, *ring[0])
        for point in ring[1:]:
            cur = vp.feature_to_dot(x, y, z, extent, *point)
            visible = _clip_to_canvas(prev[0], prev[1], cur[0], cur[1], w, h)
            prev = cur
            if visible is None:
                continue
            ax, ay, bx, by = visible
            pieces.append((math.hypot(bx - ax, by - ay), (ax + bx) / 2, (ay + by) / 2))
    if not pieces:
        return
    pieces.sort(key=lambda p: -p[0])
    color, bold, rank = style
    frame.labels.append(
        _Label(
            rank,
            pieces[0][1],
            pieces[0][2],
            text,
            mark_rgb(color),
            bold,
            min_zoom,
            alts=tuple((px, py) for _, px, py in pieces[1:4]),
        )
    )


def _marker_style(marker: MapMarker) -> tuple[str, str]:
    """The glyph and colour for a marker: self, repeater, or leaf node."""
    if marker.is_self:
        return _SELF
    if marker.is_repeater:
        return _REPEATER
    return _NODE


#: How many piled nodes it takes for a marker's glyph to reach full brightness.
_PILE_FULL = 8


def _pile_color(color: RGB, count: int, *, cap: int = _PILE_FULL) -> RGB:
    """Brighten a marker's colour by how many nodes share its cell.

    One cell can only show a single glyph, so where many nodes fall on the same spot the
    map would otherwise hide the crowd behind one marker. Instead we keep the top node's
    glyph and wash its colour toward white as the pile grows, so a bright marker reads as
    a busy cluster. The ramp is logarithmic (2 nodes → a clear lift, saturating around
    ``cap``) so it stays informative without a lone extra node looking crowded.
    """
    if count <= 1:
        return color
    t = min(1.0, math.log2(count) / math.log2(cap))
    r, g, b = color
    return (
        round(r + (255 - r) * t),
        round(g + (255 - g) * t),
        round(b + (255 - b) * t),
    )


#: How far a node outside an active find filter dims (glyph colour multiplier).
_FIND_DIM = 0.35

#: Label colour for find-filter matches: full white, the brightest thing on the map.
_FIND_MATCH_LABEL: RGB = (255, 255, 255)


def _dimmed(color: RGB, factor: float) -> RGB:
    """``color`` scaled toward black by ``factor``, clamped to byte range."""
    return tuple(max(0, min(255, round(c * factor))) for c in color)  # type: ignore[return-value]


def _draw_nodes(
    canvas: MapCanvas, viewport: Viewport, markers: list[MapMarker], *, find: str = ""
) -> None:
    """Overlay mesh nodes: every glyph, then labels by importance until they collide.

    Glyphs are drawn lowest-priority first so self/repeaters land on top of leaf nodes.
    Where several nodes share a cell the surviving glyph is brightened by the pile size
    (:func:`_pile_color`) so crowded spots glow rather than silently hiding the crowd.
    Labels are then placed highest-priority first — self, then repeaters, then leaf
    nodes — each only if it fits without overlapping. So on a crowded map the important
    labels win the available space and the rest show as a bare marker (no overlap).

    With a ``find`` filter active only the matching nodes keep labels — drawn in bright
    white so they pop — while everything else dims to near-background context and match
    labels never lose the collision contest to non-matches.
    """
    needle = find.casefold()
    placed: list[tuple[MapMarker, int, int, bool]] = []
    for marker in markers:
        x, y = viewport.lonlat_to_dot(marker.lat, marker.lon)
        ix, iy = int(round(x)), int(round(y))
        if not (0 <= ix < viewport.dot_w and 0 <= iy < viewport.dot_h):
            continue
        matched = not needle or needle in marker.label.casefold()
        placed.append((marker, ix, iy, matched))

    # A cell is (dot_x >> 1, dot_y >> 2); count how many nodes land on each.
    pile = Counter((ix >> 1, iy >> 2) for _, ix, iy, _m in placed)

    # Matches draw after (over) dimmed non-matches whatever their rank, so the node
    # being searched for is never buried under a brighter neighbour.
    for marker, ix, iy, matched in sorted(placed, key=lambda p: (p[3], p[0]._rank())):
        glyph, color = _marker_style(marker)
        rgb = parse_hex(color)
        if not matched:
            rgb = _dimmed(rgb, _FIND_DIM)
        else:
            rgb = _pile_color(rgb, pile[(ix >> 1, iy >> 2)])
        canvas.marker(ix, iy, glyph, rgb)

    # A filtered-out node is context: bare dim glyph, no label. Labels take the
    # node's key-derived name hue — the app-wide colour rule — with our own label the
    # pure ``you`` white (the ★ glyph keeps its yellow); an active find filter still
    # forces every match's label full white so the sought node pops.
    from .widgets import name_rgb  # widgets imports this module; late-bind to dodge the cycle

    labelled = (p for p in placed if p[3])
    for marker, ix, iy, _matched in sorted(labelled, key=lambda p: -p[0]._rank()):
        if needle:
            label_color = _FIND_MATCH_LABEL
        elif marker.is_self:
            label_color = (255, 255, 255)  # the `you` white
        else:
            label_color = name_rgb(marker.label, marker.key)
        canvas.marker_label(ix, iy, marker.label, label_color)
