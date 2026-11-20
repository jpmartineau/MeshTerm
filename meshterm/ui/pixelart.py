# SPDX-License-Identifier: Apache-2.0
"""Draw a pixel image (MCOimg) in the terminal with half blocks.

A cell is approximately two times as tall as it is wide. Thus a cell holds two pixels, one
above the other, and each pixel is square: ``▀`` with the top pixel as its colour and the
bottom pixel as its background. A transparent pixel shows the page. A cell with only the
bottom pixel is ``▄`` in that colour, and a cell with no pixel is a space.

An image keeps its own size: one pixel is one cell wide and half a cell tall (JP,
2026-11-20). A tall image scrolls with the transcript, as a long message does. The one
exception is an image that is wider than the lane: it becomes smaller to fit the width
(refer to :func:`fit`), because an image that is cut shows only a part of it. MCOimg
images are 10 to 30 pixels wide in practice, so this is rare.

On the picked message, ^U opens the image on a bare frame (:class:`ImageScreen`). There it
becomes larger by a whole number when the frame has the room, so each pixel stays square,
but it never becomes smaller than its own size. When the ``show_mcoimg`` preference is
off, the chat shows only the line about the image, and ^U is the way to see it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from rich.console import Group
from rich.style import Style
from rich.text import Text

from .tui.screen import ScrollScreen

if TYPE_CHECKING:
    from ..core.mcoimg_decode import Picture

RGB = tuple[int, int, int]


@lru_cache(maxsize=128)
def decoded(body: str) -> Picture | None:
    """The image of a message body, or ``None``. It keeps the last results.

    A transcript paints many times while a message is visible. Each paint asks again, and
    the cache answers without a second decode. The decoder is large, so it loads here, at
    the first image, and not when the chat opens.
    """
    from ..core.mcoimg_decode import decode

    return decode(body)


def fit(
    width: int, height: int, max_cols: int, max_rows: int | None = None, *, grow: bool = False
) -> tuple[int, int]:
    """The size in pixels at which to draw an image of ``width`` × ``height``.

    The image keeps its proportions, and becomes smaller only to fit ``max_cols`` cells and
    ``max_rows`` rows (two pixels in each row). ``None`` rows is no limit on the height:
    then only the width can make the image smaller. With ``grow``, an image that is smaller
    than that space becomes larger by the largest whole number that fits. Thus each pixel
    of the image is the same number of cells.
    """
    scale = max_cols / width
    if max_rows is not None:
        scale = min(scale, (max_rows * 2) / height)
    if scale >= 1:
        scale = int(scale) if grow else 1
    return max(1, int(width * scale)), max(1, int(height * scale))


def picture_text(
    picture: Picture,
    max_cols: int,
    *,
    indent: int = 0,
    max_rows: int | None = None,
    grow: bool = False,
) -> Text:
    """Draw ``picture`` as lines of half blocks, each after ``indent`` spaces.

    Args:
        picture: The decoded image.
        max_cols: The most cells that the image can use, after the indent.
        indent: The cells of page before each line (the indent of the body of a message).
        max_rows: The most rows that the image can use, or ``None`` for no limit (the
            image under a message: a tall image scrolls with the transcript).
        grow: Make a small image larger to fill the space (refer to :func:`fit`).

    Returns:
        One :class:`~rich.text.Text` with a line for each two rows of pixels. It has no
        newline after the last line.
    """
    rows_cap = None if max_rows is None else max(1, max_rows)
    cols, rows = fit(picture.width, picture.height, max(1, max_cols), rows_cap, grow=grow)
    pixel = _sampler(picture, cols, rows)
    text = Text(no_wrap=True)
    for y in range(0, rows, 2):
        if y:
            text.append("\n")
        text.append(" " * indent)
        for x in range(cols):
            top = pixel(x, y)
            bottom = pixel(x, y + 1) if y + 1 < rows else None
            text.append(*_cell(top, bottom))
    return text


def _sampler(picture: Picture, cols: int, rows: int):  # noqa: ANN202
    """A function that gives the pixel at ``(x, y)`` of the image drawn at ``cols`` × ``rows``.

    A smaller image takes the nearest pixel of the original, so the colours stay the
    colours of the palette. A mix of colours makes pixel art look blurred.
    """
    width, height, pixels = picture.width, picture.height, picture.pixels

    def pixel(x: int, y: int) -> RGB | None:
        return pixels[(y * height // rows) * width + (x * width // cols)]

    return pixel


def _cell(top: RGB | None, bottom: RGB | None) -> tuple[str, Style | None]:
    """The character and the style of one cell, for its top and bottom pixels."""
    if top is None and bottom is None:
        return " ", None
    if top is None:
        return "▄", Style(color=_hex(bottom))
    if bottom is None:
        return "▀", Style(color=_hex(top))
    if top == bottom:
        return "█", Style(color=_hex(top))
    return "▀", Style(color=_hex(top), bgcolor=_hex(bottom))


def _hex(rgb: RGB | None) -> str:
    """The colour of a pixel as ``#rrggbb``."""
    r, g, b = rgb  # type: ignore[misc]
    return f"#{r:02x}{g:02x}{b:02x}"


#: The cells of page at each side of the image on its bare frame.
_MARGIN_COLS = 2
#: The rows of page that the image and its caption leave free on the bare frame, in total.
#: The frame puts two fifths of the free rows above the block (refer to
#: :func:`~meshterm.ui.tui.frame.compose_bare`). With three, one row is above and two are
#: below. With two, the frame puts both below, and the image touches the top.
_MARGIN_ROWS = 3


class ImageScreen(ScrollScreen):
    """The pixel image of a message on a bare frame, as large as the frame permits (^U).

    It is the share screen of a link (:class:`~meshterm.ui.qr.QrScreen`) for an image: no
    header, no footer, no box, and no hint. Esc leaves, as it does everywhere. The image
    fits itself to the frame at each paint, so a larger terminal shows a larger image.
    Under it are the caption (its size and its colours) and the text of the message itself,
    the ``im3:`` string that the sender transmitted. Thus the user can examine or copy the
    text that made the image.

    The image never touches the edges of the frame. It keeps :data:`_MARGIN_COLS` cells at
    each side and at least one row above and below, as a QR code keeps its quiet zone. The
    frame puts the block a little above the true centre, as it does for a code.

    The image never becomes smaller than its own size (JP, 2026-11-20), except to fit the
    width of the frame. On a frame that has the room, it grows by the largest whole number
    that holds it with its text, or else alone. An image that is taller than the frame
    (on the 14 rows of the Cardputer, a 30-pixel image) starts at the top of the page, and
    the arrows scroll it.

    The text of the message has no spaces, so it breaks at the width of the image area,
    into a block of lines that is centred as one. The text never makes the image smaller
    than the size that it has alone on the frame: the text can only stop it from growing.
    When the frame cannot hold both, the image gets the rows, and the text is under it, a
    page down, as the URL of a large QR code is.
    """

    bare = True

    def __init__(self, picture: Picture, caption: Text, raw: str = "") -> None:
        """Set up the screen for ``picture``, with ``caption`` and ``raw`` under it.

        Args:
            picture: The decoded image.
            caption: The line about the image, as the chat shows it.
            raw: The text of the message: the ``im3:`` string.
        """
        super().__init__(Text(""), title="Image", floating=False)
        self.picture = picture
        self._caption = caption
        self.raw = raw.strip()

    def render_body(self, width: int) -> list[str]:
        """The image as large as the frame permits, then its caption and its text."""
        # The frame notes its height before it asks for the body, as on the share screen.
        rows = self._scroll_viewport
        picture = self.picture
        art_cols = max(1, width - 2 * _MARGIN_COLS)
        raw_lines = _raw_lines(self.raw, art_cols, width)
        # The margins, a blank row and the caption, then a blank row and the text.
        alone = max(1, rows - _MARGIN_ROWS - 2)
        art_rows = max(1, alone - (1 + len(raw_lines) if raw_lines else 0))
        size = (picture.width, picture.height)
        # The smallest that the image can be: its own size, or less only to fit the width.
        least = fit(*size, art_cols)[0]
        # The largest that it can be without the text: the text can stop it from growing,
        # but never make it smaller than that.
        alone_cols = max(least, fit(*size, art_cols, alone, grow=True)[0])
        limit: int | None = art_rows
        if fit(*size, art_cols, art_rows, grow=True)[0] < alone_cols:
            limit = alone  # the text goes a page down
        if fit(*size, art_cols, limit, grow=True)[0] < least:
            limit = None  # taller than the frame: its own size, and the page scrolls
        cols, _ = fit(*size, art_cols, limit, grow=limit is not None)
        indent = max(0, (width - cols) // 2)
        art = picture_text(picture, art_cols, indent=indent, max_rows=limit, grow=True)
        caption = self._caption.copy()
        caption.justify = "center"
        parts = [art, Text(""), caption]
        if raw_lines:
            parts += [Text(""), *raw_lines]
        self.replace_content(Group(*parts))
        return super().render_body(width)


def _raw_lines(raw: str, max_cols: int, width: int) -> list[Text]:
    """The text of a message as a block of lines no wider than ``max_cols``, centred in ``width``.

    The text is Base91 characters only, each one cell wide, so a line is a slice of it.
    Each line has the same indent, so that the block is centred and its last line starts
    under the others.
    """
    if not raw:
        return []
    size = max(1, min(len(raw), max_cols))
    indent = " " * max(0, (width - size) // 2)
    return [
        Text(indent + raw[start : start + size], style="muted", no_wrap=True)
        for start in range(0, len(raw), size)
    ]
