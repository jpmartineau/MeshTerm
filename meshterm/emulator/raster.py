# SPDX-License-Identifier: Apache-2.0
"""Cells to pixels: the panel the emulator shows, drawn from a :class:`~.vt.Terminal`.

The panel is 320×170; 53 columns of 6 pixels and 14 rows of 12 leave a two-pixel margin
right and bottom, which this splits evenly so the grid sits centred. Pixels are packed
straight into the byte layout the destination wants — 16-bit RGB565 for the Cardputer's
framebuffer, 24-bit RGB for a desktop window — so nothing converts a whole frame on its
way out.

Only rows the terminal reports dirty are redrawn, and a glyph row is drawn by lookup, not
per pixel: every (6-bit row pattern, foreground, background) triple is packed once and
reused, which is what keeps a full repaint affordable on a 1 GHz core.
"""

from __future__ import annotations

from collections.abc import Callable

from .font import CELL_H, CELL_W, Font
from .vt import BOLD, DIM, HIDDEN, REVERSE, RGB, STRIKE, UNDERLINE, Terminal

#: The Cardputer Zero's panel, in pixels.
PANEL_W = 320
PANEL_H = 170

#: A packer: one colour to the destination's bytes for one pixel.
Packer = Callable[[RGB], bytes]


def rgb565(colour: RGB) -> bytes:
    """``colour`` as one little-endian RGB565 pixel (the Cardputer framebuffer's format)."""
    r, g, b = colour
    value = (r >> 3) << 11 | (g >> 2) << 5 | b >> 3
    return value.to_bytes(2, "little")


def rgb888(colour: RGB) -> bytes:
    """``colour`` as one 24-bit RGB pixel (a desktop window's format)."""
    return bytes(colour)


def _blend(a: RGB, b: RGB) -> RGB:
    """Halfway from ``a`` to ``b`` — how a dim cell's ink is drawn."""
    return ((a[0] + b[0]) // 2, (a[1] + b[1]) // 2, (a[2] + b[2]) // 2)


class Raster:
    """The panel's pixels, kept in step with a terminal.

    Args:
        terminal: The grid to draw.
        font: The cell font.
        pack: How one pixel is laid out in :attr:`pixels`.
        default_fg: The ink of a cell whose style names none.
        default_bg: The paper of a cell whose style names none.
        width: Panel width in pixels.
        height: Panel height in pixels.
    """

    def __init__(
        self,
        terminal: Terminal,
        font: Font,
        *,
        pack: Packer = rgb888,
        default_fg: RGB = (170, 170, 170),
        default_bg: RGB = (0, 0, 0),
        width: int = PANEL_W,
        height: int = PANEL_H,
    ) -> None:
        """Lay the grid out on the panel and paint it blank."""
        self.terminal = terminal
        self.font = font
        self.pack = pack
        self.default_fg = default_fg
        self.default_bg = default_bg
        self.width = width
        self.height = height
        self.bpp = len(pack((0, 0, 0)))
        self.stride = width * self.bpp
        self.left = (width - terminal.cols * CELL_W) // 2
        self.top = (height - terminal.rows * CELL_H) // 2
        self.pixels = bytearray(pack(default_bg) * (width * height))
        self._spans: dict[tuple[int, RGB, RGB], bytes] = {}

    def _span(self, bits: int, fg: RGB, bg: RGB) -> bytes:
        """One glyph row's six pixels, packed (cached by pattern and colours)."""
        key = (bits, fg, bg)
        span = self._spans.get(key)
        if span is None:
            ink, paper = self.pack(fg), self.pack(bg)
            span = b"".join(ink if bits & (0x80 >> x) else paper for x in range(CELL_W))
            if len(self._spans) > 65536:
                self._spans.clear()
            self._spans[key] = span
        return span

    def update(self) -> list[tuple[int, int]]:
        """Redraw the rows the terminal says changed.

        Returns:
            The pixel row bands ``(first_y, last_y_exclusive)`` that changed, in order,
            for a destination that writes only what moved.
        """
        rows = sorted(self.terminal.take_dirty())
        for row in rows:
            self._draw_row(row)
        bands: list[tuple[int, int]] = []
        for row in rows:
            y0 = self.top + row * CELL_H
            if bands and bands[-1][1] == y0:
                bands[-1] = (bands[-1][0], y0 + CELL_H)
            else:
                bands.append((y0, y0 + CELL_H))
        return bands

    def redraw(self) -> None:
        """Mark every row dirty and redraw the whole panel."""
        self.terminal.take_dirty()
        for row in range(self.terminal.rows):
            self._draw_row(row)

    def _draw_row(self, row: int) -> None:
        cells = self.terminal.screen[row]
        y0 = self.top + row * CELL_H
        stride, bpp = self.stride, self.bpp
        pixels = self.pixels
        for col, (char, style) in enumerate(cells):
            fg = style.fg or self.default_fg
            bg = style.bg or self.default_bg
            flags = style.flags
            if flags & REVERSE:
                fg, bg = bg, fg
            if flags & DIM:
                fg = _blend(fg, bg)
            if flags & HIDDEN or char == "":
                glyph = bytes(CELL_H)
            else:
                glyph = self.font.glyph(char, bold=bool(flags & BOLD))
            if flags & UNDERLINE:
                glyph = glyph[: CELL_H - 2] + b"\xfc" + glyph[CELL_H - 1 :]
            if flags & STRIKE:
                glyph = glyph[:6] + b"\xfc" + glyph[7:]
            x0 = (self.left + col * CELL_W) * bpp
            for line in range(CELL_H):
                at = (y0 + line) * stride + x0
                pixels[at : at + CELL_W * bpp] = self._span(glyph[line], fg, bg)
