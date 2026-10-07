# SPDX-License-Identifier: Apache-2.0
"""A coloured Unicode-braille canvas for the terminal map.

Each cell holds a 2×4 grid of braille dots. Thus the resolution that the canvas can draw is
two times the columns by four times the rows. The canvas rasterizes streets, rivers, and
water as braille dots. It writes place names, street names, and node labels as text over
whole cells. Node markers are single glyphs on top.

A terminal cell can show only one colour. Thus each cell keeps the colour of the feature
that has the highest priority and has dots in the cell (a river drawn over water keeps the
blue of the river). The text and the markers are a separate overlay that always wins the
cell. A greedy collision check makes sure that labels never overprint each other. The canvas
renders directly to truecolour ANSI lines, which is the format that the TUI frame uses.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from itertools import pairwise

from ..platforms import Platform, on_platform
from .marks import RGB, parse_hex  # noqa: F401 - exported again for importers

#: The base of the Unicode braille patterns. Add a dot bitmask to get the glyph.
_BRAILLE_BASE = 0x2800

#: The text that a bold run emits. It is empty on a 16-slot console, because the kernel VT
#: draws bold as brightness, and then bold changes the colour of the run instead of its
#: weight (refer to :meth:`MapCanvas.to_ansi_lines`). It is bound when the platform
#: switches.
_BOLD = "\x1b[1m"


@on_platform
def _bind(platform: Platform) -> None:
    """Bind the bold of the canvas to what the platform can show, now and at each switch."""
    global _BOLD
    _BOLD = "\x1b[1m" if platform.truecolor else ""


def single_cell(text: str) -> str:
    """Reduce ``text`` to characters that render in exactly one fixed-width cell.

    The map is a fixed-width grid. The terminal draws it with its own font. The layout
    assumes that each character of a label is one column wide. Three types of character break
    this assumption:

    - Control codes and format codes.
    - Combining marks, which stack onto the previous cell.
    - East-Asian wide and fullwidth glyphs, and emoji. These take two columns, so they move
      the rest of the row out of alignment.

    A typical monospace font is also least likely to have these characters, so they show as
    tofu. When the function removes them, the labels stay legible in the Latin, Cyrillic, and
    Greek ranges that fonts reliably have. A label that has only such characters (for example
    a node name of only emoji) becomes empty, and the canvas does not draw it.
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


#: The dot bit for each (column, row) in a cell. This is the standard Unicode braille layout.
_DOT_BITS = (
    (0x01, 0x02, 0x04, 0x40),  # left column, rows 0..3
    (0x08, 0x10, 0x20, 0x80),  # right column, rows 0..3
)

#: The magnification tables, by factor (refer to :func:`_magnified`).
_MAGNIFIED: dict[int, tuple[tuple[int, ...], ...]] = {}


def _magnified(factor: int) -> tuple[tuple[int, ...], ...]:
    """How one source cell's dots land in each cell of the ``factor``x block it becomes.

    A braille cell is 2x4 dots. If a raster is magnified by *n*, each source cell becomes a
    block of ``n`` by ``n`` cells. The result is a picture, and not a smear, only when each
    of these cells shows its own quarter (or sixteenth) of the source, enlarged. The other
    method is to stamp the whole source glyph into all the cells. That method gives double
    vision: the same 2x4 pattern repeats, and it looks like a rendering fault and not like a
    coarse preview (JP, 2026-08-18).

    This arithmetic does not belong on the paint path, and it does not have to be there. A
    cell holds one byte, so the whole mapping is 256 source patterns by ``factor * factor``
    sub-positions, and the table is the same each time. The function builds the table one
    time for each factor, at the first use. Then the paint path reads it with one index for
    each cell. Thus the picture costs one list lookup more than the naive method.

    Args:
        factor: The magnification, a power of two (``1`` gives the identity table).

    Returns:
        ``table[source_byte][sub_y * factor + sub_x]``, which is the dots that this
        sub-position of the magnified source cell shows.
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
                    # The source dot that this destination dot magnifies. Integer division
                    # is exact here, because the block has 2*factor by 4*factor
                    # destination dots.
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
    """The braille layer of a finished canvas: its dots and their cell colours.

    The text overlay is not in a ``Raster``, on purpose. The labels and markers of a frame
    belong to the places where things were when MeshTerm drew the frame. A caller that uses
    an old raster over a moved viewport (:meth:`MapCanvas.paste_raster`) needs only the
    ground, and it draws its own overlay on top. Get a ``Raster`` with
    :meth:`MapCanvas.raster`. Use it with :meth:`MapCanvas.paste_raster`.
    """

    cell_w: int
    cell_h: int
    bits: list[list[int]]
    color: list[list[RGB | None]]


class MapCanvas:
    """A coloured braille raster with a text and marker overlay, and label collision tracking."""

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
        # Overlay: (cx, cy) -> (char, rgb, bold). Occupied has the cells that labels and
        # markers claimed, so that later labels can avoid them. Label_cells has only the
        # labels. Thus the anti-stacking margin can stop text from piling up, and it does
        # not also forbid a label directly above or below a (single-glyph) marker.
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
        """Draw one segment with the Bresenham method. Skip a segment that is off the canvas."""
        # Quick reject: if both endpoints are beyond the same edge, nothing is visible.
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
        """Fill a polygon (with holes) in dot space with the even-odd scanline method.

        Args:
            rings: The rings of the polygon in dot coordinates: one outer ring, then the
                holes. A multipolygon can pass all its parts at once, because the even-odd
                rule gives the same answer for footprints that do not overlap.
            color: The fill colour.
            priority: The cell-colour priority. If a polygon with a higher priority is
                drawn later, it wins the cell.
            stipple: Draw each *n*-th dot on both axes, instead of each dot, so that the
                fill looks like a texture and not like a solid. A braille dot is one bit,
                so a solid fill does not shade a region. It erases what shares those
                cells. ``2`` lights a quarter of the dots. This is enough to look like a
                tone, and a road can stay a legible line through it. ``1`` is the
                default. It is the ordinary solid fill.

        The function files each edge under the scanlines that the edge crosses. The other
        method is to test each edge on each scanline. The two methods give the same dots,
        but the second method costs rows times edges. A zoomed-out viewport is where a
        polygon is a coastline or a province with tens of thousands of edges. On the
        PicoCalc, a z7 frame spent 6.8 of its 7.8 s in this function, almost all of it to
        test edges against rows that they do not come near.
        """
        last_row = self.dot_h - 1
        crossings: dict[int, list[float]] = {}
        for ring in rings:
            for (x0, y0), (x1, y1) in pairwise(ring):
                if y0 == y1:
                    continue
                # The scanline through the middle of a row, yc = y + 0.5, crosses this edge
                # when yc is in [low, high). The interval is half-open, so a vertex that
                # two edges share is counted one time. The code solves this for the row
                # and clips it to the canvas.
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

        The raster is a copy and is not shared. The caller keeps it for as long as it is the
        newest ground that the caller has. A canvas that is still in use for drawing must
        not be able to change the raster while the caller holds it.
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
        """Fill the braille layer of this canvas from ``src``, one cell at a time.

        Cell ``(cx, cy)`` of this canvas takes cell ``(cols[cx][0], rows[cy][0])`` of
        ``src``. A ``-1`` for either is a cell with no source. It is ground that the
        viewport moved onto, and that nothing drew before. The function leaves it blank.
        The caller owns the projection, because only the caller knows what the two rasters
        mean geographically. This function is a copy loop, on purpose. It runs on the paint
        path, between a pan key press and the frame that answers it.

        If ``magnify`` is more than 1, a source cell covers a block of ``n`` by ``n`` cells
        here. The second half of each axis entry says which cell of that block this cell
        is. Thus each cell shows its own enlarged share of the dots of the source, and not
        the whole glyph again (refer to :func:`_magnified`). The cost is one list lookup for
        each cell in both cases.

        The cell granularity is the purpose of this design. In a source cell, the paste
        lands where the dots say. But the cell grids of the two rasters are aligned only to
        the nearest cell. Thus a pan is correct to within half a cell, and the real raster
        corrects it a moment later. For a reduction (a viewport that zoomed out), the
        function keeps the whole source glyph in the one cell that it shrank to, on
        purpose. If the function removed three quarters of the dots, each thin line would
        break into dashes as it gets smaller.

        Args:
            src: The raster to sample.
            cols: ``(source cell x, sub-cell x)`` for each canvas column (``(-1, 0)`` means
                none). The length is ``cell_w``.
            rows: ``(source cell y, sub-cell y)`` for each canvas row (``(-1, 0)`` means
                none). The length is ``cell_h``.
            magnify: The number of cells across that a source cell covers here. It is a
                power of two. Use ``1`` for a paste at the scale of the source or below it.
            fade: The multiplier for each pasted colour, for a caller that marks the
                ground as provisional. ``1.0`` pastes the colours without a change.
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
                # Keep the priority of an empty cell, so that anything drawn afterwards
                # wins the cell. Pasted ground is a stand-in and never evidence.
                prio[cx] = -1

    # -- overlay (markers + labels) --------------------------------------------

    def marker(self, x: int, y: int, glyph: str, color: RGB) -> None:
        """Place a marker glyph at dot ``(x, y)``.

        Markers always draw, because they are the purpose of the map. They reserve their
        cell, so that labels go around them. A separate call to :meth:`marker_label` places
        the label, if there is one. Thus the label can be removed (a bare glyph) when the map
        is crowded.
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

        The label goes to the right of the marker (with a gap of one blank cell) when there
        is room. If not, it goes to the left. It never goes over another marker or label.
        When neither side is free, the function removes the label and only the marker glyph
        shows. Thus a crowded map stays legible. If ``avoid_dots`` is true, the function
        also rejects a spot that has braille dots under it. Thus a caller can first search
        for placements that are clear of the drawn lines, and then accept a placement that
        overprints them. The function returns whether it placed the label.
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
        """Whether the run of cells at ``(start_cx…, cy)`` has no braille dots."""
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
        """Write a centred basemap label at dot ``(x, y)`` if it fits without a collision.

        Args:
            x: The anchor dot x.
            y: The anchor dot y.
            text: The label text.
            color: The text colour.
            bold: Whether to make the label bold (used for the most important places).
            avoid_dots: Also reject the spot when braille dots are under the run. Thus a
                caller can search for a placement that is clear of the drawn lines, and
                then accept one that overprints them (:meth:`marker_label` does the same
                for its side placements).

        Returns:
            ``True`` if the label is placed. ``False`` if it is off the canvas or overlaps
            existing text.
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

        If ``checked`` is true, the function skips the run completely in these cases:

        - The run goes off the canvas.
        - The run overprints or touches a claimed cell on its own row.
        - The run stacks flush against another label on the row directly above or below.

        If ``checked`` is false, the function forces the run and clips it to the canvas. The
        margin on the same row keeps a label away from its neighbours (markers included).
        The vertical margin stops only the stacking of labels. Without it, dense areas fill
        up and become a solid block of text. Thus a label can still sit directly above or
        below a single-glyph marker. The function returns whether it placed anything.
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

        The canvas is a rasterizer of its own. It emits escape codes directly and does not
        use the themed Rich console. Thus, like
        :func:`meshterm.ui.tui.render.render_to_ansi`, its output leaves through the
        render-boundary fold of the platform. On the PicoCalc, the truecolour SGR is
        quantized to the 16 palette slots (and each stray glyph folds to the console font)
        in this function, wherever the lines go afterwards.

        On a platform that has no truecolour, the function removes the bold (refer to
        :data:`_BOLD`). The kernel VT draws ``bold`` as brightness. If a bold run has a
        colour that was just quantized into the dim bank, the VT changes it to the bright
        partner of that slot. A light-grey unknown label then shows as the white "you", and a
        purple repeater shows as pink. The colour that a marker has is the important part.
        The bold is not.
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
