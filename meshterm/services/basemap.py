# SPDX-License-Identifier: Apache-2.0
"""Download OpenStreetMap vector tiles for the terminal map, and cache them.

This module is the only part of the map that uses the network. It resolves the
(versioned) tile-URL template of a tile source from its TileJSON. It downloads ``.pbf``
vector tiles over HTTPS, and caches them on disk. Thus when the user pans back over an area
that the map showed before, the tiles show immediately, and later sessions work offline.
The pure :mod:`meshterm.core.mvt` decodes the tiles. The decode is limited to the layers
that the renderer draws (``layers``). A planet tile carries buildings, house numbers, and
POIs for which the terminal map has no pixels, and they are most of its decode cost. Only
the decode is limited: the cache still keeps whole tiles. Thus if the map draws more layers
later, that costs a new decode, but never a new download.

The decoded data is also written to disk. Next to the raw tiles is a ``decoded/`` sidecar
that holds the parsed layers (refer to :func:`~meshterm.core.mvt.dumps_layers`). Thus
MeshTerm parses a tile one time only, not one time in each session. On the PicoCalc, a
reload from the sidecar measured approximately 14x less costly. That half of the cache is
derived data only:

* It never makes a tile "known".
* It carries a stamp of the layer set that it was decoded with. Thus a limited blob never
  goes to a caller that wants more layers.
* It is the half of the cache that gets a size budget, because an evicted entry costs only
  the decode that it saved.

The default source is **OpenFreeMap** (openfreemap.org): full-planet OpenStreetMap vector
tiles, free, with no API key. All of this module is best-effort. With no network and no
cached tiles, the loader returns ``None``, and the map shows the nodes on a blank grid. It
never raises an error into the UI. Best-effort, and **the module never stops trying**. It
does not remember a failed tile download or a failed metadata resolve as a verdict on the
source. The reason is that the app can open before the machine that it runs on has
started its network (refer to :meth:`BasemapSource._resolve`).

**Only tiles that decode to real geometry are written to disk.** A cache is a memory, and
the one thing that it must not remember is a false answer. On a link that is not reliable
(most of all the Wi-Fi of the PicoCalc), a timeout, a reset, or a body that is cut short is
no answer. It is not an empty tile. If MeshTerm stores it, the map shows a blank square in
each future session. Thus MeshTerm caches a download only after its bytes decode to at
least one feature. Any other result is discarded (no answer: try again at the next pan),
or it is kept in memory for this session only (the source answered, and there is really
nothing there). Cache entries from earlier builds that cannot decode are removed when
MeshTerm reads them. This repairs a cache that already has such bad entries.

Downloads block (stdlib ``urllib``). A caller on an event loop must run
:meth:`BasemapSource.load_tile` through ``asyncio.to_thread``, so that the UI continues to
respond. :meth:`BasemapSource.resident` is the one question that never blocks (what is
already in RAM), and the only question that a paint can ask.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Container
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from .. import __version__
from ..core.mvt import Layer, decode_tile, dumps_layers, loads_layers, resident_bytes

#: The OpenFreeMap planet TileJSON. Its ``tiles`` array holds the current versioned template.
DEFAULT_TILEJSON_URL = "https://tiles.openfreemap.org/planet"

#: Sent with each tile request. OpenFreeMap asks for no API key, so the user agent is the
#: only thing that tells one client from another. The people who run the server use it to
#: contact the client that costs them bandwidth. For this reason, a stub URL or an old
#: version is worse than no user agent. The version is the package's own
#: :data:`~meshterm.__version__`, not an ``importlib.metadata`` lookup. MeshTerm often runs
#: directly from a checkout (the venv of this repo, the deployment on the PicoCalc), with
#: no installed distribution to read. Thus the metadata call is the one that can raise an
#: error, and it is a second version to keep the same as the first. MeshTerm builds this
#: string one time at import, because it never changes and cannot fail. The tile path is
#: not the place to find a change or a failure.
_USER_AGENT = f"MeshTerm/{__version__} (+https://github.com/jpmartineau/MeshTerm; mesh node map)"

#: How long MeshTerm waits before it asks again for a tile about which the source gave no
#: answer. It is long enough that a map that is really offline does not try again for each
#: visible tile in a loop. It is short enough that a short Wi-Fi failure costs a few seconds
#: of missing streets, not the rest of the session. It is here, next to the source that did
#: not answer, because the two surfaces that draw tiles wait for the same cooldown (refer to
#: :meth:`meshterm.ui.map_screen.MapScreen._load` and the same method of the minimap).
TILE_RETRY_SECONDS = 20.0

#: The fallback max tile zoom if the TileJSON does not declare one (OpenFreeMap serves 14).
_DEFAULT_MAX_ZOOM = 14

#: HTTP statuses with which the source answers "there is no such resource". All other
#: results (429, 5xx, and each transport error) are not an answer.
_ABSENT_STATUSES = frozenset({404, 410})

#: The maximum size of the decoded sidecar cache. Decoded tiles are approximately 2.4x the
#: size of the bytes that they came from. Also, unlike those bytes, MeshTerm can build them
#: again from the data that is already on disk. Thus this half of the cache is the half
#: that gets a budget.
_DEFAULT_MAX_DECODED_BYTES = 64 * 1024 * 1024

#: How much MeshTerm must write before it checks the sidecar budget again. The check walks
#: the directory, which is slow on the SD card that the PicoCalc runs from.
_PRUNE_AFTER_BYTES = 8 * 1024 * 1024

#: The part of the machine's memory that decoded tiles can hold: one part in this many.
#: The budget is in bytes, because the weight of a tile can change by four times (refer to
#: :func:`~meshterm.core.mvt.resident_bytes`). It changes with the machine, because the
#: machine can be a desktop or a 100 MB handheld. On the handheld, an eighth is
#: approximately 13 MB: the viewport and the history of one pan. The rest of the RAM stays
#: free for the interpreter. If the tiles go over this budget, the map does not become slow,
#: but it stops. The PicoCalc swaps to its SD card, and a process whose code pages it reads
#: back from the card does nothing else during that time.
_MEMO_SHARE = 8

#: The limits of the budget. The floor lets a small machine still keep one viewport of
#: tiles. Above the ceiling, more history gives a desktop no change that the user can
#: notice. A machine that cannot tell how much memory it has (Windows has no ``sysconf``)
#: gets the ceiling.
_MEMO_FLOOR = 8 * 1024 * 1024
_MEMO_CEILING = 128 * 1024 * 1024


def _memo_budget() -> int:
    """The number of bytes of decoded tiles that this machine can hold in RAM.

    Refer to :data:`_MEMO_SHARE`.
    """
    try:
        physical = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        return _MEMO_CEILING
    if physical <= 0:
        return _MEMO_CEILING
    return max(_MEMO_FLOOR, min(_MEMO_CEILING, physical // _MEMO_SHARE))


#: How long a failed TileJSON resolve stays valid before the source asks again. The
#: PicoCalc starts its Wi-Fi approximately 40 seconds into the boot. This is long after the
#: app that started with it can show the menu. Thus "offline", asked one time at open, is a
#: verdict on the boot order of the handheld, not on the network. If MeshTerm keeps that
#: verdict, the session has no basemap at all. The time is long enough that a session that
#: is really offline does not try again and again for no result.
_RESOLVE_RETRY_SECONDS = 30.0

_log = logging.getLogger(__name__)


#: Where the operating system keeps its CA bundle, the most specific first. MeshTerm uses
#: these files only when the default context is empty (refer to :func:`_tls_context`).
#: macOS and Alpine put the bundle at ``/etc/ssl/cert.pem``, Debian and Ubuntu at
#: ``ca-certificates.crt``, Fedora and RHEL under ``/etc/pki``, and openSUSE at
#: ``ca-bundle.pem``. We use these files and not a bundled copy on purpose. They are the
#: trust decisions of the administrator of the machine, and the OS keeps them current. If
#: we ship our own copy, each release contains a frozen copy of them, and nothing shows it.
_CA_BUNDLES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
)


@lru_cache(maxsize=1)
def _tls_context() -> ssl.SSLContext | None:
    """The TLS context for tile downloads, with a CA bundle if the default has none.

    A frozen build can have no trust store. The default context of Python asks OpenSSL for
    one. On macOS and Linux, that is a filesystem path that is built into the interpreter's
    own build. That path is not necessarily on a machine that never installed that Python,
    and each machine that gets a PyInstaller bundle is such a machine. Then each tile
    download fails with ``CERTIFICATE_VERIFY_FAILED``. A refused download looks the same as
    empty terrain, so the map drew no ground under the nodes and did not tell why. Windows
    is the exception that hid this problem for a long time. Its default context reads the
    certificate store of the OS, so the binary always worked there.

    Thus: keep the default when it loaded certificates. If not, point it at the bundle that
    the OS supplies (:data:`_CA_BUNDLES`). The verification is never made weaker. A map
    tile is not worth a change that lets the app skip certificate checks. Also, a
    downloader here without verification is one import away from reuse in a place where
    verification is important.

    Returns:
        A context to give to ``urlopen``, or ``None`` to accept the default of urllib. That
        default is correct when it already works, and it is never worse than the failure
        that it replaces.
    """
    try:
        context = ssl.create_default_context()
        if context.get_ca_certs():
            return None  # the store of the platform answered, so keep urllib as it is
        for path in _CA_BUNDLES:
            if os.path.isfile(path):
                context.load_verify_locations(cafile=path)
                _log.debug("no default CA store; verifying against %s", path)
                return context
        _log.debug("no CA bundle found in %s — TLS will likely fail", ", ".join(_CA_BUNDLES))
        return None
    except Exception as exc:  # noqa: BLE001 - a broken store must not also break the map
        _log.debug("could not build a TLS context, using urllib's default: %s", exc)
        return None


class _Response(NamedTuple):
    """The result of one HTTP GET: whether the server answered, and what it said.

    This difference is the purpose of the class. MeshTerm can act on a definite answer:
    bytes to decode, or a 404 that means that there is no tile at those coordinates.
    Offline, a DNS failure, a timeout, a connection reset, throttling, a server fault, a
    body shorter than its declared length: none of these tell us anything about the tile.
    MeshTerm must never think that they mean that the source said that the tile is empty.

    Attributes:
        answered: Whether the server gave a definite answer.
        body: The body that the server gave. It is empty when the answer was "no such
            resource".
    """

    answered: bool
    body: bytes


class BasemapSource:
    """A cached OpenStreetMap vector-tile source.

    Attributes:
        cache_dir: The directory under which MeshTerm caches the tiles and the resolved
            TileJSON.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        tilejson_url: str = DEFAULT_TILEJSON_URL,
        timeout: float = 12.0,
        layers: Container[str] | None = None,
        max_decoded_bytes: int = _DEFAULT_MAX_DECODED_BYTES,
        memo_bytes: int | None = None,
    ) -> None:
        """Open a tile source that has a cache on disk.

        Args:
            cache_dir: Where MeshTerm stores the downloaded tiles and the TileJSON metadata.
            tilejson_url: The URL of the TileJSON document of the source.
            timeout: The network timeout for each request, in seconds.
            layers: Decode only these layer names (the renderer gives
                :data:`meshterm.ui.map_render.DRAWN_LAYERS`). Only the decode is limited:
                the cache still stores whole tiles. Thus this is only a saving of CPU, and
                a later renderer can add layers without a new download.
            max_decoded_bytes: The maximum size of the decoded sidecar cache. This cache is
                derived data, so it is the one part of the cache that MeshTerm can discard
                freely.
            memo_bytes: The maximum size of the decoded tiles held in RAM. ``None`` sets
                it from the machine (refer to :data:`_MEMO_SHARE`).
        """
        self.cache_dir = cache_dir
        self._tilejson_url = tilejson_url
        self._timeout = timeout
        self._layers = layers
        self._max_decoded_bytes = max_decoded_bytes
        self._decoded_written = 0
        # The layer set with which a sidecar was decoded. A blob written for one layer set
        # must never go to a caller that expects a different set. Thus the set goes with
        # the bytes.
        self._stamp = "all" if layers is None else ",".join(sorted(layers))  # type: ignore[arg-type]
        self._template: str | None = None
        self._max_zoom: int | None = None
        # The time when the source can next try to resolve its template. Zero means "now".
        # A success sets the template, and then the source never reads this value again.
        self._resolve_after = 0.0
        # Tiles for which the source answered "nothing here" in this session. They are in
        # memory, not on disk, so the claim ends with the process. A new question about a
        # blank tile costs little, but an old blank tile on disk is a permanent hole in the
        # map.
        self._blank: set[tuple[int, int, int]] = set()
        # Decoded layers kept in RAM with the weight of each, the most recently used last.
        # The sidecar already prevents the protobuf decode. But a marshal load from the SD
        # card still takes approximately 96 ms on the PicoCalc, and a map paints all the
        # tiles of its viewport in each frame. Thus a pan of one dot read again each tile
        # that did not move. The cache has a limit in bytes, because decoded layers are the
        # largest data that this class holds. It has a lock, because the worker threads of
        # the map fill it while the paints of the map read it.
        self._memo: OrderedDict[tuple[int, int, int], tuple[list[Layer], int]] = OrderedDict()
        self._memo_bytes = 0
        self._memo_budget = _memo_budget() if memo_bytes is None else memo_bytes
        self._memo_lock = threading.Lock()

    # -- metadata ---------------------------------------------------------------

    def _tilejson_path(self) -> Path:
        return self.cache_dir / "tilejson.json"

    def _resolve(self) -> None:
        """Resolve and cache the tile-URL template and the max zoom (best-effort, with retries).

        The result is latched on **success only**. A failed resolve is not an answer, the
        same as a failed tile download (refer to :class:`_Response`). If MeshTerm remembers
        it as "this source is offline", that is the same false answer in a place where it
        costs more. MeshTerm asks this question one time, seconds after the app opens, and
        the verdict then controls each tile for the rest of the session. On the PicoCalc,
        the Wi-Fi connects approximately forty seconds into the boot. Thus an app that
        starts with the boot answers the question before the answer can be true. Then it
        draws the map of all the session from the tiles that were already on disk. The
        result is a scatter of black tiles at the zooms for which the cache does not have
        the ground.

        Thus a failure stays valid only for :data:`_RESOLVE_RETRY_SECONDS`, and the next
        caller after that time asks again. The source sets the deadline before the round
        trip. Thus the several worker threads that a map frame sends through here do not
        all make the same call.

        This method blocks. Worker threads call it (a tile download, the menu's call to
        :meth:`warm`). The paint path never calls it, and reads :attr:`available` instead.
        """
        if self._template is not None:
            return
        now = time.monotonic()
        if now < self._resolve_after:
            return
        self._resolve_after = now + _RESOLVE_RETRY_SECONDS
        # Use a new TileJSON download first (the template has a version, and it changes).
        # If there is none, use the cached copy. Thus a session that started offline can
        # still use the tiles on disk, and can even download again if the template is
        # still valid.
        resp = self._http_get(self._tilejson_url)
        data = resp.body if resp.answered and resp.body else None
        if data is not None:
            try:
                self._tilejson_path().parent.mkdir(parents=True, exist_ok=True)
                self._tilejson_path().write_bytes(data)
            except OSError:  # pragma: no cover - a failure to write the cache is not fatal
                pass
        if data is None and self._tilejson_path().exists():
            try:
                data = self._tilejson_path().read_bytes()
            except OSError:  # pragma: no cover
                data = None
        if data is None:
            return
        try:
            doc = json.loads(data)
            tiles = doc.get("tiles") or []
            if tiles:
                self._template = str(tiles[0])
            self._max_zoom = int(doc.get("maxzoom", _DEFAULT_MAX_ZOOM))
        except (ValueError, TypeError):  # pragma: no cover - a TileJSON that is not valid
            pass

    @property
    def max_zoom(self) -> int:
        """The highest zoom that the source serves.

        MeshTerm resolves it at the first use. When offline, it has a sensible default.
        """
        self._resolve()
        return self._max_zoom if self._max_zoom is not None else _DEFAULT_MAX_ZOOM

    @property
    def available(self) -> bool:
        """Whether a tile-URL template is already known, found without a question.

        The paint path reads this property (the title of the map, the caption of the
        minimap). Thus it must never be the thing that goes to the network. A resolve is a
        blocking round trip, and a resolve from a paint stops the UI for all of its
        timeout. A read of :attr:`max_zoom` resolves, and each map surface does that read,
        off the loop, before it opens. A tile download also resolves, in its own thread.
        This property only reports.

        Thus False means "no template yet", not "stop trying". A caller that skips its
        downloads when it is False will never let a download occur (refer to
        :meth:`meshterm.ui.map_screen.MapScreen._ensure_tiles`, which asks in all cases).
        """
        return self._template is not None

    def answered_empty(self, z: int, x: int, y: int) -> bool:
        """Whether the source answered that there is no tile at these coordinates.

        This is the one way to tell apart the two ``None`` returns of :meth:`load_tile`. A
        tile that the source served as absent (a 404, or bytes that hold no layer at all)
        will never change. But a tile about which we got no answer is worth a new
        question. Callers that cache the ``None`` must know the difference. Refer to
        :meth:`meshterm.ui.map_screen.MapScreen._load`.
        """
        return (z, x, y) in self._blank

    # -- tiles ------------------------------------------------------------------

    def _tile_path(self, z: int, x: int, y: int) -> Path:
        return self.cache_dir / "tiles" / str(z) / str(x) / f"{y}.pbf"

    def _decoded_path(self, z: int, x: int, y: int) -> Path:
        return self.cache_dir / "decoded" / str(z) / str(x) / f"{y}.bin"

    def load_tile(self, z: int, x: int, y: int) -> list[Layer] | None:
        """Return the decoded layers for a tile, from the cache or the network.

        The method tries three places, in the order of cost: the decoded sidecar (a
        ``marshal`` load), the raw ``.pbf`` (a full protobuf decode), and then the network.
        The sidecar is derived data only. It is never the reason that a tile is known. If
        it is lost, that costs only the decode that it saved.

        The raw cache only holds tiles that decoded to real geometry. A tile for which the
        source gave no answer is not cached. Thus the next pan over it asks again, and the
        map does not draw a permanent blank. A cached file that does not decode now is
        removed for the same reason.

        Args:
            z: Tile zoom.
            x: Tile x index.
            y: Tile y index.

        Returns:
            The decoded layers, or ``None`` if the tile is not available (offline and not
            cached, or a tile that is really empty or missing).
        """
        if (z, x, y) in self._blank:
            return None
        hot = self.resident(z, x, y)
        if hot is not None:
            return hot
        layers = self._read_decoded(z, x, y)
        if layers is None:
            layers = self._build(z, x, y)
        if layers is not None:
            self._remember((z, x, y), layers)
        return layers

    def resident(self, z: int, x: int, y: int) -> list[Layer] | None:
        """The decoded layers of a tile if they are already in RAM, or ``None``. Never I/O.

        This is the one question that a paint can ask the source. Without this method, a
        map that moves back over an area that it drew before sends each tile of that area
        through a worker thread, to get data that is already in memory. It also draws its
        first frame without these tiles while it waits.
        """
        with self._memo_lock:
            hit = self._memo.get((z, x, y))
            if hit is None:
                return None
            self._memo.move_to_end((z, x, y))
            return hit[0]

    def warm(self, z: int, x: int, y: int) -> None:
        """Make a tile fast to load later: download, decode, and write it, but hold nothing.

        This method is for a guess at the next move. For a tile that nobody looked at yet,
        the slow parts are the network and the decode. The sidecar on disk is the full
        answer to both. When this method also held the result in RAM, it used the memory
        budget on tiles that the user may never look at, and it evicted tiles that the user
        looked at. Thus the method skips a tile that already has its sidecar, and does not
        read it.
        """
        if (z, x, y) in self._blank or self.resident(z, x, y) is not None:
            return
        if self._decoded_path(z, x, y).exists():
            return
        self._build(z, x, y)

    def _build(self, z: int, x: int, y: int) -> list[Layer] | None:
        """Decode a tile from its raw bytes (cached, or downloaded), and write both caches.

        This method holds nothing in RAM. That is the decision of :meth:`load_tile`, not of
        this method.
        """
        key = (z, x, y)
        path = self._tile_path(z, x, y)
        cached = self._read_cached(path)
        if cached is not None:
            layers = self._decode(cached, key)
            if layers is not None:
                self._write_decoded(z, x, y, layers)
                return layers
            # The decode gave nothing that the map can draw. It is a zero-byte marker from a
            # build that cached network failures, or bytes that a link (or a power cut)
            # truncated during a write. In both cases, it is a blank square forever, unless
            # we remove it and ask again.
            _log.debug("dropping unusable cached tile %s/%s/%s", z, x, y)
            self._discard_cached(path)
        raw = self._fetch_tile(z, x, y)
        if raw is None:
            return None  # no answer: we learned nothing, so we write nothing
        layers = self._decode(raw, key)
        if layers is None:
            self._blank.add(key)  # the source answered: there is really nothing here
            return None
        self._write_cached(path, raw)
        self._write_decoded(z, x, y, layers)
        return layers

    def _remember(self, key: tuple[int, int, int], layers: list[Layer]) -> None:
        """Hold a decoded tile in RAM, and evict the least recently used tiles past the budget.

        The newest tile always stays, also when it alone is over the budget. MeshTerm
        loaded it because a viewport must show it. If MeshTerm evicts it, that viewport only
        goes back to the disk for it.
        """
        cost = resident_bytes(layers)
        with self._memo_lock:
            old = self._memo.pop(key, None)
            if old is not None:
                self._memo_bytes -= old[1]
            self._memo[key] = (layers, cost)
            self._memo_bytes += cost
            while self._memo_bytes > self._memo_budget and len(self._memo) > 1:
                _, (_, dropped) = self._memo.popitem(last=False)
                self._memo_bytes -= dropped

    # -- the decoded sidecar ----------------------------------------------------

    def _read_decoded(self, z: int, x: int, y: int) -> list[Layer] | None:
        """Return the decoded layers of a tile from the sidecar, or ``None`` to decode it fully."""
        blob = self._read_cached(self._decoded_path(z, x, y))
        return None if blob is None else loads_layers(blob, stamp=self._stamp)

    def _write_decoded(self, z: int, x: int, y: int, layers: list[Layer]) -> None:
        """Write the decoded layers of a tile next to the raw bytes.

        This method also keeps the size of the directory in its budget.
        """
        try:
            blob = dumps_layers(layers, stamp=self._stamp)
        except ValueError:  # pragma: no cover - a tag type that marshal cannot represent
            return
        self._write_cached(self._decoded_path(z, x, y), blob)
        # A sweep of the tree costs a directory walk on an SD card. Thus spread its cost
        # over many writes, and do not check the budget for each tile.
        self._decoded_written += len(blob)
        if self._decoded_written >= _PRUNE_AFTER_BYTES:
            self._decoded_written = 0
            self._prune_decoded()

    def _prune_decoded(self) -> None:
        """Remove the least recently written sidecars until the size is in the budget.

        Decoded tiles are approximately 2.4x the size of the bytes that they came from, and
        the raw cache already has no limit. Thus this side of the cache gets a maximum. The
        eviction uses the modification time. On a file that is written one time only, that
        is also the order in which each file was first necessary.
        """
        root = self.cache_dir / "decoded"
        try:
            files = [(p.stat().st_mtime, p.stat().st_size, p) for p in root.rglob("*.bin")]
        except OSError:  # pragma: no cover - a cache that we cannot walk is still readable
            return
        total = sum(size for _, size, _ in files)
        if total <= self._max_decoded_bytes:
            return
        for _, size, p in sorted(files):
            if total <= self._max_decoded_bytes:
                break
            self._discard_cached(p)
            total -= size
        _log.debug("pruned decoded tile cache to %.1f MB", total / 1024 / 1024)

    def _decode(self, raw: bytes, key: tuple[int, int, int]) -> list[Layer] | None:
        """Decode the bytes of a tile, or return ``None`` if they are not a tile at all.

        This method answers the question "are these real tile bytes?". The answer decides
        whether MeshTerm keeps or removes a cache entry. Thus it must not be confused with
        "is there anything here that I want to draw". With ``layers``, most of the content
        of a tile is not decoded, on purpose. If the test looked only at the decoded
        layers, a tile whose layers we all skip looks blank. Then MeshTerm deletes its
        cache entry (a valid entry), and downloads the tile again in each session.

        Thus the test is whether the bytes parsed into any layer at all. That test is
        correct, because MeshTerm can identify the three types of tile that is not real
        without a look at the features. Empty bytes decode to no layers. Truncated or junk
        bytes cause the parser to raise an error, and do not give a well-formed layer with
        no features.

        Args:
            raw: The raw ``.pbf`` bytes of the tile.
            key: The ``(z, x, y)`` that the bytes claim to be, for the log line.

        Returns:
            The decoded layers, or ``None`` for empty or corrupt bytes.
        """
        try:
            layers = decode_tile(raw, layers=self._layers)
        except Exception as exc:  # noqa: BLE001 - a corrupt tile must not crash the map
            _log.debug("failed to decode tile %s/%s/%s: %s", *key, exc)
            return None
        return layers or None

    def _fetch_tile(self, z: int, x: int, y: int) -> bytes | None:
        """Download the raw bytes of a tile from the network.

        Args:
            z: Tile zoom.
            x: Tile x index.
            y: Tile y index.

        Returns:
            The body that the source gave, or ``None`` if the source did not answer
            (offline, timed out, throttled, faulted, or cut short). The body can be empty,
            which means "no tile at those coordinates".
        """
        self._resolve()
        if self._template is None:
            return None
        url = self._template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
        resp = self._http_get(url)
        return resp.body if resp.answered else None

    @staticmethod
    def _read_cached(path: Path) -> bytes | None:
        try:
            return path.read_bytes() if path.exists() else None
        except OSError:  # pragma: no cover
            return None

    @staticmethod
    def _write_cached(path: Path, raw: bytes) -> None:
        """Write a tile to the cache atomically, so that a half-written tile is never read."""
        # Unique for each writer: the map screen and a minimap use the same source, and both
        # can download the same tile in their own worker threads at the same time.
        tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.part")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(raw)
            os.replace(tmp, path)
        except OSError:  # pragma: no cover - a failure to write the cache is not fatal
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _discard_cached(path: Path) -> None:
        """Remove a cache entry that was not usable, so that the tile can be downloaded again."""
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - a cache that we cannot prune is still readable
            pass

    def _http_get(self, url: str) -> _Response:
        """GET a URL, and tell a definite answer apart from no answer at all.

        Args:
            url: The absolute URL to get.

        Returns:
            The result. Refer to :class:`_Response`.
        """
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout, context=_tls_context()) as resp:
                body = resp.read()
                declared = (resp.headers.get("Content-Length") or "").strip()
                # A link that is not reliable can end a read early, and not raise an error.
                # A body shorter than its declared length is a truncation, not a tile, and
                # also not an empty tile.
                if declared.isdigit() and len(body) != int(declared):
                    _log.debug("short read for %s: %d of %s bytes", url, len(body), declared)
                    return _Response(False, b"")
                return _Response(True, body)
        except urllib.error.HTTPError as exc:
            answered = exc.code in _ABSENT_STATUSES
            if not answered:
                _log.debug("fetch failed for %s: HTTP %s", url, exc.code)
            return _Response(answered, b"")
        except Exception as exc:  # noqa: BLE001 - offline, a timeout, or a reset is "no answer"
            _log.debug("fetch failed for %s: %s", url, exc)
            return _Response(False, b"")
