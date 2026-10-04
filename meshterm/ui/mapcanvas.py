# SPDX-License-Identifier: Apache-2.0
"""A colored Unicode-braille canvas for the terminal map.

Each character cell holds a 2×4 grid of braille dots, so the drawable resolution is twice
the columns by four times the rows. Streets, rivers and water are rasterized as braille
*dots*; place names, street names and node labels are written as *text* over whole cells;
node markers are single glyphs on top.

A terminal cell can only show one colour, so each cell keeps the colour of the
highest-**priority** feature whose dots fall in it (a river drawn over water keeps the river's
blue). Text and markers form a separate overlay that always wins the cell, with greedy
collision avoidance so labels never overprint each other. The canvas renders straight to
truecolour ANSI lines, which is exactly what the TUI frame consumes.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from itertools import pairwise

from ..platforms import Platform, on_platform
from .marks import RGB, parse_hex  # noqa: F401 - canonical home; re-exported for importers

#: Unicode braille pattern base; add a dot bitmask to get the glyph.
_BRAILLE_BASE = 0x2800

#: What an emboldened run emits — nothing on a 16-slot console, where the kernel VT draws
#: bold as brightness and would recolour the run rather than weight it (see
#: :meth:`MapCanvas.to_ansi_lines`). Bound at platform-switch time.
_BOLD = "\x1b[1m"


@on_platform
def _bind(platform: Platform) -> None:
    """Bind the canvas's emphasis to what the platform can express (now and on switches)."""
    global _BOLD
    _BOLD = "\x1b[1m" if platform.truecolor else ""


def single_cell(text: str) -> str:
    """Reduce ``text`` to characters that render in exactly one fixed-width cell.

    The map is a fixed-width grid drawn with whatever font the terminal happens to have, and
    the layout assumes every label character advances exactly one column. Three kinds of
    character break that: control/format codes, combining marks (which stack onto the previous
    cell), and East-Asian *wide*/*fullwidth* glyphs and emoji (which take two columns and so
    shove the rest of the row out of alignment). Those are also the characters least likely to
    exist in a typical monospace font, so they surface as tofu. Dropping them keeps labels
    legible in the Latin/Cyrillic/Greek range fonts reliably cover; a label that is *only*
    such characters (e.g. an all-emoji node name) collapses to empty and is simply not drawn.
    """
    out: list[str] = []
    for ch in text:
        if ch == " ":
            out.append(ch)
            continue
        if unicodedata.category(ch)[0] == "C":  # control, format, surrogate, unassigned
            continue
        if unicodedata.combining(ch):
            continue
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            continue
        out.append(ch)
    return "".join(out).strip()


#: Dot bit for each (col, row) within a cell — the Unicode braille standard layout.
_DOT_BITS = (
    (0x01, 0x02, 0x04, 0x40),  # left column, rows 0..3
    (0x08, 0x10, 0x20, 0x80),  # right column, rows 0..3
)

#: Magnification tables, by factor — see :func:`_magnified`.
_MAGNIFIED: dict[int, tuple[tuple[int, ...], ...]] = {}


def _magnified(factor: int) -> tuple[tuple[int, ...], ...]:
    """How one source cell's dots land in each cell of the ``factor``x block it becomes.

    A braille cell is 2x4 dots, so magnifying a raster by *n* turns every source cell into
    an ``n`` by ``n`` block of cells — and the only thing that makes the result a *picture*
    rather than a smear is that each of those cells shows its own quarter (or sixteenth) of
    the source, enlarged. Stamping the whole source glyph into all of them instead is
    double vision: the same 2x4 pattern repeated, which reads as a rendering fault rather
    than as a coarse preview (JP, 2026-08-18).

    None of that arithmetic belongs on the paint path, and it doesn't have to be there: a
    cell holds one byte, so the whole mapping is 256 source patterns by ``factor * factor``
    sub-positions, and it is the same table every time. Built once per factor, on first
    use, and read with a single index per cell — the cost of the honest picture is one
    list lookup over the naive one.

    Args:
        factor: Magnification, a power of two (``1`` yields the identity table).

    Returns:
        ``table[source_byte][sub_y * factor + sub_x]`` — the dots that sub-position of the
        magnified source cell shows.
    """
    ready = _MAGNIFIED.get(factor)
    if ready is not None:
        return ready
    table: list[tuple[int, ...]] = []
    for pattern in range(256):
        block: list[int] = []
        for sub_y in range(factor):
            for sub_x in range(factor):
                dots = 0
                for dx in range(2):
                    # Which source dot this destination dot magnifies. Integer division is
                    # exact here: the block spans 2*factor by 4*factor destination dots.
                    src_x = (sub_x * 2 + dx) // factor
                    for dy in range(4):
                        src_y = (sub_y * 4 + dy) // factor
                        if pattern & _DOT_BITS[src_x][src_y]:
                            dots |= _DOT_BITS[dx][dy]
                block.append(dots)
        table.append(tuple(block))
    _MAGNIFIED[factor] = out = tuple(table)
    return out


@dataclass(frozen=True, slots=True)
class Raster:
    """A finished canvas's braille layer alone — its dots and their cell colours.

    The text overlay is deliberately *not* here: the labels and markers a frame carries are
    tied to where things were when it was drawn, so a caller reusing an old raster over a
    moved view (:meth:`MapCanvas.paste_raster`) wants the ground and draws its own overlay
    on top. Taken with :meth:`MapCanvas.raster`, kept with :meth:`MapCanvas.paste_raster`.
    """

    cell_w: int
    cell_h: int
    bits: list[list[int]]
    color: list[list[RGB | None]]


class MapCanvas:
    """A colored braille raster with a text/marker overlay and label collision tracking."""

    def __init__(self, cell_w: int, cell_h: int) -> None:
        """Create a blank canvas.

        Args:
            cell_w: Width in character cells.
            cell_h: Height in character cells.
        """
        self.cell_w = max(1, cell_w)
        self.cell_h = max(1, cell_h)
        self.dot_w = self.cell_w * 2
        self.dot_h = self.cell_h * 4
        self._bits = [[0] * self.cell_w for _ in range(self.cell_h)]
        self._color: list[list[RGB | None]] = [[None] * self.cell_w for _ in range(self.cell_h)]
        self._prio = [[-1] * self.cell_w for _ in range(self.cell_h)]
        # Overlay: (cx, cy) -> (char, rgb, bold). Occupied tracks cells claimed by labels /
        # markers so later labels can avoid them; label_cells is the labels alone, so the
        # anti-stacking margin can guard against text piling up without also forbidding a
        # label from sitting immediately above or below a (single-glyph) marker.
        self._overlay: dict[tuple[int, int], tuple[str, RGB, bool]] = {}
        self._occupied: set[tuple[int, int]] = set()
        self._label_cells: set[tuple[int, int]] = set()

    # -- primitives -------------------------------------------------------------

    def plot(self, x: int, y: int, color: RGB, priority: int) -> None:
        """Light the braille dot at dot ``(x, y)`` and colour its cell by ``priority``."""
        if not (0 <= x < self.dot_w and 0 <= y < self.dot_h):
            return
        cx, cy = x >> 1, y >> 2
        self._bits[cy][cx] |= _DOT_BITS[x & 1][y & 3]
        if priority >= self._prio[cy][cx]:
            self._prio[cy][cx] = priority
            self._color[cy][cx] = color

    def draw_line(self, points: list[tuple[float, float]], color: RGB, priority: int) -> None:
        """Rasterize a polyline through ``points`` (dot coordinates) as braille dots."""
        for (x0, y0), (x1, y1) in pairwise(points):
            self._segment(x0, y0, x1, y1, color, priority)

    def _segment(
        self, x0: float, y0: float, x1: float, y1: float, color: RGB, priority: int
    ) -> None:
        """Bresenham a single segment, skipping ones wholly off the canvas."""
        # Quick reject: both endpoints beyond the same edge → nothing visible.
        if (x0 < 0 and x1 < 0) or (x0 >= self.dot_w and x1 >= self.dot_w):
            return
        if (y0 < 0 and y1 < 0) or (y0 >= self.dot_h and y1 >= self.dot_h):
            return
        xa, ya, xb, yb = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
        dx, dy = abs(xb - xa), -abs(yb - ya)
        sx = 1 if xa < xb else -1
        sy = 1 if ya < yb else -1
        err = dx + dy
        while True:
            self.plot(xa, ya, color, priority)
            if xa == xb and ya == yb:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                xa += sx
            if e2 <= dx:
                err += dx
                ya += sy

    def fill_polygon(
        self,
        rings: list[list[tuple[float, float]]],
        color: RGB,
        priority: int,
        *,
        stipple: int = 1,
    ) -> None:
        """Even-odd scanline fill of a polygon (with holes) in dot space.

        Args:
            rings: The polygon's rings in dot coordinates — one outer ring, then any
                holes; a multipolygon may pass all its parts at once, since even-odd
                gives the same answer for footprints that don't overlap.
            color: Fill colour.
            priority: Cell-colour priority; a higher one drawn later wins the cell.
            stipple: Draw every *n*-th dot on both axes instead of every one, so the
                fill reads as a texture rather than a solid. A braille dot is one bit,
                so a solid fill doesn't shade a region — it *erases* what shares those
                cells. ``2`` lights a quarter of the dots, enough to read as tone while
                leaving room for a road to stay a legible line through it. ``1``, the
                default, is the ordinary solid fill.

        Each edge is filed under the scanlines it actually crosses, rather than every
        scanline testing every edge. The two give the same dots, but the second costs rows
        times edges, and a zoomed-out view is exactly where a polygon is a coastline or a
        province with tens of thousands of edges: on the PicoCalc a z7 frame spent 6.8 of
        its 7.8 s here, almost all of it asking edges about rows they come nowhere near.
        """
        last_row = self.dot_h - 1
        crossings: dict[int, list[float]] = {}
        for ring in rings:
            for (x0, y0), (x1, y1) in pairwise(ring):
                if y0 == y1:
                    continue
                # The scanline through a row's middle, yc = y + 0.5, crosses this edge when
                # it lies in [low, high) — half-open, so a vertex shared by two edges is
                # counted once. Solved for the row, clipped to the canvas.
                low, high = (y0, y1) if y0 < y1 else (y1, y0)
                first = max(0, math.ceil(low - 0.5))
                last = min(last_row, math.ceil(high - 0.5) - 1)
                if stipple > 1:
                    first += -first % stipple
                if first > last:
                    continue
                dx, dy = x1 - x0, y1 - y0
                for y in range(first, last + 1, stipple):
                    row = crossings.get(y)
                    if row is None:
                        row = crossings[y] = []
                    row.append(x0 + (y + 0.5 - y0) * dx / dy)
        for y, xs in crossings.items():
            xs.sort()
            for i in range(0, len(xs) - 1, 2):
                x_from = max(0, int(round(xs[i])))
                x_to = min(self.dot_w - 1, int(round(xs[i + 1])))
                if stipple > 1:
                    x_from += -x_from % stipple
                for x in range(x_from, x_to + 1, stipple):
                    self.plot(x, y, color, priority)

    # -- reuse ------------------------------------------------------------------

    def raster(self) -> Raster:
        """Take a copy of the braille layer, for a later frame to paste back in.

        Copied rather than shared: the caller keeps this for as long as it is the newest
        ground it has, and a canvas that is still being drawn on must not be able to
        change it underneath them.
        """
        return Raster(
            self.cell_w,
            self.cell_h,
            [row[:] for row in self._bits],
            [row[:] for row in self._color],
        )

    def paste_raster(
        self,
        src: Raster,
        cols: list[tuple[int, int]],
        rows: list[tuple[int, int]],
        *,
        magnify: int = 1,
        fade: float = 1.0,
    ) -> None:
        """Fill this canvas's braille layer from ``src``, one cell at a time.

        Cell ``(cx, cy)`` here takes cell ``(cols[cx][0], rows[cy][0])`` of ``src``; a
        ``-1`` for either is a cell with no source (the ground the view has moved onto,
        which nothing has ever drawn) and is left blank. The caller owns the projection —
        it is the one that knows what the two rasters *mean* geographically — and this end
        is a copy loop, deliberately: it runs on the paint path, between a pan keystroke
        and the frame that answers it.

        Under ``magnify`` a source cell covers an ``n`` by ``n`` block of cells here, and
        the second half of each axis entry says *which* cell of that block this one is, so
        each shows its own enlarged share of the source's dots rather than the whole glyph
        over again (see :func:`_magnified`). One list lookup per cell either way.

        Cell granularity is the whole point of the shape: within a source cell the paste
        lands where the *dots* say, but the two rasters' cell grids are only aligned to
        the nearest cell, so a pan settles within half a cell of true and the real raster
        corrects it a moment later. Reduction (a view that zoomed *out*) keeps the whole
        source glyph in the one cell it shrank to, deliberately: dropping three quarters
        of its dots would break every thin line into dashes just as it gets smaller.

        Args:
            src: The raster to sample.
            cols: ``(source cell x, sub-cell x)`` per canvas column (``(-1, 0)`` = none),
                length ``cell_w``.
            rows: ``(source cell y, sub-cell y)`` per canvas row (``(-1, 0)`` = none),
                length ``cell_h``.
            magnify: How many cells across a source cell covers here — a power of two,
                ``1`` for a paste at or below the source's own scale.
            fade: Multiplier on every pasted colour, for a caller marking the ground as
                provisional. ``1.0`` pastes the colours untouched.
        """
        faded: dict[RGB, RGB] = {}
        block = _magnified(magnify)
        for cy, (sy, sub_y) in enumerate(rows):
            if sy < 0 or cy >= self.cell_h:
                continue
            src_bits, src_color = src.bits[sy], src.color[sy]
            bits, color, prio = self._bits[cy], self._color[cy], self._prio[cy]
            lane = sub_y * magnify
            for cx, (sx, sub_x) in enumerate(cols):
                if sx < 0 or cx >= self.cell_w:
                    continue
                dots = block[src_bits[sx]][lane + sub_x]
                if not dots:
                    continue
                bits[cx] = dots
                rgb = src_color[sx]
                if rgb is not None and fade != 1.0:
                    dim = faded.get(rgb)
                    if dim is None:
                        dim = faded[rgb] = (
                            round(rgb[0] * fade),
                            round(rgb[1] * fade),
                            round(rgb[2] * fade),
                        )
                    rgb = dim
                color[cx] = rgb
                # Left at the empty-cell priority so anything drawn afterwards wins the
                # cell outright: pasted ground is a stand-in, never evidence.
                prio[cx] = -1

    # -- overlay (markers + labels) --------------------------------------------

    def marker(self, x: int, y: int, glyph: str, color: RGB) -> None:
        """Place a marker glyph at dot ``(x, y)``.

        Markers always draw (they are the point of the map) and reserve their cell so
        labels route around them. The label, if any, is placed separately via
        :meth:`marker_label` so it can be dropped (bare glyph) when the map is crowded.
        """
        cx, cy = x >> 1, y >> 2
        if not (0 <= cx < self.cell_w and 0 <= cy < self.cell_h):
            return
        self._overlay[(cx, cy)] = (glyph, color, True)
        self._occupied.add((cx, cy))

    def marker_label(
        self,
        x: int,
        y: int,
        text: str,
        color: RGB,
        *,
        label_color: RGB | None = None,
        avoid_dots: bool = False,
    ) -> bool:
        """Place a label beside the marker at dot ``(x, y)``, only if it fits cleanly.

        The label goes to the right of the marker (one blank cell gap) when there's room,
        else to the left — but never over another marker or label. When neither side is
        free the label is dropped and just the marker glyph shows, so a crowded map stays
        legible. With ``avoid_dots`` a spot is also rejected when braille dots already sit
        under it, so a caller can first sweep for placements clear of the drawn lines and
        only then settle for one that overprints them. Returns whether the label was
        placed.
        """
        text = single_cell(text)
        if not text:
            return False
        cx, cy = x >> 1, y >> 2
        if not (0 <= cx < self.cell_w and 0 <= cy < self.cell_h):
            return False
        lc = label_color or color
        for start in (cx + 2, cx - 1 - len(text)):
            if avoid_dots and not self._dot_free(start, cy, len(text)):
                continue
            if self._place_run(start, cy, text, lc, bold=True, checked=True):
                return True
        return False

    def _dot_free(self, start_cx: int, cy: int, length: int) -> bool:
        """Whether the run of cells at ``(start_cx…, cy)`` holds no braille dots."""
        if not (0 <= cy < self.cell_h):
            return False
        if start_cx < 0 or start_cx + length > self.cell_w:
            return False
        return all(self._bits[cy][mx] == 0 for mx in range(start_cx, start_cx + length))

    def place_label(
        self,
        x: float,
        y: float,
        text: str,
        color: RGB,
        *,
        bold: bool = False,
        avoid_dots: bool = False,
    ) -> bool:
        """Write a centered basemap label at dot ``(x, y)`` if it fits without collision.

        Args:
            x: Anchor dot x.
            y: Anchor dot y.
            text: Label text.
            color: Text colour.
            bold: Whether to embolden (used for the most important places).
            avoid_dots: Also reject the spot when braille dots already sit under the
                run, so a caller can sweep for a placement clear of the drawn lines
                before settling for one that overprints them (as :meth:`marker_label`
                does for its side placements).

        Returns:
            ``True`` if placed, ``False`` if it fell off-canvas or overlapped existing text.
        """
        text = single_cell(text)
        if not text:
            return False
        cx = int(x) >> 1
        cy = int(y) >> 2
        start = cx - len(text) // 2
        if avoid_dots and not self._dot_free(start, cy, len(text)):
            return False
        return self._place_run(start, cy, text, color, bold=bold, checked=True)

    def _place_run(
        self,
        start_cx: int,
        cy: int,
        text: str,
        color: RGB,
        *,
        bold: bool,
        checked: bool = False,
    ) -> bool:
        """Write ``text`` starting at cell ``(start_cx, cy)``.

        With ``checked`` the run is skipped entirely if it would run off-canvas, overprint
        or touch any claimed cell on its own row, or stack flush against another label on
        the row directly above or below; otherwise it is forced and simply clipped to the
        canvas. The same-row margin keeps a label off its neighbours (markers included);
        the vertical margin guards only against *label* stacking — which is what otherwise
        lets dense areas silt up into a solid block of text — so a label may still sit
        immediately above or below a single-glyph marker. Returns whether anything was placed.
        """
        if not (0 <= cy < self.cell_h):
            return False
        if checked:
            if start_cx < 0 or start_cx + len(text) > self.cell_w:
                return False
            margin = range(start_cx - 1, start_cx + len(text) + 1)
            if any((mx, cy) in self._occupied for mx in margin):
                return False
            if any((mx, my) in self._label_cells for my in (cy - 1, cy + 1) for mx in margin):
                return False
        placed = False
        for offset, ch in enumerate(text):
            mx = start_cx + offset
            if 0 <= mx < self.cell_w:
                self._overlay[(mx, cy)] = (ch, color, bold)
                self._occupied.add((mx, cy))
                self._label_cells.add((mx, cy))
                placed = True
        return placed

    # -- output -----------------------------------------------------------------

    def to_ansi_lines(self) -> list[str]:
        """Render the canvas to one truecolour ANSI string per row.

        The canvas is a rasterizer of its own — it emits escape codes directly rather
        than going through the themed Rich console — so, like
        :func:`meshterm.ui.tui.render.render_to_ansi`, its output leaves through the
        platform's render-boundary fold: on PicoCalc the truecolour SGR quantizes to
        the 16 palette slots (and any stray glyph folds to the console font) right
        here, wherever the lines end up embedded.

        Emphasis is dropped on a platform that has no truecolour (see :data:`_BOLD`): the
        kernel VT draws ``bold`` as *brightness*, so a bold run whose colour just quantized
        into the dim bank would be promoted to that slot's bright partner — a light-grey
        unknown label arriving as white "you", a purple repeater as pink. The colour a
        marker was given is the load-bearing part; the emboldening is not.
        """
        from .theme import fold_text

        reset = "\x1b[0m"
        lines: list[str] = []
        for cy in range(self.cell_h):
            parts: list[str] = []
            cur: tuple[RGB, bool] | None = None
            for cx in range(self.cell_w):
                overlay = self._overlay.get((cx, cy))
                if overlay is not None:
                    ch, color, bold = overlay
                elif self._bits[cy][cx]:
                    bits = self._bits[cy][cx]
                    ch = chr(_BRAILLE_BASE + bits)
                    color = self._color[cy][cx] or (128, 128, 128)
                    bold = False
                else:
                    if cur is not None:
                        parts.append(reset)
                        cur = None
                    parts.append(" ")
                    continue
                style = (color, bold)
                if style != cur:
                    r, g, b = color
                    parts.append(f"\x1b[0m\x1b[38;2;{r};{g};{b}m" + (_BOLD if bold else ""))
                    cur = style
                parts.append(ch)
            if cur is not None:
                parts.append(reset)
            lines.append(fold_text("".join(parts)))
        return lines
