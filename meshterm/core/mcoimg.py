# SPDX-License-Identifier: Apache-2.0
"""Recognize a pixel image in the body of a message: the MCOimg format.

MCOimg is a format for very small pictures (1 to 256 pixels for each side, in practice 10
to 30) that some MeshCore apps send on a channel. Each image is one packet. As a text
message, it is a prefix and then Base91 text: ``im:`` for the versions 1 and 2, ``im3:``
for version 3. A client that does not know the format shows that text as noise. The
reference is ``docs/mcoimg_v3_reference.md`` in the HDDen/meshcore-open repository (MIT).

This module only *recognizes* an image: :func:`recognize` finds that a body is an image,
and for a version 3 image it reads the header (the size and the number of colours of the
palette). Thus the chat can show a short line in place of the noise. It is fast, and it
does not decode the pixels. The pixel decoder is :mod:`meshterm.core.mcoimg_decode`. It is
a separate module because it is large: the chat loads it only when it draws the first
image, and not each time that it opens (approximately 50 ms on the PicoCalc).

Version 3 data, after the Base91 decode::

    0x13                  subtype 1 (MCOimg) in the high nibble, revision 3 in the low
    nonce: u8             changes with each send, and has no meaning for the picture
    header: u8            bit 7 transparent, bit 6 white background, bits 5..4 scan,
                          bits 3..0 palette profile
    dimensions            bit-packed, LSB first (refer to :func:`_dimensions`)
    ...                   the pixels, which :mod:`meshterm.core.mcoimg_decode` reads

"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: The Base91 alphabet of MCOimg, in the order of its values. It is the alphabet of
#: basE91, which has no space, no ``'``, no ``-``, and no ``\\``.
_ALPHABET = (
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!#$%&()*+,./:;<=>?@[]^_`{|}~"'
)
_VALUE = {char: index for index, char in enumerate(_ALPHABET)}

#: A body that is an image: the prefix and one word of Base91 text. The version digit is
#: optional, because the versions 1 and 2 use ``im:``.
_IMAGE = re.compile(r"im(\d?):(\S+)")

#: The shortest Base91 text that MeshTerm accepts as an image of a version whose header it
#: cannot read. The smallest such image of the reference tests has 11 characters. A
#: shorter word after ``im:`` is more probably words that a person typed. A version 3 image
#: needs no minimum, because its first byte and its header must be correct.
_MIN_TEXT = 8

#: The first byte of a version 3 image: subtype 1 (MCOimg), revision 3.
_V3 = 0x13

#: The number of colours of each palette profile of version 3, by profile id. The ids
#: 0 to 5 are the master palettes, 6 to 8 are the grey palettes, and 9 to 15 are the
#: dynamic palettes (subsets of the global palette of 512 colours).
_PROFILE_COLOURS = (2, 4, 8, 16, 32, 64, 16, 32, 8, 8, 16, 32, 64, 128, 256, 512)


@dataclass(frozen=True)
class PixelImage:
    """What a message body tells about the pixel image that it carries.

    Attributes:
        version: The format version: 3 for ``im3:``, or ``None`` for ``im:``, which is
            version 1 or 2 (its text does not say which).
        width: The width in pixels, or ``None`` when MeshTerm cannot read the header.
        height: The height in pixels, or ``None`` when MeshTerm cannot read the header.
        colours: The number of colours of the palette, or ``None`` when MeshTerm cannot
            read the header.
    """

    version: int | None
    width: int | None = None
    height: int | None = None
    colours: int | None = None


def recognize(body: str) -> PixelImage | None:
    """Return the pixel image that ``body`` carries, or ``None`` when it is not an image.

    The body must be the image and nothing else (spaces at the ends are permitted). An
    ``im3:`` body must also start with the version 3 byte after the decode. A body with a
    version that MeshTerm does not know (``im4:``) is an image with no header.

    Args:
        body: The text of a message, without the ``Name: `` of a channel sender.

    Returns:
        The image, or ``None``.
    """
    match = _IMAGE.fullmatch(body.strip())
    if match is None:
        return None
    digit, text = match.groups()
    data = base91_decode(text)
    if data is None:
        return None
    version = int(digit) if digit else None
    if version != 3:
        # Here MeshTerm cannot read a header, so only the length makes the claim.
        return PixelImage(version=version) if len(text) >= _MIN_TEXT else None
    if len(data) < 4 or data[0] != _V3:
        return None
    header = data[2]
    size = _dimensions(_Bits(data[3:]))
    if size is None:
        return PixelImage(version=3)
    width, height = size
    return PixelImage(3, width, height, _PROFILE_COLOURS[header & 0x0F])


def base91_decode(text: str) -> bytes | None:
    """Decode the Base91 text of an image, or return ``None`` for a character not in it.

    Each pair of characters holds 13 or 14 bits: 13 when the low 13 bits of the pair are
    more than 88, or else 14. A last single character holds the remaining bits.
    """
    out = bytearray()
    queue = bits = 0
    value = -1
    for char in text:
        index = _VALUE.get(char)
        if index is None:
            return None
        if value < 0:
            value = index
            continue
        value += index * 91
        queue |= value << bits
        bits += 13 if (value & 8191) > 88 else 14
        while bits > 7:
            out.append(queue & 0xFF)
            queue >>= 8
            bits -= 8
        value = -1
    if value >= 0:
        out.append((queue | (value << bits)) & 0xFF)
    return bytes(out)


class _Bits:
    """A reader of bit fields, LSB first, from a byte string.

    :func:`recognize` uses it. :func:`decode` uses the subclass :class:`_Reader`, which
    adds the integer forms of the format.
    """

    def __init__(self, data: bytes) -> None:
        self._value = int.from_bytes(data, "little")
        self._left = len(data) * 8
        self._pos = 0

    def read(self, count: int) -> int | None:
        """The next ``count`` bits as an unsigned number, or ``None`` past the end."""
        if self._pos + count > self._left:
            return None
        value = (self._value >> self._pos) & ((1 << count) - 1)
        self._pos += count
        return value


def _dimensions(bits: _Bits) -> tuple[int, int] | None:
    """Read the width and the height of a version 3 image, or ``None`` past the end.

    The first two bits are the mode. Each mode stores the sizes minus one:

    - 0: a square up to 64, one width of 6 bits.
    - 1: up to 32 × 32, a width and a height of 5 bits each.
    - 2: up to 64 × 64, a width and a height of 6 bits each.
    - 3: up to 256 × 256. One bit is set for a rectangle. Then there is a width of 8 bits,
      and for a rectangle a height of 8 bits.
    """
    mode = bits.read(2)
    if mode == 0:
        side = bits.read(6)
        return None if side is None else (side + 1, side + 1)
    if mode in (1, 2):
        size = 5 if mode == 1 else 6
        width, height = bits.read(size), bits.read(size)
    elif mode == 3:
        rectangle = bits.read(1)
        if rectangle is None:
            return None
        width = bits.read(8)
        height = bits.read(8) if rectangle else width
    else:
        return None
    if width is None or height is None:
        return None
    return width + 1, height + 1
