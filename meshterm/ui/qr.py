# SPDX-License-Identifier: Apache-2.0
"""Render a string as a scannable QR code in the terminal, using half-block characters.

Each character cell stacks two vertical modules — ``▀`` (upper only), ``▄`` (lower only),
``█`` (both), space (neither) — so a code renders at half the row height of a full-block
one. Where the console font's braille is solid (:attr:`~meshterm.platforms.Platform.
solid_braille`, the PicoCalc), a code too big for its frame in half blocks steps down to
**braille**, two modules across and four down in every cell: half the module, a quarter
of the area, and still every module touching its neighbours, which is what a camera
needs of it. Modules are always drawn **white on black**: the code's dark modules as white ink,
its light modules and quiet zone as a black field, whatever colour theme the terminal is
running. A phone camera reads contrast, and pure white on pure black is the most of it a
screen has; a light-on-dark code is what every scanner made this decade expects to meet
on a screen.

When the code *is* the answer it is a **full-screen** thing: :func:`share_screen` puts it
on a bare frame — no header, no footer, no title, no box — with nothing beside it but the
URL it encodes, so the whole panel is contrast for the camera and the one line a reader
might type out instead. The one place a code sits *inside* a page is a ``qr`` fence in a
written page (:mod:`~meshterm.ui.markdown`), which draws :func:`qr_text` in the flow of
its prose.
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

#: Quiet-zone width (modules) around the code. Scanners want 4 to lock on; 2 still reads
#: on a screen, and is what a code falls back to when its frame is too small for 4.
_BORDER = 4

#: The glyph for each (top-module-dark, bottom-module-dark) pair, drawn as ink — so a
#: dark module is the ink colour and a light module shows the field behind it.
_GLYPH = {
    (True, True): "█",  # █ full block
    (True, False): "▀",  # ▀ upper half
    (False, True): "▄",  # ▄ lower half
    (False, False): " ",
}

#: Every glyph's style — the theme's ``qr``: pure white ink on a pure black field, both
#: ends named so the contrast holds regardless of the surrounding terminal palette. A
#: theme *name* rather than the hex itself so the PicoCalc's slots are chosen on purpose
#: (its bright white and its black), as every fixed hue in the app is. A console without
#: the theme draws the glyphs unstyled, which the plain CLI face would anyway.
_STYLE = "qr"

#: The fits a code tries, in order, until one is small enough for the frame drawing it:
#: the standard code first, then a lighter error level (a screen is never smudged, so the
#: redundancy buys nothing a camera needs), then the narrower quiet zone. A contact card
#: — a name, a 64-hex key — is 57 cells across and 29 rows tall at the standard fit; the
#: PicoCalc panel is 53 across, and a regular terminal is 24 rows.
_FITS: tuple[tuple[str, int], ...] = (("m", 4), ("l", 4), ("m", 2), ("l", 2))


#: Dot bit per (column, row) within a braille cell — the Unicode standard layout.
_BRAILLE_DOTS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))


def _braille(block: Sequence[Sequence[bool]]) -> str:
    """The braille cell lighting each dark module of a 4-row, 2-column ``block``."""
    bits = 0
    for y, row in enumerate(block):
        for x, dark in enumerate(row):
            if dark:
                bits |= _BRAILLE_DOTS[x][y]
    return chr(0x2800 + bits)


@dataclass(frozen=True, slots=True)
class _Grid:
    """How a code's modules pack into cells: ``across`` to a cell, ``down`` to a row.

    Attributes:
        across: Modules per cell, left to right.
        down: Modules per cell row, top to bottom.
        glyph: The cell for one ``down``-row by ``across``-column block of modules,
            ``True`` where the module is dark.
    """

    across: int
    down: int
    glyph: Callable[[Sequence[Sequence[bool]]], str]

    def cells(self, modules: int) -> tuple[int, int]:
        """Cells across and rows down a ``modules``-square code takes on this grid."""
        return -(-modules // self.across), -(-modules // self.down)


#: One module a cell, two a row — the code every terminal can draw.
_HALF_BLOCK = _Grid(across=1, down=2, glyph=lambda block: _GLYPH[(block[0][0], block[1][0])])

#: Two modules a cell, four a row — half the module, so only where braille is solid.
_BRAILLE = _Grid(across=2, down=4, glyph=_braille)

#: The grids a fitted code may be drawn on, largest module first: a code tries every one
#: of :data:`_FITS` on a grid before it shrinks to the next. Bound per platform — the
#: braille grid is only offered where the font draws braille solid.
_GRIDS: tuple[_Grid, ...] = (_HALF_BLOCK,)


@on_platform
def _bind(platform: Platform) -> None:
    """Offer the braille grid where the console font tiles it (now and on switches)."""
    global _GRIDS
    _GRIDS = (_HALF_BLOCK, _BRAILLE) if platform.solid_braille else (_HALF_BLOCK,)


@lru_cache(maxsize=8)
def _make(data: str, error: str) -> segno.QRCode:
    """The code for ``data`` at ``error`` — cached, since a fitted code is asked every paint."""
    import segno

    return segno.make(data, error=error)


def _draw(code: segno.QRCode, border: int, grid: _Grid = _HALF_BLOCK, *, indent: int = 0) -> Text:
    """Draw a segno code on ``grid``, ``border`` light modules around it.

    ``indent`` is the one way a code is ever moved across a line: the same run of bare
    cells in front of *every* row. A code must never be justified — Rich's centring
    strips each row's trailing spaces before it pads, so a row whose right edge is light
    modules loses cells and lands a column off its neighbours, and a finder square one
    row skewed is a code no camera can lock on to.
    """
    rows = [[bool(v) for v in row] for row in code.matrix_iter(border=border)]
    cols, _ = grid.cells(len(rows))
    # Pad the right and the bottom out to whole cells with light modules, so the last
    # cell of every row and the last row of cells draw cleanly.
    span = cols * grid.across
    rows = [row + [False] * (span - len(row)) for row in rows]
    rows += [[False] * span for _ in range(-len(rows) % grid.down)]

    text = Text(no_wrap=True)
    for i, top in enumerate(range(0, len(rows), grid.down)):
        if i:
            text.append("\n")
        if indent:
            text.append(" " * indent)
        band = rows[top : top + grid.down]
        line = "".join(
            grid.glyph([row[x : x + grid.across] for row in band])
            for x in range(0, span, grid.across)
        )
        text.append(line, style=_STYLE)
    return text


def qr_text(data: str, *, error: str = "m", border: int = _BORDER) -> Text:
    """Render ``data`` as a QR code built from half-block characters.

    Args:
        data: The string to encode (e.g. a ``meshcore://channel/add`` share URL).
        error: QR error-correction level — ``l``/``m``/``q``/``h`` (default ``m``).
        border: Quiet-zone width in modules (default :data:`_BORDER`).

    Returns:
        A Rich :class:`~rich.text.Text` (no-wrap) whose lines draw the code, white on
        black, ready to hand to ``ctx.ui.view`` / ``ctx.ui.show`` or to sit in a page.
    """
    return _draw(_make(data, error), border)


def _fit(data: str, width: int, height: int | None) -> tuple[segno.QRCode, int, _Grid] | None:
    """The first fit within ``width`` cells and ``height`` rows, as (code, border, grid).

    Every one of :data:`_FITS` on the largest grid, then on the next of :data:`_GRIDS`: on
    a screen a lighter error level or a narrower quiet zone costs a camera nothing it
    needs, and a module half the size is the one step that does. ``None`` when no fit is
    that small — the caller decides what to relax.
    """
    for grid in _GRIDS:
        for error, border in _FITS:
            code = _make(data, error)
            cols, rows = grid.cells(code.symbol_size(border=border)[0])
            if cols <= width and (height is None or rows <= height):
                return code, border, grid
    return None


def _smallest(data: str) -> tuple[segno.QRCode, int, _Grid]:
    """The last of :data:`_FITS` on the last of :data:`_GRIDS` — for a frame too small for any."""
    error, border = _FITS[-1]
    return _make(data, error), border, _GRIDS[-1]


def fit_qr(data: str, width: int, height: int | None = None) -> Text:
    """The code for ``data`` at the first fit that fits the space given (see :func:`_fit`).

    A code that fits at no fit is drawn at the smallest anyway: a cut code is not
    scannable, but neither is no code, and a larger terminal is one resize away.

    Args:
        data: The string to encode.
        width: Cells available across.
        height: Rows available, or ``None`` to fit the width alone.

    Returns:
        The code, white on black, in half blocks or — where the platform's braille is
        solid and half blocks do not fit — in braille.
    """
    code, border, grid = _fit(data, width, height) or _smallest(data)
    return _draw(code, border, grid)


class QrScreen(ScrollScreen):
    """THE share screen: a code on a bare frame, and under it the URL it encodes.

    A **bare frame, not a popup** (:attr:`~meshterm.ui.tui.screen.Screen.bare`): no
    header, no footer, no box, no title, no instruction — a camera pointed at the screen
    wants the code and nothing arguing with it for contrast, and the reader wants the
    one line they might type out instead. Esc leaves, as it does everywhere.

    The code is **fitted to the frame every paint** (:func:`fit_qr`), which is why this is
    a screen of its own rather than a renderable handed to a result window: only the
    frame knows its rows, and a code that outgrows them is cut, and a cut code scans as
    nothing. The fit is asked to hold the code *and* the URL first; failing that, the
    code alone, with the URL a page down; failing that, the smallest code there is, and
    the frame windows it from the top so a larger terminal shows it whole.
    """

    bare = True

    def __init__(self, url: str, *, title: str) -> None:
        """Set up the share screen for ``url``.

        Args:
            url: The ``meshcore://…`` share URL, encoded as the code and printed under it.
            title: ``Share {name}`` — never drawn on the bare frame; the screen's name
                for the log and the stack.
        """
        super().__init__(Text(""), title=title, floating=False)
        self.url = url

    def render_body(self, width: int) -> list[str]:
        """The code fitted to ``width`` and the frame's rows, then a blank row, then the URL."""
        # The frame notes its height before it asks for the body (compose_bare), so
        # this is the whole terminal's rows; before any paint it is the default 1, and
        # the smallest code stands in until the first paint corrects it.
        rows = self._scroll_viewport
        link = Text(self.url, style="accent", justify="center")
        link_rows = len(render_lines(link, width))
        code, border, grid = (
            _fit(self.url, width, rows - 1 - link_rows)
            or _fit(self.url, width, rows)
            or _fit(self.url, width, None)
            or _smallest(self.url)
        )
        # Centred as a block — one indent for every row (see _draw), never justified.
        cols, _ = grid.cells(code.symbol_size(border=border)[0])
        indent = max(0, (width - cols) // 2)
        self.replace_content(Group(_draw(code, border, grid, indent=indent), Text(""), link))
        return super().render_body(width)


async def share_screen(ctx: AppContext, *, name: str, url: str) -> None:
    """Show the share screen for ``url``: the code, full-frame, and the URL under it.

    One surface for every ``Share {name}`` — the channel share and the contact card both
    come through here, so a change to how MeshTerm presents a share code lands on all of
    them at once. In the menu it is :class:`QrScreen`; on the plain CLI face it prints
    the same two things in order, there being no frame to fit.

    Args:
        ctx: Shared application context (provides the UI surface).
        name: What is being shared — the screen is titled ``Share {name}`` where a
            title is stated at all.
        url: The ``meshcore://…`` share URL, encoded as the QR code and printed under it.
    """
    from .surface import TuiUi

    title = f"Share {name}"
    if isinstance(ctx.ui, TuiUi):
        await ctx.ui.session.run_screen(QrScreen(url, title=title))
        return
    await ctx.ui.view(Group(qr_text(url), Text(""), Text(url, style="accent")), title=title)
