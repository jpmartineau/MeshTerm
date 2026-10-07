# SPDX-License-Identifier: Apache-2.0
"""The interactive full-screen map: a slippy map in the terminal, with pan and zoom.

This module controls the braille street map in the TUI. The screen owns a
:class:`~meshterm.core.geo.Viewport` over the nodes of the mesh. It downloads the vector
tiles for the viewport in the background (thus the UI never waits for the network), and it
draws the map again through :func:`~meshterm.ui.map_render.render_map`. The keys:

* The **arrow keys** pan. Hold **Shift** to pan by one character cell, for a fine
  position. On the PicoCalc, the Shift watcher supplies the modifier that the console
  removes. The console keymap changes Shift+↑/↓ into PgUp/PgDn, and this screen changes
  them back into fine pans: the PicoCalc has no physical PgUp key, so the code can have no
  other meaning. The keymap also removes Shift+←/→ fully. These keys do not get to the app
  until the console keymap maps them back to plain arrows.
* ``PgUp`` / ``PgDn`` zoom in / out.
* ``Home`` centres the viewport again and fits it to the dense core of the nodes. This is
  the area that the mesh covers (the ``Region`` chip), and it is also the default viewport
  when the map opens.
* ``^Y`` (for "you") centres the viewport on **our node**. It keeps the zoom that the user
  selected, and it clears the find query. The ``You`` chip on the F-key lane does the
  same, and its Shift half also zooms in close.
* **Type to find nodes.** Each letter key adds to a live name filter. The matching nodes
  keep bright labels, and the other nodes become dim, as context. ``Backspace`` edits the
  query. ``^Enter`` fits the viewport to the matches and keeps the query. ``Enter`` or
  ``Esc`` clears the query and does not move the viewport (a second ``Esc`` leaves the
  map). For this reason, no plain letter has an action on this screen.
* ``Esc`` leaves the map.

With no network (and no cached tiles), the basemap is absent and the map draws the nodes on
a blank grid. The map still works, but it has no streets.
"""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from collections.abc import Callable
from time import monotonic
from typing import TYPE_CHECKING

from ..core.geo import DEFAULT_VIEW_FRACTION, EARTH_RADIUS_KM, Viewport, clamp_lat
from ..core.mvt import Layer
from ..platforms import get_platform
from ..services import modifier_watch
from ..services.basemap import TILE_RETRY_SECONDS, BasemapSource
from . import attribution
from .map_render import Ghost, MapMarker, render_ground, render_map
from .tui.render import query_text
from .tui.screen import Screen

if TYPE_CHECKING:
    from ..context import AppContext

#: The fraction of the viewport that one (coarse) pan key press moves.
_PAN_STEP = 0.30

#: The unit pan direction (east, south) for each direction action or keyboard key.
_PAN_DIRS: dict[str, tuple[int, int]] = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}

#: How many zoom levels the map can go past the max zoom of the tile source. Above the
#: max zoom, the map magnifies the tiles of the lower zoom.
_OVERZOOM = 2


def _loop_running() -> bool:
    """Whether an event loop is available to do the background work.

    The code asks this before it makes a coroutine, not after. ``ensure_future`` without a
    loop raises an error. But at that time the coroutine exists and is never awaited, and
    Python reports this as a resource warning. The path is otherwise fully correct (a CLI
    export, or a test that renders a map with no loop).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


#: How many tiles MeshTerm can get at the same time. To get a tile is first a network
#: download, then a decode. A download is a wait, and threads overlap waits well. A decode
#: is pure Python, and threads do not overlap it: the GIL runs one decode at a time, and
#: each decode holds all the decoded data of its tile while it runs. With two, a download
#: can overlap a decode. A larger number only multiplies the data in memory when the user
#: moves fastest, and it uses the turns that the paint needs. This limit also makes a
#: queue, from which MeshTerm can remove the tiles that a fast pan left behind (refer to
#: :meth:`MapScreen._load`).
_TILE_LOADS = 2

#: Whether the map shows a **rough first pass** for a moved viewport before its finished
#: raster. The rough pass has the ground fills, the watercourses, and the through-roads, in
#: a quarter of the time (refer to :func:`~meshterm.ui.map_render.render_ground`). This
#: switch is **off** at this time. Each viewport waits for the full picture. Until then, the
#: map shows the ground of the last raster, reprojected
#: (:class:`~meshterm.ui.map_render.Ghost`), or nothing where the viewport is also past
#: that ground.
#:
#: The switch is here and not in the renderer, because it is a decision about when to ask,
#: not about what the renderer can draw. Both passes still work, and tests still examine
#: both. Between the passes, :meth:`MapScreen._draw_ground` still has the point where a
#: held pan key can stop the work for a viewport that it left. To turn the switch on
#: again, change this line.
_COARSE_PREVIEW = False

#: How many pans in one direction make a heading, instead of a small correction.
_PREFETCH_MOMENTUM = 2

#: How long the viewport must stay still before the prefetcher starts to guess. When a
#: raster finishes between two key presses of a pan, that is not a pause. A download that
#: starts there puts a tile decode before the next raster, and that delay was measured at
#: approximately 110 ms on the handheld. This time is long enough to see the difference
#: between a pause and a gap, and short enough to be in the time that the user reads.
_PREFETCH_SETTLE = 1.0

#: How many tiles the prefetcher asks for around one viewport before it stops. This is a
#: limit, not a target: usually the plan has no more tiles before this limit. The limit
#: stops a viewport at a corner of the tile grid from a walk through all the tiles around it.
_PREFETCH_MAX = 12

#: The zoom for a fit when the matches have no extent. A single node (or several nodes at
#: one spot) has no extent to fit, so ^Enter zooms to this street-level closeness, instead
#: of the neutral default of the fit. The zoom is capped at the max zoom of the tile source,
#: thus it never zooms in past that onto blank tiles.
_FIND_ZOOM = 16


class MapScreen(Screen):
    """A full-screen map of the located mesh nodes over an OSM basemap, with keyboard control."""

    floating = False
    #: The drawing goes fully to the side borders of the panel. A picture has no text edge
    #: to keep away from the borders, so the padding cells only take space from the map.
    flush = True

    #: Whether the printable keys go to the find-as-you-type node filter. The location
    #: picker sets this to off: there, typing has no function, and a filter that the user
    #: cannot see makes the context markers dim for no clear reason.
    find_enabled = True

    @property
    def picocalc_lyra_lane(self):
        r"""The PicoCalc lane: three ways to fit the viewport, then the zoom rocker.

        The map is the only screen that gives all the navigation actions new functions.
        PgUp/PgDn zoom, Home fits the viewport again, and ``end`` has no function. Thus
        the Shift-bank jumps of the shared lane have no target here, and F4/F5 are single
        slots. This is also the only exception to the rule that a Home/End verb goes on
        the Shift half of its pager (JP, 2026-08-08). ``Region`` is not a vertical move
        through a body. It is a destination, and it keeps the best slot, F1, next to the
        other two.

        All three destinations answer the question "where do I look?", from the least
        specific to the most specific: the full **Region** that the mesh covers, **You**
        at the centre of it, or only the nodes that a find query matches (**Frame**).
        Region always works. You works only when our node is on the map. Frame works only
        when a query has matches to fit. Thus, on a map with no filter, Frame is dim (and
        does nothing). It does not do the work of Region.

        Two slots have a Shift half on their own axis (JP, 2026-08-08). Behind You is
        **You +**: the same jump to our node, but zoomed in close. The ``+`` comes from
        the words of the zoom rocker, so the pair reads as "you / you, closer". Behind
        Frame is **Clear**: the other end of the find axis. Frame commits the query, and
        Clear removes it. Clear is enabled only while there is a query to clear. On a
        keyboard, Enter and Esc both clear the query. Where there is no hint line, the
        chip shows the user this function.

        The zoom pair keeps the left-right order of the lane (refer to
        :data:`~meshterm.ui.tui.fkeys.DEFAULT_LANE`): out on the left, in on the right.
        Thus F4/F5 read as the ``−``/``+`` rocker that they are.
        """
        from .tui.fkeys import FPair

        me = self._self_marker() is not None
        return [
            FPair("Region", "home"),
            FPair("You", "locate", "You +", "locate_zoom", enabled=me, opp_enabled=me),
            FPair(
                "Frame",
                "frame",
                "Clear",
                "clear_find",
                enabled=bool(self._filter) and bool(self._matches()),
                opp_enabled=bool(self._filter),
            ),
            FPair("Zoom -", "pagedown"),
            FPair("Zoom +", "pageup"),
        ]

    def __init__(
        self,
        session,  # noqa: ANN001 - TuiSession, imported lazily to avoid a cycle
        markers: list[MapMarker],
        source: BasemapSource,
        max_tile_zoom: int,
        *,
        saved_view: tuple[float, float, int] | None = None,
        on_view_change: Callable[[Viewport], None] | None = None,
        view_fraction: float = DEFAULT_VIEW_FRACTION,
        find: str = "",
    ) -> None:
        """Create the map screen.

        Args:
            session: The running TUI session (for the size, and to schedule a paint).
            markers: The located mesh nodes to draw (must not be empty).
            source: The vector-tile source (already resolved and warmed).
            max_tile_zoom: The max zoom of the source, read off the event loop when the
                map opens.
            saved_view: A stored ``(center_lat, center_lon, zoom)`` to open on again, or
                ``None`` to fit the viewport to the nodes.
            on_view_change: Called with the viewport each time the centre or the zoom
                changes, so that the caller can store it. Duplicates are removed: only a
                real change calls it.
            view_fraction: The fraction of the nodes that the default viewport (and the
                ``Home`` reset) fits. These are the densest nodes in that fraction, so
                that the outliers do not control the zoom. Refer to
                :meth:`geo.Viewport.fit`.
            find: A find query for the map to open with, the same as if the user typed
                it. The matching nodes are bright and the other nodes are dim (the
                node-detail page puts the name of its node here). The user can edit it
                and clear it with Esc, as any typed query. ``""`` is off.
        """
        super().__init__()
        self.title = "Map"
        self._session = session
        self._markers = markers
        self._source = source
        self._max_tile_zoom = max_tile_zoom
        self._saved_view = saved_view
        self._on_view_change = on_view_change
        self._view_fraction = view_fraction
        # The viewport that was last given to ``on_view_change``. Its first value is the
        # restored viewport, so that a map that opens again with no change does not write
        # it again.
        self._last_saved = saved_view
        #: The live find-as-you-type node filter ("" is off). Each printable key goes here,
        #: because the map gives no action to a letter. The render shows the matching
        #: markers bright and makes the other markers dim. A caller can give its first value
        #: (``find``), and that value acts the same as a query that the user typed.
        self._filter = find
        self._viewport: Viewport | None = None
        self._size: tuple[int, int] = (0, 0)  # the (dot_w, dot_h) of the viewport
        # Ask the session to clean the right edge of the panel at the next paint (refer to
        # :meth:`consume_edge_scrub`). The first value is ``True``, so that MeshTerm cleans
        # the edge of the first braille raster also before the first pan.
        self._needs_scrub = True
        #: Whether the user has touched this map yet. This value selects one of the two
        #: forms of the basemap credit (refer to :mod:`meshterm.ui.attribution`). The full
        #: line shows until the user presses a key, because the attribution must be visible
        #: without an interaction. On the first key that the map handles, the line changes
        #: to the short OpenStreetMap credit. This is the "automatically on map interaction
        #: such as panning, clicking, or zooming" clause of the OSMF guideline. The value
        #: is for one visit, not for one session: a map that opens again is a new arrival.
        self._untouched = True
        # The decoded tiles of the viewport, and nothing more (refer to
        # :meth:`_trim_tiles`). A stored ``None`` is the answer of the source that there
        # is nothing at those coordinates.
        self._tiles: dict[tuple[int, int, int], list[Layer] | None] = {}
        self._pending: set[tuple[int, int, int]] = set()
        # The turns of the tile loads (refer to :data:`_TILE_LOADS`). The prefetcher
        # uses the same turns.
        self._tile_gate = asyncio.Semaphore(_TILE_LOADS)
        # The tiles that the source gave no answer about, and the time when MeshTerm can
        # ask for each one again. This is a cooldown, not a final decision (refer to
        # :meth:`_load`).
        self._unanswered: dict[tuple[int, int, int], float] = {}
        # Data for the prefetcher (refer to :meth:`_prefetch_plan`): the direction in which
        # the viewport moved and for how many steps, the tiles that the prefetcher
        # already guessed, and the one download in progress.
        self._heading: str | None = None
        self._momentum = 0
        # The time when the viewport last stopped its change, and which viewport that was
        # (monotonic).
        self._settled_at = 0.0
        self._settled_view: Viewport | None = None
        self._speculated: OrderedDict[tuple[int, int, int], None] = OrderedDict()
        self._speculating = False
        # The last finished ground raster, and the key that it was drawn for (refer to
        # :meth:`render_body`). The raster of a downtown viewport is approximately
        # 0.5-1 s of pure Python (tens of thousands of vector features). This is much too
        # slow for a key press, so it occurs off the paint path, and the paint shows the
        # raster that is ready.
        self._frame: list[str] | None = None
        self._frame_key: tuple | None = None
        # Whether that raster is only the coarse first pass. If so, it still needs its
        # detail.
        self._frame_coarse = False
        # The ground of that raster. MeshTerm keeps it so that a viewport that moved can
        # show it until the viewport has its own ground (refer to :meth:`_ground`).
        self._ghost: Ghost | None = None
        self._drawing: tuple | None = None  # the key that is now in the rasterizer
        # The next raster to draw: its key, and the full scene for that key (the viewport,
        # the markers, and the find query), copied when the request is made (refer to
        # :meth:`_schedule_ground`).
        self._wanted: tuple[tuple, Viewport, list[MapMarker], str] | None = None

    # --- rendering -----------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The key hints, or the live find query.

        While a find filter is active, the query and its edit keys replace the hints.
        Thus the typed text is always visible in a fixed place.

        The line uses all of its 72 cells. Thus the two keys that move the viewport to a
        destination share one atom (``Home/^Y region/you``). The ``⇧ fine`` atom gives
        its space to them. That atom only refines a key that the line already names, and
        it is the only atom here that shows a modifier instead of a key binding. The
        state of the basemap was once a suffix at the end of this line. That state is a
        status atom, not a key, so we moved it to the title, where the standards chain the
        status atoms (refer to :meth:`_title`). That change put the line inside its 72
        cells.
        """
        if self._filter:
            return f"find: {self._filter}▏ · ^Enter frame · ⌫ erase · Enter/Esc clear"
        return "↑↓←→ pan · PgUp/PgDn zoom · Home/^Y region/you · type to find · Esc back"

    def consume_edge_scrub(self) -> int:
        """How many right-edge cells the session must paint again at the next paint (0 is none).

        When the terminal font has no glyph for a braille character, the terminal uses a
        double-width fallback glyph. That glyph pushes the row to the right and smears the
        right border of the panel. The map body is drawn again fully when it pans, so its
        cells repair themselves. But the border does not change from one frame to the
        next. Thus the differential paint of prompt_toolkit never writes the border again,
        and the smear stays there. After a move, we ask the session to force a paint of
        only the cells at that edge. This removes the smear without the flicker that a
        full paint of the frame causes. (The map is :attr:`flush`, so no padding cell
        between the drawing and the border needs this cleaning also.)
        """
        if not self._needs_scrub:
            return 0
        self._needs_scrub = False
        return 1  # the cell of the right border of the panel

    def set_markers(self, markers: list[MapMarker]) -> None:
        """Replace the drawn nodes on the open map, to add the nodes that arrived after it opened.

        The drawn raster is for the old overlay, so the map stops showing it. Until the new
        raster is ready, the map draws the nodes over the ground of the old raster, not
        over black (refer to :meth:`_ground`). The viewport stays where it is, because the
        user possibly moved it already.
        """
        self._markers = markers
        self._frame_key = None
        self._session.invalidate()

    def _query_echo(self) -> bool:
        """Whether this paint echoes the find query over the bottom line of the canvas.

        The echo occurs only where MeshTerm does not draw the footer
        (:attr:`~meshterm.platforms.Platform.footer_fkeys`). There, the hint line that has
        the query never gets to the screen. Without this line, the map filters itself with
        no visible sign, and the user does not know what they typed. On the desktop, the
        footer already shows the query, and the canvas keeps the full body.
        """
        return bool(self._filter) and get_platform().footer_fkeys

    def render_body(self, width: int) -> list[str]:
        """Build (or resize) the viewport, ask for its tiles, and render the map body.

        Some platforms need a query echo in the body (refer to :meth:`_query_echo`).
        There, the echo is drawn over the last line of the canvas, directly above the
        F-key lane, and not on a line above the map. An extra line at the top once moved
        all the ground down by one line when a find started (JP, 2026-08-08). The echo
        hides a strip of ground while a query is live. But the viewport never changes its
        size, so nothing jumps, and the pan/zoom geometry stays the same.

        The order at the end is important. The prefetcher guesses only while the screen
        has no work open for the user, and the work of this paint is not known until
        :meth:`_ground` asks for its raster. If the prefetcher reads this one step earlier
        (from :meth:`_ensure_tiles`, where it was before), it sees the answer of the
        previous paint. Then a settled map that gets a find key press starts a
        speculative download in the same paint that queues the raster that the user waits
        for.

        Where the frame has no bottom rule, the basemap credit is stamped last, over the
        right end of that same bottom line (refer to :mod:`meshterm.ui.attribution`).
        Elsewhere, the rule shows the credit (:attr:`bottom_caption`). The stamp comes
        last because the two forms of the credit depend on what the user pressed, not on
        what the raster shows. A raster takes half a second of Python, and MeshTerm shows
        it across many paints. Thus a credit in the raster comes late after the key press
        that makes it short, or it forces a new raster only to make a caption shorter.
        Where the query echo also needs this line, the two share it: the query on the
        left, the credit on the right. The query is the half that gives way.
        """
        vp = self._ensure_viewport(width)
        self._ensure_tiles(vp)
        self._persist()
        lines = self._ground(vp)
        self._ensure_prefetch(vp)  # last, because it reads what this paint asked for
        self.title = self._title(vp)
        echo = query_text(self._filter) if self._query_echo() else None
        return attribution.map_body(lines, width, full=self._untouched, left=echo)

    @property
    def bottom_caption(self) -> str:  # type: ignore[override]
        """The basemap credit, for a frame that has a bottom rule to show it.

        This is the other half of :func:`~meshterm.ui.attribution.map_body`, and the half
        that takes nothing from the drawing. Where the panel has a bottom border, the
        credit goes into it, right-justified, and is not stamped over the last line of the
        map (JP, 2026-09-13: "that way it's not in the map"). The value is empty where the
        frame has no rule for it. That is exactly where ``map_body`` stamps the credit
        instead. Thus, together, they draw the credit one time on each platform, and
        never two times.

        The frame reads this value at each paint, as it reads the title. Thus the change to
        :data:`~meshterm.ui.attribution.CREDIT_SHORT` gets to the rule at the same key
        press that causes it.
        """
        return attribution.rule_caption(full=self._untouched)

    def _ensure_viewport(self, width: int) -> Viewport:
        """The viewport for a body of ``width`` cells: built at the first paint, else resized.

        This is a separate method from :meth:`render_body`, because a subclass can need the
        viewport before it renders anything. The crosshair of the picker stays at the
        centre, so the picker must know the centre to build its marker (refer to
        :meth:`LocationPickScreen.render_body`).
        """
        _, cell_h = self._session.base_body_size()
        dot_w, dot_h = width * 2, max(1, cell_h) * 4
        if self._viewport is None:
            self._viewport = self._initial_viewport(dot_w, dot_h)
            self._size = (dot_w, dot_h)
        elif self._size != (dot_w, dot_h):
            self._viewport = self._viewport.resized(dot_w, dot_h)
            self._size = (dot_w, dot_h)
        return self._viewport

    def _ground_key(self, vp: Viewport) -> tuple:
        """All the inputs of the rasterized ground.

        The key holds the identities of the tiles, not their contents. The decoded layers
        of a tile never change after MeshTerm gets them. Thus a new tile is the only way
        for the picture to get more detail, and a new tile makes a new key here.
        """
        loaded = tuple(t for t in vp.tiles(self._max_tile_zoom) if self._tiles.get(t))
        return (vp, loaded, self._filter, len(self._markers))

    def _ground(self, vp: Viewport) -> list[str]:
        """The map picture for ``vp``: from the last raster if it is still correct, else soon.

        On the PicoCalc, the raster of a viewport takes 0.5-1 s of pure Python (a downtown
        raster projects tens of thousands of vector features). Thus the raster cannot
        occur between a key press and the paint that answers it. Instead, the paint always
        returns immediately, with the best picture that is available now. A background
        task draws the real picture, and asks for a paint when the picture is ready. There
        are four cases:

        * **Nothing changed.** The raster is exactly this viewport: show it. (If it is
          only the coarse first pass, ask for the finished pass after it.)
        * **Only the tiles changed** (a download arrived, and the viewport did not move).
          The previous raster is still correctly aligned, but some streets are missing.
          Continue to show it. Do not blank a good picture only to draw the same ground
          again.
        * **The viewport moved.** As a picture, the old raster is in the incorrect
          position. But its ground is still the only ground that is available. Reproject
          that ground onto the new viewport (:class:`~meshterm.ui.map_render.Ghost`), and
          draw the nodes over it at their real positions. This takes a few milliseconds,
          so the pan still follows the keys exactly. The streets move with the viewport:
          dim, and not up to the edge toward which the user pans. Without this, the map
          goes black between each key press and comes back when the raster is ready (JP,
          2026-08-09).
        * **A tile that this viewport shows did not arrive yet.** Draw nothing yet. A
          raster at this time paints the ground of that tile black. That raster is the
          raster of this viewport, so it replaces the ghost. Thus a zoom showed the ghost,
          then a black screen, then the map (JP, 2026-10-04). Also, MeshTerm discarded
          that raster, because the arrival of the tile asks for another raster. Thus the
          screen keeps what it has (the aligned raster, or the ghost) until the tiles are
          in. The next raster, which is the first, is the real picture. A tile that comes
          back as silence is not on its way any more, so a map with no network still draws
          what it can.

        The third case is where the map goes black and stays black. Four coarse pan steps
        are 120% of the screen, so no drawn ground is under the viewport, and there is no
        ground to reproject. The answer to that problem is not in this method, but in the
        method that it schedules. :meth:`_draw_ground` can show a rough picture in a
        quarter of the time, instead of nothing for a full raster. That is
        :data:`_COARSE_PREVIEW`, which is off at this time.
        """
        key = self._ground_key(vp)
        if self._frame_key == key and self._frame is not None:
            if self._frame_coarse:
                self._schedule_ground(key, vp)  # the first pass is visible, so finish it
            return list(self._frame)

        if any(t in self._pending for t in vp.tiles(self._max_tile_zoom)):
            # This viewport waits for its tiles, so a queued raster is for a viewport
            # that the user already left.
            self._wanted = None
        else:
            self._schedule_ground(key, vp)
        if self._aligned(key) and self._frame is not None:
            return list(self._frame)  # the same viewport, only the tiles differ: aligned
        # The viewport moved (or nothing was drawn yet): the markers over the last ground.
        return render_map(vp, {}, self._markers, find=self._filter, ghost=self._ghost)

    def _aligned(self, key: tuple) -> bool:
        """Whether the drawn raster is the picture of this viewport, with only less detail.

        All the parts of the key, except the tiles (``key[1]``), must match. A raster
        drawn for a different set of markers is not "the same picture missing streets".
        It is a picture with a missing node (for example, the crosshair of the picker). An
        aligned raster stays visible while the newer raster draws.

        This is a question about the full picture. For this reason, it is not the question
        that :meth:`_ground_drawn` asks.
        """
        if self._frame is None or self._frame_key is None:
            return False
        return self._frame_key[0] == key[0] and self._frame_key[2:] == key[2:]

    def _ground_drawn(self, key: tuple) -> bool:
        """Whether the ground of this viewport is fully drawn. If so, a rough pass undoes it.

        The two passes are for a viewport that went past all of the drawn ground. A
        viewport whose ground is already there does not want the first pass, because a
        coarse picture of ground that the user can already see removes detail (buildings,
        small streets, all the street names) for the time of a raster.

        This method asks only about the viewport, and that is the difference from
        :meth:`_aligned`. The find query and the marker count are part of the overlay. If
        one of them changes, the visible raster is the incorrect picture, and the map
        stops showing it. But the ground under it is the same ground. It is still drawn
        and still correct, and the next paint reprojects it at zero offset as the
        temporary picture (:class:`~meshterm.ui.map_render.Ghost`, published with the
        raster that it came from). When this method asked the fuller question, each letter
        of a find query changed the streets to the rough pass and then drew them again,
        one time for each key press.
        """
        if self._frame_key is None or self._frame_coarse:
            return False
        return self._frame_key[0] == key[0]

    def _schedule_ground(self, key: tuple, vp: Viewport) -> None:
        """Mark ``key`` for a draw, and start the draw if no other raster is in progress.

        Exactly **one** raster runs at a time, and it is always the newest one that was
        asked for. A held arrow key gives a new viewport at each paint, and a render takes
        most of a second. If a raster starts for each key press, a queue of work for the
        threads grows. Each raster in it is stale when it arrives, and the contention
        makes the key presses slow, which this method must prevent. Thus a request that
        arrives during a draw only replaces the pending request, and the draw that
        finishes starts it.

        The markers and the find query are **copied here**, with the viewport, so that
        the raster draws exactly the scene that ``key`` is for. The draw itself occurs
        later, on a thread, long after the paint that asked for it returned. And a marker
        list can change for each frame: the crosshair of the picker is added for the
        duration of one ``render_body`` and then removed immediately (refer to
        :meth:`LocationPickScreen.render_body`). If the draw reads the list at draw time,
        the crosshair is gone, and the finished basemap covers the crosshair and erases
        it.
        """
        if key == self._drawing or (key == self._frame_key and not self._frame_coarse):
            return
        self._wanted = (key, vp, list(self._markers), self._filter)
        if self._drawing is None:
            self._start_ground()

    def _start_ground(self) -> None:
        """Start the pending raster, or draw it inline where there is no event loop."""
        if self._wanted is None:
            return
        key, vp, markers, find = self._wanted
        self._wanted = None
        tiles = {t: self._tiles.get(t) for t in vp.tiles(self._max_tile_zoom)}
        if not _loop_running():
            # A static render (the map export of the CLI, a test). There is no user input
            # to answer quickly, so draw the raster here and now, instead of never.
            self._drawing = None
            self._frame, self._ghost = render_ground(vp, tiles, markers, find=find)
            self._frame_key, self._frame_coarse = key, False
            return
        self._drawing = key
        preview = _COARSE_PREVIEW and not self._ground_drawn(key)
        asyncio.ensure_future(self._draw_ground(key, vp, tiles, markers, find, preview))

    async def _draw_ground(
        self,
        key: tuple,
        vp: Viewport,
        tiles: dict,
        markers: list[MapMarker],
        find: str,
        preview: bool,
    ) -> None:
        """Rasterize one viewport off the event loop, coarse then finished, and paint after each.

        The work is pure Python, so a thread does not really run it in parallel. But the
        interpreter still switches between threads every few milliseconds, and that is
        the purpose: MeshTerm continues to answer key presses during the raster, and they
        do not wait for it (the worst delay measured was approximately 50 ms, against
        approximately 1 s for a draw that blocks).

        **Two passes, because a full raster is too large to wait for or to discard.** This
        is only when ``preview`` asks for both passes, and :data:`_COARSE_PREVIEW` does
        not at this time. A coarse pass (ground, water, through-roads, refer to
        :func:`~meshterm.ui.map_render.render_ground`) is a quarter of the work: 249 ms
        against 962 ms at zoom 13 on the PicoCalc. It is ready first. Thus a viewport that
        went past all of the drawn ground shows a true picture almost immediately, and it
        does not stay black. Then the finished pass replaces it in the same place.

        The gap between the passes is also the point where a moving viewport can stop the
        work. A coarse pan step is 30% of the screen, so after four key presses, nothing
        of the last raster is under the viewport, and the ghost has no ground to
        reproject. Before the check between the passes, each of those key presses waited
        for a full raster of a viewport that was already three steps stale. A check of
        :attr:`_wanted` between the passes makes the coarse pass the unit of work that
        MeshTerm can abandon. Thus a held pan gets real ground approximately four times as
        often, and the finished raster is drawn for the position where the user stopped.

        All the inputs of the raster come as arguments (refer to
        :meth:`_schedule_ground`). The state of the screen can change before the thread
        runs. The raster is stored under the key of the scene that it was asked for, so
        it must be exactly that scene.

        Args:
            key: The key under which the finished raster is stored.
            vp: The viewport to draw.
            tiles: The decoded tiles to draw from.
            markers: The nodes to overlay, copied at the time of the request.
            find: The live find filter, also copied at that time.
            preview: Whether to draw the coarse pass first. This is always off while
                :data:`_COARSE_PREVIEW` is off. Also, MeshTerm skips the coarse pass where
                the ground of this viewport is already fully drawn (refer to
                :meth:`_ground_drawn`), because there a coarse picture removes detail that
                the user can already see.
        """
        if preview and await self._pass(key, vp, tiles, markers, find, coarse=True):
            if self._wanted is not None:
                self._drawing = None  # the viewport moved, so draw where it is now
                self._start_ground()
                return
        await self._pass(key, vp, tiles, markers, find, coarse=False)
        self._drawing = None
        if self._wanted is not None:
            self._start_ground()

    async def _pass(
        self,
        key: tuple,
        vp: Viewport,
        tiles: dict,
        markers: list[MapMarker],
        find: str,
        *,
        coarse: bool,
    ) -> bool:
        """Draw one pass off the loop and publish it. Return whether the pass completed."""
        try:
            drawn = await asyncio.to_thread(
                render_ground, vp, tiles, markers, find=find, coarse=coarse
            )
        except Exception:  # noqa: BLE001 - if a raster fails, MeshTerm draws it again later
            return False
        lines, self._ghost = drawn
        self._frame, self._frame_key, self._frame_coarse = lines, key, coarse
        self._needs_scrub = True
        self._session.invalidate()
        return True

    def _initial_viewport(self, dot_w: int, dot_h: int) -> Viewport:
        """Restore the saved viewport (clamped to sane limits), or fit the dense core of the nodes.

        With no saved viewport, the default fits the ``view_fraction`` of the nodes that are
        nearest to the median centre (half of the nodes by default). Thus the distant
        outliers do not zoom the full mesh out to a scale that has no use.
        """
        if self._saved_view is not None:
            lat, lon, zoom = self._saved_view
            z = max(2, min(int(zoom), self._max_tile_zoom + _OVERZOOM))
            return Viewport(clamp_lat(lat), lon, z, dot_w, dot_h)
        return Viewport.fit(
            [(m.lat, m.lon) for m in self._markers],
            dot_w,
            dot_h,
            max_zoom=self._max_tile_zoom,
            fraction=self._view_fraction,
        )

    def _persist(self) -> None:
        """Give the centre and zoom to ``on_view_change`` if they changed after the last call."""
        vp = self._viewport
        if vp is None or self._on_view_change is None:
            return
        view = (vp.center_lat, vp.center_lon, vp.zoom)
        if view == self._last_saved:
            return
        self._last_saved = view
        self._on_view_change(vp)

    def _matches(self) -> list[MapMarker]:
        """The markers that the live find filter matches now (all of them when it is off)."""
        needle = self._filter.strip().casefold()
        if not needle:
            return self._markers
        return [m for m in self._markers if needle in m.label.casefold()]

    def _title(self, vp: Viewport) -> str:
        """A compact status title: the zoom, the node count (or the find matches), and the scale.

        When the basemap has a state to report, that state is the last atom. The state
        replaces the scale, and it is not added after it. The states are: tiles still in
        progress, a raster still in progress, or no source. The state and the scale both
        answer "how much ground do I see", and the state is the more urgent of the two
        while it lasts. A replacement (instead of an addition) keeps the title inside a
        title bar of 53 cells, which clips the title and does not wrap it.
        """
        # The metres of ground for each braille dot at the centre of the viewport, as an
        # approximate scale.
        m_per_dot = (
            2
            * math.pi
            * EARTH_RADIUS_KM
            * 1000
            * math.cos(math.radians(vp.center_lat))
            / (256 * (2**vp.zoom))
        )
        scale = (
            f"{m_per_dot * vp.dot_w:.0f} m across"
            if m_per_dot * vp.dot_w < 1000
            else f"{m_per_dot * vp.dot_w / 1000:.1f} km across"
        )
        if self._pending:
            scale = f"{len(self._pending)} tiles…"
        elif self._drawing is not None or self._wanted is not None:
            # The ground for this viewport is still in the rasterizer, off the paint path
            # (refer to :meth:`_ground`). Thus the screen shows only the nodes, or the
            # streets of the last viewport. Say so, in the same slot as the tile downloads.
            scale = "drawing…"
        elif not self._source.available:
            scale = "offline"
        if self._filter:
            nodes = f"{len(self._matches())} of {len(self._markers)} match"
        else:
            nodes = f"{len(self._markers)} nodes"
        return f"Map · z{vp.zoom} · {nodes} · {scale}"

    # --- tiles ---------------------------------------------------------------

    def _ensure_tiles(self, vp: Viewport) -> None:
        """Schedule background downloads for each visible tile that we do not have.

        The downloads are only for the tiles that are not already pending and not in a
        cooldown. Each paint calls this method, and on purpose it does **not** check
        :attr:`~meshterm.services.basemap.BasemapSource.available` first. That attribute
        only reports whether a template is known yet. If the download is skipped while the
        template is not known, a map that opened a moment too early stays empty for the
        remainder of the session. The download resolves the source itself, in its own
        thread, so the request is what makes the source work again.

        When the source gave no answer about a tile, that tile waits for
        :data:`~meshterm.services.basemap.TILE_RETRY_SECONDS` in :attr:`_unanswered`.
        Then MeshTerm asks for it again. Refer to :meth:`_load` for why this is not the
        same as a tile for which the source answered "nothing here".

        When the source already holds a tile in RAM, this method takes it immediately,
        with no thread between. Thus ground that the user pans back onto is drawn in the
        same raster that the move asks for. Without this, the raster is drawn first
        without the tile, and then again after a round trip to memory returns.
        """
        wanted = vp.tiles(self._max_tile_zoom)
        self._trim_tiles(wanted)
        for t in wanted:
            if t not in self._tiles:
                layers = self._source.resident(*t)
                if layers is not None:
                    self._tiles[t] = layers
        if not _loop_running():  # no loop for downloads: a static render draws what it has
            return
        self._expire_cooldowns()
        for t in wanted:
            if t in self._tiles or t in self._pending or t in self._unanswered:
                continue
            self._pending.add(t)
            asyncio.ensure_future(self._load(t))

    def _expire_cooldowns(self) -> None:
        """Forget the expired cooldowns, so that a long pan does not collect them."""
        if not self._unanswered:
            return
        now = monotonic()
        self._unanswered = {t: at for t, at in self._unanswered.items() if at > now}

    # --- anticipation ---------------------------------------------------------

    def _ensure_prefetch(self, vp: Viewport) -> None:
        """Download one tile that the next move will probably need, but only when all is quiet.

        On the PicoCalc, a tile takes 1.4 s to download over Wi-Fi, and a few hundred
        milliseconds to decode the first time. Thus, if MeshTerm downloads ground only
        when the user looks at it, the ground arrives after the user wanted it. A
        download one move early costs nothing that the user can feel, on the condition
        that it never blocks other work. That condition is the full pacing rule here. The
        prefetcher runs only when the viewport from which it guesses is finished, and was
        finished for a moment: all the visible tiles are in, no raster runs, nothing is
        queued, and :data:`_PREFETCH_SETTLE` has passed after that. These are the seconds
        in which the user reads the screen, which are also the seconds before the user
        moves. Because of the wait, a raster that arrives between two key presses of a
        pan does not look like a pause. The wait starts when the screen became quiet, not
        when the viewport stopped its movement, because the user did not look for one
        second at a raster that took one second to draw.

        Only one download runs at a time. When it finishes, it asks for the next one
        itself (:meth:`_prefetch`), and it does not wait for a paint. A chain through the
        paint costs an intermediate frame of 40-80 ms for each tile on the handheld, only
        to show a picture that did not change.

        All of this works only because the paint calls this method at its end, after
        :meth:`_ground` has said what this frame still needs. Refer to
        :meth:`render_body`.
        """
        # Work for the user is still open: a tile in progress, or a raster that runs or
        # is queued.
        busy = bool(self._pending) or self._drawing is not None or self._wanted is not None
        if busy or self._settled_view is not vp:
            self._settled_view, self._settled_at = vp, monotonic()
        if busy or self._speculating or not _loop_running():
            return
        if monotonic() - self._settled_at < _PREFETCH_SETTLE:
            return  # a raster between two key presses of a pan is a gap, not a pause
        tile = self._next_speculation(vp)
        if tile is None:
            return
        self._speculating = True
        self._remember_speculation(tile)
        asyncio.ensure_future(self._prefetch(tile, vp))

    def _next_speculation(self, vp: Viewport) -> tuple[int, int, int] | None:
        """The first tile in :meth:`_prefetch_plan` that we do not have and did not guess.

        This method also skips a tile that is in its cooldown after a silence
        (:attr:`_unanswered`). That is the same rule as the pacing above, not an extra
        rule. The cooldown exists because the source just became silent about that square,
        and many requests for it help nobody (refer to :meth:`_load`). A guess is the last
        request that must be an exception to the cooldown. The tile comes back into the
        plan when the cooldown expires. At that time, the user possibly panned onto it
        already. Then the download is for the user, and the cooldown was always sized for
        that download.
        """
        for tile in self._prefetch_plan(vp):
            if tile in self._tiles or tile in self._pending or tile in self._speculated:
                continue
            if tile in self._unanswered:
                continue  # the source just became silent about it, so a guess must not push
            return tile
        return None

    def _prefetch_plan(self, vp: Viewport) -> list[tuple[int, int, int]]:
        """The tiles that the next few moves will probably need, in the order of that need.

        It is not necessary to model what the user does next. For each key that the user
        can press, the new ground comes from the **ring of tiles around the visible
        tiles** (a pan in any direction shows that ring), or from one of the two
        neighbouring zooms. Only a zoom move gives a fully different set of tiles. Thus
        the plan is that ring and those two viewports, in an order that comes from the
        last action of the user:

        * **Ahead first.** A pan is almost never alone, because the user moves the
          viewport toward a place. The prefetcher gets the side of the ring in the
          direction of the heading before all others. When the run is a heading and not a
          small correction (:data:`_PREFETCH_MOMENTUM`), the corners on each side of that
          side come too, because a diagonal is two keys and users steer.
        * **Then out, then in.** A zoom out is how the user finds their position, and it
          is the move that shares nothing with the screen: a step out is four times the
          ground, at a tile zoom that the user did not visit. A step in costs little to
          ask for, and often costs nothing to answer: at or above the max zoom of the
          source, it uses the same tiles, magnified.
        * **Then the rest of the ring**, for a user who did not move yet or who will
          change direction. With no heading, the rest of the ring is the full ring, with
          the nearest neighbours before the corners, and it comes before the zooms. That
          is the correct hedge when there is no direction to read.

        The plan uses tiles and not pan steps. This is important at the zooms where one
        step shows no new tile: the plan asks for the ground that a move gets to, not the
        ground under one key press.

        The plan does not include, on purpose, the nodes to which a find can jump. Those
        nodes are one key press away everywhere on the map, so a guess at them is a guess
        at everything. Also, the jump fits the viewport again, and it arrives here as a
        new viewport with its own plan.
        """
        visible = vp.tiles(self._max_tile_zoom)
        if not visible:
            return []
        tz = visible[0][0]
        span = 2**tz
        xs = [x for _, x, _ in visible]
        ys = [y for _, _, y in visible]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        seen = set(visible)

        ahead: list[tuple[int, int, int]] = []
        flank: list[tuple[int, int, int]] = []
        hx, hy = _PAN_DIRS.get(self._heading or "", (0, 0))
        corners = self._momentum >= _PREFETCH_MOMENTUM
        for ty in range(y0 - 1, y1 + 2):
            if not 0 <= ty < span:
                continue
            for tx in range(x0 - 1, x1 + 2):
                tile = (tz, tx % span, ty)  # wrap x around the antimeridian, as tiles() does
                if tile in seen:
                    continue
                seen.add(tile)
                # The direction from the visible block to this tile: -1, 0, or +1 on each
                # axis.
                off_x = -1 if tx < x0 else (1 if tx > x1 else 0)
                off_y = -1 if ty < y0 else (1 if ty > y1 else 0)
                towards = off_x * hx + off_y * hy
                if towards > 0 and (corners or not (off_x and off_y)):
                    ahead.append(tile)
                else:
                    # With no heading: the straight neighbours before the corners. With a
                    # heading: each tile that is not ahead is equally a change of
                    # direction, so the distance decides.
                    flank.append(tile)
        flank.sort(key=lambda t: abs(t[1] - (x0 + x1) // 2) + abs(t[2] - (y0 + y1) // 2))

        zooms: list[tuple[int, int, int]] = []
        for view in (vp.zoomed(-1), vp.zoomed(1, max_zoom=self._max_tile_zoom + _OVERZOOM)):
            for tile in view.tiles(self._max_tile_zoom):
                if tile not in seen:
                    seen.add(tile)
                    zooms.append(tile)
        # With a heading: ahead, then the zooms, then the change of direction. With no
        # heading: each direction of a pan first, because the user controls this screen
        # with the arrows, and a zoom tile before them is a tile that the pan then waits
        # for.
        plan = ahead + zooms + flank if ahead else flank + zooms
        return plan[:_PREFETCH_MAX]

    def _remember_speculation(self, tile: tuple[int, int, int]) -> None:
        """Mark ``tile`` as already guessed, and keep the list of these marks limited.

        Guessed, not held: a prefetch leaves a decoded tile in the cache of the source, on
        disk, and that is the purpose. The screen keeps only the tiles that it draws. This
        list only stops the plan from asking two times, and it can forget a tile, because
        the worst cost of that is a cache hit.
        """
        self._speculated[tile] = None
        self._speculated.move_to_end(tile)
        while len(self._speculated) > _PREFETCH_MAX * 4:
            self._speculated.popitem(last=False)

    async def _prefetch(self, tile: tuple[int, int, int], vp: Viewport) -> None:
        """Warm one tile into the disk cache of the source, then make the next guess.

        Disk, not memory (:meth:`~meshterm.services.basemap.BasemapSource.warm`). This
        screen must not hold a prefetched tile, because :attr:`_tiles` is the working set
        of the viewport. The RAM of the source must not hold it either. That memory budget
        is the history of where the user went, and twelve guesses at up to 4.6 MB each can
        evict all of it, for ground that the user possibly never visits. A guess saves the
        network and the decode, and the sidecar on disk holds the result of both. Thus the
        paint that needs the tile later reads it in a tenth of a second, and does not wait
        a second and a half. When the sidecar of a tile is already written, the guess
        costs nothing.

        The prefetch takes a turn at the tile gate, as each tile load does
        (:data:`_TILE_LOADS`). Thus, when the user moves while it runs, a turn is still
        available for the tiles that the user moved onto.

        There is no paint after the prefetch: nothing visible changed, and the purpose of
        this early work was to not use the time of the user.
        """
        async with self._tile_gate:
            try:
                await asyncio.to_thread(self._source.warm, *tile)
            except Exception:  # noqa: BLE001 - a guess that did not help is not an error
                pass
        self._speculating = False
        if self._viewport is vp:  # the same viewport, so the same plan: continue
            self._ensure_prefetch(vp)

    def _trim_tiles(self, wanted: list[tuple[int, int, int]]) -> None:
        """Release each tile with geometry that the viewport does not show now.

        The history that a pan back needs is in the source. The memory of the source has a
        budget in bytes, against the RAM of the machine (refer to
        :meth:`~meshterm.services.basemap.BasemapSource.resident`). Thus a tile that this
        screen also holds is outside that budget. That is how the map froze on the
        PicoCalc before: approximately eight tiles of history here, at up to 4.6 MB each,
        added to the history of the source, pushed a handheld with 100 MB into swap on its
        SD card.

        This method removes only the tiles with geometry. An entry with the value ``None``
        is the statement of the source that the tile is absent. It is a final answer that
        costs a dict slot, not a megabyte, so it stays. If this method removes it, the
        next paint only asks for it again, for no purpose. A tile that got no answer is
        not here: it waits for its cooldown in :attr:`_unanswered`, and then MeshTerm asks
        for it again.

        Args:
            wanted: The tiles that the current viewport needs.
        """
        keep = set(wanted)
        for key in [k for k, layers in self._tiles.items() if layers and k not in keep]:
            del self._tiles[key]

    async def _load(self, t: tuple[int, int, int]) -> None:
        """Download and decode one tile off the event loop, then paint.

        The result goes into one of the same two groups that the tile source keeps (refer
        to :class:`~meshterm.services.basemap._Response`), because here, if MeshTerm
        forgets the difference, the map loses a picture. Geometry, or the statement of the
        source that there is nothing at those coordinates, is an answer. It goes in
        :attr:`_tiles`, and MeshTerm never asks about it again. Silence (offline, a
        timeout, a body that the Wi-Fi cut short) is not an answer. If MeshTerm stores it
        as an answer, one bad moment becomes a black square that stays black until the app
        starts again. Thus silence goes in :attr:`_unanswered`, which is a cooldown, not a
        final decision.

        That difference is visible only on a link that really drops. On the PicoCalc, it
        is the difference between a map and a map with holes in it (JP, 2026-08-18: tiles
        black at the two highest zooms, where one z14 tile is the full screen).

        A tile load waits for its turn (:data:`_TILE_LOADS`). If the viewport left the
        tile before its turn comes, MeshTerm **removes the tile without a load**. A fast
        pan or a series of zoom steps asks for each viewport that it goes through. The load
        of all of them (each a decode of a second or more, and the zoomed-out tiles are
        the heaviest) was the backlog that kept the map behind the keys long after they
        stopped. A removed tile is not marked as lost: MeshTerm stores nothing about it, so
        the next paint that needs the tile asks for it as for any other tile.
        """
        async with self._tile_gate:
            vp = self._viewport
            if vp is not None and t not in vp.tiles(self._max_tile_zoom):
                self._pending.discard(t)
                return
            try:
                layers = await asyncio.to_thread(self._source.load_tile, *t)
            except Exception:  # noqa: BLE001 - a failed tile is only an absent tile
                layers = None
        self._pending.discard(t)
        if layers is not None or self._source.answered_empty(*t):
            self._tiles[t] = layers
        else:
            self._unanswered[t] = monotonic() + TILE_RETRY_SECONDS
        self._session.invalidate()

    # --- input ---------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Pan, zoom, fit the viewport again, edit the find filter, or leave.

        Each printable key goes to the find filter. No letter pans or zooms, so when the
        user types a node name, the viewport never jumps around. Esc removes one layer:
        first an active filter, and the map itself only when the filter is clear.

        Three actions fit the viewport again, and each has a key and an F-key chip:
        ``home`` fits the full region, ``locate`` (^Y, for "you") goes to our node, and
        ``frame`` (^Enter) fits the find matches. ``locate_zoom`` is the Shift-bank
        variant of ``locate`` that also zooms in. ``clear_find`` clears the query and does
        not move the viewport, and plain **Enter** does the same.

        Enter and ^Enter are the two ways out of a find, and the difference is whether
        the viewport moves (JP, 2026-08-09). A query makes dim each node that does not
        match. The usual end is "yes, that one, now let me look around it": the query did
        its work, and only the dim nodes are in the way. Thus Enter clears the query and
        leaves the viewport exactly where the user put it. ^Enter is the other end ("take
        me to them"), and it keeps the query. A fit is a place to arrive, and a tighter
        fit from there is one more key press, not a new query.
        """
        vp = self._viewport
        if action == "escape":
            if self._filter:
                self._filter = ""
            else:
                self.resolve(None)
                return
        if vp is None:
            return
        if action in _PAN_DIRS:
            # The console can report a Shift arrow as the bare arrow (a keymap that
            # removes the modifier). This arrow still does a fine pan, because the watcher
            # knows whether Shift is physically held. The watcher stays False wherever it
            # does not watch: the desktop terminals report shift_up/... themselves, on
            # the branch below.
            self._pan(vp, action, fine=modifier_watch.shift_down())
        elif action.startswith("shift_") and action[len("shift_") :] in _PAN_DIRS:
            self._pan(vp, action[len("shift_") :], fine=True)
        elif action == "pageup":
            if modifier_watch.shift_down():
                # The keymap of the PicoCalc console changes Shift+↑ into PgUp (measured
                # on the handheld, JP 2026-08-09: Shift with a vertical arrow zoomed the
                # map). That keyboard has no physical PgUp key (the pager is on the F-key
                # lane). Thus a raw PgUp with Shift held can only be a Shift arrow: do a
                # fine pan.
                self._pan(vp, "up", fine=True)
            else:
                self._reorient()
                self._viewport = vp.zoomed(1, max_zoom=self._max_tile_zoom + _OVERZOOM)
        elif action == "pagedown":
            if modifier_watch.shift_down():
                self._pan(vp, "down", fine=True)  # Shift+↓ arrives as PgDn, as above
            else:
                self._reorient()
                self._viewport = vp.zoomed(-1)
        elif action in ("home", "ctrl_home"):
            self._reorient()
            self._reset_view(vp)
        elif action == "locate":
            self._reorient()
            self._locate(vp)
        elif action == "locate_zoom":
            self._reorient()
            self._locate(vp, zoom_in=True)
        elif action in ("clear_find", "enter"):
            self._filter = ""
        elif action in ("frame", "ctrl_enter") and self._filter:
            self._reorient()
            self._frame_matches(vp)
        elif action == "text" and self.find_enabled:
            if not data.isspace() or self._filter:  # never start the filter with a space
                self._filter += data
        elif action == "space" and self._filter:
            self._filter += " "  # node names have spaces, which are useful only mid-query
        elif action == "backspace":
            self._filter = self._filter[:-1]
        # A handled key can change the body, so clean the right edge at the next paint.
        # This is also the moment when the user does more than arrive at the map. Thus the
        # basemap credit changes to its short form (refer to :attr:`_untouched`).
        self._needs_scrub = True
        self._untouched = False
        self._persist()

    def _self_marker(self) -> MapMarker | None:
        """Our node among the markers, or ``None`` when the map cannot put us on it.

        A device with no location fix of its own is absent from the marker list. Thus
        ``You`` has no destination, and its chip is dim (refer to
        :attr:`picocalc_lyra_lane`).
        """
        return next((m for m in self._markers if m.is_self), None)

    def _locate(self, vp: Viewport, *, zoom_in: bool = False) -> None:
        """Centre the viewport on our node (``^Y`` or the ``You`` chip), and clear the find query.

        Plain ``You`` keeps the zoom that the user selected. Two presses do the same thing
        two times, and one more press of Zoom + adds the zoom. Its Shift half (``You +``)
        also zooms in, to the same street-level closeness as a Frame with one match
        (:data:`_FIND_ZOOM`). Both clear the find query (JP, 2026-08-08). A jump to our
        node while a filter makes the other nodes dim (or matches nothing, and also not
        us) looks like a new start, and the map must agree.
        """
        me = self._self_marker()
        if me is None:
            return
        self._filter = ""
        zoom = min(_FIND_ZOOM, self._max_tile_zoom) if zoom_in else vp.zoom
        self._viewport = Viewport(clamp_lat(me.lat), me.lon, zoom, vp.dot_w, vp.dot_h)

    def _reset_view(self, vp: Viewport) -> None:
        """Fit the viewport to the dense core of the nodes again (as when the map opens)."""
        self._viewport = Viewport.fit(
            [(m.lat, m.lon) for m in self._markers],
            vp.dot_w,
            vp.dot_h,
            max_zoom=self._max_tile_zoom,
            fraction=self._view_fraction,
        )

    def _frame_matches(self, vp: Viewport) -> None:
        """Fit the viewport to the matches of the find filter (^Enter on an active find).

        The fit includes all the matches (``fraction=1.0``: the user asked for exactly
        these nodes, so the fit does not cut down to a dense core). A single match (or
        several matches at one spot) has no extent to fit. Thus, instead of the neutral
        default of the fit, the viewport zooms in close on it (:data:`_FIND_ZOOM`, capped
        at the max zoom of the tile source, thus it never zooms in past that onto blank
        tiles). When there are no matches, the viewport does not change.
        """
        matches = self._matches()
        if not matches:
            return
        self._viewport = Viewport.fit(
            [(m.lat, m.lon) for m in matches],
            vp.dot_w,
            vp.dot_h,
            max_zoom=self._max_tile_zoom,
            default_zoom=min(_FIND_ZOOM, self._max_tile_zoom),
            fraction=1.0,
        )

    def _reorient(self) -> None:
        """Forget the direction of the viewport movement, because this move does not continue it.

        With a zoom or a jump to a destination, the user looks around, and does not
        travel. The ground where the viewport arrives does not show which direction the
        user will take from there. The prefetcher reads the cleared heading as "no
        direction known", and it hedges equally in the four directions. It does not get
        ground ahead of a pan that has ended.
        """
        self._heading, self._momentum = None, 0

    def _pan(self, vp: Viewport, direction: str, *, fine: bool) -> None:
        """Pan by one coarse step, or by one character cell when ``fine`` is true.

        A character cell is 2 braille dots wide and 4 dots tall. Thus the fine step is
        that number of dots, as a fraction of the current viewport.
        """
        dx, dy = _PAN_DIRS[direction]
        # The direction of the viewport movement, and for how many steps. The prefetcher
        # reads this to find the difference between a heading and a small correction
        # (refer to :meth:`_prefetch_plan`). A fine step is a correction, not a
        # direction, so it keeps the heading and does not extend it.
        if not fine:
            self._momentum = self._momentum + 1 if direction == self._heading else 1
            self._heading = direction
        if fine:
            self._viewport = vp.panned(dx * 2 / vp.dot_w, dy * 4 / vp.dot_h)
        else:
            self._viewport = vp.panned(dx * _PAN_STEP, dy * _PAN_STEP)


class LocationPickScreen(MapScreen):
    """The map, used as a coordinate picker: pan the crosshair, and press Enter to select.

    The config editor and Repeater admin use it to set the advertised location of a node.
    The user points at the map, and does not type degrees. It is a :class:`MapScreen`
    with three changes:

    * A crosshair marker stays at the centre of the viewport. Its label shows the live
      coordinates, so the user always sees exactly what they will select.
    * Enter resolves with the ``(lat, lon)`` of the centre, and does not fit the viewport
      to find matches (find is off here, refer to :attr:`MapScreen.find_enabled`).
    * ``Home`` centres the viewport on the initial location again, and does not fit the
      viewport to the nodes.

    The mesh nodes around the crosshair are still drawn, so it is easy to put yourself
    at a position relative to a known repeater. Esc cancels (it resolves CANCEL, which
    the caller shows as ``None``).
    """

    find_enabled = False

    @property
    def picocalc_lyra_lane(self):  # type: ignore[override]
        """The lane of the map, without the two verbs that a picker cannot use.

        ``Frame`` goes, because find is off here. The user can never type a query to fit,
        so the slot is empty, not dim. ``You`` goes, because the full screen is about the
        selection of where "you" will be. The crosshair at the centre is already that
        answer. A chip that jumps to the position that the node has now does not help the
        selection, but competes with it. ``Home`` keeps F1, but with a new function (the
        chip is ``Start``). Here it returns to the viewport that the picker **opened** on,
        so one key undoes a pan that went wrong.
        """
        from .tui.fkeys import FPair

        return [
            FPair("Start", "home"),
            None,
            None,
            FPair("Zoom -", "pagedown"),
            FPair("Zoom +", "pageup"),
        ]

    def __init__(
        self,
        session,  # noqa: ANN001 - TuiSession, imported lazily to avoid a cycle
        markers: list[MapMarker],
        source: BasemapSource,
        max_tile_zoom: int,
        *,
        initial: tuple[float, float] | None = None,
        zoom: int = 13,
    ) -> None:
        """Create the picker.

        Args:
            session: The running TUI session (for the size, and to schedule a paint).
            markers: The located mesh nodes to draw as context (can be empty).
            source: The vector-tile source (already resolved and warmed).
            max_tile_zoom: The max zoom of the source, read off the event loop when the
                picker opens.
            initial: The location at the centre when the picker opens (the current
                position of the node), or ``None`` to fit the viewport to the mesh. When
                no node is located either, the picker shows a viewport of the world.
            zoom: The zoom when the picker opens, if ``initial`` is given.
        """
        saved = (initial[0], initial[1], zoom) if initial is not None else None
        super().__init__(session, markers, source, max_tile_zoom, saved_view=saved)
        self._initial = initial
        self._pick_zoom = zoom

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The key hints for the picker, and the live status of the tile downloads."""
        base = "↑↓←→ pan · ⇧ fine · PgUp/PgDn zoom · Enter set location · Esc cancel"
        if self._pending:
            return f"{base} · [muted]loading {len(self._pending)} tiles…[/muted]"
        if not self._source.available:
            return f"{base} · [warn]offline — no basemap[/warn]"
        return base

    def _initial_viewport(self, dot_w: int, dot_h: int) -> Viewport:
        """Open on the initial location, else fit the nodes, else show the world."""
        if self._saved_view is None and not self._markers:
            return Viewport(20.0, 0.0, 2, dot_w, dot_h)  # nothing to fit: the world
        return super()._initial_viewport(dot_w, dot_h)

    def render_body(self, width: int) -> list[str]:
        """Render the map with the crosshair marker pinned to the centre of the viewport.

        The crosshair is a temporary marker, added for this frame only (never stored in
        :attr:`_markers`). It is drawn in the "self" style, so it reads as your future
        position, and its label shows the live coordinates that Enter commits. The
        viewport is settled first (:meth:`~MapScreen._ensure_viewport`). Thus the
        crosshair is at the centre from the first frame, and at the resized centre when
        the window changes.

        The marker exists only during this call. Thus each raster that must show it must
        be requested from inside this call. For this reason, the background draw copies
        the scene, and does not read it again later (refer to
        :meth:`~MapScreen._schedule_ground`). Without the copy, the finished basemap covers
        the crosshair, and the picker loses the position that it selects.
        """
        real = self._markers
        vp = self._ensure_viewport(width)
        cross = MapMarker(
            label=f"⌖ {vp.center_lat:.5f}, {vp.center_lon:.5f}",
            lat=vp.center_lat,
            lon=vp.center_lon,
            is_self=True,
        )
        self._markers = real + [cross]
        try:
            return super().render_body(width)
        finally:
            self._markers = real

    def _title(self, vp: Viewport) -> str:
        """A live status title: the coordinates under the crosshair, and the scale."""
        base = super()._title(vp)
        scale = base.rsplit("·", 1)[-1].strip()
        return f"Set location · {vp.center_lat:.5f}, {vp.center_lon:.5f} · z{vp.zoom} · {scale}"

    def handle(self, action: str, data: str = "") -> None:
        """Commit the centre on Enter. ``Home`` goes to the initial spot. Others are map keys."""
        if action == "enter":
            vp = self._viewport
            if vp is not None:
                self.resolve((vp.center_lat, vp.center_lon))
            return
        if action in ("home", "ctrl_home") and self._viewport is not None:
            # The reset returns to the first viewport: the initial location when there is
            # one, else the fit to the nodes. It does not fit the viewport again to a set
            # of nodes that now includes no specific place. With neither, use the
            # viewport of the world.
            vp = self._viewport
            if self._initial is not None:
                lat, lon = self._initial
                self._viewport = Viewport(clamp_lat(lat), lon, self._pick_zoom, vp.dot_w, vp.dot_h)
            elif not self._markers:
                self._viewport = Viewport(20.0, 0.0, 2, vp.dot_w, vp.dot_h)
            else:
                super().handle(action, data)
                return
            self._needs_scrub = True
            return
        super().handle(action, data)


def coords_or_none(lat: object, lon: object) -> tuple[float, float] | None:
    """A stored coordinate pair as the first spot of the picker, or ``None`` to fit the mesh.

    The function accepts the values that a caller has: floats from the companion, strings
    from the CLI of a repeater, or ``None`` where nothing was read. It reads the MeshCore
    ``0, 0`` "no fix" as no location. Thus a node that never had a position opens on the
    mesh, and not on a point in the Gulf of Guinea.
    """
    try:
        lat_f, lon_f = float(lat), float(lon)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if abs(lat_f) < 1e-6 and abs(lon_f) < 1e-6:
        return None
    return lat_f, lon_f


async def pick_location(
    ctx: AppContext, *, initial: tuple[float, float] | None = None
) -> tuple[float, float] | None:
    """Open the full-screen map as a coordinate picker. Return ``(lat, lon)`` or ``None``.

    The picker opens on the located mesh nodes that MeshTerm already has: the cached
    contacts and position of the session, and our own history. It never waits for the
    device to send them, because they are context for the selection, not part of it.
    Before, when a companion refused the contacts read (some do, for a period), the picker
    stayed closed through each retry of the read, for more than twenty seconds. MeshTerm
    gets the data that the cache did not have behind the open map, and adds it to the map
    when the device answers. The read is never cancelled. Thus, when another screen waits
    for the same shared read, that read continues after the picker closes. Then the
    function runs a :class:`LocationPickScreen` until the user commits a spot with Enter,
    or leaves with Esc.

    Args:
        ctx: The shared application context (must be in the interactive menu).
        initial: The location at the centre when the picker opens (for example, the
            current coordinates of the node), or ``None`` to fit the mesh.

    Raises:
        RuntimeError: If it is called outside the interactive menu (no full-screen
            session).
    """
    import asyncio

    from ..services.markers import gather_markers
    from .surface import TuiUi
    from .tui.screen import CANCEL

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the interactive map is only available in the menu")
    session = ctx.ui.session
    devstate = ctx.devstate
    in_hand = devstate.peek_contacts() is not None and devstate.peek_self_info() is not None
    try:
        markers = await gather_markers(ctx, wait=False)
    except Exception:  # noqa: BLE001 - context markers are useful, but never necessary
        markers = []
    source = basemap_source(ctx)
    max_zoom = await asyncio.to_thread(lambda: source.max_zoom)
    screen = LocationPickScreen(session, markers, source, max_zoom, initial=initial)
    showing = True

    async def fill_in() -> None:
        """Add what the cache did not have when the device answers (or never, at no cost)."""
        try:
            fuller = await gather_markers(ctx)
        except Exception:  # noqa: BLE001 - the picker already works without them
            return
        if showing and len(fuller) != len(screen._markers):
            screen.set_markers(fuller)

    if not in_hand:
        asyncio.ensure_future(fill_in())
    try:
        result = await session.run_screen(screen)
    finally:
        showing = False
        # The same full paint as in open_map: the braille may have smeared the terminal.
        session.request_full_repaint()
    return None if result is CANCEL or result is None else result


def basemap_source(ctx: AppContext) -> BasemapSource:
    """The shared vector-tile source of the session (refer to :attr:`AppContext.basemap_source`).

    The context caches it. Thus MeshTerm resolves the TileJSON one time for the full
    session, and not each time a map opens or the Node detail shows a location preview.
    """
    return ctx.basemap_source


async def open_map(
    ctx: AppContext,
    markers: list[MapMarker],
    *,
    focus: tuple[float, float] | None = None,
    find: str | None = None,
    fraction: float = DEFAULT_VIEW_FRACTION,
) -> None:
    """Open the interactive full-screen map over ``markers``, and run until the user leaves.

    The function warms the tile source off the event loop (thus the first paint does not
    wait for the network). Then it pushes the :class:`MapScreen` and waits until the user
    leaves it.

    Args:
        ctx: The shared application context (must be in the interactive menu).
        markers: The located mesh nodes to draw (not empty).
        focus: A ``(lat, lon)`` at the centre when the map opens (for example, the node
            from which the user opened the map). It replaces the stored viewport ("where
            you left the map"). A focused map is a temporary look: on purpose, it
            connects no ``on_view_change``. Thus a pan around it never writes over that
            saved viewport, and the Map tool still opens where the user last left it.
        find: A find query for the map to open with: the name of the focused node, so
            that the node is bright among the others, the same as if the user typed the
            name. ``None`` opens with the find off.
        fraction: The fraction of the nodes that the default viewport fits (refer to
            :class:`MapScreen`).

    Raises:
        RuntimeError: If it is called outside the interactive menu (no full-screen
            session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the interactive map is only available in the menu")
    session = ctx.ui.session
    source = basemap_source(ctx)
    # Resolve the tile template and zoom in a worker thread, so the UI thread never blocks.
    max_zoom = await asyncio.to_thread(lambda: source.max_zoom)
    if focus is not None:
        # Open on the focused node, with the same centre and zoom as the detail preview
        # (``min(13, max_zoom)``). Thus the full map is clearly the same place, larger.
        # No ``on_view_change``: a focused look must not change the stored global
        # viewport.
        lat, lon = focus
        screen = MapScreen(
            session,
            markers,
            source,
            max_zoom,
            saved_view=(clamp_lat(lat), lon, min(13, max_zoom)),
            view_fraction=fraction,
            find=find or "",
        )
    else:
        screen = MapScreen(
            session,
            markers,
            source,
            max_zoom,
            saved_view=ctx.repo.get_map_view(),
            on_view_change=lambda vp: ctx.repo.set_map_view(vp.center_lat, vp.center_lon, vp.zoom),
            view_fraction=fraction,
        )
    try:
        await session.run_screen(screen)
    finally:
        # The braille of the map may have smeared the terminal through double-width
        # fallback glyphs that the diff of prompt_toolkit cannot see. Force one full
        # paint, so that the menu drawn under the map starts from a clean terminal and
        # does not keep that garbage.
        session.request_full_repaint()
