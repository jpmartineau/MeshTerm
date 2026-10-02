# SPDX-License-Identifier: Apache-2.0
"""The Mesh walk: walk the mesh's observed shape one node at a time.

The interactive face of the ``walk`` tool. The trace path composer already distills
every fragment of topology we ever received — trace walks, firmware routes, RX-logged
relay chains, repeater neighbour tables — into one evidence graph
(:mod:`~meshterm.services.topology`); this screen is that graph made explorable. Nothing
here transmits: the walk is a reading of what the radio has already heard.

Rather than plotting the whole mesh at once (which reads as a hairball the moment the
graph grows), the walk keeps one node *in focus* — our own, to begin with — and shows
only its immediate neighbourhood:

* the **canvas** — the majority of the screen, so the shape stays legible — anchors the
  focus at the far **west** with its name to its right, then fans a deliberately sparse
  spread of its strongest neighbours across the width to the east, each named just to the
  right of its marker. Every marker is the app-wide **node-type mark**, glyph and colour
  both — the very mark the map and the route graph pin that node with — while *identity*
  rides the name beside it, in the node's key hue. Edges are braille lines coloured by the
  link's median SNR (green → amber → red, slate for links with no reading) and faded by
  evidence age; they leave the *right end of the focus name* so a connecting line never
  crosses a label. The fan sits on a row grid — one marker per row, a blank row between
  each, a blank row above and below where the canvas can afford it — and only as many
  neighbours as those rows carry are drawn; the weaker rest, ranked by observed strength
  (SNR, sample count, and recency folded together, so a strong-but-long-stale link is not
  promoted over a fresher one), collapse into one ``…`` marker, which stands in for
  whichever of them the list highlights (its own mark, its own name, and a grey
  ``(2/17)`` for where it sits among them). Only ways *onward* are drawn: the node the
  walk came from is behind you, and ⌫ is how you go back to it.
* the **link list** beneath names every way onward as a selectable row, strongest
  observed link first — so the cursor's home, row 0, is always the strongest link out of
  here: type glyph, name, hash, SNR with a quality bar, the evidence behind the link
  (samples, sources, age), and how many links continue onward from that node. The highlighted
  row's marker and label light white on the canvas. The list scrolls *within* the screen — the
  canvas, legend, and heading hold still, and
  faint ``↑/↓ n more`` markers bracket the window — with ↑↓ moving one row and
  PgUp/PgDn a windowful.

**Enter walks**: the highlighted neighbour becomes the new focus, the breadcrumb trail
across the top grows (a :mod:`~meshterm.ui.pathline` path line — powerline chips where the
terminal draws them, ``you › Hilltop-Repeater › …`` where it doesn't, each name in its
node's own hue), and **⌫ steps back** along it. Walking to a node already on the trail
truncates the stack to its first appearance — the loop you walked to get back there is
dropped rather than recorded. The trail never wraps: it is one line read through a
window, drawn whole while whole fits and *cropped* at either edge the walk continues
past — a chip sheared on its own half block, the way every path row in the app that
outgrows its lane is cut. At rest the window sits at the tail, so the focus and the
steps just taken are in view and it is the walk's start that is cropped; **←→ slide
it** to read back over a long walk. **^Y** refocuses our own node.
**Typing finds** — a global filter over every node in the graph, islands included — and
narrows the canvas's fan to matching neighbours as it goes, the focus and the came-from
node holding through it; Enter teleports the focus to the highlighted match (the trail
restarts there, since the walk didn't cross the gap). Esc peels find first, the screen
second.
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
from .widgets import DEFAULT_GLYPH, NODE_GLYPHS, format_age, highlighted_hash

if TYPE_CHECKING:
    from ..context import AppContext

#: The share of the width east of the focus's name that the fan keeps for its own reach,
#: however long the names it must make room for. Below this the graph stops reading as a
#: fan at all — the markers pile onto the focus — so past it the labels clip instead (see
#: :meth:`WalkScreen._place_neighbours`).
_MIN_FAN_REACH = 1 / 3

#: The canvas's floor in character rows: below this the fan's shape stops reading.
_CANVAS_MIN_H = 6

#: Rows the link list always keeps for itself under the canvas, however tall the
#: graph would like to be — a windowed list needs at least a few rows to scroll in.
_LIST_MIN_ROWS = 3

#: The widest any on-canvas label renders before it ellipsizes — a generous cap that most
#: real node names clear whole. It is only an upper bound: each label is *also* clamped to
#: the cells actually free between its marker and the canvas edge (see :meth:`_label_right`,
#: :meth:`_focus_anchor`, :meth:`_place_label`), so a name loses letters only when it
#: genuinely won't fit, not to a fixed short budget — the fan's placement leaves most
#: markers far more room than the old flat cap allowed.
_LABEL_W = 22

#: The fan's own label cap — higher than the focus's, and the longest a name may pull its
#: marker west to spell (JP, 2026-08-09). The fan is *where the names are read*, so it gets the
#: room; the focus keeps the tighter :data:`_LABEL_W` because every cell it spends pushes
#: the whole fan east, and its name is spelled out in full on the line below the canvas
#: anyway. 32 is the protocol's own limit on a node name, so a name that fits the mesh
#: fits here.
_FAN_LABEL_W = 32

#: The name length the fan's *default* reach is sized for. Markers whose own name is longer
#: step west from there one cell per extra cell of name; markers with a shorter one stay
#: put, so a single long name never drags the whole fan in with it.
_FAN_BASE_LABEL_W = 12

#: The fan's angular reach on each side of due east, in radians. This sets the fan's
#: *vertical* spread (the marker rows the leaves fan across); the *horizontal* reach is
#: flattened from it by :data:`_FAN_X_FLATTEN`, so a leaf's height and its easting are
#: decoupled. The whole fan stays east of the focus — neighbours to the right, labels
#: rightward — and a smaller neighbourhood uses proportionally less of the arc so two
#: nodes never sit at its extremes with nothing between them.
_FAN_HALF_ANGLE = math.radians(72)

#: How much flatter the fan's horizontal reach is than its vertical spread. A leaf's full
#: fan angle sets its *height*; its *east reach* uses only this fraction of that angle, so
#: the rim (top/bottom) leaves bow just gently back from due-east instead of curling in
#: toward the focus on a true circle. The fan then spreads across the width rather than
#: bulging in the middle with empty corners. ``1.0`` restores the old circular arc; lower
#: flattens it further. The setback still scales with the fan's angular reach, so a small
#: fan sitting near due-east barely eases back at all.
_FAN_X_FLATTEN = 0.55

#: The fan size worth giving up its outer blank rows for. Above this the canvas keeps a
#: blank row top and bottom; below it, those rows go to nodes instead — see
#: :meth:`WalkScreen._fan_capacity`.
_FAN_MIN_SLOTS = 7

#: Sentinel key for the collapsed weaker-links marker in the placed-node map. NUL can
#: never collide with a canonical id (those are hex).
_MORE = "\x00more"

#: The breadcrumb trail's own hop joiner (``›``, not the app-wide ``→``). Either end that
#: holds walk out of view wears the app's cut mark — a chip broken off on its own fill,
#: the faint ``…`` where the line is drawn in arrows (see :meth:`WalkScreen._trail_text`).
_TRAIL_SEP = " › "

#: Cells one ←/→ press slides the breadcrumb by — the same step every other windowed path
#: row in the app takes (the node page's routes, the Message paths lanes, the select
#: list's ``hscroll``), because this is the same window: one line, cropped, read a lane at
#: a time. A hop a press was the old fit's unit, when the overflow was whole hops going
#: behind a ``⋯``; a cropped line has no hop boundaries to step by, and stepping by the
#: crop's own unit is what makes the slide read as a slide.
_HSCROLL_STEP = 8

#: How many find matches the list shows at most (the filter narrows it fast).
_MAX_MATCHES = 10

#: The narrowest the neighbour/match list's name lane shrinks to. The lane is content-sized
#: and flexes up to whatever the fixed lanes leave (see :meth:`WalkScreen._lane_widths`),
#: so as much of a long name shows as the row can spare.
_LIST_NAME_MIN = 10

#: The *widest* the key lane spans — the whole 12-hex canonical id, addressed prefix lit in
#: the node's hue (see :func:`highlighted_hash`). The lane flexes down from here on a byte
#: boundary to hand a long name more room, but never below its lit hash (see
#: :meth:`WalkScreen._lane_widths`): the name loses letters before the hash does.
_LIST_HASH_W = 12

#: Cells a link row spends *outside* its two flexing lanes (name and key), so those two can
#: size to what's left: pointer (2) + type glyph and its space (2) + the name→key space (1)
#: + gap (2) + SNR (5) + space (1) + quality bar (4) + samples (5) + source tags (5)
#: + age (5) + a reserve for the trailing ⌫/⋯ marker (8).
_LINK_ROW_FIXED = 2 + 2 + 1 + 2 + 5 + 1 + 4 + 5 + 5 + 5 + 8

#: The same, for a find-match row — which trails a short distance note rather than the SNR
#: evidence: pointer (2) + glyph and space (2) + name→key space (1) + gap (2)
#: + a distance reserve (12, for ``this device`` / ``N hops out``).
_MATCH_ROW_FIXED = 2 + 2 + 1 + 2 + 12

#: SNR (dB) → edge colour anchors, interpolated linearly and clamped at the ends: the
#: red/amber/green of the app's snr styles, so the graph and the rows agree.
_SNR_STOPS: tuple[tuple[float, RGB], ...] = (
    (-15.0, (239, 68, 68)),
    (0.0, (250, 204, 21)),
    (10.0, (74, 222, 128)),
)

#: Edge colour for a link with no SNR reading at all (e.g. known only from a route).
_NO_READING: RGB = (100, 116, 139)

#: One-letter tags for the evidence classes backing a link, matching the path
#: composer's: T(race), R(oute), P(acket log), N(eighbour table).
_SOURCE_TAGS = {"trace": "T", "route": "R", "packet": "P", "neighbour": "N"}


def _snr_rgb(snr: float | None) -> RGB:
    """The edge colour for a link's median SNR (see :data:`_SNR_STOPS`)."""
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
    """``rgb`` dimmed (or mildly brightened) by ``factor``, clamped to byte range."""
    return tuple(max(0, min(255, round(c * factor))) for c in rgb)  # type: ignore[return-value]


def _freshness(last_seen: datetime | None, now: datetime) -> float:
    """How brightly a link draws for its evidence age: 1.0 fresh → 0.5 stale."""
    if last_seen is None or getattr(last_seen, "tzinfo", None) is None:
        return 0.55
    age = (now - last_seen).total_seconds()
    if age < 86400:
        return 1.0
    if age < 7 * 86400:
        return 0.8
    return 0.5


class WalkScreen(Screen):
    """The full-screen mesh walker: a focus neighbourhood canvas over a link list."""

    floating = False

    @property
    def fkey_lane(self):
        """``You`` on the screen's own F3, the shared pager and its two ends on F4/F5.

        ``You`` is not an end of the link list — it drops the whole trail and puts the
        focus back on our own node — so it never belonged behind the pager, where it had
        to stand in for *Top* and leave *Bottom* blank to keep the pretence. It takes a
        slot of the screen's own instead (JP, 2026-08-09), which is what F1-F3 are for,
        and the pager's Shift bank goes back to meaning here what it means everywhere
        else: the two ends of the list this screen scrolls. ^Y reaches the same action on
        a keyboard, the same chord the map spends on our node.

        Both nav slots need rows to move through, which a leaf node in a sparse graph may
        not have; ``You`` needs somewhere to come back *from* — a walked trail, or a find
        narrowing the list — and dims once the focus is already us.
        """
        from .tui.fkeys import FPair, default_lane

        lane = list(default_lane(nav=len(self._rows()) > 1))
        lane[2] = FPair("You", "locate", enabled=len(self._trail) > 1 or bool(self._filter))
        return lane

    def __init__(
        self,
        *,
        session,  # noqa: ANN001 - TuiSession, imported lazily to avoid a cycle
        topo: MeshTopology,
        contacts: dict[str, Contact],
        self_label: str,
        prefix_bytes: int = 0,
    ) -> None:
        """Create the walk over a built topology snapshot.

        Args:
            session: The running TUI session (repaints).
            topo: The evidence graph to walk.
            contacts: Contacts keyed by canonical id, for glyphs, names, and ages.
            self_label: Display name for our own node (its mesh name when known).
            prefix_bytes: Path-hash width to light in the hash lane (0 = none).
        """
        super().__init__()
        # The screen's name, and nothing else (JP, 2026-08-30). It carried the focus and
        # the graph's size — ``Mesh walk — Hilltop-Repeater · 42 nodes · 61 links`` — which
        # ran past a 53-column title bar, and every atom of it was already on the screen
        # below: the trail ends on the focus, the line under it names that node in colour
        # with its hash and its ring, and how big the neighbourhood is, is what the canvas
        # and the link list are *for*. Set once here rather than composed per render, so
        # the top row never rewrites itself as the walk moves.
        self.title = "Mesh walk"
        self._session = session
        self._topo = topo
        self._contacts = contacts
        self._self_label = self_label
        self._prefix_bytes = prefix_bytes
        #: The walked trail of canonical ids; the focus is its last entry. Walking
        #: appends, ⌫ pops, ^Y resets to us, a find teleport restarts it.
        self._trail: list[str] = [topo.self_id]
        #: Hops the breadcrumb line is scrolled off its *tail* end (see
        #: :meth:`_trail_text`). 0 is the resting state — the focus flush right — and
        #: every change to the trail returns it there.
        self._trail_scroll = 0
        #: ``((width, trail) → the furthest that can scroll)``, settled at render (see
        #: :meth:`_trail_max_scroll`) so a keypress can clamp itself without a width.
        self._trail_fit: tuple[tuple, int] | None = None
        #: The width the trail last rendered at, for that same clamp.
        self._trail_width = 0
        #: Index of the highlighted row in the current list (neighbours or matches).
        self._index = 0
        #: The live find-as-you-type filter ("" = off; matches every node known).
        self._filter = ""
        self._needs_scrub = True  # braille smear scrub, exactly like the map
        #: The link list's window (the list scrolls, the screen doesn't); its
        #: settled capacity is the stride a PgUp/PgDn moves the highlight by.
        self._list = ListWindow()
        # Graph-derived memos. The topology is snapshotted once when the screen opens
        # (fresh evidence means reopening), so everything derived purely from it —
        # neighbour lists, the node set, BFS depths, onward-link counts — is computed
        # once per key instead of once per repaint. Nothing ever invalidates these:
        # they live exactly as long as the frozen graph they describe.
        self._links_of_memo: dict[str, list[tuple[str, Link]]] = {}
        self._all_nodes_memo: set[str] | None = None
        self._hops_out_memo: dict[str, int] | None = None
        self._onward_memo: dict[str, dict[str, int]] = {}

    # --- state -------------------------------------------------------------------

    @property
    def _focus(self) -> str:
        """The node currently in focus (the trail's last step)."""
        return self._trail[-1]

    @property
    def _came_from(self) -> str | None:
        """The node the trail arrived from, or ``None`` at the trail's start."""
        return self._trail[-2] if len(self._trail) > 1 else None

    def _links_of(self, node: str) -> list[tuple[str, Link]]:
        """``(other, link)`` for every link off ``node``, strongest evidence first.

        Memoized per node over the frozen graph (the sort order is pinned at first
        ask — strength decays over hours, far slower than a screen stays open).
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
        """Every node the graph mentions, plus us (walkable even when alone)."""
        if self._all_nodes_memo is None:
            nodes = {self._topo.self_id}
            for link in self._topo.links():
                nodes.add(link.a)
                nodes.add(link.b)
            self._all_nodes_memo = nodes
        return self._all_nodes_memo

    def _hops_out(self) -> dict[str, int]:
        """BFS hop distance from our own node over the evidence links.

        Nodes with no path to us are absent — they are the islands, flagged as such
        wherever a distance would otherwise show.
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

        The one predicate behind both things the query narrows: the list of teleport
        candidates (:meth:`_matches`, over the whole graph) and the canvas's fan
        (:meth:`_canvas_lines`, over the focus's own neighbours). No filter matches
        everything, so a caller need not check for one first.
        """
        needle = self._filter.strip().casefold()
        if not needle:
            return True
        return needle in self._label(node).casefold() or needle in node.casefold()

    def _matches(self) -> list[str]:
        """Nodes the find filter matches: nearest first, then by display name."""
        if not self._filter.strip():
            return []
        depths = self._hops_out()
        candidates = [node for node in self._all_nodes() if self._is_match(node)]
        candidates.sort(key=lambda n: (depths.get(n, 999), self._label(n).casefold()))
        return candidates[:_MAX_MATCHES]

    def _rows(self) -> list[str]:
        """The selectable node ids the list currently shows: matches, or the ways onward.

        Neighbour rows are :meth:`_fan_nodes` — the same set the canvas draws, in the same
        strongest-first order — so row 0, where every reset of the cursor lands, is the
        strongest link out of here.
        """
        if self._filter:
            return self._matches()
        return self._fan_nodes()

    # --- input -------------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """Walking keys — or the live find query while one is being typed.

        Every atom fits the 72-cell budget only because two of them take turns. ``←→
        trail`` appears exactly while the breadcrumb has more walk than width (an inert
        key is never advertised — the hint line's own long-standing rule, and the F-lane's
        after it), and it takes the place of ``type to find``: a walk deep enough to
        overflow the trail is one where the scroll is the unknown key, while typing
        announces itself the instant a letter lands, replacing this whole line with the
        query. The find is self-teaching; the scroll had no way to be.
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
        """Move the highlight, walk, back up, find, or dismiss.

        Home/End are the list's own ends here, as on every other scrolling screen; the jump
        back to our own node is ``locate`` (^Y, and the lane's F3), which is not a place in
        this list at all — see :attr:`fkey_lane`.
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
            # Both ends clamp rather than wrap: the list scrolls inside a window (see
            # ListWindow), and a highlight that jumped end to end would take the window
            # with it — the one move that looks like the screen changed under you.
            self._index = max(0, self._index - 1)
        elif action == "down" and rows:
            self._index = min(len(rows) - 1, self._index + 1)
        elif action == "pageup" and rows:
            # The highlight pages by one list windowful: the window follows the
            # highlight, so paging the view without it would just snap straight back.
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
            # The breadcrumb scrolls, the list doesn't — it is the one line here with more
            # content than width. Clamped against the width the last paint settled, so
            # holding ← parks at the head instead of banking presses to undo (0 until the
            # trail actually overflows, which makes the key inert on a short walk).
            self._trail_scroll = min(
                self._trail_scroll + _HSCROLL_STEP,
                self._trail_max_scroll(self._trail_width),
            )
        elif action == "right":
            self._trail_scroll = max(0, self._trail_scroll - _HSCROLL_STEP)
        elif action == "locate":
            # ^Y (and F3): abandon the walk rather than move within it — the trail goes
            # back to just us and any find narrowing the list is dropped with it.
            self._trail = [self._topo.self_id]
            self._filter = ""
            self._index = 0
            self._trail_scroll = 0
        elif action == "text":
            if not data.isspace() or self._filter:  # never begin the filter with a space
                self._filter += data
                self._index = 0
        elif action == "space" and self._filter:
            self._filter += " "  # node names carry spaces; only meaningful mid-query
        self._needs_scrub = True
        self._session.invalidate()

    def _walk(self, rows: list[str]) -> None:
        """Focus the highlighted row: a step along the trail, or a find teleport."""
        if not rows:
            return
        target = rows[min(self._index, len(rows) - 1)]
        if self._filter:
            # A teleport restarts the trail at the target — the walk didn't cross the
            # gap, so pretending it did would make ⌫ retrace a path never taken.
            self._trail = [target]
            self._filter = ""
        elif target in self._trail:
            # Revisiting a node already on the trail — stepping back through the west
            # node, or looping round to an earlier one — truncates the stack to that
            # node's first appearance. We drop the circular stretch we walked to get
            # back here rather than recording the round trip; losing the loop is the point.
            self._trail = self._trail[: self._trail.index(target) + 1]
        else:
            self._trail.append(target)
        self._index = 0
        self._trail_scroll = 0  # the new focus is the news; put it back in view

    # --- smear scrub (same fallback-glyph problem as the map) ---------------------

    def consume_edge_scrub(self) -> int:
        """Right-edge columns to force-repaint after a redraw (see the map screen)."""
        if not self._needs_scrub:
            return 0
        self._needs_scrub = False
        return 2

    # --- rendering -----------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render the trail, the focus line, the canvas, and the windowed link list.

        The body is laid out to fit the frame's viewport exactly: the canvas takes
        the majority of the rows (a touch less of the share on tall terminals), the
        chrome around it holds still, and whatever remains is the link list's
        window — only its rows scroll, inside :meth:`_list_lines`.
        """
        links = self._topo.links()
        rows = self._rows()
        self._index = max(0, min(self._index, len(rows) - 1)) if rows else 0
        if not links:
            return self._empty_state(width)

        depths = self._hops_out()
        selected = rows[self._index] if rows else None
        viewport = self._scroll_viewport  # recorded by the frame before this render

        header = self._header_lines(width, depths)
        chrome = len(header) + 3 + self._query_row()  # legend, blank, list heading (+find echo)
        canvas_h = self._canvas_height(viewport, chrome)
        list_win = max(1, viewport - chrome - canvas_h)

        lines: list[str] = list(header)
        lines.extend(self._canvas_lines(width, canvas_h, selected))
        lines.append(render_to_ansi(self._legend(), width, no_wrap=True))
        lines.append("")
        lines.extend(self._list_lines(width, rows, depths, list_win))
        self._scroll_total = max(1, len(lines))
        return lines

    def _query_row(self) -> int:
        """Whether this paint spends a list row echoing the find query (1) or not (0).

        Only where the footer isn't drawn (:attr:`~meshterm.platforms.Platform.footer_fkeys`):
        there the hint line carrying the query never reaches the screen, so without this row
        the list would narrow to matches with no sign of what was typed to narrow it. The row
        sits directly above the ``Matches`` heading — above what it narrows, as on every other
        find-as-you-type screen — and the list, not the canvas, cedes the line for it.
        """
        return 1 if self._filter and get_platform().footer_fkeys else 0

    def _canvas_height(self, viewport: int, chrome: int) -> int:
        """Rows the canvas takes: the majority of the screen, ceded where pointless.

        The share starts at ~62% of the viewport and tapers toward half on tall
        terminals (a huge graph area buys little once the fan is legible, while the
        list keeps earning rows). A sparse neighbourhood caps it lower — a two-node
        link needs no half-screen void — and the link list always keeps its
        :data:`_LIST_MIN_ROWS` under the fixed chrome.
        """
        crowd = len(self._fan_nodes())
        share = 0.62 - 0.12 * min(max(viewport - 20, 0) / 24.0, 1.0)
        height = round(viewport * share)
        height = min(height, max(_CANVAS_MIN_H, 2 * crowd + 1))
        height = min(height, viewport - chrome - _LIST_MIN_ROWS)
        return max(4, height)

    def _header_lines(self, width: int, depths: dict[str, int]) -> list[str]:
        """The breadcrumb trail and the focus node's identity line.

        The trail draws always — even at the root, where it is just our own node — so the
        breadcrumb is a constant fixture and the header keeps a steady height whether you
        have walked or not, rather than the whole body shifting up a row the moment you take
        the first step.
        """
        return [
            render_to_ansi(self._trail_text(width), width, no_wrap=True),
            render_to_ansi(self._focus_line(depths), width, no_wrap=True),
        ]

    def _trail_text(self, width: int) -> Text:
        """The breadcrumb trail as a path line: one line, ← → scrollable, never wrapped.

        The walk *is* a path — us, then every node stepped through, ending on the focus —
        so it renders through :class:`~meshterm.ui.pathline.PathLine` like every other hop
        sequence in the app: powerline chips wherever the terminal can draw them, the
        trail's own ``›`` arrows where it can't. Each hop wears its node's key-derived hue
        (ours the white ``you``; a node known only by a bare hash stays muted — colour is
        reserved for keyed identities), so the trail and the rows below it agree on who is
        who.

        The line never wraps and it is never elided: it is composed once at its natural
        length and then **read through a window**, exactly as every other path row in the
        app that outgrows its lane is (the node page's routes, the Message paths lanes, a
        trophy row under ``←→``). Whole while whole fits — flush left, opening on its
        rounded head chip, as it would in a lane with cells to spare — and cropped when it
        doesn't, with **each edge the walk continues past wearing the cut mark**
        (:func:`~meshterm.ui.pathline.cut_mark`): a chip sheared on the half block in its
        own fill, the faint ``…`` where the trail is drawn in arrows. What the reader
        loses is the cells the lane ran out of, never the whole hop those cells were part
        of (JP, 2026-09-05) — no ``⋯`` here, on either side, because nothing is elided.

        Which edge the line hangs off follows one thing: **whether the walk's start is on
        it.** A line showing its first hop sits left, where a path that begins at its
        beginning belongs; one cropped at the head sits right, holding the crack against
        the edge it continues past — and a crop lands exactly on the width, so that flush
        is by construction rather than by a pad. At rest the window is at the tail, so the
        focus and the steps that just led to it are what a glance lands on.

        ← and → then **slide the window** (JP, 2026-08-09), :data:`_HSCROLL_STEP` cells a
        press: the cropped head is a real part of the walk and a long one had no way to be
        read at all. The marks are chrome *inside* the lane rather than extra width — each
        costs the window a cell, so the crop is measured only once both are known, else the
        line would draw a cell past the row. Sliding stops where the head comes into view
        rather than running the line off the edge, and any change to the trail itself
        resets it: a walk, a step back, a teleport and ^Y all end with the focus in view,
        which is where the next move is read from.
        """
        self._trail_width = width
        full = PathLine(
            [self._trail_hop(node) for node in self._trail], separator=_TRAIL_SEP
        ).text()
        total = full.cell_len
        # Clamped before the fit is read, so a line that grew back into its width (a
        # wider terminal, a step back) leaves no slide banked behind a whole picture.
        self._trail_scroll = max(0, min(self._trail_scroll, self._trail_max_scroll(width)))
        if total <= width:
            return full  # whole, from the start, flush left — nothing to crop or slide
        end = total - self._trail_scroll  # one past the last cell the window shows
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
        """How far ← may slide the trail: the first whole step that shows its head.

        Sliding past that only shortens a line already showing every cell it has, so the
        walk stops there — the same claim the F-lane makes when it dims a key that would
        do nothing. The stop is a whole :data:`_HSCROLL_STEP` rather than the exact cell
        that brings the head in: a slid line has given up a cell to its right mark, so its
        last window spans one less than the lane, and stopping short of a whole step would
        leave the *left* mark drawn, promising a head ← can no longer reach. (Every other
        windowed path row in the app clamps the same way.) ``0`` for a trail that fits,
        which is what makes ← inert on a short walk without the handler knowing the width.

        Memoized on ``(width, trail)``: it costs a rendering, and the answer only moves
        when the walk or the terminal does.
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
        """One walked step as a path hop: its display name in its own identity colour.

        Our own node takes the app-wide :data:`~meshterm.ui.pathline.SELF_GLYPH` instead
        of its name: every walk sets out from us, so the star says in one cell what the
        trail's most width-starved line would otherwise spend a whole name on — and the
        cells it frees are steps that stay in view before the head has to be elided.
        """
        style = self._list_name_style(node)
        if style == "you":
            return PathHop(SELF_GLYPH, you=True)
        return PathHop(
            self._label(node),
            key=None if style == "node.unknown" else node,
        )

    def _focus_line(self, depths: dict[str, int]) -> Text:
        """Who is in focus: glyph, name with its parenthesized hash, distance, and recency.

        The leading glyph carries the node type as its *shape* and the node's own key hue
        as its *colour* (:meth:`_glyph_style`, matching the canvas marker), so the line no
        longer spells the type out; the name reads ``name (hash)`` — the addressed
        path-hash in parentheses, its digits lit in the node's hue — rather than a bare
        slice of the key.

        The ``N hops out`` distance is the *shortest* observed path from us to this node
        (a BFS over the whole evidence graph, :meth:`_hops_out`) — the node's ring in the
        mesh. It is deliberately **not** the length of the breadcrumb trail above, which is
        the route you happened to *walk* to reach the focus: wander out a long way and
        double back, or step to a node also reachable by a shorter link, and the walk is
        longer than the ring. The two answer different questions — "how near is this node?"
        versus "how did I get here?" — so a shorter "hops out" than the trail is correct,
        not a miscount.
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
        """The node's addressable path-hash as hex, or ``''`` for a placeholder id.

        The first ``prefix_bytes`` bytes (at least one, so there is always a hash to show)
        of a hex id; a non-hex stand-in like ``"local"`` — our own node with no key — has
        no hash and yields the empty string, so the focus line drops the parenthetical.
        """
        raw = node.lower()
        if not raw or any(c not in "0123456789abcdef" for c in raw):
            return ""
        return raw[: max(1, self._prefix_bytes) * 2]

    # -- the canvas --

    def _canvas_lines(self, width: int, canvas_h: int, selected: str | None) -> list[str]:
        """Draw the focus neighbourhood: focus at the far west, the strongest fan east.

        The focus icon sits at the far west with its name to its right; the fan spreads
        east across the available width (see :meth:`_place_neighbours`), each neighbour
        named to the right of its marker. Every marker is a whole **node-type mark**, glyph
        and colour both (:meth:`_marker_rgb`), the same one the map and the route graph
        pin that node with; identity rides the *name* beside it, in the key hue. No marker
        is recoloured, not even the selection's — the highlighted row is shown by its
        *label* going white and by the lit route.

        While a find query is being typed the fan is narrowed to the neighbours it matches
        — the east is *the ways onward*, which is what the query is asking about — and the
        focus holds through it: it is the walk so far, not a candidate, so the picture keeps
        its bearings while the choices thin out. The node the walk came *from* is not drawn
        at all (JP, 2026-08-09): it is behind you, ⌫ is how you go back to it, and drawing
        it made the one thing on the canvas that isn't a way onward look like one.

        Only as many neighbours as the area can carry get their own marker (see
        :meth:`_fan_capacity`); the weaker rest collapse into one ``…`` marker at the fan's
        foot. Highlighting a collapsed row from the list makes that marker *stand in for*
        the highlighted node — its own type mark, its own name, and a grey ``(2/17)`` for
        where it sits among the collapsed — so a selection is never invisible and never
        mistaken for a node of its own. Every edge leaves the *right end of the focus name*
        so a connecting line never crosses a label, and the edge to the selected link draws
        brightest and undimmed over the rest.
        """
        canvas = MapCanvas(width, canvas_h)
        by_other = dict(self._links_of(self._focus))
        fan = self._fan_nodes()
        capacity = self._fan_capacity(canvas_h)
        if len(fan) > capacity:
            # The … marker takes a slot of its own, so it has to earn it: it stands in for
            # the last slot's worth *plus* whatever didn't fit, never for a single node.
            shown, hidden = fan[: capacity - 1], fan[capacity - 1 :]
        else:
            shown, hidden = fan, []
        slots = len(shown) + (1 if hidden else 0)
        fx, fy = self._focus_pos(width, canvas_h, slots)
        ax, ay, focus_name = self._focus_anchor(width, canvas_h, slots)
        stand_in = selected if selected in hidden else None
        # The … slot is laid out for the label it wears at rest. A stand-in's name is not
        # allowed to move it: the geometry must not depend on the selection, or the whole
        # fan would shuffle as the cursor walked the collapsed nodes. A long name that
        # doesn't fit the slot simply truncates (JP, 2026-08-09).
        placed = self._place_neighbours(
            width, canvas_h, shown, bool(hidden), more_label=f"+{len(hidden)} weaker"
        )

        # Edges first (markers and labels overprint them), coloured by SNR and faded by
        # evidence age. Every edge leaves the focus's name-end anchor and lands on its
        # neighbour's marker. The selected link's edge draws brightest and on top, shedding
        # the age fade so it reads as one lit thread over the dimmer rest (the age is still
        # legible in the row's age column). The collapsed marker's edge is slate — unless
        # the selection hides in it, when it becomes that node's own edge.
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

        # The focus marker at the far west, its name to the RIGHT — icon left, name right,
        # the walk's reading way. The edges have already left the name-end, so they run east
        # clear of the label rather than through it.
        glyph, _colour = self._glyph(self._focus)
        canvas.marker(fx, fy, glyph, self._marker_rgb(self._focus))
        self._label_right(canvas, fx, fy, focus_name, self._label_rgb(self._focus))

        # Markers first, then labels. Every marker wears its node type's colour — the
        # selection is shown by its white *label* and the lit route, never by recolouring
        # a mark that means something else. Labels are laid most-important-first (the
        # selection, then the strongest links) so the collision check drops the least
        # important where two would overprint.
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
                # The marker is that node now: its own mark, and its name in the selection
                # white — a collapsed node is *only ever drawn while selected*, so white is
                # what it is here, exactly as it would be if it had a marker of its own. The
                # rank among the collapsed set sits *west* of the mark (JP, 2026-08-09)
                # rather than trailing the name, so the name still ends where every other
                # name on the fan ends and the count reads as an annotation on the marker
                # instead of part of what the node is called. That count is what keeps the
                # stand-in honest: a lone name would claim the fan has one more member than
                # it drew.
                self._label_left(canvas, x, y, self._more_rank(stand_in, hidden), grey)
                self._label_right(canvas, x, y, self._label(stand_in), white)
            else:
                self._label_right(canvas, x, y, f"+{len(hidden)} weaker", grey)

        return canvas.to_ansi_lines()

    @staticmethod
    def _more_rank(node: str, hidden: list[str]) -> str:
        """``(2/17)`` — where ``node`` sits among the collapsed links, and how many there are."""
        return f"({hidden.index(node) + 1}/{len(hidden)})"

    def _fan_nodes(self) -> list[str]:
        """The focus's ways *onward*: every neighbour but the one the walk came from.

        The single definition of what the east of this screen is about, shared by the
        canvas and the link list so the picture and the rows can never disagree about what
        is on offer — including under a find, which narrows both. The came-from node is
        excluded (JP, 2026-08-09) — it is where you have been, ⌫ is how you return to it,
        and listing it as a link both invited a walk that just undoes the last one and cost
        the list its top row: with it gone, the strongest link is always the row the cursor
        opens on.
        """
        back = self._came_from
        return [
            other
            for other, _link in self._links_of(self._focus)
            if other != back and self._is_match(other)
        ]

    def _fan_capacity(self, canvas_h: int) -> int:
        """How many marker rows the canvas carries — the ``…`` marker's own among them.

        The fan is a **row grid, not an arc's spread** (JP, 2026-08-09): one marker per
        row with a blank row between each, and a blank row above and below the block —
        ``2n + 1`` rows for ``n`` markers. Even spacing is what makes a fan of five read as
        five rather than as a clump at the rim, which is what an evenly-spaced *angle*
        sweep produced (its rows fell out of a sine, so they crowded top and bottom).

        The outer blank rows are the part that gives: a canvas too short to reach
        :data:`_FAN_MIN_SLOTS` neighbours with them drops them and fits ``n`` in ``2n - 1``
        instead. Air is worth a row until it costs a *node* — seeing the neighbourhood is
        what the screen is for.
        """
        padded = (canvas_h - 1) // 2
        if padded >= _FAN_MIN_SLOTS:
            return max(3, padded)
        return max(3, (canvas_h + 1) // 2)

    def _fan_rows(self, canvas_h: int, slots: int) -> list[int]:
        """The canvas rows the fan's ``slots`` markers sit on, top to bottom.

        Centred as one block, which is what hands back the outer blank row whenever the
        canvas is taller than the block needs — and drops it, evenly, when it isn't (see
        :meth:`_fan_capacity`).
        """
        if slots <= 0:
            return []
        span = 2 * slots - 1
        top = max(0, (canvas_h - span) // 2)
        return [min(canvas_h - 1, top + 2 * i) for i in range(slots)]

    def _focus_pos(self, width: int, canvas_h: int, slots: int = 0) -> tuple[int, int]:
        """The focus marker's dot position: near the west edge, name and fan to its east.

        The icon hugs the left so its name (drawn to the right) and the neighbour fan get
        the whole width to spread across — the graph aired out rather than squeezed into
        the middle.

        Vertically it sits level with the **fan's** middle rather than the canvas's: the
        block of ``slots`` markers is centred, and the focus is centred on *it*, so the
        edges leaving it fan out symmetrically however the block's parity falls. With no
        fan to be level with (``slots`` 0 — a leaf node, or a find matching nothing here)
        it takes the canvas midline.
        """
        x = max(4, (width * 2) // 12)
        rows = self._fan_rows(canvas_h, slots)
        row = (rows[0] + rows[-1]) / 2 if rows else (canvas_h - 1) / 2
        return x, round(row * 4 + 2)

    def _focus_anchor(self, width: int, canvas_h: int, slots: int = 0) -> tuple[int, int, str]:
        """The focus's edge-attach point and its on-canvas name.

        The focus icon sits at the far west (:meth:`_focus_pos`) with its name to the
        *right*; every edge leaves the **right end of that name** so the connecting lines
        never cross the label. Returns the attach point in dot coordinates and the name as
        it is drawn (clipped to the room between the icon and the canvas edge).
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
        """Dot-space positions for the drawn fan: a row grid east of the focus's name.

        Everyone shown takes a row of their own east of the focus's name-end anchor,
        strongest link at the top and weakest at the bottom — the same order as the list
        below, so the picture and the rows correspond — with the collapsed ``…`` marker
        (keyed :data:`_MORE`) taking the fan's last slot. The rows come from
        :meth:`_fan_rows`: evenly spaced, one blank row between each, the block centred.

        The easting is an arc, reined in **per marker** by the name that marker has to
        spell (JP, 2026-08-09). The fan reaches for a tip sized to an ordinary name
        (:data:`_FAN_BASE_LABEL_W`), and then each marker whose own name wants more room
        than that steps west by exactly the difference — so a single long name costs *its*
        row some reach and leaves the rest of the fan where it was. Sizing the whole fan to
        the longest name (what this did first) meant one 29-cell repeater pulled every
        marker west with it; not sizing at all (what it did before that) meant a fixed
        20-cell margin ellipsized anything longer on a canvas with room to spare.

        A marker never comes further west than :data:`_MIN_FAN_REACH` of the width,
        whatever its name wants: past that the markers pile onto the focus and the graph
        stops reading as a fan at all, so a name too long for what's left is truncated by
        the label placer instead.

        Only the *row* of a leaf sets its angle; its east reach bows by
        :data:`_FAN_X_FLATTEN` of that, so the fan curves gently rather than standing as a
        flat column, while the rim leaves still reach well east instead of curling back
        toward the focus on a true circle. A small fan uses proportionally less of the arc,
        so two neighbours sit near due east rather than at opposite rims. Anchoring at the
        name-end (rather than at the icon) hands the fan the whole width east of the focus
        label to breathe in.

        Args:
            width: The canvas width in cells.
            canvas_h: The canvas height in cells.
            shown: The neighbours drawn with markers of their own, strongest first.
            more: Whether a collapsed ``…`` marker takes the last slot.
            more_label: What that marker is labelled at rest, so its slot is laid out for
                the label it *usually* wears rather than for a passing selection.
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
        # The marker's own cell, a blank, then the label: that is what has to sit east of a
        # marker, in cells, doubled into dot space.
        rx = max(floor, 2 * (width - 2 - _FAN_BASE_LABEL_W)) - ax
        phi = _FAN_HALF_ANGLE * min(1.0, (len(keys) - 1) / 5.0)
        for i, (node, row, label) in enumerate(zip(keys, rows, labels, strict=True)):
            angle = 0.0 if len(keys) == 1 else -phi + (2 * phi) * i / (len(keys) - 1)
            x = ax + rx * math.cos(angle * _FAN_X_FLATTEN)
            room = 2 * (width - 2 - min(cell_len(label), _FAN_LABEL_W))
            placed[node] = (
                round(max(floor, min(x, room))),
                row * 4 + 2,  # mid-cell, so an edge meets the glyph
            )
        return placed

    @staticmethod
    def _clip(label: str, room: int, cap: int = _LABEL_W) -> str:
        """``label`` fit to ``room`` cells.

        Whole if it fits, else ellipsized (``…`` alone at one cell, nothing at zero).
        The cap and the room-to-edge both flow through here, so a name is only ever
        shortened as far as it truly must be.
        """
        room = min(room, cap)
        if room <= 0:
            return ""
        if len(label) <= room:
            return label
        return "…" if room == 1 else label[: room - 1] + "…"

    def _place_label(self, canvas: MapCanvas, x: int, y: int, label: str, rgb: RGB) -> None:
        """Place one marker label, retrying a row below then above on collision.

        It goes to the right of the marker where that fits, else to the left.

        The label is clamped to whichever side has more room — the cells free to the
        right of the marker, or to its left — so the selection keeps as much of its name
        as the canvas allows before ellipsizing.
        """
        cx = x >> 1
        room = max(canvas.cell_w - (cx + 2), cx - 1)  # the roomier of right / left
        label = self._clip(label, room)
        if not label:
            return
        for dy in (0, 4, -4):
            if canvas.marker_label(x, y + dy, label, rgb):
                return

    def _label_right(self, canvas: MapCanvas, x: int, y: int, label: str, rgb: RGB) -> None:
        """Place a node's label to the *right* of its marker, dodging by row.

        Every node but the selected one (which keeps the two-sided
        :meth:`_place_label`) is named to the right of its icon — the reading
        direction of the walk. The label tries the marker's own row first, then a
        row below and above to slip past a crowded neighbour; if every checked row
        is blocked it is stamped to the right regardless, so a node is never left a
        bare glyph. The name is clamped to the cells actually free between the marker
        and the canvas edge — which :meth:`_place_neighbours` has already sized the fan
        to keep, so on any canvas wide enough the clamp never bites.
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
        """Place a short annotation to the *left* of a marker, on the marker's own row.

        The one thing drawn on that side (the collapsed marker's rank — see
        :meth:`_canvas_lines`), and it stays on the row it annotates rather than dodging:
        a counter that slipped a row would read as belonging to the neighbour above. West
        of a fan marker is the marker's own incoming edge and little else, so it is placed
        unconditionally and simply overprints the braille it lands on — the same fallback
        :meth:`_label_right` ends on. It carries its own trailing blank into the run so the
        gap before the mark is a real gap: left unwritten, the incoming edge's braille
        threads straight through it and glues the counter to the glyph.
        """
        cx, cy = x >> 1, y >> 2
        label = self._clip(label, cx - 1, cap=_FAN_LABEL_W)
        if not label:
            return
        canvas._place_run(cx - 1 - cell_len(label), cy, label + " ", rgb, bold=True)

    def _legend(self) -> Text:
        """The one-line glyph legend and edge key under the canvas.

        A key to both halves of a mark, now that the canvas draws marks whole: each role's
        glyph in the very colour it wears up there, so the line is a sample rather than a
        shape chart. The pairs come from the same places :meth:`_glyph` draws them from, so
        the key cannot drift from the picture — including on the console, where the
        ``type.*`` names are what let each mark pick its slot deliberately rather than
        landing wherever a downsample of the hex drops it. The words stay muted: they are
        chrome, the marks are the content.
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
        """The selectable rows, windowed to ``win`` lines under a pinned heading.

        Only the rows scroll — the heading (and everything above it) holds still.
        When the list outgrows the window, faint ``↑/↓ n more`` markers take the
        window's edge rows and the highlight is kept inside what remains; the
        window's row count becomes the PgUp/PgDn stride.
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
            # Rows are the authority on *which* links this list holds and in what order
            # (:meth:`_rows`); the link table is only looked up *through* them. Rendering
            # straight out of the table was the bug: it still carried the node the walk came
            # from, so every index past it named one row and drew another, and the highlight
            # sat one link away from the one the canvas was lighting. Onward counts are
            # still gathered over the whole table — that memo is per focus, not per paint,
            # and a narrowed view must not be what fills it.
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
        """The list's ``(name, key)`` lane widths — the name lane first, the key filling in.

        The name lane sizes to the widest name the rows carry (a short-name list stays
        tight); the key lane takes whatever's left, capped at the whole 12-hex id. When the
        two together outrun the row, the *key* gives ground first — shrinking on a byte
        boundary down to a floor that still shows its whole lit hash plus a ``…`` — and only
        once the key is at that floor does a long name start losing letters. So the name is
        the last thing truncated and the hash never is (the user reads names, and addresses
        by hash).
        """
        hash_w = max(2, min(_LIST_HASH_W, self._prefix_bytes * 2))
        # The tightest key lane that still shows the whole hash: the hash plus a "…". An odd
        # width, so :func:`highlighted_hash` keeps an even hash_w digits with no wasted cell;
        # when the hash already fills the id there's nothing to drop, so the floor is the
        # full width.
        key_floor = _LIST_HASH_W if hash_w >= _LIST_HASH_W else hash_w + 1
        avail = width - fixed
        widest = max((cell_len(self._label(n)) for n in nodes), default=_LIST_NAME_MIN)
        name_w = max(_LIST_NAME_MIN, min(widest, avail - key_floor))
        key_w = max(key_floor, min(_LIST_HASH_W, avail - name_w))
        return name_w, key_w

    def _onward_counts(self, pairs: list[tuple[str, Link]]) -> dict[str, int]:
        """How many links continue from each neighbour, the one back here excluded.

        Memoized per focus (the counts depend only on the frozen graph and whose
        neighbours are being listed) — this was quadratic per repaint: every
        neighbour re-walked a fresh copy of the whole link table.
        """
        focus = self._focus
        memoized = self._onward_memo.get(focus)
        if memoized is not None:
            return memoized
        counts: dict[str, int] = {}
        for other, _link in pairs:
            # A link off the neighbour "continues onward" unless its far end is here:
            # one endpoint is the neighbour itself, so only the far endpoint can be us.
            counts[other] = sum(1 for far, _l in self._links_of(other) if far != focus)
        self._onward_memo[focus] = counts
        return counts

    def _link_row(
        self, other: str, link: Link, selected: bool, onward: int, name_w: int, key_w: int
    ) -> Text:
        """One neighbour row: glyph, name, hash, SNR + bar, evidence, onward count."""
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
        """One find match: glyph, name, hash, and how far out it sits."""
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
        """The list's name colour: the app-wide palette hue (hash-derived), us in pure white."""
        if node == self._topo.self_id:
            return "you"
        label = self._label(node)
        if label == node[:8]:  # a bare hash is not a name — colour is the name signal
            return "node.unknown"
        return name_style(label, node)

    def _known(self, node: str) -> bool:
        """Whether the node is identified — named, or us — so its hash may carry a hue.

        The gate every hash lane here passes to :func:`~meshterm.ui.widgets.highlighted_hash`:
        an unknown node's hash reads grey whole, matching its ``node.unknown`` name lane
        and its ``○`` ring on the canvas.
        """
        return self._list_name_style(node) != "node.unknown"

    def _marker_rgb(self, node: str) -> RGB:
        """The marker colour for a node: the hue its *type* wears everywhere (JP, 2026-08-09).

        The walk used to colour markers by identity — the key-derived per-node hue — with
        the glyph shape left to carry the type alone. That put this one canvas at odds with
        every other place a typed node is pinned (the map, the route graph, a contact row),
        where the mark's colour *is* the type and is chosen deliberately per platform
        through the theme's ``type.*`` (a naive downsample greys the repeater's violet).
        Identity has its own lane here and always did: the **name** beside the marker, in
        the key hue (:meth:`_label_rgb`). Shape and colour now agree, and the legend under
        the canvas can key both.
        """
        return mark_rgb(self._glyph(node)[1])

    def _label_rgb(self, node: str) -> RGB:
        """The label colour for a node on the canvas: its name hue, ours white, a bare hash grey.

        The RGB counterpart of :meth:`_list_name_style`: a named node's key hue, our own
        node white, and a node known only by a bare hash the unknown-node grey (a key
        standing in as a name is never itself coloured, so the marker carries the identity
        and the hash-label stays grey).
        """
        style = self._list_name_style(node)
        if style == "you":
            return (255, 255, 255)
        if "#" not in style:  # a theme name (node.unknown) — the platform picks the shade
            return mark_rgb(style)
        return parse_hex(style.rsplit("#", 1)[-1])

    def _glyph_style(self, node: str) -> str:
        """The Rich style a node's type glyph takes in body text — its type colour.

        The text-side companion of :meth:`_marker_rgb`: the same ``type.*`` entry (or the
        star's own yellow), so the focus line's leading glyph, the list rows' glyphs and
        the canvas markers are one mark drawn three ways.
        """
        return self._glyph(node)[1]

    def _empty_state(self, width: int) -> list[str]:
        """A friendly explanation while the evidence graph is still empty."""
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
        """A node's mark: the type glyph and the colour that type wears app-wide.

        The shared marks (:data:`~meshterm.ui.widgets.NODE_GLYPHS`) and the shared
        ``type.*`` colours behind them, so a node is pinned here in exactly the glyph and
        hue the map, the route graph and the contact list pin it in — ``▲`` violet for a
        repeater, ``■`` for a room, ``◉`` for a sensor, ``●`` pink for a plain node, our
        own the yellow ``★``, and a node we have heard of but never identified the grey
        ``○``. Colour and shape say the same thing, which is what makes the legend below
        the canvas a key rather than a coincidence.

        Returns:
            ``(glyph, colour)`` — the colour in whichever encoding
            :func:`~meshterm.ui.theme.mark_rgb` takes (a literal hex, or a theme name where
            the platform must pick its own slot).
        """
        if node == self._topo.self_id:
            return SELF_MARK
        contact = self._contacts.get(node)
        if contact is None:
            return UNKNOWN_MARK
        return NODE_GLYPHS.get(contact.node_type, DEFAULT_GLYPH)

    def _label(self, node: str) -> str:
        """A node's display name: its own name for us, contact name, or short hash."""
        if node == self._topo.self_id:
            return self._self_label
        return self._topo.display_name(node) or node[:8]


async def open_walk(ctx: AppContext) -> None:
    """Build the evidence graph and run the full-screen mesh walk until dismissed.

    Contacts and our own identity come from the device when one is reachable
    (best-effort — the stored evidence draws fine without them, just with hashes for
    names); the graph itself comes entirely from the repository, snapshotted once when
    the screen opens. No transmissions, ever.

    Args:
        ctx: The shared application context (must be running the interactive TUI).

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from ..services.topology import build_topology
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
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
    except Exception:  # noqa: BLE001 - names are a nicety; the graph renders without them
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
        # The same clean-slate repaint as the map: braille fallback glyphs may have
        # smeared cells prompt_toolkit's differential paint will never rewrite.
        session.request_full_repaint()
