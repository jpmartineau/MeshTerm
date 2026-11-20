# SPDX-License-Identifier: Apache-2.0
"""Draw a pixel image (MCOimg) in the terminal with half blocks.

A cell is approximately two times as tall as it is wide. Thus a cell holds two pixels, one
above the other, and each pixel is square: ``▀`` with the top pixel as its colour and the
bottom pixel as its background. A transparent pixel shows the page. A cell with only the
bottom pixel is ``▄`` in that colour, and a cell with no pixel is a space.

An image that is wider than the lane, or taller than the rows that the platform can give
it, becomes smaller (refer to :func:`fit`). Pixel art loses detail when it becomes smaller,
but an image that is cut loses more: the user sees only a part of it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from rich.style import Style
from rich.text import Text

from ..core import mcoimg
from ..platforms import Platform, on_platform

if TYPE_CHECKING:
    from ..core.mcoimg import Picture

RGB = tuple[int, int, int]

#: The most rows that an image can use under a message, on the active platform. The frame
#: takes some rows (the title bar, the footer, a header, the sender chip), so an image of
#: this height and its message are visible together.
_max_rows = 20

#: The rows of a frame that an image does not get: the title bar, the header or footer,
#: the sender chip, and the line of the message.
_CHROME_ROWS = 4


def _bind(platform: Platform) -> None:
    global _max_rows
    _max_rows = max(4, platform.readable_rows - _CHROME_ROWS)


on_platform(_bind)


@lru_cache(maxsize=128)
def decoded(body: str) -> Picture | None:
    """The image of a message body, or ``None``. It keeps the last results.

    A transcript paints many times while a message is visible. Each paint asks again, and
    the cache answers without a second decode.
    """
    return mcoimg.decode(body)


def fit(width: int, height: int, max_cols: int, max_rows: int) -> tuple[int, int]:
    """The size in pixels at which to draw an image of ``width`` × ``height``.

    The image keeps its proportions. It never becomes larger, and it becomes smaller only
    to fit ``max_cols`` cells and ``max_rows`` rows (two pixels in each row).
    """
    scale = min(1.0, max_cols / width, (max_rows * 2) / height)
    return max(1, int(width * scale)), max(1, int(height * scale))


def picture_text(picture: Picture, max_cols: int, *, indent: int = 0) -> Text:
    """Draw ``picture`` as lines of half blocks, each after ``indent`` spaces.

    Args:
        picture: The decoded image.
        max_cols: The most cells that the image can use, after the indent.
        indent: The cells of page before each line (the indent of the body of a message).

    Returns:
        One :class:`~rich.text.Text` with a line for each two rows of pixels. It has no
        newline after the last line.
    """
    cols, rows = fit(picture.width, picture.height, max(1, max_cols), _max_rows)
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
