# SPDX-License-Identifier: Apache-2.0
"""Compose a rendered map raster: basemap geometry, labels, and mesh nodes.

This module gets a :class:`~meshterm.core.geo.Viewport` and the decoded vector tiles that
cover it. It projects each street, river, water body, and place label onto a
:class:`MapCanvas`. Then it puts the mesh nodes, with their names, on top. The module is
pure and synchronous: it does not get the tiles, because :mod:`meshterm.services.basemap`
does that. Thus the interactive screen and the one-shot CLI render call the same code.

To compose a raster takes most of a second of pure Python. That is much longer than a pan
key press can wait. Thus a raster can also be drawn on the ground of the last raster,
reprojected onto the viewport that moved after it (:class:`Ghost`). The interactive map
shows this while the real raster is drawn behind it.

The colours are for a dark terminal (the app theme): warm roads, grey minor streets, blue
water and rivers, and faint green parks. A cell shows one colour. Thus, where features
overlap, the draw priorities keep the important feature visible (rivers over water, major
roads over minor roads). Some features have a colour that is the information: water, its
watercourses, parks, and highways. These features name a ``map.*`` theme style instead of
a hex value, so that the 16-slot console can choose its own shade (refer to
:data:`~meshterm.ui.theme.MESH_THEME_16`). All the other features are grey on each
platform.
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

# The marker palette, shared across all the app. The canonical tuples are in ui.marks.
# These names are local short names for the hot paths of the renderer. All other code
# imports from ui.marks.
_SELF = SELF_MARK
_REPEATER = REPEATER_MARK
_NODE = NODE_MARK
_UNKNOWN = UNKNOWN_MARK


# -- basemap styling ----------------------------------------------------------

# Each colour here is a value that :func:`~meshterm.ui.theme.mark_rgb` accepts: a literal
# ``#rrggbb``, or a theme style name where the colour has a meaning and the platform must
# choose its own shade (the ``map.*`` entries). Refer to the theme, which explains why
# water, parks, and highways cannot survive a naive downsample to 16 slots.

# Road class -> (colour, priority). The higher priority wins a shared cell.
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

# The label style for each place class: (colour, bold, rank, min_display_zoom). Places
# with a lower rank get screen space first. The minimum zoom hides the fine detail until
# you zoom in.
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

#: The zoom at which street names appear. Below it, a canvas 53 cells wide cannot hold one
#: name for each street. The few names that fit label a grid that is too coarse to show
#: which line each name belongs to. At that scale, the place names are what tell the user
#: where they are.
#:
#: Two places read this value, and that is necessary: :func:`_compose` does not place a
#: label below its own ``min_zoom``, and :func:`_draw_tile` does not build a label that it
#: knows cannot be placed. The second place is the important one. A street label in the
#: queue is not cheap: the line is clipped to the canvas segment by segment, each piece
#: that stays is measured, and the pieces are sorted (refer to :func:`_add_line_label`). A
#: central tile has 270 of them. They took 16.8 ms of a 46 ms raster on the desktop, and
#: thus more than a third of a second on the PicoCalc, at the two zooms that the map uses
#: most. And the placement removed this text each time.
_STREET_LABEL_MIN_ZOOM = 15

#: The polygon layers drawn as fills, in draw order (a later layer wins the shared cell).
_FILL_LAYERS = ("water", "landcover", "landuse", "park")

#: Buildings: the colour, the cell priority, the stipple density, and the zoom at which
#: they appear. They are above the ground fills and below all the roads. They are
#: stippled, not solid, because a braille dot is one bit. A solid fill does not shade an
#: area: it fills each cell that it touches, and the street grid disappears below it.
#: Below the zoom gate, a typical footprint is approximately two dots wide (less than half
#: a cell). Thus the full layer becomes a haze that makes the streets hard to read, and it
#: shows nothing that the street pattern does not already show.
_BUILDING_FILL = ("#4a4f5a", 8)
_BUILDING_STIPPLE = 2
_BUILDING_MIN_ZOOM = 15

#: All the basemap layers that :func:`_draw_tile` reads. Thus this is the only geometry
#: that the map uses. A planet tile also has ``housenumber``, ``poi``, ``mountain_peak``,
#: and the aero layers. Together, they are a large part of its features, but none of its
#: pixels. Their decode cost approximately 40% of each tile, until MeshTerm gave this set
#: to :func:`~meshterm.core.mvt.decode_tile` (measured on the Lyra: a 153 KB tile went
#: 1236 ms → 281 ms). The tile source gets this set when it is built
#: (:attr:`meshterm.context.AppContext.basemap_source`). The cache on disk still holds full
#: tiles. Thus, if this set becomes larger, the cost is a new decode, never a new download.
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

#: The mark for "this feature class has no style yet" in the style caches of
#: :func:`_draw_tile` (one set of caches for each tile). In those caches, ``None`` is a real
#: answer: "a class that we do not draw, on purpose".
_UNRESOLVED = object()

#: The lowest draw priority that a **coarse** pass keeps among the road and rail lines
#: (refer to :func:`render_ground`). Tertiary roads and higher: the through-roads, whose
#: pattern tells where you are, and not the residential streets, which tell only that this
#: is a town. This priority is also above rail (:data:`_RAIL`) and above the default for an
#: unrecognized class. It is stated as the priority that the styles already have, not as a
#: second list of class names. Thus a road that changes class cannot be in one pass and
#: not in the other. At zoom 13 on the PicoCalc, it keeps 2 522 lines of 5 416: less than
#: half of them, for almost all of the legibility.
_COARSE_ROAD_PRIORITY = 23


@dataclass(slots=True)
class _Label:
    """A candidate for a label placement, not placed yet.

    Attributes:
        alts: More anchors to try, in order, when the preferred anchor is already taken.
            A line feature offers these along the part of itself that is on the screen.
            Thus a street whose middle is below a node marker still gets a name at a
            different point.
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
    """The work state while MeshTerm composes one raster.

    Attributes:
        coarse: Whether this is the quick first pass: ground and through-roads only, with
            no buildings, no back streets, and no text (refer to :func:`render_ground`).
    """

    canvas: MapCanvas
    viewport: Viewport
    labels: list[_Label] = field(default_factory=list)
    coarse: bool = False


# -- the stale ground ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ghost:
    """The ground of a finished raster, and the viewport that it was drawn for.

    To rasterize a viewport takes most of a second of pure Python. Thus a pan moves the
    viewport long before its picture can exist. MeshTerm does not answer the key press with
    an empty canvas, because then the map goes black at each step and comes back when the
    raster is ready. Instead, the paint reprojects this ghost: the streets as they were,
    moved to where they now are, below new markers. It is only the ground (refer to
    :class:`~meshterm.ui.mapcanvas.Raster`). The labels and the node markers are drawn
    again at their real positions on top.

    Attributes:
        raster: The braille layer of the earlier raster.
        viewport: The viewport that the earlier raster was drawn for. It tells where the
            dots of that raster moved to after that.
    """

    raster: Raster
    viewport: Viewport


#: How much the reused ground dims while MeshTerm draws its replacement. It is enough to
#: show that the ground is temporary, and to make the edge that the viewport pans onto
#: (not drawn yet) look different from real empty ground. It is not so much that the
#: shape of the streets is lost.
#:
#: The value is bound when the platform changes, and it is off where there is no
#: truecolour. On the 16-slot console, a colour does not dim: it goes to a different slot.
#: Thus a fade there changes the colours: the faint road classes become black, and the
#: other classes mix together. The ``drawing…`` in the title gives the same information on
#: the two platforms.
_GHOST_FADE = 0.6

#: The difference in zoom steps above which MeshTerm does not use the ghost, instead of a
#: scaled ghost. What the ghost shows is true at any distance, because the dots are
#: reprojected, not the glyphs (refer to :func:`~meshterm.ui.mapcanvas._magnified`). But a
#: dot enlarged by more than two steps is eight cells wide. A mosaic that coarse does not
#: help the user to find a position: nothing of the street pattern is left to recognize,
#: and the markers alone are the better picture.
_GHOST_MAX_STEPS = 2


@on_platform
def _bind(platform: Platform) -> None:
    """Bind the fade of the ghost to what the palette of the platform can express."""
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
    """Source cell and sub-cell for each canvas cell on one axis (``(-1, 0)`` for none).

    The two viewports are rectangles in Web Mercator. Thus one maps onto the other by an
    affine scale and shift, on each axis independently: a dot at world position
    ``d + origin`` is at ``(d + origin) * scale - src_origin`` in the older viewport. The
    full reprojection becomes two small index tables, which the paste then reads.

    Where the viewport zoomed in, one source cell spans ``magnify`` cells here. Thus the
    integer part of that division is only half of the answer. The remainder tells which of
    those cells this cell is, and thus which part of the dots of the source it must show
    enlarged (refer to :meth:`~meshterm.ui.mapcanvas.MapCanvas.paste_raster`). At a
    ``magnify`` of 1, the remainder is always zero, and this is the plain table of cell
    indexes that it was before.

    Args:
        cells: The size of the canvas along this axis, in cells.
        dots: The dots for each cell on this axis (2 across, 4 down).
        origin: The world-pixel origin of the canvas viewport on this axis.
        src_origin: The world-pixel origin of the ghost viewport, at the zoom of the ghost.
        scale: The world pixels at the zoom of the ghost for each world pixel at our zoom.
        src_cells: The size of the ghost raster along this axis, for the bounds test.
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
        # The magnified sub-cell that contains the middle of this cell. Floating point can
        # make the fraction very slightly less than 1.0. Thus the last sub-cell is clamped,
        # and the division is not trusted.
        out.append((src, min(int((exact - src) * magnify), magnify - 1)))
    return out


def _paste_ghost(canvas: MapCanvas, viewport: Viewport, ghost: Ghost) -> None:
    """Put the ground of ``ghost`` onto ``canvas``, reprojected to ``viewport``.

    The function does nothing when the zooms of the two viewports are too far apart, or
    when the pan moved the viewport fully off the ground of the old raster. In the two
    cases, nothing is left that can stand in, and the markers alone are the honest picture.
    """
    src = ghost.viewport
    steps = viewport.zoom - src.zoom
    if abs(steps) > _GHOST_MAX_STEPS:
        return
    scale = 2.0**-steps
    magnify = 1 << steps if steps > 0 else 1  # a viewport zoomed out keeps whole glyphs
    ox, oy = viewport.origin_world
    sx, sy = src.origin_world
    cols = _ghost_axis(canvas.cell_w, 2, ox, sx, scale, ghost.raster.cell_w, magnify)
    if not any(c >= 0 for c, _ in cols):
        return
    rows = _ghost_axis(canvas.cell_h, 4, oy, sy, scale, ghost.raster.cell_h, magnify)
    if not any(r >= 0 for r, _ in rows):
        return
    canvas.paste_raster(ghost.raster, cols, rows, magnify=magnify, fade=_GHOST_FADE)


# -- composing a raster -------------------------------------------------------


def render_map(
    viewport: Viewport,
    tiles: dict[tuple[int, int, int], list[Layer] | None],
    markers: list[MapMarker],
    *,
    max_labels: int = 80,
    find: str = "",
    ghost: Ghost | None = None,
) -> list[str]:
    """Render a full map raster to truecolour ANSI lines.

    Args:
        viewport: The viewport to render (its dot size sets the canvas size).
        tiles: Decoded layers, indexed by ``(z, x, y)``. A ``None`` value is pending or
            absent.
        markers: The mesh nodes to put on top.
        max_labels: The maximum number of basemap labels to place, to keep the map
            readable.
        find: A live node-name filter. Markers whose label contains it (case-insensitive)
            draw with bright white labels, and the other markers dim to context with no
            label. When it is empty, each node draws normally.
        ghost: Ground from an earlier raster to put down first, reprojected onto this
            viewport (refer to :class:`Ghost`). It stands in for ground that is not drawn
            yet. Thus it goes with the rasters that have no ``tiles`` of their own. A
            raster that draws the real ground does not use one.

    Returns:
        One ANSI string for each line, ready for the TUI frame or the console.
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
    """Render a raster and keep its ground, so that the next moved viewport can use it.

    It does the same work as :func:`render_map`, and also returns the :class:`Ghost` that
    the raster leaves. That is why the interactive map calls this function for the real
    (background) raster, and :func:`render_map` for the immediate paints between them.

    ``coarse`` draws the **first pass**: the ground fills, the watercourses, the admin
    boundaries, and the through-roads (:data:`_COARSE_ROAD_PRIORITY`). It draws no
    buildings, no back streets, and no text. It is the same picture with less detail, and
    it is a quarter of the work: on the PicoCalc, 249 ms instead of 962 ms at zoom 13, and
    120 ms instead of 529 ms at zoom 16. When it is drawn before the full raster, it gives
    a viewport that moved past its last ground something true almost immediately. It also
    gives a held pan a unit of work that is small enough to cancel. The map asks for this
    pass when :data:`~meshterm.ui.map_screen._COARSE_PREVIEW` is on (refer to
    :meth:`meshterm.ui.map_screen.MapScreen._draw_ground`).

    Args:
        viewport: The viewport to render.
        tiles: Decoded layers, indexed by ``(z, x, y)``.
        markers: The mesh nodes to put on top.
        max_labels: The maximum number of basemap labels to place.
        find: A live node-name filter (refer to :func:`render_map`).
        coarse: Draw the quick first pass instead of the finished raster.

    Returns:
        The ANSI lines, and the ground that they leave.
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
    """Draw one raster onto a new canvas. Refer to :func:`render_map` for the arguments."""
    canvas = MapCanvas(viewport.dot_w // 2, viewport.dot_h // 4)
    frame = _Frame(canvas=canvas, viewport=viewport, coarse=coarse)

    if ghost is not None:
        _paste_ghost(canvas, viewport, ghost)

    for (z, x, y), layers in tiles.items():
        if layers:
            _draw_tile(frame, layers, z, x, y)

    # The nodes reserve their cells first, so that basemap labels go around them.
    _draw_nodes(canvas, viewport, markers, find=find)

    # Then place the basemap labels by importance, and obey the zoom gate and the
    # collisions. OpenStreetMap divides a long street into several named features, so one
    # name can arrive many times. In the width of a terminal, the second copy only takes
    # space from a street that has no name. Thus a name is drawn one time in each raster.
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


#: The y of a point, for ``min``/``max`` to read in C (refer to ``on_view`` in
#: :func:`_draw_tile`).
_Y = itemgetter(1)


def _draw_tile(frame: _Frame, layers: list[Layer], z: int, x: int, y: int) -> None:
    """Draw the geometry of one decoded tile, and collect its label candidates."""
    vp = frame.canvas
    by_name = {layer.name: layer for layer in layers}

    # The projection from tile-local units to dots is affine, and in practice all the
    # layers of a tile have the same extent. Thus its three coefficients are resolved one
    # time for each extent, and the work for each vertex is written inline below (refer to
    # Viewport.tile_transform). A downtown raster projects approximately 50k vertices. At
    # that count, it is worth the removal of even the one function call for each vertex.
    transforms: dict[int, tuple[float, float, float]] = {}

    def transform(extent: int) -> tuple[float, float, float]:
        t = transforms.get(extent)
        if t is None:
            t = transforms[extent] = frame.viewport.tile_transform(x, y, z, extent)
        return t

    def project(ring: list[tuple[int, int]], extent: int) -> list[tuple[float, float]]:
        bx, by, step = transform(extent)
        return [(bx + lx * step, by + ly * step) for lx, ly in ring]

    # The canvas as a rectangle in the units of this tile, one dot wider on each side. Thus
    # a ring can be tested before one point of it is projected. A zoomed-out viewport shows
    # a thin part of tiles whose coastlines and land cover have tens of thousands of
    # points. The projection of all of them was half of a z7 raster. To skip a ring that is
    # fully outside is exact, never an approximation: a line there sets no dot, and the
    # crossings of a closed ring on each scanline come in pairs off the canvas. Thus the
    # even-odd fill of the other rings in the same polygon does not change (refer to
    # MapCanvas.fill_polygon).
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
        # min and max over the tuples run in C. The first element decides, so these are x.
        return not (
            max(ring)[0] < lo_x
            or min(ring)[0] > hi_x
            or max(ring, key=_Y)[1] < lo_y
            or min(ring, key=_Y)[1] > hi_y
        )

    # The palette of a tile is a few colours that tens of thousands of features share. Thus
    # each colour is resolved one time here, not for each feature. mark_rgb caches its
    # results, but at approximately 30 000 calls for each raster, even a dict hit behind a
    # function call takes real time on the handheld. Also, the styles below are constants,
    # so the answer never changes in a tile. The styles for each class (roads, waterways,
    # places) are cached in the dicts next to them.
    water_rgb, water_prio = mark_rgb(_WATER_FILL[0]), _WATER_FILL[1]
    green_rgb, green_prio = mark_rgb(_GREEN_FILL[0]), _GREEN_FILL[1]
    boundary_rgb = mark_rgb("#6d5f88")
    road_styles: dict[str | None, tuple[tuple[int, int, int], int]] = {}
    water_styles: dict[str | None, tuple[tuple[int, int, int], int] | None] = {}

    # Fills first (water, green space), so that lines and labels are on top.
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

    # Buildings, as a stippled texture below the streets.
    building = by_name.get("building")
    if building is not None and frame.viewport.zoom >= _BUILDING_MIN_ZOOM and not frame.coarse:
        rgb = mark_rgb(_BUILDING_FILL[0])
        # A downtown tile holds thousands of footprints, and a zoomed-in viewport shows a
        # few dozen of them. Thus the code tests the bounds of each footprint before it
        # projects the footprint, and rejects each footprint that is outside.
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

    # Waterways (rivers and streams) as lines.
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

    # Roads and rail.
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
                continue  # the back streets are most of the lines, and the least important
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
        return  # all after this point is text, and text is for the second pass

    # Street-name labels, when the streets are far enough apart to have a name each. The
    # zoom gate is here and also at placement (:data:`_STREET_LABEL_MIN_ZOOM`). The label
    # is the expensive half of a named road, and below the gate, each label was built and
    # then removed. The zoom of the viewport decides, not the zoom of the tile. An
    # overzoomed viewport gets its ground from a lower tile, but it is still zoomed in, and
    # it still needs names.
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
    """Clip a segment to the ``0..w`` by ``0..h`` canvas (Liang-Barsky).

    Returns:
        The part of the segment in the canvas, or ``None`` if no part is in it. A street
        that crosses the viewport with its two endpoints outside the edges still gives the
        part that you can see. That is the purpose of a clip, instead of a test of the
        endpoints.
    """
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0), (dx, w - x0), (-dy, y0), (dy, h - y0)):
        if p == 0:
            if q < 0:
                return None  # parallel to this edge, and fully outside it
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
    """Put a label in the queue, on the longest part of a line feature that is on the screen.

    An anchor at the midpoint of the feature (the middle of the street as the tile drew it)
    pins the label to a fixed geographic point, not to your viewport. The more you zoom in,
    the less likely it is that this point is still on the screen. Because of this, street
    names became rarer the more you zoomed in: of 265 named streets in a central Montréal
    tile, 37 anchors were on a 53x26 canvas at zoom 14, and only 6 at zoom 16.

    Thus the line is clipped to the canvas first, and the label goes on the longest piece
    that stays. The next-longest pieces are kept as alternatives, for when that position is
    already taken. A feature that is fully off the screen puts nothing in the queue.
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
    """The glyph and colour of a marker: our node, a repeater, or a leaf node."""
    if marker.is_self:
        return _SELF
    if marker.is_repeater:
        return _REPEATER
    return _NODE


#: The number of piled nodes at which the glyph of a marker gets full brightness.
_PILE_FULL = 8


def _pile_color(color: RGB, count: int, *, cap: int = _PILE_FULL) -> RGB:
    """Make the colour of a marker brighter, by the number of nodes that share its cell.

    One cell can show only one glyph. Thus, where many nodes are at the same position, the
    map would hide the crowd behind one marker. Instead, we keep the glyph of the top node,
    and move its colour toward white as the pile grows. Thus a bright marker shows a busy
    cluster. The ramp is logarithmic (2 nodes → a clear increase, then saturation near
    ``cap``). Thus it stays informative, and one extra node does not look like a crowd.
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


#: How much a node outside an active find filter dims (a multiplier for the glyph colour).
_FIND_DIM = 0.35

#: The label colour for find-filter matches: full white, the brightest thing on the map.
_FIND_MATCH_LABEL: RGB = (255, 255, 255)


def _dimmed(color: RGB, factor: float) -> RGB:
    """``color``, scaled toward black by ``factor``, and clamped to the byte range."""
    return tuple(max(0, min(255, round(c * factor))) for c in color)  # type: ignore[return-value]


def _draw_nodes(
    canvas: MapCanvas, viewport: Viewport, markers: list[MapMarker], *, find: str = ""
) -> None:
    """Draw the mesh nodes on top: each glyph, then labels by importance until they collide.

    The glyphs are drawn lowest priority first, so that our node and the repeaters are on
    top of the leaf nodes. Where several nodes share a cell, the glyph that stays is made
    brighter by the pile size (:func:`_pile_color`). Thus crowded positions glow, and do
    not silently hide the crowd. Then the labels are placed highest priority first (our
    node, then repeaters, then leaf nodes), each only if it fits with no overlap. Thus, on
    a crowded map, the important labels get the available space, and the other nodes show
    as a bare marker (with no overlap).

    When a ``find`` filter is active, only the matching nodes keep their labels. These
    labels are bright white, so that they are easy to see. All the other nodes dim to
    context near the background colour, and a match label never loses a collision to a
    node that does not match.
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

    # A cell is (dot_x >> 1, dot_y >> 2). Count how many nodes are in each cell.
    pile = Counter((ix >> 1, iy >> 2) for _, ix, iy, _m in placed)

    # Matches draw after (over) the dimmed nodes that do not match, at any rank. Thus the
    # node that the user looks for is never hidden below a brighter neighbour.
    for marker, ix, iy, matched in sorted(placed, key=lambda p: (p[3], p[0]._rank())):
        glyph, color = _marker_style(marker)
        rgb = parse_hex(color)
        if not matched:
            rgb = _dimmed(rgb, _FIND_DIM)
        else:
            rgb = _pile_color(rgb, pile[(ix >> 1, iy >> 2)])
        canvas.marker(ix, iy, glyph, rgb)

    # A node that the filter removes is context: a bare dim glyph with no label. Labels get
    # the name hue of the node, which comes from its key (the colour rule for all the app).
    # The label of our node is the pure ``you`` white (the ★ glyph keeps its yellow). An
    # active find filter still makes the label of each match full white, so that the node
    # that the user looks for is easy to see.
    from .widgets import name_rgb  # widgets imports this module, so bind late to avoid the cycle

    labelled = (p for p in placed if p[3])
    for marker, ix, iy, _matched in sorted(labelled, key=lambda p: -p[0]._rank()):
        if needle:
            label_color = _FIND_MATCH_LABEL
        elif marker.is_self:
            label_color = (255, 255, 255)  # the `you` white
        else:
            label_color = name_rgb(marker.label, marker.key)
        canvas.marker_label(ix, iy, marker.label, label_color)
