# SPDX-License-Identifier: Apache-2.0
"""A small, static map region that any screen can embed. It has no controls.

The full-screen :class:`~meshterm.ui.map_screen.MapScreen` is a complete layer of its own.
It pans, zooms, finds nodes, and stores its view. Some screens only need a short look: a
few rows of basemap fixed on one place, with the markers of the mesh on it, and no
controls. The "Location" preview of the Node detail screen is the first such caller. It
shows where a node is, and the user does not need to leave for the big map. The action
"Open full map" of that screen still goes to the big map.

This class is the tile code of the map, reduced to that job. It has a fixed centre and
zoom, and its own small tile cache. It gets tiles in the background, off the event loop,
so the host screen never waits for the network. Its :meth:`render` method is synchronous
and draws the tiles that have arrived. The picture gets better in place when more tiles
arrive, the same as on the big map. If there is no network and no cached tiles, there is
no basemap, and the markers are plotted on a blank grid. Thus the preview always shows
something. It owns no keys and resolves nothing. The screen that embeds it calls
:meth:`render` from its own ``render_body`` and gets nothing back from it.

It is a produced work, the same as the big map, so it also carries the basemap credit.
But it carries only the short form (:data:`~meshterm.ui.attribution.CREDIT_SHORT`) and
never the full line. There are two reasons, and the guideline supports both. First, a
preview is five to thirteen rows of the node page of the user, not a map that the user
went to look at. Thus forty cells of credit across the bottom of it would be the most
noticeable item on the row. Second, the short form is not a concession. "© OpenStreetMap"
is one of the two forms that OSMF accepts without conditions, so this code does not use
the permission to collapse the credit that the big map uses. The preview is also not
interactive, and this makes the difference important. There is no pan or zoom here that
can cause a collapse, so the mark that the preview draws must be complete when nothing
moves. OpenMapTiles and the ODbL are named on the About page. The guideline gives that
page as its own example of where the licence information of a credit can be found.
"""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING

from ..core.geo import Viewport, clamp_lat
from ..core.mvt import Layer
from ..services.basemap import TILE_RETRY_SECONDS
from . import attribution
from .map_render import MapMarker, render_map

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..services.basemap import BasemapSource

#: The number of zoom levels above the maximum zoom of the tile source that a preview can
#: use. The tiles of a lower zoom are magnified to fill it. The value is the same as the
#: overzoom of the big map, so a close preview has a (blurred) basemap instead of blank
#: tiles.
_OVERZOOM = 2


def _loop_running() -> bool:
    """Whether there is an event loop that can take background work.

    The caller must ask this before it builds a coroutine, not after. Without a loop,
    ``ensure_future`` raises an error. But at that time the coroutine exists and nothing
    awaits it. Python then reports a resource warning on a path that is correct in all
    other ways (a screen rendered in a test, or a one-shot CLI draw).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class MiniMap:
    """A fixed slippy-map region, with no controls, that is drawn in the body of a host screen.

    Build one with the resolved tile source (as :func:`~meshterm.ui.map_screen.open_map`
    resolves it), and with the centre, the zoom, and the markers to plot. Then call
    :meth:`render` from the ``render_body`` of the host. Give it the width in cells and
    the height in rows that the region must have. The tiles download in the background,
    and the region is drawn again when they arrive. Nothing here transmits over the radio.
    """

    def __init__(
        self,
        session,  # noqa: ANN001 - TuiSession, untyped to avoid an import cycle
        source: BasemapSource,
        max_tile_zoom: int,
        *,
        center_lat: float,
        center_lon: float,
        zoom: int,
        markers: list[MapMarker],
    ) -> None:
        """Create the preview over a fixed area of the map.

        Args:
            session: The running TUI session. The preview uses it only to paint the host
                screen again when a background tile arrives (:meth:`_load`).
            source: The vector-tile source. The caller has already resolved and warmed it.
            max_tile_zoom: The maximum zoom of the source. The host gets it off the event
                loop when it opens the screen. Reading it can use the network, so the host
                resolves it one time at the start.
            center_lat: The latitude at the centre of the preview.
            center_lon: The longitude at the centre of the preview.
            zoom: The display zoom. It is limited to a reasonable range, with the
                maximum zoom of the source as the upper limit.
            markers: The nodes with a location that the preview puts on the map. The list
                can be empty.
        """
        self._session = session
        self._source = source
        self._max_tile_zoom = max_tile_zoom
        self._center_lat = clamp_lat(center_lat)
        self._center_lon = center_lon
        self._zoom = max(2, min(int(zoom), max_tile_zoom + _OVERZOOM))
        self._markers = markers
        # The decoded tiles, with (z, x, y) as the key. A stored ``None`` is the answer of
        # the source that there is no tile there. No answer at all is not this answer, and
        # this dict does not store it.
        self._tiles: dict[tuple[int, int, int], list[Layer] | None] = {}
        self._pending: set[tuple[int, int, int]] = set()
        # The tiles with no answer, and the time at which each can be requested again.
        self._unanswered: dict[tuple[int, int, int], float] = {}

    @property
    def has_basemap(self) -> bool:
        """Whether the source has resolved a basemap to draw from.

        "Not yet" does not mean "never". The preview requests tiles in both cases (refer
        to :meth:`_ensure_tiles`). This property is for a host that wants to put a caption
        on the wait.
        """
        return self._source.available

    @property
    def pending(self) -> int:
        """The number of visible tiles that have not arrived yet, for a loading note of a host."""
        return len(self._pending)

    def render(self, width: int, rows: int) -> list[str]:
        """Render the preview at ``width`` cells by ``rows`` rows, and schedule tile loads.

        This method builds a :class:`~meshterm.core.geo.Viewport` for the size of this
        region (each cell is two braille dots wide and each row is four dots tall). The
        viewport is fixed on the centre and the zoom of the preview. The method starts
        background downloads for the tiles that are not loaded yet. Then it draws the frame
        from the tiles that have arrived. A paint calls it each time. The viewport is
        cheap to build again, so a resize only gives it a new size.

        Args:
            width: The width of the region in cells.
            rows: The height of the region in rows of cells.

        Returns:
            One ANSI string for each row. The number is exactly ``rows``, because the
            canvas fills its box.
        """
        viewport = Viewport(
            self._center_lat, self._center_lon, self._zoom, max(2, width) * 2, max(1, rows) * 4
        )
        self._ensure_tiles(viewport)
        tiles = {t: self._tiles.get(t) for t in viewport.tiles(self._max_tile_zoom)}
        lines = render_map(viewport, tiles, self._markers)
        return attribution.stamp(lines, width, full=False)

    def _ensure_tiles(self, viewport: Viewport) -> None:
        """Schedule background downloads for each visible tile that is not here and not due.

        The method does not depend on
        :attr:`~meshterm.services.basemap.BasemapSource.available`. If the source gave no
        answer about a tile, the method requests the tile again when its cooldown ends.
        These are the rules of the big map, and they have the same reasons (refer to
        :meth:`meshterm.ui.map_screen.MapScreen._ensure_tiles`).
        """
        if not _loop_running():  # no loop to download on, so draw the tiles that are here
            return
        if self._unanswered:  # the tiles with no answer whose cooldown has ended
            now = monotonic()
            self._unanswered = {t: at for t, at in self._unanswered.items() if at > now}
        for t in viewport.tiles(self._max_tile_zoom):
            if t in self._tiles or t in self._pending or t in self._unanswered:
                continue
            self._pending.add(t)
            asyncio.ensure_future(self._load(t))

    async def _load(self, t: tuple[int, int, int]) -> None:
        """Download and decode one tile off the event loop, then paint the host screen again."""
        try:
            layers = await asyncio.to_thread(self._source.load_tile, *t)
        except Exception:  # noqa: BLE001 - a tile that failed is the same as an absent tile
            layers = None
        self._pending.discard(t)
        if layers is not None or self._source.answered_empty(*t):
            self._tiles[t] = layers  # an answer, final for the session
        else:
            self._unanswered[t] = monotonic() + TILE_RETRY_SECONDS  # no answer, so request again
        self._session.invalidate()
