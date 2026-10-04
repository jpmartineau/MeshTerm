# SPDX-License-Identifier: Apache-2.0
"""Fetch and cache OpenStreetMap vector tiles for the terminal map.

This is the only networked part of the map. It resolves a tile source's (versioned) tile-URL
template from its TileJSON, fetches ``.pbf`` vector tiles over HTTPS, and caches them on disk
so panning back over ground you've seen is instant and later sessions work offline. Decoding
is delegated to the pure :mod:`meshterm.core.mvt`, and narrowed to the layers the renderer
actually draws (``layers``) — a planet tile carries buildings, house numbers and POIs the
terminal map has no pixels for, and they are the bulk of its decode cost. Only the decode
narrows: whole tiles are still cached, so drawing more later costs a re-decode, never a
re-download.

Decoding is also written down. Beside the raw tiles sits a ``decoded/`` sidecar holding
the parsed layers (see :func:`~meshterm.core.mvt.dumps_layers`), so a tile is parsed once
ever rather than once per session — measured ~14x cheaper to reload on the PicoCalc. That
half of the cache is pure derived data: it is never what makes a tile "known", it carries
a stamp of the layer set it was decoded under so a narrowed blob is never served to a
caller wanting more, and it is the half that gets a size budget, because anything evicted
costs only the decode it was saving.

The default source is **OpenFreeMap** (openfreemap.org) — full-planet OpenStreetMap vector
tiles, free and requiring no API key. Everything here is best-effort: with no network and no
cached tiles the loader simply returns ``None`` and the map falls back to plotting nodes on a
blank grid, never raising into the UI. Best-effort, and **never given up on**: neither a
failed tile fetch nor a failed metadata resolve is remembered as a verdict on the source,
because the app can perfectly well open before the device it runs on has finished bringing
its network up (see :meth:`BasemapSource._resolve`).

**Only tiles that decode to real geometry are ever written to disk.** A cache is a memory,
and the one thing it must not remember is a lie: on a flaky link (the PicoCalc's Wi-Fi, most
of all) a timeout, a reset, or a body cut short is *silence*, not an empty tile, and storing
it leaves a blank square on the map for every future session. So a fetch is only cached once
its bytes have decoded to at least one feature; anything else is either dropped on the floor
(no answer — try again next pan) or remembered in memory for this session alone (the source
answered, and there is genuinely nothing there). Cache entries written by earlier builds that
can't decode are pruned on read, which heals a cache already poisoned this way.

Fetches are blocking (stdlib ``urllib``); callers on an event loop should run
:meth:`BasemapSource.load_tile` via ``asyncio.to_thread`` so the UI stays responsive.
:meth:`BasemapSource.resident` is the one question that never blocks — what is already in
RAM — and the only one a paint may ask.
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

#: OpenFreeMap planet TileJSON — its ``tiles`` array holds the current versioned template.
DEFAULT_TILEJSON_URL = "https://tiles.openfreemap.org/planet"

#: Sent on every tile request. OpenFreeMap asks for no API key, so the user agent is the
#: only thing telling one client from another — which is how an operator reaches whoever is
#: costing them bandwidth, and why a stub URL or a version that has drifted is worse than
#: none at all. The version is the package's own :data:`~meshterm.__version__` rather than
#: an ``importlib.metadata`` lookup: MeshTerm is routinely run straight from a checkout
#: (this repo's venv, the PicoCalc's deploy) with no installed distribution to read, so the
#: metadata call would be the one that raises, and it would be a second version to keep in
#: step with the first. Built once at import because it never changes and cannot fail —
#: the tile path is no place to discover either.
_USER_AGENT = f"MeshTerm/{__version__} (+https://github.com/jpmartineau/MeshTerm; mesh node map)"

#: How long a tile the source gave no answer about is left alone before it is asked for
#: again. Long enough that a genuinely offline map isn't retrying every visible tile on a
#: loop, short enough that a Wi-Fi blip costs a few seconds of missing streets rather than
#: the rest of the session. It lives here, beside the source that failed to answer, because
#: both surfaces that draw tiles wait out the same cooldown (see
#: :meth:`meshterm.ui.map_screen.MapScreen._load` and the minimap's).
TILE_RETRY_SECONDS = 20.0

#: Fallback max tile zoom if the TileJSON doesn't declare one (OpenFreeMap serves 14).
_DEFAULT_MAX_ZOOM = 14

#: HTTP statuses that are the source *answering* "there is no such resource". Everything
#: else — 429, 5xx, and every transport error — is the absence of an answer.
_ABSENT_STATUSES = frozenset({404, 410})

#: Ceiling on the decoded sidecar cache. Decoded tiles run ~2.4x the size of the bytes
#: they came from, and unlike those bytes they can be rebuilt from what's already on disk,
#: so this half of the cache is the half that gets a budget.
_DEFAULT_MAX_DECODED_BYTES = 64 * 1024 * 1024

#: How much must be written before the sidecar budget is checked again. The check walks
#: the directory, which is slow on the SD card the PicoCalc runs from.
_PRUNE_AFTER_BYTES = 8 * 1024 * 1024

#: The share of the machine's memory that decoded tiles may hold — one part in this many.
#: Budgeted in bytes because a tile's weight varies fourfold (see
#: :func:`~meshterm.core.mvt.resident_bytes`), and scaled to the machine because the one
#: running it may be a desktop or a 100 MB handheld: there an eighth is ~13 MB, the view
#: and a pan's worth of history, and the rest of the RAM stays free for the interpreter.
#: Overrunning it is not a slow map but a stopped one — the PicoCalc swaps to its SD card,
#: and a process whose code pages are being read back off it does nothing else meanwhile.
_MEMO_SHARE = 8

#: The budget's bounds: a floor so a small machine still keeps a view's worth, and a
#: ceiling past which more history buys a desktop nothing it would notice. A machine that
#: cannot say how much memory it has (Windows has no ``sysconf``) gets the ceiling.
_MEMO_FLOOR = 8 * 1024 * 1024
_MEMO_CEILING = 128 * 1024 * 1024


def _memo_budget() -> int:
    """How many bytes of decoded tiles this machine can hold in RAM (see :data:`_MEMO_SHARE`)."""
    try:
        physical = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        return _MEMO_CEILING
    if physical <= 0:
        return _MEMO_CEILING
    return max(_MEMO_FLOOR, min(_MEMO_CEILING, physical // _MEMO_SHARE))


#: How long a failed TileJSON resolve stands before the source will ask again. The PicoCalc
#: brings its Wi-Fi up some 40 seconds into the boot, well after the app it was started
#: alongside can reach the menu — so "offline" asked once at open is a verdict on the
#: *device's boot order*, not on the network, and latching it costs the whole session's
#: basemap. Long enough that a genuinely offline session isn't retrying into a void.
_RESOLVE_RETRY_SECONDS = 30.0

_log = logging.getLogger(__name__)


#: Where the operating system keeps its CA bundle, most specific first. Only consulted when
#: the default context came up empty (see :func:`_tls_context`) — macOS and Alpine put it at
#: ``/etc/ssl/cert.pem``, Debian and Ubuntu at ``ca-certificates.crt``, Fedora and RHEL under
#: ``/etc/pki``, openSUSE at ``ca-bundle.pem``. Files rather than a bundled copy on purpose:
#: these are the trust decisions the *machine's administrator* has made, kept current by the
#: OS, and shipping our own would quietly freeze a snapshot of them into every release.
_CA_BUNDLES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
)


@lru_cache(maxsize=1)
def _tls_context() -> ssl.SSLContext | None:
    """The TLS context for tile fetches, with a CA bundle found if the default has none.

    A frozen build can have no trust store to consult. Python's default context asks
    OpenSSL for one, and on macOS and Linux that is a *filesystem path baked into the
    interpreter's own build* — which need not exist on a machine that never installed that
    Python, which is every machine a PyInstaller bundle lands on. Every tile fetch then
    fails with ``CERTIFICATE_VERIFY_FAILED``; and because a refused fetch is
    indistinguishable from empty terrain, the map drew no ground under the nodes and said
    nothing about why. Windows is the exception that hid it for so long: its default
    context reads the OS certificate store, so the binary there always worked.

    So: keep the default when it actually loaded certificates, and otherwise point it at
    the bundle the OS ships (:data:`_CA_BUNDLES`). Verification is never weakened — a map
    tile is not worth teaching the app to skip certificate checks, and an unverified
    fetcher here would be one import away from being reused somewhere it matters.

    Returns:
        A context to pass to ``urlopen``, or ``None`` to accept urllib's default — which
        is right whenever that default already works, and is never worse than the failure
        it replaces.
    """
    try:
        context = ssl.create_default_context()
        if context.get_ca_certs():
            return None  # the platform store answered; leave urllib exactly as it was
        for path in _CA_BUNDLES:
            if os.path.isfile(path):
                context.load_verify_locations(cafile=path)
                _log.debug("no default CA store; verifying against %s", path)
                return context
        _log.debug("no CA bundle found in %s — TLS will likely fail", ", ".join(_CA_BUNDLES))
        return None
    except Exception as exc:  # noqa: BLE001 - a broken store must not take the map with it
        _log.debug("could not build a TLS context, using urllib's default: %s", exc)
        return None


class _Response(NamedTuple):
    """One HTTP GET's outcome: whether the server answered, and what it said.

    The distinction is the whole point. A definitive answer can be acted on — bytes to
    decode, or a 404 meaning there is no tile at those coordinates. Offline, DNS failure,
    timeout, connection reset, throttling, a server fault, a body short of its declared
    length: none of those tell us anything about the tile, and must never be mistaken for
    the source saying it is empty.

    Attributes:
        answered: Whether the server gave a definitive answer.
        body: The body it gave; empty when the answer was "no such resource".
    """

    answered: bool
    body: bytes


class BasemapSource:
    """A cached OpenStreetMap vector-tile source.

    Attributes:
        cache_dir: Directory under which tiles and the resolved TileJSON are cached.
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
        """Open a tile source backed by an on-disk cache.

        Args:
            cache_dir: Where fetched tiles and TileJSON metadata are stored.
            tilejson_url: URL of the source's TileJSON document.
            timeout: Per-request network timeout in seconds.
            layers: Restrict decoding to these layer names (the renderer passes
                :data:`meshterm.ui.map_render.DRAWN_LAYERS`). Only the *decode* narrows —
                the cache still stores whole tiles — so this is a pure CPU saving that a
                later renderer can widen without re-fetching anything.
            max_decoded_bytes: Ceiling on the decoded sidecar cache, which is derived data
                and so is the one part of the cache that can be thrown away freely.
            memo_bytes: Ceiling on the decoded tiles held in RAM; ``None`` sizes it to the
                machine (see :data:`_MEMO_SHARE`).
        """
        self.cache_dir = cache_dir
        self._tilejson_url = tilejson_url
        self._timeout = timeout
        self._layers = layers
        self._max_decoded_bytes = max_decoded_bytes
        self._decoded_written = 0
        # What a sidecar was decoded *under*. A blob written for one layer set must never
        # be served to a caller expecting another, so the set travels with the bytes.
        self._stamp = "all" if layers is None else ",".join(sorted(layers))  # type: ignore[arg-type]
        self._template: str | None = None
        self._max_zoom: int | None = None
        # When the source may next try to resolve its template. Zero means "now";
        # a success sets the template and this is never consulted again.
        self._resolve_after = 0.0
        # Tiles the source answered "nothing here" for this session. Held in memory rather
        # than on disk so the claim expires with the process: a blank tile is cheap to
        # re-ask about, and a stale one on disk is a permanent hole in the map.
        self._blank: set[tuple[int, int, int]] = set()
        # Decoded layers kept in RAM with what each weighs, most-recently-used last. The
        # sidecar already spares the protobuf decode, but a marshal load off the SD card is
        # still ~96 ms on the PicoCalc — and a map paints its whole viewport's worth of
        # tiles on *every* frame, so panning one dot re-read every tile that had not moved.
        # Bounded in bytes because decoded layers are the fattest thing this class holds,
        # and locked because the map's worker threads fill it while its paints read it.
        self._memo: OrderedDict[tuple[int, int, int], tuple[list[Layer], int]] = OrderedDict()
        self._memo_bytes = 0
        self._memo_budget = _memo_budget() if memo_bytes is None else memo_bytes
        self._memo_lock = threading.Lock()

    # -- metadata ---------------------------------------------------------------

    def _tilejson_path(self) -> Path:
        return self.cache_dir / "tilejson.json"

    def _resolve(self) -> None:
        """Resolve and cache the tile-URL template and max zoom (best-effort, retried).

        Latched on **success only**. A failed resolve is the absence of an answer, exactly
        as a failed tile fetch is (see :class:`_Response`), and remembering it as "this
        source is offline" is the same lie in a costlier place: it is asked once, seconds
        after the app opens, and its verdict then governs every tile for the rest of the
        session. On the PicoCalc the Wi-Fi associates some forty seconds into the boot, so
        an app started with it answers that question before the answer can be true and
        draws the whole session's map from whatever was already on disk — which is exactly
        a scatter of black tiles at the zooms whose ground the cache happens not to hold.

        So a failure only stands for :data:`_RESOLVE_RETRY_SECONDS`, and the next caller
        past that asks again. The deadline is claimed *before* the round-trip, so the
        several worker threads a map frame puts through here don't all make the same call.

        Blocking, and called from worker threads (a tile fetch, the menu's warm) — never
        from the paint path, which reads :attr:`available` instead.
        """
        if self._template is not None:
            return
        now = time.monotonic()
        if now < self._resolve_after:
            return
        self._resolve_after = now + _RESOLVE_RETRY_SECONDS
        # Prefer a freshly fetched TileJSON (the template is versioned and rotates), but fall
        # back to a previously cached copy so a session started offline can still use disk
        # tiles and even re-fetch if the template is still valid.
        resp = self._http_get(self._tilejson_url)
        data = resp.body if resp.answered and resp.body else None
        if data is not None:
            try:
                self._tilejson_path().parent.mkdir(parents=True, exist_ok=True)
                self._tilejson_path().write_bytes(data)
            except OSError:  # pragma: no cover - cache write failure is non-fatal
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
        except (ValueError, TypeError):  # pragma: no cover - malformed TileJSON
            pass

    @property
    def max_zoom(self) -> int:
        """The highest zoom the source serves (resolved lazily; sensible default offline)."""
        self._resolve()
        return self._max_zoom if self._max_zoom is not None else _DEFAULT_MAX_ZOOM

    @property
    def available(self) -> bool:
        """Whether a tile-URL template is already known — asking nothing to find out.

        Read on the paint path (the map's title, the mini-map's caption), so it must never
        be the thing that goes to the network: a resolve is a blocking round-trip, and one
        answered from a repaint would stall the UI for its whole timeout. Touching
        :attr:`max_zoom` — which every map surface does, off the loop, before it opens —
        is what resolves; a tile fetch resolves too, in its own thread. This only reports.

        False therefore means "no template *yet*", not "give up": a caller that skips its
        fetches on it will never let one happen (see
        :meth:`meshterm.ui.map_screen.MapScreen._ensure_tiles`, which asks regardless).
        """
        return self._template is not None

    def answered_empty(self, z: int, x: int, y: int) -> bool:
        """Whether the source *answered* that there is no tile at these coordinates.

        The one way to tell the two ``None`` returns of :meth:`load_tile` apart: a tile the
        source served as absent (a 404, or bytes holding no layer at all) will never
        become anything else, while a tile we simply got no answer about is worth asking
        again. Callers that cache the ``None`` need the difference — see
        :meth:`meshterm.ui.map_screen.MapScreen._load`.
        """
        return (z, x, y) in self._blank

    # -- tiles ------------------------------------------------------------------

    def _tile_path(self, z: int, x: int, y: int) -> Path:
        return self.cache_dir / "tiles" / str(z) / str(x) / f"{y}.pbf"

    def _decoded_path(self, z: int, x: int, y: int) -> Path:
        return self.cache_dir / "decoded" / str(z) / str(x) / f"{y}.bin"

    def load_tile(self, z: int, x: int, y: int) -> list[Layer] | None:
        """Return the decoded layers for a tile, from cache or the network.

        Three places are tried in cost order: the decoded sidecar (a ``marshal`` load),
        the raw ``.pbf`` (a full protobuf decode), then the network. The sidecar is pure
        derived data — it is never the reason a tile is considered known, and losing it
        costs only the decode it was there to save.

        The raw cache only ever holds tiles that decoded to real geometry. A tile the
        source never answered for is left uncached, so the next pan over it asks again
        instead of drawing a permanent blank; a cached file that no longer decodes is
        pruned for the same reason.

        Args:
            z: Tile zoom.
            x: Tile x index.
            y: Tile y index.

        Returns:
            The decoded layers, or ``None`` if the tile is unavailable (offline and
            uncached, or a genuinely empty/missing tile).
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
        """A tile's decoded layers if they are already in RAM, else ``None`` — never I/O.

        The one question a paint may put to the source. A map moving back over ground it
        has drawn would otherwise send every tile of it through a worker thread to fetch
        what is sitting in memory, and draw its first frame without them while it waited.
        """
        with self._memo_lock:
            hit = self._memo.get((z, x, y))
            if hit is None:
                return None
            self._memo.move_to_end((z, x, y))
            return hit[0]

    def warm(self, z: int, x: int, y: int) -> None:
        """Make a tile cheap to load later — fetched, decoded and written down — holding nothing.

        What a guess at the next move is for: the slow part of a tile nobody has looked at
        yet is the network and the decode, and the sidecar on disk is the whole of the
        answer to both. Holding the result in RAM as well spent the memory budget on tiles
        that may never be looked at, evicting ones that were, so a tile that already has
        its sidecar is passed over without being read.
        """
        if (z, x, y) in self._blank or self.resident(z, x, y) is not None:
            return
        if self._decoded_path(z, x, y).exists():
            return
        self._build(z, x, y)

    def _build(self, z: int, x: int, y: int) -> list[Layer] | None:
        """Decode a tile from its raw bytes — cached, else fetched — writing both caches.

        Holds nothing in RAM; that is :meth:`load_tile`'s decision, not this one's.
        """
        key = (z, x, y)
        path = self._tile_path(z, x, y)
        cached = self._read_cached(path)
        if cached is not None:
            layers = self._decode(cached, key)
            if layers is not None:
                self._write_decoded(z, x, y, layers)
                return layers
            # Nothing drawable came out: a zero-byte marker from a build that cached
            # network failures, or bytes truncated by a link (or a power cut) mid-write.
            # Either way it is a blank square for ever unless we drop it and re-ask.
            _log.debug("dropping unusable cached tile %s/%s/%s", z, x, y)
            self._discard_cached(path)
        raw = self._fetch_tile(z, x, y)
        if raw is None:
            return None  # no answer — nothing learned, so nothing is written down
        layers = self._decode(raw, key)
        if layers is None:
            self._blank.add(key)  # the source answered: there is genuinely nothing here
            return None
        self._write_cached(path, raw)
        self._write_decoded(z, x, y, layers)
        return layers

    def _remember(self, key: tuple[int, int, int], layers: list[Layer]) -> None:
        """Hold a decoded tile in RAM, evicting the least recently used past the budget.

        The newest tile always stays, even alone over budget: it was loaded because a view
        needs it, and evicting it would only send that view back to the disk for it.
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
        """Return a tile's already-decoded layers, or ``None`` to decode it properly."""
        blob = self._read_cached(self._decoded_path(z, x, y))
        return None if blob is None else loads_layers(blob, stamp=self._stamp)

    def _write_decoded(self, z: int, x: int, y: int, layers: list[Layer]) -> None:
        """Write a tile's decoded layers beside the raw bytes, and keep the dir bounded."""
        try:
            blob = dumps_layers(layers, stamp=self._stamp)
        except ValueError:  # pragma: no cover - a tag type marshal can't represent
            return
        self._write_cached(self._decoded_path(z, x, y), blob)
        # Sweeping the tree costs a directory walk on an SD card, so amortise it over a
        # good many writes rather than checking the budget on every tile.
        self._decoded_written += len(blob)
        if self._decoded_written >= _PRUNE_AFTER_BYTES:
            self._decoded_written = 0
            self._prune_decoded()

    def _prune_decoded(self) -> None:
        """Drop the least recently written sidecars until the budget is met.

        Decoded tiles run ~2.4x the size of the bytes they came from, and the raw cache is
        already unbounded, so this side of it gets a ceiling. Eviction is by modification
        time, which on a write-once file is also the order it was first needed in.
        """
        root = self.cache_dir / "decoded"
        try:
            files = [(p.stat().st_mtime, p.stat().st_size, p) for p in root.rglob("*.bin")]
        except OSError:  # pragma: no cover - a cache we can't walk is still readable
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
        """Decode a tile's bytes, or ``None`` if they aren't a tile at all.

        The question this answers is "are these real tile bytes?", and the answer decides
        whether a cache entry is kept or pruned — so it must not be confused with "is
        there anything here I feel like drawing". Under ``layers`` most of a tile's
        content is deliberately left undecoded, and a tile whose every layer we skip
        would otherwise read as blank and get its (perfectly good) cache entry deleted
        and re-downloaded every session.

        So the test is whether the bytes parsed into any layer at all. That is sound
        because the three ways a tile is *not* real are all distinguishable without
        looking at features: empty bytes decode to no layers, and truncated or junk bytes
        raise out of the parser rather than yielding a well-formed featureless layer.

        Args:
            raw: The tile's raw ``.pbf`` bytes.
            key: The ``(z, x, y)`` the bytes claim to be, for the log line.

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
        """Fetch a tile's raw bytes from the network.

        Args:
            z: Tile zoom.
            x: Tile x index.
            y: Tile y index.

        Returns:
            The body the source gave — possibly empty, meaning "no tile at those
            coordinates" — or ``None`` if it never answered (offline, timed out,
            throttled, faulted, or cut short).
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
        """Write a tile to the cache atomically, so a half-written one is never read back."""
        # Unique per writer: the map screen and a mini-map share one source, and both may
        # be fetching the same tile in their own worker threads.
        tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.part")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(raw)
            os.replace(tmp, path)
        except OSError:  # pragma: no cover - cache write failure is non-fatal
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _discard_cached(path: Path) -> None:
        """Drop a cache entry that proved unusable, so the tile can be fetched afresh."""
        try:
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - a cache we can't prune is still readable
            pass

    def _http_get(self, url: str) -> _Response:
        """GET a URL, telling a definitive answer apart from no answer at all.

        Args:
            url: The absolute URL to fetch.

        Returns:
            The outcome — see :class:`_Response`.
        """
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout, context=_tls_context()) as resp:
                body = resp.read()
                declared = (resp.headers.get("Content-Length") or "").strip()
                # A flaky link can end a read early without raising. A body short of its
                # declared length is a truncation, not a tile — and not an empty one.
                if declared.isdigit() and len(body) != int(declared):
                    _log.debug("short read for %s: %d of %s bytes", url, len(body), declared)
                    return _Response(False, b"")
                return _Response(True, body)
        except urllib.error.HTTPError as exc:
            answered = exc.code in _ABSENT_STATUSES
            if not answered:
                _log.debug("fetch failed for %s: HTTP %s", url, exc.code)
            return _Response(answered, b"")
        except Exception as exc:  # noqa: BLE001 - offline / timeout / reset are "no answer"
            _log.debug("fetch failed for %s: %s", url, exc)
            return _Response(False, b"")
