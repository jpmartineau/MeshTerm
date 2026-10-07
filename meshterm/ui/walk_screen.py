# SPDX-License-Identifier: Apache-2.0
"""The Mesh walk: go through the observed shape of the mesh, one node at a time.

This screen is the interactive form of the ``walk`` tool. The trace path composer
collects each part of the topology that we received into one evidence graph
(:mod:`~meshterm.services.topology`): trace walks, firmware routes, relay chains from the
RX log, and the neighbour tables of repeaters. This screen lets the user explore that
graph. Nothing here transmits: the walk shows only what the radio has already heard.

The walk does not plot the full mesh at one time, because a large graph then looks like
a tangle. Instead, the walk keeps one node *in focus* (at the start, our node) and shows
only its direct neighbours:

* The **canvas** takes most of the screen, so that the shape stays easy to read. It puts
  the focus at the far **west**, with its name to its right. Then it spreads a fan of the
  strongest neighbours to the east across the width. The fan is sparse on purpose. The
  name of each neighbour is immediately to the right of its marker. Each marker is the
  **node-type mark** of the app, with its glyph and its colour: the same mark that the
  map and the route graph use for that node. The *identity* of the node is in the name
  next to the marker, in the hue of the node. Edges are braille lines. The median SNR of
  the link sets the colour of an edge (green → amber → red, and slate for a link with no
  reading), and the age of the evidence makes it fainter. Each edge starts at the *right
  end of the focus name*, so that a line never crosses a label. The fan is on a grid of
  rows: one marker on each row, a blank row between two markers, and a blank row above
  and below when the canvas has the space. The canvas draws only as many neighbours as
  these rows can hold. The remaining weaker neighbours collapse into one ``…`` marker.
  They are ranked by observed strength: the SNR, the sample count, and the recency
  together. Thus a strong link that is very old does not go above a newer one. When the
  list highlights one of the collapsed neighbours, the ``…`` marker stands in for it: it
  shows the mark of that node, its name, and a grey ``(2/17)`` for its position among
  them. The canvas draws only the ways *onward*. The node that the walk came from is
  behind you, and ⌫ takes you back to it.
* The **link list** below the canvas shows each way onward as a row that you can
  select. The strongest observed link is first. Thus row 0, where the highlight starts,
  is always the strongest link from this node. Each row has the type glyph, the name,
  the hash, the SNR with a quality bar, the evidence for the link (samples, sources, and
  age), and the number of links that continue onward from that node. On the canvas, the
  label of the highlighted row shows in white. The list scrolls *inside* the screen: the
  canvas, the legend, and the heading do not move, and faint ``↑/↓ n more`` markers are
  at the top and bottom of the list window. ↑↓ move one row, and PgUp/PgDn move one list
  window.

**Enter walks**: the highlighted neighbour becomes the new focus, and the breadcrumb trail
at the top becomes longer. The trail is a :mod:`~meshterm.ui.pathline` path line: powerline
chips where the terminal can draw them, or ``you › Hilltop-Repeater › …`` where it cannot.
Each name is in the hue of its node. **⌫ steps back** along the trail. If you walk to a
node that is already on the trail, the stack is cut back to the first position of that
node. Thus the loop that you walked to come back there is removed, not stored. The trail
never wraps. It is one line that the user reads through a window. It is drawn complete
while it fits. If it does not fit, it is *cropped* at each edge where the walk
continues: the chip breaks on its own half block, the same as each path row in the app
that is too long for its lane. At rest, the window is at the tail. Thus the focus and the
last steps are visible, and the crop removes the start of the walk. **←→ slide the
window**, so that you can read back over a long walk. **^Y** puts the focus on our node
again. **Typing finds**: a filter over all the nodes in the graph, islands also. While you
type, it also narrows the fan on the canvas to the neighbours that match, and the focus
stays. Enter teleports the focus to the highlighted match. The trail then starts again
there, because the walk did not cross the gap. The first Esc clears the find, and the
second Esc leaves the screen.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime
from itertools import pairwise
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.text import Text

from ..core.models import NODE_TYPE_REPEATER, Contact, utcnow
from ..platforms import get_platform
from ..services.topology import Link, MeshTopology
from .mapcanvas import RGB, MapCanvas, parse_hex
from .marks import SELF_MARK, UNKNOWN_MARK
from .menus import fit_cells
from .pathline import ELIDE_HEAD, ELIDE_TAIL, SELF_GLYPH, PathHop, PathLine, cut_mark
from .theme import mark_rgb, name_style, snr_style
from .trace_screen import snr_bar
from .tui.render import crop_cells, query_line, render_to_ansi
from .tui.screen import ListWindow, Screen
from .widgets import DEFAULT_GLYPH, NODE_GLYPHS, format_age, highlighted_hash, short_frame

if TYPE_CHECKING:
    from ..context import AppContext

#: The part of the width east of the focus name that the fan keeps for its reach, however
#: long the names are that it must make space for. Below this part, the graph does not
#: look like a fan (the markers pile onto the focus). Thus, past this limit, the labels
#: are clipped instead (refer to :meth:`WalkScreen._place_neighbours`).
_MIN_FAN_REACH = 1 / 3

#: The minimum height of the canvas in character rows. Below this height, the user cannot
#: see the shape of the fan.
_CANVAS_MIN_H = 6

#: The rows that the link list always keeps for itself under the canvas, however tall the
#: graph wants to be. A list in a list window must have at least a few rows to scroll in.
_LIST_MIN_ROWS = 3

#: The maximum width of a label on the canvas before it ellipsizes. This limit is large,
#: and most real node names fit it completely. It is only an upper limit: each label is
#: *also* clamped to the free cells between its marker and the edge of the canvas (refer to
#: :meth:`_label_right`, :meth:`_focus_anchor`, :meth:`_place_label`). Thus a name loses
#: letters only when it really does not fit, not because of a fixed short limit. The fan
#: placement gives most markers much more space than the old fixed limit did.
_LABEL_W = 22

#: The label limit of the fan. It is higher than the limit of the focus, and it is the
#: longest name for which a marker can move west (JP, 2026-08-09). The user reads the names
#: in the fan, so the fan gets the space. The focus keeps the smaller :data:`_LABEL_W`,
#: because each cell that it uses pushes the full fan east. Also, the line below the
#: canvas shows the full name of the focus. 32 is the limit of the protocol for a node
#: name, so a name that fits the mesh fits here.
_FAN_LABEL_W = 32

#: The name length for which the *default* reach of the fan is sized. A marker with a
#: longer name moves west from there, one cell for each extra cell of the name. A marker
#: with a shorter name does not move. Thus one long name never pulls the full fan in.
_FAN_BASE_LABEL_W = 12

#: The angular reach of the fan on each side of due east, in radians. This angle sets the
#: *vertical* spread of the fan (the marker rows that the leaves spread across).
#: :data:`_FAN_X_FLATTEN` makes the *horizontal* reach flatter than this angle. Thus the
#: height of a leaf and its distance to the east are independent. The full fan stays east
#: of the focus (neighbours to the right, labels to the right). A smaller neighbourhood
#: uses a proportionally smaller part of the arc. Thus two nodes are never at the two ends
#: of the arc with nothing between them.
_FAN_HALF_ANGLE = math.radians(72)

#: How much flatter the horizontal reach of the fan is than its vertical spread. The full
#: fan angle of a leaf sets its *height*. Its *east reach* uses only this fraction of that
#: angle. Thus the leaves at the rim (top and bottom) curve back from due east only a
#: little, and do not curve in toward the focus on a true circle. The fan then spreads
#: across the width, and it does not bulge in the middle with empty corners. ``1.0`` gives
#: the old circular arc again, and a lower value makes it flatter. The setback still
#: changes with the angular reach of the fan, so a small fan near due east moves back
#: almost not at all.
_FAN_X_FLATTEN = 0.55

#: The fan size for which the canvas gives up its outer blank rows. Above this size, the
#: canvas keeps a blank row at the top and at the bottom. Below it, these rows go to nodes
#: instead. Refer to :meth:`WalkScreen._fan_capacity`.
_FAN_MIN_SLOTS = 7

#: The sentinel key for the collapsed marker of the weaker links, in the map of placed
#: nodes. NUL can never be the same as a canonical id (canonical ids are hex).
_MORE = "\x00more"

#: The separator between the hops of the breadcrumb trail (``›``, not the ``→`` of the
#: rest of the app). Each end that has more walk outside the viewport shows the cut mark
#: of the app: a chip that breaks on its own fill, or the faint ``…`` where the line is
#: drawn in arrows (refer to :meth:`WalkScreen._trail_text`).
_TRAIL_SEP = " › "

#: The cells by which one ←/→ press slides the breadcrumb. This is the same step as each
#: other path row in a window in the app (the routes on the node page, the Message paths
#: lanes, the ``hscroll`` of the select list), because this is the same type of window:
#: one line, cropped, that the user reads one lane at a time. The old fit moved one hop for
#: each press, when the overflow put whole hops behind a ``⋯``. A cropped line has no hop
#: boundaries to step by. When the step is the unit of the crop, the slide looks like a
#: slide.
_HSCROLL_STEP = 8

#: The maximum number of find matches that the list shows (the filter narrows it fast).
_MAX_MATCHES = 10

#: The minimum width of the name lane in the list of neighbours or matches. The lane width
#: comes from its content, and it can grow to the width that the fixed lanes leave (refer
#: to :meth:`WalkScreen._lane_widths`). Thus a long name shows as much as the row has space
#: for.
_LIST_NAME_MIN = 10

#: The *maximum* width of the key lane: the full canonical id of 12 hex digits, with the
#: addressed prefix lit in the hue of the node (refer to :func:`highlighted_hash`). The
#: lane becomes narrower from here on a byte boundary, to give a long name more space. But
#: it never becomes narrower than its lit hash (refer to :meth:`WalkScreen._lane_widths`):
#: the name loses letters before the hash does.
_LIST_HASH_W = 12

#: The cells that a link row uses *outside* its two flexible lanes (name and key), so that
#: these two lanes can use the remaining cells: pointer (2) + type glyph and its space (2)
#: + the space between name and key (1) + gap (2) + SNR (5) + space (1) + quality bar (4)
#: + samples (5) + source tags (5) + age (5) + a reserve for the ⋯ count at the end (8).
_LINK_ROW_FIXED = 2 + 2 + 1 + 2 + 5 + 1 + 4 + 5 + 5 + 5 + 8

#: The same, for a find-match row. This row ends with a short note of the distance, not
#: with the SNR evidence: pointer (2) + glyph and space (2) + the space between name and
#: key (1) + gap (2) + a reserve for the distance (12, for ``this device`` / ``N hops out``).
_MATCH_ROW_FIXED = 2 + 2 + 1 + 2 + 12

#: The anchors from SNR (dB) to edge colour. The colour between two anchors is a linear
#: interpolation, and it is clamped at the ends. These are the red, amber, and green of the
#: SNR styles of the app, so that the graph and the rows agree.
_SNR_STOPS: tuple[tuple[float, RGB], ...] = (
    (-15.0, (239, 68, 68)),
    (0.0, (250, 204, 21)),
    (10.0, (74, 222, 128)),
)

#: The edge colour for a link with no SNR reading (for example, a link known only from a
#: route).
_NO_READING: RGB = (100, 116, 139)

#: One-letter tags for the types of evidence for a link, the same as in the path composer:
#: T(race), R(oute), P(acket log), N(eighbour table).
_SOURCE_TAGS = {"trace": "T", "route": "R", "packet": "P", "neighbour": "N"}


def _snr_rgb(snr: float | None) -> RGB:
    """The edge colour for the median SNR of a link (refer to :data:`_SNR_STOPS`)."""
    if snr is None:
        return _NO_READING
    if snr <= _SNR_STOPS[0][0]:
        return _SNR_STOPS[0][1]
    if snr >= _SNR_STOPS[-1][0]:
        return _SNR_STOPS[-1][1]
    (x0, c0), (x1, c1) = next((lo, hi) for lo, hi in pairwise(_SNR_STOPS) if lo[0] <= snr <= hi[0])
    f = (snr - x0) / (x1 - x0)
    return tuple(round(a + (b - a) * f) for a, b in zip(c0, c1, strict=True))  # type: ignore[return-value]


def _scaled(rgb: RGB, factor: float) -> RGB:
    """``rgb`` made dimmer (or a little brighter) by ``factor``, clamped to the byte range."""
    return tuple(max(0, min(255, round(c * factor))) for c in rgb)  # type: ignore[return-value]


def _freshness(last_seen: datetime | None, now: datetime) -> float:
    """How bright a link is for the age of its evidence: 1.0 new → 0.5 stale."""
    if last_seen is None or getattr(last_seen, "tzinfo", None) is None:
        return 0.55
    age = (now - last_seen).total_seconds()
    if age < 86400:
        return 1.0
    if age < 7 * 86400:
        return 0.8
    return 0.5


class WalkScreen(Screen):
    """The full-screen mesh walk: a canvas of the focus neighbourhood over a link list."""

    floating = False

    @property
    def picocalc_lyra_lane(self):
        """``You`` on the F3 of this screen, and the shared pager with its two ends on F4/F5.

        ``You`` is not an end of the link list. It removes the full trail and puts the
        focus on our node again. Thus it was never correct behind the pager, where it had
        to stand in for *Top* and leave *Bottom* blank to keep up the pretence. Instead, it
        takes a slot of this screen (JP, 2026-08-09), because F1-F3 are for this purpose.
        The Shift bank of the pager then has the same meaning here as in all other places:
        the two ends of the list that this screen scrolls. On a keyboard, ^Y does the same
        action. The map uses the same chord for our node.

        The two navigation slots must have rows to move through, and a leaf node in a
        sparse graph may not have them. ``You`` must have a place to come back *from* (a
        walked trail, or a find that narrows the list). It dims when the focus is already
        our node.
        """
        from .tui.fkeys import FPair, default_lane

        lane = list(default_lane(nav=len(self._rows()) > 1))
        lane[2] = FPair("You", "locate", enabled=len(self._trail) > 1 or bool(self._filter))
        return lane

    def __init__(
        self,
        *,
        session,  # noqa: ANN001 - a TuiSession, imported late to prevent an import cycle
        topo: MeshTopology,
        contacts: dict[str, Contact],
        self_label: str,
        prefix_bytes: int = 0,
    ) -> None:
        """Create the walk over a snapshot of a built topology.

        Args:
            session: The running TUI session (for paints).
            topo: The evidence graph to walk.
            contacts: The contacts by canonical id, for glyphs, names, and ages.
            self_label: The display name for our node (its mesh name when it is known).
            prefix_bytes: The width of the path hash to light in the hash lane (0 = none).
        """
        super().__init__()
        # The name of the screen, and nothing more (JP, 2026-08-30). The title once had the
        # focus and the size of the graph:
        # ``Mesh walk — Hilltop-Repeater · 42 nodes · 61 links``. That title was longer than
        # a title bar of 53 columns, and each atom of it was already on the screen below.
        # The trail ends on the focus. The line under the trail shows the name of that node
        # in colour, with its hash and its ring. The canvas and the link list exist to show
        # the size of the neighbourhood. The title is set one time here, not composed at
        # each render. Thus the top row never changes while the walk moves.
        self.title = "Mesh walk"
        self._session = session
        self._topo = topo
        self._contacts = contacts
        self._self_label = self_label
        self._prefix_bytes = prefix_bytes
        #: The walked trail of canonical ids. The focus is its last entry. A walk appends,
        #: ⌫ pops, ^Y resets the trail to our node, and a find teleport starts it again.
        self._trail: list[str] = [topo.self_id]
        #: The cells by which the breadcrumb line is scrolled off its *tail* end (refer to
        #: :meth:`_trail_text`). 0 is the rest state (the focus at the right edge). Each
        #: change to the trail sets it back to 0.
        self._trail_scroll = 0
        #: ``((width, trail) → the furthest that can scroll)``, set at render (refer to
        #: :meth:`_trail_max_scroll`). Thus a key press can clamp itself without a width.
        self._trail_fit: tuple[tuple, int] | None = None
        #: The width of the last render of the trail, for that same clamp.
        self._trail_width = 0
        #: The index of the highlighted row in the current list (neighbours or matches).
        self._index = 0
        #: The live find-as-you-type filter ("" = off). It searches all the known nodes.
        self._filter = ""
        self._needs_scrub = True  # the scrub of the braille smear, the same as on the map
        #: The list window of the link list (the list scrolls, the screen does not). Its
        #: settled capacity is the step by which a PgUp/PgDn moves the highlight.
        self._list = ListWindow()
        # Caches of values from the graph. MeshTerm takes a snapshot of the topology one
        # time when the screen opens (to see new evidence, open the screen again). Thus
        # each value that comes only from the snapshot (neighbour lists, the node set, BFS
        # depths, the counts of onward links) is calculated one time for each key, not at
        # each paint. Nothing ever makes these caches invalid: they exist exactly as long
        # as the frozen graph that they describe.
        self._links_of_memo: dict[str, list[tuple[str, Link]]] = {}
        self._all_nodes_memo: set[str] | None = None
        self._hops_out_memo: dict[str, int] | None = None
        self._onward_memo: dict[str, dict[str, int]] = {}

    # --- state -------------------------------------------------------------------

    @property
    def _focus(self) -> str:
        """The node that is now in focus (the last step of the trail)."""
        return self._trail[-1]

    @property
    def _came_from(self) -> str | None:
        """The node that the trail came from, or ``None`` at the start of the trail."""
        return self._trail[-2] if len(self._trail) > 1 else None

    def _links_of(self, node: str) -> list[tuple[str, Link]]:
        """``(other, link)`` for each link from ``node``, the strongest evidence first.

        The result is cached for each node over the frozen graph. The sort order is set
        at the first request: the strength decreases over hours, much slower than the
        time that a screen stays open.
        """
        memoized = self._links_of_memo.get(node)
        if memoized is not None:
            return memoized
        now = utcnow()
        pairs = [
            (link.b if link.a == node else link.a, link)
            for link in self._topo.links()
            if node in (link.a, link.b)
        ]
        pairs.sort(key=lambda pair: -pair[1].strength(now))
        self._links_of_memo[node] = pairs
        return pairs

    def _all_nodes(self) -> set[str]:
        """All the nodes in the graph, and our node (a walk can start there, also alone)."""
        if self._all_nodes_memo is None:
            nodes = {self._topo.self_id}
            for link in self._topo.links():
                nodes.add(link.a)
                nodes.add(link.b)
            self._all_nodes_memo = nodes
        return self._all_nodes_memo

    def _hops_out(self) -> dict[str, int]:
        """The BFS hop distance from our node over the evidence links.

        A node with no path to our node is not in the result. Such nodes are the islands.
        Where a distance usually shows, the screen marks them as islands.
        """
        if self._hops_out_memo is not None:
            return self._hops_out_memo
        adjacency: dict[str, set[str]] = {}
        for link in self._topo.links():
            adjacency.setdefault(link.a, set()).add(link.b)
            adjacency.setdefault(link.b, set()).add(link.a)
        depths = {self._topo.self_id: 0}
        queue: deque[str] = deque([self._topo.self_id])
        while queue:
            node = queue.popleft()
            for neighbour in adjacency.get(node, ()):
                if neighbour not in depths:
                    depths[neighbour] = depths[node] + 1
                    queue.append(neighbour)
        self._hops_out_memo = depths
        return depths

    def _is_match(self, node: str) -> bool:
        """Whether the live find query matches this node (by display name or by id).

        This is the only predicate for the two things that the query narrows: the list of
        teleport candidates (:meth:`_matches`, over the full graph) and the fan on the
        canvas (:meth:`_canvas_lines`, over the neighbours of the focus). When there is no
        filter, all nodes match. Thus a caller does not have to check for a filter first.
        """
        needle = self._filter.strip().casefold()
        if not needle:
            return True
        return needle in self._label(node).casefold() or needle in node.casefold()

    def _matches(self) -> list[str]:
        """The nodes that the find filter matches: the nearest first, then by display name."""
        if not self._filter.strip():
            return []
        depths = self._hops_out()
        candidates = [node for node in self._all_nodes() if self._is_match(node)]
        candidates.sort(key=lambda n: (depths.get(n, 999), self._label(n).casefold()))
        return candidates[:_MAX_MATCHES]

    def _rows(self) -> list[str]:
        """The node ids that the list now shows and that the user can select.

        These are the matches, or the ways onward. The neighbour rows are
        :meth:`_fan_nodes`: the same set that the canvas draws, in the same order (the
        strongest first). Thus row 0, where the highlight goes at each reset, is the
        strongest link from this node.
        """
        if self._filter:
            return self._matches()
        return self._fan_nodes()

    # --- input -------------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The keys of the walk, or the live find query while the user types one.

        All the atoms fit the limit of 72 cells only because two of them alternate.
        ``←→ trail`` shows exactly while the breadcrumb has more walk than width. (A
        keyboard key that does nothing is never shown: this is an old rule of the hint
        line, and of the F-key lane after it.) It takes the place of ``type to find``. When a
        walk is long enough to overflow the trail, the scroll is the key that the user
        does not know. But typing shows itself at the first letter, because the query
        then replaces this full line. The find teaches itself, and the scroll could not.
        """
        if self._filter:
            return f"find: {self._filter}▏ · ↑↓ move · Enter focus · ⌫ erase · Esc clear"
        scrolls = self._trail_width > 0 and bool(self._trail_max_scroll(self._trail_width))
        atoms = ["↑↓ move"]
        if scrolls:
            atoms.append("←→ trail")
        atoms += ["Enter focus", "⌫ back", "^Y you"]
        if not scrolls:
            atoms.append("type to find")
        atoms.append("Esc back")
        return " · ".join(atoms)

    def handle(self, action: str, data: str = "") -> None:
        """Move the highlight, walk, step back, find, or close.

        Home/End go to the ends of the list here, the same as on each other screen that
        scrolls. The jump back to our node is ``locate`` (^Y, and F3 on the lane), which is
        not a position in this list. Refer to :attr:`picocalc_lyra_lane`.
        """
        rows = self._rows()
        if action == "escape":
            if self._filter:
                self._filter = ""
                self._index = 0
            else:
                self.resolve(None)
                return
        elif action == "up" and rows:
            # Both ends clamp and do not wrap. The list scrolls inside a list window (refer
            # to ListWindow). If the highlight jumps from one end to the other, it takes the
            # list window with it. That is the one move that looks like a change of the
            # screen under you.
            self._index = max(0, self._index - 1)
        elif action == "down" and rows:
            self._index = min(len(rows) - 1, self._index + 1)
        elif action == "pageup" and rows:
            # The highlight moves by the height of one list window. The list window follows
            # the highlight. Thus, if only the list window moves, it goes back immediately.
            self._index = max(0, self._index - self._list.page)
        elif action == "pagedown" and rows:
            self._index = min(len(rows) - 1, self._index + self._list.page)
        elif action == "enter":
            self._walk(rows)
        elif action == "backspace":
            if self._filter:
                self._filter = self._filter[:-1]
                self._index = 0
            elif len(self._trail) > 1:
                self._trail.pop()
                self._index = 0
                self._trail_scroll = 0
        elif action in ("home", "ctrl_home") and rows:
            self._index = 0
        elif action in ("end", "ctrl_end") and rows:
            self._index = len(rows) - 1
        elif action == "left":
            # The breadcrumb scrolls, and the list does not: the breadcrumb is the only line
            # here with more content than width. The scroll is clamped against the width of
            # the last paint. Thus, when the user holds ←, the line stops at the head, and
            # the extra presses are not kept for later. (The limit is 0 until the trail
            # overflows, so the keyboard key does nothing on a short walk.)
            self._trail_scroll = min(
                self._trail_scroll + _HSCROLL_STEP,
                self._trail_max_scroll(self._trail_width),
            )
        elif action == "right":
            self._trail_scroll = max(0, self._trail_scroll - _HSCROLL_STEP)
        elif action == "locate":
            # ^Y (and F3): stop the walk, instead of a move inside it. The trail goes back
            # to only our node, and a find that narrows the list is also removed.
            self._trail = [self._topo.self_id]
            self._filter = ""
            self._index = 0
            self._trail_scroll = 0
        elif action == "text":
            if not data.isspace() or self._filter:  # never start the filter with a space
                self._filter += data
                self._index = 0
        elif action == "space" and self._filter:
            self._filter += " "  # node names have spaces. A space is useful only mid-query
        self._needs_scrub = True
        self._session.invalidate()

    def _walk(self, rows: list[str]) -> None:
        """Put the focus on the highlighted row: a step along the trail, or a find teleport."""
        if not rows:
            return
        target = rows[min(self._index, len(rows) - 1)]
        if self._filter:
            # A teleport starts the trail again at the target. The walk did not cross the
            # gap. If the trail shows that it did, ⌫ goes back along a path that the user
            # never took.
            self._trail = [target]
            self._filter = ""
        elif target in self._trail:
            # A walk to a node that is already on the trail (a step back through the west
            # node, or a loop round to an earlier node) cuts the stack back to the first
            # position of that node. We remove the loop that we walked to come back here,
            # and we do not store the round trip. The removal of the loop is intentional.
            self._trail = self._trail[: self._trail.index(target) + 1]
        else:
            self._trail.append(target)
        self._index = 0
        self._trail_scroll = 0  # the new focus is the important part. Make it visible again

    # --- smear scrub (the same problem with fallback glyphs as on the map) ---------

    def consume_edge_scrub(self) -> int:
        """The columns at the right edge to paint again after a paint (refer to the map)."""
        if not self._needs_scrub:
            return 0
        self._needs_scrub = False
        return 2

    # --- rendering -----------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render the trail, the focus line, the canvas, and the link list in its list window.

        The layout of the body fits the viewport of the frame exactly. The canvas takes
        most of the rows (a slightly smaller part on tall terminals). The chrome around it
        does not move. The remaining rows are the list window of the link list. Only its
        rows scroll, inside :meth:`_list_lines`.
        """
        links = self._topo.links()
        rows = self._rows()
        self._index = max(0, min(self._index, len(rows) - 1)) if rows else 0
        if not links:
            return self._empty_state(width)

        depths = self._hops_out()
        selected = rows[self._index] if rows else None
        viewport = self._scroll_viewport  # the frame stores it before this render

        header = self._header_lines(width, depths)
        # On a short frame, the legend and the blank line under it are removed, because
        # there the rows go to the list (refer to widgets.short_frame). The heading of the
        # list stays in both cases.
        keyed = not short_frame(viewport)
        chrome = len(header) + (3 if keyed else 1) + self._query_row()  # (+ the find echo)
        canvas_h = self._canvas_height(viewport, chrome)
        list_win = max(1, viewport - chrome - canvas_h)

        lines: list[str] = list(header)
        lines.extend(self._canvas_lines(width, canvas_h, selected))
        if keyed:
            lines.append(render_to_ansi(self._legend(), width, no_wrap=True))
            lines.append("")
        lines.extend(self._list_lines(width, rows, depths, list_win))
        self._scroll_total = max(1, len(lines))
        return lines

    def _query_row(self) -> int:
        """Whether this paint uses a list row to echo the find query (1) or not (0).

        This occurs only where the footer is not drawn
        (:attr:`~meshterm.platforms.Platform.footer_fkeys`). There, the hint line that has
        the query never gets to the screen. Without this row, the list narrows to the
        matches and shows no sign of the text that narrowed it. The row is immediately
        above the ``Matches`` heading: above what it narrows, as on each other
        find-as-you-type screen. The list gives the line for it, not the canvas.
        """
        return 1 if self._filter and get_platform().footer_fkeys else 0

    def _canvas_height(self, viewport: int, chrome: int) -> int:
        """The rows for the canvas: most of the screen, but fewer where more do not help.

        The part starts at approximately 62% of the viewport, and it decreases toward half
        on tall terminals. When the fan is easy to read, a very large graph area adds
        little, but more rows still help the list. A sparse neighbourhood sets a lower
        limit: a link between two nodes does not need half a screen of empty space. The
        link list always keeps its :data:`_LIST_MIN_ROWS` under the fixed chrome.
        """
        crowd = len(self._fan_nodes())
        share = 0.62 - 0.12 * min(max(viewport - 20, 0) / 24.0, 1.0)
        height = round(viewport * share)
        height = min(height, max(_CANVAS_MIN_H, 2 * crowd + 1))
        height = min(height, viewport - chrome - _LIST_MIN_ROWS)
        return max(4, height)

    def _header_lines(self, width: int, depths: dict[str, int]) -> list[str]:
        """The breadcrumb trail and the identity line of the focus node.

        The trail always shows, also at the root, where it is only our node. Thus the
        breadcrumb is always there, and the header keeps the same height before and after
        you walk. Without it, the full body moves up one row at your first step.
        """
        return [
            render_to_ansi(self._trail_text(width), width, no_wrap=True),
            render_to_ansi(self._focus_line(depths), width, no_wrap=True),
        ]

    def _trail_text(self, width: int) -> Text:
        """The breadcrumb trail as a path line: one line, scrolls with ← →, never wraps.

        The walk *is* a path: our node, then each node that the walk stepped through, and
        at the end the focus. Thus it renders through
        :class:`~meshterm.ui.pathline.PathLine`, the same as each other sequence of hops in
        the app: powerline chips where the terminal can draw them, and the ``›`` arrows of
        the trail where it cannot. Each hop shows in the hue of its node, which comes from
        its key. Our node is the white ``you``. A node known only by a bare hash takes the
        grey of an unknown node (``node.unknown``), because colour is only for nodes that
        have a key. Thus the trail and the rows below it agree about which node is which.

        The line never wraps, and nothing in it is ever elided. It is composed one time at
        its natural length, and then the user **reads it through a window**, the same as
        each other path row in the app that is too long for its lane (the routes on the
        node page, the Message paths lanes, a trophy row under ``←→``). While all of it
        fits, it shows complete: at the left edge, and it starts on its rounded head chip,
        as it does in a lane with free cells. When it does not fit, it is cropped, and
        **each edge where the walk continues shows the cut mark**
        (:func:`~meshterm.ui.pathline.cut_mark`): a chip that breaks on the half block in
        its own fill, or the faint ``…`` where the trail is drawn in arrows. The user
        loses only the cells for which the lane had no space, never the full hop that
        these cells were part of (JP, 2026-09-05). There is no ``⋯`` here on either side,
        because nothing is elided.

        Only one thing sets the edge where the line starts: **whether the start of the
        walk is on the line.** A line that shows its first hop is at the left, because a
        path that starts at its start belongs there. A line that is cropped at the head is
        at the right, with the break against the edge where it continues. A crop is
        exactly the width, so the line touches the edge because of its construction, not
        because of padding. At rest, the window is at the tail. Thus the user sees the
        focus and the last steps to it first.

        ← and → then **slide the window** (JP, 2026-08-09), by :data:`_HSCROLL_STEP` cells
        for each press. The cropped head is a real part of the walk, and before this
        change the user could not read a long walk at all. The marks are chrome *inside*
        the lane, not extra width. Each mark takes one cell from the window. Thus the crop
        is measured only when the two marks are known, or the line draws one cell past the
        row. The slide stops when the head becomes visible, so the line does not move off
        the edge. Each change to the trail resets the slide: a walk, a step back, a
        teleport, and ^Y all end with the focus visible, because the user reads the next
        move from there.
        """
        self._trail_width = width
        full = PathLine(
            [self._trail_hop(node) for node in self._trail], separator=_TRAIL_SEP
        ).text()
        total = full.cell_len
        # The scroll is clamped before the fit is read. Thus, when a line fits its width
        # again (a wider terminal, a step back), no slide stays behind a complete line.
        self._trail_scroll = max(0, min(self._trail_scroll, self._trail_max_scroll(width)))
        if total <= width:
            return full  # complete, from the start, at the left edge. No crop, no slide
        end = total - self._trail_scroll  # one cell after the last cell in the window
        inner = width - (1 if self._trail_scroll else 0)
        left = 1 if end > inner else 0
        window = min(end, inner - left)
        start = end - window
        line = Text(style=full.style, no_wrap=True)
        if left:
            line.append_text(cut_mark(full, start, ELIDE_HEAD))
        line.append_text(crop_cells(full, start, window))
        if self._trail_scroll:
            line.append_text(cut_mark(full, end - 1, ELIDE_TAIL))
        return line

    def _trail_max_scroll(self, width: int) -> int:
        """How far ← can slide the trail: the first whole step that shows its head.

        A slide past that point only makes a line shorter that already shows all its
        cells. Thus the slide stops there. The F-key lane says the same thing when it dims
        a key that does nothing. The stop is a whole :data:`_HSCROLL_STEP`, not the exact
        cell that makes the head visible. A slid line gives one cell to its right mark,
        so its last window is one cell narrower than the lane. If the slide stops before a
        whole step, the *left* mark stays drawn, and it shows a head that ← can no longer
        reach. (Each other path row in a window in the app clamps the same
        way.) The result is ``0`` for a trail that fits. Thus ← does nothing on a short
        walk, and the handler does not have to know the width.

        The result is cached on ``(width, trail)``: it costs a render, and the answer
        changes only when the walk or the terminal changes.
        """
        key = (width, tuple(self._trail))
        if self._trail_fit is not None and self._trail_fit[0] == key:
            return self._trail_fit[1]
        hops = [self._trail_hop(node) for node in self._trail]
        total = PathLine(hops, separator=_TRAIL_SEP).text().cell_len
        limit = 0
        if total > width:
            steps = -(-(total - (width - 1)) // _HSCROLL_STEP)
            limit = steps * _HSCROLL_STEP
        self._trail_fit = (key, limit)
        return limit

    def _trail_hop(self, node: str) -> PathHop:
        """One walked step as a path hop: its display name in the colour of its identity.

        Our node takes the :data:`~meshterm.ui.pathline.SELF_GLYPH` of the app instead of
        its name. Each walk starts from our node, and the trail is the line that has the
        least free width. Thus the star says in one cell what a full name says in many.
        The cells that it makes free hold more steps that stay visible before the head
        must be cropped.
        """
        style = self._list_name_style(node)
        if style == "you":
            return PathHop(SELF_GLYPH, you=True)
        return PathHop(
            self._label(node),
            key=None if style == "node.unknown" else node,
        )

    def _focus_line(self, depths: dict[str, int]) -> Text:
        """The node in focus: glyph, name with its hash in parentheses, distance, and recency.

        The first glyph shows the node type with its *shape* and with its *colour*: the
        colour of the type (:meth:`_glyph_style`, the same as the canvas marker). Thus the
        line does not write the type in words. The name shows as ``name (hash)``: the
        addressed path hash in parentheses, with its digits lit in the hue of the node,
        not a bare part of the key.

        The ``N hops out`` distance is the *shortest* observed path from our node to this
        node (a BFS over the full evidence graph, :meth:`_hops_out`): the ring of the node
        in the mesh. On purpose, it is **not** the length of the breadcrumb trail above,
        which is the route that you *walked* to get to the focus. If you walk out far and
        come back, or step to a node that a shorter link also gets to, the walk is longer
        than the ring. The two values answer different questions ("how near is this
        node?" and "how did I get here?"). Thus a "hops out" that is shorter than the
        trail is correct, not a wrong count.
        """
        node = self._focus
        glyph, _type_color = self._glyph(node)
        contact = self._contacts.get(node)
        line = Text()
        line.append(glyph, style=self._glyph_style(node))
        line.append(" ")
        line.append(self._label(node), style=self._list_name_style(node))
        short = self._short_hash(node)
        if short:
            line.append(" (", style="muted")
            line.append_text(highlighted_hash(short, self._prefix_bytes, known=self._known(node)))
            line.append(")", style="muted")
        if node == self._topo.self_id:
            line.append("  ·  this device", style="muted")
        else:
            if node not in depths:
                line.append("  ·  island — no observed path to you", style="warn")
            else:
                ring = depths[node]
                line.append(f"  ·  {ring} hop{'s' if ring != 1 else ''} out", style="muted")
            if contact is not None and contact.last_seen is not None:
                secs = max(0.0, (utcnow() - contact.last_seen).total_seconds())
                line.append(f"  ·  heard {format_age(secs)}", style="muted")
        return line

    def _short_hash(self, node: str) -> str:
        """The addressable path hash of the node as hex, or ``''`` for a placeholder id.

        This is the first ``prefix_bytes`` bytes of a hex id (at least one, so that there
        is always a hash to show). A stand-in that is not hex, such as ``"local"`` (our
        node with no key), has no hash, and the result is the empty string. Thus the focus
        line does not show the parentheses.
        """
        raw = node.lower()
        if not raw or any(c not in "0123456789abcdef" for c in raw):
            return ""
        return raw[: max(1, self._prefix_bytes) * 2]

    # -- the canvas --

    def _canvas_lines(self, width: int, canvas_h: int, selected: str | None) -> list[str]:
        """Draw the focus neighbourhood: the focus at the far west, the strongest fan east.

        The focus icon is at the far west, with its name to its right. The fan spreads east
        across the available width (refer to :meth:`_place_neighbours`), and the name of
        each neighbour is to the right of its marker. Each marker is a complete
        **node-type mark**, with its glyph and its colour (:meth:`_marker_rgb`): the same
        mark that the map and the route graph use for that node. The identity of the node
        is in the *name* next to the marker, in the hue of the key. No marker changes its
        colour, also not the marker of the selection. The highlighted row shows as a white
        *label* and as the lit route.

        While the user types a find query, the fan narrows to the neighbours that match.
        The east is *the ways onward*, and the query asks about them. The focus stays
        through the find: it is the walk until now, not a candidate. Thus the picture
        keeps its orientation while the choices become fewer. The node that the walk came
        *from* is not drawn (JP, 2026-08-09). It is behind you, and ⌫ takes you back to
        it. When it was drawn, the one thing on the canvas that is not a way onward looked
        like one.

        Only as many neighbours as the area can hold get their own marker (refer to
        :meth:`_fan_capacity`). The remaining weaker neighbours collapse into one ``…``
        marker at the foot of the fan. When the list highlights a collapsed row, that
        marker *stands in for* the highlighted node: its type mark, its name, and a grey
        ``(2/17)`` for its position among the collapsed nodes. Thus a selection is never
        invisible, and the user never thinks that it is a separate node. Each edge starts
        at the *right end of the focus name*, so that a line never crosses a label. The
        edge to the selected link is the brightest, is not dimmed, and is drawn over the
        others.
        """
        canvas = MapCanvas(width, canvas_h)
        by_other = dict(self._links_of(self._focus))
        fan = self._fan_nodes()
        capacity = self._fan_capacity(canvas_h)
        if len(fan) > capacity:
            # The … marker takes a slot of its own, so it must have a reason for it. It
            # stands in for the node of the last slot *and* all the nodes that did not fit,
            # never for one node only.
            shown, hidden = fan[: capacity - 1], fan[capacity - 1 :]
        else:
            shown, hidden = fan, []
        slots = len(shown) + (1 if hidden else 0)
        fx, fy = self._focus_pos(width, canvas_h, slots)
        ax, ay, focus_name = self._focus_anchor(width, canvas_h, slots)
        stand_in = selected if selected in hidden else None
        # The layout of the … slot is for the label that it has at rest. The name of a
        # stand-in must not move it. The geometry must not depend on the selection. If it
        # does, the full fan moves around while the highlight goes through the collapsed
        # nodes. A long name that does not fit the slot is truncated (JP, 2026-08-09).
        placed = self._place_neighbours(
            width, canvas_h, shown, bool(hidden), more_label=f"+{len(hidden)} weaker"
        )

        # The edges first (the markers and labels overprint them). The SNR sets their
        # colour, and the age of the evidence makes them fainter. Each edge starts at the
        # anchor at the end of the focus name and ends on the marker of its neighbour.
        # The edge of the selected link is the brightest and is on top. It does not get
        # the age fade, so it shows as one lit line over the dimmer others. (The user can
        # still read the age in the age column of the row.) The edge of the collapsed
        # marker is slate. But when the selection is one of its nodes, it becomes the
        # edge of that node.
        now = utcnow()
        for other, (x, y) in placed.items():
            if other == _MORE:
                if selected in hidden:
                    color = _scaled(_snr_rgb(by_other[selected].median_snr), 1.0)
                    priority = 4
                else:
                    color = _scaled(_NO_READING, 0.6)
                    priority = 1
            else:
                link = by_other[other]
                if other == selected:
                    color = _scaled(_snr_rgb(link.median_snr), 1.0)
                    priority = 4
                else:
                    color = _scaled(_snr_rgb(link.median_snr), _freshness(link.last_seen, now))
                    priority = 2
            canvas.draw_line([(ax, ay), (x, y)], color, priority)

        # The focus marker at the far west, with its name to the RIGHT: icon at the left,
        # name at the right, in the direction in which the user reads the walk. The edges
        # start at the end of the name. Thus they go east away from the label, not through
        # it.
        glyph, _colour = self._glyph(self._focus)
        canvas.marker(fx, fy, glyph, self._marker_rgb(self._focus))
        self._label_right(canvas, fx, fy, focus_name, self._label_rgb(self._focus))

        # The markers first, then the labels. Each marker has the colour of its node type.
        # The selection shows as a white *label* and the lit route, never as a new colour
        # on a mark that has a different meaning. The labels go on in order of importance
        # (the selection, then the strongest links). Thus, where two labels overprint, the
        # collision check removes the less important one.
        white = (255, 255, 255)
        for other, (x, y) in placed.items():
            node = stand_in if other == _MORE else other
            if node is None:
                canvas.marker(x, y, "…", mark_rgb(UNKNOWN_MARK[1]))
                continue
            glyph, _colour = self._glyph(node)
            canvas.marker(x, y, glyph, self._marker_rgb(node))
        if selected is not None and selected in placed:
            x, y = placed[selected]
            self._place_label(canvas, x, y, self._label(selected), white)
        for other in shown:
            if other != selected:
                x, y = placed[other]
                self._label_right(canvas, x, y, self._label(other), self._label_rgb(other))
        if _MORE in placed:
            x, y = placed[_MORE]
            grey = mark_rgb(UNKNOWN_MARK[1])
            if stand_in is not None:
                # The marker is now that node: its own mark, and its name in the white of
                # the selection. A collapsed node is drawn *only while it is selected*, so
                # here it is white, exactly as it would be with a marker of its own. The rank
                # in the collapsed set is *west* of the mark (JP, 2026-08-09), not after the
                # name. Thus the name ends where each other name on the fan ends, and the
                # count reads as a note on the marker, not as a part of the name of the node.
                # The count keeps the stand-in correct: a name alone tells the user that the
                # fan has one more member than it drew.
                self._label_left(canvas, x, y, self._more_rank(stand_in, hidden), grey)
                self._label_right(canvas, x, y, self._label(stand_in), white)
            else:
                self._label_right(canvas, x, y, f"+{len(hidden)} weaker", grey)

        return canvas.to_ansi_lines()

    @staticmethod
    def _more_rank(node: str, hidden: list[str]) -> str:
        """``(2/17)``: the position of ``node`` among the collapsed links, and their count."""
        return f"({hidden.index(node) + 1}/{len(hidden)})"

    def _fan_nodes(self) -> list[str]:
        """The ways *onward* from the focus: each neighbour but the one the walk came from.

        This is the only definition of the content of the east of this screen. The canvas
        and the link list share it, so that the picture and the rows always agree about
        the choices, also under a find, which narrows the two. The node that the walk came
        from is excluded (JP, 2026-08-09). You were there before, and ⌫ takes you back to
        it. When the list showed it as a link, it asked for a walk that only undoes the
        last walk, and it took the top row of the list. Without it, the strongest link is
        always the row where the highlight starts.
        """
        back = self._came_from
        return [
            other
            for other, _link in self._links_of(self._focus)
            if other != back and self._is_match(other)
        ]

    def _fan_capacity(self, canvas_h: int) -> int:
        """How many marker rows the canvas holds, with the row of the ``…`` marker.

        The fan is a **grid of rows, not the spread of an arc** (JP, 2026-08-09): one
        marker on each row, with a blank row between two markers, and a blank row above
        and below the block. That is ``2n + 1`` rows for ``n`` markers. With equal
        spacing, a fan of five reads as five, not as a group at the rim. An equal spacing
        of the *angle* gave that group, because its rows came from a sine, so they were
        close together at the top and the bottom.

        The outer blank rows are the part that can go. If a canvas with them is too short
        for :data:`_FAN_MIN_SLOTS` neighbours, it removes them and fits ``n`` in
        ``2n - 1`` rows instead. Empty space is worth a row until it costs a *node*: the
        screen exists to show the neighbourhood.
        """
        padded = (canvas_h - 1) // 2
        if padded >= _FAN_MIN_SLOTS:
            return max(3, padded)
        return max(3, (canvas_h + 1) // 2)

    def _fan_rows(self, canvas_h: int, slots: int) -> list[int]:
        """The canvas rows for the ``slots`` markers of the fan, from top to bottom.

        The rows are centred as one block. Thus the outer blank row comes back when the
        canvas is taller than the block, and it goes equally from the two ends when the
        canvas is not (refer to :meth:`_fan_capacity`).
        """
        if slots <= 0:
            return []
        span = 2 * slots - 1
        top = max(0, (canvas_h - span) // 2)
        return [min(canvas_h - 1, top + 2 * i) for i in range(slots)]

    def _focus_pos(self, width: int, canvas_h: int, slots: int = 0) -> tuple[int, int]:
        """The dot position of the focus marker: near the west edge, name and fan to its east.

        The icon is near the left edge. Thus its name (drawn to the right) and the fan of
        neighbours have the full width to spread across: the graph has space, and it is
        not pushed into the middle.

        Vertically, it is level with the middle of the **fan**, not of the canvas. The
        block of ``slots`` markers is centred, and the focus is centred on *the block*.
        Thus the edges from the focus spread equally up and down, for an odd or even
        number of markers. When there is no fan (``slots`` 0: a leaf node, or a find that
        matches nothing here), the focus is on the middle line of the canvas.
        """
        x = max(4, (width * 2) // 12)
        rows = self._fan_rows(canvas_h, slots)
        row = (rows[0] + rows[-1]) / 2 if rows else (canvas_h - 1) / 2
        return x, round(row * 4 + 2)

    def _focus_anchor(self, width: int, canvas_h: int, slots: int = 0) -> tuple[int, int, str]:
        """The attach point of the edges on the focus, and the focus name on the canvas.

        The focus icon is at the far west (:meth:`_focus_pos`), with its name to the
        *right*. Each edge starts at the **right end of that name**, so that the lines
        never cross the label. The result is the attach point in dot coordinates and the
        name as it is drawn (clipped to the space between the icon and the canvas edge).
        """
        fx, fy = self._focus_pos(width, canvas_h, slots)
        icon_cx = fx >> 1
        name = self._clip(self._label(self._focus), width - (icon_cx + 2))
        anchor_cx = icon_cx + 2 + cell_len(name) + 1
        return anchor_cx * 2, fy, name

    def _place_neighbours(
        self,
        width: int,
        canvas_h: int,
        shown: list[str],
        more: bool,
        *,
        more_label: str = "",
    ) -> dict[str, tuple[int, int]]:
        """The dot positions of the drawn fan: a grid of rows east of the focus name.

        Each node that shows takes its own row, east of the anchor at the end of the focus
        name. The strongest link is at the top and the weakest is at the bottom. This is the
        same order as the list below, so the picture and the rows agree. The collapsed
        ``…`` marker (key :data:`_MORE`) takes the last slot of the fan. The rows come from
        :meth:`_fan_rows`: equal spacing, one blank row between two markers, and the block
        centred.

        The distance to the east follows an arc, and the name that each marker must show
        limits it **for each marker** (JP, 2026-08-09). The fan reaches to a tip sized for a
        usual name (:data:`_FAN_BASE_LABEL_W`). Then each marker whose name needs more
        space than that moves west by exactly the difference. Thus one long name makes the
        reach shorter only on *its* row, and the rest of the fan stays where it was. The
        first version sized the full fan for the longest name, so one repeater name of 29
        cells pulled each marker west. The version before that did no sizing, so a fixed
        margin of 20 cells ellipsized each longer name, also on a canvas with free space.

        A marker never comes further west than :data:`_MIN_FAN_REACH` of the width,
        whatever its name needs. Past that point, the markers pile onto the focus and the
        graph does not look like a fan. Thus the label placer truncates a name that is too
        long for the remaining space.

        Only the *row* of a leaf sets its angle. Its east reach curves by
        :data:`_FAN_X_FLATTEN` of that angle. Thus the fan has a gentle curve and is not a
        flat column, and the leaves at the rim still reach far east and do not curve back
        toward the focus on a true circle. A small fan uses a proportionally smaller part
        of the arc, so two neighbours are near due east, not at opposite rims. The anchor
        is at the end of the name (not at the icon). Thus the fan has the full width east
        of the focus label.

        Args:
            width: The canvas width in cells.
            canvas_h: The canvas height in cells.
            shown: The neighbours drawn with markers of their own, the strongest first.
            more: Whether a collapsed ``…`` marker takes the last slot.
            more_label: The label of that marker at rest. Thus the layout of its slot is
                for the label that it *usually* has, not for a selection that soon moves.
        """
        placed: dict[str, tuple[int, int]] = {}
        keys = list(shown) + ([_MORE] if more else [])
        if not keys:
            return placed
        dot_w = width * 2
        ax, _ay, _name = self._focus_anchor(width, canvas_h, len(keys))
        rows = self._fan_rows(canvas_h, len(keys))
        labels = [self._label(node) for node in shown] + ([more_label] if more else [])
        floor = ax + (dot_w - ax) * _MIN_FAN_REACH
        # The cell of the marker, a blank, then the label: these must be east of a marker.
        # They are in cells, multiplied by two for dot space.
        rx = max(floor, 2 * (width - 2 - _FAN_BASE_LABEL_W)) - ax
        phi = _FAN_HALF_ANGLE * min(1.0, (len(keys) - 1) / 5.0)
        for i, (node, row, label) in enumerate(zip(keys, rows, labels, strict=True)):
            angle = 0.0 if len(keys) == 1 else -phi + (2 * phi) * i / (len(keys) - 1)
            x = ax + rx * math.cos(angle * _FAN_X_FLATTEN)
            room = 2 * (width - 2 - min(cell_len(label), _FAN_LABEL_W))
            placed[node] = (
                round(max(floor, min(x, room))),
                row * 4 + 2,  # the middle of the cell, so that an edge meets the glyph
            )
        return placed

    @staticmethod
    def _clip(label: str, room: int, cap: int = _LABEL_W) -> str:
        """``label`` fit to ``room`` cells.

        The label is complete if it fits, or else ellipsized (only ``…`` at one cell,
        nothing at zero). The limit and the space to the edge both go through here. Thus a
        name is never made shorter than is necessary.
        """
        room = min(room, cap)
        if room <= 0:
            return ""
        if len(label) <= room:
            return label
        return "…" if room == 1 else label[: room - 1] + "…"

    def _place_label(self, canvas: MapCanvas, x: int, y: int, label: str, rgb: RGB) -> None:
        """Place one marker label. On a collision, try one row below, then one row above.

        The label goes to the right of the marker where it fits, or else to the left.

        The label is clamped to the side that has more space: the free cells to the
        right of the marker, or to its left. Thus the selection keeps as much of its name
        as the canvas permits before it ellipsizes.
        """
        cx = x >> 1
        room = max(canvas.cell_w - (cx + 2), cx - 1)  # the larger space, right or left
        label = self._clip(label, room)
        if not label:
            return
        for dy in (0, 4, -4):
            if canvas.marker_label(x, y + dy, label, rgb):
                return

    def _label_right(self, canvas: MapCanvas, x: int, y: int, label: str, rgb: RGB) -> None:
        """Place a node label to the *right* of its marker, on another row if necessary.

        Each node except the selected one (which keeps the two-sided :meth:`_place_label`)
        has its name to the right of its icon: the direction in which the user reads the
        walk. The label tries the row of the marker first. Then it tries a row below and
        above, to go past a neighbour that is too near. If all the checked rows are
        blocked, the label is stamped to the right in all cases, so that a node is never
        only a bare glyph. The name is clamped to the free cells between the marker and
        the canvas edge. :meth:`_place_neighbours` already sized the fan to keep these
        cells free, so on a canvas that is wide enough the clamp never cuts a name.
        """
        cx, cy = x >> 1, y >> 2
        start = cx + 2
        label = self._clip(label, canvas.cell_w - start, cap=_FAN_LABEL_W)
        if not label:
            return
        for dy in (0, 1, -1, 2, -2):
            if canvas._place_run(start, cy + dy, label, rgb, bold=True, checked=True):
                return
        canvas._place_run(start, cy, label, rgb, bold=True)

    def _label_left(self, canvas: MapCanvas, x: int, y: int, label: str, rgb: RGB) -> None:
        """Place a short note to the *left* of a marker, on the row of the marker.

        This note is the only thing drawn on that side (the rank of the collapsed marker,
        refer to :meth:`_canvas_lines`). It stays on the row that it is about and does not
        move. If the counter moves one row, it looks like a part of the neighbour above.
        West of a fan marker there is only the incoming edge of the marker and little
        else. Thus the note is placed in all cases, and it overprints the braille
        under it: the same fallback as at the end of :meth:`_label_right`. The run includes
        a blank after the note, so that the gap before the mark is a real gap. If that
        cell is not written, the braille of the incoming edge goes through it and joins
        the counter to the glyph.
        """
        cx, cy = x >> 1, y >> 2
        label = self._clip(label, cx - 1, cap=_FAN_LABEL_W)
        if not label:
            return
        canvas._place_run(cx - 1 - cell_len(label), cy, label + " ", rgb, bold=True)

    def _legend(self) -> Text:
        """The one-line legend of the glyphs and the edges under the canvas.

        The legend explains the two halves of a mark, because the canvas now draws complete
        marks. It shows the glyph of each role in the same colour that it has on the
        canvas, so the line is a sample, not a chart of shapes. The pairs come from the same
        source as in :meth:`_glyph`. Thus the legend cannot become different from the
        picture. This is also true on the console, where the ``type.*`` names let each mark
        select its slot on purpose, and not get the slot to which a downsample of the hex
        moves it. The words stay muted: they are chrome, and the marks are the content.
        """
        legend = Text()
        for (glyph, colour), word in (
            (SELF_MARK, " you   "),
            (NODE_GLYPHS[NODE_TYPE_REPEATER], " repeater   "),
            (DEFAULT_GLYPH, " node   "),
            (UNKNOWN_MARK, " unknown"),
        ):
            legend.append(glyph, style=colour)
            legend.append(word, style="muted")
        legend.append("  ·  edge = SNR · faint = stale", style="muted")
        return legend

    # -- the list --

    def _list_lines(
        self, width: int, rows: list[str], depths: dict[str, int], win: int
    ) -> list[str]:
        """The selectable rows, in a list window of ``win`` lines under a pinned heading.

        Only the rows scroll. The heading (and all that is above it) does not move. When
        the list is longer than the list window, faint ``↑/↓ n more`` markers take the
        edge rows of the list window, and the highlight stays in the remaining rows. The
        row count of the list window becomes the PgUp/PgDn step.
        """
        out: list[str] = []
        if self._query_row():
            out.append(query_line(self._filter, width))
        if self._filter:
            heading = Text("Matches", style="accent")
            heading.append("  ·  nearest first · Enter focuses", style="muted")
            out.append(render_to_ansi(heading, width, no_wrap=True))
            if not rows:
                out.append(render_to_ansi(Text("no matches", style="muted"), width))
                return out

            name_w, key_w = self._lane_widths(width, rows, _MATCH_ROW_FIXED)

            def render(i: int) -> Text:
                return self._match_row(rows[i], i == self._index, depths, name_w, key_w)
        else:
            heading = Text("Links", style="accent")
            heading.append("  ·  strongest observed first · Enter walks", style="muted")
            out.append(render_to_ansi(heading, width, no_wrap=True))
            if not rows:
                note = Text(
                    "no observed links from here — type to find another node", style="muted"
                )
                out.append(render_to_ansi(note, width))
                return out
            # The rows set *which* links this list has, and in which order (:meth:`_rows`).
            # The code finds a link in the link table only *through* the rows. There was a
            # bug when the list rendered directly from the table: the table still had the
            # node that the walk came from. Thus each index after it named one row and drew
            # another, and the highlight was one link away from the link that the canvas
            # lit. The onward counts still come from the full table: that cache is for each
            # focus, not for each paint, and a narrowed list must not fill it.
            by_other = dict(self._links_of(self._focus))
            onward = self._onward_counts(self._links_of(self._focus))
            name_w, key_w = self._lane_widths(width, rows, _LINK_ROW_FIXED)

            def render(i: int) -> Text:
                other = rows[i]
                return self._link_row(
                    other,
                    by_other[other],
                    i == self._index,
                    onward.get(other, 0),
                    name_w,
                    key_w,
                )

        top, count = self._list.fit(len(rows), win, self._index)
        if top > 0:
            out.append(render_to_ansi(ListWindow.marker(top, "above"), width))
        for i in range(top, top + count):
            out.append(render_to_ansi(render(i), width, no_wrap=True))
        below = len(rows) - top - count
        if below > 0:
            out.append(render_to_ansi(ListWindow.marker(below, "below"), width))
        return out

    def _lane_widths(self, width: int, nodes: list[str], fixed: int) -> tuple[int, int]:
        """The ``(name, key)`` lane widths of the list: the name lane first, then the key.

        The name lane is as wide as the widest name in the rows (a list of short names
        stays narrow). The key lane takes the remaining width, with a limit of the full id
        of 12 hex digits. When the two together are wider than the row, the *key* becomes
        narrower first. It shrinks on a byte boundary down to a minimum that still shows
        its full lit hash and a ``…``. Only when the key is at that minimum does a long
        name start to lose letters. Thus the name is the last thing that is truncated, and
        the hash is never truncated (the user reads names, and addresses by hash).
        """
        hash_w = max(2, min(_LIST_HASH_W, self._prefix_bytes * 2))
        # The narrowest key lane that still shows the full hash: the hash and a "…". The
        # width is odd, so :func:`highlighted_hash` keeps an even number (hash_w) of digits
        # with no unused cell. When the hash already fills the id, there is nothing to
        # remove, so the minimum is the full width.
        key_floor = _LIST_HASH_W if hash_w >= _LIST_HASH_W else hash_w + 1
        avail = width - fixed
        widest = max((cell_len(self._label(n)) for n in nodes), default=_LIST_NAME_MIN)
        name_w = max(_LIST_NAME_MIN, min(widest, avail - key_floor))
        key_w = max(key_floor, min(_LIST_HASH_W, avail - name_w))
        return name_w, key_w

    def _onward_counts(self, pairs: list[tuple[str, Link]]) -> dict[str, int]:
        """How many links continue from each neighbour, without the link back here.

        The result is cached for each focus (the counts depend only on the frozen graph and
        on the node whose neighbours the list shows). Before the cache, this was quadratic
        at each paint: each neighbour went again through a new copy of the full link table.
        """
        focus = self._focus
        memoized = self._onward_memo.get(focus)
        if memoized is not None:
            return memoized
        counts: dict[str, int] = {}
        for other, _link in pairs:
            # A link from the neighbour "continues onward" if its far end is not here. One
            # endpoint is the neighbour, so only the far endpoint can be the focus.
            counts[other] = sum(1 for far, _l in self._links_of(other) if far != focus)
        self._onward_memo[focus] = counts
        return counts

    def _link_row(
        self, other: str, link: Link, selected: bool, onward: int, name_w: int, key_w: int
    ) -> Text:
        """One neighbour row: glyph, name, hash, SNR and bar, evidence, onward count."""
        glyph, glyph_style = self._glyph(other)
        row = Text()
        row.append("❯ " if selected else "  ", style="cursor" if selected else "")
        row.append(glyph, style=glyph_style)
        row.append(" ")
        name_style_ = self._list_name_style(other)
        row.append(fit_cells(self._label(other), name_w), style=name_style_)
        row.append(" ")
        row.append_text(
            highlighted_hash(other, self._prefix_bytes, width=key_w, known=self._known(other))
        )
        row.append("  ")
        snr = link.median_snr
        if snr is not None:
            row.append(f"{snr:+5.1f}", style=snr_style(snr))
        else:
            row.append("    —", style="muted")
        row.append(" ")
        row.append_text(snr_bar(snr, width=4))
        row.append(f" {min(link.samples, 999):>3}×", style="muted")
        tags = "".join(_SOURCE_TAGS[s] for s in sorted(link.sources & _SOURCE_TAGS.keys()))
        row.append(f" {tags:<4}", style="faint")
        age = format_age(
            max(0.0, (utcnow() - link.last_seen).total_seconds())
            if link.last_seen is not None and getattr(link.last_seen, "tzinfo", None)
            else None
        )
        row.append(f"{age:>5}", style="muted")
        if onward:
            row.append(f"  ⋯ {onward}", style="faint")
        if selected:
            row.style = "cursor"
        return row

    def _match_row(
        self, node: str, selected: bool, depths: dict[str, int], name_w: int, key_w: int
    ) -> Text:
        """One find match: glyph, name, hash, and its distance from our node."""
        glyph, glyph_style = self._glyph(node)
        row = Text()
        row.append("❯ " if selected else "  ", style="cursor" if selected else "")
        row.append(glyph, style=glyph_style)
        row.append(" ")
        row.append(fit_cells(self._label(node), name_w), style=self._list_name_style(node))
        row.append(" ")
        row.append_text(
            highlighted_hash(node, self._prefix_bytes, width=key_w, known=self._known(node))
        )
        row.append("  ")
        if node == self._topo.self_id:
            row.append("this device", style="muted")
        elif node not in depths:
            row.append("island", style="warn")
        else:
            ring = depths[node]
            row.append(f"{ring} hop{'s' if ring != 1 else ''} out", style="muted")
        if selected:
            row.style = "cursor"
        return row

    def _list_name_style(self, node: str) -> str:
        """The name colour in the list: the node hue (from its key), our node pure white."""
        if node == self._topo.self_id:
            return "you"
        label = self._label(node)
        if label == node[:8]:  # a bare hash is not a name. The colour shows a name
            return "node.unknown"
        return name_style(label, node)

    def _known(self, node: str) -> bool:
        """Whether the node is identified (named, or our node), so that its hash can have a hue.

        Each hash lane here gives this gate to
        :func:`~meshterm.ui.widgets.highlighted_hash`. The full hash of an unknown node is
        grey, the same as its ``node.unknown`` name lane and its ``○`` ring on the canvas.
        """
        return self._list_name_style(node) != "node.unknown"

    def _marker_rgb(self, node: str) -> RGB:
        """The marker colour of a node: its *type* colour, as in all places (JP, 2026-08-09).

        The walk once gave each marker the colour of its identity (the hue of the node,
        from its key), and only the glyph shape showed the type. Thus this canvas was
        different from each other place that shows a node with its type (the map, the
        route graph, a contact row). There, the colour of the mark *is* the type, and the
        ``type.*`` styles of the theme select it on purpose for each platform (a simple
        downsample makes the violet of the repeater grey). The identity has its own lane
        here, and always had it: the **name** next to the marker, in the hue of the key
        (:meth:`_label_rgb`). The shape and the colour now agree, and the legend under the
        canvas can explain both.
        """
        return mark_rgb(self._glyph(node)[1])

    def _label_rgb(self, node: str) -> RGB:
        """The label colour for a node on the canvas: its hue, our node white, a bare hash grey.

        This is the RGB form of :meth:`_list_name_style`: the hue of the key for a node
        with a name, white for our node, and the grey of an unknown node for a node known
        only by a bare hash. A key that stands in for a name never gets a colour of its
        own. Thus the hash label stays grey, and the marker shows only the type mark
        (refer to :meth:`_marker_rgb`).
        """
        style = self._list_name_style(node)
        if style == "you":
            return (255, 255, 255)
        if "#" not in style:  # a theme name (node.unknown). The platform selects the shade
            return mark_rgb(style)
        return parse_hex(style.rsplit("#", 1)[-1])

    def _glyph_style(self, node: str) -> str:
        """The Rich style of the type glyph of a node in body text: the colour of its type.

        This is the text form of :meth:`_marker_rgb`: the same ``type.*`` entry (or the
        yellow of the star). Thus the first glyph of the focus line, the glyphs of the list
        rows, and the canvas markers are one mark, drawn in three ways.
        """
        return self._glyph(node)[1]

    def _empty_state(self, width: int) -> list[str]:
        """A helpful explanation while the evidence graph is still empty."""
        lines = [
            Text(),
            Text("This walk has no evidence to draw yet.", style="accent"),
            Text(),
            Text("Topology accrues passively as the mesh talks:", style="muted"),
            Text("  · every successful trace maps each link it walked", style="muted"),
            Text("  · firmware routes and overheard relay chains fill in more", style="muted"),
            Text("  · a repeater's neighbour table adds its own vantage point", style="muted"),
            Text(),
            Text("Run a trace, or just leave MeshTerm listening.", style="muted"),
        ]
        return [render_to_ansi(t, width, no_wrap=True) for t in lines]

    def _glyph(self, node: str) -> tuple[str, str]:
        """The mark of a node: the type glyph and the colour of that type in all the app.

        These are the shared marks (:data:`~meshterm.ui.widgets.NODE_GLYPHS`) and the
        shared ``type.*`` colours for them. Thus a node shows here with exactly the glyph
        and colour that the map, the route graph, and the contact list use for it: ``▲``
        violet for a repeater, ``■`` for a room, ``◉`` for a sensor, ``●`` pink for a
        plain node, the yellow ``★`` for our node, and the grey ``○`` for a node that we
        heard of but never identified. The colour and the shape say the same thing. Thus
        the legend below the canvas is a real explanation, not a coincidence.

        Returns:
            ``(glyph, colour)``: the colour in the encoding that
            :func:`~meshterm.ui.theme.mark_rgb` takes (a literal hex, or a theme name where
            the platform must select its own slot).
        """
        if node == self._topo.self_id:
            return SELF_MARK
        contact = self._contacts.get(node)
        if contact is None:
            return UNKNOWN_MARK
        return NODE_GLYPHS.get(contact.node_type, DEFAULT_GLYPH)

    def _label(self, node: str) -> str:
        """The display name: our own name for our node, a contact name, or a short hash."""
        if node == self._topo.self_id:
            return self._self_label
        return self._topo.display_name(node) or node[:8]


async def open_walk(ctx: AppContext) -> None:
    """Build the evidence graph and run the full-screen mesh walk until the user closes it.

    The contacts and our identity come from the device when the device can be reached.
    This is best-effort: the stored evidence draws correctly without them, but with
    hashes instead of names. The graph comes fully from the repository, as a snapshot
    taken one time when the screen opens. Nothing is ever transmitted.

    Args:
        ctx: The shared application context (it must run the interactive TUI).

    Raises:
        RuntimeError: If the call is not from the interactive menu (no full-screen
            session).
    """
    from ..services.topology import build_topology
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - the caller is only in the menu
        raise RuntimeError("the mesh walk is only available in the menu")
    session = ctx.ui.session

    contacts: list[Contact] = []
    self_label = "you"
    self_hash: str | None = None
    try:
        if ctx.is_connected or ctx.settings.connect_on_start:
            contacts = await ctx.devstate.contacts()
            info = await ctx.devstate.self_info()
            self_label = str(info.get("name") or "you")
            self_hash = str(info.get("public_key") or "") or None
    except Exception:  # noqa: BLE001 - names are optional. The graph renders without them
        contacts = []
    prefix_bytes = await ctx.devstate.routing_prefix_bytes()

    topo = build_topology(
        self_id=self_hash or "local",
        contacts=contacts,
        trace_paths=ctx.repo.trace_paths(),
        packet_paths=ctx.repo.packet_paths(),
        neighbour_links=ctx.repo.neighbour_links(),
    )
    by_id: dict[str, Contact] = {}
    for contact in contacts:
        canonical = topo.canonical(contact.public_key or contact.key_prefix)
        if canonical:
            by_id[canonical] = contact

    screen = WalkScreen(
        session=session,
        topo=topo,
        contacts=by_id,
        self_label=self_label,
        prefix_bytes=prefix_bytes,
    )
    try:
        await session.run_screen(screen)
    finally:
        # The same full paint from a clean screen as on the map. Braille fallback glyphs
        # may have smeared cells that the differential paint of prompt_toolkit never
        # writes again.
        session.request_full_repaint()
