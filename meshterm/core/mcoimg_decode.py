# SPDX-License-Identifier: Apache-2.0
"""Decode the pixels of a version 3 pixel image (MCOimg).

:func:`decode` decodes all the pixels of a version 3 image into colours. It knows all the
containers, block algorithms, scan orders, palette profiles, and local palettes of the
reference. It does not decode the versions 1 and 2. :mod:`meshterm.core.mcoimg` recognizes
an image and reads its header. This module reads the rest, and it is separate because it
is large: it loads only when the chat draws its first image.

Version 3 data, after the Base91 decode::

    0x13                  subtype 1 (MCOimg) in the high nibble, revision 3 in the low
    nonce: u8             changes with each send, and has no meaning for the picture
    header: u8            bit 7 transparent, bit 6 white background, bits 5..4 scan,
                          bits 3..0 palette profile
    dimensions            bit-packed, LSB first (refer to :func:`~meshterm.core.mcoimg._dimensions`)
    container: 8 bits     bits 7..5 the container, bits 4..0 its context
    transparent colour    a colour reference, only when header bit 7 is set
    container body        bit-packed, then zero bits to the end of the last byte

The palette colours are in :mod:`meshterm.core.mcoimg_palettes`.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import NamedTuple

from .mcoimg import _IMAGE, _V3, _Bits, _dimensions, base91_decode


@dataclass(frozen=True)
class Picture:
    """The decoded pixels of a version 3 image.

    Attributes:
        width: The width in pixels, 1 to 256.
        height: The height in pixels, 1 to 256.
        pixels: The colour of each pixel as (red, green, blue), row by row from the top
            left (``pixels[y * width + x]``). ``None`` is a transparent pixel.
    """

    width: int
    height: int
    pixels: tuple[tuple[int, int, int] | None, ...]


def decode(body: str) -> Picture | None:
    """Decode the pixels of the version 3 image that ``body`` carries.

    The body must be the image and nothing else, as for
    :func:`~meshterm.core.mcoimg.recognize`. The function does not raise an error. A body
    that is not a version 3 image, or that has an error at any point in its data, gives
    ``None``. The data comes from a stranger on the mesh, so the decoder also limits its
    work (refer to :data:`_MAX_TEXT` and :data:`_WORK`).

    Args:
        body: The text of a message, without the ``Name: `` of a channel sender.

    Returns:
        The picture, or ``None``.
    """
    indexed = _decode_refs(body)
    if indexed is None:
        return None
    lookup = _profiles()[indexed.profile].rgb
    clear = indexed.transparent
    pixels = tuple(None if ref == clear else lookup[ref] for ref in indexed.refs)
    return Picture(indexed.width, indexed.height, pixels)


class _Indexed(NamedTuple):
    """A decoded image before its colours: the palette reference of each pixel.

    For a dynamic profile, a reference is an index in ``GLOBAL_512``. For a fixed profile,
    it is a colour id of the profile. These are the numbers of the reference codec and of
    its test vectors.
    """

    width: int
    height: int
    #: The palette profile id, 0 to 15 (refer to :data:`~meshterm.core.mcoimg._PROFILE_COLOURS`).
    profile: int
    #: The references, row by row from the top left.
    refs: list[int]
    #: The reference that is transparent, or ``None``.
    transparent: int | None


def _decode_refs(body: str) -> _Indexed | None:
    """Decode a version 3 image to palette references, or return ``None`` for any error."""
    try:
        return _decode_indexed(body)
    except Exception:  # noqa: BLE001 - hostile data can fail in many ways. Each one is "no image".
        return None


class _Bad(Exception):
    """The data of an image does not obey a rule of the format."""


#: The longest Base91 text that :func:`decode` reads. A message on the mesh is much
#: shorter. The limit stops a long hostile text before it can make the decode slow.
_MAX_TEXT = 4096

#: The work limit of a decode, in pixels for each pixel of the image. Each block that the
#: decoder reads uses the pixels of its rectangle. The regions of a real image do not
#: overlap, so they use one pixel for each pixel of the image. Hostile regions can overlap
#: as much as they want, and this limit stops them.
_WORK = 4

#: The block algorithms that use the compact block header: ``rawGlobal`` (0), ``rawLocal``
#: (1), and ``biColorMask`` (4). They have no scan order, so the scan is always ``h``.
_COMPACT_HEADER = frozenset({0, 1, 4})

#: The block algorithms that have a background colour: ``compactSparse`` (3),
#: ``biColorMask`` (4), and ``varUintSparse`` (15).
_WITH_BACKGROUND = frozenset({3, 4, 15})

#: The block algorithms that cannot use a shared region palette, because they have no
#: local palette: ``rawGlobal`` (0), ``directBitplanes`` (10), and ``directRowDelta`` (12).
_NO_SHARED_PALETTE = frozenset({0, 10, 12})

#: The ids of the grey palette profiles.
_GREY_PROFILES = frozenset({6, 7, 8})

#: The marker of a hybrid common header in a regions stream. It is not an algorithm id.
_HYBRID = 31

_WHITE = (255, 255, 255)


class _Profile(NamedTuple):
    """A palette profile of version 3, as the decoder uses it.

    In the decoder, each colour is a colour id of the profile: 0 to ``size - 1``. Only the
    last step changes the ids of a dynamic profile into ``GLOBAL_512`` indices.
    """

    size: int
    #: The number of bits of one colour id in the data.
    bits: int
    #: The colour id of white, for the implicit white background.
    white: int
    grey: bool
    #: For a dynamic profile, the ``GLOBAL_512`` index of each colour id. For a fixed
    #: profile, ``None``.
    table: tuple[int, ...] | None
    #: The RGB value of each output reference (refer to :class:`_Indexed`).
    rgb: tuple[tuple[int, int, int], ...]


@cache
def _profiles() -> tuple[_Profile, ...]:
    """The 16 palette profiles, by profile id.

    The import of the colour tables occurs here, at the first decode, and not when the
    module loads. Thus the chat can call :func:`~meshterm.core.mcoimg.recognize` and not
    load the tables.
    """
    from meshterm.core import mcoimg_palettes as tables

    fixed = (
        tables.MONO,
        tables.MASTER_4,
        tables.MASTER_8,
        tables.MASTER_16,
        tables.MASTER_32,
        tables.MASTER_64,
        tables.GREY_16,
        tables.GREY_32,
        tables.GREY_8,
    )
    dynamic = (
        tables.DYNAMIC_8,
        tables.DYNAMIC_16,
        tables.DYNAMIC_32,
        tables.DYNAMIC_64,
        tables.DYNAMIC_128,
        tables.DYNAMIC_256,
        tables.DYNAMIC_512,
    )
    profiles = []
    for ident, colours in enumerate(fixed):
        size = len(colours)
        bits = (size - 1).bit_length()
        profiles.append(_Profile(size, bits, 0, ident in _GREY_PROFILES, None, colours))
    for table in dynamic:
        size = len(table)
        bits = (size - 1).bit_length()
        white = next(i for i, index in enumerate(table) if tables.GLOBAL_512[index] == _WHITE)
        profiles.append(_Profile(size, bits, white, False, table, tables.GLOBAL_512))
    return tuple(profiles)


def _decode_indexed(body: str) -> _Indexed | None:
    """Decode a version 3 image to palette references.

    Returns:
        The image, or ``None`` when the body is not a version 3 image.

    Raises:
        _Bad: The data does not obey the format. Other errors can also occur for hostile
            data. :func:`_decode_refs` catches all of them.
    """
    match = _IMAGE.fullmatch(body.strip())
    if match is None or match.group(1) != "3" or len(match.group(2)) > _MAX_TEXT:
        return None
    data = base91_decode(match.group(2))
    if data is None or len(data) < 5 or data[0] != _V3:
        return None
    header = data[2]
    profile_id = header & 0x0F
    profile = _profiles()[profile_id]
    scan = (header >> 4) & 0x03
    implicit_white = bool(header & 0x40)
    reader = _Reader(data[3:])
    size = _dimensions(reader)
    if size is None:
        raise _Bad
    width, height = size
    if reader.pos != _canonical_dimension_bits(width, height):
        raise _Bad
    container_byte = reader.bits(8)
    container, context = container_byte >> 5, container_byte & 0x1F
    algorithm = _algorithm(context) if container <= 3 else 0
    _check_top_level(container, algorithm, scan, implicit_white)
    transparent = reader.bits(profile.bits) if header & 0x80 else None

    decoder = _Decoder(reader, profile, implicit_white, width * height * _WORK)
    if container <= 1:
        # block (0) and compactBlock (1). The checks above made the scan of a compact
        # block h.
        refs = _unscan(decoder.block(width, height, algorithm, scan), width, height, scan)
    elif container <= 3:
        refs = decoder.bounds(width, height, algorithm, scan, compact=container == 3)
    elif container <= 5:
        refs = decoder.regions(width, height, context + 1, compact=container == 5)
    elif container == 6:
        refs = [decoder.solid_colour(context)] * (width * height)
    else:
        refs = decoder.solid_rects(width, height, context)
    reader.finish()

    if profile.table is not None:
        table = profile.table
        refs = [table[ref] for ref in refs]
        if transparent is not None:
            transparent = table[transparent]
    return _Indexed(width, height, profile_id, refs, transparent)


def _canonical_dimension_bits(width: int, height: int) -> int:
    """The number of bits of the canonical dimensions of a size.

    The format permits only the shortest mode for a size. Each mode has its own number of
    bits, so a different number of bits after :func:`_dimensions` shows a mode that is
    not canonical.
    """
    if width == height and width <= 64:
        return 8  # mode 0
    if width <= 32 and height <= 32:
        return 12  # mode 1
    if width <= 64 and height <= 64:
        return 14  # mode 2
    return 11 if width == height else 19  # mode 3, square or rectangle


def _algorithm(ident: int) -> int:
    """Check a block algorithm id (0 to 15) and return it."""
    if ident > 15:
        raise _Bad
    return ident


def _check_top_level(container: int, algorithm: int, scan: int, implicit_white: bool) -> None:
    """Check that the header, the container, and the algorithm agree.

    - A ``block`` cannot hold an algorithm of the compact header, and a ``compactBlock``
      can hold only those algorithms.
    - Some containers and the ``quadtree`` algorithm (7) permit only the scan ``h``.
    - The implicit white background is valid only where there is a background colour.
    """
    if container == 0 and algorithm in _COMPACT_HEADER:
        raise _Bad
    if container == 1 and algorithm not in _COMPACT_HEADER:
        raise _Bad
    horizontal = container in (1, 4, 5, 6, 7) or (container == 3 and algorithm in _COMPACT_HEADER)
    if (horizontal or algorithm == 7) and scan != 0:
        raise _Bad
    if implicit_white and container <= 1 and algorithm not in _WITH_BACKGROUND:
        raise _Bad


def _local_bits(colours: int) -> int:
    """The number of bits of one index in a local palette of ``colours`` colours."""
    return (colours - 1).bit_length() if colours > 1 else 0


def _row_length(scan: int, width: int, height: int) -> int:
    """The length of one row of the scan sequence: a row for ``h`` and ``s``, else a column."""
    return width if scan in (0, 2) else height


def _scan_positions(width: int, height: int, scan: int) -> list[int]:
    """The row-major position of each item of the scan sequence.

    - 0 ``h``: rows from the top, each row from the left.
    - 1 ``v``: columns from the left, each column from the top.
    - 2 ``s``: as ``h``, but the odd rows go from the right.
    - 3 ``sv``: as ``v``, but the odd columns go from the bottom.
    """
    if scan == 0:
        return list(range(width * height))
    if scan == 1:
        return [y * width + x for x in range(width) for y in range(height)]
    if scan == 2:
        return [
            y * width + x
            for y in range(height)
            for x in (range(width) if y % 2 == 0 else range(width - 1, -1, -1))
        ]
    return [
        y * width + x
        for x in range(width)
        for y in (range(height) if x % 2 == 0 else range(height - 1, -1, -1))
    ]


def _unscan(linear: list[int], width: int, height: int, scan: int) -> list[int]:
    """Put the items of a scan sequence back in row-major order."""
    if len(linear) != width * height:
        raise _Bad
    if scan == 0:
        return linear
    result = [0] * len(linear)
    for item, position in zip(linear, _scan_positions(width, height, scan), strict=True):
        result[position] = item
    return result


def _pick(palette: list[int], index: int) -> int:
    """The colour at ``index`` in a local palette, or :class:`_Bad` past its end."""
    if index >= len(palette):
        raise _Bad
    return palette[index]


def _pick_all(palette: list[int], indices: list[int]) -> list[int]:
    """The colours of a list of local palette indices."""
    if indices and max(indices) >= len(palette):
        raise _Bad
    return [palette[index] for index in indices]


class _Reader(_Bits):
    """A reader of the bit fields of version 3, which raises :class:`_Bad` past the end.

    It also reads the integer forms of the format. The data is short (a message), so one
    Python integer holds all of it.
    """

    @property
    def pos(self) -> int:
        """The number of bits that the reader has read."""
        return self._pos

    def bits(self, count: int) -> int:
        """The next ``count`` bits as an unsigned number."""
        if self._pos + count > self._left:
            raise _Bad
        value = (self._value >> self._pos) & ((1 << count) - 1)
        self._pos += count
        return value

    def flags(self, count: int) -> list[bool]:
        """The next ``count`` bits, one flag each, in the order of the data."""
        if count == 0:
            return []
        text = format(self.bits(count), "b").zfill(count)
        return [char == "1" for char in reversed(text)]

    def varuint(self) -> int:
        """A ``bitVarUint``: 7 bits in each byte, and bit 7 set when a byte follows."""
        value = 0
        for shift in range(0, 35, 7):
            byte = self.bits(8)
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
        raise _Bad

    def compact(self) -> int:
        """A ``compactUint``: a short prefix, then 2, 4, or 8 bits, or a ``bitVarUint``.

        The ``bitVarUint`` holds the value itself, not the value minus 276. The reference
        encoder writes it that way.
        """
        if not self.bits(1):
            return self.bits(2)
        if not self.bits(1):
            return self.bits(4) + 4
        if not self.bits(1):
            return self.bits(8) + 20
        return self.varuint()

    def range_compact(self, maximum: int) -> int:
        """A ``rangeCompactUint``: fixed bits up to a maximum of 7, else a ``compactUint``."""
        if maximum < 0:
            raise _Bad
        value = self.bits(maximum.bit_length()) if maximum <= 7 else self.compact()
        if value > maximum:
            raise _Bad
        return value

    def bounded(self, maximum: int) -> int:
        """A ``boundedCompactUint``: as ``compactUint``, but its last form has fixed bits."""
        if maximum < 0:
            raise _Bad
        if maximum <= 7:
            value = self.bits(maximum.bit_length())
        elif not self.bits(1):
            value = self.bits(2)
        elif not self.bits(1):
            value = self.bits(4) + 4
        elif not self.bits(1):
            value = self.bits(8) + 20
        elif maximum < 276:
            raise _Bad
        else:
            value = self.bits((maximum - 276).bit_length()) + 276
        if value > maximum:
            raise _Bad
        return value

    def signed(self) -> int:
        """A ``signedCompactInt``: a zigzag value in a ``compactUint``."""
        code = self.compact()
        return code // 2 if code % 2 == 0 else -((code + 1) // 2)

    def finish(self) -> None:
        """Check the end: only zero bits to the end of the last byte, and no more bytes."""
        if (self._pos + 7) // 8 * 8 != self._left or self._value >> self._pos:
            raise _Bad


class _Decoder:
    """The decoder of the body of one image: the containers and the block algorithms.

    All colours here are colour ids of the profile (refer to :class:`_Profile`).
    """

    def __init__(self, reader: _Reader, profile: _Profile, implicit_white: bool, work: int):
        self.reader = reader
        self.profile = profile
        self.implicit_white = implicit_white
        #: The pixels that the blocks can still use (refer to :data:`_WORK`).
        self.work = work

    # The containers.

    def background(self) -> int:
        """The background colour of a container or a block: white, or a colour in the data."""
        if self.implicit_white:
            return self.profile.white
        return self.reader.bits(self.profile.bits)

    def solid_colour(self, context: int) -> int:
        """The colour of a ``solidBackground`` image.

        The low bits of the colour id are in the context. A colour id of more than 5 bits
        has its high bits in the body.
        """
        if self.implicit_white:
            if context:
                raise _Bad
            return self.profile.white
        bits = self.profile.bits
        low = min(bits, 5)
        if context >> low:
            raise _Bad
        return context | (self.reader.bits(bits - low) << low)

    def solid_rects(self, width: int, height: int, context: int) -> list[int]:
        """A ``solidRects`` image: a background, then up to 64 rectangles of one colour."""
        background = self.background()
        palette = self.palette()
        bits = _local_bits(len(palette))
        count = context + (self.reader.bits(1) << 5) + 1
        refs = [background] * (width * height)
        for _ in range(count):
            x, y, w, h = self.geometry(width, height, compact=True)
            colour = _pick(palette, self.reader.bits(bits))
            for row in range(y, y + h):
                start = row * width + x
                refs[start : start + w] = [colour] * w
        return refs

    def bounds(
        self, width: int, height: int, algorithm: int, scan: int, *, compact: bool
    ) -> list[int]:
        """A ``boundsBlock`` or ``compactBoundsBlock``: one block on a background."""
        background = self.background()
        x, y, w, h = self.geometry(width, height, compact=compact)
        linear = self.block(w, h, algorithm, scan, background=background)
        refs = [background] * (width * height)
        _paste(refs, width, (x, y, w, h), _unscan(linear, w, h, scan))
        return refs

    def regions(self, width: int, height: int, count: int, *, compact: bool) -> list[int]:
        """A ``regions`` or ``compactRegionsStream`` image: blocks on a background.

        A compact stream starts with three flags: a common block header, delta geometry,
        and a shared local palette. A common header with the id 31 is a hybrid header:
        the real id follows, and each region has one bit that selects its own header.
        """
        reader = self.reader
        background = self.background()
        common = delta = shared = False
        if compact:
            common, delta, shared = (bool(reader.bits(1)) for _ in range(3))
        hybrid = False
        common_algorithm = common_scan = 0
        if common:
            ident = reader.bits(5)
            hybrid = ident == _HYBRID
            common_algorithm = _algorithm(reader.bits(5) if hybrid else ident)
            common_scan = self.region_scan(common_algorithm)
        palette = self.palette() if shared else None
        refs = [background] * (width * height)
        box: tuple[int, int, int, int] | None = None
        for _ in range(count):
            if delta and box is not None:
                box = self.delta_geometry(box, width, height)
            else:
                box = self.geometry(width, height, compact=compact)
            own_header = bool(reader.bits(1)) if hybrid else not common
            if own_header:
                algorithm = _algorithm(reader.bits(5))
                scan = self.region_scan(algorithm)
            else:
                algorithm, scan = common_algorithm, common_scan
            _x, _y, w, h = box
            linear = self.block(w, h, algorithm, scan, shared=palette, background=background)
            _paste(refs, width, box, _unscan(linear, w, h, scan))
        return refs

    def region_scan(self, algorithm: int) -> int:
        """The scan of a region header: ``h`` for a compact header algorithm, else 2 bits."""
        return 0 if algorithm in _COMPACT_HEADER else self.reader.bits(2)

    def geometry(self, width: int, height: int, *, compact: bool) -> tuple[int, int, int, int]:
        """A rectangle (x, y, width, height) in the image.

        Plain geometry has 8 bits for each field. Compact geometry has only the bits that
        the image size, and then the space to the right and below, make necessary.
        """
        reader = self.reader
        x = reader.bits((width - 1).bit_length() if compact else 8)
        y = reader.bits((height - 1).bit_length() if compact else 8)
        if x >= width or y >= height:
            raise _Bad
        w = reader.bits((width - x - 1).bit_length() if compact else 8) + 1
        h = reader.bits((height - y - 1).bit_length() if compact else 8) + 1
        if x + w > width or y + h > height:
            raise _Bad
        return x, y, w, h

    def delta_geometry(
        self, previous: tuple[int, int, int, int], width: int, height: int
    ) -> tuple[int, int, int, int]:
        """A rectangle as four signed differences from the previous rectangle."""
        x, y, w, h = (value + self.reader.signed() for value in previous)
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
            raise _Bad
        return x, y, w, h

    # The local palettes.

    def palette(self) -> list[int]:
        """A local palette: a list of colour ids with no duplicates.

        A flat palette gives a length and then each colour id. A descriptor palette
        gives the colour ids in a shorter form (refer to :meth:`descriptor`).
        """
        reader = self.reader
        size, bits = self.profile.size, self.profile.bits
        if size <= 64:
            if reader.bits(1):
                return self.descriptor(reader.bits(2))
            length = reader.bits(bits) + 1
        elif not reader.bits(1):
            length = reader.bits(6) + 1
        elif not reader.bits(1):
            length = reader.bits(6) + 65
        elif not reader.bits(1):
            length = reader.bits(8) + 129
        elif not reader.bits(1):
            length = reader.bits(7) + 385
        else:
            return self.descriptor(reader.bits(2))
        if length > size:
            raise _Bad
        colours = [reader.bits(bits) for _ in range(length)]
        if len(set(colours)) != length:
            raise _Bad
        return colours

    def palette_length(self) -> int:
        """The length of a sorted-delta or ordered-banked palette."""
        reader = self.reader
        size = self.profile.size
        if size <= 64:
            return reader.bits(self.profile.bits) + 1
        if not reader.bits(1):
            return reader.bits(6) + 1
        if not reader.bits(1):
            return reader.bits(6) + 65
        if size <= 128:
            raise _Bad
        length = reader.bits((size - 129).bit_length()) + 129
        if length > size:
            raise _Bad
        return length

    def descriptor(self, kind: int) -> list[int]:
        """A descriptor palette.

        - 0 bitmap: one bit for each colour of the profile.
        - 1 sorted delta: the length, the first colour id, then the gap to each next one.
        - 2 range runs: runs of consecutive colour ids.
        - 3 banked (only for ``dynamicGlobal512``): one bit selects bank bitmaps or the
          ordered form. Refer to :meth:`banked`.

        All forms except the ordered form give the colour ids in ascending order.
        """
        reader = self.reader
        size, bits = self.profile.size, self.profile.bits
        if kind == 0:
            refs = [ref for ref, flag in enumerate(reader.flags(size)) if flag]
        elif kind == 1:
            length = self.palette_length()
            refs = [reader.bits(bits)]
            while len(refs) < length:
                refs.append(refs[-1] + reader.compact() + 1)
        elif kind == 2:
            runs = reader.range_compact(size - 1) + 1
            refs = []
            previous_end = -1
            for _ in range(runs):
                start = reader.bits(bits)
                length = reader.compact() + 1
                end = start + length - 1
                if start <= previous_end or end >= size or len(refs) + length > size:
                    raise _Bad
                refs.extend(range(start, end + 1))
                previous_end = end
        else:
            refs = self.banked()
        if not refs or len(refs) > size or len(set(refs)) != len(refs):
            raise _Bad
        if max(refs) >= size:
            raise _Bad
        return refs

    def banked(self) -> list[int]:
        """A banked palette of ``dynamicGlobal512``: 8 banks of 64 colours.

        - Bank bitmaps (bit 0): a mask of 8 banks, then 64 bits for each selected bank.
        - Ordered (bit 1): the length, then for each colour its bank and its offset in
          the bank. A single bank has 3 bits once. Several banks have a mask of 8 bits,
          and each colour has the index of its bank in the list of selected banks.

        Each selected bank must have one colour or more.
        """
        reader = self.reader
        if self.profile.size != 512:
            raise _Bad
        if not reader.bits(1):
            mask = reader.bits(8)
            if not mask:
                raise _Bad
            refs: list[int] = []
            for bank in range(8):
                if mask >> bank & 1:
                    chosen = [
                        bank << 6 | offset for offset, flag in enumerate(reader.flags(64)) if flag
                    ]
                    if not chosen:
                        raise _Bad
                    refs.extend(chosen)
            return refs
        length = self.palette_length()
        if not reader.bits(1):
            if length > 64:
                raise _Bad
            bank = reader.bits(3)
            return [bank << 6 | reader.bits(6) for _ in range(length)]
        mask = reader.bits(8)
        banks = [bank for bank in range(8) if mask >> bank & 1]
        if len(banks) <= 1 or length > len(banks) * 64:
            raise _Bad
        bank_bits = (len(banks) - 1).bit_length()
        refs = []
        used = set()
        for _ in range(length):
            index = reader.bits(bank_bits)
            if index >= len(banks):
                raise _Bad
            used.add(index)
            refs.append(banks[index] << 6 | reader.bits(6))
        if len(used) != len(banks):
            raise _Bad
        return refs

    # The block algorithms.

    def block(
        self,
        width: int,
        height: int,
        algorithm: int,
        scan: int,
        *,
        shared: list[int] | None = None,
        background: int | None = None,
    ) -> list[int]:
        """Decode one block. Return its colours in the order of its scan.

        Args:
            width: The width of the block.
            height: The height of the block.
            algorithm: The block algorithm id, 0 to 15.
            scan: The scan id, 0 to 3.
            shared: The shared palette of a regions stream. A block that has a shared
                palette does not read its own palette.
            background: The background of the container. A block that has a background
                uses it, and does not read its own background.
        """
        count = width * height
        self.work -= count
        if self.work < 0:
            raise _Bad
        reader = self.reader
        profile = self.profile
        if shared is not None and algorithm in _NO_SHARED_PALETTE:
            raise _Bad
        if algorithm == 0:  # rawGlobal
            return [reader.bits(profile.bits) for _ in range(count)]
        if algorithm in _WITH_BACKGROUND:
            if background is None:
                background = self.background()
            return self.with_background(count, algorithm, shared, background)
        if algorithm in (10, 12):  # directBitplanes, directRowDelta
            if not (profile.grey or profile.table is not None):
                raise _Bad
            if algorithm == 10:
                return self.adaptive_bitplanes(count, list(range(profile.size)))
            return self.compact_row_delta(
                count,
                _row_length(scan, width, height),
                profile.bits,
                profile.size - 1,
                residual=profile.grey,
            )

        palette = shared if shared is not None else self.palette()
        bits = _local_bits(len(palette))
        if algorithm == 1:  # rawLocal
            return _pick_all(palette, [reader.bits(bits) for _ in range(count)])
        if algorithm in (2, 14):  # compactRle, varUintRle
            return self.rle(count, palette, varuint=algorithm == 14)
        if algorithm == 6:
            return self.lz_pixels(count, palette)
        if algorithm == 7:
            if scan != 0:
                raise _Bad
            return self.quadtree(width, height, palette)
        if algorithm == 8:
            return _pick_all(palette, self.bitplanes(count, bits))
        if algorithm == 9:
            return self.adaptive_bitplanes(count, palette)
        row_length = _row_length(scan, width, height)
        if algorithm == 13:
            values = self.row_delta(count, row_length, bits, len(palette) - 1)
        elif algorithm == 11:
            values = self.compact_row_delta(
                count, row_length, bits, len(palette) - 1, residual=False
            )
        else:  # 5, rowRepeat
            values = self.row_repeat(count, row_length, bits)
        return _pick_all(palette, values)

    def with_background(
        self, count: int, algorithm: int, shared: list[int] | None, background: int
    ) -> list[int]:
        """Decode ``biColorMask`` (4), ``compactSparse`` (3), or ``varUintSparse`` (15).

        With its own palette, a sparse block must not have the background in the palette.
        With a shared palette, each segment must not use the background. A bicolour block
        has one foreground colour: a colour id, or an index in the shared palette.
        """
        reader = self.reader
        if algorithm == 4:
            if shared is None:
                foreground = reader.bits(self.profile.bits)
            else:
                foreground = _pick(shared, reader.bits(_local_bits(len(shared))))
                if foreground == background:
                    raise _Bad
            return [foreground if flag else background for flag in reader.flags(count)]

        palette = shared
        if palette is None:
            palette = self.palette()
            if background in palette:
                raise _Bad
        bits = _local_bits(len(palette))
        varuint = algorithm == 15
        refs = [background] * count
        if varuint:
            segments = reader.varuint()
            if segments <= 0 or segments > count:
                raise _Bad
        else:
            segments = reader.bounded(count - 1) + 1
        position = 0
        for _ in range(segments):
            if varuint:
                skip = reader.varuint()
                if skip > count - position:
                    raise _Bad
                position += skip
                if position >= count:
                    raise _Bad
            else:
                if position >= count:
                    raise _Bad
                position += reader.bounded(count - position - 1)
            colour = _pick(palette, reader.bits(bits))
            if colour == background:
                raise _Bad
            if varuint:
                length = reader.varuint()
                if length <= 0 or position + length > count:
                    raise _Bad
            else:
                length = reader.bounded(count - position - 1) + 1
            refs[position : position + length] = [colour] * length
            position += length
        return refs

    def rle(self, count: int, palette: list[int], *, varuint: bool) -> list[int]:
        """Decode ``compactRle`` or ``varUintRle``: runs of one colour each."""
        reader = self.reader
        bits = _local_bits(len(palette))
        refs: list[int] = []
        while len(refs) < count:
            colour = _pick(palette, reader.bits(bits))
            if varuint:
                length = reader.varuint()
                if length <= 0 or len(refs) + length > count:
                    raise _Bad
            else:
                length = reader.bounded(count - len(refs) - 1) + 1
            refs.extend([colour] * length)
        return refs

    def lz_pixels(self, count: int, palette: list[int]) -> list[int]:
        """Decode ``lzPixels``: literal runs, and copies of earlier pixels (3 or more)."""
        reader = self.reader
        bits = _local_bits(len(palette))
        refs: list[int] = []
        while len(refs) < count:
            done = len(refs)
            if reader.bits(1):
                remaining = count - done
                if done == 0 or remaining < 3:
                    raise _Bad
                distance = reader.range_compact(done - 1) + 1
                length = reader.range_compact(remaining - 3) + 3
                start = done - distance
                if distance >= length:
                    refs.extend(refs[start : start + length])
                else:
                    # The copy overlaps the pixels that it makes, so copy one at a time.
                    for index in range(start, start + length):
                        refs.append(refs[index])
            else:
                length = reader.range_compact(count - done - 1) + 1
                refs.extend(_pick(palette, reader.bits(bits)) for _ in range(length))
        return refs

    def quadtree(self, width: int, height: int, palette: list[int]) -> list[int]:
        """Decode ``quadtree``: a node is one colour, or it splits in two or four.

        A node one pixel wide splits into a top and a bottom half. A node one pixel high
        splits into a left and a right half. Other nodes split into four, in the order top
        left, top right, bottom left, bottom right. The first half is the smaller one.
        """
        reader = self.reader
        bits = _local_bits(len(palette))
        refs = [palette[0]] * (width * height)

        def node(x: int, y: int, w: int, h: int) -> None:
            if reader.bits(1):
                colour = _pick(palette, reader.bits(bits))
                for row in range(y, y + h):
                    start = row * width + x
                    refs[start : start + w] = [colour] * w
                return
            if w == 1 and h == 1:
                raise _Bad
            if w == 1:
                top = h // 2
                node(x, y, 1, top)
                node(x, y + top, 1, h - top)
            elif h == 1:
                left = w // 2
                node(x, y, left, 1)
                node(x + left, y, w - left, 1)
            else:
                left, top = w // 2, h // 2
                node(x, y, left, top)
                node(x + left, y, w - left, top)
                node(x, y + top, left, h - top)
                node(x + left, y + top, w - left, h - top)

        node(0, 0, width, height)
        return refs

    def bitplanes(self, count: int, bits: int) -> list[int]:
        """Decode ``bitplanes`` to local indices: each bit plane raw or in runs."""
        reader = self.reader
        values = [0] * count
        for plane in range(bits):
            mask = 1 << plane
            if not reader.bits(1):
                values = _set_flags(values, reader.flags(count), mask)
            else:
                self.plane_runs(values, count, mask, short=False)
        return values

    def adaptive_bitplanes(self, count: int, palette: list[int]) -> list[int]:
        """Decode ``adaptiveBitplanes``. Each bit plane has a prefix that gives its form.

        The prefixes, in the order of the data: 0 raw, 10 runs, 110 short runs, 11100 all
        zero, 11110 all one, 11101 a list of the one bits, 11111 a list of the zero bits.
        """
        reader = self.reader
        values = [0] * count
        for plane in range(_local_bits(len(palette))):
            mask = 1 << plane
            if not reader.bits(1):
                values = _set_flags(values, reader.flags(count), mask)
            elif not reader.bits(1):
                self.plane_runs(values, count, mask, short=False)
            elif not reader.bits(1):
                self.plane_runs(values, count, mask, short=True)
            else:
                form = reader.bits(2)
                if form & 1:  # all one, or all one and then a list of the zero bits
                    values = [value | mask for value in values]
                if form & 2:
                    self.plane_positions(values, count, mask, set_bit=form == 2)
        return _pick_all(palette, values)

    def plane_runs(self, values: list[int], count: int, mask: int, *, short: bool) -> None:
        """Read the runs of one bit plane. The bit value changes after each run."""
        reader = self.reader
        bit = reader.bits(1)
        position = 0
        while position < count:
            remaining = count - position
            length = self.short_run(remaining) if short else reader.range_compact(remaining - 1) + 1
            if bit:
                values[position : position + length] = [
                    value | mask for value in values[position : position + length]
                ]
            position += length
            bit ^= 1

    def short_run(self, remaining: int) -> int:
        """The length of a short run: 0 is 1, 10 is 2, 110 is 3, 111 and a number is 4 or more."""
        reader = self.reader
        for length in (1, 2, 3):
            if not reader.bits(1):
                if length > remaining:
                    raise _Bad
                return length
        return reader.range_compact(remaining - 4) + 4

    def plane_positions(self, values: list[int], count: int, mask: int, *, set_bit: bool) -> None:
        """Read the list of positions of a sparse bit plane, and set or clear their bit."""
        reader = self.reader
        total = reader.range_compact(count - 1) + 1
        previous = -1
        for index in range(total):
            later = total - index - 1
            position = previous + reader.range_compact(count - previous - later - 2) + 1
            if set_bit:
                values[position] |= mask
            else:
                values[position] &= ~mask
            previous = position

    def row_repeat(self, count: int, row_length: int, bits: int) -> list[int]:
        """Decode ``rowRepeat``: the first row, then each row is a copy (bit 1) or raw."""
        reader = self.reader
        values = [reader.bits(bits) for _ in range(row_length)]
        for _ in range(count // row_length - 1):
            if reader.bits(1):
                values.extend(values[-row_length:])
            else:
                values.extend(reader.bits(bits) for _ in range(row_length))
        return values

    def row_delta(self, count: int, row_length: int, bits: int, maximum: int) -> list[int]:
        """Decode ``rowDelta``: each row is raw, or a change of the predicted row.

        Operations (2 bits): 0 raw, 1 repeat, 2 changes at listed positions, 3 extended.
        Extended operations (2 bits): 0 a mask and a value for each set bit, 1 segments,
        2 a mask and one value for all set bits, 3 a run of repeated rows.
        """
        reader = self.reader
        rows = count // row_length
        virtual = bool(reader.bits(1))
        shifts = bool(reader.bits(1))
        values: list[int] = []

        def value() -> int:
            result = reader.bits(bits)
            if result > maximum:
                raise _Bad
            return result

        def predictor(row: int) -> int:
            if not shifts:
                return 0
            return self.predictor(row, virtual)

        if not virtual:
            values.extend(value() for _ in range(row_length))
        position_bits = (row_length - 1).bit_length()
        while len(values) < count:
            row = len(values) // row_length
            op = reader.bits(2)
            if op == 0:
                values.extend(value() for _ in range(row_length))
                continue
            if op == 1:
                values.extend(_predicted(values, row_length, row, 0, virtual))
                continue
            if op == 2:
                current = _predicted(values, row_length, row, predictor(row), virtual)
                changes = reader.bits(row_length.bit_length())
                if changes > row_length:
                    raise _Bad
                previous = -1
                for _ in range(changes):
                    x = reader.bits(position_bits)
                    if x >= row_length or x <= previous:
                        raise _Bad
                    current[x] = value()
                    previous = x
                values.extend(current)
                continue
            extended = reader.bits(2)
            if extended == 3:
                if row + 2 > rows:
                    raise _Bad
                for _ in range(reader.range_compact(rows - row - 2) + 2):
                    values.extend(_predicted(values, row_length, row, 0, virtual))
                    row += 1
                continue
            current = _predicted(values, row_length, row, predictor(row), virtual)
            if extended == 1:
                segments = reader.bits(position_bits) + 1
                previous_end = -1
                for _ in range(segments):
                    start = reader.bits(position_bits)
                    length = reader.bits(position_bits) + 1
                    if start <= previous_end or start + length > row_length:
                        raise _Bad
                    for x in range(start, start + length):
                        current[x] = value()
                    previous_end = start + length - 1
            else:
                positions = [x for x, flag in enumerate(reader.flags(row_length)) if flag]
                if not positions:
                    raise _Bad
                shared = value() if extended == 2 else None
                for x in positions:
                    current[x] = value() if shared is None else shared
            values.extend(current)
        return values

    def compact_row_delta(
        self, count: int, row_length: int, bits: int, maximum: int, *, residual: bool
    ) -> list[int]:
        """Decode ``compactRowDelta`` or ``directRowDelta``.

        Operations (3 bits): 0 repeat, 1 raw, 2 changes at listed positions, 3 one value
        at listed positions, 4 segments, 5 a trimmed mask, 6 a run of repeated rows, 7 the
        predicted row with no change. A grey ``directRowDelta`` has ``residual``: then
        operations 2 to 5 have one bit that selects values as differences from the
        predicted row.

        For operation 3 with differences, the value at the first position is the value at
        all positions. The reference decoder does this.
        """
        reader = self.reader
        rows = count // row_length
        virtual = bool(reader.bits(1))
        values: list[int] = []

        def raw_value() -> int:
            result = reader.bits(bits)
            if result > maximum:
                raise _Bad
            return result

        if not virtual:
            values.extend(raw_value() for _ in range(row_length))
        while len(values) < count:
            row = len(values) // row_length
            op = reader.bits(3)
            if op in (0, 6):
                repeats = 1 if op == 0 else reader.range_compact(rows - row - 2) + 2
                for _ in range(repeats):
                    values.extend(_predicted(values, row_length, row, 0, virtual))
                    row += 1
                continue
            if op == 1:
                values.extend(raw_value() for _ in range(row_length))
                continue
            guess = _predicted(values, row_length, row, self.predictor(row, virtual), virtual)
            current = list(guess)
            if op == 7:
                values.extend(current)
                continue
            differences = residual and bool(reader.bits(1))

            def changed(x: int, differences: bool = differences, guess: list[int] = guess) -> int:
                if not differences:
                    return raw_value()
                code = reader.compact() + 1
                result = guess[x] + ((code + 1) // 2 if code % 2 else -(code // 2))
                if result < 0 or result > maximum:
                    raise _Bad
                return result

            if op in (2, 3):
                positions = self.change_positions(row_length)
                if op == 3:
                    one = changed(positions[0])
                    for x in positions:
                        current[x] = one
                else:
                    for x in positions:
                        current[x] = changed(x)
            else:
                positions = (
                    self.segment_positions(row_length)
                    if op == 4
                    else self.mask_positions(row_length)
                )
                for x in positions:
                    current[x] = changed(x)
            values.extend(current)
        return values

    def predictor(self, row: int, virtual: bool) -> int:
        """A row predictor: 0 is ``same``, 10 is ``left``, 11 is ``right``.

        The first row on the virtual row of zeros permits only ``same``.
        """
        reader = self.reader
        result = 0 if not reader.bits(1) else 1 + reader.bits(1)
        if result and row == 0 and virtual:
            raise _Bad
        return result

    def change_positions(self, row_length: int) -> list[int]:
        """The positions of ``compactRowDelta`` operations 2 and 3: a count, then gaps."""
        reader = self.reader
        total = reader.range_compact(row_length - 1) + 1
        positions = []
        previous = -1
        for index in range(total):
            later = total - index - 1
            previous += 1 + reader.range_compact(row_length - previous - later - 2)
            positions.append(previous)
        return positions

    def segment_positions(self, row_length: int) -> list[int]:
        """The positions of ``compactRowDelta`` operation 4: segments, each a gap and a length."""
        reader = self.reader
        total = reader.range_compact(row_length - 1) + 1
        positions: list[int] = []
        previous_end = 0
        for index in range(total):
            later = total - index - 1
            start = previous_end + reader.range_compact(row_length - previous_end - later - 1)
            length = reader.range_compact(row_length - start - later - 1) + 1
            if start + length > row_length:
                raise _Bad
            positions.extend(range(start, start + length))
            previous_end = start + length
        return positions

    def mask_positions(self, row_length: int) -> list[int]:
        """The positions of ``compactRowDelta`` operation 5: a start, a span, and a mask."""
        reader = self.reader
        start = reader.range_compact(row_length - 1)
        span = reader.range_compact(row_length - start - 1) + 1
        positions = [start + offset for offset, flag in enumerate(reader.flags(span)) if flag]
        if not positions:
            raise _Bad
        return positions


def _predicted(
    values: list[int], row_length: int, row: int, predictor: int, virtual: bool
) -> list[int]:
    """The predicted row: the previous row, moved one place left or right with wrap.

    ``left[x]`` is ``previous[x - 1]``, and ``right[x]`` is ``previous[x + 1]``. Row 0 on a
    virtual base has only zeros before it.
    """
    if row == 0 and virtual:
        return [0] * row_length
    previous = values[(row - 1) * row_length : row * row_length]
    if predictor == 1:
        return previous[-1:] + previous[:-1]
    if predictor == 2:
        return previous[1:] + previous[:1]
    return previous


def _set_flags(values: list[int], flags: list[bool], mask: int) -> list[int]:
    """Set the ``mask`` bit in each value whose flag is set."""
    return [value | mask if flag else value for value, flag in zip(values, flags, strict=True)]


def _paste(refs: list[int], width: int, box: tuple[int, int, int, int], block: list[int]) -> None:
    """Copy a row-major block into the image at its rectangle."""
    x, y, w, h = box
    for row in range(h):
        start = (y + row) * width + x
        refs[start : start + w] = block[row * w : (row + 1) * w]
