# SPDX-License-Identifier: Apache-2.0
"""The places of the nodes: what each screen reads, and the slow job that finds them.

The place of a node is the named area nearest its location: ``Villeray, Montréal``, a
village, or a peak (refer to :mod:`meshterm.core.places` for the rules). This module has two
parts.

**The read side** (:meth:`PlaceService.get`) answers from memory, so a screen can ask at each
paint. A place that the user typed (:class:`~meshterm.core.place_store.PlaceStore`) wins over
a found place. A found place comes from the ``node_places`` table, which this service reads
one time. :attr:`PlaceService.revision` increases at each change, so that a screen with a
render cache knows when to draw again.

**The job** finds the places, one node at a time, in the background. Each minute, it reads
the locations that arrived since its last read (``observed_at`` is indexed, so this read is
cheap). A node gets a place when it has none, or when it moved more than
:data:`~meshterm.core.places.MOVE_KM` from where its place was found. The
``place_source`` preference sets the source:

* ``tiles``: the OpenStreetMap vector tiles of the map (:class:`BasemapSource`). The tiles are
  in the cache when the map showed the area, and the job downloads the others. A tile is
  shared with the map, so the map is ready where the job went first.
* ``nominatim``: the Nominatim ``/reverse`` endpoint at ``nominatim_url``. Nominatim knows the
  boundaries of each place, so it tells the place that *contains* the node.
* ``tiles_nominatim`` (the default): the tiles give the first answer at once, and Nominatim
  corrects it later, one node at a time, when MeshTerm is online. A peak that the tiles found
  near its summit stays, because Nominatim does not answer with peaks.

**Nominatim has a strict usage policy**, and the job obeys it: a user agent that names the
app, a maximum of one request each second (the job waits :data:`NOMINATIM_INTERVAL_S`), and a
cache of each answer (the table). The job asks again only for a node that moved. A screen can
put a node at the head of the queue (:meth:`PlaceService.want`), for example the node page
that the user opened.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from typing import TYPE_CHECKING

from .. import __version__
from ..core.geo import usable_fix
from ..core.places import (
    PLACE_LAYERS,
    SOURCE_NOMINATIM,
    SOURCE_TILES,
    SOURCE_USER,
    Place,
    moved,
    place_from_nominatim,
    place_from_tiles,
)

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.mvt import Layer

#: The time (seconds) between two reads of the new locations.
RESCAN_S = 60.0

#: The time (seconds) before the first read, so that the job does not slow the start.
START_DELAY_S = 8.0

#: The time (seconds) between two lookups in the tiles. A lookup in cached tiles takes a
#: few milliseconds. A lookup that downloads takes the time of its downloads, and this pause
#: comes after it.
TILES_PACE_S = 0.5

#: The time (seconds) between two Nominatim requests. The policy permits one each second.
#: Two seconds keep MeshTerm well inside that limit.
NOMINATIM_INTERVAL_S = 2.0

#: The time (seconds) that the job waits after a source gave no answer (offline, a timeout,
#: a refusal). It then tries again.
RETRY_S = 300.0

#: The network timeout of one Nominatim request (seconds).
_TIMEOUT_S = 12.0

#: The number of decoded tiles that the job keeps in memory. Nodes near each other share
#: their tiles, and the nodes of one mesh are near each other.
_TILE_MEMO = 24

#: Sent with each Nominatim request. The policy asks that the user agent names the
#: application, so that the people who run the server can contact it.
_USER_AGENT = f"MeshTerm/{__version__} (+https://github.com/jpmartineau/MeshTerm; node places)"


def _norm(node: str) -> str:
    """The 12-hex id of a node, from a key prefix or a full public key."""
    return node.lower().removeprefix("0x")[:12]


def ask_nominatim(base_url: str, lat: float, lon: float) -> object | None:
    """Ask Nominatim for the address at a location. This function blocks.

    Args:
        base_url: The root of the Nominatim server (the ``nominatim_url`` preference).
        lat: The latitude.
        lon: The longitude.

    Returns:
        The parsed JSON answer, or ``None`` when the server gave no answer.
    """
    from .basemap import tls_context

    query = urllib.parse.urlencode(
        {
            "format": "jsonv2",
            "lat": f"{lat:.6f}",
            "lon": f"{lon:.6f}",
            # Zoom 16 (streets) is the first level whose address names the neighbourhood.
            # Zoom 14 stops at the borough. MeshTerm reads only the area keys, never the road.
            "zoom": 16,
            "addressdetails": 1,
        }
    )
    url = f"{base_url.rstrip('/')}/reverse?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S, context=tls_context()) as resp:
            return json.loads(resp.read())
    except Exception:  # noqa: BLE001 - offline, a timeout, a refusal, or bad JSON is no answer
        return None


class PlaceService:
    """The places of the nodes: a read side from memory, and a background job that finds them."""

    def __init__(self, ctx: AppContext) -> None:
        """Make an idle service that is bound to an application context.

        Args:
            ctx: The shared application context (the repository, the stores, the
                preferences, and the tile source).
        """
        self._ctx = ctx
        self._found: dict[str, tuple[Place, float, float]] | None = None
        self._revision = 0
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        #: The newest ``observed_at`` that the job read, so that the next read is short.
        self._since: str | None = None
        #: The newest location of each node that the job knows.
        self._locations: dict[str, tuple[float, float]] = {}
        #: The nodes that a screen asked for, first in the queue.
        self._wanted: list[str] = []
        #: The monotonic time before which each source stays silent after it gave no answer,
        #: and the same time for each node that got no answer. Thus one node that a source
        #: cannot answer does not stop the queue.
        self._node_after: dict[str, float] = {}
        self._tiles_after = 0.0
        self._nominatim_after = 0.0
        self._scan_after = 0.0
        self._tile_memo: OrderedDict[tuple[int, int, int], list[Layer]] = OrderedDict()
        self._tile_lock = threading.Lock()

    # -- the read side ----------------------------------------------------------

    @property
    def revision(self) -> int:
        """A number that increases each time a place changes."""
        return self._revision

    def _stored(self) -> dict[str, tuple[Place, float, float]]:
        """The found places, read from the database at the first use."""
        if self._found is None:
            try:
                self._found = self._ctx.repo.node_places()
            except Exception as exc:  # noqa: BLE001 - no places is better than no screen
                self._ctx.log.debug("places: read failed: %s", exc)
                self._found = {}
        return self._found

    def get(self, node: str | None) -> Place | None:
        """The place of a node, or ``None`` when MeshTerm does not know one.

        Args:
            node: The key prefix or the full public key of the node.

        Returns:
            The place that the user typed, else the found place. An empty found place (the
            source answered, but there is no place near) is ``None`` here too.
        """
        if not node:
            return None
        key = _norm(node)
        typed = self._ctx.place_store.get(key)
        if typed:
            return Place(typed, None, SOURCE_USER)
        hit = self._stored().get(key)
        if hit is None or hit[0].empty:
            return None
        return hit[0]

    def typed(self, node: str | None) -> str | None:
        """The place that the user typed for a node, or ``None``."""
        return self._ctx.place_store.get(node) if node else None

    def set_typed(self, node: str, name: str | None) -> None:
        """Type a place for a node, or clear the typed place with ``None`` or blank text."""
        self._ctx.place_store.set(node, name, when=int(time.time()))
        self._revision += 1

    def want(self, nodes: list[str | None]) -> None:
        """Put these nodes at the head of the queue of the job (a screen shows them now).

        A node that already has its place from the current source is not queued again.
        """
        added = False
        for node in nodes:
            if not node:
                continue
            key = _norm(node)
            if key not in self._wanted:
                self._wanted.append(key)
                added = True
        if added:
            self._wake.set()

    # -- the job ----------------------------------------------------------------

    @property
    def active(self) -> bool:
        """True while the job runs."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start the job. Idempotent. The first read waits :data:`START_DELAY_S`."""
        if self.active:
            return
        self._scan_after = time.monotonic() + START_DELAY_S
        self._task = asyncio.ensure_future(self._run())
        self._ctx.log.info("place finder started")

    async def stop(self) -> None:
        """Stop the job. Idempotent."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._ctx.log.info("place finder stopped")

    async def aclose(self) -> None:
        """Stop the job at the end of the session (an alias for :meth:`stop`)."""
        await self.stop()

    async def _run(self) -> None:
        """Do one step, then wait the time that the step returned, or until a screen asks."""
        while True:
            delay = RESCAN_S
            try:
                delay = await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad node must not stop the job
                self._ctx.log.debug("places: step failed: %s", exc)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def _scan(self) -> None:
        """Read the locations that arrived since the last read.

        The heard adverts give most locations. The contact table of the device adds the
        nodes that advertised before MeshTerm listened, and the self-info adds our node.
        Both come from the cache of the device state, never from the device: the job never
        sends a command.
        """
        fresh = self._ctx.repo.node_locations(since=self._since)
        for node, (lat, lon, stamp) in fresh.items():
            if self._since is None or stamp > self._since:
                self._since = stamp
            if usable_fix(lat, lon):
                self._locations[node] = (lat, lon)
        devstate = getattr(self._ctx, "devstate", None)
        if devstate is None:
            return
        for contact in devstate.peek_contacts() or []:
            ident = (contact.public_key or contact.key_prefix or "").lower().removeprefix("0x")
            self._locate(ident[:12], contact.lat, contact.lon, newer=False)
        info = devstate.peek_self_info() or {}
        ident = str(info.get("public_key") or "").lower().removeprefix("0x")[:12]
        self._locate(ident, info.get("adv_lat"), info.get("adv_lon"), newer=True)

    def _locate(self, node: str, lat: object, lon: object, *, newer: bool) -> None:
        """Add a location that did not come from the heard adverts.

        Args:
            node: The 12-hex id of the node (blank to skip).
            lat: The latitude, in any form that ``float`` accepts.
            lon: The longitude, in any form that ``float`` accepts.
            newer: Whether this location replaces a heard one. Our own fix does, because it
                is the current setting. The location in a contact is only the newest that
                the device kept, so a heard advert is as new or newer.
        """
        if not node:
            return
        try:
            flat, flon = float(lat), float(lon)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if usable_fix(flat, flon) and (newer or node not in self._locations):
            self._locations[node] = (flat, flon)

    def want_at(self, node: str | None, lat: float | None, lon: float | None) -> None:
        """Put a node at the head of the queue with a location that a screen knows.

        The node page uses it: the page knows the location of the node (also our own node,
        which MeshTerm never hears), and the user looks at it now.
        """
        if not node or lat is None or lon is None:
            return
        key = _norm(node)
        self._locate(key, lat, lon, newer=True)
        self.want([key])

    def _step_for(self, node: str) -> str | None:
        """The source that the node needs next, or ``None`` when its place is current."""
        location = self._locations.get(node)
        if location is None:
            return None
        lat, lon = location
        mode = self._ctx.preferences.place_source
        stored = self._stored().get(node)
        first = SOURCE_NOMINATIM if mode == "nominatim" else SOURCE_TILES
        if stored is None or moved(stored[1], stored[2], lat, lon):
            return first
        place = stored[0]
        if mode != "tiles" and place.source == SOURCE_TILES and not place.peak:
            return SOURCE_NOMINATIM
        return None

    def _next(self, now: float) -> tuple[str, str] | None:
        """The next ``(node, source)`` to look up, the wanted nodes first."""
        ready = {
            SOURCE_TILES: now >= self._tiles_after,
            SOURCE_NOMINATIM: now >= self._nominatim_after,
        }
        while self._wanted:
            node = self._wanted[0]
            source = self._step_for(node)
            if source is None or not ready[source] or now < self._node_after.get(node, 0.0):
                self._wanted.pop(0)
                continue
            return node, source
        # The tiles first for all the nodes: they are fast, and they give each node an
        # answer. Nominatim then corrects them at its own slow pace.
        for wanted_source in (SOURCE_TILES, SOURCE_NOMINATIM):
            if not ready[wanted_source]:
                continue
            for node in self._locations:
                if now < self._node_after.get(node, 0.0):
                    continue
                if self._step_for(node) == wanted_source:
                    return node, wanted_source
        return None

    async def step(self) -> float:
        """Do one lookup, and return the time until the next step.

        The job calls this method. A test can call it directly.
        """
        now = time.monotonic()
        if now >= self._scan_after:
            self._scan_after = now + RESCAN_S
            self._scan()
        work = self._next(now)
        if work is None:
            soonest = min(
                (t for t in (self._tiles_after, self._nominatim_after) if t > now),
                default=self._scan_after,
            )
            return max(1.0, min(self._scan_after, soonest) - now)
        node, source = work
        lat, lon = self._locations[node]
        if source == SOURCE_TILES:
            place = await asyncio.to_thread(place_from_tiles, lat, lon, self._load_tile)
            if place is None:
                self._no_answer(node, SOURCE_TILES)
                return 1.0
            self._save(node, lat, lon, place)
            return TILES_PACE_S
        url = self._ctx.preferences.nominatim_url
        self._nominatim_after = time.monotonic() + NOMINATIM_INTERVAL_S
        doc = await asyncio.to_thread(ask_nominatim, url, lat, lon)
        place = place_from_nominatim(doc)
        if place is None:
            self._no_answer(node, SOURCE_NOMINATIM)
            return 1.0
        stored = self._stored().get(node)
        if place.empty and stored is not None and not stored[0].empty:
            # Nominatim found nothing (the sea, a lake). The answer of the tiles stays, and
            # it is marked as checked, so that the job does not ask again.
            old = stored[0]
            place = Place(old.fine, old.coarse, SOURCE_NOMINATIM, peak=old.peak)
        self._save(node, lat, lon, place)
        return NOMINATIM_INTERVAL_S

    def _no_answer(self, node: str, source: str) -> None:
        """Make a source and a node wait :data:`RETRY_S` after the source gave no answer.

        Offline, each node fails in the same way, so the source waits. But only the node
        waits as long, so that a source that cannot answer for this one node answers for the
        next one at the end of a short pause.
        """
        now = time.monotonic()
        self._node_after[node] = now + RETRY_S
        pause = now + 30.0
        if source == SOURCE_TILES:
            self._tiles_after = max(self._tiles_after, pause)
        else:
            self._nominatim_after = max(self._nominatim_after, pause)

    def _save(self, node: str, lat: float, lon: float, place: Place) -> None:
        """Store a found place, and tell the screens that a place changed."""
        self._ctx.repo.save_node_place(node, lat, lon, place)
        self._stored()[node] = (place, lat, lon)
        self._revision += 1
        self._ctx.log.debug("places: %s -> %r (%s)", node, place.label, place.source)

    def _load_tile(self, z: int, x: int, y: int) -> list[Layer] | None:
        """The ``place`` and ``mountain_peak`` layers of a tile. This method blocks."""
        from ..core.mvt import decode_tile

        key = (z, x, y)
        with self._tile_lock:
            hit = self._tile_memo.get(key)
            if hit is not None:
                self._tile_memo.move_to_end(key)
                return hit
        raw = self._ctx.basemap_source.raw_tile(z, x, y)
        if raw is None:
            return None
        try:
            layers = decode_tile(raw, layers=PLACE_LAYERS)
        except Exception:  # noqa: BLE001 - a corrupt tile is no answer
            return None
        with self._tile_lock:
            self._tile_memo[key] = layers
            while len(self._tile_memo) > _TILE_MEMO:
                self._tile_memo.popitem(last=False)
        return layers
