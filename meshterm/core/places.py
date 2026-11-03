# SPDX-License-Identifier: Apache-2.0
"""The place of a node: the named area nearest its location, in pure functions.

Many nodes have a name that says nothing about where they are: a call sign (``VE2XYZ``) or
the name of the hardware (``RAK-1W``). A node that advertises a location has a better answer
near it on the map. This module finds that answer, the **place** of the node: a
neighbourhood, a village, a town, or a peak. It has no I/O. A caller gives it the decoded
tiles (:func:`place_from_tiles`) or the document that Nominatim returned
(:func:`place_from_nominatim`), and it returns a :class:`Place`.

A place has two parts. The **fine** part is the most precise name: a neighbourhood in a
city, or the village itself in the country. The **coarse** part is the town or the city
that holds a fine part that is not itself a town: ``Villeray, Montréal``. A village, a town,
a city, and a peak stand alone. A path chip shows only the fine part, because a chip must be
short (:attr:`Place.short`).

**The tiles give points, not areas.** The OpenMapTiles ``place`` layer has one point for
each place, where its label goes. It has no boundary. Thus the tiles can tell the nearest
place, but not the place that contains a node. Near a border, the answer can be the
neighbouring place. :func:`place_from_tiles` limits this error: each class has a radius
past which its point no longer names the node, and the candidate that is nearest relative
to its own radius wins. Nominatim knows the boundaries, and when the preference permits,
its answer replaces the answer of the tiles (refer to :mod:`meshterm.services.places`).

**A peak wins near its summit.** Many repeaters are on a summit, and there the name of the
peak tells more than the name of the town below it. A named peak within
:data:`PEAK_RADIUS_KM` of the node is the place. Nominatim does not replace such a place.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .geo import TILE_PX, haversine_km, lonlat_to_world, usable_fix, world_to_lonlat

if TYPE_CHECKING:
    from .mvt import Layer

    #: Loads the decoded layers of one tile ``(z, x, y)``, or ``None`` when it has no answer.
    TileLoader = Callable[[int, int, int], list[Layer] | None]

#: Where a place came from. ``tiles`` and ``nominatim`` are found. ``user`` is the place
#: that the user typed for a node, and it wins over a found place.
SOURCE_TILES = "tiles"
SOURCE_NOMINATIM = "nominatim"
SOURCE_USER = "user"

#: The layers of a tile that :func:`place_from_tiles` reads.
PLACE_LAYERS = frozenset({"place", "mountain_peak"})

#: The distance (km) within which a named peak is the place of a node.
PEAK_RADIUS_KM = 0.3

#: The distance (km) that a node must move before MeshTerm finds its place again.
MOVE_KM = 0.5

#: The classes that are a part of a town or a city, from the most specific, with the radius
#: (km) past which their point no longer names a node. The most specific class that has a
#: point within its radius gives the fine part: ``Villeray``, not the borough around it. A
#: hamlet is here and not with the settlements. Its name is often known only near it, so it
#: gets the name of its town as the coarse part. An island is last, because a large island
#: holds a full city.
_AREA_RADIUS_KM: dict[str, float] = {
    "neighbourhood": 1.2,
    "quarter": 1.8,
    "hamlet": 1.5,
    "suburb": 2.5,
    "borough": 4.0,
    "island": 1.5,
}

#: The settlements, with the radius (km) within which one is the fine part, alone.
_SETTLEMENT_RADIUS_KM: dict[str, float] = {
    "village": 2.5,
    "town": 4.0,
    "city": 6.0,
}

#: The weight of a settlement when MeshTerm chooses the coarse part. The distance is divided
#: by the weight, so a city a little farther away wins over a small town nearer. A node in a
#: district of a city is usually nearer to the point of a neighbouring town than to the
#: centre of its own city.
_SETTLEMENT_WEIGHT: dict[str, float] = {"village": 1.0, "town": 2.0, "city": 4.0}

#: The ``rank`` that MeshTerm uses for a settlement whose tile gives none. OpenMapTiles ranks
#: the labels of a tile from 1, the most important. A large city has a low rank. For a node
#: in Ahuntsic, Montréal (rank 3, 9.4 km) must win over Laval (rank 11, 4.4 km), and the
#: class weight alone cannot see that difference. Thus the rank multiplies the distance
#: (refer to :func:`_coarse`).
_DEFAULT_RANK = 10

#: The classes of ``mountain_peak`` that have a summit.
_PEAK_CLASSES = frozenset({"peak", "volcano"})

#: The zoom of the tiles for the settlements and the peaks, and how far (km) MeshTerm looks
#: in them. At z11, a tile holds cities, towns, villages, hamlets, suburbs, islands, and the
#: important peaks, at approximately 40 KB a tile.
_COARSE_ZOOM = 11
_COARSE_REACH_KM = 15.0

#: The zoom of the tiles for the parts of a town, and how far (km) MeshTerm looks in them.
#: Only z14 has the neighbourhoods, the quarters, and the boroughs. MeshTerm loads these
#: tiles only for a node that is in a town (refer to :func:`place_from_tiles`).
_FINE_ZOOM = 14
_FINE_REACH_KM = 2.0

#: The zoom and the reach (km) for a node far from all settlements at :data:`_COARSE_ZOOM`.
_FAR_ZOOM = 9
_FAR_REACH_KM = 50.0


@dataclass(frozen=True, slots=True)
class Place:
    """The place of a node.

    Attributes:
        fine: The most precise name (a neighbourhood, a village, a peak), or ``None``.
        coarse: The town or the city that holds :attr:`fine`, or ``None`` when the fine
            part stands alone.
        source: :data:`SOURCE_TILES`, :data:`SOURCE_NOMINATIM`, or :data:`SOURCE_USER`.
        peak: Whether :attr:`fine` is a peak near its summit. Nominatim does not replace
            such a place.
    """

    fine: str | None
    coarse: str | None
    source: str
    peak: bool = False

    @property
    def empty(self) -> bool:
        """Whether the source answered, but found no place. MeshTerm stores this answer too."""
        return not self.fine and not self.coarse

    @property
    def label(self) -> str:
        """The full name: ``fine, coarse``, or the one part that there is (``""`` if empty)."""
        parts = [p for p in (self.fine, self.coarse) if p]
        if len(parts) == 2 and parts[0] == parts[1]:
            parts = parts[:1]
        return ", ".join(parts)

    @property
    def short(self) -> str:
        """The name for a path chip: the fine part, else the coarse part (``""`` if empty)."""
        return self.fine or self.coarse or ""


def moved(lat1: float, lon1: float, lat2: float, lon2: float) -> bool:
    """Whether a node moved far enough from where its place was found to find it again."""
    return haversine_km(lat1, lon1, lat2, lon2) > MOVE_KM


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One named point from a tile, with its class and its distance from the node."""

    name: str
    cls: str
    km: float
    rank: int = _DEFAULT_RANK


def _tiles_near(lat: float, lon: float, zoom: int, reach_km: float) -> list[tuple[int, int, int]]:
    """The tiles at ``zoom`` that a square of ``reach_km`` around the point touches.

    The tile that holds the point comes first. If it has no answer, the caller has no answer.
    """
    n = 2**zoom
    cx, cy = lonlat_to_world(lat, lon, zoom)
    # The length of one world pixel at this latitude, in km.
    px_km = 2 * math.pi * 6371.0088 * math.cos(math.radians(lat)) / (TILE_PX * n)
    reach_px = reach_km / max(px_km, 1e-9)
    home = (int(cx // TILE_PX), int(cy // TILE_PX))
    x0, x1 = int((cx - reach_px) // TILE_PX), int((cx + reach_px) // TILE_PX)
    y0, y1 = int((cy - reach_px) // TILE_PX), int((cy + reach_px) // TILE_PX)
    tiles = [(zoom, home[0] % n, home[1])]
    for ty in range(max(y0, 0), min(y1, n - 1) + 1):
        for tx in range(x0, x1 + 1):
            key = (zoom, tx % n, ty)
            if key not in tiles:
                tiles.append(key)
    return tiles


def _candidates(
    lat: float,
    lon: float,
    zoom: int,
    reach_km: float,
    load: TileLoader,
    *,
    peaks: list[_Candidate],
) -> list[_Candidate] | None:
    """The named place points within ``reach_km`` of the node, in the tiles at ``zoom``.

    The peaks go into ``peaks`` (each peak one time only, also when two tiles have it).

    Returns:
        The place candidates, or ``None`` when the tile that holds the node has no answer.
    """
    found: dict[tuple[str, str], _Candidate] = {}
    for i, (z, x, y) in enumerate(_tiles_near(lat, lon, zoom, reach_km)):
        layers = load(z, x, y)
        if layers is None:
            if i == 0:
                return None
            continue
        for layer in layers:
            if layer.name not in PLACE_LAYERS:
                continue
            for feature in layer.features:
                if not feature.rings or not feature.rings[0]:
                    continue
                name = feature.name
                if not name:
                    continue
                px, py = feature.rings[0][0]
                wx = (x + px / layer.extent) * TILE_PX
                wy = (y + py / layer.extent) * TILE_PX
                plat, plon = world_to_lonlat(wx, wy, z)
                km = haversine_km(lat, lon, plat, plon)
                if km > reach_km:
                    continue
                cls = str(feature.tags.get("class") or "")
                if layer.name == "mountain_peak":
                    if cls in _PEAK_CLASSES and all(p.name != name for p in peaks):
                        peaks.append(_Candidate(name, cls, km))
                    continue
                try:
                    rank = int(feature.tags.get("rank"))
                except (TypeError, ValueError):
                    rank = _DEFAULT_RANK
                key = (name, cls)
                old = found.get(key)
                if old is None or km < old.km:
                    found[key] = _Candidate(name, cls, km, min(rank, old.rank) if old else rank)
    return list(found.values())


def _most_specific(candidates: Iterable[_Candidate]) -> _Candidate | None:
    """The nearest point of the most specific part of a town that is within its radius."""
    within = [c for c in candidates if c.km <= _AREA_RADIUS_KM.get(c.cls, -1.0)]
    for cls in _AREA_RADIUS_KM:
        of_class = [c for c in within if c.cls == cls]
        if of_class:
            return min(of_class, key=lambda c: c.km)
    return None


def _best(candidates: Iterable[_Candidate], radius: dict[str, float]) -> _Candidate | None:
    """The candidate that is nearest relative to the radius of its class, within that radius."""
    best: tuple[float, _Candidate] | None = None
    for c in candidates:
        r = radius.get(c.cls)
        if r is None or c.km > r:
            continue
        score = c.km / r
        if best is None or score < best[0]:
            best = (score, c)
    return None if best is None else best[1]


def _coarse(candidates: Iterable[_Candidate]) -> _Candidate | None:
    """The settlement that holds the node most probably: the least weighted distance.

    The weight is the class weight, and the rank of the label (plus two, so that rank 1
    does not weigh three times rank 2) multiplies the distance.
    """
    best: tuple[float, _Candidate] | None = None
    for c in candidates:
        weight = _SETTLEMENT_WEIGHT.get(c.cls)
        if weight is None:
            continue
        score = c.km * (c.rank + 2) / weight
        if best is None or score < best[0]:
            best = (score, c)
    return None if best is None else best[1]


def place_from_tiles(lat: float, lon: float, load: TileLoader) -> Place | None:
    """Find the place of a location in the vector tiles.

    The steps:

    1. Load the z11 tiles near the node: its settlements and the important peaks.
    2. If the node is in a town (a settlement or a suburb near it), also load the z14 tiles
       near it, for the neighbourhoods, the quarters, and the boroughs.
    3. A named peak within :data:`PEAK_RADIUS_KM` is the place.
    4. Else the nearest point of the most specific part of a town (within its radius) is
       the fine part, and the settlement with the least weighted distance is the coarse
       part.
    5. Else the settlement that is nearest relative to its radius stands alone.
    6. Else the nearest settlement (looked for up to :data:`_FAR_REACH_KM` away) stands
       alone, or the nearest named peak.

    Args:
        lat: The latitude of the node.
        lon: The longitude of the node.
        load: Loads the decoded layers of one tile (``place`` and ``mountain_peak`` at
            minimum), or returns ``None`` when it has no answer.

    Returns:
        The place, an empty place when the tiles answered but hold nothing near, or ``None``
        when the tile that holds the node has no answer (offline and not cached). The
        caller tries a ``None`` again later, and stores the other answers.
    """
    if not usable_fix(lat, lon):
        return None
    peaks: list[_Candidate] = []
    coarse = _candidates(lat, lon, _COARSE_ZOOM, _COARSE_REACH_KM, load, peaks=peaks)
    if coarse is None:
        return None
    # A town or a city near the node (two of its radii), or a suburb around it: there are
    # neighbourhoods to look for.
    urban = any(
        (c.cls in ("town", "city") and c.km <= 2 * _SETTLEMENT_RADIUS_KM[c.cls])
        or (c.cls == "suburb" and c.km <= _AREA_RADIUS_KM["suburb"])
        for c in coarse
    )
    fine: list[_Candidate] = []
    if urban:
        fine = _candidates(lat, lon, _FINE_ZOOM, _FINE_REACH_KM, load, peaks=peaks) or []
    summit = min((p for p in peaks if p.km <= PEAK_RADIUS_KM), key=lambda p: p.km, default=None)
    if summit is not None:
        return Place(summit.name, None, SOURCE_TILES, peak=True)
    everything = fine + coarse
    area = _most_specific(everything)
    if area is not None:
        holder = _coarse(everything)
        return Place(area.name, holder.name if holder else None, SOURCE_TILES)
    settlement = _best(everything, _SETTLEMENT_RADIUS_KM) or _coarse(everything)
    if settlement is None:
        far = _candidates(lat, lon, _FAR_ZOOM, _FAR_REACH_KM, load, peaks=peaks) or []
        settlement = _coarse(far)
    if settlement is not None:
        return Place(settlement.name, None, SOURCE_TILES)
    nearest_peak = min(peaks, key=lambda p: p.km, default=None)
    if nearest_peak is not None:
        return Place(nearest_peak.name, None, SOURCE_TILES)
    return Place(None, None, SOURCE_TILES)


#: The address keys of a Nominatim answer, from the most precise. The first key that is
#: present is the fine part.
_NOMINATIM_FINE = ("neighbourhood", "quarter", "suburb", "city_district", "borough", "hamlet")

#: The address keys of a settlement in a Nominatim answer, in the order of preference.
_NOMINATIM_COARSE = ("city", "town", "village", "municipality")


def place_from_nominatim(doc: object) -> Place | None:
    """Make a place from the document that the Nominatim ``/reverse`` endpoint returned.

    Args:
        doc: The parsed JSON (``format=jsonv2``, ``addressdetails=1``).

    Returns:
        The place. An answer with no address (``{"error": "Unable to geocode"}``, the sea)
        is an empty place, because Nominatim answered. ``None`` for a document that is not
        an answer at all.
    """
    if not isinstance(doc, dict):
        return None
    address = doc.get("address")
    if not isinstance(address, dict):
        if "error" in doc:
            return Place(None, None, SOURCE_NOMINATIM)
        return None

    def first(keys: tuple[str, ...]) -> str | None:
        for key in keys:
            value = address.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    fine = first(_NOMINATIM_FINE)
    coarse = first(_NOMINATIM_COARSE)
    if fine is None:
        return Place(coarse, None, SOURCE_NOMINATIM)
    return Place(fine, coarse if coarse != fine else None, SOURCE_NOMINATIM)
