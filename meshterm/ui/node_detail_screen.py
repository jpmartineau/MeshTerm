# SPDX-License-Identifier: Apache-2.0
"""The Node detail screen: one node's whole story on a single page, and the ways into it.

Reached by pressing Enter on any contact in the Contacts list (see
:mod:`~meshterm.ui.contacts_screen`). Where the list is one aligned row per node, this is
the node itself, in full:

* **who it is** — a one-line identity header (its type glyph, its name in the node's own
  hue, its type label), the only chrome pinned above the stage — so the stage keeps nearly
  the whole screen;
* **a tabbed stage** below it — one full-height view at a time, switched with
  ``Tab``/``Shift+Tab`` across a boxed tab strip (see
  :func:`~meshterm.ui.widgets.tab_strip`). Rather than stack the vitals, the location
  preview, and the route graph down one long scroll, each view earns the whole stage:

  * **Info** — the node's vitals as labelled rows (its key with the routing hash lit, when
    it was first and last heard, how many packets we've overheard, its reception SNR and
    last RSSI, where it sits, and — for a repeater — the regions it was last heard to relay
    floods for, whether it relays unscoped floods too, and when it said so; see
    :func:`regions_value`). The key is a single lane, never wrapped: a full public
    key outruns the row on any terminal we target, so ``←→`` scroll it a whole four bytes
    at a time under faint ``…`` edge marks, exactly as a long pathline scrolls on the
    Routes tab. Then — when the node has advertised a location — a
    static basemap preview (see :class:`~meshterm.ui.minimap.MiniMap`) centred on the node,
    grown to whatever rows the viewport spares. Its actions: ``Open full map`` (the full
    map opens centred here with its find filter seeded to this node, so it lights among
    the rest), ``Time machine``, ``Share contact`` — a popup contact card (QR code +
    ``meshcore://`` link, see :func:`~meshterm.ui.config_editor.show_contact_card`),
    offered whenever the node's full key is known — ``Ask which regions it carries`` on a
    repeater (one anonymous request, answered only when it arrives direct; the answer is
    learned into the region store and the row redraws in place) — and, last,
    ``Remove contact``: the
    single-contact counterpart to the Contacts list's bulk archive (see
    :mod:`~meshterm.ui.contacts_screen`), dropping *this* node from the device's contact
    table behind a red confirm. It is the one thing on the page that changes anything, so
    it sits at the foot of the actions, and committing it closes the page — the contact it
    details no longer exists to detail.
  * **Routes** — the routes we've actually heard the node arrive over, drawn on the shared
    route graph (:mod:`~meshterm.ui.pathgraph`) node→us (the inbound direction the packets
    travelled, contact on the left, us on the right), each relay tagged by its first hash
    byte alone — two cells, so the fan reads however many lanes it carries. Beneath the
    graph sits the *route list*, where the names live: one selectable row per distinct route
    (the firmware's learned route and the observed alternatives, the strongest marked
    ``★ best``), each spelled out through THE path widget as a single **named** line — a hop
    nobody can name standing in its hash — with its bottleneck SNR / sample count / tag
    hanging on the line below. The two bands cross-reference by node hue rather than by
    spelling the same hex twice; it is the Message paths dialog's split exactly.
    ``↑↓`` moves the selection; the picked route lights white in the graph
    while the rest go grey and every node not on it fades its label to grey, so the graph
    reads as *this* route through the fan. A pathline too wide for the lane never wraps —
    the highlighted row instead scrolls horizontally (``←→``) under faint ``…`` edge marks
    to read it to the end; an unselected long row just ellipsizes. Only *good* routes are
    drawn — stale evidence and
    far-weaker outliers are dropped, so the list is the routes worth trusting rather than
    every chain ever heard. Enter on a route arms a trace on it (nothing transmits here — it
    opens the trace screen loaded with that path); when there is no route evidence to list, a
    ``🎯 Trace — auto route …`` action stands in.

* **the ways in** — each tab's action rows (``↑↓`` moves the cursor through them, Enter
  commits) and the shared ``Back`` closing every tab; Esc backs to the list.

The page never scrolls as one long strip. The identity header, the tab strip, and the
stage are pinned; the stage sizes itself to the terminal (the route graph compresses its
lanes before it would overflow, the location preview grows into what the Info tab spares)
and a faint rule closes it, so the drawn view and the rows below read as separate bands.
The route list scrolls *inside* the leftover rows with faint ``↑ n more`` / ``↓ n more``
edge markers — the app-wide windowed-list pattern (see
:class:`~meshterm.ui.tui.screen.ListWindow`, fitting whole blocks) — so the graph, the
selection driving it, and the action rows share one screen however many routes a busy
node has. ``PgUp/PgDn`` page the cursor through the window.

The screen is a pure read-and-route view: it renders already-resolved display data and
resolves an action token (the Trace token carrying the selected route's spec via
:meth:`NodeDetailScreen.selected_spec`); :func:`open_node_detail` owns the data-gathering
and runs the sub-flows each action opens, then re-shows the page — the same loop the Time
Machine and Contacts list use. The screen itself never transmits; the one action that does
is the regions question, asked once, by the opener, when the reader commits its row.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from itertools import pairwise
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.text import Text

from ..core.connection import ContactNotOnDeviceError, DeviceCommandError
from ..core.geo import haversine_km, usable_fix
from ..core.models import NODE_TYPE_LABELS, NODE_TYPE_REPEATER, Contact, utcnow
from ..platforms import Platform, on_platform
from . import menus
from .mapcanvas import RGB
from .marks import SELF_MARK
from .minimap import MiniMap
from .pathgraph import (
    DST_NODE,
    SRC_NODE,
    GlyphOf,
    LabelOf,
    LabelRgbOf,
    PathLayer,
    bidir_clusters,
    render_path_graph,
)
from .pathline import (
    ELIDE_HEAD,
    ELIDE_TAIL,
    SELF_GLYPH,
    PathHop,
    PathLine,
    cut_mark,
    cut_to,
    hops_atom,
    with_action_mark,
)
from .theme import mark_rgb, name_style, snr_style
from .tui.render import crop_cells, render_hanging, render_lines, render_to_ansi
from .tui.screen import CANCEL, ListWindow, Screen
from .widgets import (
    DEFAULT_GLYPH,
    NODE_GLYPHS,
    _recency_style,
    age_seconds,
    format_ago,
    highlighted_hash,
    node_type_legend,
    tab_air,
    tab_strip,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import AppContext

#: Whether the route fan is captioned ("node → you, as heard  ·  white = selected route")
#: and allowed to keep the empty canvas row its top air leaves. Both go on the PicoCalc
#: (JP, 2026-08-09) and both rows go to the graph instead: the caption explains a picture
#: the white line and the ``you`` star already make, and the renderer's end padding
#: (:data:`~meshterm.ui.pathgraph._GRAPH_END_DOTS`, two cells) reserves a whole cell of air
#: above the topmost lane's label that nothing ever draws into. Rides the same frugality
#: signal as the tab strip's air (:func:`~meshterm.ui.widgets.tab_air`), bound at
#: platform-switch time like every platform constant.
_GRAPH_CAPTION = True


@on_platform
def _bind_graph_caption(platform: Platform) -> None:
    """Bind the fan's caption/air to the platform (runs now and on every switch)."""
    global _GRAPH_CAPTION
    _GRAPH_CAPTION = platform.frame_border


#: An SGR escape, so a rendered canvas row can be tested for holding nothing but air.
_SGR = re.compile(r"\x1b\[[0-9;]*m")

#: The inline location preview's row bounds: it grows into whatever the Info tab's
#: viewport spares (the vitals and action rows are short), floored so a cramped terminal
#: still shows a recognisable neighbourhood and capped so a tall one doesn't become all map.
_MAP_MIN_ROWS = 5
_MAP_MAX_ROWS = 13

#: Route-graph tuning for the Routes tab. It draws the contact on the left and us on the
#: right (node → us, so the graph reads left to right as the inbound direction its packets
#: travelled to reach us). A well-connected node offers several candidate paths at once, and
#: cramming them into a short box compresses the lanes together — so this page keeps the shared
#: lane pitch and lets the graph grow with its lanes, up to this ceiling. The real budget is
#: struck per render against the terminal: the stage takes what the viewport leaves after the
#: pinned chrome, the action rows, and the route list's guaranteed window — so the graph
#: compresses its pitch before it would ever push the list or the actions off the screen, and
#: this ceiling is only reached on a terminal tall enough to afford it.
_PATH_MAX_ROWS = 22

#: The fewest canvas rows the route graph is ever granted (the shared renderer's own floor);
#: on a terminal too short for even this, the frame's cursor-follow keeps the active row in
#: view rather than the stage shrinking into noise.
_GRAPH_MIN_ROWS = 5

#: Route-list lines the stage must leave room for before taking the rest of the viewport —
#: enough for a couple of routes (or one wrapped one) to show beside the graph they light.
_LIST_MIN_LINES = 4

#: A drawn route is dropped as *stale* when its freshest-limiting link — the stalest hop it
#: rides through — has not been heard in this many days. A route is only as current as its
#: weakest-heard link: if any hop along it has gone quiet, the whole chain may no longer
#: carry. Matches the spirit of the topology's one-week evidence half-life (four half-lives
#: leaves a link weighted ~1/16), the point past which a path is more memory than fact.
_PATH_STALE_DAYS = 28.0

#: A drawn alternative route is dropped as an *outlier* when its evidence score falls below
#: this fraction of the strongest observed alternative's. Keeps the graph to the handful of
#: routes actually worth trusting rather than every far-weaker chain the evidence can string
#: together. The best-evidence (white) route is always drawn regardless — it is the answer to
#: "how do we reach it", not one of the alternatives being weighed.
_PATH_OUTLIER_RATIO = 0.25

#: Cells the labelled info rows reserve for their label lane, so the value blocks line up
#: and a wrapped value hangs under itself rather than under the label (the app-wide
#: hanging-indent rule). Sized to the widest label the block uses ("packets").
_LABEL_LANE = 9

#: The selected route's edge colour (white, the spine) over the alternatives' grey, and the
#: grey a node's label fades to when it sits on no part of the selected route. One grey for
#: both, so an off-route relay's line and its name read as the same "not this route" dim.
_WHITE = (255, 255, 255)
_GREY = (120, 120, 120)

#: The synthetic node-id prefix a contracted bidirectional cluster draws under. A ``\x00``
#: lead keeps it non-hex (so the graph never tries to coalesce it against a hash) and clear of
#: any real hop, mirroring the graph's own endpoint sentinels.
_CLUSTER_NODE = "\x00clu"

#: Columns a route row's pathline and its hanging context line indent by — the ``"❯ "``/
#: ``"  "`` pointer's own width, so the context lines up under the path it belongs to.
_ROUTE_INDENT = 2

#: Cells one ←/→ press horizontally scrolls the highlighted route's pathline, or the Info
#: tab's key lane (see :meth:`NodeDetailScreen.handle`) — the same step the app-wide select
#: list's ``hscroll`` uses. On a key it happens to be exactly four bytes, so every window the
#: lane can settle on starts on a byte boundary, the way a hash reads.
_HSCROLL_STEP = 8

#: The info row whose value is a key: the one lane that scrolls (``←→``) rather than wraps.
#: A public key is 64 hex digits — wider than the value lane on either platform, and a run
#: with no word to break on — so wrapping it just buys a second line that says nothing,
#: while every other vital here is prose short enough to hang under itself.
_KEY_LABEL = "key"

#: The faint mark drawn at whichever edge of the key lane the key continues past.
_MORE_MARK = "…"

#: Cursor moves that abandon a route's in-progress horizontal scroll — each row scrolls on
#: its own, so leaving it resets the shift rather than carrying it to whatever row is next.
_HSHIFT_RESET_ACTIONS = frozenset(
    {
        "up",
        "down",
        "pageup",
        "pagedown",
        "home",
        "ctrl_home",
        "end",
        "ctrl_end",
    }
)


@dataclass(slots=True)
class _Action:
    """One action row at the foot of a tab.

    Attributes:
        key: The token the screen resolves with when this row is committed.
        glyph: The leading icon.
        glyph_style: The icon's style.
        label: The row's text (a current value inlined, muted where it's context).
    """

    key: str
    glyph: str
    glyph_style: str
    label: str


@dataclass(slots=True)
class _Route:
    """One selectable route on the Routes tab: how it draws, how it traces, how it reads.

    Attributes:
        draw: The relay hops in the graph's inbound draw order (contact → us), so a
            :class:`~meshterm.ui.pathgraph.PathLayer` built from them lands the contact on
            the left endpoint and our star on the right. Empty = a straight zero-hop shot.
        spec: The forced-path spec a trace arms on when this route is selected (the symmetric
            round trip through these hops); ``""`` lets the trace screen auto-resolve.
        path: The pre-rendered pathline — the whole route named through THE path widget,
            contact → relays → us — with the trailing opens-further-prompts mark applied at
            render time (:data:`_OPENS_MARKER`), not baked in here. One line, never wrapped:
            the highlighted row instead horizontally scrolls to read a long hash chain past
            the lane's width, and an unselected row simply ellipsizes.
        context: The line shown hanging under ``path`` — the bottleneck SNR, sample count,
            and a ``★ best`` / ``device route`` tag — empty when a route earns none of them.
    """

    draw: tuple[str, ...]
    spec: str
    path: Text
    context: Text


@dataclass(slots=True)
class _Cluster:
    """A contracted bidirectional cluster's stand-in marker on the graph.

    Three or more repeaters that relay each other in every order are a knot the left-to-right
    flow can't seat (see :func:`~meshterm.ui.pathgraph.bidir_clusters`); they draw as one
    super-node instead, and this is how it presents. The route rows below the graph still name
    every member in order, so the detail the marker folds away is one glance down.

    Attributes:
        glyph: The marker glyph — the members' shared node-type mark when they agree
            (``▲`` repeater, ``■`` room, …), else the plain node dot.
        color: The marker glyph's ``#rrggbb`` colour (the node-type hue, or the plain dot's).
        label: The count-and-type label, e.g. ``3 repeaters`` (``n nodes`` when mixed).
        rgb: The label colour — the same type hue as the marker, so the super-node reads as a
            typed group rather than a named node.
    """

    glyph: str
    color: str
    label: str
    rgb: tuple[int, int, int]


@dataclass(slots=True)
class _RoutesView:
    """The Routes tab's selectable routes and the shared per-node draw callbacks, or a note.

    The routes are drawn as one fan (best-evidence spine plus alternatives); which one lights
    white — and which nodes keep their name hue rather than fading to grey — follows the
    screen's live selection, so the layers and label colours are composed per render rather
    than baked in here.

    Attributes:
        routes: The selectable routes (strongest first), or empty when there is no route
            evidence at all — then ``note`` carries the muted stand-in.
        glyph_of: Per-node marker callback for the graph.
        label_of: Per-node label callback for the graph.
        label_rgb_of: Per-node label-colour callback (the un-muted base; the screen fades the
            off-route nodes over it).
        legend: Whether to draw the node-type key beneath the graph (a typed relay showed).
        note: The muted line shown instead of a graph when there is no evidence.
    """

    routes: list[_Route] = field(default_factory=list)
    glyph_of: GlyphOf | None = None
    label_of: LabelOf | None = None
    label_rgb_of: LabelRgbOf | None = None
    legend: bool = False
    note: str = ""


@dataclass(slots=True)
class _Tab:
    """One tab in the stage's strip.

    Attributes:
        name: The strip label (``Info`` / ``Routes``).
        kind: Which stage it draws (``info`` / ``routes``).
    """

    name: str
    kind: str


class NodeDetailScreen(Screen):
    """A full-screen page for one node: identity, a tabbed stage, and the ways in.

    A read-and-route view. It renders already-resolved display data (see
    :func:`open_node_detail`, which assembles it) and, on Enter, resolves the highlighted
    action's token for the opener to act on (a Trace token is paired with
    :meth:`selected_spec`); Esc resolves :data:`CANCEL` to leave.

    Two axes of navigation, matching the app's spatial feel: ``Tab``/``Shift+Tab`` switches
    which view fills the stage, and ``↑↓`` moves the cursor *within* the active tab — through
    its route list (on the Routes tab, the selection drives the graph highlight and Enter arms
    a trace on the picked route) and action rows. The page
    itself never scrolls: the identity header, tab strip, and stage are pinned, the stage
    is sized to the viewport, and the route list windows itself into the leftover rows —
    ``PgUp/PgDn`` page the cursor through it, ``Home/End`` jump it to the ends.

    ``←→`` read whatever the active tab holds that is wider than its lane, one lane per tab:
    the highlighted route's pathline on Routes, the key on Info. Both are gated on actually
    overflowing, so the keys stay inert — and unadvertised — where they would do nothing.
    """

    floating = False

    @property
    def fkey_lane(self):
        """The shared pager over the route list, plus the tab switch on F3.

        The strip shows *that* there are two views; nothing on screen says the key that
        moves between them, and on a platform with no hint line the chip is the only place
        to learn it. It names the tab it would take you *to*, never the one you are on —
        the strip already marks that, and a chip repeating it would say nothing about what
        pressing it does. (A per-tab chip on F1/F2 was tried and reverted — JP, 2026-08-09:
        the toggle was better.) Unlike the Time Machine's window chip, this one carries no
        ``▸`` lead-in: ``Routes`` is exactly six cells on its own, and the strip beside it
        makes the direction plain anyway. A page with one tab has nothing to switch, so
        the slot stays empty.

        The pager gates on the route list actually being windowed: on the Info tab, and on
        a node whose routes all fit, the keys move nothing.
        """
        from .tui.fkeys import FPair, default_lane

        lane = list(default_lane(nav=self._list_hidden))
        if len(self._tabs) >= 2:
            nxt = self._tabs[(self._tab_index + 1) % len(self._tabs)]
            lane[2] = FPair(nxt.name, "tab")
        return lane

    def __init__(
        self,
        *,
        title: str,
        header: Text,
        info_rows: list[tuple[str, Text]],
        tabs: list[_Tab],
        minimap: MiniMap | None = None,
        map_caption: Text | None = None,
        routes: _RoutesView | None = None,
        info_actions: list[_Action] | None = None,
        trace_action: _Action | None = None,
    ) -> None:
        """Build the page over resolved display data.

        Args:
            title: The screen heading (``Node — <name>``).
            header: The identity line: type glyph, the coloured name, its type label.
            info_rows: ``(label, value)`` pairs for the Info tab's vitals block; each
                renders as a muted label lane with the value hanging under itself when it
                wraps.
            tabs: The stage tabs to offer, in strip order (empty = no stage, just actions).
            minimap: The Info tab's inline location preview, or ``None`` (no advertised
                fix).
            map_caption: A faint line under the preview (its centre/scale), when a map shows.
            routes: The Routes tab's route list + graph callbacks (or a muted note), or
                ``None`` when there is no Routes tab (we never overhear our own node).
            info_actions: The Info tab's action rows (Open full map when there is a fix,
                Time machine when the recorder holds history, Share contact when the full
                key is known).
            trace_action: The Routes tab's ``Trace — auto route …`` action, shown only when
                there are no routes to list (with routes listed, Enter on a route row is the
                trace entry point), or ``None``.
        """
        super().__init__()
        self.title = title
        self._header = header
        self._info_rows = info_rows
        self._tabs = tabs
        self._minimap = minimap
        self._map_caption = map_caption
        self._routes = routes
        self._info_actions = info_actions or []
        self._trace_action = trace_action
        #: The action rows' icon column, in cells — measured once over every mark this page
        #: can draw, so a one-cell ``🗑`` pads out to its two-cell siblings and every label
        #: starts in the same column. Zero on a platform that draws no icon lane at all.
        self._icon_lane = self._measure_icon_lane()
        self._tab_index = 0
        self._row_index = 0
        #: The highlighted route on the Routes tab (drives the graph); tracks the cursor as
        #: it moves onto a route row and holds while it rests on an action row, so the graph
        #: keeps showing the last pick.
        self._route_sel = 0
        #: The composed route fan for one (width, rows, selection) — see _routes_stage.
        self._stage_memo: tuple[tuple, list[str]] | None = None
        self._cursor: int | None = None
        #: The route list's window over the leftover rows (its ``page`` is the PgUp/PgDn
        #: stride), and whether any rows are hidden by it (gates the footer's scroll atom).
        self._list = ListWindow()
        self._list_hidden = False
        #: The highlighted route's pathline horizontal scroll (cells shifted in, ``←/→``);
        #: resets whenever the cursor leaves that row (see :data:`_HSHIFT_RESET_ACTIONS`).
        self._hshift = 0
        #: The Info tab's key lane horizontal scroll (cells shifted in, ``←/→``). Unlike the
        #: route pathline's, it is not the cursor's — the key is pinned chrome in the stage,
        #: no row you can land on — so moving through the action rows leaves it where the
        #: reader put it; only leaving the tab resets it.
        self._key_shift = 0
        #: The last render width, so the footer hint can tell whether the highlighted
        #: route's pathline actually overflows (nothing does before the first paint).
        self._last_width = 0

    # --- input -----------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The hint line: tab switch, move, open, whatever ``←→`` reads here, Esc last.

        The scroll atom folds ``PgUp/PgDn`` and ``←→`` into one when both apply (a busy node's
        list is windowed *and* its highlighted route overflows) rather than stacking two atoms
        and risking the 72-column hint budget. On the Info tab the ``←→`` atom names its
        subject — the key is the one thing there that scrolls, and the cursor is elsewhere.
        Each atom is gated on there actually being something to scroll, so the line never
        advertises a key that would do nothing.
        """
        parts: list[str] = []
        if len(self._tabs) >= 2:
            parts.append("Tab/⇧Tab switch")
        parts.extend(("↑↓ move", "Enter open"))
        hscroll = self._selected_route_overflows()
        if self._list_hidden and hscroll:
            parts.append("PgUp/PgDn/←→ scroll")
        elif self._list_hidden:
            parts.append("PgUp/PgDn scroll")
        elif hscroll:
            parts.append("←→ scroll")
        elif self._key_overflows():
            parts.append("←→ scroll key")
        parts.append("Esc back")
        return " · ".join(parts)

    def _selected_route_overflows(self) -> bool:
        """Whether the highlighted route's pathline is too wide for the list's content lane.

        Gates both the ``←→`` scroll and its footer atom — a pathline that fits has nowhere
        to scroll. Measured against the last render width (``0`` before the first paint, so
        nothing reads as overflowing until a real width is known).
        """
        if self._last_width <= 0 or self._routes is None:
            return False
        tab = self._tabs[self._tab_index] if self._tabs else None
        if tab is None or tab.kind != "routes":
            return False
        routes = self._routes.routes
        if not routes or not (0 <= self._row_index < len(routes)):
            return False
        avail = max(1, self._last_width - _ROUTE_INDENT)
        # The trailing `…` is chrome riding outside the lane (pathline.with_action_mark), so
        # it is no part of what has to fit: a route exactly as wide as its lane scrolls
        # nowhere, and the mark is simply the thing that goes unshown.
        return cell_len(routes[self._row_index].path.plain) > avail

    def _on_info_tab(self) -> bool:
        """Whether the Info tab is the one filling the stage (so its key lane is on screen)."""
        tab = self._tabs[self._tab_index] if self._tabs else None
        return tab is not None and tab.kind == "info"

    def _key_value(self) -> Text | None:
        """The Info tab's key value — the one vital rendered as a scrolling lane, or ``None``.

        Found by its label rather than its position: the block is assembled elsewhere
        (:func:`open_node_detail`) and the rows around it come and go with what is known
        about the node, but the row *labelled* :data:`_KEY_LABEL` is always the key.
        """
        for label, value in self._info_rows:
            if label == _KEY_LABEL:
                return value
        return None

    def _key_overflows(self) -> bool:
        """Whether the key is wider than its lane — gating both ``←→`` and the footer atom.

        Measured against the last render width (``0`` before the first paint, so nothing
        reads as overflowing until a real width is known), exactly as the route pathline's
        sibling probe is.
        """
        if self._last_width <= 0 or not self._on_info_tab():
            return False
        value = self._key_value()
        if value is None:
            return False
        return cell_len(value.plain) > max(1, self._last_width - _LABEL_LANE)

    def selected_spec(self) -> str:
        """The forced-path spec of the currently-highlighted route (``""`` = auto).

        The opener reads this after a ``trace`` token resolves, so a trace arms on whichever
        route the cursor had picked rather than always the best one.
        """
        routes = self._routes.routes if self._routes is not None else []
        if routes:
            sel = self._route_sel if 0 <= self._route_sel < len(routes) else 0
            return routes[sel].spec
        return ""

    def consume_edge_scrub(self) -> int:
        """Scrub the panel's right edge only while the Info tab is showing its braille map."""
        if self._minimap is None or not self._tabs:
            return 0
        return 2 if self._tabs[self._tab_index].kind == "info" else 0

    def _measure_icon_lane(self) -> int:
        """The action rows' icon column over every mark currently on the page."""
        trace = [self._trace_action] if self._trace_action else []
        return menus.icon_lane(action.glyph for action in (*self._info_actions, *trace))

    def replace_info_row(self, label: str, value: Text) -> None:
        """Swap one vital's value in place — the regions row, once the repeater has answered.

        The row keeps its place in the block (a label not already there is appended), and
        the cursor stays on whatever it was on: the page is the same visit, one fact newer.
        """
        for i, (existing, _value) in enumerate(self._info_rows):
            if existing == label:
                self._info_rows[i] = (label, value)
                return
        self._info_rows.append((label, value))

    def replace_info_actions(self, actions: list[_Action]) -> None:
        """Swap the Info tab's action rows in place, keeping the cursor on the row it was on.

        For an action that changes what the page offers without ending the visit — locking a
        contact turns its row into Unlock and withdraws Archive. The highlight follows the
        committed row by its :attr:`_Action.key` family (``lock`` and ``unlock`` are one
        row), so the reader's next press lands where their last one did.
        """
        focus = self._focusables()
        current = focus[self._row_index % len(focus)][1] if focus else None
        was = getattr(current, "key", None)
        self._info_actions = list(actions)
        self._icon_lane = self._measure_icon_lane()
        family = {"lock": "unlock", "unlock": "lock"}
        for index, (_kind, payload) in enumerate(self._focusables()):
            key = getattr(payload, "key", None)
            if key is not None and key in (was, family.get(was or "")):
                self._row_index = index
                break

    def handle(self, action: str, data: str = "") -> None:
        """Answer one key press on the page.

        Switch tab, move the cursor within a tab, commit a row, page the list, scroll
        whichever over-wide lane the active tab owns (the highlighted route's pathline,
        the Info tab's key), or leave.
        """
        focus = self._focusables()
        n = len(focus)
        if action in _HSHIFT_RESET_ACTIONS:
            self._hshift = 0  # leaving a row abandons its scroll — each one rides its own
        if action == "enter":
            if n:
                kind, payload = focus[self._row_index % n]
                if kind == "path":
                    # A route row is itself the trace entry point: Enter arms a trace on the
                    # route it names (the opener reads :meth:`selected_spec` for the pick).
                    self.resolve("trace")
                else:
                    assert isinstance(payload, _Action)
                    self.resolve(payload.key)
        elif action == "tab":
            self._switch_tab(1)
        elif action == "shift_tab":
            self._switch_tab(-1)
        elif action == "up":
            if n:
                # Both ends clamp rather than wrap: the route rows scroll inside a
                # window (see ListWindow), and a highlight that jumped end to end would
                # take the window with it — the one move that looks like the screen
                # changed under you.
                self._row_index = max(0, self._row_index - 1)
                self._sync_route_sel(focus)
        elif action == "down":
            if n:
                self._row_index = min(n - 1, self._row_index + 1)
                self._sync_route_sel(focus)
        elif action == "pageup":
            if n:
                self._row_index = max(0, self._row_index - self._list.page)
                self._sync_route_sel(focus)
        elif action in ("pagedown", "space"):
            if n:
                self._row_index = min(n - 1, self._row_index + self._list.page)
                self._sync_route_sel(focus)
        elif action in ("home", "ctrl_home"):
            if n:
                self._row_index = 0
                self._sync_route_sel(focus)
            # On a terminal too short for the pinned layout the frame follows the cursor;
            # jumping home should surface the very top of the page, header included.
            self.scroll_to_top()
        elif action in ("end", "ctrl_end"):
            if n:
                self._row_index = n - 1
                self._sync_route_sel(focus)
        elif action == "left":
            if self._on_route_row(focus):
                self._hshift = max(0, self._hshift - _HSCROLL_STEP)
            elif self._key_overflows():
                self._key_shift = max(0, self._key_shift - _HSCROLL_STEP)
        elif action == "right":
            if self._on_route_row(focus):
                self._hshift += _HSCROLL_STEP  # clamped to the pathline's tail at render
            elif self._key_overflows():
                self._key_shift += _HSCROLL_STEP  # clamped to the key's tail at render
        elif action == "escape":
            self.resolve(CANCEL)

    def _on_route_row(self, focus: list[tuple[str, object]]) -> bool:
        """Whether the cursor currently rests on a route row (as opposed to an action row)."""
        return bool(focus) and focus[self._row_index % len(focus)][0] == "path"

    def cursor_line(self) -> int | None:
        """The highlighted row's body line, which the frame keeps in view.

        That only matters on a terminal too short for the pinned layout's minimums.
        """
        return self._cursor

    def _switch_tab(self, delta: int) -> None:
        """Move the active tab, resetting the cursor and list window to that tab's top."""
        if len(self._tabs) < 2:
            return
        self._tab_index = (self._tab_index + delta) % len(self._tabs)
        self._row_index = 0
        self._route_sel = 0
        self._list.top = 0
        self._hshift = 0
        self._key_shift = 0
        self.scroll_to_top()
        self._sync_route_sel(self._focusables())

    def _sync_route_sel(self, focus: list[tuple[str, object]]) -> None:
        """Point the graph highlight at the route under the cursor, when it rests on one."""
        if focus:
            kind, payload = focus[self._row_index % len(focus)]
            if kind == "path":
                assert isinstance(payload, int)
                self._route_sel = payload

    def _focusables(self) -> list[tuple[str, object]]:
        """The active tab's cursor stops: ``("path", route_idx)`` and ``("action", _Action)``.

        On the Routes tab the route rows come first — each is itself the trace entry
        point — with the auto-route Trace action standing in only when there are no routes
        to list; the Info tab offers its own actions (Open full map, Time machine). The
        shared tail (Back) closes every tab.
        """
        tab = self._tabs[self._tab_index] if self._tabs else None
        focus: list[tuple[str, object]] = []
        if tab is not None and tab.kind == "routes" and self._routes is not None:
            focus.extend(("path", i) for i in range(len(self._routes.routes)))
            if not self._routes.routes and self._trace_action is not None:
                focus.append(("action", self._trace_action))
        elif tab is not None and tab.kind == "info":
            focus.extend(("action", a) for a in self._info_actions)
        return focus

    # --- rendering -------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render the pinned chrome, the sized-to-fit stage, and the windowed cursor rows.

        The viewport the frame recorded (:meth:`~meshterm.ui.tui.screen.Screen.note_viewport`)
        is split three ways each paint: the pinned chrome (identity header, tab strip) and
        the action rows take their fixed lines first, the stage takes what it needs of the
        rest (the route graph's row ceiling shrinks to fit, the Info tab's location preview
        grows into what its rows spare), and the route list windows itself into the leftover
        lines — so nothing here ever pushes the graph or the actions off the screen.
        """
        viewport = self._scroll_viewport
        self._last_width = width
        focus = self._focusables()
        if focus:
            self._row_index %= len(focus)
        self._cursor = None
        self._list_hidden = False

        # -- pinned chrome: the identity header, then the tab strip (a lone tab collapses
        # to one line; a multi-tab strip boxes the active tab across three). The budget
        # below is struck from ``len(lines)`` after this, so either shape sizes correctly.
        lines: list[str] = []
        lines.extend(render_lines(self._header, width))
        if self._tabs:
            lines.extend([""] * tab_air())
            lines.extend(
                render_lines(
                    tab_strip([t.name for t in self._tabs], self._tab_index, width),
                    width,
                    no_wrap=True,
                )
            )

        # The action rows are fixed chrome too — a leading blank, one line per row —
        # struck before the stage draws so it can size against them.
        actions = [payload for _kind, payload in focus if _kind == "action"]
        action_lines = (1 + len(actions)) if actions else 0

        # -- the stage, sized to what the viewport leaves, closed by a faint rule.
        route_blocks: list[list[str]] = []
        tab = self._tabs[self._tab_index] if self._tabs else None
        if tab is not None:
            if tab.kind == "info":
                budget = viewport - len(lines) - action_lines - 1  # the rule's line
                lines.extend(self._info_stage(width, budget))
            else:
                route_blocks = [
                    self._route_row_lines(route, i == self._row_index, width)
                    for i, route in enumerate(self._routes.routes if self._routes else [])
                ]
                if not route_blocks:
                    # The bare note pays for its own air under the strip — where the
                    # platform affords the strip any air at all.
                    lines.extend([""] * tab_air())
                budget = viewport - len(lines) - action_lines - 1  # the rule's line
                lines.extend(self._routes_stage(width, budget, route_blocks))
            # The rule closes the stage, so the drawn view and the rows below it read as
            # separate bands rather than one run-on column.
            lines.append(render_to_ansi(Text("─" * width, style="faint"), width))

        # -- the route list, windowed into whatever the stage left.
        if route_blocks:
            window = max(1, viewport - len(lines) - action_lines)
            heights = [len(block) for block in route_blocks]
            on_route = self._row_index if self._row_index < len(route_blocks) else None
            top, count = self._list.fit_blocks(heights, window, on_route)
            if top > 0:
                lines.append(render_to_ansi(ListWindow.marker(top, "above"), width))
            for i in range(top, top + count):
                if i == self._row_index:
                    self._cursor = len(lines)
                lines.extend(route_blocks[i])
            below = len(route_blocks) - top - count
            if below > 0:
                lines.append(render_to_ansi(ListWindow.marker(below, "below"), width))
            self._list_hidden = top > 0 or below > 0

        # -- the pinned action rows (the route rows precede them in focus order).
        if actions:
            lines.append("")
        base = len(route_blocks)
        for j, payload in enumerate(actions):
            assert isinstance(payload, _Action)
            selected = base + j == self._row_index
            if selected:
                self._cursor = len(lines)
            lines.append(self._action_line(payload, selected, width))

        self._scroll_total = max(1, len(lines))
        return lines

    def _info_stage(self, width: int, budget: int) -> list[str]:
        """The vitals block and, with a fix, the location preview grown to fit.

        ``budget`` is the viewport lines left for the whole stage; the vitals rows never
        truncate — it is the preview that flexes, taking whatever they and its caption
        leave, clamped to ``[_MAP_MIN_ROWS, _MAP_MAX_ROWS]``. Every row but the key hangs
        under itself when it wraps; the key holds its one line and scrolls (see
        :meth:`_key_line`), so a row count the preview sizes against can't move as a reader
        walks a long key.
        """
        lines: list[str] = [""] * tab_air()  # the stage's air under the strip, where afforded
        for label, value in self._info_rows:
            if label == _KEY_LABEL:
                lines.append(self._key_line(label, value, width))
                continue
            lines.extend(
                render_hanging(
                    Text(f"{label:<{_LABEL_LANE}}", style="muted"),
                    value,
                    width,
                    indent=_LABEL_LANE,
                )
            )
        if self._minimap is not None:
            lines.append("")
            caption = 1 if self._map_caption is not None else 0
            rows = max(_MAP_MIN_ROWS, min(_MAP_MAX_ROWS, budget - len(lines) - caption))
            lines.extend(self._minimap.render(width, rows))
            if self._map_caption is not None:
                lines.extend(render_lines(self._map_caption, width, no_wrap=True))
        return lines

    def _key_line(self, label: str, value: Text, width: int) -> str:
        """The key vital as one scrolling lane: the label, then the window ``←→`` settled on.

        A full public key is 64 hex digits and the lane is whatever the terminal leaves after
        the label — 63 cells at the 72-column standard, fewer on the PicoCalc — so the key
        never fits, and wrapping it would spend a whole second line on the handful of digits
        that fell off. Instead the lane shows a window of it and :attr:`_key_shift` slides
        that window (``←→``, :data:`_HSCROLL_STEP` cells a press), with a faint
        :data:`_MORE_MARK` at whichever edge the key continues past — the affordance that says
        the row is a viewport, not the whole value.

        The marks sit *beside* the window rather than over its first cell, so the digits keep
        their byte alignment at every shift (the step is four whole bytes): a key always reads
        in byte pairs, and a window starting mid-byte would spell it wrong. The shift is
        clamped here, against the width actually being drawn, to the first step that brings
        the key's tail into the lane — so scrolling can never run off into empty lane, and a
        resize only ever pulls an out-of-range shift back in.
        """
        lane = Text(no_wrap=True)
        lane.append(f"{label:<{_LABEL_LANE}}", style="muted")
        avail = max(1, width - _LABEL_LANE)
        total = cell_len(value.plain)
        if total <= avail:
            self._key_shift = 0
            lane.append_text(value)
            return render_to_ansi(lane, width, no_wrap=True)
        # The lane gives up a cell to the left mark the moment it scrolls, so its last window
        # spans ``avail - 1`` cells; the ceiling is the first whole step that reaches the end.
        steps = -(-(total - (avail - 1)) // _HSCROLL_STEP)
        self._key_shift = max(0, min(self._key_shift, steps * _HSCROLL_STEP))
        shift = self._key_shift
        left = 1 if shift else 0
        inner = avail - left
        right = 1 if shift + inner < total else 0
        if left:
            lane.append(_MORE_MARK, style="muted")
        lane.append_text(crop_cells(value, shift, inner - right))
        if right:
            lane.append(_MORE_MARK, style="muted")
        return render_to_ansi(lane, width, no_wrap=True)

    def _routes_stage(self, width: int, budget: int, route_blocks: list[list[str]]) -> list[str]:
        """The route-graph fan, sized to fit — or the muted note when there is no evidence.

        ``budget`` is what the viewport leaves for the stage *and* the route list together;
        the graph's row ceiling is what remains after its caption/legend and the list's
        guaranteed minimum (the lesser of :data:`_LIST_MIN_LINES` and what the rows actually
        need), floored at :data:`_GRAPH_MIN_ROWS` — so the graph compresses its lane pitch
        before the list would lose its window, and a busy node's fan only spreads out on a
        terminal tall enough to afford it. The selected route draws white with its off-route
        labels faded, as ever.

        Where the caption is dropped (:data:`_GRAPH_CAPTION` — the PicoCalc), the fan is
        drawn one row *over* its ceiling and its leading blank canvas row is peeled off
        after: the renderer always centres the lane band inside two cells of end padding
        while its topmost label wants only one, so that row is reliably empty and the peel
        pays for the row it was granted. Both rows land back in the ceiling itself.
        """
        rv = self._routes
        assert rv is not None
        if not rv.routes or rv.glyph_of is None:
            return render_lines(Text(rv.note or "no route observed yet", style="muted"), width)
        sel = self._route_sel if 0 <= self._route_sel < len(rv.routes) else 0
        # The routes view is a snapshot (the opener built it once), so the whole fan —
        # a multi-pass graph layout — is a pure function of the width, the row budget,
        # and which route is emphasized. One slot suffices: the key only moves on a
        # selection change or a resize, and then the previous layout is dead anyway.
        caption_lines = (1 if _GRAPH_CAPTION else 0) + (1 if rv.legend else 0)
        list_need = min(sum(len(block) for block in route_blocks), _LIST_MIN_LINES)
        max_rows = max(_GRAPH_MIN_ROWS, min(_PATH_MAX_ROWS, budget - caption_lines - list_need))
        stage_key = (width, max_rows, sel)
        if self._stage_memo is not None and self._stage_memo[0] == stage_key:
            return self._stage_memo[1]
        # Priority is fixed by evidence order (route 0, the best-evidence route, is always the
        # spine) so the fan's geometry never moves as the selection changes — only emphasis
        # (which route draws white and on top, and which nodes keep their hue) follows the pick.
        layers = [
            PathLayer(
                hops=route.draw,
                color=_WHITE if i == sel else _GREY,
                priority=len(rv.routes) - i,
                emphasis=1 if i == sel else 0,
            )
            for i, route in enumerate(rv.routes)
        ]
        # A node keeps its hue only while it sits on the selected route, and everything off it
        # — marker and label — recedes to grey. That is the widget's own rule now (see
        # :func:`~meshterm.ui.pathgraph.render_path_graph`), decided in the graph's own id
        # space, so a route reaching a relay by a short hash still lights the wide marker its
        # hop is folded into.
        label_rgb_of = rv.label_rgb_of
        assert label_rgb_of is not None

        lines = list(
            render_path_graph(
                layers,
                width,
                glyph_of=rv.glyph_of,
                label_of=rv.label_of,  # type: ignore[arg-type]
                label_rgb_of=label_rgb_of,
                max_rows=max_rows if _GRAPH_CAPTION else max_rows + 1,
            )
        )
        if _GRAPH_CAPTION:
            caption = Text("node → you, as heard  ·  white = selected route", style="faint")
            lines.extend(render_lines(caption, width, no_wrap=True))
        else:
            while lines and not _SGR.sub("", lines[0]).strip():
                lines.pop(0)
        if rv.legend:
            lines.extend(render_lines(node_type_legend(width=width), width, no_wrap=True))
        self._stage_memo = (stage_key, lines)
        return lines

    def _route_row_lines(self, route: _Route, selected: bool, width: int) -> list[str]:
        """One route row: a ``❯``-pointed pathline, its context hanging on the line below.

        The route keeps its per-node colours even when selected (the pointer, and the white
        line in the graph above, carry the selection) — unlike the plain action rows, which
        take the highlight, since here the colour *is* the content. The trailing ``…`` is the
        app-wide opens-further-prompts mark: Enter on the row arms a trace on it. It rides
        *outside* the lane (:func:`~meshterm.ui.pathline.with_action_mark`) — chrome buys no
        cells off the route, so a chain that fits stays whole and closes on its rounded cap
        instead of cracking to make room for two cells of hint, and the mark is what goes
        unshown where the lane is full. The
        pathline never hop-wraps — it is one line, cropped: the highlighted row rides
        :attr:`_hshift` (``←→``, see :meth:`handle`) so a named chain wider than the lane can
        still be read to its end, under a :func:`~meshterm.ui.pathline.cut_mark` at whichever
        edge the line continues past — a chip broken off on its own colour where the route is
        drawn in chips, the faint ``…`` where it is drawn in arrows. Same window the Info
        tab's key lane scrolls in, and the same one the Message paths rows do. That scroll is
        what buys the row its *names*
        (:func:`_route_line`): a hop chain spelled out in places outruns a lane far sooner
        than one spelled in hash bytes, and the answer is to read it, not to shorten it. Every
        other row is simply cut to the lane (:func:`~meshterm.ui.pathline.cut_to`, cracked or
        ellipsized by the same rule), and the context line under any row — plain prose, never
        a path — ellipsizes: only the row you are on can scroll, and it is the only one whose
        end you are asking to see. The context — hop count, bottleneck SNR, sample count, the
        ``★ best`` / ``device route`` tag — always leads with the count, so every row is two
        lines tall and the list stops changing height as the cursor walks it.
        """
        indent = _ROUTE_INDENT
        avail = max(1, width - indent)
        pointer = Text("❯ " if selected else "  ", style="cursor" if selected else "")
        marked = route.path
        if selected and self._hshift:
            total = cell_len(marked.plain)
            # A scrolled row has given up a cell to its left mark, so its last window spans
            # ``avail - 1``; clamp to the first whole step that brings the tail inside it —
            # short of that the right mark would still be drawn at the far end, promising a
            # remainder the keys can no longer reach. (The key lane clamps the same way.)
            steps = -(-max(0, total - (avail - 1)) // _HSCROLL_STEP)
            self._hshift = max(0, min(self._hshift, steps * _HSCROLL_STEP))
            shift = self._hshift
            # The marks are chrome inside the window, not extra width: each costs the lane a
            # cell, so the crop is measured after they are known — else the line drawn under a
            # right-hand ``…`` would run a cell past the row.
            left = 1 if shift else 0
            inner = avail - left
            right = 1 if shift + inner < total else 0
            window = max(1, inner - right)
            path = Text()
            if left:
                path.append_text(cut_mark(marked, shift, ELIDE_HEAD))
            path.append_text(crop_cells(marked, shift, window))
            if right:
                path.append_text(cut_mark(marked, shift + window - 1, ELIDE_TAIL))
        else:
            path = cut_to(marked, avail)
        path = with_action_mark(path, avail)
        path.no_wrap = True
        line1 = Text()
        line1.append_text(pointer)
        line1.append_text(path)
        lines = [render_to_ansi(line1, width)]
        if route.context.plain:
            context = route.context.copy()
            context.no_wrap = True
            context.overflow = "ellipsis"
            context.truncate(avail)
            line2 = Text(" " * indent)
            line2.append_text(context)
            lines.append(render_to_ansi(line2, width))
        return lines

    def _action_line(self, action: _Action, selected: bool, width: int) -> str:
        """One action row: ``❯`` + icon + label, the whole row lit when it is the cursor's."""
        text = Text("❯ " if selected else "  ", style="cursor" if selected else "")
        # The icon lane is decoration a platform may drop whole (command_icon); the label
        # is what names the action, so an emptied lane simply gives it the cells. A tinted
        # icon carries a claim the decoration can't take with it, though — a red 🗑 is how a
        # destructive row announces itself — so where the lane goes, the tint lands on the
        # label instead. Same move as :func:`~meshterm.ui.menus.marked_label` makes for a
        # menu row, for the same reason: a delete must not read like any other action.
        #
        # Padded to the page's own lane rather than written as `icon + " "`: this page mixes
        # a one-cell 🗑 with two-cell siblings, and the unpadded form started its label a
        # column early (JP, 2026-09-01).
        mark = menus.icon_mark(action.glyph, action.glyph_style, self._icon_lane)
        text.append_text(mark)
        text.append(action.label, style="" if mark.plain else action.glyph_style)
        if selected:
            text.style = "cursor"
        text.no_wrap = True
        text.truncate(width, overflow="ellipsis")
        return render_to_ansi(text, width)


# -- geographic helpers --------------------------------------------------------

_COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """The 8-point compass direction from ``(lat1, lon1)`` toward ``(lat2, lon2)``."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return _COMPASS[round(deg / 45) % 8]


def _range_text(lat: float, lon: float, self_lat: float | None, self_lon: float | None) -> Text:
    """A location value: the coordinates, plus range + bearing from us when we're placed."""
    text = Text(f"{lat:.4f}, {lon:.4f}", style="")
    if self_lat is not None and self_lon is not None:
        km = haversine_km(self_lat, self_lon, lat, lon)
        dist = f"{km * 1000:.0f} m" if km < 1 else f"{km:.1f} km"
        text.append(f"  ·  {dist} {_bearing(self_lat, self_lon, lat, lon)}", style="muted")
    return text


def _as_float(value: object) -> float | None:
    """Best-effort float coercion for the raw lat/lon a device reports (``None`` on junk)."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _located(lat: float | None, lon: float | None) -> bool:
    """Whether a coordinate is a real, plottable fix (see :func:`~meshterm.core.geo.usable_fix`).

    Rejects both the 0/0 null-island a no-GPS node reports and the out-of-range nonsense a
    misconfigured companion sometimes advertises (``lat -97, lon -1042`` seen in the wild),
    either of which would drop the location preview onto an all-black off-world view.
    """
    return lat is not None and lon is not None and usable_fix(lat, lon)


# -- data gathering + the action loop -----------------------------------------


async def open_node_detail(
    ctx: AppContext, contact: Contact | None, *, manage: bool = True
) -> bool:
    """Open the Node detail page for a contact (or our own node) and run its action loop.

    Assembles the page from stored history, the device's contacts, and the observed
    topology — identity, reception stats, a location preview, and the observed routes — then
    loops: show the page, run whatever action the user commits (trace, full map, time
    machine), and show it again, until Esc backs out. This is the same show/act/reshow loop
    the Time Machine and Contacts list use.

    **Three actions end the loop instead of returning to it**, because each leaves a page
    describing a state that no longer holds: archiving the contact off the device, restoring
    it, and deleting it outright. All three are *contact management*, they are grouped last
    on the Info tab, and the page offers exactly the ones that make sense — a live contact
    can be archived or deleted, an archived one restored or deleted.

    **Locking is management that stays.** A live contact can be locked against archiving
    (see :meth:`~meshterm.core.contact_store.ContactStore.set_locked`): the sweep then never
    counts it a candidate, and this page withdraws its Archive row for as long as the lock
    holds. The toggle rewrites the page's action rows in place rather than ending the visit
    — nothing about the contact the page describes has gone — and the list underneath redraws
    its padlock when the reader comes back to it.

    ``manage`` is how a caller says the page is being opened to *look*, not to act. The
    archive preview passes ``False``: a screen whose whole job is choosing what to archive
    should not also hand out a second, singular way to archive — or a delete — from inside
    its own candidate list. The rule is that management verbs belong to the list a contact
    actually lives in, never to a page opened out of a list that is about to act on it
    wholesale.

    Args:
        ctx: The shared application context (must be running the interactive TUI).
        contact: The contact to detail, or ``None`` for our own node (an identity-and-ledger
            page — we never overhear ourselves, so there is no reception history to show).
        manage: Whether to offer the contact-management actions (lock, archive, restore,
            delete).
            ``False`` draws none of them, and the page can then only ever return ``False``.

    Returns:
        ``True`` if the visit ended by archiving, restoring, or deleting the contact — the
        caller's cue to re-read and rebuild the list it came from — ``False`` on any
        ordinary exit.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from ..services.markers import gather_markers
    from ..services.topology import build_topology, is_path_hash
    from ..services.trace_runner import (
        make_name_key_resolver,
        make_node_resolver,
        make_node_type_resolver,
    )
    from .config_editor import show_contact_card
    from .map_screen import basemap_source, open_map
    from .surface import TuiUi
    from .timemachine_screen import open_timemachine_node, open_timemachine_self
    from .trace_screen import collapse_trace_width, open_trace
    from .widgets import route_graph_style

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the node detail screen is only available in the menu")
    session = ctx.ui.session

    you = contact is None

    # -- our own node, for the "us" label, the location preview centre, and range/bearing.
    try:
        info = await ctx.devstate.self_info()
    except Exception:  # noqa: BLE001 - the page is useful without our own identity/fix
        info = {}
    self_name = str(info.get("name") or "this node")
    self_key = str(info.get("public_key") or "")
    self_lat, self_lon = _as_float(info.get("adv_lat")), _as_float(info.get("adv_lon"))
    if not _located(self_lat, self_lon):
        self_lat = self_lon = None

    # -- the device's contacts (for name/type/key resolution) and the routing hash width.
    try:
        contacts = await ctx.devstate.contacts()
    except Exception:  # noqa: BLE001 - resolution just falls back to stored data
        contacts = []
    try:
        mode = int(await ctx.devstate.path_hash_mode())
        prefix_bytes = (mode + 1) if 0 <= mode <= 3 else 0
        width_bytes = collapse_trace_width(mode)
    except Exception:  # noqa: BLE001 - optional reads; sane defaults keep the page working
        prefix_bytes, width_bytes = 0, 1

    stored_names = ctx.repo.node_names()
    # One hash→name resolver for the whole page: the route list and the route graph all name
    # their hops through it (contacts first, recorder history behind).
    resolve = make_node_resolver(contacts, stored_names)

    if you:
        node_id = (self_key.lower().removeprefix("0x")[:12]) or ""
        name: str | None = self_name
        key = self_key
        node_type = info.get("adv_type")
        lat, lon = self_lat, self_lon
    else:
        assert contact is not None
        key = contact.public_key or contact.key_prefix or ""
        node_id = key.lower().removeprefix("0x")[:12]
        name = contact.name or None
        node_type = contact.node_type
        lat = contact.lat if _located(contact.lat, contact.lon) else None
        lon = contact.lon if _located(contact.lat, contact.lon) else None

    # -- reception stats + first-heard from the recorder's history.
    heard = {n.node: n for n in ctx.repo.heard_nodes() if n.node}
    hn = heard.get(node_id)
    firsts = {n: when for n, _n, when in ctx.repo.first_seen()}
    first_heard = firsts.get(node_id)
    if you:
        hn = None  # we never overhear ourselves — no reception history to show
    # A node with a fix but no observation-derived location still shows the map — but only
    # when that overheard fix is itself real (a no-GPS/nonsense advert never places it).
    if lat is None and hn is not None and hn.has_location and _located(hn.lat, hn.lon):
        lat, lon = hn.lat, hn.lon

    label = name or node_id or key or "?"

    # -- identity header.
    if you:
        glyph, glyph_style = SELF_MARK
        name_hue = "you"
    else:
        glyph, glyph_style = NODE_GLYPHS.get(node_type, DEFAULT_GLYPH)
        name_hue = name_style(name, key) if name else "muted"
    header = Text()
    header.append(f"{glyph} ", style=glyph_style)
    header.append(name or "unknown", style=name_hue)
    type_label = NODE_TYPE_LABELS.get(node_type) if node_type is not None else None
    if you:
        header.append("   your node", style="muted")
    elif type_label:
        header.append(f"   {type_label}", style="muted")

    # -- the observed topology: the suggested best path and the routes to draw.
    target_hash = key.lower().removeprefix("0x")
    target_hash = target_hash if (not you and is_path_hash(target_hash)) else None
    topo = build_topology(
        self_id=(self_key.lower().removeprefix("0x")[:12]) or "local",
        contacts=contacts,
        trace_paths=ctx.repo.trace_paths(),
        packet_paths=ctx.repo.packet_paths(),
        neighbour_links=ctx.repo.neighbour_links(),
    )
    device_route: tuple[str, ...] | None = None
    if not you and contact is not None and contact.route_hops is not None:
        device_route = tuple(topo.canonical(h) or h for h in contact.route_hops)
    canonical_target = (topo.canonical(target_hash) or target_hash[:12]) if target_hash else None
    suggested = topo.suggested(canonical_target) if canonical_target else None
    scenarios = (
        topo.scenarios(canonical_target, device_route=device_route) if canonical_target else []
    )

    # -- the info block (identity + how-heard + where; the routes fold into the Routes tab).
    info_rows: list[tuple[str, Text]] = []
    if key:
        info_rows.append(("key", highlighted_hash(key, prefix_bytes, known=bool(name))))
    else:
        info_rows.append(("key", Text("?", style="muted")))
    if not you:
        # A contact's heard time already arrives merged with our reception history (see
        # ``DeviceState._merge_heard``), so it is the later of the two and is what the
        # contact list's lane shows — take it, and the two surfaces can never disagree about
        # one node. Only a node that is no contact, or one whose merge could not read our
        # history, falls back to the raw reception stat.
        heard_at = contact.last_seen if contact else None
        secs = age_seconds(heard_at or (hn.last_seen if hn else None))
        heard_val = Text(format_ago(secs), style=_recency_style(secs))
        if first_heard is not None:
            heard_val.append(f"  ·  first {first_heard.astimezone():%b %d %Y}", style="muted")
        info_rows.append(("heard", heard_val))
        # A node never overheard reads a faint em-dash, as the contact list draws it.
        packets = Text(str(hn.count)) if hn else Text("—", style="faint")
        info_rows.append(("packets", packets))
        signal = _signal_row(hn)
        if signal is not None:
            info_rows.append(("signal", signal))
    if lat is not None and lon is not None:
        info_rows.append(("where", _range_text(lat, lon, self_lat, self_lon)))
    # A repeater's regions: what it was last heard to relay, from the region store (its own
    # answer to the regions request, or its table read on the admin page). Only a repeater
    # answers the request, so only a repeater's page asks it.
    region_store = getattr(ctx, "region_store", None)
    asks_regions = (
        not you
        and contact is not None
        and node_type == NODE_TYPE_REPEATER
        and bool(node_id)
        and region_store is not None
    )
    routed = contact is not None and contact.route_hops is not None
    if asks_regions:
        info_rows.append(("regions", regions_value(region_store, node_id, routed=routed)))

    # -- the location preview (only when the node advertised a fix).
    minimap: MiniMap | None = None
    map_caption: Text | None = None
    markers: list = []
    if lat is not None and lon is not None:
        try:
            markers = await gather_markers(ctx)
        except Exception:  # noqa: BLE001 - a preview of just this node is still useful
            markers = []
        source = basemap_source(ctx)
        try:
            max_zoom = await asyncio.to_thread(lambda: source.max_zoom)
        except Exception:  # noqa: BLE001 - offline: markers on a blank grid
            max_zoom = 14
        minimap = MiniMap(
            session,
            source,
            max_zoom,
            center_lat=lat,
            center_lon=lon,
            zoom=min(13, max_zoom),
            markers=markers,
        )
        map_caption = Text(f"{label} · centred here", style="faint")

    # -- the "routes heard" route list + graph (node → us), best-evidence path white.
    routes_view: _RoutesView | None = None
    if not you:
        routes_view = _routes_view(
            topo,
            scenarios,
            suggested,
            device_route,
            canonical_target,
            target_hash,
            width_bytes,
            resolve=resolve,
            type_of=make_node_type_resolver(contacts),
            key_of=make_name_key_resolver(contacts, stored_names),
            style=route_graph_style,
            self_name=self_name,
            node_label=label,
            name_key=key,
            node_known=name is not None,
            hash_bytes=prefix_bytes,
        )

    # -- the tabs (Info always; Routes only when there is a routes view) and their actions.
    tabs: list[_Tab] = [_Tab(name="Info", kind="info")]
    if routes_view is not None:
        tabs.append(_Tab(name="Routes", kind="routes"))

    info_actions: list[_Action] = []
    if minimap is not None:
        info_actions.append(_Action("map", "🌍", "", "Open full map"))
    if you:
        info_actions.append(_Action("timemachine", "⏳", "", "Time machine — your activity"))
    elif hn is not None:
        info_actions.append(
            _Action("timemachine", "⏳", "", f"Time machine — {hn.count} receptions")
        )
    # The share card encodes the full 32-byte key; a prefix-only contact (heard but never
    # synced from the device) has nothing scannable to offer, so the row only shows when
    # the whole key is known.
    full_key = key.lower().removeprefix("0x")
    if len(full_key) == 64 and is_path_hash(full_key):
        info_actions.append(_Action("share", "📱", "", "Share contact — QR / link"))
    if asks_regions:
        # Acts at once — one anonymous request, one transmission — so no trailing "…".
        info_actions.append(_Action("regions", "🔖", "", "Ask which regions it carries"))
    # -- contact management, the page's last group and the only actions that end the visit.
    # Withheld entirely when the caller opened the page to look rather than to act (see
    # ``manage``), and never offered for our own node or for a contact carrying no key at
    # all — there would be nothing to address the write by.
    archived = manage and _is_archived(ctx, contact, self_key)
    manageable = (
        manage
        and not you
        and contact is not None
        and bool(contact.public_key or contact.key_prefix)
    )
    store = getattr(ctx, "contact_store", None)
    dev_pub = self_key.lower().removeprefix("0x")
    whole_key = contact is not None and len(contact.public_key.lower().removeprefix("0x")) == 64
    # A lock is MeshTerm's own mark, hung on the full key in the contact store, and only a
    # live contact carries one — it exists to keep a contact off the archive path.
    lockable = manageable and not archived and whole_key and store is not None and bool(dev_pub)
    base_actions = list(info_actions)

    def management(locked: bool) -> list[_Action]:
        """The page's action rows, closing on the management group for this lock state."""
        rows = list(base_actions)
        if not manageable:
            return rows
        # The lock first: it changes nothing on the device and ends nothing, so it is the
        # safest row in the group to land on. Then the constructive verb, then the
        # destructive one, so the row a mistaken press is likeliest to land on is the
        # recoverable one. Archive and restore are the two halves of one reversible move,
        # and take the ``💾``/``📂`` pair the lexicon already gives put-it-away and
        # bring-it-back; only a contact with a full key can be written back to the device,
        # so a prefix-only contact is never offered the archive that would strand it — and
        # a locked one is never offered it at all, which is what the lock is for.
        if lockable:
            if locked:
                rows.append(_Action("unlock", "🔓", "", "Unlock contact"))
            else:
                rows.append(_Action("lock", "🔒", "", "Lock contact"))
        if archived:
            rows.append(_Action("restore", "📂", "ok", "Restore to device"))
        elif whole_key and not locked:
            rows.append(_Action("archive", "💾", "", "Archive contact"))
        rows.append(_Action("remove", "🗑", "err", "Delete contact…"))
        return rows

    locked = bool(lockable and store.is_locked(dev_pub, contact.public_key))
    info_actions = management(locked)
    try:
        adv_type = int(node_type) if node_type is not None else 1
    except (TypeError, ValueError):
        adv_type = 1
    # With routes listed, each route row is its own trace entry point (Enter arms it); the
    # dedicated action only stands in when there is no route evidence to list.
    trace_action: _Action | None = None
    if not you and node_id and not (routes_view is not None and routes_view.routes):
        trace_action = _Action("trace", "🎯", "", "Trace — auto route …")
    title = f"Node — {label}" if not you else f"Node — {label} (you)"
    # Whether the visit ended by deleting the contact — the caller's cue to rebuild its list.
    contact_removed = False
    # One screen for the whole visit. The page is a *hub* — every action on it opens
    # something else and comes back — so it stays pushed while each of them runs
    # (``session.stay``) instead of being rebuilt per round. Rebuilding cost the open tab
    # and the cursor every time: Tab to Routes, pick a route, trace it, come back, and you
    # were on Info at the top again, two keypresses from the route you were working
    # through. Keeping the object keeps all of it. It also means the peers below *nest*
    # above this page rather than replacing it, so Esc from a trace lands back here; ^W is
    # what leaves the whole excursion at once.
    screen = NodeDetailScreen(
        title=title,
        header=header,
        info_rows=info_rows,
        tabs=tabs,
        minimap=minimap,
        map_caption=map_caption,
        routes=routes_view,
        info_actions=info_actions,
        trace_action=trace_action,
    )
    async with session.stay(screen) as visit:
        while True:
            action = await visit.result()
            if action is CANCEL or action is None:
                break
            if action == "trace":
                await open_trace(ctx, name or key, initial_spec=screen.selected_spec())
            elif action == "map":
                if markers:
                    # Open centred on this node — the very spot the inline preview showed —
                    # not wherever the global map was last left — with the find filter seeded
                    # to it, so this node lights among the rest. Seed only when its label
                    # actually matches a marker; a needle nothing matches would dim
                    # everything. (The map action only exists when the node has a fix, so
                    # lat/lon are set.)
                    needle = label.casefold()
                    await open_map(
                        ctx,
                        markers,
                        focus=(lat, lon),
                        find=(
                            label if any(needle in m.label.casefold() for m in markers) else None
                        ),
                    )
            elif action == "share":
                await show_contact_card(ctx, label, full_key, adv_type)
            elif action == "regions":
                assert contact is not None and region_store is not None  # only offered so
                failed = await _ask_regions(ctx, contact, node_id, label, routed=routed)
                screen.replace_info_row(
                    "regions",
                    regions_value(region_store, node_id, routed=routed, failed=failed),
                )
            elif action == "timemachine":
                if you:
                    await open_timemachine_self(ctx)
                else:
                    await open_timemachine_node(ctx, node_id, label)
            elif action == "restore":
                # A restore writes the contact back and ends the visit for the same reason a
                # deletion does: the page was opened from the Archived list, and the row it
                # was opened from is about to stop existing there.
                assert contact is not None  # the row only exists for a real contact
                if await _restore_archived(ctx, contact, self_key, label):
                    contact_removed = True
                    break
            elif action in ("lock", "unlock"):
                # No confirm and no notice: the lock is reversible from the very row that set
                # it, and the row turning into its opposite is the acknowledgement.
                assert contact is not None and store is not None  # only offered when lockable
                locked = action == "lock"
                store.set_locked(dev_pub, contact, locked)
                screen.replace_info_actions(management(locked))
            elif action == "archive":
                assert contact is not None
                if await _archive_contact(ctx, contact, self_key, label):
                    contact_removed = True
                    break
            elif action == "remove":
                # The confirm floats over this page, which is already the backdrop — the
                # reader confirms against the node they are looking at. A removal ends the
                # visit: there is no contact left to detail, and the list we return to
                # rebuilds without it.
                assert contact is not None  # the row only exists for a real contact
                contact_removed = await _remove_contact(ctx, contact, self_key, label)
                if contact_removed:
                    break
    if minimap is not None:
        # The preview's braille may have smeared the terminal (double-width fallback
        # glyphs prompt_toolkit's diff can't see); force one clean repaint of the list
        # underneath, exactly as the full map does on the way out.
        session.request_full_repaint()
    return contact_removed


def _is_archived(ctx: AppContext, contact: Contact | None, self_key: str) -> bool:
    """Whether this contact was swept off the device and is being kept by MeshTerm.

    Read straight from the contact store rather than passed in, so the page answers for
    itself wherever it was opened from — the Contacts list's ``Archived`` section, a map
    marker, a route row — instead of only where the caller happened to know.
    """
    store = getattr(ctx, "contact_store", None)
    dev_pub = (self_key or "").lower().removeprefix("0x")
    if store is None or contact is None or not dev_pub or not contact.public_key:
        return False
    key = contact.public_key.lower().removeprefix("0x")
    return any(c.public_key == key for c in store.archived(dev_pub))


async def _archive_contact(ctx: AppContext, contact: Contact, self_key: str, label: str) -> bool:
    """Confirm and archive one contact off the device; ``True`` once it is gone from the radio.

    The single-contact counterpart to the bulk sweep (see
    :func:`~meshterm.ui.sweep_screen.archive_contacts`), doing exactly what the sweep does to
    each of its victims: remove from the device, then record it in the cross-session store
    with an archive stamp so nothing is actually lost.

    The confirm is **amber, not red, and takes no typing**. Two escalating caution tiers
    exist for two different costs, and this one is recoverable: the contact keeps its key,
    its reception history and its transcripts, the Archived list is one row on the Contacts
    screen away, and a restore is a single write. Red and a typed word are for
    :func:`_remove_contact`, which is the one that cannot be undone.

    Args:
        ctx: The shared application context (interactive menu; the page is the backdrop).
        contact: The contact to archive, addressed by the key it carries.
        self_key: The device's own public key (hex) — how the contact store scopes this
            device's remembered contacts.
        label: How the node is named on the page, for the prompt and the failure notice.

    Returns:
        ``True`` if the contact was archived, ``False`` if the user cancelled or the device
        refused. A device that has no such contact is not a refusal: the archive finishes on
        our side, since being off the radio is the state it was asking for.
    """
    from .surface import TuiUi

    assert isinstance(ctx.ui, TuiUi)  # guaranteed by open_node_detail
    session = ctx.ui.session

    # The contact is named as the Contacts list names it: the type mark in its own colour,
    # then the name in its key-derived hue (a bare id standing in for a nameless one is
    # ``node.unknown``). The prose carries the amber span by span rather than as the
    # prompt's base style, whose bold Rich would merge into the mark.
    glyph, glyph_style = NODE_GLYPHS.get(contact.node_type, DEFAULT_GLYPH)
    prompt = Text()
    prompt.append("Archive ", style="warn")
    prompt.append(f"{glyph} ", style=glyph_style)
    prompt.append(
        label,
        style=(
            name_style(label, contact.public_key or contact.key_prefix)
            if contact.name
            else "node.unknown"
        ),
    )
    prompt.append(
        "? It comes off this device's contact list, freeing a slot for a new one. MeshTerm "
        "keeps it — with its key, reception history, and messages — and you can restore it "
        "at any time.",
        style="warn",
    )
    if not await ctx.ui.dialog(
        prompt,
        [("Cancel", False), ("Archive", True)],
        title="Archive contact",
        default=1,
        danger=True,
    ):
        return False

    device = await ctx.device()
    try:
        await device.remove_contact(contact)
    except ContactNotOnDeviceError:
        # The radio doesn't hold it, which is where the archive was taking it anyway: the
        # store write below is the whole of the remaining work.
        ctx.log.debug("contacts: %s was not on the device; archiving ours", contact.name)
    except Exception as exc:  # noqa: BLE001 - a refused removal is reported, not raised
        ctx.log.debug("contacts: archive failed for %s: %s", contact.name, exc)
        await session.message_dialog(
            Text(f"✗ couldn't archive {label} — {exc}", style="err"),
            title="Archive contact",
        )
        return False

    dev_pub = (self_key or "").lower().removeprefix("0x")
    if ctx.contact_store is not None and dev_pub and contact.public_key:
        ctx.contact_store.archive(dev_pub, contact, when=int(time.time()))
    ctx.devstate.invalidate_contacts()
    return True


async def _restore_archived(ctx: AppContext, contact: Contact, self_key: str, label: str) -> bool:
    """Write an archived contact back onto the device; ``True`` once it is live again.

    The inverse of the Contacts sweep, one contact at a time (see
    :mod:`~meshterm.ui.sweep_screen`). The device write comes **first** and the store's
    archive mark is cleared only once it succeeded — a failed write must never leave a
    contact listed as live on a radio that doesn't hold it, which would be a row you cannot
    message and cannot restore.

    No confirm: restoring is constructive, reversible by the sweep that archived it, and
    costs one contact slot. The dialog is kept for the failures, which are the only part the
    reader can't infer from the list coming back with the row in it.

    Args:
        ctx: The shared application context (interactive menu; the page is the backdrop).
        contact: The archived contact to write back.
        self_key: The device's own public key (hex).
        label: How the node is named on the page, for the failure notice.

    Returns:
        ``True`` if the contact is back on the device, ``False`` if the write was refused.
    """
    from .surface import TuiUi

    assert isinstance(ctx.ui, TuiUi)  # guaranteed by open_node_detail
    session = ctx.ui.session

    device = await ctx.device()
    try:
        await device.add_contact(contact)
    except Exception as exc:  # noqa: BLE001 - a refused write is reported, not raised
        ctx.log.debug("contacts: restore failed for %s: %s", contact.name, exc)
        await session.message_dialog(
            Text(f"✗ couldn't restore {label} — {exc}", style="err"),
            title="Restore contact",
        )
        return False

    dev_pub = (self_key or "").lower().removeprefix("0x")
    if ctx.contact_store is not None and dev_pub and contact.public_key:
        ctx.contact_store.restore(dev_pub, contact.public_key)
    ctx.devstate.invalidate_contacts()
    return True


async def _remove_contact(ctx: AppContext, contact: Contact, self_key: str, label: str) -> bool:
    """Confirm and drop one contact from the device; ``True`` once it is gone.

    The single-contact counterpart to the Contacts list's bulk sweep (see
    :func:`~meshterm.ui.sweep_screen.archive_contacts`) — but a *deletion* where that one
    archives: this is the way to make MeshTerm forget a node entirely, so it removes the
    contact in both places, because the list a screen sees is the *union* of the two: the device's
    own contact table, and the contacts MeshTerm remembers for that device (see
    :mod:`meshterm.core.contact_store`). Forget only the first and the store merges the
    contact straight back on the next read, so the deletion would look like a screen that
    did nothing.

    Only the *contact* goes. The node's reception history, its overheard traffic and the
    chat messages exchanged with it are all MeshTerm's own and are untouched — what is lost
    is the device's ability to address it, which is why a chat send to a contact the
    firmware no longer holds offers to write it back rather than failing (see
    :func:`~meshterm.ui.chat._restore_contact`).

    **Irreversible, and the confirm says so.** Nothing here can re-derive a key the store has
    forgotten, so this is the one contact action with no way back — which is exactly what
    separates it from :func:`_archive_contact`, whose amber confirm sits one row above it on
    the same page. It wears the reserved red for data loss: Cancel on the left, the
    committing Delete on the right and default.

    Args:
        ctx: The shared application context (interactive menu; the page is pushed as the
            confirm's backdrop by the caller).
        contact: The contact to delete, addressed by the key it carries.
        self_key: The device's own public key (hex) — how the contact store scopes this
            device's remembered contacts.
        label: How the node is named on the page, for the prompt and the failure notice.

    Returns:
        ``True`` if the contact was removed, ``False`` if the user cancelled or the device
        refused (the refusal is shown, never swallowed — a contact still on the radio must
        not vanish from the list). A device that has no such contact is not a refusal: the
        removal finishes on our side and says so
        (:class:`~meshterm.core.connection.ContactNotOnDeviceError`).
    """
    from .surface import TuiUi

    assert isinstance(ctx.ui, TuiUi)  # guaranteed by open_node_detail
    session = ctx.ui.session

    if not await ctx.ui.dialog(
        f"Delete {label} for good? Its key is forgotten by both this device and MeshTerm, "
        "so it can't be messaged or restored — only heard again. To free the device slot "
        "and keep the contact, archive it instead.",
        [("Cancel", False), ("Delete", True)],
        title="Delete contact",
        default=1,
        destructive=True,
    ):
        return False

    device = await ctx.device()
    try:
        await device.remove_contact(contact)
    except ContactNotOnDeviceError:
        # Nothing to delete on the radio — the entry only ever existed in what MeshTerm
        # remembers for this device (or the firmware dropped it since we read the table).
        # That is not a failed removal: the contact still goes, and the reader is told why
        # the device had no part in it, because "removed" would otherwise be a claim about
        # a radio that never held it.
        ctx.log.debug("contacts: %s was not on the device; removing ours", contact.name)
        await session.message_dialog(
            Text(
                f"⚠ {label} wasn't in this device's contacts — deleted from the ones "
                "MeshTerm remembers for it.",
                style="warn",
            ),
            title="Delete contact",
        )
    except Exception as exc:  # noqa: BLE001 - a refused removal is reported, not raised
        ctx.log.debug("contacts: remove failed for %s: %s", contact.name, exc)
        await session.message_dialog(
            Text(f"✗ couldn't delete {label} — {exc}", style="err"), title="Delete contact"
        )
        return False

    dev_pub = (self_key or "").lower().removeprefix("0x")
    if ctx.contact_store is not None and dev_pub and contact.public_key:
        ctx.contact_store.forget(dev_pub, contact.public_key)
    # The device's table just changed under the session cache; the list we return to re-reads.
    ctx.devstate.invalidate_contacts()
    # No success notice: unlike the archive sweep — whose outcome is a count nobody could predict —
    # this one is self-evident. The page closes on the contact it detailed and the list
    # behind it comes back without the row, which says it better than a dialog to dismiss.
    return True


#: Why a repeater may not answer the regions request, in the words the page uses for it.
_REGIONS_REACH = "it answers only a neighbour, or over a known route"


def regions_value(store, node_id: str, *, routed: bool, failed: bool = False) -> Text:  # noqa: ANN001 - RegionStore
    """A repeater's ``regions`` vital: what it relays, whether unscoped too, and how fresh.

    Four states, each in its own words so none reads as another:

    * **answered** — the regions it named, then whether it also relays *unscoped* floods
      (the ``*`` its answer leads with, which names no region), then when it said so. A
      repeater that relays unscoped floods and no region reads ``unscoped floods only``, and
      one that named nothing at all reads that it relays no floods;
    * **not asked** — nothing on record; a repeater with no known route also says why the
      question may go unanswered (the request is only answered when it arrives direct);
    * **no answer** — the question was just asked and nothing came back, on a repeater that
      never answered before. One that answered earlier keeps its answer, marked stale by its
      age: an unanswered repeat says nothing about what it carries.

    Args:
        store: The region store.
        node_id: The repeater's 12-hex id.
        routed: Whether the device holds a route to it (a neighbour or a learned path).
        failed: Whether the ask this visit went unanswered.

    Returns:
        The row's value.
    """
    answer = store.answer_of(node_id)
    if answer is None:
        if failed:
            text = Text("no answer", style="warn")
            text.append(f" — {_REGIONS_REACH}", style="muted")
            return text
        text = Text("not asked yet", style="muted")
        if not routed:
            text.append(f" — {_REGIONS_REACH}", style="muted")
        return text
    names = store.carried_by(node_id)
    text = Text()
    if names:
        text.append(", ".join(names))
        text.append("  ·  " + ("unscoped too" if answer.unscoped else "scoped only"), style="muted")
    elif answer.unscoped:
        text.append("unscoped floods only")
    else:
        text.append("none — it relays no floods", style="warn")
    text.append(f"  ·  answered {format_ago(age_seconds(answer.answered_at))}", style="muted")
    if failed:
        text.append("  ·  no answer now", style="warn")
    return text


async def _ask_regions(
    ctx: AppContext, contact: Contact, node_id: str, label: str, *, routed: bool
) -> bool:
    """Ask a repeater which regions it carries — once — and learn the answer.

    One anonymous request, one transmission, under the busy overlay; the answer replaces
    what the store held for this repeater (:meth:`~meshterm.core.region_store.RegionStore.
    learn_carried`). An unanswered request is said in a popup with the reason it most
    likely went unanswered — the request is dropped unless it arrives direct, and the
    repeater rate-limits it — and nothing retries it.

    Returns:
        ``True`` when the repeater did not answer.
    """
    device = await ctx.device()
    try:
        async with ctx.ui.busy_overlay():
            names = await device.request_regions(contact)
    except DeviceCommandError as exc:
        where = "" if routed else " There is no known route to it, so only a neighbour answers."
        await ctx.ui.session.message_dialog(
            Text(f"{exc}{where}", style="warn"),
            title=f"Regions — {label}",
        )
        return True
    ctx.region_store.learn_carried(node_id, names)
    return False


def _signal_row(hn) -> Text | None:  # noqa: ANN001 - Optional[HeardNode]
    """Median/best reception SNR and last RSSI, or ``None`` when nothing was measured."""
    if hn is None:
        return None
    text = Text()
    if hn.median_snr is not None:
        text.append("median ", style="muted")
        text.append(f"{hn.median_snr:+.1f} dB", style=snr_style(hn.median_snr))
        if hn.best_snr is not None:
            text.append("  ·  best ", style="muted")
            text.append(f"{hn.best_snr:+.1f} dB", style=snr_style(hn.best_snr))
    if hn.last_rssi is not None:
        if text.plain:
            text.append("  ·  ", style="muted")
        text.append(f"RSSI {hn.last_rssi:.0f} dBm", style="muted")
    return text if text.plain else None


def _hop_hash(key: str | None, fallback: str, hash_bytes: int) -> str:
    """A key's hash at ``hash_bytes`` width, or ``fallback`` when there is no usable key."""
    hex_key = (key or "").lower().removeprefix("0x")
    return hex_key[: hash_bytes * 2] if hex_key else fallback


def _route_line(
    node_label: str,
    name_key: str,
    hops_out: tuple[str, ...],
    tag: str,
    weakest: float | None,
    samples: int,
    *,
    resolve,  # noqa: ANN001 - NodeResolver, kept loose like the graph callbacks
    node_known: bool,
    self_name: str | None,
    hash_bytes: int,
) -> tuple[Text, Text]:
    """One route as ``(path, context)`` — the pathline, and the line it hangs its context under.

    The whole route reads left to right in the graph's own direction (contact on the left, us
    on the right), so the row and the drawn line cross-read. ``path`` is one
    :class:`~meshterm.ui.pathline.PathLine` drawn exactly as the Message paths dialog draws
    its arrivals (JP, 2026-08-09): **hops read as names** — every hop the resolver can place
    wears its contact name in that node's key-derived hue, as powerline chips where the
    terminal can draw them, and our own end stands on the app-wide ``★`` — every route on this
    screen ends on us, so the star says it in one cell and leaves the rest of the lane to the
    hops that differ from row to row (the same trade the trace route lane and the trophy card
    make, and the same mark the graph above plants on our end). A hop nobody can name has
    no name to show, so it stands in its own hash at the device's path-hash-mode width (the
    width the radio itself carries per hop) in the app-wide unknown-node grey — colour being
    the "this is a name" signal, exactly as in the graph above. No hop repeats its hash after
    its name: the hash bytes are the *graph's* job one band up, and what ties a row to the fan
    is the shared node hue rather than a second spelling of the hex.

    Names cost cells that hashes don't, which is what the row's horizontal scroll is for (see
    :meth:`NodeDetailScreen._route_row_lines`) — the same trade the Message paths rows make,
    and the reason a name is worth it: a route reads as places, and the reader pays only for
    the one row they are on. ``context`` leads with the route's hop count
    (:func:`~meshterm.ui.pathline.hops_atom` — the figure the line above encodes but never
    states, and the one two routes are compared on first), then the bottleneck SNR, sample
    count, and a ``★ best`` / ``device route`` tag marking the winner and the firmware's
    learned route — all kept off the pathline itself so a long chain never crowds it out.
    The count means the row always earns a context line, which is right: every route has a
    length, and a row whose second line came and went with the evidence made the list jump
    a row taller as the cursor moved.
    """
    hash_bytes = max(hash_bytes, 1)  # an unknown mode still needs a real width to slice

    def hop_of(hop: str) -> PathHop:
        named = resolve(hop)
        if named and named != hop:
            return PathHop(named, key=hop)
        return PathHop(_hop_hash(hop, hop, hash_bytes))  # unknown: its hash, keyless grey

    path = PathLine(
        [
            PathHop(node_label, key=name_key)
            if node_known and name_key
            else PathHop(_hop_hash(name_key, node_label, hash_bytes)),
            *(hop_of(hop) for hop in reversed(hops_out)),
            PathHop(SELF_GLYPH, you=True),
        ]
    ).text()

    atoms: list[Text] = [hops_atom(len(hops_out))]
    if weakest is not None:
        snr = Text("weakest ", style="muted")
        snr.append(f"{weakest:+.1f} dB", style=snr_style(weakest))
        atoms.append(snr)
    if samples:
        atoms.append(Text(f"{samples}×", style="muted"))
    if tag == "best":
        atoms.append(Text("★ best", style="brand"))
    elif tag == "device":
        atoms.append(Text("device route", style="accent"))
    context = Text()
    for i, atom in enumerate(atoms):
        if i:
            context.append("  ·  ", style="muted")
        context.append_text(atom)
    return path, context


def _cluster_presentation(members: tuple[str, ...], type_of) -> _Cluster:  # noqa: ANN001
    """The marker + label a contracted bidirectional cluster draws under.

    A homogeneous cluster (every member the same node type) wears that type's map marker and
    hue — ``3 repeaters`` under a ``▲`` — so it reads as a group of that kind at a glance; a
    mixed one falls back to the plain node dot and ``n nodes``.
    """
    types = {type_of(m) for m in members}
    only = next(iter(types)) if len(types) == 1 else None
    if only is not None:
        glyph, color = NODE_GLYPHS.get(only, DEFAULT_GLYPH)
        kind = NODE_TYPE_LABELS.get(only, "node")
    else:
        glyph, color = DEFAULT_GLYPH
        kind = "node"
    return _Cluster(glyph=glyph, color=color, label=f"{len(members)} {kind}s", rgb=mark_rgb(color))


def _contract_bidir_clusters(
    routes: list[_Route], type_of
) -> tuple[list[_Route], dict[str, _Cluster]]:  # noqa: ANN001
    """Fold each 3+ bidirectional cluster in the routes' draw sequences to one super-node.

    A dense knot of mutually-relaying repeaters is a strongly-connected component the flow
    graph can't order (see :func:`~meshterm.ui.pathgraph.bidir_clusters`); left as-is every
    member collapses onto one jammed column. So each such cluster is contracted to a single
    synthetic node: every route's ``draw`` has its members rewritten to the one cluster id
    (consecutive members merged), and the returned map gives each cluster its marker and label.

    Only ``draw`` — the graph geometry — changes. Each route's spelled-out ``row`` and its
    trace ``spec`` keep every member named in order, so the list under the graph still carries
    the exact ordering the single marker can't, and a trace still arms on the real path.

    Returns ``(routes, clusters)`` unchanged (and an empty map) when there is no such knot.
    """
    groups = bidir_clusters([(SRC_NODE, *route.draw, DST_NODE) for route in routes])
    if not groups:
        return routes, {}
    member_of: dict[str, str] = {}
    clusters: dict[str, _Cluster] = {}
    for i, members in enumerate(groups):
        cid = f"{_CLUSTER_NODE}{i}"
        clusters[cid] = _cluster_presentation(members, type_of)
        for member in members:
            member_of[member] = cid
    contracted: list[_Route] = []
    for route in routes:
        draw: list[str] = []
        for hop in route.draw:
            cid = member_of.get(hop, hop)
            if draw and draw[-1] == cid:
                continue  # a run of members through the same cluster is one stop
            draw.append(cid)
        contracted.append(replace(route, draw=tuple(draw)))
    return contracted, clusters


def _routes_view(
    topo,
    scenarios,
    suggested,
    device_route,
    canonical_target,
    target_hash,
    width_bytes,
    *,
    resolve,
    type_of,
    key_of,
    style,
    self_name,
    node_label,
    name_key,
    node_known,
    hash_bytes,
) -> _RoutesView:
    """Build the Routes tab's selectable routes (node → us) + graph callbacks, or a muted note.

    The routes are drawn contact-on-the-left to us-on-the-right — the *inbound* direction the
    packets travelled to reach us, since everything the graph knows was received, not sent.
    That is exactly the orientation the shared :func:`~meshterm.ui.widgets.route_graph_style`
    already draws (it was built for the Message paths view, where traffic arrives *at* us on
    the right), so we hand it the target as its ``source`` — the left endpoint — and use its
    callbacks as they come, only reversing each route's hop order so the drawn line runs from
    the contact inward to us.

    The selectable list, strongest first, folds the old ``route`` / ``suggest`` info rows into
    the tab: the best-evidence route (the observed suggestion, else the firmware's learned
    route, else a bare direct shot), then the firmware's learned route when it is something
    different, then the *good* observed alternatives. Only good alternatives are kept: an
    observed route survives when its evidence is both **fresh** (its stalest hop heard within
    :data:`_PATH_STALE_DAYS`) and **not an outlier** (its score within
    :data:`_PATH_OUTLIER_RATIO` of the strongest observed one) — so the list is the routes
    worth trusting rather than every chain ever heard. With no route evidence at all — no
    learned route, no observed path, not even a direct link — there is nothing honest to draw,
    so a muted note stands in and the tab still offers an auto trace.

    Which route lights white (and which nodes keep their name hue) is the screen's live
    selection, applied per render; this only assembles the routes and the base callbacks.
    """
    from ..services.topology import render_forced_spec

    if canonical_target is None:
        return _RoutesView(note="no key to route to")
    has_evidence = (
        device_route is not None
        or any(s.source == "observed" for s in scenarios)
        or topo.link(topo.self_id, canonical_target) is not None
    )
    if not has_evidence:
        return _RoutesView(note="no route observed yet — trace to discover one")

    best_hops = (
        suggested.hops
        if suggested is not None
        else device_route
        if device_route is not None
        else None
    )
    # A node we only ever hear directly (no relays, no learned route) still earns a line —
    # the straight zero-hop shot — so the graph shows the direct link rather than a bare note.
    if best_hops is None and topo.link(topo.self_id, canonical_target) is not None:
        best_hops = ()

    # The ordered, de-duplicated selectable routes (outbound hops us → target).
    entries: list[tuple[tuple[str, ...], str, float | None, int]] = []
    seen: set[tuple[str, ...]] = set()

    def add(hops_out: tuple[str, ...], tag: str, weakest: float | None, samples: int) -> None:
        key = tuple(hops_out)
        if key in seen:
            return
        seen.add(key)
        entries.append((key, tag, weakest, samples))

    if best_hops is not None:
        if suggested is not None:
            add(best_hops, "best", suggested.weakest_snr, suggested.samples)
        else:
            add(best_hops, "best", None, 0)
    if device_route is not None:
        add(device_route, "device", None, 0)
    for scenario in _good_alternatives(topo, scenarios, canonical_target):
        add(scenario.hops, "", scenario.weakest_snr, scenario.samples)

    routes: list[_Route] = []
    for hops_out, tag, weakest, samples in entries:
        spec = render_forced_spec(hops_out, target_hash, width_bytes) if target_hash else ""
        path, context = _route_line(
            node_label,
            name_key,
            hops_out,
            tag,
            weakest,
            samples,
            resolve=resolve,
            node_known=node_known,
            self_name=self_name,
            hash_bytes=hash_bytes,
        )
        routes.append(_Route(draw=tuple(reversed(hops_out)), spec=spec, path=path, context=context))

    # The legend explains the typed relay marks, so it is decided on the members' real types —
    # before contraction folds a dense cluster's members behind one synthetic id.
    legend = any(type_of(h) is not None for route in routes for h in route.draw)
    # Fold any 3+ bidirectional cluster (a knot the flow can't order) to one super-node, so it
    # draws as a single marker rather than piling its members onto one column. Each route's row
    # and spec keep the members named in order — only the drawn geometry contracts.
    routes, clusters = _contract_bidir_clusters(routes, type_of)

    glyph_of, byte_label_of, label_rgb_of = style(
        resolve=resolve, self_name=self_name, source=node_label, type_of=type_of, key_of=key_of
    )

    # The graph labels exactly as the Message paths graph does (JP, 2026-08-09): the two
    # endpoints by name, every relay by its first hash byte alone. Names in here were the
    # obvious read — until a busy fan drew them: each label is set above or below its own
    # marker, and a long name spills across the lanes either side of it. Two cells never can,
    # so the fan stays legible however many routes it carries. The row list under it is where
    # the names live now, and a relay's byte and its row chip cross-reference by hue rather
    # than by spelling the same node twice. A contracted cluster keeps its own count-and-type
    # label — it stands for no single hash.
    def label_of(node: str) -> str | None:
        cluster = clusters.get(node)
        return cluster.label if cluster is not None else byte_label_of(node)

    base_rgb_of = label_rgb_of

    def cluster_label_rgb_of(node: str) -> RGB:
        cluster = clusters.get(node)
        return cluster.rgb if cluster is not None else base_rgb_of(node)

    # The target wears its own map glyph (▲ repeater, ■ room, ◉ sensor) — the same mark the
    # header and the map give it. route_graph_style draws the far (left) endpoint as a plain
    # dot, since on its home screen (Message paths) that end is an arbitrary message origin;
    # here it is a known contact whose type we can show. A contracted cluster draws its own
    # type mark; everything else keeps the style's glyph.
    base_glyph_of = glyph_of

    def cluster_glyph_of(node: str) -> tuple[str, str]:
        cluster = clusters.get(node)
        return (cluster.glyph, cluster.color) if cluster is not None else base_glyph_of(node)

    glyph_of = _with_target_glyph(
        cluster_glyph_of, NODE_GLYPHS.get(type_of(canonical_target), DEFAULT_GLYPH)
    )
    return _RoutesView(
        routes=routes,
        glyph_of=glyph_of,
        label_of=label_of,
        label_rgb_of=cluster_label_rgb_of,
        legend=legend,
    )


def _good_alternatives(topo, scenarios, target) -> list:  # noqa: ANN001
    """The observed alternative routes worth drawing: fresh, evidence-backed, not outliers.

    Trims the raw scenario list down to the alternatives the graph should show as grey
    alternatives:

    * only the **observed** family (the device/direct scenarios are not routes the evidence
      *observed* the node arrive over — the device route rides on its own row, and a bare
      direct line the evidence never saw is not worth a lane);
    * only ones with real evidence behind them (a positive score — an unobserved link scores
      zero, see :meth:`~meshterm.services.topology.MeshTopology._score_route`);
    * only **fresh** ones — every hop heard within :data:`_PATH_STALE_DAYS`, so a route whose
      weakest link has gone quiet drops out rather than lingering as a line that may no longer
      carry;
    * only ones **not far weaker** than the best observed alternative (score within
      :data:`_PATH_OUTLIER_RATIO` of the strongest), so one clearly-best route isn't buried
      under a fan of marginal ones.

    Returns the survivors in the order :meth:`~meshterm.services.topology.MeshTopology.scenarios`
    ranked them (strongest first).
    """
    observed = [s for s in scenarios if s.source == "observed" and s.hops and s.score > 0]
    if not observed:
        return []
    best_score = max(s.score for s in observed)
    now = utcnow()
    return [
        s
        for s in observed
        if s.score >= best_score * _PATH_OUTLIER_RATIO
        and _route_is_fresh(topo, s.hops, target, now)
    ]


def _route_is_fresh(topo, hops: tuple[str, ...], target: str, now: datetime) -> bool:
    """Whether every link along ``us → hops… → target`` was heard within the stale horizon.

    A route is only as current as its stalest hop: if any link on it has not been heard in
    :data:`_PATH_STALE_DAYS`, the chain may no longer carry, so the whole route counts as
    stale. A link with no timestamp (evidence that carries no ``when``, e.g. a firmware route)
    is treated as fresh — we have no age to hold against it.
    """
    chain = [topo.self_id, *hops, target]
    for a, b in pairwise(chain):
        link = topo.link(a, b)
        if link is None:
            return False
        last = link.last_seen
        if last is None or getattr(last, "tzinfo", None) is None:
            continue  # undated evidence — no age to judge it stale by
        if (now - last).total_seconds() > _PATH_STALE_DAYS * 86400.0:
            return False
    return True


def _with_target_glyph(glyph_of: GlyphOf, target_glyph: tuple[str, str]) -> GlyphOf:
    """Wrap the graph's glyph callback so the target endpoint draws its own node glyph.

    The graph's left endpoint (``SRC_NODE``) is the node this page is about, and here we know
    its type — so it draws the map's own mark for that type (``▲`` repeater, ``■`` room,
    ``◉`` sensor, ``●`` plain), matching the identity header and the location preview, rather
    than ``route_graph_style``'s generic origin dot. Every other node passes through
    untouched (our own star on the right endpoint keeps the style's ``you`` mark).
    """

    def wrapped(node: str) -> tuple[str, str]:
        return target_glyph if node == SRC_NODE else glyph_of(node)

    return wrapped
