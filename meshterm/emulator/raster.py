# SPDX-License-Identifier: Apache-2.0
"""Cells to pixels: the display that the emulator shows, drawn from a :class:`~.vt.Terminal`.

The Cardputer Zero's display is 320×170. Its grid of 53 columns of 6 pixels and 14 rows of
12 pixels leaves a two-pixel margin at the right and at the bottom. This module divides
the margin equally, so that the grid is at the centre. A Linux console (the PicoCalc's)
starts its grid at the top-left corner of the display instead. It also draws bold as
bright: a dim palette colour becomes the matching bright colour, in the same glyph.
``top_left`` and ``bold_is_bright`` copy these two behaviours.

The pixels are packed directly into the byte layout that the destination wants: 16-bit
RGB565 for the Cardputer's framebuffer, 24-bit RGB for a desktop window. Thus nothing
converts a full frame on its way out.

Only the rows that the terminal reports as dirty are drawn again. A glyph row is drawn by
lookup, not pixel by pixel: each triple of (6-bit row pattern, foreground, background) is
packed one time and used again. This is what makes a full paint affordable on a 1 GHz
core.
"""

from __future__ import annotations

from collections.abc import Callable

from .font import CELL_H, CELL_W, Font
from .vt import BOLD, DIM, HIDDEN, REVERSE, RGB, STRIKE, UNDERLINE, Terminal

#: The size of the Cardputer Zero's display, in pixels.
PANEL_W = 320
PANEL_H = 170

#: A packer: one colour to the bytes of the destination, for one pixel.
Packer = Callable[[RGB], bytes]


def rgb565(colour: RGB) -> bytes:
    """``colour`` as one little-endian RGB565 pixel (the format of the Cardputer's framebuffer)."""
    r, g, b = colour
    value = (r >> 3) << 11 | (g >> 2) << 5 | b >> 3
    return value.to_bytes(2, "little")


def rgb888(colour: RGB) -> bytes:
    """``colour`` as one 24-bit RGB pixel (the format of a desktop window)."""
    return bytes(colour)


def _blend(a: RGB, b: RGB) -> RGB:
    """Halfway from ``a`` to ``b``: how the ink of a dim cell is drawn."""
    return ((a[0] + b[0]) // 2, (a[1] + b[1]) // 2, (a[2] + b[2]) // 2)


class Raster:
    """The pixels of the display, kept in step with a terminal.

    Args:
        terminal: The grid to draw.
        font: The cell font.
        pack: How one pixel is laid out in :attr:`pixels`.
        default_fg: The ink of a cell whose style names none.
        default_bg: The paper of a cell whose style names none.
        width: The width of the display in pixels.
        height: The height of the display in pixels.
        top_left: Start the grid at the top-left corner of the display, as a Linux
            console does, instead of at the centre.
        bold_is_bright: Draw bold as the Linux console does. A foreground from the eight
            dim slots of the palette takes the matching bright colour (slot ``n + 8``),
            and the glyph stays regular, because a console font has no bold face.
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
        top_left: bool = False,
        bold_is_bright: bool = False,
    ) -> None:
        """Lay out the grid on the display, and fill it with the default paper."""
        self.terminal = terminal
        self.font = font
        self.pack = pack
        self.default_fg = default_fg
        self.default_bg = default_bg
        self.width = width
        self.height = height
        self.bpp = len(pack((0, 0, 0)))
        self.stride = width * self.bpp
        self.left = 0 if top_left else (width - terminal.cols * CELL_W) // 2
        self.top = 0 if top_left else (height - terminal.rows * CELL_H) // 2
        palette = terminal.palette
        self._bright = {palette[n]: palette[n + 8] for n in range(8)} if bold_is_bright else None
        self.pixels = bytearray(pack(default_bg) * (width * height))
        self._spans: dict[tuple[int, RGB, RGB], bytes] = {}

    def _span(self, bits: int, fg: RGB, bg: RGB) -> bytes:
        """The six pixels of one glyph row, packed (cached by pattern and colours)."""
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
        """Draw again the rows that the terminal reports as changed.

        Returns:
            The bands of pixel rows ``(first_y, last_y_exclusive)`` that changed, in
            order. They are for a destination that writes only the parts that changed.
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
        """Mark each row as dirty, and draw the whole display again."""
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
            bold = bool(flags & BOLD)
            if bold and self._bright is not None:
                fg, bold = self._bright.get(fg, fg), False
            if flags & REVERSE:
                fg, bg = bg, fg
            if flags & DIM:
                fg = _blend(fg, bg)
            if flags & HIDDEN or char == "":
                glyph = bytes(CELL_H)
            else:
                glyph = self.font.glyph(char, bold=bold)
            if flags & UNDERLINE:
                glyph = glyph[: CELL_H - 2] + b"\xfc" + glyph[CELL_H - 1 :]
            if flags & STRIKE:
                glyph = glyph[:6] + b"\xfc" + glyph[7:]
            x0 = (self.left + col * CELL_W) * bpp
            for line in range(CELL_H):
                at = (y0 + line) * stride + x0
                pixels[at : at + CELL_W * bpp] = self._span(glyph[line], fg, bg)
