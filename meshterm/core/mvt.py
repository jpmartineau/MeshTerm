# SPDX-License-Identifier: Apache-2.0
"""A tiny, dependency-free Mapbox Vector Tile (MVT) decoder.

Vector tiles are Protocol-Buffer messages (spec: github.com/mapbox/vector-tile-spec). Rather
than pull in a protobuf runtime and a geometry library, this decodes the small subset the map
needs by hand: the protobuf wire format (varints, length-delimited fields) and the MVT
geometry command encoding (MoveTo/LineTo/ClosePath with zig-zag deltas). It is pure and
offline — the network fetch lives in :mod:`meshterm.services.basemap`.

The result is a list of :class:`Layer`, each with decoded :class:`Feature` geometry in
*tile-local* integer coordinates (``0..extent``) and a resolved ``tags`` dict, ready to be
projected to the screen by :class:`meshterm.core.geo.Viewport`.
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

# Geometry command ids (low 3 bits of a command integer).
_CMD_MOVE_TO = 1
_CMD_LINE_TO = 2
_CMD_CLOSE = 7


class _Reader:
    """A minimal protobuf wire-format reader over a byte buffer."""

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
        """Read a field tag, returning ``(field_number, wire_type)``."""
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
            # Step over the bytes rather than slicing them out: skipping is how the
            # decoder walks past everything it doesn't want, and a discarded copy of
            # every feature in an undrawn layer is the bulk of that cost. The length
            # must land in a temporary first — ``self.i += self.varint()`` would add to
            # the offset as it was *before* the varint moved it.
            n = self.varint()
            self.i += n
        elif wire_type == 5:
            self.i += 4
        elif wire_type == 1:
            self.i += 8
        else:  # pragma: no cover - groups are obsolete and never appear in MVT
            raise ValueError(f"unsupported wire type {wire_type}")


def _zigzag(n: int) -> int:
    """Decode a protobuf zig-zag encoded signed integer."""
    return (n >> 1) ^ -(n & 1)


@dataclass(slots=True)
class Feature:
    """One decoded vector-tile feature.

    Attributes:
        geom_type: :data:`GEOM_POINT`, :data:`GEOM_LINE`, or :data:`GEOM_POLYGON`.
        rings: The geometry as a list of parts, each a list of ``(x, y)`` integer points in
            tile-local coordinates (``0..extent``). Points have one part holding every point;
            lines/polygons have one part per line-string / ring.
        tags: Resolved attribute dict (e.g. ``{"class": "primary", "name": "Rue X"}``).
    """

    geom_type: int
    rings: list[list[tuple[int, int]]]
    tags: dict[str, Any]

    def get(self, key: str, default: Any = None) -> Any:
        """Return a tag value, or ``default`` if absent."""
        return self.tags.get(key, default)

    @property
    def name(self) -> str | None:
        """The feature's display name, preferring a romanized form over the local script.

        The terminal map draws labels in a fixed-width cell grid with whatever font the
        user has, so a local-script name (CJK, Arabic, Thai…) tends to render as tofu or,
        being double-width, shove the row out of alignment. OpenMapTiles ships a
        transliterated ``name:latin`` (and often ``name:en``) beside the local ``name``, so
        prefer those; fall back to the local ``name`` only when no latin form exists.
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
        name: Layer id (e.g. ``transportation``, ``water``, ``place``).
        extent: The tile's internal coordinate extent (typically 4096).
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
                # Each MoveTo starts a new part (multipoint keeps them in one part below).
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
    """Decode a single Feature message, resolving its tags against the layer's key/value pools."""
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
    # A malformed tile may carry an odd tag list; drop the dangling key rather than
    # raise — the rest of this decoder is deliberately lenient about bad tiles.
    for k, v in zip(tag_ints[0::2], tag_ints[1::2], strict=False):
        if 0 <= k < len(keys) and 0 <= v < len(values):
            tags[keys[k]] = values[v]
    return Feature(geom_type=geom_type, rings=_decode_geometry(geom_ints), tags=tags)


def _decode_layer(buf: bytes, wanted: Container[str] | None = None) -> Layer:
    """Decode a single Layer message, and its features unless the caller doesn't want them.

    The name arrives in field 1, which in practice is the first thing written, so once it
    has been read the walk already knows whether any of the rest is worth keeping — from
    there an unwanted layer is stepped over rather than copied out. Protobuf permits any
    field order though, so ``skipping`` is only ever an optimisation: the decision is the
    check after the loop, which is correct however the fields arrived (and covers a layer
    that declares no name at all).

    Args:
        buf: The Layer message's bytes.
        wanted: Layer names worth decoding features for; ``None`` decodes every layer.

    Returns:
        The layer. One the caller didn't ask for comes back named and empty.
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
    """Decode a vector tile (optionally gzip-compressed) into its layers.

    Args:
        data: Raw ``.pbf`` bytes, gzip-compressed or not.
        layers: When given, only layers whose name is in it are decoded. The others are
            still returned — named, with their extent, and no features — so the result
            still describes the whole tile and a caller can tell a real tile from junk
            without paying for geometry it will never draw. ``None`` decodes everything.

    Returns:
        The tile's layers, in the order they appear in it. An empty tile yields an empty
        list rather than raising; truncated or junk bytes raise, which is what lets
        :class:`~meshterm.services.basemap.BasemapSource` tell a corrupt cache entry from
        a tile that simply holds nothing it draws.
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


# -- the decoded form, for callers that would rather not decode twice ------------------

#: Bumped whenever :func:`dumps_layers` changes the shape it writes.
_WIRE_VERSION = 1


def dumps_layers(layers: list[Layer], *, stamp: str = "") -> bytes:
    """Serialise decoded layers to bytes that :func:`loads_layers` can restore.

    Decoding a vector tile is by far the most expensive thing this app does (a 153 KB
    tile costs ~365 ms on the PicoCalc's Cortex-A7 even after the layer narrowing), and
    the result is a pure function of the bytes and the layer set. Writing it down means a
    later session pays a ``marshal`` load and some object construction — measured ~14x
    cheaper — instead of parsing the protobuf again.

    ``marshal`` is the format because it is stdlib, C-speed, and understands the plain
    tuples/lists/dicts/scalars a decoded tile reduces to. It is deliberately *not*
    ``pickle``: this reads a file off disk, and marshal cannot be made to import a module
    or call a constructor. It is still only safe against data we wrote ourselves, which
    is why the cache lives under the app's own directory and why every load is guarded —
    see :func:`loads_layers`.

    Args:
        layers: The decoded layers to write down.
        stamp: An opaque caller token describing *how* these were decoded (the layer set,
            typically). :func:`loads_layers` refuses a blob whose stamp differs, which is
            what stops a narrowed decode from being served to a caller that wants more.

    Returns:
        The encoded bytes.
    """
    payload = [
        (layer.name, layer.extent, [(f.geom_type, f.rings, f.tags) for f in layer.features])
        for layer in layers
    ]
    return marshal.dumps((_WIRE_VERSION, marshal.version, stamp, payload))


def loads_layers(blob: bytes, *, stamp: str = "") -> list[Layer] | None:
    """Restore layers written by :func:`dumps_layers`, or ``None`` if they can't be used.

    Every way the blob can fail to be what this build expects — a bumped wire version, a
    Python whose ``marshal`` writes a different format, a different layer set, a file
    truncated by a power cut — resolves to ``None``, meaning "decode the tile again".
    Nothing here raises into the map: a derived cache that can't be read is not an error,
    it is just a cache miss.

    Args:
        blob: Bytes previously produced by :func:`dumps_layers`.
        stamp: The token the caller expects; a blob written under any other is rejected.

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


# -- what a decoded tile weighs ------------------------------------------------------

#: What a decoded tile costs in RAM, in pointer widths: per point (its ``(x, y)`` tuple,
#: the ints in it, its slot in the ring), per feature (the object, its tag dict, its ring
#: lists), and per tag. Fitted by least squares against ``tracemalloc`` over every tile in
#: the PicoCalc's cache (32-bit) and the same tiles decoded on a 64-bit desktop, which came
#: out at the same counts of words to within a few percent: 15-16 a point, 42-44 a
#: feature, 1.5-2 a tag.
_POINT_WORDS = 16
_FEATURE_WORDS = 44
_TAG_WORDS = 2

#: A pointer, in bytes — the unit the above are counted in. A tag's string *value* costs
#: half of one per UTF-8 byte (2.1 bytes measured on the device, 3.4 on the desktop).
_WORD = struct.calcsize("P")


def resident_bytes(layers: list[Layer]) -> int:
    """Roughly how much RAM ``layers`` hold, for a cache that must budget in bytes.

    A tile's weight is no use counted in *tiles*: on the PicoCalc a decoded tile runs from
    1 MB (a suburb at z10) to 4.6 MB (a coastline at z7), and a cache bounded by count held
    50-odd MB of them on a 100 MB device, which ended in the SD-card swap — the map frozen
    for fifteen seconds while the kernel paged the interpreter back in.

    Points alone are not the measure either: a zoomed-out tile is mostly place labels, each
    carrying its name in dozens of languages, and a z2 tile weighing 1.6 MB has almost no
    geometry at all. So the tags count too, and their text. The walk never touches a
    coordinate, so it is cheap beside the decode that produced the layers.

    An estimate: on the device's 157 cached tiles it lands from 1% under to 9% over what
    ``tracemalloc`` measures, and from 6% under to 21% over on a 64-bit desktop.
    """
    points = features = tags = text = 0
    for layer in layers:
        for feat in layer.features:
            features += 1
            tags += len(feat.tags)
            for value in feat.tags.values():
                if isinstance(value, str):
                    # Bytes, not characters: a name in another script is stored wider,
                    # and those are what fill a zoomed-out tile.
                    text += len(value.encode())
            for ring in feat.rings:
                points += len(ring)
    words = points * _POINT_WORDS + features * _FEATURE_WORDS + tags * _TAG_WORDS
    return words * _WORD + text * _WORD // 2
