# SPDX-License-Identifier: Apache-2.0
"""Tests for the node map: MVT decoding, projection, the braille canvas, and the tool.

The vector-tile decoder and geometry are exercised against a real cached tile fixture
(``tests/fixtures/tile_14_4843_5861.mvt``, central Montréal) so the parsing is verified end
to end without the network; the projection, canvas, and compositor are pure; the tool runs
against the :class:`MockDevice` simulator with the basemap disabled (no network in tests).
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

import pytest
from rich.cells import cell_len
from rich.console import Console

from meshterm.core.geo import Viewport, haversine_km, lonlat_to_world, world_to_lonlat
from meshterm.core.models import (
    NODE_TYPE_REPEATER,
    Observation,
    utcnow,
)
from meshterm.core.mvt import GEOM_LINE, GEOM_POLYGON, Layer, decode_tile
from meshterm.services.basemap import TILE_RETRY_SECONDS as _TILE_RETRY
from meshterm.tools.map import MapTool
from meshterm.ui.map_screen import _PAN_DIRS, _PAN_STEP
from meshterm.ui.mapcanvas import MapCanvas, parse_hex
from tests.conftest import plain as _plain  # THE strip-and-join screen reader

if TYPE_CHECKING:
    from meshterm.ui.map_screen import MapScreen

_FIXTURE = Path(__file__).parent / "fixtures" / "tile_14_4843_5861.mvt"

# -- MVT decoding -------------------------------------------------------------


def test_decode_tile_reads_expected_layers() -> None:
    """The real fixture decodes into the OpenMapTiles layers the map relies on."""
    layers = {layer.name: layer for layer in decode_tile(_FIXTURE.read_bytes())}
    for expected in ("transportation", "transportation_name", "water", "waterway", "place"):
        assert expected in layers, expected
    assert layers["transportation"].extent == 4096
    assert layers["transportation"].features  # roads were decoded


def test_decode_tile_resolves_names_and_geometry() -> None:
    """Street and place features carry resolved names and non-trivial geometry."""
    layers = {layer.name: layer for layer in decode_tile(_FIXTURE.read_bytes())}
    streets = [f.name for f in layers["transportation_name"].features if f.name]
    assert any("Rue" in s or "Avenue" in s or "Av." in s for s in streets)

    places = {f.name: f.get("class") for f in layers["place"].features if f.name}
    assert "Montréal" in places and places["Montréal"] == "city"

    # A road is a line with at least two points; water is a polygon.
    assert any(
        f.geom_type == GEOM_LINE and len(f.rings[0]) >= 2 for f in layers["transportation"].features
    )
    assert any(f.geom_type == GEOM_POLYGON for f in layers["water"].features)


def test_decode_tile_handles_empty_input() -> None:
    """An empty byte string decodes to no layers rather than raising."""
    assert decode_tile(b"") == []


def test_decode_tile_filter_keeps_the_whole_tile_visible() -> None:
    """A narrowed decode still names every layer — it only skips their features.

    The tile source decides whether a cache entry is real by whether it parsed into
    layers, so narrowing must never make a tile look like it holds nothing.
    """
    raw = _FIXTURE.read_bytes()
    full = decode_tile(raw)
    narrow = decode_tile(raw, layers={"water"})

    assert [layer.name for layer in narrow] == [layer.name for layer in full]
    assert all(layer.extent == 4096 for layer in narrow)
    picked = {layer.name: layer for layer in narrow}
    assert picked["water"].features  # the one we asked for was decoded
    assert not picked["building"].features  # the rest were skipped, not dropped


def test_decode_tile_filter_is_invisible_to_the_renderer() -> None:
    """Narrowing to ``DRAWN_LAYERS`` draws exactly the map a full decode draws.

    This is the anti-drift guard: if ``_draw_tile`` grows a lookup for a layer that
    isn't in ``DRAWN_LAYERS``, that layer arrives featureless and the rendered output
    diverges here.
    """
    from meshterm.ui.map_render import DRAWN_LAYERS, MapMarker, render_map

    raw = _FIXTURE.read_bytes()
    vp = Viewport(45.5019, -73.5674, 14, 180, 120)
    markers = [MapMarker("Yagi", 45.5019, -73.5674, is_repeater=True)]

    def drawn(layers):
        return render_map(vp, {(14, 4843, 5861): layers}, markers)

    assert drawn(decode_tile(raw, layers=DRAWN_LAYERS)) == drawn(decode_tile(raw))


def test_decode_tile_filter_skips_the_layers_the_map_never_draws() -> None:
    """The layers left undecoded are the ones that made a tile expensive."""
    from meshterm.ui.map_render import DRAWN_LAYERS

    drawn = decode_tile(_FIXTURE.read_bytes(), layers=DRAWN_LAYERS)
    narrow = {layer.name: layer for layer in drawn}
    for skipped in ("housenumber", "poi", "mountain_peak"):
        assert skipped in narrow, f"{skipped} should still be named"
        assert not narrow[skipped].features, f"{skipped} should not have been decoded"


# -- buildings ----------------------------------------------------------------


def _building_dots(zoom: int) -> int:
    """Lit cells in a render of the fixture's buildings alone, at ``zoom``."""
    from meshterm.ui.map_render import render_map

    layers = decode_tile(_FIXTURE.read_bytes(), layers={"building"})
    vp = Viewport(45.4995, -73.5690, zoom, 53 * 2, 26 * 4)
    out = _plain(render_map(vp, {(14, 4843, 5861): layers}, []))
    return sum(1 for ch in out if ch not in " \n")


def test_buildings_wait_for_the_zoom_that_can_hold_them() -> None:
    """Below the gate a footprint is about two dots across, so the layer is a haze."""
    from meshterm.ui.map_render import _BUILDING_MIN_ZOOM

    assert _building_dots(_BUILDING_MIN_ZOOM) > 100
    assert _building_dots(_BUILDING_MIN_ZOOM - 1) == 0


def test_building_fill_is_stippled_so_the_streets_survive_it() -> None:
    """A braille dot is one bit: a *solid* fill would erase the street grid it covers.

    The stipple is what keeps a building a shade rather than an eraser — every cell it
    touches keeps free dots for a road to be drawn through.
    """
    from meshterm.ui.map_render import _BUILDING_MIN_ZOOM, render_map

    layers = decode_tile(_FIXTURE.read_bytes(), layers={"building"})
    vp = Viewport(45.4995, -73.5690, _BUILDING_MIN_ZOOM, 53 * 2, 26 * 4)
    out = _plain(render_map(vp, {(14, 4843, 5861): layers}, []))

    assert "⣿" not in out, "a fully lit cell means the fill left a road nowhere to go"


def test_offscreen_buildings_do_not_change_the_frame() -> None:
    """The bbox reject is an optimisation, so it must be invisible in the output.

    A downtown tile carries thousands of footprints and a zoomed-in view holds a few
    dozen; each one is rejected on its own tile-local bounds before a single point of it
    is projected. Adding a footprint the view cannot reach must render byte-identically.
    """
    from meshterm.core.mvt import Feature
    from meshterm.ui.map_render import _BUILDING_MIN_ZOOM, render_map

    vp = Viewport(45.4995, -73.5690, _BUILDING_MIN_ZOOM, 53 * 2, 26 * 4)
    cx, cy = _tile_local_of_view_centre(vp, 14, 4843, 5861)
    here = [
        (cx - 40, cy - 40),
        (cx + 40, cy - 40),
        (cx + 40, cy + 40),
        (cx - 40, cy + 40),
        (cx - 40, cy - 40),
    ]
    far = [(10, 10), (90, 10), (90, 90), (10, 90), (10, 10)]

    def frame(rings):
        layer = Layer(
            name="building",
            extent=4096,
            features=[Feature(geom_type=GEOM_POLYGON, rings=rings, tags={})],
        )
        return render_map(vp, {(14, 4843, 5861): [layer]}, [])

    assert _plain(frame([here])).strip(), "the in-view footprint should draw something"
    assert frame([here]) == frame([here, far])


def test_offscreen_rings_do_not_change_the_frame() -> None:
    """Every layer skips the rings its view cannot reach, and the skip is just as invisible.

    A zoomed-out view sees a sliver of tiles whose coastlines and land cover run to tens of
    thousands of points, and projecting every one of them was half of a z7 frame on the
    PicoCalc. A hole far off screen in a lake that covers the view, and a road and a river
    that never come near it, must render byte-identically to their absence — the hole most
    of all, since it shares an even-odd fill with the lake around it.
    """
    from meshterm.core.mvt import Feature
    from meshterm.ui.map_render import render_map

    # At z16 a z14 tile is 1024 dots across, four tile units a dot: the 106x104-dot view
    # spans 424x416 units around its centre, so 400 units out is beyond it on every side.
    vp = Viewport(45.4995, -73.5690, 16, 53 * 2, 26 * 4)
    cx, cy = _tile_local_of_view_centre(vp, 14, 4843, 5861)

    def square(x0, y0, x1, y1):
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]

    lake = square(cx - 600, cy - 600, cx + 600, cy + 600)
    island = square(cx - 20, cy - 20, cx + 20, cy + 20)  # a hole the view does show
    far_hole = square(cx - 590, cy - 590, cx - 500, cy - 500)
    far_road = [(cx - 590, cy + 300), (cx - 400, cy + 590)]
    far_river = [(cx + 450, cy - 590), (cx + 590, cy + 590)]

    def frame(water_rings, roads, rivers):
        layers = [
            Layer("water", 4096, [Feature(GEOM_POLYGON, water_rings, {})]),
            Layer("transportation", 4096, [Feature(GEOM_LINE, roads, {"class": "primary"})]),
            Layer("waterway", 4096, [Feature(GEOM_LINE, rivers, {"class": "river"})]),
        ]
        return render_map(vp, {(14, 4843, 5861): layers}, [])

    near_road = [(cx - 300, cy), (cx + 300, cy)]
    near_river = [(cx, cy - 300), (cx, cy + 300)]
    bare = frame([lake, island], [near_road], [near_river])
    assert _plain(bare).strip(), "the lake, the road and the river should all draw"
    assert bare == frame([lake, island, far_hole], [near_road, far_road], [near_river, far_river])


def test_polygon_fill_lights_exactly_the_dots_a_scan_of_every_edge_would() -> None:
    """Filing each edge under the rows it crosses is a speed-up, so it must not move a dot.

    The reference is the fill as it was: every scanline asking every edge whether it is
    crossed. That costs rows times edges, which a coastline at z7 made 6.8 s of a frame on
    the PicoCalc; the answer it gave is the specification.
    """
    import random
    from itertools import pairwise

    def reference(canvas: MapCanvas, rings, stipple: int) -> None:
        edges = [
            (x0, y0, x1, y1) for ring in rings for (x0, y0), (x1, y1) in pairwise(ring) if y0 != y1
        ]
        for y in range(0, canvas.dot_h, stipple):
            yc = y + 0.5
            xs = sorted(
                x0 + (yc - y0) * (x1 - x0) / (y1 - y0)
                for x0, y0, x1, y1 in edges
                if y0 <= yc < y1 or y1 <= yc < y0
            )
            for i in range(0, len(xs) - 1, 2):
                lo = max(0, int(round(xs[i])))
                hi = min(canvas.dot_w - 1, int(round(xs[i + 1])))
                if stipple > 1:
                    lo += -lo % stipple
                for x in range(lo, hi + 1, stipple):
                    canvas.plot(x, y, (1, 2, 3), 5)

    def coordinate(rnd: random.Random) -> float:
        # Mostly anywhere, including well off the canvas; sometimes exactly on a row's
        # middle or a dot's edge, which is where an off-by-one would show.
        if rnd.random() < 0.5:
            return rnd.uniform(-30.0, 110.0)
        return rnd.randint(-5, 90) + rnd.choice((0.0, 0.5, -0.5))

    rnd = random.Random(7)
    for _ in range(500):
        w, h = rnd.randint(1, 40), rnd.randint(1, 20)
        rings = []
        for _ in range(rnd.randint(1, 3)):
            ring = [(coordinate(rnd), coordinate(rnd)) for _ in range(rnd.randint(2, 10))]
            rings.append(ring + ring[:1] if rnd.random() < 0.7 else ring)
        stipple = rnd.choice((1, 1, 2, 3))
        fast, slow = MapCanvas(w, h), MapCanvas(w, h)
        fast.fill_polygon(rings, (1, 2, 3), 5, stipple=stipple)
        reference(slow, rings, stipple)
        assert fast._bits == slow._bits, (w, h, stipple, rings)


# -- projection / viewport ----------------------------------------------------


def test_world_projection_round_trips() -> None:
    """lon/lat -> world pixels -> lon/lat recovers the original coordinate."""
    for lat, lon, z in [(45.5, -73.6, 14), (0.0, 0.0, 3), (-33.9, 151.2, 12)]:
        wx, wy = lonlat_to_world(lat, lon, z)
        rlat, rlon = world_to_lonlat(wx, wy, z)
        assert rlat == pytest.approx(lat, abs=1e-6)
        assert rlon == pytest.approx(lon, abs=1e-6)


def test_haversine_known_distance() -> None:
    """One degree of longitude at the equator is ~111 km; identical points are 0."""
    assert haversine_km(0.0, 0.0, 0.0, 1.0) == pytest.approx(111.19, abs=0.5)
    assert haversine_km(45.0, -73.0, 45.0, -73.0) == 0.0


def test_viewport_center_projects_to_the_middle() -> None:
    """The viewport's centre lands at the middle dot; north is up."""
    vp = Viewport(45.5, -73.6, 13, 200, 120)
    cx, cy = vp.lonlat_to_dot(45.5, -73.6)
    assert cx == pytest.approx(100, abs=0.5) and cy == pytest.approx(60, abs=0.5)
    # A point due north projects above the centre (smaller y).
    _, ny = vp.lonlat_to_dot(45.6, -73.6)
    assert ny < cy


def test_viewport_fit_frames_all_points() -> None:
    """A fitted viewport is centred on the points and zoomed so they share a tile scale."""
    pts = [(45.5019, -73.5674), (45.4768, -73.5990)]
    vp = Viewport.fit(pts, 200, 120, max_zoom=14)
    assert 8 <= vp.zoom <= 14
    for lat, lon in pts:
        x, y = vp.lonlat_to_dot(lat, lon)
        assert 0 <= x < vp.dot_w and 0 <= y < vp.dot_h  # every point is on-canvas


def test_viewport_fit_fraction_ignores_outliers() -> None:
    """Framing half the nodes zooms to the dense core, letting far outliers fall off-canvas."""
    # A tight downtown cluster of five nodes (~50 m across) plus one distant outlier.
    core = [
        (45.5000, -73.5600),
        (45.5003, -73.5602),
        (45.4998, -73.5598),
        (45.5001, -73.5599),
        (45.4999, -73.5601),
    ]
    outlier = (46.80, -71.20)
    pts = core + [outlier]

    full = Viewport.fit(pts, 200, 120, max_zoom=16)
    half = Viewport.fit(pts, 200, 120, max_zoom=16, fraction=0.5)

    # The core-only frame is zoomed in tighter than the frame that must hold the outlier.
    assert half.zoom > full.zoom
    # Every clustered node stays on-canvas; the outlier is pushed off.
    for lat, lon in core:
        x, y = half.lonlat_to_dot(lat, lon)
        assert 0 <= x < half.dot_w and 0 <= y < half.dot_h
    ox, oy = half.lonlat_to_dot(*outlier)
    assert not (0 <= ox < half.dot_w and 0 <= oy < half.dot_h)


def test_viewport_fit_fraction_keeps_small_sets_whole() -> None:
    """With only two nodes there is no core to isolate — both are still framed."""
    pts = [(45.5019, -73.5674), (45.4768, -73.5990)]
    half = Viewport.fit(pts, 200, 120, max_zoom=14, fraction=0.5)
    full = Viewport.fit(pts, 200, 120, max_zoom=14)
    assert (half.center_lat, half.center_lon, half.zoom) == (
        full.center_lat,
        full.center_lon,
        full.zoom,
    )


def test_viewport_zoom_and_pan() -> None:
    """Zooming and panning return new viewports; zoom clamps to its bounds."""
    vp = Viewport(45.5, -73.6, 10, 200, 120)
    assert vp.zoomed(2).zoom == 12
    assert vp.zoomed(50, max_zoom=16).zoom == 16  # clamped
    assert vp.zoomed(-50, min_zoom=3).zoom == 3
    east = vp.panned(0.5, 0)
    assert east.center_lon > vp.center_lon  # panning east increases longitude
    north = vp.panned(0, -0.5)
    assert north.center_lat > vp.center_lat  # panning north increases latitude


def test_viewport_tiles_cover_the_view() -> None:
    """The viewport reports the handful of tiles overlapping it at the tile zoom."""
    vp = Viewport(45.5019, -73.5674, 14, 200, 120)
    tiles = vp.tiles(14)
    assert tiles and all(z == 14 for z, _, _ in tiles)
    # The fixture tile for this centre must be among them.
    assert (14, 4843, 5861) in tiles


# -- braille canvas -----------------------------------------------------------


def test_canvas_plots_dots_as_braille() -> None:
    """A single plotted dot renders as a braille glyph in the top-left cell."""
    canvas = MapCanvas(4, 2)
    canvas.plot(0, 0, (255, 0, 0), 1)
    out = _plain(canvas.to_ansi_lines())
    assert out.splitlines()[0][0] == "⠁"  # braille dot-1


def test_canvas_priority_decides_cell_colour() -> None:
    """When dots from two features share a cell, the higher priority sets its colour."""
    canvas = MapCanvas(1, 1)
    canvas.plot(0, 0, (10, 20, 30), 1)  # low priority
    canvas.plot(1, 0, (200, 100, 50), 9)  # high priority, same cell
    ansi = "".join(canvas.to_ansi_lines())
    assert "38;2;200;100;50" in ansi  # the high-priority colour won
    assert "38;2;10;20;30" not in ansi


def test_canvas_marker_and_label_do_not_overprint() -> None:
    """A marker keeps its glyph and places its label in a neighbouring cell."""
    canvas = MapCanvas(20, 3)
    canvas.marker(4, 4, "★", (255, 255, 255))
    assert canvas.marker_label(4, 4, "me", (255, 255, 255))
    out = _plain(canvas.to_ansi_lines())
    assert "★" in out and "me" in out
    row = out.splitlines()[1]
    assert "★me" not in row  # a gap sits between the marker and its label


def test_canvas_label_collision_is_avoided() -> None:
    """A checked label is skipped when it would overlap an already-placed one."""
    canvas = MapCanvas(20, 1)
    assert canvas.place_label(10, 0, "First", (200, 200, 200))
    assert not canvas.place_label(10, 0, "Second", (200, 200, 200))  # overlaps → skipped


def test_canvas_labels_keep_a_vertical_gap() -> None:
    """A checked label is skipped when it would sit flush above/below existing text."""
    canvas = MapCanvas(20, 3)
    assert canvas.place_label(20, 4, "Row1", (200, 200, 200))  # dot y=4 → cell row 1
    # Directly below (cell row 2) with overlapping columns: rejected for lack of a gap.
    assert not canvas.place_label(20, 8, "Row2", (200, 200, 200))
    # Same row but clear of the horizontal span still fits.
    assert canvas.place_label(2, 4, "Far", (200, 200, 200))


def _dot_colors(lines: list[str]) -> set[tuple[int, int, int]]:
    """Every truecolour a braille run was drawn in (the ground's colours, not the text's)."""
    return {
        (int(r), int(g), int(b))
        for r, g, b in re.findall(r"38;2;(\d+);(\d+);(\d+)m(?:\x1b\[1m)?[⠀-⣿]", "".join(lines))
    }


def test_canvas_paste_raster_offsets_fades_and_yields_the_cell() -> None:
    """A pasted raster lands where the caller's index tables put it, dimmed, underneath.

    ``-1`` is a cell with no source — the ground a pan has just moved onto — and the
    pasted cell keeps the empty-cell priority so anything drawn afterwards wins it.
    """
    from meshterm.ui.mapcanvas import MapCanvas

    src = MapCanvas(3, 1)
    src.plot(0, 0, (200, 100, 50), 5)  # cell 0
    src.plot(4, 0, (60, 120, 240), 5)  # cell 2

    canvas = MapCanvas(3, 1)
    canvas.paste_raster(src.raster(), [(-1, 0), (0, 0), (2, 0)], [(0, 0)], fade=0.5)
    out = "".join(canvas.to_ansi_lines())
    assert _plain([out]) == " ⠁⠁"  # shifted one cell right; the gap has no source
    assert _dot_colors([out]) == {(100, 50, 25), (30, 60, 120)}

    canvas.plot(2, 0, (10, 20, 30), 0)  # a real feature, drawn after, takes the cell
    assert _dot_colors(["".join(canvas.to_ansi_lines())]) >= {(10, 20, 30)}


def test_canvas_paste_raster_magnifies_by_the_dot_not_by_the_glyph() -> None:
    """A zoomed-in stand-in shows each cell its own share of the source, enlarged.

    Stamping the whole source glyph into every cell of the block it grew into is double
    vision — the same 2x4 pattern side by side and one above the other, which reads as a
    fault rather than as a coarse preview (JP, 2026-08-18).
    """
    from meshterm.ui.mapcanvas import MapCanvas

    src = MapCanvas(1, 1)
    src.plot(0, 0, (200, 100, 50), 5)  # the cell's top-left dot, and nothing else

    canvas = MapCanvas(2, 2)
    cols = [(0, 0), (0, 1)]
    canvas.paste_raster(src.raster(), cols, cols, magnify=2)
    rendered = [_plain([line]) for line in canvas.to_ansi_lines()]

    # A source cell is 2x4 dots, so at 2x it becomes the 2x2 block of cells drawn here,
    # and its one top-left dot becomes a 2x2 block of dots: the top half of the top-left
    # cell alone. The glyph repeated four times would be the bug.
    assert rendered == ["⠛ ", "  "], rendered


def test_parse_hex() -> None:
    """Hex colours parse to RGB triples, with or without the leading hash."""
    assert parse_hex("#38bdf8") == (0x38, 0xBD, 0xF8)
    assert parse_hex("ffffff") == (255, 255, 255)


# -- compositor over a real tile ----------------------------------------------


def test_render_map_draws_basemap_streets_and_labels() -> None:
    """Rendering the real fixture yields braille geometry and a place label."""
    from meshterm.ui.map_render import MapMarker, render_map

    vp = Viewport(45.5019, -73.5674, 14, 180, 120)
    layers = decode_tile(_FIXTURE.read_bytes())
    tiles = {(14, 4843, 5861): layers}
    markers = [MapMarker("Yagi", 45.5019, -73.5674, is_repeater=True)]
    out = _plain(render_map(vp, tiles, markers))
    assert any(0x2800 <= ord(ch) <= 0x28FF for ch in out)  # braille was drawn
    assert "▲" in out and "Yagi" in out  # the repeater marker + label
    assert "Montréal" in out  # a place label from the tile


def test_render_map_prioritises_repeater_glyph() -> None:
    """A repeater and a node at the same spot resolve to the repeater's marker on top."""
    from meshterm.ui.map_render import MapMarker, render_map

    vp = Viewport(45.50, -73.57, 14, 60, 40)
    markers = [
        MapMarker("node", 45.50, -73.57),
        MapMarker("rptr", 45.50, -73.57, is_repeater=True),
    ]
    out = _plain(render_map(vp, {}, markers))
    assert "▲" in out and "●" not in out


def _street_labels_placed(cols: int, rows: int, zoom: int) -> int:
    """How many street names actually reach the canvas at this size and zoom."""
    from meshterm.ui.map_render import DRAWN_LAYERS, _draw_tile, _Frame
    from meshterm.ui.mapcanvas import MapCanvas

    layers = decode_tile(_FIXTURE.read_bytes(), layers=DRAWN_LAYERS)
    vp = Viewport(45.5019, -73.5674, zoom, cols * 2, (rows - 4) * 4)
    canvas = MapCanvas(vp.dot_w // 2, vp.dot_h // 4)
    frame = _Frame(canvas=canvas, viewport=vp)
    _draw_tile(frame, layers, 14, 4843, 5861)

    placed = 0
    for label in sorted(frame.labels, key=lambda lab: lab.rank):
        if vp.zoom < label.min_zoom:
            continue
        for ax, ay in ((label.x, label.y), *label.alts):
            if canvas.place_label(ax, ay, label.text, label.color, bold=label.bold):
                placed += label.min_zoom == 15  # the street-label gate identifies them
                break
    return placed


def test_street_labels_get_denser_as_you_zoom_in() -> None:
    """Zooming toward a street must not make its name less likely to appear.

    Anchoring a label to the feature's own midpoint pinned it to a fixed geographic
    point, so the tighter the view the less often that point was still on screen — the
    fixture's 265 named streets yielded 2 labels at zoom 16 and 10 at zoom 15.
    """
    assert _street_labels_placed(53, 26, 16) >= 5
    assert _street_labels_placed(53, 26, 15) >= 10
    # A wider terminal sees more of the same ground, so it may name more -- never fewer.
    assert _street_labels_placed(72, 24, 16) >= _street_labels_placed(53, 26, 16)


def test_street_labels_are_not_built_below_the_zoom_that_can_place_them() -> None:
    """The expensive half of a named road is the label, and below the gate it was binned.

    ``_compose`` never places a label under its own ``min_zoom``; ``_draw_tile`` used to
    build one anyway — clipping the line segment by segment, measuring each surviving
    piece and sorting them, 270 times over for a central tile. Measured at 16.8 ms of a
    46 ms frame on the desktop, so upwards of a third of a second on the PicoCalc, at the
    two zooms the map most often sits at.

    The gate must be invisible in the picture: what it drops is exactly what was thrown
    away a moment later, so the frame either side of it is the same frame.
    """
    from meshterm.ui.map_render import (
        _STREET_LABEL_MIN_ZOOM,
        DRAWN_LAYERS,
        MapMarker,
        render_ground,
    )

    layers = decode_tile(_FIXTURE.read_bytes(), layers=DRAWN_LAYERS)
    tiles = {(14, 4843, 5861): layers}
    markers = [MapMarker("Yagi", 45.5019, -73.5674)]
    below = _STREET_LABEL_MIN_ZOOM - 1

    assert _street_labels_queued(below, tiles) == 0, "street labels built below the gate"
    assert _street_labels_queued(_STREET_LABEL_MIN_ZOOM, tiles) > 0, (
        "the gate swallowed the labels at the zoom that wants them"
    )
    # Overzoom: the display is past the source's max, so the ground comes off a z14 tile —
    # but the reader is still zoomed in, and still wants the names.
    assert _street_labels_queued(_STREET_LABEL_MIN_ZOOM + 2, tiles) > 0, (
        "an overzoomed view lost its street names to the tile's zoom"
    )

    # And the picture is untouched either side of the gate, coarse pass included.
    for zoom in (below - 2, below, _STREET_LABEL_MIN_ZOOM, _STREET_LABEL_MIN_ZOOM + 2):
        vp = Viewport(45.5019, -73.5674, zoom, 53 * 2, 26 * 4)
        for coarse in (False, True):
            lines, ghost = render_ground(vp, tiles, markers, coarse=coarse)
            assert lines, f"nothing drawn at z{zoom}"
            # The ghost is the braille layer alone: a label never reached it anyway.
            assert ghost.raster.cell_w == vp.dot_w // 2


def _street_labels_queued(zoom: int, tiles) -> int:
    """How many street-name candidates a frame at ``zoom`` builds before placing anything."""
    from meshterm.ui.map_render import _STREET_LABEL, _draw_tile, _Frame
    from meshterm.ui.mapcanvas import MapCanvas

    vp = Viewport(45.5019, -73.5674, zoom, 53 * 2, 26 * 4)
    frame = _Frame(canvas=MapCanvas(vp.dot_w // 2, vp.dot_h // 4), viewport=vp)
    for (z, x, y), layers in tiles.items():
        _draw_tile(frame, layers, z, x, y)
    return sum(1 for label in frame.labels if label.rank == _STREET_LABEL[2])


def _tile_local_of_view_centre(vp: Viewport, tz: int, tx: int, ty: int, extent: int = 4096):
    """The tile-local point that lands in the middle of this viewport (inverts feature_to_dot)."""
    from meshterm.core.geo import TILE_PX

    ox, oy = vp.origin_world
    scale = 2.0 ** (vp.zoom - tz)
    lx = extent * ((vp.dot_w / 2 + ox) / (TILE_PX * scale) - tx)
    ly = extent * ((vp.dot_h / 2 + oy) / (TILE_PX * scale) - ty)
    return lx, ly


def test_street_name_is_drawn_only_once() -> None:
    """OSM splits a long street into several features; the map names it once."""
    from meshterm.ui.map_render import DRAWN_LAYERS, MapMarker, render_map

    layers = decode_tile(_FIXTURE.read_bytes(), layers=DRAWN_LAYERS)
    vp = Viewport(45.5019, -73.5674, 16, 53 * 2, 22 * 4)
    out = _plain(
        render_map(
            vp, {(14, 4843, 5861): layers}, [MapMarker("Hub", 45.5040, -73.5700, is_repeater=True)]
        )
    )

    # René-Lévesque arrives as several segments and used to be drawn twice on one screen.
    assert out.count("René-Lévesque") <= 1, "a street was named more than once"


def test_line_label_anchors_on_the_visible_stretch() -> None:
    """A street crossing the view is named even with both its endpoints off screen."""
    from meshterm.ui.map_render import _add_line_label, _Frame
    from meshterm.ui.mapcanvas import MapCanvas

    vp = Viewport(45.5019, -73.5674, 14, 120, 80)
    frame = _Frame(canvas=MapCanvas(60, 20), viewport=vp)
    _, cy = _tile_local_of_view_centre(vp, 14, 4843, 5861)
    # A line spanning the whole tile at the view's latitude: its own midpoint is far away,
    # but it crosses the canvas, so the clip must find it.
    _add_line_label(
        frame, [[(0, cy), (4096, cy)]], 4096, 14, 4843, 5861, "Rue Long", ("#9aa0aa", False, 7)
    )

    assert len(frame.labels) == 1
    label = frame.labels[0]
    assert 0 <= label.x <= vp.dot_w and 0 <= label.y <= vp.dot_h


def test_line_label_off_screen_is_not_queued() -> None:
    """A feature with nothing on screen costs no label slot at all."""
    from meshterm.ui.map_render import _add_line_label, _Frame
    from meshterm.ui.mapcanvas import MapCanvas

    vp = Viewport(45.5019, -73.5674, 16, 120, 80)
    frame = _Frame(canvas=MapCanvas(60, 20), viewport=vp)
    # A short line in the far corner of the tile, well outside a zoom-16 window.
    _add_line_label(
        frame, [[(0, 0), (8, 8)]], 4096, 14, 4843, 5861, "Nowhere", ("#9aa0aa", False, 7)
    )

    assert frame.labels == []


def test_line_label_offers_alternates_along_the_visible_run() -> None:
    """A crowded first choice falls back further along the street rather than vanishing."""
    from meshterm.ui.map_render import _add_line_label, _Frame
    from meshterm.ui.mapcanvas import MapCanvas

    vp = Viewport(45.5019, -73.5674, 14, 120, 80)
    frame = _Frame(canvas=MapCanvas(60, 20), viewport=vp)
    cx, cy = _tile_local_of_view_centre(vp, 14, 4843, 5861)
    # A zig-zag across the view gives several visible pieces to choose between.
    ring = [(cx - 120 + i * 40, cy - 40 + (i % 2) * 80) for i in range(8)]
    _add_line_label(frame, [ring], 4096, 14, 4843, 5861, "Rue Zig", ("#9aa0aa", False, 7))

    assert frame.labels and frame.labels[0].alts, "no fallback anchors offered"
    assert all(a != (frame.labels[0].x, frame.labels[0].y) for a in frame.labels[0].alts)


def test_render_map_drops_crowded_labels_favouring_repeaters() -> None:
    """When labels can't all fit, the repeater's wins and a crowded node's is dropped."""
    from meshterm.ui.map_render import MapMarker, render_map

    # Three nodes stacked on one spot: only the two sides (left/right) can hold a label,
    # so one of the three must show as a bare marker — and the repeater must not be it.
    vp = Viewport(45.50, -73.57, 14, 40, 8)
    markers = [
        MapMarker("NODEONE", 45.50, -73.57),
        MapMarker("NODETWO", 45.50, -73.57),
        MapMarker("REPEATER", 45.50, -73.57, is_repeater=True),
    ]
    out = _plain(render_map(vp, {}, markers))
    assert "▲" in out  # the repeater's glyph is on top
    assert "REPEATER" in out  # and it keeps its label (placed first)
    # Only one of the two leaf nodes could fit a label; the other is a bare marker.
    assert ("NODEONE" in out) != ("NODETWO" in out)


def _glyph_color(lines: list[str], glyph: str) -> tuple[int, int, int]:
    """Extract the truecolour ``(r, g, b)`` the given glyph was rendered with."""
    m = re.search(r"38;2;(\d+);(\d+);(\d+)m(?:\x1b\[1m)?" + re.escape(glyph), "".join(lines))
    assert m, f"glyph {glyph!r} not found with a colour"
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def test_render_map_brightens_piled_markers() -> None:
    """A cell many nodes share renders its glyph brighter than a lone node's."""
    from meshterm.ui.map_render import MapMarker, render_map
    from meshterm.ui.marks import NODE_MARK

    base = parse_hex(NODE_MARK[1])
    vp = Viewport(45.50, -73.57, 14, 60, 40)

    lone = render_map(vp, {}, [MapMarker("solo", 45.50, -73.57)])
    assert _glyph_color(lone, "●") == base  # a single node keeps its base colour

    crowd = [MapMarker(f"n{i}", 45.50, -73.57) for i in range(8)]
    piled = _glyph_color(render_map(vp, {}, crowd), "●")
    # Washed toward white: every channel is brighter than the base cyan.
    assert all(p > b for p, b in zip(piled, base, strict=True))
    assert piled != base


def test_render_map_works_without_basemap() -> None:
    """With no tiles the nodes still render on a blank grid (the offline fallback)."""
    from meshterm.ui.map_render import MapMarker, render_map

    vp = Viewport.fit([(45.5, -73.6), (45.4, -73.5)], 120, 80, max_zoom=14)
    markers = [
        MapMarker("A", 45.5, -73.6, is_self=True),
        MapMarker("B", 45.4, -73.5, is_repeater=True),
    ]
    out = _plain(render_map(vp, {}, markers))
    assert "★" in out and "▲" in out


# -- the ghost ground ---------------------------------------------------------


def _dot_rows(lines: list[str]) -> list[str]:
    """Each rendered row reduced to its braille dots (labels and markers blanked out)."""
    return [
        "".join(ch if 0x2800 <= ord(ch) <= 0x28FF else " " for ch in row)
        for row in _plain(lines).split("\n")
    ]


def _ghosted(zoom: int = 14, *, dlon: float = 0.0, dlat: float = 0.0, dz: int = 0):
    """Draw the fixture, then paint a moved view standing on that frame's ground."""
    from meshterm.ui.map_render import MapMarker, render_ground, render_map

    vp = Viewport(45.5019, -73.5674, zoom, 106, 104)
    tiles = {(14, 4843, 5861): decode_tile(_FIXTURE.read_bytes())}
    markers = [MapMarker("Yagi", 45.5019, -73.5674, is_repeater=True)]
    lines, ghost = render_ground(vp, tiles, markers)
    moved = Viewport(vp.center_lat + dlat, vp.center_lon + dlon, zoom + dz, 106, 104)
    return lines, render_map(moved, {}, markers, ghost=ghost)


def test_a_panned_view_stands_on_the_ground_it_just_had() -> None:
    """Panning slides the last frame's streets across rather than blanking to black.

    Rasterizing takes far too long to sit on a keypress, so the paint that answers a pan
    has no ground of its own. Without the ghost it drew the markers on an empty canvas —
    the map going black between every step and flashing back when the frame landed.
    """
    from meshterm.ui.map_render import MapMarker, render_map

    vp = Viewport(45.5019, -73.5674, 14, 106, 104)
    markers = [MapMarker("Yagi", 45.5019, -73.5674, is_repeater=True)]
    bare = sum(1 for ch in _plain(render_map(vp, {}, markers)) if ch.strip())

    drawn, moved = _ghosted(dlon=0.004)
    lit = sum(1 for ch in _plain(moved) if ch.strip())
    assert lit > bare * 10, "the panned paint is as empty as one with no ghost at all"
    assert lit < sum(1 for ch in _plain(drawn) if ch.strip()), "nothing was left behind"


def test_the_ghost_ground_lands_where_the_pan_put_it() -> None:
    """The reused dots move by exactly the cells the view moved, to within one cell."""
    drawn, moved = _ghosted(dlon=0.004)
    # 0.004° of longitude at zoom 14: dots per degree = 256 * 2^14 / 360.
    shift = round(0.004 * 256 * (2**14) / 360 / 2)  # → cells (2 dots wide)

    before, after = _dot_rows(drawn), _dot_rows(moved)
    assert len(before) == len(after)
    matched = sum(
        1
        for old, new in zip(before, after, strict=True)
        if old[shift:].rstrip() and new.rstrip() == old[shift:].rstrip()
    )
    assert matched > len(before) // 2, "the ground did not slide with the view"


def test_the_ghost_ground_carries_no_stale_text() -> None:
    """Only the dots come along: a label pinned to old ground would name the wrong place."""
    drawn, moved = _ghosted(dlon=0.004)
    assert "Montréal" in _plain(drawn)
    assert "Montréal" not in _plain(moved)
    assert "Yagi" in _plain(moved)  # the markers are redrawn where they really are


def test_the_ghost_ground_dims_while_its_replacement_is_drawn() -> None:
    """Reused ground reads as provisional — it is stale, and short of the leading edge."""
    from meshterm.ui.map_render import _GHOST_FADE

    assert 0 < _GHOST_FADE < 1  # the desktop default; the 16-slot console binds it to 1.0
    drawn, moved = _ghosted(dlon=0.004)
    faded = {tuple(round(c * _GHOST_FADE) for c in rgb) for rgb in _dot_colors(drawn)}
    ghost = _dot_colors(moved)
    assert ghost and ghost <= faded, "the reused ground is not the ground we drew"
    assert not ghost & _dot_colors(drawn), "it came through at full strength"


def test_the_ghost_ground_is_dropped_once_it_says_nothing() -> None:
    """A view that has left the old frame behind gets the honest empty canvas."""
    _, far = _ghosted(dlon=4.0)  # panned clean off the ground we had
    assert not any(row.strip() for row in _dot_rows(far))
    _, deep = _ghosted(dz=3)  # zoomed past what a cell-coarse stand-in can say
    assert not any(row.strip() for row in _dot_rows(deep))
    _, near = _ghosted(dz=1)  # one step is still a readable preview
    assert any(row.strip() for row in _dot_rows(near))


# -- basemap source (offline behaviour) ---------------------------------------


def test_basemap_user_agent_is_contactable() -> None:
    """The tile user agent names a repository that exists and the version actually running.

    OpenFreeMap issues no API key, so this string is the whole of what a tile operator has
    to tell one client from another and to reach whoever is costing them bandwidth. It read
    ``MeshTerm/0.1 (+https://github.com/; mesh node map)`` — a host nobody can visit, and a
    number that was already wrong — which is the shape of traffic that gets a project
    blocked rather than emailed.
    """
    from meshterm import __version__
    from meshterm.services.basemap import _USER_AGENT

    assert _USER_AGENT == (
        f"MeshTerm/{__version__} (+https://github.com/jpmartineau/MeshTerm; mesh node map)"
    )
    assert "https://github.com/;" not in _USER_AGENT, "the stub URL is back"
    assert "mesh node map" in _USER_AGENT, "the operator can no longer tell what the traffic is"


def test_basemap_sends_its_user_agent_on_every_fetch(tmp_path: Path) -> None:
    """The string is not decoration: it rides the header of the request that leaves."""
    from meshterm.services import basemap as basemap_mod

    src = _offline_source(tmp_path / "cache")
    seen: list[urllib.request.Request] = []

    def _urlopen(req: urllib.request.Request, timeout: float | None = None, context=None):
        seen.append(req)
        raise urllib.error.URLError("no network in tests")

    original = urllib.request.urlopen
    urllib.request.urlopen = _urlopen  # type: ignore[assignment]
    try:
        assert src._http_get("http://tiles.invalid/x") == basemap_mod._Response(False, b"")
    finally:
        urllib.request.urlopen = original  # type: ignore[assignment]

    assert seen, "no request was ever built"
    assert seen[0].get_header("User-agent") == basemap_mod._USER_AGENT


def test_basemap_keeps_the_default_tls_context_when_it_works() -> None:
    """A working platform trust store is left alone — urllib's default is passed through."""
    from meshterm.services import basemap as basemap_mod

    basemap_mod._tls_context.cache_clear()
    try:
        assert basemap_mod._tls_context() is None
    finally:
        basemap_mod._tls_context.cache_clear()


def test_basemap_finds_a_ca_bundle_when_the_default_store_is_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty default store falls back to the OS CA bundle instead of failing every fetch.

    THE macOS bug: a frozen build asks OpenSSL for a trust store at a filesystem path baked
    into the interpreter's own build, which need not exist on a machine that never
    installed that Python — which is every machine a PyInstaller bundle lands on. Fetches
    then failed with ``CERTIFICATE_VERIFY_FAILED``, and because a refused fetch is
    indistinguishable from empty terrain the map drew no ground under the nodes and said
    nothing. Windows hid it by reading the OS certificate store in its default context.
    """
    import ssl

    from meshterm.services import basemap as basemap_mod

    # The file only has to exist: loading is recorded below rather than really parsed,
    # so the test pins *which path is chosen* without depending on a PEM being around.
    bundle = tmp_path / "cert.pem"
    bundle.write_text("# stand-in for the OS bundle\n", encoding="utf-8")

    loaded: list[str] = []

    class _EmptyStore(ssl.SSLContext):
        def get_ca_certs(self, binary_form: bool = False):  # type: ignore[override]
            return []

        def load_verify_locations(self, cafile=None, capath=None, cadata=None):  # type: ignore[override]
            loaded.append(cafile or "")

    monkeypatch.setattr(
        ssl, "create_default_context", lambda *a, **k: _EmptyStore(ssl.PROTOCOL_TLS_CLIENT)
    )
    monkeypatch.setattr(basemap_mod, "_CA_BUNDLES", (str(tmp_path / "nope.pem"), str(bundle)))

    basemap_mod._tls_context.cache_clear()
    try:
        ctx = basemap_mod._tls_context()
        assert ctx is not None, "no context built despite a readable bundle"
        assert loaded == [str(bundle)]  # skipped the missing path, used the present one
        assert ctx.verify_mode == ssl.CERT_REQUIRED  # never verified away to make it work
    finally:
        basemap_mod._tls_context.cache_clear()


def test_basemap_never_disables_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no bundle anywhere, the fetch still verifies — it fails rather than trusts."""
    import ssl

    from meshterm.services import basemap as basemap_mod

    class _EmptyStore(ssl.SSLContext):
        def get_ca_certs(self, binary_form: bool = False):  # type: ignore[override]
            return []

    monkeypatch.setattr(
        ssl, "create_default_context", lambda *a, **k: _EmptyStore(ssl.PROTOCOL_TLS_CLIENT)
    )
    monkeypatch.setattr(basemap_mod, "_CA_BUNDLES", ("/nonexistent/ca.pem",))

    basemap_mod._tls_context.cache_clear()
    try:
        assert basemap_mod._tls_context() is None  # urllib's default, still verifying
    finally:
        basemap_mod._tls_context.cache_clear()


def test_basemap_user_agent_needs_no_installed_distribution() -> None:
    """Importing the module must not depend on package metadata being on disk.

    MeshTerm is run straight from a checkout as often as from a wheel — this repo's own
    venv and the PicoCalc's deploy both are — so a version read through
    ``importlib.metadata`` would raise :class:`PackageNotFoundError` on the tile path in
    exactly the setups it is developed in. The version comes from the package instead, and
    this pins that: with the metadata lookup made to fail outright, the module still
    imports and still carries a real version.
    """
    import importlib
    import importlib.metadata

    from meshterm import __version__
    from meshterm.services import basemap as basemap_mod

    def _raise(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    original = importlib.metadata.version
    importlib.metadata.version = _raise  # type: ignore[assignment]
    try:
        reloaded = importlib.reload(basemap_mod)
        assert f"MeshTerm/{__version__}" in reloaded._USER_AGENT
    finally:
        importlib.metadata.version = original  # type: ignore[assignment]
        importlib.reload(basemap_mod)


def test_basemap_source_offline_is_graceful(tmp_path: Path) -> None:
    """An unreachable source reports unavailable and returns no tiles, never raising."""
    from meshterm.services.basemap import BasemapSource

    src = BasemapSource(tmp_path / "cache", tilejson_url="http://127.0.0.1:1/none", timeout=0.2)
    assert src.available is False
    assert src.max_zoom == 14  # sensible default when the TileJSON can't be fetched
    assert src.load_tile(14, 4843, 5861) is None


def test_basemap_source_reads_cached_tile(tmp_path: Path) -> None:
    """A tile already on disk is decoded without any network access."""
    from meshterm.services.basemap import BasemapSource

    cache = tmp_path / "cache"
    dest = cache / "tiles" / "14" / "4843"
    dest.mkdir(parents=True)
    (dest / "5861.pbf").write_bytes(_FIXTURE.read_bytes())
    src = BasemapSource(cache, tilejson_url="http://127.0.0.1:1/none", timeout=0.2)
    layers = src.load_tile(14, 4843, 5861)
    assert layers is not None
    assert any(layer.name == "transportation" for layer in layers)


def _offline_source(cache: Path):
    """A source that can't reach anything — every fetch is silence, not an empty tile."""
    from meshterm.services.basemap import BasemapSource

    return BasemapSource(cache, tilejson_url="http://127.0.0.1:1/none", timeout=0.2)


def test_basemap_source_never_caches_an_unanswered_fetch(tmp_path: Path) -> None:
    """A timeout writes nothing: a blank square now must not become a blank square for ever."""
    from meshterm.services import basemap as basemap_mod

    cache = tmp_path / "cache"
    assert _offline_source(cache).load_tile(14, 4843, 5861) is None
    assert list(cache.rglob("*.pbf")) == []

    # The same, with the source resolved and the *fetch* the thing that fails — the flaky
    # link the PicoCalc lives on. The tile stays unknown, so the next pan asks again.
    src = _offline_source(cache)
    src._template = "http://tiles.invalid/{z}/{x}/{y}.pbf"  # resolved: the fetch is what fails
    calls: list[str] = []

    def _get(url: str):
        calls.append(url)
        return basemap_mod._Response(False, b"")

    src._http_get = _get  # type: ignore[method-assign]
    assert src.load_tile(14, 4843, 5861) is None
    assert src.load_tile(14, 4843, 5861) is None
    assert len(calls) == 2  # retried, not written off as empty
    assert list(cache.rglob("*.pbf")) == []


def test_basemap_source_asks_again_after_a_failed_resolve(tmp_path: Path) -> None:
    """Offline at open is a moment, not a verdict — the PicoCalc's Wi-Fi lands after boot.

    Latching the first failure meant an app started alongside the device's network drew
    every tile of every session from whatever the cache already held (JP, 2026-08-18).
    """
    from meshterm.services import basemap as basemap_mod

    src = _offline_source(tmp_path / "cache")
    assert src.available is False
    assert src.max_zoom == 14  # the default, because nothing was resolved

    doc = b'{"tiles": ["http://tiles.invalid/{z}/{x}/{y}.pbf"], "maxzoom": 14}'
    src._http_get = lambda url: basemap_mod._Response(True, doc)  # type: ignore[method-assign]
    src._resolve_after = 0.0  # the cooldown, stepped over rather than slept through
    assert src.max_zoom == 14
    assert src.available is True, "the source never asked again"


def test_basemap_source_holds_a_failed_resolve_for_its_cooldown(tmp_path: Path) -> None:
    """Asking again is not asking constantly: a genuinely offline map isn't a retry loop."""
    from meshterm.services import basemap as basemap_mod

    src = _offline_source(tmp_path / "cache")
    calls: list[str] = []
    src._http_get = lambda url: (  # type: ignore[method-assign]
        calls.append(url),
        basemap_mod._Response(False, b""),
    )[1]
    for _ in range(5):
        assert src.available is False
    assert calls == []  # the constructor's own failed resolve still stands


def test_basemap_source_tells_an_empty_answer_from_silence(tmp_path: Path) -> None:
    """The two ``None`` returns of ``load_tile``, told apart — a caching caller needs it."""
    from meshterm.services import basemap as basemap_mod

    src = _offline_source(tmp_path / "cache")
    src._template = "http://tiles.invalid/{z}/{x}/{y}.pbf"
    src._http_get = lambda url: basemap_mod._Response(False, b"")  # type: ignore[method-assign]
    assert src.load_tile(14, 4843, 5861) is None
    assert src.answered_empty(14, 4843, 5861) is False  # no answer — worth asking again

    src._http_get = lambda url: basemap_mod._Response(True, b"")  # type: ignore[method-assign]
    assert src.load_tile(14, 1, 1) is None
    assert src.answered_empty(14, 1, 1) is True  # the source said so; that settles it


def test_basemap_source_prunes_a_blank_cached_tile(tmp_path: Path) -> None:
    """A zero-byte entry (an older build's failure marker) is dropped rather than drawn."""
    cache = tmp_path / "cache"
    tile = cache / "tiles" / "14" / "4843" / "5861.pbf"
    tile.parent.mkdir(parents=True)
    tile.write_bytes(b"")
    assert _offline_source(cache).load_tile(14, 4843, 5861) is None
    assert not tile.exists()  # pruned, so a session with network re-fetches it


def test_basemap_source_prunes_a_corrupt_cached_tile(tmp_path: Path) -> None:
    """Bytes cut short mid-write don't decode, so they're pruned instead of kept blank."""
    cache = tmp_path / "cache"
    tile = cache / "tiles" / "14" / "4843" / "5861.pbf"
    tile.parent.mkdir(parents=True)
    tile.write_bytes(_FIXTURE.read_bytes()[:200])
    assert _offline_source(cache).load_tile(14, 4843, 5861) is None
    assert not tile.exists()


def test_basemap_source_caches_only_tiles_with_content(tmp_path: Path) -> None:
    """The disk cache holds decodable geometry — never an empty or unparseable answer."""
    from meshterm.services import basemap as basemap_mod

    cache = tmp_path / "cache"
    src = _offline_source(cache)
    src._template = "http://tiles.invalid/{z}/{x}/{y}.pbf"  # skip TileJSON resolution
    src._resolved = True

    bodies = iter([b"", b"not a vector tile at all", _FIXTURE.read_bytes()])
    src._http_get = lambda url: basemap_mod._Response(True, next(bodies))  # type: ignore[method-assign]

    assert src.load_tile(14, 1, 1) is None  # answered "empty"
    assert src.load_tile(14, 2, 2) is None  # answered with junk
    assert list(cache.rglob("*.pbf")) == []
    layers = src.load_tile(14, 4843, 5861)  # answered with a real tile
    assert layers is not None
    assert (cache / "tiles" / "14" / "4843" / "5861.pbf").exists()


def test_basemap_source_keeps_a_tile_whose_layers_are_all_undrawn(tmp_path: Path) -> None:
    """Narrowing the decode must not make a good cached tile look blank and get pruned.

    A source told to decode a layer the tile doesn't carry sees no features at all. If
    that were read as "nothing here", the entry would be deleted and re-downloaded on
    every session — the cache paying for a rendering decision.
    """
    from meshterm.services.basemap import BasemapSource

    cache = tmp_path / "cache"
    tile = cache / "tiles" / "14" / "4843" / "5861.pbf"
    tile.parent.mkdir(parents=True)
    tile.write_bytes(_FIXTURE.read_bytes())

    src = BasemapSource(
        cache,
        tilejson_url="http://127.0.0.1:1/none",
        timeout=0.2,
        layers=frozenset({"a_layer_this_tile_does_not_have"}),
    )
    layers = src.load_tile(14, 4843, 5861)

    assert layers, "a real tile must still read as real"
    assert not any(layer.features for layer in layers)  # nothing was decoded
    assert tile.exists(), "the cache entry must survive"


def test_decoded_layers_survive_a_round_trip() -> None:
    """The sidecar encoding restores exactly what the decoder produced."""
    from meshterm.core.mvt import dumps_layers, loads_layers
    from meshterm.ui.map_render import DRAWN_LAYERS

    original = decode_tile(_FIXTURE.read_bytes(), layers=DRAWN_LAYERS)
    restored = loads_layers(dumps_layers(original, stamp="x"), stamp="x")

    assert restored is not None

    def shape(layers):
        return [(layer.name, layer.extent) for layer in layers]

    assert shape(restored) == shape(original)
    for before, after in zip(original, restored, strict=True):
        assert after.features == before.features


def test_decoded_layers_refuse_a_blob_from_another_layer_set() -> None:
    """A narrowed blob must never be served to a caller that wants more of the tile."""
    from meshterm.core.mvt import dumps_layers, loads_layers

    blob = dumps_layers(decode_tile(_FIXTURE.read_bytes(), layers={"water"}), stamp="water")
    assert loads_layers(blob, stamp="water") is not None
    assert loads_layers(blob, stamp="water,place") is None


def test_decoded_layers_treat_damage_as_a_miss() -> None:
    """A truncated or foreign sidecar is a cache miss, never an exception."""
    from meshterm.core.mvt import dumps_layers, loads_layers

    blob = dumps_layers(decode_tile(_FIXTURE.read_bytes(), layers={"water"}), stamp="s")
    assert loads_layers(blob[: len(blob) // 2], stamp="s") is None
    assert loads_layers(b"", stamp="s") is None
    assert loads_layers(b"not marshal at all", stamp="s") is None


def test_basemap_source_writes_and_reuses_a_decoded_sidecar(tmp_path: Path) -> None:
    """The second load parses nothing: it comes back from the sidecar."""
    cache = tmp_path / "cache"
    tile = cache / "tiles" / "14" / "4843" / "5861.pbf"
    tile.parent.mkdir(parents=True)
    tile.write_bytes(_FIXTURE.read_bytes())

    src = _offline_source(cache)
    first = src.load_tile(14, 4843, 5861)
    sidecar = cache / "decoded" / "14" / "4843" / "5861.bin"
    assert first is not None
    assert sidecar.exists(), "decoding should have been written down"

    # Break the raw tile: a second load that still works can only have used the sidecar.
    tile.write_bytes(b"junk that cannot decode")
    second = src.load_tile(14, 4843, 5861)
    assert second is not None
    assert [(layer.name, len(layer.features)) for layer in second] == [
        (layer.name, len(layer.features)) for layer in first
    ]


def test_basemap_source_ignores_a_sidecar_from_another_layer_set(tmp_path: Path) -> None:
    """Widening what the map draws re-decodes rather than serving the narrower blob."""
    from meshterm.services.basemap import BasemapSource

    cache = tmp_path / "cache"
    tile = cache / "tiles" / "14" / "4843" / "5861.pbf"
    tile.parent.mkdir(parents=True)
    tile.write_bytes(_FIXTURE.read_bytes())

    def source(layers):
        return BasemapSource(
            cache, tilejson_url="http://127.0.0.1:1/none", timeout=0.2, layers=layers
        )

    narrow = source(frozenset({"water"}))
    assert narrow.load_tile(14, 4843, 5861) is not None

    wider = source(frozenset({"water", "place"}))
    layers = {layer.name: layer for layer in wider.load_tile(14, 4843, 5861) or []}
    assert layers["place"].features, "the wider set must have been decoded afresh"


def test_basemap_source_prunes_the_decoded_cache_to_its_budget(tmp_path: Path) -> None:
    """The sidecar half of the cache is bounded; the raw tiles it derives from are not."""
    from meshterm.services.basemap import BasemapSource

    cache = tmp_path / "cache"
    decoded = cache / "decoded" / "14" / "1"
    decoded.mkdir(parents=True)
    for i in range(6):
        blob = decoded / f"{i}.bin"
        blob.write_bytes(b"x" * 1000)
        os.utime(blob, (i, i))  # oldest first

    # 6 KB present, 3 KB allowed: the three oldest go, the three newest stay.
    src = BasemapSource(cache, tilejson_url="http://127.0.0.1:1/none", max_decoded_bytes=3000)
    src._prune_decoded()

    left = sorted(p.stem for p in decoded.glob("*.bin"))
    assert left == ["3", "4", "5"], "the oldest sidecars go first"


def test_resident_bytes_tracks_what_a_decoded_tile_really_holds() -> None:
    """The memory budget is only as good as its scale, so the scale is checked against RAM.

    A count of tiles was the old measure, and a tile weighs anywhere from 1 to 4.6 MB on the
    PicoCalc; the estimate has to land near what ``tracemalloc`` sees, on this build's word
    size, or the budget it feeds is a guess.
    """
    import gc
    import tracemalloc

    from meshterm.core.mvt import dumps_layers, loads_layers, resident_bytes
    from meshterm.ui.map_render import DRAWN_LAYERS

    blob = dumps_layers(decode_tile(_FIXTURE.read_bytes(), layers=DRAWN_LAYERS))
    gc.collect()
    tracemalloc.start()
    try:
        layers = loads_layers(blob)
        held, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert layers
    assert 0.8 <= resident_bytes(layers) / held <= 1.4


def test_basemap_source_holds_decoded_tiles_to_a_byte_budget(tmp_path: Path) -> None:
    """Decoded tiles in RAM are budgeted in bytes: the oldest go first, the newest always stays."""
    from meshterm.core.mvt import resident_bytes
    from meshterm.services.basemap import BasemapSource

    tile = decode_tile(_FIXTURE.read_bytes())
    one = resident_bytes(tile)
    src = BasemapSource(
        tmp_path / "cache", tilejson_url="http://127.0.0.1:1/none", memo_bytes=one * 5 // 2
    )
    for i in range(5):
        src._remember((14, i, 0), tile)

    assert [key[1] for key in src._memo] == [3, 4], "the two newest fit; the rest went oldest first"
    assert src._memo_bytes == 2 * one
    assert src.resident(14, 4, 0) is tile
    assert src.resident(14, 0, 0) is None

    # A tile heavier than the whole budget is still kept, alone: a view asked for it.
    tight = BasemapSource(tmp_path / "cache", tilejson_url="http://127.0.0.1:1/none", memo_bytes=1)
    tight._remember((14, 0, 0), tile)
    tight._remember((14, 1, 0), tile)
    assert list(tight._memo) == [(14, 1, 0)]


def test_basemap_memory_budget_scales_with_the_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """An eighth of the RAM, within bounds — the PicoCalc's 100 MB gets ~13 MB of tiles."""
    from meshterm.services import basemap

    def machine(pages: int):
        sizes = {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": pages}
        monkeypatch.setattr(basemap.os, "sysconf", sizes.__getitem__, raising=False)

    machine(26142)  # the PicoCalc's 104568 kB
    assert basemap._memo_budget() == 4096 * 26142 // basemap._MEMO_SHARE
    machine(4 * 1024 * 1024)  # a 16 GB desktop
    assert basemap._memo_budget() == basemap._MEMO_CEILING
    machine(1024)  # 4 MB: too small to hold a view at an eighth
    assert basemap._memo_budget() == basemap._MEMO_FLOOR
    monkeypatch.delattr(basemap.os, "sysconf", raising=False)  # Windows
    assert basemap._memo_budget() == basemap._MEMO_CEILING


def test_basemap_warm_writes_a_tile_down_and_holds_nothing(tmp_path: Path) -> None:
    """A prefetch buys the decode, on disk, and leaves the RAM to where the reader has been."""
    cache = tmp_path / "cache"
    tile = cache / "tiles" / "14" / "4843" / "5861.pbf"
    tile.parent.mkdir(parents=True)
    tile.write_bytes(_FIXTURE.read_bytes())

    src = _offline_source(cache)
    src.warm(14, 4843, 5861)
    assert (cache / "decoded" / "14" / "4843" / "5861.bin").exists()
    assert src.resident(14, 4843, 5861) is None, "a guess was held in RAM"

    # A tile already written down costs a second guess nothing: not even a read.
    reads: list[Path] = []
    src._read_cached = lambda path: reads.append(path)  # type: ignore[method-assign]
    src.warm(14, 4843, 5861)
    assert reads == []


def test_basemap_source_remembers_a_blank_tile_for_the_session(tmp_path: Path) -> None:
    """An answered-empty tile isn't re-requested this session (but isn't written down)."""
    from meshterm.services import basemap as basemap_mod

    src = _offline_source(tmp_path / "cache")
    src._template = "http://tiles.invalid/{z}/{x}/{y}.pbf"
    src._resolved = True
    calls: list[str] = []

    def _get(url: str):
        calls.append(url)
        return basemap_mod._Response(True, b"")

    src._http_get = _get  # type: ignore[method-assign]
    assert src.load_tile(14, 1, 1) is None
    assert src.load_tile(14, 1, 1) is None
    assert len(calls) == 1


def test_basemap_http_get_separates_an_answer_from_silence(tmp_path: Path) -> None:
    """404 is an answer; a 500, and a body short of Content-Length, are not."""
    import urllib.error

    from meshterm.services.basemap import BasemapSource

    src = BasemapSource(tmp_path / "cache")

    class _Resp:
        """The slice of an ``http.client.HTTPResponse`` the fetcher touches."""

        def __init__(self, body: bytes, declared: str | None) -> None:
            self._body, self.headers = body, {"Content-Length": declared}

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> bool:
            return False

    def _fake(result):
        def _urlopen(_req, timeout=None, context=None):  # noqa: ANN001
            if isinstance(result, Exception):
                raise result
            return result

        return _urlopen

    def _get(result) -> tuple[bool, bytes]:
        import urllib.request

        saved = urllib.request.urlopen
        urllib.request.urlopen = _fake(result)  # type: ignore[assignment]
        try:
            return tuple(src._http_get("http://tiles.invalid/t"))
        finally:
            urllib.request.urlopen = saved  # type: ignore[assignment]

    err = lambda code: urllib.error.HTTPError("u", code, "", {}, None)  # noqa: E731
    assert _get(_Resp(b"tile-bytes", "10")) == (True, b"tile-bytes")
    assert _get(_Resp(b"tile-b", "10")) == (False, b"")  # truncated read
    assert _get(_Resp(b"anything", None)) == (True, b"anything")  # no declared length
    assert _get(err(404)) == (True, b"")  # "no tile here" — an answer
    assert _get(err(500)) == (False, b"")  # server fault — no answer
    assert _get(err(429)) == (False, b"")  # throttled — no answer
    assert _get(TimeoutError("timed out")) == (False, b"")


# -- the tool -----------------------------------------------------------------


@pytest.fixture()
def ctx(tmp_path: Path):
    """A mock-backed application context with the plain (console) UI surface."""
    from meshterm.context import AppContext
    from meshterm.core.admin_store import AdminStore
    from meshterm.core.config import Settings
    from meshterm.core.device_store import DeviceStore
    from meshterm.persistence.repository import Repository
    from meshterm.ui.theme import MESH_THEME

    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "map.db")
    context = AppContext(
        console=Console(file=io.StringIO(), theme=MESH_THEME),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    yield context
    context.repo.close()


def test_usable_fix_rejects_out_of_range_and_null_island() -> None:
    """A fix is usable only in valid lat/lon range and away from the 0/0 no-GPS sentinel."""
    from meshterm.core.geo import usable_fix

    assert usable_fix(45.5, -73.6) is True
    assert usable_fix(90.0, 180.0) is True and usable_fix(-90.0, -180.0) is True  # extremes
    assert usable_fix(0.0, 0.0) is False  # null island (a no-GPS companion reports this)
    assert usable_fix(-97.0, -1041.97) is False  # out of range (a real advert seen in the wild)
    assert usable_fix(45.0, 181.0) is False and usable_fix(-91.0, -73.0) is False


async def test_gather_markers_drops_out_of_range_fix(ctx) -> None:
    """A node advertising nonsense coordinates is never plotted, only the real one is.

    Some firmware reports an out-of-range fix (a companion has advertised ``lat -97,
    lon -1042``); projecting it flings the view off the world, leaving the map an all-black
    off-world frame. It is treated as no fix at all, the same guard the 0/0 null island gets.
    """
    from meshterm.services.markers import gather_markers

    run_id = ctx.repo.start_run("monitor", {})
    ctx.repo.record_observation(
        run_id,
        Observation(
            node="beefbeefbeef",
            name="BadFix",
            node_type=NODE_TYPE_REPEATER,
            lat=-97.0,
            lon=-1041.97,
            observed_at=utcnow(),
        ),
    )
    ctx.repo.record_observation(
        run_id,
        Observation(
            node="cafecafecafe",
            name="GoodFix",
            node_type=NODE_TYPE_REPEATER,
            lat=45.5,
            lon=-73.6,
            observed_at=utcnow(),
        ),
    )
    labels = {m.label for m in await gather_markers(ctx)}
    assert "GoodFix" in labels
    assert "BadFix" not in labels


def test_repository_round_trips_map_view(ctx) -> None:
    """The saved map viewport persists and reads back; absent by default."""
    assert ctx.repo.get_map_view() is None
    ctx.repo.set_map_view(45.51, -73.57, 13)
    lat, lon, zoom = ctx.repo.get_map_view()
    assert (lat, lon) == pytest.approx((45.51, -73.57))
    assert zoom == 13
    # A later save overwrites the single stored view.
    ctx.repo.set_map_view(40.0, -74.0, 9)
    assert ctx.repo.get_map_view() == pytest.approx((40.0, -74.0, 9))


async def test_map_tool_reports_nothing_to_plot(ctx, monkeypatch) -> None:
    """With no located contacts and no located history the tool returns cleanly."""

    async def _no_contacts(_ctx):
        return []

    monkeypatch.setattr("meshterm.services.markers._contacts", _no_contacts)
    result = await MapTool().run(ctx, {"static": True, "basemap": False})
    assert result.summary == {"located": 0}


async def test_gather_markers_drops_null_island_fixes(ctx, monkeypatch) -> None:
    """A node advertising a 0/0 fix (no GPS lock) is not plotted at null island.

    Framing such a marker (filter to it, press Enter) would drop the map onto the empty
    mid-Atlantic — a screen of solid water fill — so a both-near-zero fix is treated as no
    fix at all, exactly as our own node's marker already is.
    """
    from meshterm.core.models import Contact
    from meshterm.services.markers import gather_markers

    async def _contacts(_ctx):
        return [
            Contact(name="Real", public_key="aa" * 32, lat=45.50, lon=-73.57),
            Contact(name="NoFix", public_key="bb" * 32, lat=0.0, lon=0.0),
        ]

    monkeypatch.setattr("meshterm.services.markers._contacts", _contacts)
    labels = [m.label for m in await gather_markers(ctx)]
    assert "Real" in labels
    assert "NoFix" not in labels  # the null-island node is dropped


class _StubSession:
    """Minimal stand-in for :class:`TuiSession` for driving :class:`MapScreen`."""

    def __init__(self, cols: int, rows: int) -> None:
        self._cols, self._rows = cols, rows
        self.invalidated = 0

    def base_body_size(self) -> tuple[int, int]:
        return self._cols, self._rows

    def invalidate(self) -> None:
        self.invalidated += 1


class _StubSource:
    """An always-offline tile source, so the screen renders nodes only (no network)."""

    available = False
    max_zoom = 14

    def load_tile(self, z: int, x: int, y: int):
        return None

    def answered_empty(self, z: int, x: int, y: int) -> bool:
        return False  # offline is silence, never the source saying "nothing there"

    def resident(self, z: int, x: int, y: int):
        return None  # nothing is ever held in RAM

    def warm(self, z: int, x: int, y: int) -> None:
        return None


def _loaded_tile() -> list[Layer]:
    """A stand-in for a decoded tile: truthy, which is all the budget cares about."""
    return [Layer(name="water", extent=4096)]


def _map_screen_with_tiles(count: int) -> MapScreen:
    """A rendered map screen carrying ``count`` tiles' worth of pan history."""
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    screen = MapScreen(_StubSession(80, 24), [MapMarker("A", 45.5, -73.6)], _StubSource(), 14)
    screen.render_body(80)
    for i in range(count):
        screen._tiles[(14, 9000 + i, 9000 + i)] = _loaded_tile()
    return screen


def test_map_holds_only_the_tiles_its_view_shows() -> None:
    """Panning far leaves no decoded ground behind on the screen.

    A decoded tile is 1-4.6 MB on the PicoCalc, which has ~100 MB in total. The history a
    pan back needs is the source's, budgeted in bytes; a tile the screen kept as well would
    sit outside that budget, which is how a fast map once swapped the device to a standstill.
    """
    screen = _map_screen_with_tiles(200)
    for _ in range(12):
        screen.handle("right")
        screen.render_body(80)

    visible = set(screen._viewport.tiles(14))
    held = {k for k, layers in screen._tiles.items() if layers}
    assert held <= visible, f"{len(held - visible)} decoded tiles held off screen"


def test_map_tile_cache_never_drops_what_is_on_screen() -> None:
    """Whatever the view needs survives the trim — eviction starts at the far end."""
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    screen = MapScreen(_StubSession(80, 24), [MapMarker("A", 45.5, -73.6)], _StubSource(), 14)
    screen.render_body(80)
    visible = list(screen._viewport.tiles(14))
    for t in visible:
        screen._tiles[t] = _loaded_tile()
    for i in range(200):  # a long pan's worth of history arriving after them
        screen._tiles[(14, 9000 + i, 9000 + i)] = _loaded_tile()

    screen.render_body(80)

    for t in visible:
        assert screen._tiles.get(t), f"{t} is on screen and must not have been evicted"


def test_map_tile_cache_keeps_the_absent_tile_markers() -> None:
    """A ``None`` is the memory of having asked; it costs a slot, so it is never evicted.

    Dropping one would buy nothing back and cost a re-request on the very next repaint.
    """
    screen = _map_screen_with_tiles(200)
    for i in range(30):
        screen._tiles[(14, 100 + i, 100)] = None

    screen.render_body(80)

    absent = [k for k, v in screen._tiles.items() if v is None]
    assert len(absent) == 30, "absent-tile markers were evicted along with the geometry"


class _CapturingSession(_StubSession):
    """Drives :func:`open_map` headless, so a test can watch what it does.

    Captures the pushed screen, renders it once, then replays a canned key sequence —
    which is enough to assert on where the map opened and what it did (or didn't)
    persist as the view moves.
    """

    def __init__(self, cols: int, rows: int, keys: tuple[str, ...] = ()) -> None:
        super().__init__(cols, rows)
        self.keys = keys
        self.screen = None
        self.repainted = False

    async def run_screen(self, screen):  # noqa: ANN001, ANN201
        self.screen = screen
        screen.render_body(self._cols)
        for key in self.keys:
            screen.handle(key)
        return None

    def request_full_repaint(self) -> None:
        self.repainted = True


def test_map_screen_renders_pans_zooms_and_resets() -> None:
    """The interactive screen fills its body, and wasd/zoom/reset move the viewport."""
    from meshterm.core.geo import Viewport
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    session = _StubSession(80, 24)
    markers = [
        MapMarker("A", 45.50, -73.60, is_repeater=True),
        MapMarker("B", 45.40, -73.50),
    ]
    screen = MapScreen(session, markers, _StubSource(), 14)

    lines = screen.render_body(80)
    assert len(lines) == 24  # fills the body height reported by the session
    start = screen._viewport

    screen.handle("right")  # pan east → longitude increases
    assert screen._viewport.center_lon > start.center_lon
    screen.handle("up")  # pan north → latitude increases
    assert screen._viewport.center_lat > start.center_lat

    z = screen._viewport.zoom
    screen.handle("pageup")
    assert screen._viewport.zoom == z + 1
    screen.handle("pagedown")
    assert screen._viewport.zoom == z

    screen.handle("home")  # reset refits to the nodes
    refit = Viewport.fit([(m.lat, m.lon) for m in markers], start.dot_w, start.dot_h, max_zoom=14)
    assert screen._viewport.zoom == refit.zoom
    assert screen._viewport.center_lat == pytest.approx(refit.center_lat)

    # Letters never pan or zoom — they feed the find filter instead.
    view = screen._viewport
    screen.handle("text", "d")
    assert screen._viewport is view and screen._filter == "d"
    screen.handle("backspace")

    # Offline source: nodes still render and both markers are present.
    assert "▲" in _plain(screen.render_body(80)) and "●" in _plain(screen.render_body(80))
    # The basemap's state is a status atom, so it rides the title's `·` chain (standing in
    # for the scale) and leaves the footer a pure key line inside its 72-cell budget.
    assert screen.title.endswith("· offline")
    assert "offline" not in screen.footer_hint
    assert cell_len(screen.footer_hint) <= 72


def test_map_screen_shift_pans_by_a_single_cell() -> None:
    """Holding Shift with an arrow pans finely, by one character cell."""
    from meshterm.core.geo import lonlat_to_world, world_to_lonlat
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    markers = [MapMarker("A", 45.50, -73.60), MapMarker("B", 45.40, -73.50)]
    screen = MapScreen(_StubSession(80, 24), markers, _StubSource(), 14)
    screen.render_body(80)
    start = screen._viewport

    # A coarse east pan (30% of the view) moves much further than a fine one.
    screen.handle("right")
    coarse = screen._viewport.center_lon - start.center_lon

    screen._viewport = start
    screen.handle("shift_right")
    fine = screen._viewport.center_lon - start.center_lon
    assert 0 < fine < coarse

    # The fine step is exactly one cell (2 dots) east at the current zoom.
    cx, cy = lonlat_to_world(start.center_lat, start.center_lon, start.zoom)
    _, expected_lon = world_to_lonlat(cx + 2, cy, start.zoom)
    assert screen._viewport.center_lon == pytest.approx(expected_lon)


def test_map_fkey_lane_names_its_three_destinations_and_the_zoom() -> None:
    """The map repurposes the nav keys, so its lane labels them — and offers no dead key."""
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen
    from meshterm.ui.tui.fkeys import PICOCALC_LYRA_DECK

    screen = MapScreen(_StubSession(80, 24), [MapMarker("A", 45.5, -73.6)], _StubSource(), 14)
    lane = screen.picocalc_lyra_lane

    # PgUp/PgDn zoom here and Home reframes — the shared paging labels would all be lies,
    # and "end" is bound to nothing at all, so no jump rides the zoom rocker's Shift bank.
    # The zoom pair rises to the right, like every right-hand directional pair.
    assert [pair.label if pair else None for pair in lane] == [
        "Region",
        "You",
        "Frame",
        "Zoom -",
        "Zoom +",
    ]
    assert (
        PICOCALC_LYRA_DECK.action_for(lane, 1) == "home"
        and PICOCALC_LYRA_DECK.action_for(lane, 5) == "pageup"
    )
    assert (
        PICOCALC_LYRA_DECK.action_for(lane, 2) == "locate"
        and PICOCALC_LYRA_DECK.action_for(lane, 3) == "frame"
    )
    # Two slots carry a Shift half along their own axis: You + homes in on us zoomed,
    # Clear drops the find query. The other three Shift keys stay unbound.
    assert (
        PICOCALC_LYRA_DECK.action_for(lane, 7) == "locate_zoom"
        and PICOCALC_LYRA_DECK.action_for(lane, 8) == "clear_find"
    )
    assert all(PICOCALC_LYRA_DECK.action_for(lane, n) is None for n in (6, 9, 10))
    # Region and the zoom always act; the other two only where they'd land somewhere —
    # and each Shift half gates with its own axis (no self marker, no query typed).
    assert [pair.enabled for pair in lane] == [True, False, False, True, True]
    assert lane[1].opp_enabled is False and lane[2].opp_enabled is False


def test_map_locate_recenters_on_our_own_node_at_the_current_zoom() -> None:
    """``You`` moves the view to us and changes nothing else — not even the zoom."""
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    markers = [
        MapMarker("Hilltop-Repeater", 45.53, -73.71, is_repeater=True),
        MapMarker("Homestead", 45.40, -73.50, is_self=True),
    ]
    screen = MapScreen(_StubSession(80, 24), markers, _StubSource(), 14)
    screen.render_body(80)
    screen.handle("pageup")  # zoom in one step, so the recentre has a zoom to preserve
    zoom = screen._viewport.zoom
    screen.handle("left")  # and pan away from wherever we are

    assert screen.picocalc_lyra_lane[1].enabled is True  # we're on the map, so You can act
    screen.handle("locate")
    assert screen._viewport.center_lat == pytest.approx(45.40)
    assert screen._viewport.center_lon == pytest.approx(-73.50)
    assert screen._viewport.zoom == zoom

    # A mesh we aren't located in has nowhere to go: the chip dims and the key no-ops.
    away = MapScreen(_StubSession(80, 24), markers[:1], _StubSource(), 14)
    away.render_body(80)
    before = away._viewport
    away.handle("locate")
    assert away.picocalc_lyra_lane[1].enabled is False and away._viewport is before


def test_map_echoes_the_find_query_in_the_body_only_where_the_footer_is_gone() -> None:
    """The PicoCalc draws no hint line, so the query it carries moves into the body."""
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
    from meshterm.ui.attribution import CREDIT_SHORT
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    markers = [MapMarker("Hilltop-Repeater", 45.53, -73.71, is_repeater=True)]

    try:
        set_platform(REGULAR)
        desktop = MapScreen(_StubSession(72, 20), markers, _StubSource(), 14)
        assert len(desktop.render_body(72)) == 20
        for ch in "hil":
            desktop.handle("text", ch)
        # Unchanged: the footer already shows it, so the canvas keeps the whole body.
        assert "/hil" not in _plain(desktop.render_body(72))
        assert len(desktop.render_body(72)) == 20
        assert "find: hil" in desktop.footer_hint

        set_platform(PICOCALC_LYRA)
        device = MapScreen(_StubSession(53, 23), markers, _StubSource(), 14)
        assert len(device.render_body(53)) == 23
        for ch in "hil":
            device.handle("text", ch)
        lines = device.render_body(53)
        # The echo overlays the canvas's *last* row — directly above the F-key lane —
        # so the ground never shifts: same body height, same viewport, top anchor held.
        # The basemap credit is pinned to that same row's other end (it is chrome on the
        # drawing, not a body line of its own), so the echo takes what is left of it.
        assert _plain(lines[-1]).strip().startswith("/hil")
        assert _plain(lines[-1]).rstrip().endswith(CREDIT_SHORT)
        assert "/hil" not in _plain(lines[0])
        assert len(lines) == 23
        # Clearing the find hands the row back to the canvas, still without a reflow.
        for _ in "hil":
            device.handle("backspace")
        assert len(device.render_body(53)) == 23
        assert "/" not in _plain(device.render_body(53))
    finally:
        set_platform(REGULAR)


def test_map_shifted_vertical_arrows_survive_the_console_keymap(monkeypatch) -> None:
    """Shifted vertical arrows survive the console keymap flattening them.

    Shift+↑ arrives as PgUp on the PicoCalc console; with Shift physically down it
    must fine-pan, not zoom — and plain PgUp (the F-lane's zoom) stays a zoom.
    """
    from meshterm.services import modifier_watch
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    screen = MapScreen(_StubSession(80, 24), [MapMarker("A", 45.5, -73.6)], _StubSource(), 14)
    screen.render_body(80)
    zoom = screen._viewport.zoom
    lat = screen._viewport.center_lat

    monkeypatch.setattr(modifier_watch, "_shift_down", True)
    screen.handle("pageup")  # the keymap's Shift+↑, rescued by the watcher
    assert screen._viewport.zoom == zoom  # no zoom happened
    assert screen._viewport.center_lat > lat  # …the view nudged north instead

    monkeypatch.setattr(modifier_watch, "_shift_down", False)
    screen.handle("pageup")  # a real zoom press (the F5 chip's action)
    assert screen._viewport.zoom == zoom + 1


def test_map_frame_chip_lights_only_with_matches_to_frame() -> None:
    """``Frame`` is the find's Enter under another name — dim, and inert, without a query."""
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    markers = [
        MapMarker("Hilltop-Repeater", 45.53, -73.71, is_repeater=True),
        MapMarker("Alice", 45.40, -73.50),
    ]
    screen = MapScreen(_StubSession(80, 24), markers, _StubSource(), 14)
    screen.render_body(80)
    before = screen._viewport
    screen.handle("frame")  # no query typed: nothing to frame, so nothing moves
    assert screen.picocalc_lyra_lane[2].enabled is False and screen._viewport is before

    for ch in "ali":
        screen.handle("text", ch)
    assert screen.picocalc_lyra_lane[2].enabled is True
    screen.handle("frame")
    assert screen._viewport.center_lat == pytest.approx(45.40)

    # A query nothing matches has no extent either — the chip goes back to dim.
    for ch in "zzz":
        screen.handle("text", ch)
    assert screen._matches() == [] and screen.picocalc_lyra_lane[2].enabled is False


def test_map_screen_find_filters_frames_and_clears() -> None:
    """Typing builds the query; ^Enter frames matches; Enter/Esc drop it; Esc then leaves."""
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    markers = [
        MapMarker("Hilltop-Repeater", 45.53, -73.71, is_repeater=True),
        MapMarker("Alice", 45.40, -73.50),
        MapMarker("Yagi-North", 45.60, -73.65),
    ]
    screen = MapScreen(_StubSession(80, 24), markers, _StubSource(), 14)
    screen.render_body(80)

    for ch in "hi":
        screen.handle("text", ch)
    assert screen._filter == "hi"
    assert [m.label for m in screen._matches()] == ["Hilltop-Repeater"]
    screen.render_body(80)
    assert "1 of 3 match" in screen.title
    assert "find: hi" in screen.footer_hint

    # Only the match keeps a label; the others dim to bare context glyphs.
    body = _plain(screen.render_body(80))
    assert "Hilltop-Repeater" in body
    assert "Alice" not in body and "Yagi-North" not in body

    # ^Enter frames the matches: the view centres on the repeater, and the query survives.
    screen.handle("ctrl_enter")
    assert screen._viewport.center_lat == pytest.approx(45.53, abs=0.05)
    assert screen._viewport.center_lon == pytest.approx(-73.71, abs=0.05)
    assert screen._filter == "hi"

    # Plain Enter is the other way out: the query goes, the view stays exactly where the
    # frame left it.
    centre = (screen._viewport.center_lat, screen._viewport.center_lon)
    screen.handle("enter")
    assert screen._filter == ""
    assert (screen._viewport.center_lat, screen._viewport.center_lon) == centre

    # Backspace edits; Esc clears the filter first and only then dismisses.
    for ch in "hi":
        screen.handle("text", ch)
    screen.handle("backspace")
    assert screen._filter == "h"

    async def drive() -> object:
        screen.future = asyncio.get_running_loop().create_future()
        screen.handle("escape")
        assert screen._filter == "" and not screen.future.done()
        screen.handle("escape")
        return await screen.future

    assert asyncio.run(drive()) is None


def test_map_screen_frame_zooms_in_on_a_single_match() -> None:
    """^Enter on a lone filtered node homes in close on it, not the fit's neutral default."""
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import _FIND_ZOOM, MapScreen

    markers = [
        MapMarker("Hilltop-Repeater", 45.53, -73.71, is_repeater=True),
        MapMarker("Alice", 45.40, -73.50),
    ]
    screen = MapScreen(_StubSession(80, 24), markers, _StubSource(), 19)  # source max 19
    screen.render_body(80)
    for ch in "hil":
        screen.handle("text", ch)
    assert [m.label for m in screen._matches()] == ["Hilltop-Repeater"]
    screen.handle("ctrl_enter")
    assert screen._viewport.center_lat == pytest.approx(45.53, abs=0.01)
    assert screen._viewport.center_lon == pytest.approx(-73.71, abs=0.01)
    assert screen._viewport.zoom == _FIND_ZOOM  # zoomed in, not the fit's default 14


def test_map_screen_restores_and_persists_view() -> None:
    """The screen reopens on a saved view and reports every centre/zoom change."""
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    saved: list[tuple[float, float, int]] = []
    markers = [MapMarker("A", 45.50, -73.60), MapMarker("B", 45.40, -73.50)]
    screen = MapScreen(
        _StubSession(80, 24),
        markers,
        _StubSource(),
        14,
        saved_view=(46.80, -71.20, 12),
        on_view_change=lambda vp: saved.append((vp.center_lat, vp.center_lon, vp.zoom)),
    )

    screen.render_body(80)
    # Restored to the saved centre/zoom, not a fit of the markers.
    assert screen._viewport.zoom == 12
    assert screen._viewport.center_lat == pytest.approx(46.80)
    assert screen._viewport.center_lon == pytest.approx(-71.20)
    assert saved == []  # reopening unchanged doesn't rewrite the stored view

    screen.handle("right")  # pan east persists the moved view
    assert saved and saved[-1][2] == 12  # zoom unchanged
    assert saved[-1][1] > -71.20  # centre moved east

    screen.handle("pageup")  # zooming in persists too
    assert saved[-1][2] == 13


async def test_open_map_focus_centres_on_the_node_without_clobbering_the_saved_view(
    ctx, monkeypatch
) -> None:
    """A focused map centres on its node without clobbering the saved view.

    Opening the full map from a node's detail page centres there at the inline
    preview's zoom and, being a transient peek, never overwrites the persisted "where
    you left the map" global view — even as the peek is panned and zoomed.
    """
    from meshterm.core.geo import clamp_lat
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import open_map
    from meshterm.ui.surface import TuiUi

    monkeypatch.setattr("meshterm.ui.map_screen.basemap_source", lambda _c: _StubSource())
    ctx.repo.set_map_view(10.0, 20.0, 8)  # where the user last left the global map

    session = _CapturingSession(80, 24, keys=("right", "pageup"))  # pan + zoom the peek
    ctx.ui = TuiUi(session)
    await open_map(
        ctx,
        [MapMarker("Hub", 45.5, -73.6, is_repeater=True)],
        focus=(45.5, -73.6),
        find="Hub",
    )

    # Opened on the node, at the detail preview's zoom (min(13, max_zoom)).
    assert session.screen._saved_view == (clamp_lat(45.5), -73.6, 13)
    assert session.screen._on_view_change is None  # a focused peek wires no persistence
    assert session.screen._filter == "Hub"  # the find opens seeded to the focused node
    # Panning/zooming the peek left the global 'where you left the map' view untouched.
    assert ctx.repo.get_map_view() == pytest.approx((10.0, 20.0, 8))
    assert session.repainted  # cleaned the terminal on the way out, like the plain open


async def test_open_map_without_focus_restores_and_persists_the_global_view(
    ctx, monkeypatch
) -> None:
    """With no focus, the full map restores and persists the global view.

    It reopens where the user left it, and pans persist straight back to that view.
    """
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import open_map
    from meshterm.ui.surface import TuiUi

    monkeypatch.setattr("meshterm.ui.map_screen.basemap_source", lambda _c: _StubSource())
    ctx.repo.set_map_view(46.80, -71.20, 12)  # the saved view to reopen on

    session = _CapturingSession(80, 24, keys=("right",))  # pan east
    ctx.ui = TuiUi(session)
    await open_map(ctx, [MapMarker("Hub", 46.8, -71.2)])

    assert session.screen._viewport.zoom == 12  # reopened on the saved view, not a node fit
    lat, lon, zoom = ctx.repo.get_map_view()
    assert zoom == 12 and lon > -71.20  # the eastward pan persisted back to the global view


def test_map_screen_seeded_find_behaves_like_a_typed_one() -> None:
    """A seeded find matches, titles, and clears exactly as if the user had typed it."""
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    markers = [MapMarker("Hub", 45.5, -73.6), MapMarker("Far", 45.6, -73.5)]
    screen = MapScreen(_StubSession(80, 24), markers, _StubSource(), 14, find="Hub")
    assert [m.label for m in screen._matches()] == ["Hub"]  # only the seed's node lights
    assert "find: Hub" in screen.footer_hint  # the query shows, editable, as ever
    screen.handle("escape")  # first Esc clears the find (not the map)
    assert screen._filter == "" and len(screen._matches()) == 2


def test_map_screen_scrubs_right_edge_after_move() -> None:
    """A move asks the session to scrub the right edge once, to clear any braille smear."""
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    markers = [MapMarker("A", 45.5, -73.6)]
    braille = MapScreen(_StubSession(80, 24), markers, _StubSource(), 14)
    braille.render_body(80)
    braille.handle("right")  # a move flags the edge for a scrub
    assert braille.consume_edge_scrub() == 1  # the border column: the map is flush
    assert braille.consume_edge_scrub() == 0  # consumed — not repeated without another move


def test_map_screen_escape_dismisses() -> None:
    """Esc resolves the screen's future with None (backs out to the menu)."""
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    screen = MapScreen(_StubSession(80, 24), [MapMarker("A", 45.5, -73.6)], _StubSource(), 14)

    async def drive() -> object:
        screen.future = asyncio.get_running_loop().create_future()
        screen.handle("escape")
        return await screen.future

    assert asyncio.run(drive()) is None


def test_map_observation_round_trips_node_type(ctx) -> None:
    """A stored observation's advert type survives persistence and aggregation."""
    run_id = ctx.repo.start_run("monitor", {})
    ctx.repo.record_observation(
        run_id,
        Observation(
            node="a1", name="Yagi", node_type=NODE_TYPE_REPEATER, snr=6.0, lat=45.5, lon=-73.5
        ),
    )
    node = next(n for n in ctx.repo.heard_nodes() if n.node == "a1")
    assert node.is_repeater and node.node_type == NODE_TYPE_REPEATER


def test_render_map_labels_take_the_name_hue_ours_white() -> None:
    """Map labels take the node hue, and ours takes the you white.

    Our own label is white while the ★ glyph stays yellow, and a keyless label lands
    on the muted grey.
    """
    from meshterm.ui.map_render import MapMarker, render_map
    from meshterm.ui.marks import SELF_MARK
    from meshterm.ui.widgets import name_rgb

    vp = Viewport.fit([(45.5, -73.6), (45.4, -73.5)], 120, 80, max_zoom=14)
    markers = [
        MapMarker("US", 45.5, -73.6, is_self=True, key="cc" * 32),
        MapMarker("KEYED", 45.4, -73.5, key="d4" * 32),
        MapMarker("BARE", 45.45, -73.55),
    ]
    lines = render_map(vp, {}, markers)
    assert _glyph_color(lines, "★") == parse_hex(SELF_MARK[1])  # the glyph keeps its yellow
    assert _glyph_color(lines, "US") == (255, 255, 255)  # ...the label goes you-white
    assert _glyph_color(lines, "KEYED") == name_rgb("KEYED", "d4" * 32)
    assert _glyph_color(lines, "BARE") == (148, 163, 184)  # no key, the muted grey


def test_render_map_find_matches_still_label_white() -> None:
    """An active find filter keeps forcing matching labels to full white."""
    from meshterm.ui.map_render import MapMarker, render_map

    vp = Viewport.fit([(45.5, -73.6), (45.4, -73.5)], 120, 80, max_zoom=14)
    markers = [MapMarker("TARGET", 45.5, -73.6, key="d4" * 32)]
    lines = render_map(vp, {}, markers, find="targ")
    assert _glyph_color(lines, "TARGET") == (255, 255, 255)


# --- a tile nobody answered about is asked for again ----------------------------------


def _map_over(source) -> MapScreen:  # noqa: ANN001
    """A map screen fetching from ``source``, with an event loop it can hand work to."""
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    return MapScreen(_StubSession(80, 24), [MapMarker("A", 45.5, -73.6)], source, 14)


async def _drawn(screen) -> None:  # noqa: ANN001
    """Paint until the view's tiles have answered and its raster has landed.

    Real sleeps, not ``sleep(0)``: a tile load goes through a thread. And no raster is
    asked for while a tile of the view is on its way, so a test of the raster has to let
    the loads finish first — the stub sources answer at once, with silence.
    """
    for _ in range(400):
        screen.render_body(80)
        if (
            not screen._pending
            and screen._drawing is None
            and screen._frame_key == screen._ground_key(screen._viewport)
        ):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("the view's ground never landed")


class _SilentSource(_StubSource):
    """A source that never answers — the flaky Wi-Fi, not an empty planet."""

    def __init__(self) -> None:
        self.asked: list[tuple[int, int, int]] = []

    def load_tile(self, z: int, x: int, y: int):
        self.asked.append((z, x, y))
        return None


class _EmptySource(_SilentSource):
    """A source that answers, and the answer is that there is nothing at those tiles."""

    def answered_empty(self, z: int, x: int, y: int) -> bool:
        return True


def test_map_asks_again_for_a_tile_it_got_no_answer_about(monkeypatch) -> None:  # noqa: ANN001
    """One Wi-Fi blip must not black out that ground until the app is restarted.

    On the PicoCalc a missing z14 tile *is* the picture at the two highest zooms, where
    one tile covers the whole screen many times over (JP, 2026-08-18).
    """
    from meshterm.ui import map_screen as ms

    source = _SilentSource()
    screen = _map_over(source)

    async def drive() -> None:
        screen.render_body(80)
        while screen._pending:
            await asyncio.sleep(0)
        first = len(source.asked)
        assert first, "no tile was ever asked for"

        screen.render_body(80)  # still inside the cooldown
        await asyncio.sleep(0)
        assert len(source.asked) == first, "the source is being hammered"

        monkeypatch.setattr(ms, "monotonic", lambda: monotonic() + _TILE_RETRY + 1)
        screen.render_body(80)
        while screen._pending:
            await asyncio.sleep(0)
        assert len(source.asked) > first, "the unanswered tile was written off for good"

    asyncio.run(drive())


def test_map_takes_the_sources_word_that_a_tile_is_empty(monkeypatch) -> None:  # noqa: ANN001
    """An answer settles it: ocean tiles must not be re-requested for ever."""
    from meshterm.ui import map_screen as ms

    source = _EmptySource()
    screen = _map_over(source)

    async def drive() -> None:
        screen.render_body(80)
        while screen._pending:
            await asyncio.sleep(0)
        asked = len(source.asked)

        monkeypatch.setattr(ms, "monotonic", lambda: monotonic() + _TILE_RETRY * 10)
        screen.render_body(80)
        await asyncio.sleep(0)
        assert len(source.asked) == asked, "a settled answer was asked about again"

    asyncio.run(drive())


def test_map_fetches_before_the_source_has_resolved(monkeypatch) -> None:  # noqa: ANN001
    """``available`` says "not yet", never "don't bother" — a fetch is what resolves it."""
    source = _SilentSource()
    assert source.available is False  # nothing has reached the network at open time
    screen = _map_over(source)

    async def drive() -> None:
        screen.render_body(80)
        while screen._pending:
            await asyncio.sleep(0)
        assert source.asked, "the map wrote the source off instead of asking it"

    asyncio.run(drive())


# --- anticipating the next move ------------------------------------------------------


class _CountingSource(_StubSource):
    """A source that answers instantly and remembers everything it was asked for."""

    available = True

    def __init__(self) -> None:
        self.asked: list[tuple[int, int, int]] = []

    def load_tile(self, z: int, x: int, y: int):
        self.asked.append((z, x, y))
        return _loaded_tile()

    def warm(self, z: int, x: int, y: int) -> None:
        self.asked.append((z, x, y))  # a guess is an ask too, just one that holds nothing


def _settled_map(source, zoom: int = 13):
    """A map screen with its own view drawn and every visible tile in hand."""
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    screen = MapScreen(
        _StubSession(80, 24),
        [MapMarker("A", 45.5, -73.6)],
        source,
        14,
        saved_view=(45.5, -73.6, zoom),
    )
    return screen


async def _quiet(screen, source) -> list:
    """Paint until the prefetcher runs dry, and report what it went and got.

    Real sleeps, not ``sleep(0)``: both the raster and every tile load go through
    ``asyncio.to_thread``, and a thread does not finish on a bare loop turn.
    """
    for _ in range(200):
        screen.render_body(80)
        screen._settled_at = 0.0  # the settle wait has its own test; don't sleep it out
        await asyncio.sleep(0.005)
        if (
            not screen._pending
            and not screen._speculating
            and screen._drawing is None
            and screen._next_speculation(screen._viewport) is None
        ):
            break
    visible = set(screen._viewport.tiles(14))
    return [t for t in source.asked if t not in visible]


def test_map_fetches_ahead_of_the_way_it_is_being_panned() -> None:
    """Two steps one way is a heading, and ground is bought in front of it."""
    source = _CountingSource()
    screen = _settled_map(source)

    async def drive() -> list:
        await _quiet(screen, source)
        source.asked.clear()
        screen._speculated.clear()
        for _ in range(2):  # two steps east: a heading, not a nudge
            screen.handle("right")
        return await _quiet(screen, source)

    ahead = asyncio.run(drive())
    vp = screen._viewport
    east = set(vp.panned(_PAN_STEP, 0).tiles(14)) - set(vp.tiles(14))
    west = set(vp.panned(-_PAN_STEP, 0).tiles(14)) - set(vp.tiles(14))
    assert east, "the fixture leaves no tile to the east to fetch"
    assert east <= set(ahead), "the ground ahead of the pan was not fetched"
    if west & set(ahead):  # a hedge is allowed, but it comes after the heading
        assert ahead.index(next(iter(east))) < ahead.index(next(iter(west & set(ahead))))


def test_map_fetches_the_step_out_it_has_no_tiles_for() -> None:
    """Zooming out is the one move whose tiles were never on screen."""
    source = _CountingSource()
    screen = _settled_map(source)

    async def drive() -> list:
        return await _quiet(screen, source)

    ahead = asyncio.run(drive())
    out = set(screen._viewport.zoomed(-1).tiles(14))
    assert out <= set(ahead), "the view one step out was not anticipated"


def test_map_does_not_fetch_ahead_while_the_view_is_still_arriving() -> None:
    """Speculation must never be the thing in the way of the view being looked at."""
    source = _CountingSource()
    screen = _settled_map(source)

    async def drive() -> None:
        screen.render_body(80)  # visible tiles now pending, nothing drawn
        assert screen._pending, "the fixture settled too early to test this"
        screen._ensure_prefetch(screen._viewport)
        assert not screen._speculating, "fetched ahead while the visible tiles were in flight"

        screen._pending.clear()
        screen._drawing = ("busy",)
        screen._ensure_prefetch(screen._viewport)
        assert not screen._speculating, "fetched ahead while the ground was being drawn"

    asyncio.run(drive())


def test_map_waits_for_the_view_to_settle_before_it_guesses() -> None:
    """A raster landing between two keypresses of a pan is a gap, not a pause.

    Starting a fetch there puts a tile decode in front of the next frame — measured at
    ~110 ms on the device, which is exactly what the prefetcher must never cost.
    """
    source = _CountingSource()
    screen = _settled_map(source)

    async def drive() -> None:
        screen.render_body(80)  # builds the viewport
        screen._pending.clear()  # ...and here it is, arrived and drawn this instant
        screen._drawing = screen._wanted = None

        screen._ensure_prefetch(screen._viewport)  # stamps the view as newly arrived
        assert not screen._speculating, "guessed while the view was still moving"

        screen._settled_at = 0.0  # ...and now it has been still for a while
        screen._ensure_prefetch(screen._viewport)
        assert screen._speculating, "never got round to guessing at all"

    asyncio.run(drive())


def test_map_forgets_its_heading_when_the_reader_reframes() -> None:
    """A zoom or a jump home is looking around, not travelling on."""
    screen = _settled_map(_CountingSource())
    screen.render_body(80)
    screen.handle("right")
    screen.handle("right")  # two coarse steps east
    assert screen._heading == "right" and screen._momentum == 2

    screen.handle("pagedown")  # a zoom out
    assert screen._heading is None and screen._momentum == 0
    plan = screen._prefetch_plan(screen._viewport)
    vp = screen._viewport
    for direction, (dx, dy) in _PAN_DIRS.items():
        stepped = set(vp.panned(dx * _PAN_STEP, dy * _PAN_STEP).tiles(14))
        assert stepped - set(vp.tiles(14)) <= set(plan) or not (stepped - set(vp.tiles(14))), (
            f"{direction} was not hedged once the heading was gone"
        )


def test_map_does_not_guess_in_the_paint_that_queues_a_raster() -> None:
    """The pacing rule is read at the end of a paint, not off the last one's answer.

    A find keystroke moves the view nowhere, so this is still the settled view the
    prefetcher is entitled to guess from — but the paint it triggers queues the raster
    the reader is waiting for. Asked a step earlier, from inside ``_ensure_tiles``, the
    prefetcher saw the *previous* paint's quiet and started a tile decode alongside it.
    """
    source = _CountingSource()
    screen = _settled_map(source)

    async def drive() -> None:
        await _quiet(screen, source)
        screen._speculated.clear()
        screen._settled_at = 0.0  # settled, quiet, and entitled to guess

        screen.handle("text", "a")
        screen.render_body(80)
        assert screen._drawing is not None, "the fixture drew nothing to be in the way of"
        assert not screen._speculating, "guessed alongside the raster the reader waits for"

    asyncio.run(drive())


def test_map_leaves_a_cooling_tile_alone_when_it_guesses() -> None:
    """A source that has just gone quiet is the last one a guess should be poking."""
    screen = _settled_map(_CountingSource())
    screen.render_body(80)  # builds the viewport
    vp = screen._viewport
    plan = screen._prefetch_plan(vp)
    assert plan, "the fixture had nothing to guess at"

    screen._unanswered = {t: monotonic() + 20.0 for t in plan}
    assert screen._next_speculation(vp) is None, "guessed at a tile serving out its silence"

    screen._unanswered.clear()  # the cooldown expires and the plan is a plan again
    assert screen._next_speculation(vp) in plan


def test_map_keeps_no_speculated_tile_of_its_own() -> None:
    """A prefetch warms the source's cache; the screen holds only what it draws."""
    source = _CountingSource()
    screen = _settled_map(source)

    async def drive() -> None:
        await _quiet(screen, source)

    asyncio.run(drive())
    visible = set(screen._viewport.tiles(14))
    assert set(screen._tiles) <= visible, "speculated tiles landed in the view's working set"
    assert screen._speculated, "nothing was speculated at all"


def test_map_takes_a_tile_already_in_memory_without_a_round_trip() -> None:
    """Ground panned back onto is in the very first raster, not a thread hop later."""

    class _HeldSource(_CountingSource):
        def resident(self, z: int, x: int, y: int):
            return _loaded_tile()

    source = _HeldSource()

    async def drive():
        screen = _settled_map(source)
        screen.render_body(80)
        return screen

    screen = asyncio.run(drive())
    visible = screen._viewport.tiles(14)
    assert all(screen._tiles.get(t) for t in visible), "a held tile was not taken"
    assert not screen._pending and not source.asked, "a held tile was sent to a thread"


def test_map_drops_a_tile_the_view_left_before_its_turn() -> None:
    """A fast pan asks for every view it passes; only where it stopped is worth loading.

    Loading the rest — a decode of a second or more each, on the PicoCalc — was the backlog
    that kept the map behind the keys long after they stopped.
    """
    source = _CountingSource()

    async def drive():
        screen = _settled_map(source)
        screen.render_body(80)  # asks for the opening view's tiles
        opening = set(screen._pending)
        for _ in range(8):  # gone, before any load has had its turn
            screen.handle("right")
        await asyncio.sleep(0.05)
        return screen, opening

    screen, opening = asyncio.run(drive())
    left_behind = opening - set(screen._viewport.tiles(14))
    assert left_behind, "the pan never left the opening view"
    assert not left_behind & set(source.asked), "a tile the view had left was loaded anyway"
    assert not left_behind & screen._pending, "a dropped tile is still counted as on its way"


# --- the coarse first pass -----------------------------------------------------------


def test_coarse_ground_keeps_the_shape_and_drops_the_detail() -> None:
    """The quick pass draws water, parks and through-roads — no buildings, no text."""
    from meshterm.ui.map_render import MapMarker, render_ground

    vp = Viewport(45.5019, -73.5674, 15, 160, 96)
    tiles = {(14, 4843, 5861): decode_tile(_FIXTURE.read_bytes())}
    markers = [MapMarker("Yagi", 45.5019, -73.5674)]

    coarse, _ = render_ground(vp, tiles, markers, coarse=True)
    full, _ = render_ground(vp, tiles, markers)

    def ink(lines):
        return sum(1 for ch in _plain(lines) if ch.strip())

    assert ink(coarse), "the coarse pass drew nothing at all"
    assert ink(coarse) < ink(full), "the coarse pass drew as much as the full one"
    # Our own marker label is overlaid on every paint; the basemap's text is not.
    assert "Yagi" in _plain(coarse)
    assert not (_street_names(full) & _street_names(coarse)), "text came with the quick pass"


def _street_names(lines: list[str]) -> set:
    """Words in a frame that are basemap text rather than a node marker's label."""
    return {w for w in re.findall(r"[A-Za-zÀ-ÿ]{4,}", _plain(lines))} - {"Yagi"}


def test_map_draws_a_coarse_frame_before_the_finished_one(monkeypatch) -> None:  # noqa: ANN001
    """A view with no ground under it must not sit black for a whole raster.

    A coarse pan step is 30% of the screen, so four keypresses leave nothing of the last
    frame to reproject — and a full raster is most of a second on the PicoCalc (JP,
    2026-08-18).
    """
    from meshterm.ui import map_screen as ms

    passes: list = []
    monkeypatch.setattr(
        ms,
        "render_ground",
        lambda vp, tiles, m, **k: passes.append(k.get("coarse", False)) or (["x"], None),
    )
    screen = _map_over(_StubSource())

    async def drive() -> None:
        screen.render_body(80)  # builds the viewport, and asks for its own first draw
        while screen._drawing is not None:
            await asyncio.sleep(0)
        passes.clear()
        await screen._draw_ground(("k",), screen._viewport, {}, [], "", True)

    asyncio.run(drive())
    assert passes == [True, False], "the coarse pass did not come first, or at all"
    assert screen._frame_coarse is False, "the frame was left owing its detail"


def test_map_abandons_the_finished_pass_for_a_view_that_moved(monkeypatch) -> None:  # noqa: ANN001
    """Between the passes is where a held key gets off: don't finish a stale frame."""
    from meshterm.ui import map_screen as ms

    screen = _map_over(_StubSource())
    passes: list = []

    def render(vp, tiles, m, **k):
        coarse = k.get("coarse", False)
        passes.append(coarse)
        if coarse and len(passes) == 1:
            # The user pans on while the coarse pass is being drawn.
            screen._wanted = (("newer",), screen._viewport, [], "")
        return ["x"], None

    monkeypatch.setattr(ms, "render_ground", render)

    async def drive() -> tuple:
        screen.render_body(80)
        while screen._drawing is not None:
            await asyncio.sleep(0)
        passes.clear()
        screen._drawing = ("k",)
        await screen._draw_ground(("k",), screen._viewport, {}, [], "", True)
        # Read here, not after the loop closes: the newer view's own draw is a task of its
        # own by now, and what it goes on to ask for is that view's business, not this
        # one's. The question is only what the *abandoned* key got.
        return list(passes), screen._frame_coarse

    stale, rough_on_screen = asyncio.run(drive())
    assert stale == [True], "a finished pass was drawn for a view already left behind"
    assert rough_on_screen is True, "the abandoned view left no rough picture on screen"


def test_map_skips_the_coarse_pass_over_a_picture_already_drawn(monkeypatch) -> None:  # noqa: ANN001
    """A tile landing must refine the frame, never coarsen it."""
    from meshterm.ui import map_screen as ms

    screen = _map_over(_StubSource())
    coarse_asked: list = []
    monkeypatch.setattr(
        ms,
        "render_ground",
        lambda vp, tiles, m, **k: coarse_asked.append(k.get("coarse", False)) or (["x"], None),
    )

    async def drive() -> None:
        await _drawn(screen)
        coarse_asked.clear()
        # A tile lands: same view, more detail. The frame on screen is still aligned.
        screen._tiles[screen._viewport.tiles(14)[0]] = _loaded_tile()
        screen.render_body(80)
        while screen._drawing is not None:
            await asyncio.sleep(0)

    asyncio.run(drive())
    assert coarse_asked == [False], "a drawn frame was replaced by a coarser one"


def test_the_rough_pass_is_a_switch_the_map_reads(monkeypatch) -> None:  # noqa: ANN001
    """Both passes work; whether a moved view is offered the first one is a knob.

    Off, every view waits for the whole picture and stands on the last frame's ground
    until it lands. The machinery is :meth:`MapScreen._draw_ground`'s either way — what
    the switch decides is the *asking*, which is why it is read where the raster is
    requested and not inside the renderer.
    """
    from meshterm.ui import map_screen as ms

    def first_draw() -> list:
        """The passes a view with nothing drawn under it asks for."""
        asked: list = []
        monkeypatch.setattr(
            ms,
            "render_ground",
            lambda vp, tiles, m, **k: asked.append(k.get("coarse", False)) or (["x"], None),
        )
        screen = _map_over(_StubSource())

        asyncio.run(_drawn(screen))
        return asked

    monkeypatch.setattr(ms, "_COARSE_PREVIEW", True)
    assert first_draw() == [True, False], "the rough pass did not come first when asked for"

    monkeypatch.setattr(ms, "_COARSE_PREVIEW", False)
    assert first_draw() == [False], "a rough pass was drawn with the switch off"


def test_map_keeps_its_detail_while_a_find_is_typed(monkeypatch) -> None:  # noqa: ANN001
    """Typing a node name moves the view nowhere, so the streets must not flatten.

    A find query changes which markers are bright — the *overlay* — while the ground
    under them is the same ground, already drawn and reprojected at zero offset as the
    stand-in. Deciding the rough pass on the whole picture rather than on the view meant
    every letter replaced the streets with the coarse pass and drew them back.
    """
    from meshterm.ui import map_screen as ms

    screen = _map_over(_StubSource())
    coarse_asked: list = []
    monkeypatch.setattr(
        ms,
        "render_ground",
        lambda vp, tiles, m, **k: coarse_asked.append(k.get("coarse", False)) or (["x"], None),
    )

    async def drive() -> None:
        await _drawn(screen)
        coarse_asked.clear()
        screen.handle("text", "a")  # one letter of a find query; the view does not move
        screen.render_body(80)
        while screen._drawing is not None:
            await asyncio.sleep(0)

    asyncio.run(drive())
    assert coarse_asked == [False], "a find keystroke coarsened the ground under it"


# --- the ground raster runs off the paint path ---------------------------------------


def _async_map(monkeypatch, drawn: list):
    """A map screen whose rasters are captured instead of run, with a live event loop.

    Its tile loads answer the moment they are asked, with silence: a captured load would
    otherwise be on its way for ever, and no raster is asked for while one is.
    """
    from meshterm.ui import map_screen as ms
    from meshterm.ui.map_render import MapMarker

    monkeypatch.setattr(ms, "_loop_running", lambda: True)
    started: list[tuple] = []
    monkeypatch.setattr(
        ms.asyncio, "ensure_future", lambda coro: (coro.close(), started.append(coro))[0]
    )

    def answered_at_once(self, t):  # noqa: ANN001 - MapScreen._load, minus the thread
        self._pending.discard(t)
        self._unanswered[t] = monotonic() + _TILE_RETRY
        return asyncio.sleep(0)  # a coroutine for the captured ensure_future to close

    monkeypatch.setattr(ms.MapScreen, "_load", answered_at_once)
    screen = ms.MapScreen(_StubSession(80, 24), [MapMarker("A", 45.5, -73.6)], _StubSource(), 14)
    drawn.append(started)
    return screen


def test_map_paints_without_waiting_for_the_ground(monkeypatch) -> None:  # noqa: ANN001
    """A pan must answer the keystroke now — rasterizing takes ~1 s on the PicoCalc."""
    from meshterm.ui import map_screen as ms

    calls: list[tuple] = []
    real = ms.render_map
    monkeypatch.setattr(
        ms,
        "render_map",
        lambda vp, tiles, m, **k: calls.append(tiles) or real(vp, tiles, m, **k),
    )
    started: list = []
    screen = _async_map(monkeypatch, started)

    screen.render_body(80)
    # Whatever ran on the paint path drew no tiles: the ground is somebody else's job.
    assert calls, "the paint drew nothing at all"
    assert all(tiles == {} for tiles in calls), calls
    assert screen._drawing is not None, "no background raster was scheduled"


def _rasters(started: list) -> list:
    """Just the ground-rasterizing coroutines — a pan also schedules its tile fetches."""
    return [c for c in started[0] if "_draw_ground" in c.__qualname__]


def test_map_keeps_one_raster_in_flight_while_panning(monkeypatch) -> None:  # noqa: ANN001
    """A held arrow key must not queue a second of thread work per keypress."""
    started: list = []
    screen = _async_map(monkeypatch, started)
    screen.render_body(80)
    scheduled = len(_rasters(started))

    for _ in range(8):
        screen.handle("right")
        screen.render_body(80)

    assert len(_rasters(started)) == scheduled, "a second raster started before the first finished"
    assert screen._wanted is not None, "the newest view was not remembered for next"


def test_map_holds_an_aligned_frame_while_a_tile_lands(monkeypatch) -> None:  # noqa: ANN001
    """A tile arriving must not blank streets that are still in the right place.

    The view has not moved, so the finished raster is still correctly aligned — it is only
    missing the newcomer's detail. Redrawing from nothing would flash the ground away for
    the second it takes to draw the same picture again.
    """
    started: list = []
    screen = _async_map(monkeypatch, started)
    screen.render_body(80)
    # Pretend the scheduled raster finished.
    screen._frame = ["ground"] * 20
    screen._frame_key = screen._ground_key(screen._viewport)
    screen._drawing = None
    # All but the bottom row is the frame verbatim; that one also carries the basemap
    # credit, which is stamped at paint time rather than baked into a raster served
    # across many paints (see meshterm/ui/attribution.py).
    painted = screen.render_body(80)
    assert painted[:-1] == ["ground"] * 19
    assert _plain(painted[-1]).startswith("ground")

    in_view = screen._viewport.tiles(14)[0]
    screen._tiles[in_view] = _loaded_tile()  # a tile lands, view unmoved
    assert screen.render_body(80)[:-1] == ["ground"] * 19, "dropped an aligned frame"
    assert screen._drawing is not None, "did not redraw for the new tile"


def test_map_stands_on_its_ghost_until_the_view_tiles_are_in(monkeypatch) -> None:  # noqa: ANN001
    """A zoom shows the ghost, then the map — never a black screen in between.

    A raster drawn while the new zoom's tiles were still loading painted their ground
    black, and being the view's own frame it replaced the ghost: ghost, black, then the
    streets drawn in (JP, 2026-10-04). It was a raster thrown away besides, since the tiles
    landing ask for another.
    """
    from meshterm.ui import map_screen as ms
    from meshterm.ui.map_render import MapMarker

    monkeypatch.setattr(ms, "_loop_running", lambda: True)
    started: list = []
    monkeypatch.setattr(
        ms.asyncio, "ensure_future", lambda coro: (coro.close(), started.append(coro))[0]
    )
    stood_on: list = []
    monkeypatch.setattr(
        ms, "render_map", lambda vp, tiles, m, **k: stood_on.append(k.get("ghost")) or [""]
    )
    screen = ms.MapScreen(_StubSession(80, 24), [MapMarker("A", 45.5, -73.6)], _StubSource(), 14)
    screen._ghost = ghost = object()  # the ground the last view left behind

    def rasters() -> list:
        return [c for c in started if "_draw_ground" in c.__qualname__]

    screen.handle("pageup")  # zoom in: a tile zoom whose tiles are not in hand
    screen.render_body(80)
    assert screen._pending, "the fixture's view asked for no tiles"
    assert not rasters(), "a raster was drawn before the view's tiles were in"
    assert stood_on == [ghost], "the paint did not stand on the ghost"

    for t in list(screen._pending):  # the tiles land
        screen._pending.discard(t)
        screen._tiles[t] = _loaded_tile()
    screen.render_body(80)
    assert len(rasters()) == 1, "the tiles landed and their raster was not drawn"


def test_map_drops_a_stale_frame_once_the_view_moves(monkeypatch) -> None:  # noqa: ANN001
    """Streets drawn for somewhere else are a lie about where you are looking."""
    started: list = []
    screen = _async_map(monkeypatch, started)
    screen.render_body(80)
    screen._frame = ["ground"] * 20
    screen._frame_key = screen._ground_key(screen._viewport)
    screen._drawing = None

    screen.handle("right")
    assert screen.render_body(80) != ["ground"] * 20, "kept a misaligned frame"


async def test_map_pans_on_the_ground_its_last_raster_left(monkeypatch) -> None:  # noqa: ANN001
    """A misaligned frame is dropped, but the ground it drew is kept and reprojected.

    It is the only ground anyone has until the next raster lands, and a pan that answers
    with an empty canvas blanks the map to black between every keypress.
    """
    from meshterm.ui import map_screen as ms

    started: list = []
    screen = _async_map(monkeypatch, started)
    screen.render_body(80)

    await screen._draw_ground(
        screen._ground_key(screen._viewport),
        screen._viewport,
        {},
        screen._markers,
        "",
        False,
    )
    assert screen._ghost is not None, "the finished raster left no ground behind"
    assert screen._ghost.viewport == screen._viewport

    handed: list = []
    real = ms.render_map
    monkeypatch.setattr(
        ms,
        "render_map",
        lambda vp, tiles, m, **k: handed.append(k.get("ghost")) or real(vp, tiles, m, **k),
    )
    screen.handle("right")
    screen.render_body(80)
    assert handed and handed[-1] is screen._ghost, "the pan painted on an empty canvas"


def test_each_road_class_keeps_its_own_colour_through_the_per_tile_style_memo() -> None:
    """The tile's palette is resolved once per class, not once per feature.

    ``mark_rgb`` was reached ~30 000 times a frame and ``Feature.get`` ~32 000 — both
    answering the same handful of questions. Hoisting them behind a per-tile memo is only
    sound while every class still resolves to exactly the colour it did: a motorway must
    not inherit the footway's grey because it drew second.
    """
    from meshterm.core.mvt import GEOM_LINE, Feature, Layer
    from meshterm.ui.map_render import (
        _RAIL,
        _ROAD_DEFAULT,
        _ROAD_STYLE,
        _draw_tile,
        _Frame,
    )
    from meshterm.ui.mapcanvas import MapCanvas
    from meshterm.ui.theme import mark_rgb

    classes = ["motorway", "footway", "residential", "rail", "no_such_class", None]
    features = [
        Feature(
            geom_type=GEOM_LINE,
            # One short horizontal run per class, each on its own row of the tile.
            rings=[[(200, 500 + i * 500), (3900, 500 + i * 500)]],
            tags={} if cls is None else {"class": cls},
        )
        for i, cls in enumerate(classes)
    ]
    # Wide enough that the whole tile lands on the canvas, so every class gets drawn.
    vp = Viewport(45.5019, -73.5674, 14, 512, 512)
    canvas = MapCanvas(vp.dot_w // 2, vp.dot_h // 4)
    frame = _Frame(canvas=canvas, viewport=vp)
    _draw_tile(
        frame,
        [Layer(name="transportation", extent=4096, features=features)],
        14,
        4843,
        5861,
    )

    painted = {colour for row in canvas._color for colour in row if colour}
    for cls in classes:
        if cls == "rail":
            expected = _RAIL
        else:
            expected = _ROAD_STYLE.get(cls or "", _ROAD_DEFAULT)
        assert mark_rgb(expected[0]) in painted, f"{cls} lost its own colour"


def test_the_map_registers_no_cli_command() -> None:
    """The map is menu-only: a picture has no scripted face.

    Its answer is a drawing whose nodes are told apart by colour, which the scripted CLI
    does not have (see :mod:`meshterm.ui.script`) — so the tool registers nothing rather
    than printing an unparseable block of braille. The located nodes stay scriptable
    through ``meshterm contacts``.
    """
    import typer

    app = typer.Typer()
    MapTool().register_cli(app)
    assert app.registered_commands == []


# -- the basemap credit -------------------------------------------------------
#
# The OpenStreetMap Foundation's Attribution Guideline is what these pin (quoted in
# meshterm/ui/attribution.py): the credit sits in a corner of the map, names
# "OpenStreetMap" rather than an abbreviation, is visible without interacting, and may
# collapse once the map is used — to a form that is still a complete OSM attribution.
#
# Which corner is the *frame's*, and the two platforms' frames differ: a bordered one sets
# the credit into its bottom border rule and leaves the drawing alone, while the
# borderless one has no rule and stamps the drawing's own last row instead. So most of
# these run twice, once per platform, and assert on the surface that platform actually
# marks.


def _credited_map(cols: int = 80) -> MapScreen:
    """A map screen over the offline source, arrived at and not yet touched."""
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.map_screen import MapScreen

    return MapScreen(_StubSession(cols, 24), [MapMarker("A", 45.5, -73.6)], _StubSource(), 14)


def _last_row(screen: MapScreen, cols: int = 80) -> str:
    """The bottom row of a rendered map body, stripped to plain text."""
    return _plain(screen.render_body(cols)).splitlines()[-1]


#: A panel's bottom corners, in both shapes Rich draws them: rounded wherever the console
#: can take them, square where it judges the console a legacy Windows one — which a
#: captured pytest run on Windows is. Asserting one shape passes on one OS only.
_BOTTOM_CORNERS = ("╰╯", "└┘")


def _bottom_rule(screen: MapScreen, cols: int = 80, rows: int = 24) -> str:
    """The composed frame's bottom border rule — the line the caption is set into."""
    from rich.text import Text

    from meshterm.ui.tui import frame

    screen.note_viewport(max(1, rows - 4))
    composed = frame.compose_base(Text(""), screen, screen.footer_hint, cols, rows)
    return next(
        ln
        for ln in reversed([Text.from_ansi(x).plain for x in composed.split("\n")])
        if any(pair[0] in ln for pair in _BOTTOM_CORNERS)
    )


def _mark(screen: MapScreen, cols: int = 80) -> str:
    """Wherever this platform puts the credit: the bottom rule, else the drawing's last row."""
    return _bottom_rule(screen, cols) if screen.bottom_caption else _last_row(screen, cols)


def _both_platforms():
    """The two platforms, restoring REGULAR afterwards — the credit differs across them."""
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform

    try:
        for platform in (REGULAR, PICOCALC_LYRA):
            set_platform(platform)
            yield platform
    finally:
        set_platform(REGULAR)


def test_credit_names_openstreetmap_in_full_never_an_abbreviation() -> None:
    """OSMF: "Attribution must be to 'OpenStreetMap'" — so neither form may abbreviate it."""
    from meshterm.ui import attribution

    for text in (attribution.CREDIT_FULL, attribution.CREDIT_SHORT):
        assert "OpenStreetMap" in text
        assert not re.search(r"\bOSM\b", text), text
    # The remnant is one of the two historical forms OSMF names as acceptable outright,
    # so collapsing never costs the OpenStreetMap credit itself.
    assert attribution.CREDIT_SHORT in ("© OpenStreetMap", "© OpenStreetMap contributors")
    # OpenFreeMap's required line, less only the half it calls optional.
    assert "OpenMapTiles" in attribution.CREDIT_FULL
    assert "Data from OpenStreetMap" in attribution.CREDIT_FULL


def test_an_untouched_map_shows_the_whole_credit_on_every_platform() -> None:
    """The attribution may not be something you have to interact with the map to see."""
    from meshterm.ui import attribution

    for platform in _both_platforms():
        mark = _mark(_credited_map(platform.readable_cols), platform.readable_cols)
        assert attribution.CREDIT_FULL in mark, platform.name


def test_a_bordered_frame_sets_the_credit_in_its_bottom_rule_and_spares_the_drawing() -> None:
    """JP, 2026-09-13: "that way it's not in the map" — the rule carries it, the map doesn't."""
    from meshterm.platforms import REGULAR, set_platform
    from meshterm.ui import attribution

    set_platform(REGULAR)
    screen = _credited_map(72)
    # Right-justified with exactly one rule cell before the corner, as a title sits in the
    # top rule: "╰──… © OpenMapTiles · Data from OpenStreetMap ─╯".
    rule = _bottom_rule(screen, 72)
    assert rule[0] + rule[-1] in _BOTTOM_CORNERS, rule
    assert rule[:-1].endswith(f" {attribution.CREDIT_FULL} ─"), rule
    assert cell_len(rule) == 72
    # And nothing of it reached the drawing: the map's own rows are pure picture.
    assert "OpenStreetMap" not in _plain(screen.render_body(72))


def test_a_frame_with_no_bottom_rule_stamps_the_drawing_instead() -> None:
    """The PicoCalc has a title bar and an F-key lane, and no rule to set a caption into."""
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
    from meshterm.ui import attribution

    try:
        set_platform(PICOCALC_LYRA)
        screen = _credited_map(53)
        assert screen.bottom_caption == ""  # nowhere to put it but the map
        assert _last_row(screen, 53).endswith(attribution.CREDIT_FULL)
    finally:
        set_platform(REGULAR)


def test_the_credit_collapses_once_the_map_is_used_and_stays_collapsed() -> None:
    """OSMF allows a collapse "automatically on map interaction such as panning … zooming"."""
    from meshterm.ui import attribution

    for platform in _both_platforms():
        cols = platform.readable_cols
        for action in ("right", "pageup", "home", "text"):
            screen = _credited_map(cols)
            screen.render_body(cols)  # the arrival paint the reader is answering
            assert attribution.CREDIT_FULL in _mark(screen, cols), (platform.name, action)
            screen.handle(action, "a" if action == "text" else "")
            mark = _mark(screen, cols)
            assert attribution.CREDIT_SHORT in mark, (platform.name, action)
            assert attribution.CREDIT_FULL not in mark, (platform.name, action)
            # A repaint never brings the whole line back — including through the frame's
            # own composition memo, which has to key on the caption to notice.
            assert attribution.CREDIT_FULL not in _mark(screen, cols), (platform.name, action)


def test_a_reopened_map_arrives_with_the_whole_credit_again() -> None:
    """The collapse is a visit's state, not a session's — a new map is arrived at anew."""
    from meshterm.ui import attribution

    used = _credited_map()
    used.render_body(80)  # keys do nothing until the map has a viewport to move
    used.handle("right")
    assert attribution.CREDIT_SHORT in _mark(used)
    assert attribution.CREDIT_FULL in _mark(_credited_map())


def test_the_credit_costs_no_chrome_row_and_no_footer_character() -> None:
    """It rides the frame, so the body keeps its height and the hint keeps its wording."""
    for platform in _both_platforms():
        cols = platform.readable_cols
        screen = _credited_map(cols)
        assert len(screen.render_body(cols)) == len(_credited_map(cols).render_body(cols))
        assert "OpenStreetMap" not in screen.footer_hint
        screen.render_body(cols)
        assert "OpenStreetMap" not in screen.title


def test_the_bottom_rule_keeps_its_clip_arrows_when_a_caption_shares_it() -> None:
    """Neither half is dropped: the arrows lead, the caption follows, the run moves right."""
    from meshterm.ui.tui.frame import _panel_box

    panel = _panel_box("Map", ["body"], True, True, "accent", "© OpenStreetMap")
    assert "↑↓ more · © OpenStreetMap" in (panel.subtitle or "")
    assert panel.subtitle_align == "right"
    # With no caption the arrows keep the centre they have always had.
    assert _panel_box("Map", ["body"], False, True, "accent", "").subtitle_align == "center"


def test_the_find_query_and_the_credit_share_the_row_with_the_credit_kept_whole() -> None:
    """Where the F-key lane replaces the hint, the echo yields its right end to the credit."""
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
    from meshterm.ui import attribution

    try:
        set_platform(PICOCALC_LYRA)
        screen = _credited_map(53)
        screen.render_body(53)  # keys do nothing until the map has a viewport to move
        screen.handle("right")
        for ch in "Hilltop-Repeater-With-A-Very-Long-Name":
            screen.handle("text", ch)
        row = _last_row(screen, 53)
        assert row.lstrip().startswith("/Hilltop")  # the query, cropped to what is left
        assert row.endswith(attribution.CREDIT_SHORT)  # the credit, whole
        assert cell_len(row) <= 53
    finally:
        set_platform(REGULAR)


def test_the_location_preview_credits_the_basemap_in_its_short_form() -> None:
    """A produced work too — and a non-interactive one, so its mark must stand still.

    The preview is embedded in somebody else's body, with no frame of its own to set a
    caption into, so it stamps on both platforms — unlike the full-screen map.
    """
    from meshterm.ui import attribution
    from meshterm.ui.map_render import MapMarker
    from meshterm.ui.minimap import MiniMap

    for platform in _both_platforms():
        cols = platform.readable_cols
        preview = MiniMap(
            _StubSession(cols, 24),
            _StubSource(),
            14,
            center_lat=45.5,
            center_lon=-73.6,
            zoom=12,
            markers=[MapMarker("A", 45.5, -73.6)],
        )
        rows = preview.render(cols, 6)
        assert len(rows) == 6, platform.name  # the credit takes no row of its own
        last = _plain(rows).splitlines()[-1]
        assert last.endswith(attribution.CREDIT_SHORT), platform.name
        assert attribution.CREDIT_FULL not in last, platform.name


def test_the_credit_glyphs_are_in_the_picocalc_console_font() -> None:
    """The mark is drawn on the device, so its glyphs must be in the 512-glyph font."""
    from meshterm.ui import attribution
    from meshterm.ui.fontset import FONT_CODEPOINTS

    for text in (attribution.CREDIT_FULL, attribution.CREDIT_SHORT):
        missing = {ch for ch in text if ord(ch) not in FONT_CODEPOINTS}
        assert not missing, f"{text!r} needs glyphs the console font lacks: {missing!r}"
        assert cell_len(text) == len(text), f"{text!r} draws wider than it measures"


def test_a_row_too_narrow_for_the_remnant_takes_no_credit_rather_than_a_cut_one() -> None:
    """A truncated credit would claim something untrue; the About page still carries it."""
    from meshterm.ui import attribution

    rows = ["x" * 8]
    assert attribution.stamp(rows, 8, full=True) == rows
    # And a row with room for the remnant but not the whole line falls back to it.
    stamped = _plain(attribution.stamp(["x" * 20], 20, full=True)).splitlines()[-1]
    assert stamped.endswith(attribution.CREDIT_SHORT)


def test_stamping_leaves_the_callers_rows_untouched() -> None:
    """The map serves one cached raster across many paints; stamping may not edit it."""
    from meshterm.ui import attribution

    rows = ["street " * 10, "more ground here"]
    before = list(rows)
    attribution.stamp(rows, 70, full=True)
    assert rows == before
