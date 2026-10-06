# SPDX-License-Identifier: Apache-2.0
"""A very small Mapbox Vector Tile (MVT) decoder, with no dependencies.

Vector tiles are Protocol Buffer messages (spec: github.com/mapbox/vector-tile-spec). This
module does not add a protobuf runtime and a geometry library. Instead, it decodes by hand
the small subset that the map needs: the protobuf wire format (varints, length-delimited
fields) and the MVT geometry command encoding (MoveTo, LineTo, and ClosePath with zig-zag
deltas). It is pure and works offline. The network download is in
:mod:`meshterm.services.basemap`.

The result is a list of :class:`Layer`. Each layer has decoded :class:`Feature` geometry in
local tile integer coordinates (``0..extent``) and a resolved ``tags`` dict.
:class:`meshterm.core.geo.Viewport` can then project them to the screen.
"""

from __future__ import annotations

import gzip
import marshal
import struct
from collections.abc import Container
from dataclasses import dataclass, field
from typing import Any

# MVT geometry types (Feature.type).
GEOM_POINT = 1
GEOM_LINE = 2
GEOM_POLYGON = 3

# Geometry command ids (the low 3 bits of a command integer).
_CMD_MOVE_TO = 1
_CMD_LINE_TO = 2
_CMD_CLOSE = 7


class _Reader:
    """A minimal protobuf wire-format reader on a byte buffer."""

    __slots__ = ("b", "i")

    def __init__(self, b: bytes) -> None:
        self.b = b
        self.i = 0

    def eof(self) -> bool:
        return self.i >= len(self.b)

    def varint(self) -> int:
        """Read a base-128 varint."""
        result = shift = 0
        while True:
            byte = self.b[self.i]
            self.i += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7

    def tag(self) -> tuple[int, int]:
        """Read a field tag, and return ``(field_number, wire_type)``."""
        key = self.varint()
        return key >> 3, key & 0x7

    def blob(self) -> bytes:
        """Read a length-delimited byte string."""
        n = self.varint()
        v = self.b[self.i : self.i + n]
        self.i += n
        return v

    def skip(self, wire_type: int) -> None:
        """Skip a field of the given wire type."""
        if wire_type == 0:
            self.varint()
        elif wire_type == 2:
            # Move past the bytes, and do not slice them out. The decoder uses this skip to
            # walk past all that it does not want, and a discarded copy of each feature in
            # a layer that is not drawn is most of that cost. The length must go into a
            # temporary variable first: ``self.i += self.varint()`` adds to the offset as
            # it was before the varint moved it.
            n = self.varint()
            self.i += n
        elif wire_type == 5:
            self.i += 4
        elif wire_type == 1:
            self.i += 8
        else:  # pragma: no cover - groups are obsolete and never appear in MVT
            raise ValueError(f"unsupported wire type {wire_type}")


def _zigzag(n: int) -> int:
    """Decode a signed integer in the protobuf zig-zag encoding."""
    return (n >> 1) ^ -(n & 1)


@dataclass(slots=True)
class Feature:
    """One decoded vector-tile feature.

    Attributes:
        geom_type: :data:`GEOM_POINT`, :data:`GEOM_LINE`, or :data:`GEOM_POLYGON`.
        rings: The geometry as a list of parts. Each part is a list of ``(x, y)`` integer
            points in local tile coordinates (``0..extent``). Points have one part that
            holds all the points. Lines and polygons have one part for each line string or
            ring.
        tags: The resolved attribute dict (for example,
            ``{"class": "primary", "name": "Rue X"}``).
    """

    geom_type: int
    rings: list[list[tuple[int, int]]]
    tags: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        """Return a tag value, or ``default`` if it is absent."""
        return self.tags.get(key, default)

    @property
    def name(self) -> str | None:
        """The name to show for the feature, with a romanized form before the local script.

        The terminal map draws labels in a grid of fixed-width cells, with the font that the
        user has. Thus a name in a local script (CJK, Arabic, Thai, and other scripts) often
        renders as tofu. Or, because it is double-width, it pushes the row out of alignment.
        OpenMapTiles has a transliterated ``name:latin`` (and often ``name:en``) next to the
        local ``name``, so use those first. Use the local ``name`` only when no Latin form
        exists.
        """
        for key in ("name:latin", "name:en", "name_en", "name_int", "name"):
            val = self.tags.get(key)
            if val:
                return str(val)
        return None


@dataclass(slots=True)
class Layer:
    """A named vector-tile layer and its decoded features.

    Attributes:
        name: The layer id (for example, ``transportation``, ``water``, ``place``).
        extent: The internal coordinate extent of the tile (usually 4096).
        features: The decoded features.
    """

    name: str
    extent: int
    features: list[Feature] = field(default_factory=list)


def _decode_geometry(cmds: list[int]) -> list[list[tuple[int, int]]]:
    """Decode an MVT geometry command stream into a list of point rings."""
    rings: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    x = y = 0
    i = 0
    n = len(cmds)
    while i < n:
        command = cmds[i]
        i += 1
        cmd_id = command & 0x7
        count = command >> 3
        if cmd_id == _CMD_MOVE_TO:
            for _ in range(count):
                x += _zigzag(cmds[i])
                y += _zigzag(cmds[i + 1])
                i += 2
                # Each MoveTo starts a new part (a multipoint keeps them in one part below).
                if current:
                    rings.append(current)
                current = [(x, y)]
        elif cmd_id == _CMD_LINE_TO:
            for _ in range(count):
                x += _zigzag(cmds[i])
                y += _zigzag(cmds[i + 1])
                i += 2
                current.append((x, y))
        elif cmd_id == _CMD_CLOSE:
            if current:
                current.append(current[0])  # close the ring back to its start
    if current:
        rings.append(current)
    return rings


def _decode_value(buf: bytes) -> Any:
    """Decode an MVT attribute Value message into a Python scalar."""
    r = _Reader(buf)
    while not r.eof():
        field_no, wire = r.tag()
        if field_no == 1 and wire == 2:
            return r.blob().decode("utf-8", "ignore")  # string_value
        if field_no == 2 and wire == 5:  # float_value
            import struct

            v = struct.unpack("<f", r.b[r.i : r.i + 4])[0]
            r.i += 4
            return v
        if field_no == 3 and wire == 1:  # double_value
            import struct

            v = struct.unpack("<d", r.b[r.i : r.i + 8])[0]
            r.i += 8
            return v
        if field_no in (4, 5) and wire == 0:  # int_value / uint_value
            return r.varint()
        if field_no == 6 and wire == 0:  # sint_value
            return _zigzag(r.varint())
        if field_no == 7 and wire == 0:  # bool_value
            return bool(r.varint())
        r.skip(wire)
    return None


def _packed_uints(buf: bytes) -> list[int]:
    """Decode a packed repeated uint32 field into a list of ints."""
    r = _Reader(buf)
    out: list[int] = []
    while not r.eof():
        out.append(r.varint())
    return out


def _decode_feature(buf: bytes, keys: list[str], values: list[Any]) -> Feature | None:
    """Decode one Feature message, and resolve its tags with the layer key and value pools."""
    r = _Reader(buf)
    geom_type = 0
    tag_ints: list[int] = []
    geom_ints: list[int] = []
    while not r.eof():
        field_no, wire = r.tag()
        if field_no == 2 and wire == 2:
            tag_ints = _packed_uints(r.blob())
        elif field_no == 3 and wire == 0:
            geom_type = r.varint()
        elif field_no == 4 and wire == 2:
            geom_ints = _packed_uints(r.blob())
        else:
            r.skip(wire)
    if not geom_ints:
        return None
    tags: dict[str, Any] = {}
    # A malformed tile can have an odd tag list. Ignore the last key, which has no value,
    # and do not raise: the remainder of this decoder is lenient about bad tiles on purpose.
    for k, v in zip(tag_ints[0::2], tag_ints[1::2], strict=False):
        if 0 <= k < len(keys) and 0 <= v < len(values):
            tags[keys[k]] = values[v]
    return Feature(geom_type=geom_type, rings=_decode_geometry(geom_ints), tags=tags)


def _decode_layer(buf: bytes, wanted: Container[str] | None = None) -> Layer:
    """Decode one Layer message, and its features if the caller wants them.

    The name arrives in field 1, which is usually the first field written. Thus, after the
    walk reads the name, it already knows whether the remainder is worth a copy. From there,
    the walk steps over an unwanted layer, and does not copy it out. But protobuf permits
    any field order, so ``skipping`` is only an optimization. The decision is the check
    after the loop, which is correct in whatever order the fields arrived (and also for a
    layer that declares no name at all).

    Args:
        buf: The bytes of the Layer message.
        wanted: The layer names for which to decode features. ``None`` decodes each layer.

    Returns:
        The layer. A layer that the caller did not ask for is returned with its name and
        no features.
    """
    r = _Reader(buf)
    name = ""
    extent = 4096
    keys: list[str] = []
    values: list[Any] = []
    feature_blobs: list[bytes] = []
    skipping = False
    while not r.eof():
        field_no, wire = r.tag()
        if field_no == 1 and wire == 2:
            name = r.blob().decode("utf-8", "ignore")
            skipping = wanted is not None and name not in wanted
        elif field_no == 5 and wire == 0:
            extent = r.varint()
        elif skipping:
            r.skip(wire)
        elif field_no == 2 and wire == 2:
            feature_blobs.append(r.blob())
        elif field_no == 3 and wire == 2:
            keys.append(r.blob().decode("utf-8", "ignore"))
        elif field_no == 4 and wire == 2:
            values.append(_decode_value(r.blob()))
        else:
            r.skip(wire)
    layer = Layer(name=name, extent=extent)
    if wanted is not None and name not in wanted:
        return layer
    for blob in feature_blobs:
        feat = _decode_feature(blob, keys, values)
        if feat is not None:
            layer.features.append(feat)
    return layer


def decode_tile(data: bytes, *, layers: Container[str] | None = None) -> list[Layer]:
    """Decode a vector tile (gzip-compressed or not) into its layers.

    Args:
        data: The raw ``.pbf`` bytes, gzip-compressed or not.
        layers: When given, only the layers whose name is in it are decoded. The function
            still returns the other layers, with their name, their extent, and no
            features. Thus the result still describes the full tile, and a caller can tell
            a real tile from junk without the cost of geometry that it will never draw.
            ``None`` decodes all the layers.

    Returns:
        The layers of the tile, in their order in the tile. An empty tile gives an empty
        list, and does not raise. Truncated bytes or junk bytes raise. With this,
        :class:`~meshterm.services.basemap.BasemapSource` can tell a corrupt cache entry
        from a tile that holds nothing that it draws.
    """
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    r = _Reader(data)
    out: list[Layer] = []
    while not r.eof():
        field_no, wire = r.tag()
        if field_no == 3 and wire == 2:  # Tile.layers
            out.append(_decode_layer(r.blob(), layers))
        else:
            r.skip(wire)
    return out


# -- the decoded form, for callers that do not want to decode a tile two times ---------

#: Increased each time :func:`dumps_layers` changes the structure that it writes.
_WIRE_VERSION = 1


def dumps_layers(layers: list[Layer], *, stamp: str = "") -> bytes:
    """Serialize decoded layers to bytes that :func:`loads_layers` can restore.

    The decode of a vector tile is by far the most expensive operation of this app. A
    153 KB tile costs ~365 ms on the Cortex-A7 of the PicoCalc, even after the layer
    narrowing. The result depends only on the bytes and the layer set. When MeshTerm writes
    the result down, a later session pays for a ``marshal.loads`` call and some object
    construction (measured as ~14x cheaper), and does not parse the protobuf again.

    The format is ``marshal`` because it is in the standard library, it runs at C speed,
    and it understands the plain tuples, lists, dicts, and scalars to which a decoded tile
    reduces. It is not ``pickle``, on purpose: the cache reads a file from disk, and
    marshal cannot be made to import a module or call a constructor. It is still safe only
    for data that we wrote ourselves. This is why the cache is in the directory of the app
    itself, and why each read is guarded (refer to :func:`loads_layers`).

    Args:
        layers: The decoded layers to write down.
        stamp: An opaque token from the caller that describes how these layers were
            decoded (usually the layer set). :func:`loads_layers` refuses a blob with a
            different stamp. This stops a narrowed decode from going to a caller that
            wants more.

    Returns:
        The encoded bytes.
    """
    payload = [
        (layer.name, layer.extent, [(f.geom_type, f.rings, f.tags) for f in layer.features])
        for layer in layers
    ]
    return marshal.dumps((_WIRE_VERSION, marshal.version, stamp, payload))


def loads_layers(blob: bytes, *, stamp: str = "") -> list[Layer] | None:
    """Restore layers that :func:`dumps_layers` wrote, or ``None`` if they cannot be used.

    The blob can be different from what this build expects in many ways: a newer wire
    version, a Python whose ``marshal`` writes a different format, a different layer set,
    or a file that a power failure truncated. Each of these gives ``None``, which means
    "decode the tile again". Nothing here raises into the map: a derived cache that cannot
    be read is not an error. It is only a cache miss.

    Args:
        blob: Bytes that :func:`dumps_layers` made before.
        stamp: The token that the caller expects. A blob written with a different token
            is rejected.

    Returns:
        The layers, or ``None`` to mean "re-decode".
    """
    try:
        version, marshal_version, written_stamp, payload = marshal.loads(blob)
        if (version, marshal_version, written_stamp) != (_WIRE_VERSION, marshal.version, stamp):
            return None
        return [
            Layer(
                name=name,
                extent=extent,
                features=[Feature(geom_type=g, rings=r, tags=t) for g, r, t in feats],
            )
            for name, extent, feats in payload
        ]
    except Exception:  # noqa: BLE001 - any malformed blob is simply a cache miss
        return None


# -- the memory size of a decoded tile ------------------------------------------------

#: The RAM cost of a decoded tile, in pointer widths: for each point (its ``(x, y)`` tuple,
#: the ints in it, its slot in the ring), for each feature (the object, its tag dict, its
#: ring lists), and for each tag. Fitted by least squares against ``tracemalloc`` over all
#: the tiles in the cache of the PicoCalc (32-bit), and over the same tiles decoded on a
#: 64-bit desktop. The two gave the same counts of words, to within a few percent: 15-16
#: for a point, 42-44 for a feature, 1.5-2 for a tag.
_POINT_WORDS = 16
_FEATURE_WORDS = 44
_TAG_WORDS = 2

#: A pointer, in bytes: the unit of the counts above. The string value of a tag costs half
#: of one pointer for each UTF-8 byte (2.1 bytes measured on the handheld, 3.4 on the
#: desktop).
_WORD = struct.calcsize("P")


def resident_bytes(layers: list[Layer]) -> int:
    """Approximately how much RAM ``layers`` hold, for a cache that has a budget in bytes.

    A count of tiles is no use as a measure of the weight of tiles. On the PicoCalc, a
    decoded tile is from 1 MB (a suburb at z10) to 4.6 MB (a coastline at z7). A cache that
    a count limited held a little more than 50 MB of tiles on a handheld with 100 MB. The
    result was the SD-card swap: the map froze for fifteen seconds while the kernel paged
    the interpreter back in.

    Points alone are also not the measure. A zoomed-out tile is mostly place labels, and
    each label carries its name in dozens of languages. A z2 tile of 1.6 MB has almost no
    geometry at all. Thus the tags count too, and their text. The walk never touches a
    coordinate, so its cost is small compared to the decode that made the layers.

    This is an estimate. On the 157 cached tiles of the handheld, it is from 1% under to 9%
    over what ``tracemalloc`` measures. On a 64-bit desktop, it is from 6% under to 21%
    over.
    """
    points = features = tags = text = 0
    for layer in layers:
        for feat in layer.features:
            features += 1
            tags += len(feat.tags)
            for value in feat.tags.values():
                if isinstance(value, str):
                    # Bytes, not characters: a name in a different script is stored
                    # wider, and these names fill a zoomed-out tile.
                    text += len(value.encode())
            for ring in feat.rings:
                points += len(ring)
    words = points * _POINT_WORDS + features * _FEATURE_WORDS + tags * _TAG_WORDS
    return words * _WORD + text * _WORD // 2
