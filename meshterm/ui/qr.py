# SPDX-License-Identifier: Apache-2.0
"""Render a string as a scannable QR code in the terminal, in half blocks or in braille.

Each cell stacks two vertical modules: ``▀`` (upper only), ``▄`` (lower only), ``█`` (both),
and space (neither). Thus a code has half the row height of a code that uses full blocks.
Some console fonts draw braille solid (:attr:`~meshterm.platforms.Platform.solid_braille`,
the PicoCalc). There, MeshTerm draws each code in **braille** instead. One module is one
dot, so a cell holds eight modules: two across and four down. The code then has a quarter
of the area, and each module still touches its neighbours, which a camera needs.

MeshTerm always draws the modules **white on black**. The dark modules of the code are white
ink. The light modules and the quiet zone are a black field. This is the same for each
colour theme of the terminal. A phone camera reads contrast, and pure white on pure black is
the most contrast that a screen has. Each scanner made in this decade expects a light-on-dark
code on a screen.

When the code is the answer, it is a **full-screen** item. :func:`share_screen` puts it on a
bare frame, with no header, no footer, no title, and no box. Only the URL that the code
encodes is beside it. Thus the whole panel is contrast for the camera, and the user has the
one line that they can type out instead. A code is inside a page in two places:

- A ``qr`` fence in a written page (:mod:`~meshterm.ui.markdown`). It draws :func:`qr_text`
  in the flow of its prose.
- On the PicoCalc, a chat message that has URLs. It hangs a code for each URL under its
  text, side by side (:func:`qr_strip`).

The links of the same message also open on the share screen (^U), on each platform, one
code at a time.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from rich.console import Group
from rich.text import Text

from ..platforms import Platform, on_platform
from .tui.render import render_lines
from .tui.screen import ScrollScreen

if TYPE_CHECKING:
    import segno

    from ..context import AppContext

#: The width of the quiet zone (in modules) around the code. A scanner needs 4 to lock on.
#: A width of 2 still reads on a screen. A code uses it when its frame is too small for 4.
_BORDER = 4

#: The glyph for each (top-module-dark, bottom-module-dark) pair, drawn as ink. Thus a dark
#: module has the ink colour, and a light module shows the field behind it.
_GLYPH = {
    (True, True): "█",  # █ full block
    (True, False): "▀",  # ▀ upper half
    (False, True): "▄",  # ▄ lower half
    (False, False): " ",
}

#: The style of each glyph: the ``qr`` style of the theme. It is pure white ink on a pure
#: black field. The style names both colours, so the contrast holds in each terminal
#: palette. The code uses a theme name and not the hex value, so that the slots of the
#: PicoCalc (its bright white and its black) are chosen on purpose, as for each fixed hue in
#: the app. A console without the theme draws the glyphs with no style, as the plain CLI
#: face does.
_STYLE = "qr"

#: The fits that a code tries, in order, until one is small enough for the frame that draws
#: it. First the standard code. Then a lower error level (a screen is never smudged, so the
#: extra redundancy gives a camera nothing that it needs). Then the narrower quiet zone. A
#: contact card (a name and a key of 64 hex digits) is 57 cells across and 29 rows tall at
#: the standard fit in half blocks (29 by 15 in braille). A regular terminal has 24 rows.
_FITS: tuple[tuple[str, int], ...] = (("m", 4), ("l", 4), ("m", 2), ("l", 2))


#: The dot bit for each (column, row) in a braille cell. This is the standard Unicode layout.
_BRAILLE_DOTS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))


def _braille(block: Sequence[Sequence[bool]]) -> str:
    """The braille cell that lights each dark module of a ``block`` of 4 rows and 2 columns."""
    bits = 0
    for y, row in enumerate(block):
        for x, dark in enumerate(row):
            if dark:
                bits |= _BRAILLE_DOTS[x][y]
    return chr(0x2800 + bits)


@dataclass(frozen=True, slots=True)
class _Grid:
    """How the modules of a code pack into cells: ``across`` in a cell, ``down`` in a row.

    Attributes:
        across: The modules in a cell, left to right.
        down: The modules in a cell row, top to bottom.
        glyph: The cell for one block of modules (``down`` rows by ``across`` columns).
            A value is ``True`` where the module is dark.
    """

    across: int
    down: int
    glyph: Callable[[Sequence[Sequence[bool]]], str]

    def cells(self, modules: int) -> tuple[int, int]:
        """The cells across and the rows down that a square code of ``modules`` takes here."""
        return -(-modules // self.across), -(-modules // self.down)


#: One module across and two down in a cell. Each terminal can draw this code.
_HALF_BLOCK = _Grid(across=1, down=2, glyph=lambda block: _GLYPH[(block[0][0], block[1][0])])

#: One module is one dot: two across and four down in a cell. Use it only where the font
#: draws braille solid.
_BRAILLE = _Grid(across=2, down=4, glyph=_braille)

#: The grid on which this platform draws each code. It is bound for each platform. It is
#: braille where the font tiles braille. The braille of a desktop font is dotted, and a code
#: that is drawn in it does not scan.
_GRID = _HALF_BLOCK


@on_platform
def _bind(platform: Platform) -> None:
    """Draw codes in braille where the console font tiles it, now and at each switch."""
    global _GRID
    _GRID = _BRAILLE if platform.solid_braille else _HALF_BLOCK


@lru_cache(maxsize=8)
def _make(data: str, error: str) -> segno.QRCode:
    """The code for ``data`` at ``error``. It is cached, because each paint asks for it."""
    import segno

    return segno.make(data, error=error)


def _draw(code: segno.QRCode, border: int, *, indent: int = 0) -> Text:
    """Draw a segno code on the grid of the platform, with ``border`` light modules around it.

    ``indent`` is the only way that MeshTerm moves a code across a line: the same run of
    bare cells in front of each row. A code must never be justified. The centring of Rich
    removes the trailing spaces of each row before it pads the row. Thus a row that has
    light modules at its right edge loses cells, and it is one column off from its
    neighbours. A finder square that is skewed in one row is a code that no camera can lock
    on to.
    """
    text = Text(no_wrap=True)
    for i, line in enumerate(_glyph_rows(code, border)):
        if i:
            text.append("\n")
        if indent:
            text.append(" " * indent)
        text.append(line, style=_STYLE)
    return text


def _glyph_rows(code: segno.QRCode, border: int) -> list[str]:
    """A segno code packed onto the grid of the platform, as one string of glyphs per cell row.

    Each row has the same number of cells, so codes that are side by side stay square.
    """
    grid = _GRID
    rows = [[bool(v) for v in row] for row in code.matrix_iter(border=border)]
    cols, _ = grid.cells(len(rows))
    # Pad the right side and the bottom with light modules to whole cells. Thus the last
    # cell of each row and the last row of cells draw cleanly.
    span = cols * grid.across
    rows = [row + [False] * (span - len(row)) for row in rows]
    rows += [[False] * span for _ in range(-len(rows) % grid.down)]
    return [
        "".join(
            grid.glyph([row[x : x + grid.across] for row in rows[top : top + grid.down]])
            for x in range(0, span, grid.across)
        )
        for top in range(0, len(rows), grid.down)
    ]


def qr_text(data: str, *, error: str = "m", border: int = _BORDER) -> Text:
    """Render ``data`` as a QR code, in half blocks, or in braille where braille is solid.

    Args:
        data: The string to encode (for example a ``meshcore://channel/add`` share URL).
        error: The QR error-correction level: ``l``, ``m``, ``q``, or ``h`` (default ``m``).
        border: The width of the quiet zone in modules (default :data:`_BORDER`).

    Returns:
        A Rich :class:`~rich.text.Text` (no-wrap) whose lines draw the code, white on
        black. The caller can pass it to ``ctx.ui.view`` or ``ctx.ui.show``, or put it in a
        page.
    """
    return _draw(_make(data, error), border)


def _fit(data: str, width: int, height: int | None) -> tuple[segno.QRCode, int] | None:
    """The first of :data:`_FITS` in ``width`` cells and ``height`` rows, as (code, border).

    It returns ``None`` when no fit is that small. Then the caller decides what to relax.
    """
    for error, border in _FITS:
        code = _make(data, error)
        cols, rows = _GRID.cells(code.symbol_size(border=border)[0])
        if cols <= width and (height is None or rows <= height):
            return code, border
    return None


def _smallest(data: str) -> tuple[segno.QRCode, int]:
    """The last of :data:`_FITS`. A frame that is too small for each fit gets it anyway."""
    error, border = _FITS[-1]
    return _make(data, error), border


def fit_qr(data: str, width: int, height: int | None = None) -> Text:
    """The code for ``data`` at the first of :data:`_FITS` that fits the space that is given.

    If a code fits at no fit, MeshTerm draws it at the smallest fit anyway. A cut code does
    not scan, but no code does not scan either, and the user can resize to a larger terminal.

    Args:
        data: The string to encode.
        width: The cells that are available across.
        height: The rows that are available, or ``None`` to fit the width only.

    Returns:
        The code, as :func:`qr_text` draws it.
    """
    code, border = _fit(data, width, height) or _smallest(data)
    return _draw(code, border)


#: The blank cells between two codes that are side by side. The quiet zone of each code
#: stops a camera from reading two codes as one. The gap only shows where the field of one
#: code ends.
_GAP = 1


@lru_cache(maxsize=64)
def _fitted_rows(data: str, width: int, grid: _Grid) -> tuple[str, ...]:
    """The code of ``data`` at its first fit in ``width`` cells, as glyph rows.

    It is cached, because a transcript asks for the same code at each paint of a message
    that it cannot reuse (the picked message). ``grid`` is in the key, so that a platform
    switch never returns a code that is packed for the other platform.
    """
    code, border = _fit(data, width, None) or _smallest(data)
    return tuple(_glyph_rows(code, border))


def qr_strip(data: Sequence[str], width: int, *, indent: int = 0) -> Text:
    """A code for each of ``data``, side by side, with a new band when the line is full.

    Each code takes the first fit in the room after ``indent``, as the code of a frame does
    (:data:`_FITS`). The codes go from left to right, with :data:`_GAP` between them. A code
    that would pass the right edge starts the next band, aligned at the top with the other
    codes of that band. There is no blank row between bands, because the quiet zones above
    and below the codes already separate them.

    Args:
        data: The strings to encode, in reading order.
        width: The cells across the whole line, with ``indent`` included.
        indent: The bare cells in front of each row, so that the strip can hang under a
            block of text.

    Returns:
        The codes as one no-wrap :class:`~rich.text.Text`, white on black like each code.
    """
    room = max(1, width - indent)
    bands: list[list[tuple[str, ...]]] = []
    used = 0
    for datum in data:
        rows = _fitted_rows(datum, room, _GRID)
        cols = len(rows[0])
        if bands and used + _GAP + cols <= room:
            bands[-1].append(rows)
            used += _GAP + cols
        else:
            bands.append([rows])
            used = cols

    text = Text(no_wrap=True)
    for band in bands:
        for y in range(max(len(rows) for rows in band)):
            if text:
                text.append("\n")
            text.append(" " * indent)
            for i, rows in enumerate(band):
                if i:
                    text.append(" " * _GAP)
                if y < len(rows):
                    text.append(rows[y], style=_STYLE)
                else:  # a shorter code on the band: bare cells under it, and not its field
                    text.append(" " * len(rows[0]))
    return text


class QrScreen(ScrollScreen):
    """The only share screen: a code on a bare frame, and under it the URL that it encodes.

    It is a **bare frame, not a dialog** (:attr:`~meshterm.ui.tui.screen.Screen.bare`). It
    has no header, no footer, no box, no title, and no instruction. A camera that points at
    the screen needs the code, and nothing else that competes with it for contrast. The user
    needs the one line that they can type out instead. Esc leaves, as it does everywhere.

    The screen **fits the code to the frame at each paint** (:func:`fit_qr`). For this
    reason it is a screen of its own, and not a renderable that MeshTerm passes to a result
    window. Only the frame knows its rows. A code that is taller than the rows is cut, and a
    cut code does not scan. The screen asks for these fits, in this order:

    1. A fit that holds the code and the URL.
    2. If there is none, a fit that holds the code only. The URL is a page down.
    3. If there is none, the smallest code. The frame shows it from the top, so that a
       larger terminal shows it whole.

    If the screen has **several** URLs (the links of a chat message), it is still one screen,
    and it shows one code at a time. The ←→ keys step through the codes in order and stop at
    each end. The URL line has ``←`` and ``→`` on its two sides. A mark is lit where another
    code is in that direction, and dim where there is none. This is the only mark that the
    bare frame has besides the code and its URL, because nothing else on the frame can show
    that the other codes are there.
    """

    bare = True

    def __init__(self, *urls: str, title: str) -> None:
        """Set up the share screen for ``urls``, and show the first one.

        Args:
            urls: The URLs to show, in order: one share URL (``meshcore://…``), or the
                links of a chat message. The screen encodes each URL as a code and prints
                the URL under it.
            title: ``Share {name}``. The bare frame never draws it. It is the name of the
                screen for the log and the stack.
        """
        super().__init__(Text(""), title=title, floating=False)
        self.urls = urls
        self._index = 0

    @property
    def url(self) -> str:
        """The URL whose code the screen shows."""
        return self.urls[self._index]

    def _link(self) -> Text:
        """The URL line: the URL, and if there are several, the ←→ marks for the other codes."""
        link = Text(justify="center")
        several = len(self.urls) > 1
        if several:
            link.append("←  ", style="accent" if self._index > 0 else "muted")
        link.append(self.url, style="accent")
        if several:
            last = len(self.urls) - 1
            link.append("  →", style="accent" if self._index < last else "muted")
        return link

    def handle(self, action: str, data: str = "") -> None:
        """Step to the code of the previous or next URL on ←→. Other keys act as usual."""
        if action in ("left", "right"):
            # Clamp as each cursor in the app does: held arrows stop at an end.
            step = -1 if action == "left" else 1
            index = max(0, min(len(self.urls) - 1, self._index + step))
            if index != self._index:
                self._index = index
                self.scroll_to_top()
            return
        super().handle(action, data)

    def render_body(self, width: int) -> list[str]:
        """The code that fits ``width`` and the frame rows, then a blank row, then the URL."""
        # The frame notes its height before it asks for the body (compose_bare), so
        # this value is all the rows of the terminal. Before the first paint it is the
        # default 1. Then the smallest code is the stand-in until the first paint
        # corrects it.
        rows = self._scroll_viewport
        link = self._link()
        link_rows = len(render_lines(link, width))
        code, border = (
            _fit(self.url, width, rows - 1 - link_rows)
            or _fit(self.url, width, rows)
            or _fit(self.url, width, None)
            or _smallest(self.url)
        )
        # Centre it as a block: one indent for each row (refer to _draw), never justified.
        cols, _ = _GRID.cells(code.symbol_size(border=border)[0])
        indent = max(0, (width - cols) // 2)
        self.replace_content(Group(_draw(code, border, indent=indent), Text(""), link))
        return super().render_body(width)


async def share_screen(ctx: AppContext, *, name: str, url: str) -> None:
    """Show the share screen for ``url``: the code, full-frame, and the URL under it.

    This is the one surface for each ``Share {name}``. The channel share and the contact
    card both use it. Thus a change in the way that MeshTerm shows a share code applies to
    all of them at the same time. In the menu, it is :class:`QrScreen`. On the plain CLI
    face, it prints the same two items in order, because there is no frame to fit.

    Args:
        ctx: The shared application context (it gives the UI surface).
        name: What the user shares. The screen has the title ``Share {name}`` where a
            title is shown at all.
        url: The ``meshcore://…`` share URL. MeshTerm encodes it as the QR code and prints
            it under the code.
    """
    from .surface import TuiUi

    title = f"Share {name}"
    if isinstance(ctx.ui, TuiUi):
        await ctx.ui.session.run_screen(QrScreen(url, title=title))
        return
    await ctx.ui.view(Group(qr_text(url), Text(""), Text(url, style="accent")), title=title)
