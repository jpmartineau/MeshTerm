# SPDX-License-Identifier: Apache-2.0
"""Pure geographic helpers for the terminal map: Web Mercator, tiles, and viewports.

This module has no I/O and no rendering, on purpose, so that unit tests can run it without a
device, a terminal, or the network. It changes latitude and longitude into the Web Mercator
"world pixel" space of slippy-map vector tiles. It models the :class:`Viewport` on the screen
(which braille dot shows which coordinate), and it measures great-circle distances.

The map renders one braille **dot** for each Web Mercator pixel at the zoom of the viewport.
Thus a viewport of ``dot_w`` × ``dot_h`` dots shows exactly that many Mercator pixels. A pan
or a zoom then only moves or scales this area over the world. In a monospace font, the
sub-cells of a braille glyph (one for each dot) are approximately square: 2 dots wide and 4
dots tall, in a cell that is approximately two times as tall as it is wide. Thus Mercator
pixels show on the screen without visible distortion.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: The mean radius of the Earth in kilometres, for haversine distances.
EARTH_RADIUS_KM = 6371.0088

#: The edge of a Web Mercator tile in pixels at its own zoom (the slippy-map convention). At
#: a given zoom, the world is a square of ``TILE_PX * 2**zoom`` pixels.
TILE_PX = 256

#: The default fraction of nodes that a map viewport fits: the densest half. Thus a few
#: distant outliers do not zoom the full mesh out to a continent. Refer to
#: :meth:`Viewport.fit`.
#:
#: It is in this module, next to the viewport mathematics that it is a parameter of, and not
#: in the map screen that made it. The ``map`` subcommand must have it as a ``--fraction``
#: default at CLI registration time, which occurs for each tool at each startup. When the
#: constant was in :mod:`meshterm.ui.map_screen`, the import pulled the full map stack (and,
#: through the tile downloader of the basemap, ``urllib.request`` → ``http.client`` →
#: ``ssl``) into each run, also runs that never open a map. This module is pure arithmetic
#: and is already on the boot path, so the constant costs nothing here. It is also the
#: default of the ``map_view_fraction`` preference. The registry names this constant and
#: does not type the number again, so the code default and the preference default are one
#: value.
DEFAULT_VIEW_FRACTION = 0.5


@dataclass(frozen=True, slots=True)
class BBox:
    """An axis-aligned bounding box of latitude and longitude (decimal degrees)."""

    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float

    @classmethod
    def around(cls, points: list[tuple[float, float]]) -> BBox:
        """Return the smallest box that contains each ``(lat, lon)`` point.

        Args:
            points: One or more latitude and longitude pairs (must not be empty).

        Returns:
            The bounding box that contains the points.

        Raises:
            ValueError: If ``points`` is empty.
        """
        if not points:
            raise ValueError("cannot build a bounding box from no points")
        lats = [lat for lat, _ in points]
        lons = [lon for _, lon in points]
        return cls(min(lats), min(lons), max(lats), max(lons))

    @property
    def center(self) -> tuple[float, float]:
        """The centre of the box, as a ``(lat, lon)`` pair."""
        return ((self.min_lat + self.max_lat) / 2, (self.min_lon + self.max_lon) / 2)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance between two points, in kilometres.

    Args:
        lat1: The latitude of the first point (degrees).
        lon1: The longitude of the first point (degrees).
        lat2: The latitude of the second point (degrees).
        lon2: The longitude of the second point (degrees).

    Returns:
        The distance along the surface of the Earth, in kilometres.
    """
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    # Clamped, because the rounding can push ``a`` a very small amount above 1 for points
    # that are almost antipodal. Then ``asin`` raises a domain error, and does not return
    # half the planet.
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def _central_points(
    points: list[tuple[float, float]], fraction: float
) -> list[tuple[float, float]]:
    """Return the ``fraction`` of ``points`` nearest to their median centre (the dense core).

    The centre is the median on each axis, so that outliers do not pull it. The points are
    sorted by their great-circle distance from the centre. The function always keeps a
    minimum of two points (so that the result still limits a zoom). When the count to keep
    reaches the length of the list, the function returns the full list.

    Args:
        points: Latitude and longitude pairs (not empty).
        fraction: The portion to keep, ``0 < fraction <= 1``.

    Returns:
        The nearest ``ceil(len(points) * fraction)`` points (minimum 2), or all of them.
    """
    n = len(points)
    keep = max(2, math.ceil(n * fraction))
    if keep >= n:
        return points
    lats = sorted(lat for lat, _ in points)
    lons = sorted(lon for _, lon in points)
    mid = n // 2
    med_lat = lats[mid] if n % 2 else (lats[mid - 1] + lats[mid]) / 2
    med_lon = lons[mid] if n % 2 else (lons[mid - 1] + lons[mid]) / 2
    ordered = sorted(points, key=lambda p: haversine_km(p[0], p[1], med_lat, med_lon))
    return ordered[:keep]


def clamp_lat(lat: float) -> float:
    """Clamp a latitude to the Web Mercator limit (~±85.051°), where the projection is finite."""
    return max(-85.05112878, min(85.05112878, lat))


def usable_fix(lat: float, lon: float) -> bool:
    """Whether an advertised ``(lat, lon)`` is a real position that is worth a plot.

    The location in the advert of a node is usable only if it is a true fix. It is not
    usable in two cases:

    * **out of range**: the latitude must be in ±90°, and the longitude in ±180°. Some
      firmware and adverts report nonsense (MeshTerm heard a MeshCore companion with the
      advert ``lat -97, lon -1042``). If MeshTerm projects that position, the viewport
      goes off the world. Then the map is a screen of empty or water fill, which looks
      solid black.
    * **null island**: a companion with no GPS lock advertises a latitude and a
      longitude of zero. This position projects to the empty middle of the Atlantic. A
      node there is worse than useless: if the viewport fits only that node, the full
      viewport goes onto open ocean (again, solid black water).

    In both cases, MeshTerm treats the fix as absent, so the node has no location. This
    is the shared guard that the map markers and the location preview of the Node detail
    screen both use.
    """
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return False
    return not (abs(lat) < 1e-6 and abs(lon) < 1e-6)


def lonlat_to_world(lat: float, lon: float, zoom: float) -> tuple[float, float]:
    """Project ``(lat, lon)`` to Web Mercator world pixels at ``zoom``.

    Args:
        lat: The latitude in degrees (clamped to the Mercator limit).
        lon: The longitude in degrees.
        zoom: The zoom level (it can be a fraction).

    Returns:
        ``(x, y)`` in world pixels. The world is ``TILE_PX * 2**zoom`` on each axis, and
        ``y`` increases to the south (north is up, at a smaller ``y``).
    """
    scale = TILE_PX * (2.0**zoom)
    x = (lon + 180.0) / 360.0 * scale
    s = math.sin(math.radians(clamp_lat(lat)))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * scale
    return x, y


def world_to_lonlat(x: float, y: float, zoom: float) -> tuple[float, float]:
    """Invert :func:`lonlat_to_world`: change world pixels at ``zoom`` back to ``(lat, lon)``.

    Args:
        x: The world-pixel x.
        y: The world-pixel y.
        zoom: The zoom at which the pixels were calculated.

    Returns:
        The ``(lat, lon)`` in degrees.
    """
    scale = TILE_PX * (2.0**zoom)
    lon = x / scale * 360.0 - 180.0
    n = math.pi - 2 * math.pi * y / scale
    lat = math.degrees(math.atan(math.sinh(n)))
    return lat, lon


@dataclass(frozen=True, slots=True)
class Viewport:
    """The visible area of the world on the screen: the position that each braille dot shows.

    One dot is one Web Mercator pixel at :attr:`zoom`. Thus the visible area is exactly
    ``dot_w`` × ``dot_h`` Mercator pixels, with its centre at ``(center_lat, center_lon)``.
    A pan or a zoom returns a new viewport. Nothing in this class mutates.

    Attributes:
        center_lat: The latitude at the centre of the viewport.
        center_lon: The longitude at the centre of the viewport.
        zoom: The viewport zoom (an integer here). It can be more than the maximum of the
            tile source. In that case, the map magnifies the tiles of a lower zoom (refer to
            :meth:`tiles` and :meth:`feature_to_dot`).
        dot_w: The width of the viewport in braille dots.
        dot_h: The height of the viewport in braille dots.
    """

    center_lat: float
    center_lon: float
    zoom: int
    dot_w: int
    dot_h: int

    @property
    def origin_world(self) -> tuple[float, float]:
        """The top-left corner of the viewport in world pixels at :attr:`zoom`."""
        cx, cy = lonlat_to_world(self.center_lat, self.center_lon, self.zoom)
        return cx - self.dot_w / 2, cy - self.dot_h / 2

    def lonlat_to_dot(self, lat: float, lon: float) -> tuple[float, float]:
        """Project a coordinate to a dot position in the viewport (it can be off the screen)."""
        ox, oy = self.origin_world
        wx, wy = lonlat_to_world(lat, lon, self.zoom)
        return wx - ox, wy - oy

    def tile_zoom(self, max_tile_zoom: int) -> int:
        """The tile zoom to download: the viewport zoom, limited to the maximum of the source."""
        return max(0, min(self.zoom, max_tile_zoom))

    def tiles(self, max_tile_zoom: int) -> list[tuple[int, int, int]]:
        """Return the ``(z, x, y)`` tiles that cover the viewport at the downloadable tile zoom.

        When the viewport zoom is more than ``max_tile_zoom``, the function returns the tiles
        of a lower zoom that cover the same ground (the renderer magnifies them). Thus a zoom
        past the limit of the source still works.

        Args:
            max_tile_zoom: The highest zoom that the tile source serves.

        Returns:
            Tile coordinates, clamped to the valid range, without duplicates, in row-major
            order.
        """
        tz = self.tile_zoom(max_tile_zoom)
        scale = 2.0 ** (self.zoom - tz)  # viewport pixels for each tile-zoom pixel
        ox, oy = self.origin_world
        n = 2**tz
        # The visible rectangle in tile-zoom world pixels, then in tile indices.
        tx0 = int((ox / scale) // TILE_PX)
        tx1 = int(((ox + self.dot_w) / scale) // TILE_PX)
        ty0 = int((oy / scale) // TILE_PX)
        ty1 = int(((oy + self.dot_h) / scale) // TILE_PX)
        out: list[tuple[int, int, int]] = []
        for ty in range(ty0, ty1 + 1):
            if not 0 <= ty < n:
                continue
            for tx in range(tx0, tx1 + 1):
                out.append((tz, tx % n, ty))  # wrap x around the antimeridian
        return out

    def tile_transform(
        self, tile_x: int, tile_y: int, tile_zoom: int, extent: int
    ) -> tuple[float, float, float]:
        """Return ``(base_x, base_y, step)``: the change from local tile coordinates to dots.

        :meth:`feature_to_dot` is the readable form of the same projection, but it is a call
        for each vertex. One frame of the map projects tens of thousands of vertices: 107k of
        them on a map of a downtown area. Each call calculates :attr:`origin_world` again (a
        ``sin`` and ``log`` pair), and a ``2**`` for a value that does not change in the full
        frame. That was more than half the cost to draw the map.

        The projection is affine in the local coordinates of the tile. Thus all of that
        becomes three numbers that the caller can move out of its loop::

            dot_x = base_x + lx * step
            dot_y = base_y + ly * step

        Then each vertex costs two multiplications and two additions, inline, with no call
        at all. The caller must write that arithmetic in its own comprehension, and not get
        a closure back. After the trigonometry is gone, a function call for each vertex is
        itself most of the remaining cost.

        Args:
            tile_x: The x index of the tile at ``tile_zoom``.
            tile_y: The y index of the tile at ``tile_zoom``.
            tile_zoom: The zoom at which the tile was downloaded.
            extent: The internal coordinate extent of the tile (for example, 4096).

        Returns:
            The ``(base_x, base_y, step)`` coefficients that are described above.
        """
        span = TILE_PX * (2.0 ** (self.zoom - tile_zoom))
        ox, oy = self.origin_world
        return tile_x * span - ox, tile_y * span - oy, span / extent

    def feature_to_dot(
        self, tile_x: int, tile_y: int, tile_zoom: int, extent: int, lx: float, ly: float
    ) -> tuple[float, float]:
        """Project a point in local tile coordinates to a dot position in this viewport.

        Args:
            tile_x: The x index of the tile at ``tile_zoom``.
            tile_y: The y index of the tile at ``tile_zoom``.
            tile_zoom: The zoom at which the tile was downloaded.
            extent: The internal coordinate extent of the tile (for example, 4096).
            lx: The local x in the tile, ``0..extent``.
            ly: The local y in the tile, ``0..extent``.

        Returns:
            The ``(x, y)`` dot position. It can be outside the canvas, and the caller clips
            it.
        """
        scale = 2.0 ** (self.zoom - tile_zoom)
        wx = (tile_x + lx / extent) * TILE_PX * scale
        wy = (tile_y + ly / extent) * TILE_PX * scale
        ox, oy = self.origin_world
        return wx - ox, wy - oy

    def panned(self, frac_x: float, frac_y: float) -> Viewport:
        """Return a viewport moved by a fraction of its own width and height.

        Args:
            frac_x: The move east, as a fraction of the viewport width (negative = west).
            frac_y: The move south, as a fraction of the viewport height (negative = north).

        Returns:
            A new :class:`Viewport` at the moved centre, with the same zoom and size.
        """
        cx, cy = lonlat_to_world(self.center_lat, self.center_lon, self.zoom)
        cx += frac_x * self.dot_w
        cy += frac_y * self.dot_h
        lat, lon = world_to_lonlat(cx, cy, self.zoom)
        return Viewport(clamp_lat(lat), lon, self.zoom, self.dot_w, self.dot_h)

    def zoomed(self, delta: int, *, min_zoom: int = 2, max_zoom: int = 19) -> Viewport:
        """Return a viewport zoomed by ``delta`` levels on the same centre (clamped)."""
        z = max(min_zoom, min(max_zoom, self.zoom + delta))
        return Viewport(self.center_lat, self.center_lon, z, self.dot_w, self.dot_h)

    def resized(self, dot_w: int, dot_h: int) -> Viewport:
        """Return the same viewport, with the same centre, but at a new canvas size."""
        return Viewport(self.center_lat, self.center_lon, self.zoom, dot_w, dot_h)

    @classmethod
    def fit(
        cls,
        points: list[tuple[float, float]],
        dot_w: int,
        dot_h: int,
        *,
        pad: float = 0.18,
        min_zoom: int = 2,
        max_zoom: int = 16,
        default_zoom: int = 14,
        fraction: float = 1.0,
    ) -> Viewport:
        """Build a viewport that fits ``points``: centred on them, at the highest zoom that fits.

        Args:
            points: The latitude and longitude pairs to fit (can be empty).
            dot_w: The canvas width in dots.
            dot_h: The canvas height in dots.
            pad: The fraction of the canvas that is kept as a margin around the points.
            min_zoom: The lowest zoom to consider.
            max_zoom: The highest zoom to consider.
            default_zoom: The zoom to use when the points do not limit it (0 or 1 point).
            fraction: The fraction of the points to fit, ``0 < fraction <= 1``. Below ``1``,
                the viewport fits only the densest core (the points nearest to the median
                centre). Thus a few distant outliers cannot force the full viewport to zoom
                out. The other nodes go off the edges.

        Returns:
            A :class:`Viewport` centred on the fitted points, at a zoom where they fit with
            a margin. An empty input centres on the world at ``min_zoom``.
        """
        if not points:
            return cls(0.0, 0.0, min_zoom, dot_w, dot_h)
        if fraction < 1.0:
            points = _central_points(points, fraction)
        box = BBox.around(points)
        center_lat, center_lon = box.center
        if len(points) == 1 or (box.min_lat == box.max_lat and box.min_lon == box.max_lon):
            return cls(center_lat, center_lon, default_zoom, dot_w, dot_h)
        avail_w = dot_w * (1 - pad)
        avail_h = dot_h * (1 - pad)
        chosen = min_zoom
        for z in range(max_zoom, min_zoom - 1, -1):
            x0, y0 = lonlat_to_world(box.max_lat, box.min_lon, z)  # NW corner
            x1, y1 = lonlat_to_world(box.min_lat, box.max_lon, z)  # SE corner
            if abs(x1 - x0) <= avail_w and abs(y1 - y0) <= avail_h:
                chosen = z
                break
        return cls(center_lat, center_lon, chosen, dot_w, dot_h)
