# SPDX-License-Identifier: Apache-2.0
"""The Node detail screen: all the information about one node on one page, and the ways into it.

The user opens it with Enter on a contact in the Contacts list (refer to
:mod:`~meshterm.ui.contacts_screen`). The list has one aligned row for each node. This
screen is the node itself, in full:

* **Who it is.** A one-line identity header: the type glyph, the name in the hue of the
  node, and the type label. It is the only chrome pinned above the stage, so the stage
  keeps almost the full screen.
* **A stage with tabs** below the header. It shows one full-height tab at a time. The
  user changes the tab with ``Tab``/``Shift+Tab`` on a strip of tabs in a box (refer to
  :func:`~meshterm.ui.widgets.tab_strip`). The vitals, the location preview, and the
  route graph are not stacked in one long scroll. Instead, each tab gets the full stage:

  * **Info**: the vitals of the node as labelled rows. These are its key with the routing
    hash lit, when it was first and last heard, how many of its packets the device
    overheard, its reception SNR and last RSSI, and where it is. For a repeater, they are
    also the regions for which it was last heard to relay floods, whether it also relays
    unscoped floods, and when it said so (refer to :func:`regions_value`). The key is on
    one lane, and it is never wrapped. A full public key is longer than the row on each
    terminal that MeshTerm supports. Thus ``←→`` scroll it by four bytes at a time, under
    faint ``…`` marks at the edges, the same as a long path line scrolls on the Routes
    tab. Then, when the node has advertised a location, a static basemap preview (refer
    to :class:`~meshterm.ui.minimap.MiniMap`) centred on the node. The preview grows into
    the rows of the viewport that remain.

    The actions of the tab: ``Open full map`` opens the full map, centred here, with its
    find filter set to this node, so the node is lit among the others. Then
    ``Time machine``. ``Share contact`` opens a contact card in a dialog (a QR code and a
    ``meshcore://`` link, refer to :func:`~meshterm.ui.config_editor.show_contact_card`).
    It is available when the full key of the node is known.
    ``Ask which regions it carries`` is on a repeater. It sends one anonymous request,
    and the answer comes only when it arrives direct. MeshTerm learns the answer into the
    region store, and the row is drawn again in place.

    Last, the contact-management group, when the caller lets the page manage the contact
    (refer to :func:`open_node_detail`). It has ``Lock contact`` or ``Unlock contact``,
    then ``Archive contact`` or ``Restore to device`` when they apply, then
    ``Delete contact…``. ``Delete contact…`` is the last row, because it is the only
    action that cannot be undone. It is the single-contact counterpart of the bulk sweep
    of the Contacts list (refer to :mod:`~meshterm.ui.contacts_screen`), but it deletes
    where the sweep archives. After a red confirm, it removes this node from the contact
    table of the device and from the contacts that MeshTerm remembers for the device.
    Then the page closes, because the contact that the page shows does not exist any
    more. An archive and a restore also close the page. A lock does not.
  * **Routes**: the routes on which the device really heard the node arrive. The shared
    route graph (:mod:`~meshterm.ui.pathgraph`) draws them from the node to our node, in
    the inbound direction of the packets: the contact on the left, our node on the right.
    The label of each relay is only the first byte of its hash. That is two cells, so the
    graph stays readable with any number of graph lanes.

    Below the graph is the *route list*, which has the names. It has one selectable row
    for each different route: the route that the firmware learned, and the observed
    alternatives, with ``★ best`` on the strongest. Each row is one **named** line, drawn
    by the only path widget. A hop that has no known name shows its hash instead. The
    line below it has the bottleneck SNR, the sample count, and the tag. The two parts
    refer to each other by node hue, and the same hex is not written two times. The
    Message paths dialog has exactly the same split.

    ``↑↓`` moves the selection. The selected route is white in the graph, and the other
    routes are grey. Each node that is not on the selected route has a grey label. Thus
    the graph shows this one route among all the routes. A path line that is too wide
    for the lane never wraps. Instead, the highlighted row scrolls horizontally (``←→``)
    under faint ``…`` marks at the edges, so that the user can read it to the end. A
    long row that is not selected gets an ellipsis.

    Only good routes are drawn. MeshTerm removes stale evidence and outliers that are
    much weaker. Thus the list has the routes that the user can trust, not each chain
    that was ever heard. Enter on a route prepares a trace on it. (Nothing transmits
    here. Enter opens the trace screen with that path.) When there is no route evidence
    to list, the ``🎯 Trace — auto route …`` action is shown instead.

* **The ways in.** The action rows of each tab (``↑↓`` moves the highlight through them,
  and Enter commits). There is no Back row: Esc on each tab closes the page and goes back
  to the list.

The page never scrolls as one long strip. The identity header, the tab strip, and the
stage are pinned. The stage gets its size from the terminal: the route graph compresses
its graph lanes before it overflows, and the location preview grows into the rows that the
Info tab leaves. A faint rule closes the stage, so the drawn tab and the rows below read as
separate bands. The route list scrolls in the remaining rows, with faint ``↑ n more`` and
``↓ n more`` markers at its edges. This is the list window of the whole app (refer to
:class:`~meshterm.ui.tui.screen.ListWindow`, which fits whole blocks). Thus the graph,
the selection that controls it, and the action rows share one screen, also when a busy
node has many routes. ``PgUp/PgDn`` move the highlight through the list window by pages.

The screen only shows data and returns a choice. It renders display data that is already
resolved, and it resolves with an action token. (The Trace token carries the spec of the
selected route, through :meth:`NodeDetailScreen.selected_spec`.) :func:`open_node_detail`
gets the data and runs the sub-flows that each action opens, then shows the page again.
The Time Machine and the Contacts list use the same loop. The screen itself never
transmits. The only action that transmits is the regions question. The opener asks it one
time, when the user commits its row.
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
    short_frame,
    tab_air,
    tab_strip,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import AppContext

#: Whether the route graph has a caption ("node → you, as heard  ·  white = selected route"),
#: and whether it can keep the empty canvas row that its top space leaves. The PicoCalc has
#: neither (JP, 2026-08-09), and both rows go to the graph instead. The caption explains a
#: picture that the white line and the ``you`` star already make clear. The end padding of
#: the renderer (:data:`~meshterm.ui.pathgraph._GRAPH_END_DOTS`, two cells) keeps a full
#: cell of empty space above the label of the top graph lane, and nothing ever draws into it.
#: This value follows the same signal for economy of rows as the space of the tab strip
#: (:func:`~meshterm.ui.widgets.tab_air`). It is bound at platform-switch time, like each
#: platform constant.
_GRAPH_CAPTION = True


@on_platform
def _bind_graph_caption(platform: Platform) -> None:
    """Bind the caption and the top space of the graph to the platform (now and at each switch)."""
    global _GRAPH_CAPTION
    _GRAPH_CAPTION = platform.frame_border


#: An SGR escape, so that MeshTerm can check whether a rendered canvas row holds only
#: empty space.
_SGR = re.compile(r"\x1b\[[0-9;]*m")

#: The row limits of the inline location preview. It grows into the rows of the viewport
#: that the Info tab leaves (the vitals and the action rows are short). The minimum lets a small
#: terminal still show an area that the user can recognize. The maximum keeps the map from
#: filling all of a tall terminal.
_MAP_MIN_ROWS = 5
_MAP_MAX_ROWS = 13

#: The maximum number of rows of the route graph on the Routes tab. The graph draws the
#: contact on the left and our node on the right (node → us). Thus the graph reads from left
#: to right in the inbound direction in which its packets travelled to our node. A node with
#: many connections has several candidate paths at the same time. If MeshTerm puts them into
#: a short box, the graph lanes are compressed together. Thus this page keeps the shared
#: pitch of the graph lanes, and lets the graph grow with its graph lanes, up to this limit.
#: The real budget is calculated for each render against the terminal. The stage gets the
#: rows of the viewport that remain after the pinned chrome, the action rows, and the
#: minimum list window of the route list. Thus the graph compresses its pitch before it can
#: push the list or the actions off the screen, and it gets to this limit only on a
#: terminal that is tall enough.
_PATH_MAX_ROWS = 22

#: The minimum number of canvas rows for the route graph (the minimum of the shared
#: renderer). On a terminal that is too short for this minimum, the frame follows the
#: highlight and keeps the active row visible. The stage does not become so small that it
#: shows nothing useful.
_GRAPH_MIN_ROWS = 5

#: The number of route-list lines that the stage must leave free before it takes the
#: remaining rows of the viewport. That is sufficient for two routes (or one wrapped route)
#: to show next to the graph that shows them in white.
_LIST_MIN_LINES = 4

#: MeshTerm removes a drawn route as stale when its limiting link (the stalest hop that it
#: goes through) was not heard in this number of days. A route is only as current as its
#: link that was heard least recently. If a hop on it has gone quiet, the full chain may not
#: carry packets now. This value agrees with the one-week evidence half-life of the topology
#: (after four half-lives, a link has a weight of approximately 1/16). After that point, a
#: path is more a memory than a fact.
_PATH_STALE_DAYS = 28.0

#: MeshTerm removes a drawn alternative route as an outlier when its evidence score is less
#: than this fraction of the score of the strongest observed alternative. Thus the graph
#: keeps the few routes that the user can trust, not each much weaker chain that the
#: evidence can connect. The route with the best evidence (white) is always drawn, because
#: it is the answer to "how do we reach it", not one of the alternatives that MeshTerm
#: compares.
_PATH_OUTLIER_RATIO = 0.25

#: The cells that the labelled info rows keep for their label lane. Thus the value blocks
#: align, and a wrapped value hangs under itself, not under the label (the hanging-indent
#: rule of the whole app). The size is the widest label of the block ("packets").
_LABEL_LANE = 9

#: The edge colour of the selected route (white, the spine) over the grey of the
#: alternatives, and the grey of the label of a node that is on no part of the selected
#: route. One grey is used for both. Thus the line of a relay that is not on the route, and
#: its name, show the same dim "not this route".
_WHITE = (255, 255, 255)
_GREY = (120, 120, 120)

#: The synthetic node-id prefix under which a contracted bidirectional cluster is drawn. A
#: ``\x00`` at the start makes it not hex, so the graph never tries to merge it with a hash,
#: and it cannot be equal to a real hop. The graph uses its own endpoint sentinels in the
#: same way.
_CLUSTER_NODE = "\x00clu"

#: The indent in cells of the path line of a route row, and of the context line that hangs
#: under it. It is the width of the ``"❯ "`` or ``"  "`` pointer, so the context aligns under
#: the path that it is part of.
_ROUTE_INDENT = 2

#: The number of cells by which one ← or → key press scrolls the path line of the
#: highlighted route, or the key lane of the Info tab (refer to
#: :meth:`NodeDetailScreen.handle`). It is the same step that the ``hscroll`` of the select
#: list uses everywhere in the app. On a key, it is exactly four bytes. Thus each position at
#: which the lane can stop starts on a byte boundary, in the same way that a hash reads.
_HSCROLL_STEP = 8

#: The info row whose value is a key: the only lane that scrolls (``←→``) and does not wrap.
#: A public key has 64 hex digits. That is wider than the value lane on each platform, and it
#: has no word break. Thus a wrap only adds a second line that says nothing. All the other
#: vitals here are prose that is short enough to hang under itself.
_KEY_LABEL = "key"

#: The faint mark at each edge of the key lane past which the key continues.
_MORE_MARK = "…"

#: The highlight moves that cancel the horizontal scroll of a route. Each row scrolls
#: independently. Thus, when the highlight leaves a row, the shift goes back to zero, and it
#: does not go to the next row.
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
    """One action row at the bottom of a tab.

    Attributes:
        key: The token that the screen resolves with when the user commits this row.
        glyph: The icon at the start of the row.
        glyph_style: The style of the icon.
        label: The text of the row (with a current value in it, muted where it is
            context).
    """

    key: str
    glyph: str
    glyph_style: str
    label: str


@dataclass(slots=True)
class _Route:
    """One selectable route on the Routes tab: how it is drawn, traced, and read.

    Attributes:
        draw: The relay hops in the inbound draw order of the graph (contact → us). Thus a
            :class:`~meshterm.ui.pathgraph.PathLayer` built from them puts the contact on
            the left end and our star on the right end. Empty means a direct route with
            zero hops.
        spec: The forced-path spec that a trace prepares when the user selects this route
            (the symmetric round trip through these hops). ``""`` lets the trace screen
            resolve the path automatically.
        path: The path line, rendered in advance. It is the full route, named by the only
            path widget (contact → relays → us). The mark at the end that says that the row
            opens more prompts is added at render time
            (:func:`~meshterm.ui.pathline.with_action_mark`), and it is not stored here. It
            is one line, and it is never wrapped. Instead, the highlighted row scrolls
            horizontally, so that the user can read a long hash chain past the width of the
            lane. An unselected row gets an ellipsis.
        context: The line that hangs under ``path``: the hop count, then the bottleneck
            SNR, the sample count, and a ``★ best`` or ``device route`` tag when the route
            has them. The hop count is always there, so this line is never empty.
    """

    draw: tuple[str, ...]
    spec: str
    path: Text
    context: Text


@dataclass(slots=True)
class _Cluster:
    """The marker on the graph that stands for a contracted bidirectional cluster.

    Three or more repeaters that relay each other in each order make a knot that the flow
    from left to right cannot place (refer to :func:`~meshterm.ui.pathgraph.bidir_clusters`).
    Instead, they are drawn as one super-node, and this class is how that super-node looks.
    The route rows below the graph still name each member in order. Thus the user finds the
    detail that the marker hides immediately below the graph.

    Attributes:
        glyph: The glyph of the marker: the node-type glyph that all the members share
            (``▲`` repeater, ``■`` room, …), or the plain node dot if they do not agree.
        color: The ``#rrggbb`` colour of the marker glyph (the colour of the node type, or
            the colour of the plain dot).
        label: The label with the count and the type, for example ``3 repeaters``
            (``n nodes`` when the types are mixed).
        rgb: The colour of the label: the same type colour as the marker. Thus the
            super-node reads as a group of one type, not as a named node.
    """

    glyph: str
    color: str
    label: str
    rgb: tuple[int, int, int]


@dataclass(slots=True)
class _RoutesView:
    """The selectable routes of the Routes tab and the shared draw callbacks, or a note.

    The routes are drawn as one graph (the spine with the best evidence, and the
    alternatives). The live selection of the screen sets which route is white, and which
    nodes keep the hue of their name and do not become grey. Thus the layers and the label
    colours are composed for each render, and they are not stored here.

    Attributes:
        routes: The selectable routes (strongest first), or empty when there is no route
            evidence. Then ``note`` has the muted text that is shown instead.
        glyph_of: The marker callback of the graph, for each node.
        label_of: The label callback of the graph, for each node.
        label_rgb_of: The label-colour callback, for each node (the base colour, not
            muted. The screen makes the nodes off the route grey over it).
        legend: Whether to draw the node-type legend below the graph (a relay with a known
            type was shown).
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
    """One tab in the strip of the stage.

    Attributes:
        name: The label on the strip (``Info`` or ``Routes``).
        kind: The stage that the tab draws (``info`` or ``routes``).
    """

    name: str
    kind: str


class NodeDetailScreen(Screen):
    """A full-screen page for one node: the identity, a stage with tabs, and the ways in.

    The screen only shows data and returns a choice. It renders display data that is
    already resolved (refer to :func:`open_node_detail`, which assembles it). On Enter, it
    resolves with the token of the highlighted action, and the opener does the action (a
    Trace token goes with :meth:`selected_spec`). Esc resolves :data:`CANCEL` to leave.

    There are two axes of navigation, as in the spatial controls of the whole app.
    ``Tab``/``Shift+Tab`` changes the tab that fills the stage. ``↑↓`` moves the highlight
    in the active tab, through its route list and its action rows. (On the Routes tab, the
    selection controls the white route of the graph, and Enter prepares a trace on the
    selected route.) The page itself never scrolls. The identity header, the tab strip, and
    the stage are pinned, the stage gets its size from the viewport, and the route list
    puts itself in a list window in the remaining rows. ``PgUp/PgDn`` move the highlight through
    the list by pages, and ``Home/End`` move it to the ends.

    ``←→`` scroll the one item of the active tab that is wider than its lane, one lane for
    each tab: the path line of the highlighted route on Routes, and the key on Info. Both
    keys work only when the item really overflows. Thus, when there is nothing to scroll,
    the keys have no effect, and the hint does not show them.
    """

    floating = False

    @property
    def picocalc_lyra_lane(self):
        """The shared pager over the route list, and the tab switch on F3.

        The strip shows that there are two tabs. But nothing on the screen gives the key
        that moves between them, and on a platform with no hint line, the chip is the only
        place to learn it. The chip names the tab that it goes to, never the tab that is
        open. The strip already marks the open tab, and a chip that repeats it says nothing
        about what a press of the chip does. (We tried a chip for each tab on F1/F2, then
        removed it. JP, 2026-08-09: the toggle was better.) Like each cycling chip, it is
        only the name of the target, with nothing before it. ``Routes`` is exactly six
        cells, and the strip next to it makes the direction clear. A page with one tab has
        nothing to switch, so the slot stays empty.

        The pager works only when the route list is really in a list window. On the Info tab,
        and on a node whose routes all fit, the keys move nothing.
        """
        from .tui.fkeys import FPair, default_lane

        lane = list(default_lane(nav=self._list_hidden))
        if len(self._tabs) >= 2:
            nxt = self._tabs[(self._tab_index + 1) % len(self._tabs)]
            lane[2] = FPair(nxt.name, "tab")
        return lane

    @property
    def title(self) -> str:  # type: ignore[override]
        """``Node — <name>``, or on a short frame the identity mark, ``▲ <name>``.

        On a short frame (the 14 rows of the Cardputer, JP 2026-10-04), the title bar is the
        identity. The type glyph is shown instead of the word ``Node``, and the name has its
        own hue (:meth:`bar_title`). Thus the identity line under the bar only says the same
        thing two times, and its row goes to the stage instead (refer to
        :meth:`render_body`). The frame reads this value after the paint that set it,
        because the frame draws the bar after the body.
        """
        return self._mark.plain if self._compact and self._mark is not None else self._title

    @title.setter
    def title(self, value: str) -> None:
        self._title = value

    def bar_title(self, title: str, style: str) -> Text:
        """In the bar, the identity mark keeps the type colour of its glyph and the name hue."""
        if self._mark is not None and title == self._mark.plain:
            return self._mark.copy()
        return super().bar_title(title, style)

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
        mark: Text | None = None,
    ) -> None:
        """Build the page for display data that is already resolved.

        Args:
            title: The heading of the screen (``Node — <name>``).
            header: The identity line: the type glyph, the coloured name, and its type
                label.
            mark: The identity as the title bar shows it on a short frame: the type glyph
                in its type colour and the name in its own hue, ``▲ Hilltop-Repeater``
                (refer to :attr:`title`). ``None`` keeps the plain title and the identity
                line there.
            info_rows: ``(label, value)`` pairs for the block of vitals on the Info tab.
                Each pair renders as a muted label lane, with the value hanging under itself
                when it wraps.
            tabs: The stage tabs to show, in the order of the strip (empty means no stage,
                only actions).
            minimap: The inline location preview of the Info tab, or ``None`` (no
                advertised fix).
            map_caption: A faint line under the preview (its centre and scale), when a map
                shows.
            routes: The route list and the graph callbacks of the Routes tab (or a muted
                note), or ``None`` when there is no Routes tab (our node never overhears
                itself).
            info_actions: The action rows of the Info tab (Open full map when there is a
                fix, Time machine when the recorder has history, Share contact when the
                full key is known).
            trace_action: The ``Trace — auto route …`` action of the Routes tab, or
                ``None``. It is shown only when there are no routes to list. When routes
                are listed, Enter on a route row starts the trace.
        """
        super().__init__()
        self.title = title
        self._mark = mark
        #: Whether the last paint was on a short frame with a mark to show (refer to
        #: :attr:`title`).
        self._compact = False
        self._header = header
        self._info_rows = info_rows
        self._tabs = tabs
        self._minimap = minimap
        self._map_caption = map_caption
        self._routes = routes
        self._info_actions = info_actions or []
        self._trace_action = trace_action
        #: The width in cells of the icon lane of the action rows. It is measured one time
        #: over each mark that this page can draw. Thus a one-cell ``🗑`` gets padding to the
        #: width of its two-cell siblings, and each label starts in the same cell. It is zero
        #: on a platform that draws no icon lane.
        self._icon_lane = self._measure_icon_lane()
        self._tab_index = 0
        self._row_index = 0
        #: The highlighted route on the Routes tab (it controls the graph). It follows the
        #: highlight when the highlight moves onto a route row, and it stays when the
        #: highlight is on an action row. Thus the graph continues to show the last selected
        #: route.
        self._route_sel = 0
        #: The composed route graph for one (width, rows, selection). Refer to _routes_stage.
        self._stage_memo: tuple[tuple, list[str]] | None = None
        self._cursor: int | None = None
        #: The list window of the route list over the remaining rows (its ``page`` is the
        #: step of PgUp and PgDn), and whether the list window hides rows (the scroll atom of
        #: the footer shows only then).
        self._list = ListWindow()
        self._list_hidden = False
        #: The horizontal scroll of the path line of the highlighted route (the cells moved,
        #: ``←/→``). It goes back to zero each time the highlight leaves that row (refer to
        #: :data:`_HSHIFT_RESET_ACTIONS`).
        self._hshift = 0
        #: The horizontal scroll of the key lane of the Info tab (the cells moved, ``←/→``).
        #: It is not part of the highlight, as the scroll of the route path line is. The key
        #: is pinned chrome in the stage, not a row that the highlight can go to. Thus a move
        #: through the action rows leaves it where the user put it. Only when the user leaves
        #: the tab does it go back to zero.
        self._key_shift = 0
        #: The last render width. With it, the footer hint can tell whether the path line of
        #: the highlighted route really overflows (nothing overflows before the first paint).
        self._last_width = 0

    # --- input -----------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The hint line: tab switch, move, open, the item that ``←→`` scrolls here, Esc last.

        When both apply, the scroll atom puts ``PgUp/PgDn`` and ``←→`` into one atom. (Both
        apply when the list of a busy node is in a list window and its highlighted route also
        overflows.) Two atoms can go past the hint budget of 72 cells. On the Info tab, the
        ``←→`` atom names its subject, because the key is the one thing there that scrolls,
        and the highlight is at a different place. Each atom shows only when there is
        something to scroll. Thus the line never shows a key that does nothing.
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
        """Whether the highlighted path line is too wide for the content lane of the list.

        The ``←→`` scroll and its footer atom work only when this is true, because a path
        line that fits has nowhere to scroll. The value is measured against the last render
        width. That width is ``0`` before the first paint, so nothing overflows until a real
        width is known.
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
        # The `…` at the end is chrome outside the lane (pathline.with_action_mark), so it
        # is not part of what must fit. A route exactly as wide as its lane does not scroll,
        # and the mark is the thing that is not shown.
        return cell_len(routes[self._row_index].path.plain) > avail

    def _on_info_tab(self) -> bool:
        """Whether the Info tab fills the stage (thus its key lane is visible)."""
        tab = self._tabs[self._tab_index] if self._tabs else None
        return tab is not None and tab.kind == "info"

    def _key_value(self) -> Text | None:
        """The key value of the Info tab, or ``None``. It is the one vital in a lane that scrolls.

        The method finds the row by its label, not by its position. The block is assembled
        at a different place (:func:`open_node_detail`), and the rows around the key come
        and go with what is known about the node. But the row with the label
        :data:`_KEY_LABEL` is always the key.
        """
        for label, value in self._info_rows:
            if label == _KEY_LABEL:
                return value
        return None

    def _key_overflows(self) -> bool:
        """Whether the key is wider than its lane. ``←→`` and the footer atom work only then.

        The value is measured against the last render width, exactly as the sibling check
        for the route path line is. That width is ``0`` before the first paint, so nothing
        overflows until a real width is known.
        """
        if self._last_width <= 0 or not self._on_info_tab():
            return False
        value = self._key_value()
        if value is None:
            return False
        return cell_len(value.plain) > max(1, self._last_width - _LABEL_LANE)

    def selected_spec(self) -> str:
        """The forced-path spec of the highlighted route (``""`` means automatic).

        The opener reads this value after a ``trace`` token resolves. Thus a trace prepares
        the route that the highlight selected, not always the best route.
        """
        routes = self._routes.routes if self._routes is not None else []
        if routes:
            sel = self._route_sel if 0 <= self._route_sel < len(routes) else 0
            return routes[sel].spec
        return ""

    def consume_edge_scrub(self) -> int:
        """Scrub the right edge of the panel only while the Info tab shows its braille map."""
        if self._minimap is None or not self._tabs:
            return 0
        return 2 if self._tabs[self._tab_index].kind == "info" else 0

    def _measure_icon_lane(self) -> int:
        """The icon lane of the action rows, measured over each mark that is now on the page."""
        trace = [self._trace_action] if self._trace_action else []
        return menus.icon_lane(action.glyph for action in (*self._info_actions, *trace))

    def replace_info_row(self, label: str, value: Text) -> None:
        """Replace the value of one vital in place: the regions row, after the repeater answered.

        The row keeps its place in the block (a label that is not there yet is added at the
        end). The highlight stays on the row that it was on, because the page is the same
        visit, with one newer fact.
        """
        for i, (existing, _value) in enumerate(self._info_rows):
            if existing == label:
                self._info_rows[i] = (label, value)
                return
        self._info_rows.append((label, value))

    def replace_info_actions(self, actions: list[_Action]) -> None:
        """Replace the action rows of the Info tab in place, with the highlight on the same row.

        This is for an action that changes what the page offers, but does not end the
        visit. For example, when the user locks a contact, its row becomes Unlock, and
        Archive goes away. The highlight follows the committed row by its
        :attr:`_Action.key` family (``lock`` and ``unlock`` are one row). Thus the next key
        press of the user goes to the same row as the last.
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

        Change the tab, move the highlight in a tab, commit a row, page the list, scroll
        the lane of the active tab that is too wide (the path line of the highlighted
        route, or the key of the Info tab), or leave.
        """
        focus = self._focusables()
        n = len(focus)
        if action in _HSHIFT_RESET_ACTIONS:
            self._hshift = 0  # each row has its own scroll, lost when the highlight leaves
        if action == "enter":
            if n:
                kind, payload = focus[self._row_index % n]
                if kind == "path":
                    # A route row itself starts the trace. Enter prepares a trace on the route
                    # that the row names (the opener reads :meth:`selected_spec` to get it).
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
                # Both ends clamp. The highlight does not go around to the other end,
                # because the route rows scroll in a list window (refer to ListWindow). If the
                # highlight jumps from one end to the other, the list window goes with it.
                # That is the only move that looks like a change of the whole screen.
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
            # On a terminal that is too short for the pinned layout, the frame follows the
            # highlight. Home must show the very top of the page, with the header.
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
                self._hshift += _HSCROLL_STEP  # clamped to the path line end at render
            elif self._key_overflows():
                self._key_shift += _HSCROLL_STEP  # clamped to the end of the key at render
        elif action == "escape":
            self.resolve(CANCEL)

    def _on_route_row(self, focus: list[tuple[str, object]]) -> bool:
        """Whether the highlight is now on a route row (and not on an action row)."""
        return bool(focus) and focus[self._row_index % len(focus)][0] == "path"

    def cursor_line(self) -> int | None:
        """The body line of the highlighted row, which the frame keeps visible.

        That has an effect only on a terminal that is too short for the minimums of the
        pinned layout.
        """
        return self._cursor

    def _switch_tab(self, delta: int) -> None:
        """Change the active tab, and move the highlight and the list window to its top."""
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
        """Make the route under the highlight the white route of the graph, if it is on one."""
        if focus:
            kind, payload = focus[self._row_index % len(focus)]
            if kind == "path":
                assert isinstance(payload, int)
                self._route_sel = payload

    def _focusables(self) -> list[tuple[str, object]]:
        """The highlight stops: ``("path", route_idx)`` and ``("action", _Action)``.

        These are the stops of the active tab. On the Routes tab, the route rows come first,
        and each row itself starts the trace. The auto-route Trace action is there instead
        only when there are no routes to list. The Info tab has its own actions (for example
        Open full map and Time machine). No tab has a Back row, because Esc closes the page.
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
        """Render the pinned chrome, the stage sized to fit, and the rows in a list window.

        At each paint, the viewport that the frame noted
        (:meth:`~meshterm.ui.tui.screen.Screen.note_viewport`) is divided into three
        parts. First, the pinned chrome (the identity header and the tab strip) and the
        action rows get their fixed lines. Then the stage gets what it needs of the
        remaining lines. (The row limit of the route graph becomes smaller to fit, and the
        location preview of the Info tab grows into the rows that the vitals leave.) Last,
        the route list puts itself in a list window in the remaining lines. Thus nothing here
        ever pushes the graph or the actions off the screen.
        """
        viewport = self._scroll_viewport
        self._last_width = width
        focus = self._focusables()
        if focus:
            self._row_index %= len(focus)
        self._cursor = None
        self._list_hidden = False
        short = short_frame(viewport)
        # On a short frame, the title bar shows the identity (refer to :attr:`title`). Thus
        # the identity line under the bar is removed, and so is the rule under the route
        # graph. Both rows go to the graph, whose row limit comes from the remaining rows.
        self._compact = short and self._mark is not None

        # -- pinned chrome: the identity header, then the tab strip. (A single tab collapses
        # to one line. A strip of more tabs puts the active tab in a box of three lines.)
        # The budget below is calculated from ``len(lines)`` after this, so the size is
        # correct for each shape.
        lines: list[str] = []
        if not self._compact:
            lines.extend(render_lines(self._header, width))
        if self._tabs:
            lines.extend([""] * tab_air())
            lines.extend(
                render_lines(
                    tab_strip(
                        [t.name for t in self._tabs],
                        self._tab_index,
                        width,
                        compact=short,
                    ),
                    width,
                    no_wrap=True,
                )
            )

        # The action rows are also fixed chrome: a blank line first, then one line for each
        # row. They are subtracted before the stage draws, so that the stage can get its
        # size against them.
        actions = [payload for _kind, payload in focus if _kind == "action"]
        action_lines = (1 + len(actions)) if actions else 0

        # -- the stage, sized to the rest of the viewport, closed by a faint rule.
        route_blocks: list[list[str]] = []
        tab = self._tabs[self._tab_index] if self._tabs else None
        if tab is not None:
            if tab.kind == "info":
                budget = viewport - len(lines) - action_lines - 1  # the line of the rule
                lines.extend(self._info_stage(width, budget))
            else:
                route_blocks = [
                    self._route_row_lines(route, i == self._row_index, width)
                    for i, route in enumerate(self._routes.routes if self._routes else [])
                ]
                if not route_blocks:
                    # The note alone pays for its own space under the strip, where the
                    # platform gives the strip space.
                    lines.extend([""] * tab_air())
                rule = 0 if short else 1  # on a short frame, the graph keeps the rule line
                budget = viewport - len(lines) - action_lines - rule
                lines.extend(self._routes_stage(width, budget, route_blocks))
            # The rule closes the stage, so the drawn tab and the rows below it read as
            # separate bands, not as one continuous column. The Routes tab on a short frame
            # has no rule (JP, 2026-10-04). The white line of the selected route and the
            # highlight of the list already divide the two, and the row is more useful to
            # the graph.
            if tab.kind == "info" or not short:
                lines.append(render_to_ansi(Text("─" * width, style="faint"), width))

        # -- the route list, in a list window in the rows that the stage left.
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

        # -- the pinned action rows (the route rows come before them in the focus order).
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
        """The block of vitals and, with a fix, the location preview grown to fit.

        ``budget`` is the number of visible lines that remain for the full stage. The rows
        of vitals are never truncated. The preview changes its size instead: it takes the
        lines that the vitals and its caption leave, up to ``_MAP_MAX_ROWS``. When that is
        less than ``_MAP_MIN_ROWS``, the preview is not shown at all (a short frame keeps
        its header and actions visible instead). Each row, but not the key, hangs under
        itself when it wraps. The key keeps its one line and scrolls (refer to
        :meth:`_key_line`). Thus the row count that sets the size of the preview cannot
        change while the user scrolls through a long key.
        """
        lines: list[str] = [""] * tab_air()  # the space of the stage under the strip, if any
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
            caption = 1 if self._map_caption is not None else 0
            room = budget - len(lines) - 1 - caption  # minus the blank line above it
            # Below its minimum, the preview is not shown. It does not push the identity
            # header and the actions off a short frame (the 14 rows of the Cardputer leave
            # it two).
            if room >= _MAP_MIN_ROWS:
                lines.append("")
                lines.extend(self._minimap.render(width, min(_MAP_MAX_ROWS, room)))
                if self._map_caption is not None:
                    lines.extend(render_lines(self._map_caption, width, no_wrap=True))
        return lines

    def _key_line(self, label: str, value: Text, width: int) -> str:
        """The key vital as one lane that scrolls: the label, then the part that ``←→`` set.

        A full public key has 64 hex digits. The lane is the width that the terminal leaves
        after the label: 63 cells at the standard width of 72 cells, fewer on the PicoCalc.
        Thus the key never fits. A wrap uses a full second line for the few digits that do
        not fit. Instead, the lane shows a part of the key, and :attr:`_key_shift` moves
        that part (``←→``, :data:`_HSCROLL_STEP` cells for each key press). A faint
        :data:`_MORE_MARK` is at each edge past which the key continues. This mark tells the
        user that the row is a viewport, not the full value.

        The marks are next to the visible part, not over its first cell. Thus the digits
        keep their byte alignment at each shift (the step is four full bytes). A key always
        reads in byte pairs, and a part that starts in the middle of a byte shows it wrongly.
        The shift is clamped here, against the width that is really drawn, to the first
        step that brings the end of the key into the lane. Thus a scroll can never go into
        an empty lane, and a resize can only pull a shift that is out of range back in.
        """
        lane = Text(no_wrap=True)
        lane.append(f"{label:<{_LABEL_LANE}}", style="muted")
        avail = max(1, width - _LABEL_LANE)
        total = cell_len(value.plain)
        if total <= avail:
            self._key_shift = 0
            lane.append_text(value)
            return render_to_ansi(lane, width, no_wrap=True)
        # When the lane scrolls, it gives one cell to the left mark immediately. Thus its last
        # visible part has ``avail - 1`` cells. The limit is the first full step that gets to
        # the end.
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
        """The route graph, sized to fit, or the muted note when there is no evidence.

        ``budget`` is what the viewport leaves for the stage and the route list together.
        The row limit of the graph is what remains after its caption and legend, and after
        the minimum of the list (the smaller of :data:`_LIST_MIN_LINES` and what the rows
        really need). The limit is never less than :data:`_GRAPH_MIN_ROWS`. Thus the graph
        compresses the pitch of its graph lanes before the list loses its list window, and
        the graph of a busy node spreads out only on a terminal that is tall enough. As
        always, the selected route is white, and the labels that are not on it are grey.

        Where the caption is removed (:data:`_GRAPH_CAPTION`, on the PicoCalc), the graph
        is drawn one row more than its limit, and then its first blank canvas row is
        removed. The renderer always centres the band of graph lanes in two cells of end
        padding, but its top label needs only one. Thus that row is always empty, and its
        removal pays for the row that the graph got. Both rows go back into the limit
        itself.
        """
        rv = self._routes
        assert rv is not None
        if not rv.routes or rv.glyph_of is None:
            return render_lines(Text(rv.note or "no route observed yet", style="muted"), width)
        sel = self._route_sel if 0 <= self._route_sel < len(rv.routes) else 0
        # The routes data is a snapshot (the opener built it one time). Thus the full graph
        # (a graph layout in many passes) is a pure function of the width, the row budget,
        # and the emphasized route. One slot is sufficient: the cache key changes only on a
        # change of selection or a resize, and then the previous layout is not used again.
        # The node-type legend is not shown on a short frame, the same as in the message
        # paths dialog. The markers have one meaning in the whole app, and the rows go to
        # the graph and the list.
        legend = rv.legend and not short_frame(self._scroll_viewport)
        caption_lines = (1 if _GRAPH_CAPTION else 0) + (1 if legend else 0)
        list_need = min(sum(len(block) for block in route_blocks), _LIST_MIN_LINES)
        max_rows = max(_GRAPH_MIN_ROWS, min(_PATH_MAX_ROWS, budget - caption_lines - list_need))
        stage_key = (width, max_rows, sel, legend)
        if self._stage_memo is not None and self._stage_memo[0] == stage_key:
            return self._stage_memo[1]
        # The order of the evidence sets the priority (route 0, the route with the best
        # evidence, is always the spine). Thus the shape of the graph never moves when the
        # selection changes. Only the emphasis follows the selection: which route is white
        # and on top, and which nodes keep their hue.
        layers = [
            PathLayer(
                hops=route.draw,
                color=_WHITE if i == sel else _GREY,
                priority=len(rv.routes) - i,
                emphasis=1 if i == sel else 0,
            )
            for i, route in enumerate(rv.routes)
        ]
        # A node keeps its hue only while it is on the selected route. All that is off the
        # route (marker and label) becomes grey. This is now the rule of the widget itself
        # (refer to :func:`~meshterm.ui.pathgraph.render_path_graph`), and the widget
        # decides it in the id space of the graph. Thus a route that gets to a relay by a
        # short hash still gives the hue to the wide marker into which its hop is folded.
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
        if legend:
            lines.extend(render_lines(node_type_legend(width=width), width, no_wrap=True))
        self._stage_memo = (stage_key, lines)
        return lines

    def _route_row_lines(self, route: _Route, selected: bool, width: int) -> list[str]:
        """One route row: a path line with the ``❯`` pointer, and its context on the line below.

        The route keeps the colours of its nodes, also when it is selected. (The pointer and
        the white line in the graph above show the selection.) The plain action rows are
        different: they take the highlight. Here, the colour is the content. The ``…`` at the
        end is the mark of the whole app that says that the row opens more prompts: Enter on
        the row prepares a trace on it. The mark is outside the lane
        (:func:`~meshterm.ui.pathline.with_action_mark`), so the chrome takes no cells from
        the route. Thus a chain that fits stays whole and ends on its rounded cap. It does
        not crack to make space for two cells of hint. Where the lane is full, the mark is
        the thing that is not shown.

        The path line never wraps at a hop. It is one line, cropped. The highlighted row
        moves with :attr:`_hshift` (``←→``, refer to :meth:`handle`). Thus the user can read
        a named chain that is wider than the lane to its end, under a
        :func:`~meshterm.ui.pathline.cut_mark` at each edge past which the line continues.
        That mark is a chip broken off in its own colour where the route is drawn in chips,
        and the faint ``…`` where it is drawn in arrows. It is the same horizontal scroll as
        in the key lane of the Info tab, and the same as in the rows of Message paths. That
        scroll is what lets the row show names (:func:`_route_line`). A hop chain written as
        names becomes longer than a lane much sooner than a chain written in hash bytes. The
        solution is to read it, not to make it shorter.

        All the other rows are cut to the lane (:func:`~meshterm.ui.pathline.cut_to`,
        cracked or with an ellipsis by the same rule). The context line under each row
        (plain prose, never a path) gets an ellipsis. Only the highlighted row can scroll,
        and it is the only row whose end the user asks to see. The context (hop count,
        bottleneck SNR, sample count, the ``★ best`` or ``device route`` tag) always starts
        with the count. Thus each row is two lines tall, and the height of the list does not
        change while the highlight moves through it.
        """
        indent = _ROUTE_INDENT
        avail = max(1, width - indent)
        pointer = Text("❯ " if selected else "  ", style="cursor" if selected else "")
        marked = route.path
        if selected and self._hshift:
            total = cell_len(marked.plain)
            # A scrolled row gave one cell to its left mark, so its last visible part has
            # ``avail - 1`` cells. Clamp to the first full step that brings the end into that
            # part. If the clamp is less than that step, the right mark is still drawn at
            # the far end, and it shows more text that the keys cannot get to. (The key lane
            # clamps the same way.)
            steps = -(-max(0, total - (avail - 1)) // _HSCROLL_STEP)
            self._hshift = max(0, min(self._hshift, steps * _HSCROLL_STEP))
            shift = self._hshift
            # The marks are chrome in the visible part, not more width. Each mark takes a cell of
            # the lane, so the crop is measured after the marks are known. If not, the line
            # drawn under a right ``…`` goes one cell past the row.
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
        """One action row: ``❯``, the icon, and the label, all white when it is highlighted."""
        text = Text("❯ " if selected else "  ", style="cursor" if selected else "")
        # The icon lane is decoration, and a platform can remove all of it (command_icon).
        # The label names the action, so an empty lane gives its cells to the label. But a
        # tinted icon has a meaning that does not go away with the decoration: a red 🗑 is
        # how a destructive row shows what it is. Thus, where the lane is removed, the label
        # gets the tint instead. :func:`~meshterm.ui.menus.marked_label` does the same for a
        # menu row, for the same reason: a delete must not look like all other actions.
        #
        # The icon gets padding to the lane of this page. It is not written as
        # `icon + " "`, because this page has a one-cell 🗑 and two-cell siblings. Without
        # the padding, the label started one cell too early (JP, 2026-09-01).
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
    """The 8-point compass direction from ``(lat1, lon1)`` to ``(lat2, lon2)``."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    deg = (math.degrees(math.atan2(y, x)) + 360) % 360
    return _COMPASS[round(deg / 45) % 8]


def _range_text(lat: float, lon: float, self_lat: float | None, self_lon: float | None) -> Text:
    """A location: the coordinates, and the range and bearing from our node, if it has a fix."""
    text = Text(f"{lat:.4f}, {lon:.4f}", style="")
    if self_lat is not None and self_lon is not None:
        km = haversine_km(self_lat, self_lon, lat, lon)
        dist = f"{km * 1000:.0f} m" if km < 1 else f"{km:.1f} km"
        text.append(f"  ·  {dist} {_bearing(self_lat, self_lon, lat, lon)}", style="muted")
    return text


def _as_float(value: object) -> float | None:
    """Change the raw lat/lon that a device reports to a float, if possible (``None`` if not)."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _located(lat: float | None, lon: float | None) -> bool:
    """Whether a coordinate is a real fix that a map can show.

    The check is :func:`~meshterm.core.geo.usable_fix`. It refuses the 0/0 null island that
    a node with no GPS reports. It also refuses the values out of range that a companion
    with a bad configuration sometimes advertises (``lat -97, lon -1042``, heard on a real
    mesh). Each of these puts the location preview on an all-black area outside the world.
    """
    return lat is not None and lon is not None and usable_fix(lat, lon)


# -- data gathering + the action loop -----------------------------------------


async def open_node_detail(
    ctx: AppContext, contact: Contact | None, *, manage: bool = True
) -> bool:
    """Open the Node detail page for a contact (or our node) and run its action loop.

    The function assembles the page from the stored history, the contacts of the device,
    and the observed topology: the identity, the reception statistics, a location preview,
    and the observed routes. Then it runs a loop. It shows the page, runs the action that
    the user commits (trace, full map, time machine), and shows the page again, until the
    user leaves with Esc. The Time Machine and the Contacts list use the same loop of show,
    act, and show again.

    **Three actions end the loop instead of a return to it**, because after each of them,
    the page describes a state that is not true now. These actions archive the contact off
    the device, restore it, and delete it permanently. All three are *contact management*.
    They are the last group on the Info tab, and the page shows only the ones that apply.
    The user can archive or delete a live contact, and restore or delete an archived
    contact.

    **A lock is management that stays.** The user can lock a live contact against an
    archive (refer to :meth:`~meshterm.core.contact_store.ContactStore.set_locked`). Then
    the sweep never counts it as a candidate, and this page removes its Archive row for as
    long as the lock stays. The toggle writes the action rows of the page again in place,
    and it does not end the visit, because nothing about the contact on the page is gone.
    The list below draws its padlock again when the user comes back to it.

    With ``manage``, a caller says that it opens the page to look, not to act. The archive
    preview gives ``False``. A screen whose full purpose is to select what to archive must
    not also give a second, single way to archive (or to delete) from its own list of
    candidates. The rule is that the management verbs belong to the list that the contact
    is really in. They never belong to a page opened from a list that is about to act on
    all its rows together.

    Args:
        ctx: The shared application context (the interactive TUI must be running).
        contact: The contact to show, or ``None`` for our node (a page of identity and
            ledger only, because our node never overhears itself, so there is no reception
            history to show).
        manage: Whether to show the contact-management actions (lock, archive, restore,
            delete). ``False`` draws none of them, and then the page can only return
            ``False``.

    Returns:
        ``True`` if the visit ended because the user archived, restored, or deleted the
        contact. This tells the caller to read and build again the list that it came from.
        ``False`` on each ordinary exit.

    Raises:
        RuntimeError: If it is called outside the interactive menu (no full-screen
            session).
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

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - the caller is only in the menu
        raise RuntimeError("the node detail screen is only available in the menu")
    session = ctx.ui.session

    you = contact is None

    # -- our node, for the "us" label, the centre of the location preview, and the range
    # and bearing.
    try:
        info = await ctx.devstate.self_info()
    except Exception:  # noqa: BLE001 - the page is useful without our identity and fix
        info = {}
    self_name = str(info.get("name") or "this node")
    self_key = str(info.get("public_key") or "")
    self_lat, self_lon = _as_float(info.get("adv_lat")), _as_float(info.get("adv_lon"))
    if not _located(self_lat, self_lon):
        self_lat = self_lon = None

    # -- the contacts of the device (to resolve the name, type, and key) and the width of the
    # routing hash.
    try:
        contacts = await ctx.devstate.contacts()
    except Exception:  # noqa: BLE001 - resolution uses the stored data instead
        contacts = []
    try:
        mode = int(await ctx.devstate.path_hash_mode())
        prefix_bytes = (mode + 1) if 0 <= mode <= 3 else 0
        width_bytes = collapse_trace_width(mode)
    except Exception:  # noqa: BLE001 - optional reads. Safe defaults keep the page working.
        prefix_bytes, width_bytes = 0, 1

    stored_names = ctx.repo.node_names()
    # One resolver from hash to name for the full page. The route list and the route graph
    # all name their hops through it (the contacts first, then the history of the recorder).
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

    # -- the reception statistics and the first-heard time, from the history of the recorder.
    heard = {n.node: n for n in ctx.repo.heard_nodes() if n.node}
    hn = heard.get(node_id)
    firsts = {n: when for n, _n, when in ctx.repo.first_seen()}
    first_heard = firsts.get(node_id)
    if you:
        hn = None  # our node never overhears itself, so there is no reception history
    # A node with a fix, but no location from the observations, still shows the map. But it
    # does so only when that overheard fix is real (an advert with no GPS or with bad values
    # never places it).
    if lat is None and hn is not None and hn.has_location and _located(hn.lat, hn.lon):
        lat, lon = hn.lat, hn.lon

    label = name or node_id or key or "?"

    # -- the identity header.
    if you:
        glyph, glyph_style = SELF_MARK
        name_hue = "you"
    else:
        glyph, glyph_style = NODE_GLYPHS.get(node_type, DEFAULT_GLYPH)
        name_hue = name_style(name, key) if name else "muted"
    # The mark is the identity in the title bar of a short frame. The header line adds the type.
    mark = Text()
    mark.append(f"{glyph} ", style=glyph_style)
    mark.append(name or "unknown", style=name_hue)
    header = mark.copy()
    type_label = NODE_TYPE_LABELS.get(node_type) if node_type is not None else None
    if you:
        header.append("   your node", style="muted")
    elif type_label:
        header.append(f"   {type_label}", style="muted")

    # -- the observed topology: the suggested best path, and the routes to draw.
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

    # -- the info block (the identity, how the node was heard, and where it is). The routes
    # go into the Routes tab.
    info_rows: list[tuple[str, Text]] = []
    if key:
        info_rows.append(("key", highlighted_hash(key, prefix_bytes, known=bool(name))))
    else:
        info_rows.append(("key", Text("?", style="muted")))
    if not you:
        # The heard time of a contact is already merged with our reception history (refer
        # to ``DeviceState._merge_heard``). Thus it is the later of the two, and it is what
        # the lane of the contact list shows. Use it, and the two surfaces can never
        # disagree about one node. Only a node that is not a contact, or a node whose merge
        # could not read our history, uses the raw reception statistic instead.
        heard_at = contact.last_seen if contact else None
        secs = age_seconds(heard_at or (hn.last_seen if hn else None))
        heard_val = Text(format_ago(secs), style=_recency_style(secs))
        if first_heard is not None:
            heard_val.append(f"  ·  first {first_heard.astimezone():%b %d %Y}", style="muted")
        info_rows.append(("heard", heard_val))
        # A node that was never overheard shows a faint em dash, as the contact list draws it.
        packets = Text(str(hn.count)) if hn else Text("—", style="faint")
        info_rows.append(("packets", packets))
        signal = _signal_row(hn)
        if signal is not None:
            info_rows.append(("signal", signal))
    if lat is not None and lon is not None:
        info_rows.append(("where", _range_text(lat, lon, self_lat, self_lon)))
    # The regions of a repeater: what it was last heard to relay, from the region store (its
    # own answer to the regions request, or its table read on the admin page). Only a
    # repeater answers the request, so only the page of a repeater asks it.
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
        except Exception:  # noqa: BLE001 - a preview of only this node is still useful
            markers = []
        source = basemap_source(ctx)
        try:
            max_zoom = await asyncio.to_thread(lambda: source.max_zoom)
        except Exception:  # noqa: BLE001 - offline. The markers go on a blank grid.
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

    # -- the route list of the "routes heard" and the graph (node → us), with the path that
    # has the best evidence in white.
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

    # -- the tabs (Info always, Routes only when there is routes data) and their actions.
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
    # The share card encodes the full 32-byte key. A contact with only a key prefix (heard,
    # but never synced from the device) has nothing that a camera can scan. Thus the row
    # shows only when the full key is known.
    full_key = key.lower().removeprefix("0x")
    if len(full_key) == 64 and is_path_hash(full_key):
        info_actions.append(_Action("share", "📱", "", "Share contact — QR / link"))
    if asks_regions:
        # It acts immediately (one anonymous request, one transmission), so no "…" at the
        # end.
        info_actions.append(_Action("regions", "🔖", "", "Ask which regions it carries"))
    # -- contact management: the last group of the page, and the only actions that end the
    # visit. None of them shows when the caller opened the page to look, not to act (refer
    # to ``manage``). They never show for our node, or for a contact with no key at
    # all, because the write has nothing to use as its address.
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
    # A lock is a mark of MeshTerm itself, attached to the full key in the contact store, and
    # only a live contact can have one. Its purpose is to keep a contact out of the archive.
    lockable = manageable and not archived and whole_key and store is not None and bool(dev_pub)
    base_actions = list(info_actions)

    def management(locked: bool) -> list[_Action]:
        """The action rows of the page, with the management group for this lock state last."""
        rows = list(base_actions)
        if not manageable:
            return rows
        # The lock is first. It changes nothing on the device and ends nothing, so it is
        # the safest row of the group for the highlight. Then the constructive verb, then
        # the destructive verb. Thus a key press by mistake most probably goes to the row
        # that the user can undo. Archive and restore are the two halves of one move that
        # the user can reverse. They use the ``💾``/``📂`` pair that the lexicon already
        # gives to "put away" and "bring back". Only a contact with a full key can be
        # written back to the device. Thus a contact with only a key prefix never gets the
        # archive, because the archive leaves it with no way back. A locked contact
        # never gets the archive at all, and that is the purpose of the lock.
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
    # When routes are listed, each route row itself starts a trace (Enter prepares it). The
    # separate action is shown instead only when there is no route evidence to list.
    trace_action: _Action | None = None
    if not you and node_id and not (routes_view is not None and routes_view.routes):
        trace_action = _Action("trace", "🎯", "", "Trace — auto route …")
    title = f"Node — {label}" if not you else f"Node — {label} (you)"
    # Whether the visit ended with a delete of the contact. This tells the caller to build
    # its list again.
    contact_removed = False
    # One screen for the full visit. The page is a *hub*: each action on it opens a different
    # screen and comes back. Thus the page stays pushed while each action runs
    # (``session.stay``), and it is not built again for each round. Before, each new build
    # lost the open tab and the highlight. The user went to Routes with Tab, selected a
    # route, traced it, and came back. Then the user was on Info at the top again, two key
    # presses away from the route of the work. When the object stays, all of this stays.
    # Also, the peers below *nest* above this page and do not replace it. Thus Esc from a
    # trace comes back here, and ^W leaves the full sequence at one time.
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
        mark=mark,
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
                    # Open the map centred on this node (the same place that the inline
                    # preview showed), not at the place where the global map was last left.
                    # Set the find filter to this node, so that it is lit among the others.
                    # Set the filter only when the label really matches a marker, because a
                    # search text that matches nothing makes all the markers dim. (The map
                    # action exists only when the node has a fix, so lat/lon are set.)
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
                assert contact is not None and region_store is not None  # offered only then
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
                # A restore writes the contact back, and it ends the visit for the same reason
                # as a delete. The page was opened from the Archived list, and the row that
                # it was opened from will not be in that list any more.
                assert contact is not None  # the row exists only for a real contact
                if await _restore_archived(ctx, contact, self_key, label):
                    contact_removed = True
                    break
            elif action in ("lock", "unlock"):
                # No confirm and no notice. The same row that set the lock can reverse it,
                # and the change of the row into its opposite is the acknowledgement.
                assert contact is not None and store is not None  # offered only when lockable
                locked = action == "lock"
                store.set_locked(dev_pub, contact, locked)
                screen.replace_info_actions(management(locked))
            elif action == "archive":
                assert contact is not None
                if await _archive_contact(ctx, contact, self_key, label):
                    contact_removed = True
                    break
            elif action == "remove":
                # The confirm floats over this page, which is already the backdrop. Thus the
                # user confirms while the node is visible. A removal ends the visit, because
                # there is no contact left to show, and the list that MeshTerm goes back to
                # is built again without it.
                assert contact is not None  # the row exists only for a real contact
                contact_removed = await _remove_contact(ctx, contact, self_key, label)
                if contact_removed:
                    break
    if minimap is not None:
        # The braille of the preview may have left marks on the terminal (double-width
        # fallback glyphs that the diff of prompt_toolkit cannot see). Force one clean paint
        # of the list below, exactly as the full map does when it closes.
        session.request_full_repaint()
    return contact_removed


def _is_archived(ctx: AppContext, contact: Contact | None, self_key: str) -> bool:
    """Whether the sweep removed this contact from the device, and MeshTerm keeps it.

    The value is read directly from the contact store, and the caller does not give it.
    Thus the page has the correct answer from each place that it can be opened from (the
    ``Archived`` section of the Contacts list, a map marker, a route row), not only from a
    place where the caller knew the answer.
    """
    store = getattr(ctx, "contact_store", None)
    dev_pub = (self_key or "").lower().removeprefix("0x")
    if store is None or contact is None or not dev_pub or not contact.public_key:
        return False
    key = contact.public_key.lower().removeprefix("0x")
    return any(c.public_key == key for c in store.archived(dev_pub))


async def _archive_contact(ctx: AppContext, contact: Contact, self_key: str, label: str) -> bool:
    """Confirm and archive one contact off the device. ``True`` when it is off the device.

    This is the single-contact counterpart of the bulk sweep (refer to
    :func:`~meshterm.ui.sweep_screen.archive_contacts`). It does exactly what the sweep does
    to each contact that it removes. It removes the contact from the device, then stores it
    in the cross-session store with an archive stamp, so that nothing is really lost.

    The confirm is **amber, not red, and it asks for no typed text**. The two caution tiers,
    one above the other, are for two different costs, and this cost is recoverable. The
    contact keeps its key, its reception history, and its transcripts. The Archived list is
    one row away on the Contacts screen, and a restore is one write. Red and a typed word
    are for :func:`_remove_contact`, which is the action that cannot be undone.

    Args:
        ctx: The shared application context (interactive menu, with the page as the
            backdrop).
        contact: The contact to archive, addressed by the key that it carries.
        self_key: The public key (hex) of the device itself. The contact store uses it
            to keep the remembered contacts of each device apart.
        label: The name of the node on the page, for the prompt and the failure notice.

    Returns:
        ``True`` if the contact was archived. ``False`` if the user cancelled or the device
        refused. A device that does not have the contact is not a refusal: the archive
        completes on our side, because the contact is not on the device, and that is the
        state that the archive asked for.
    """
    from .surface import TuiUi

    assert isinstance(ctx.ui, TuiUi)  # open_node_detail makes sure of this
    session = ctx.ui.session

    # The contact is named as the Contacts list names it: the type mark in its own colour,
    # then the name in the hue that comes from its key. (A bare id that is shown for a
    # contact with no name is ``node.unknown``.) Each span of the prose has the amber,
    # instead of the base style of the prompt, because Rich merges the bold of the base
    # style into the mark.
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
        # The device does not have the contact, and the archive wanted that result. The
        # store write below is all the work that remains.
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
    """Write an archived contact back to the device. ``True`` when it is live again.

    This is the inverse of the Contacts sweep, for one contact at a time (refer to
    :mod:`~meshterm.ui.sweep_screen`). The device write comes **first**, and the archive
    mark in the store is cleared only after the write succeeded. A failed write must never
    leave a contact listed as live on a device that does not have it. That row is a contact
    that the user cannot send a message to and cannot restore.

    No confirm: a restore is constructive, the sweep that archived the contact can reverse
    it, and it uses one contact slot. The dialog stays for the failures. They are the only
    part that the user cannot see from the list when the list comes back with the row in it.

    Args:
        ctx: The shared application context (interactive menu, with the page as the
            backdrop).
        contact: The archived contact to write back.
        self_key: The public key (hex) of the device itself.
        label: The name of the node on the page, for the failure notice.

    Returns:
        ``True`` if the contact is back on the device, ``False`` if the write was refused.
    """
    from .surface import TuiUi

    assert isinstance(ctx.ui, TuiUi)  # open_node_detail makes sure of this
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
    """Confirm and remove one contact from the device. ``True`` when it is gone.

    This is the single-contact counterpart of the bulk sweep of the Contacts list (refer to
    :func:`~meshterm.ui.sweep_screen.archive_contacts`). But it deletes, where the sweep
    archives. It is the way to make MeshTerm forget a node completely. Thus it removes the
    contact in two places, because the list that a screen sees is the *union* of the two:
    the contact table of the device itself, and the contacts that MeshTerm remembers for
    that device (refer to :mod:`meshterm.core.contact_store`). If only the first forgets
    the contact, the store merges the contact back at the next read. Then the delete looks
    like a screen that did nothing.

    Only the *contact* goes. The reception history of the node, its overheard traffic, and
    the chat messages with it all belong to MeshTerm, and they do not change. What is lost
    is the ability of the device to address the node. Thus, when a chat sends to a contact
    that the firmware does not have now, it offers to write the contact back and does not
    fail (refer to :func:`~meshterm.ui.chat._restore_contact`).

    **It cannot be undone, and the confirm says so.** Nothing here can derive again a key
    that the store forgot. Thus this is the only contact action with no way back, and that
    is exactly what makes it different from :func:`_archive_contact`, whose amber confirm
    is one row above it on the same page. It has the red that is reserved for data loss:
    Cancel on the left, and the committing Delete on the right as the default.

    Args:
        ctx: The shared application context (interactive menu. The caller pushes the page
            as the backdrop of the confirm).
        contact: The contact to delete, addressed by the key that it carries.
        self_key: The public key (hex) of the device itself. The contact store uses it
            to keep the remembered contacts of each device apart.
        label: The name of the node on the page, for the prompt and the failure notice.

    Returns:
        ``True`` if the contact was removed. ``False`` if the user cancelled or the device
        refused. The refusal is always shown, because a contact that is still on the device
        must not go away from the list. A device that does not have the contact is not a
        refusal: the removal completes on our side and says so
        (:class:`~meshterm.core.connection.ContactNotOnDeviceError`).
    """
    from .surface import TuiUi

    assert isinstance(ctx.ui, TuiUi)  # open_node_detail makes sure of this
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
        # There is nothing to delete on the device. The entry existed only in what MeshTerm
        # remembers for this device (or the firmware removed it after MeshTerm read the
        # table). That is not a failed removal. The contact still goes, and MeshTerm tells
        # the user why the device had no part in it. If not, "removed" is a claim about a
        # device that never had the contact.
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
    # The table of the device changed, and the session cache does not have the change. The
    # list that MeshTerm goes back to reads it again.
    ctx.devstate.invalidate_contacts()
    # No notice of success. The result of the archive sweep is a count that nobody can know
    # before, but this result is clear. The page of the contact closes, and the list behind
    # it comes back without the row. That says it better than a dialog that the user must
    # close.
    return True


#: Why a repeater may not answer the regions request, in the words that the page uses.
_REGIONS_REACH = "it answers only a neighbour, or over a known route"


def regions_value(store, node_id: str, *, routed: bool, failed: bool = False) -> Text:  # noqa: ANN001 - RegionStore
    """The ``regions`` vital of a repeater: what it relays, whether also unscoped, and its age.

    Four states, each in its own words, so that no state reads as a different one:

    * **Answered**: the regions that it named, then whether it also relays *unscoped*
      floods (``unscoped too`` or ``scoped only``, from the ``*`` at the start of its
      answer, which names no region), then when it said so. A repeater that relays
      unscoped floods and no region reads ``unscoped floods only``. A repeater that named
      nothing reads that it relays no floods.
    * **Not asked**: nothing is stored (``not asked yet``). A repeater with no known route
      also says why the question may get no answer (the repeater answers the request only
      when it arrives direct).
    * **No answer**: the question was asked on this visit and no answer came back, from a
      repeater that never answered before. The row also says why the question may get no
      answer.
    * **No answer now**: the question was asked on this visit and no answer came back,
      but the repeater answered before. The row keeps the earlier answer and adds
      ``no answer now``. The age of the earlier answer shows that it is stale, because a
      repeated question with no answer says nothing about what the repeater carries.

    Args:
        store: The region store.
        node_id: The 12-hex id of the repeater.
        routed: Whether the device has a route to it (a neighbour or a learned path).
        failed: Whether the question on this visit got no answer.

    Returns:
        The value of the row.
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
    """Ask a repeater one time which regions it carries, and learn the answer.

    One anonymous request and one transmission, under the busy overlay. The answer replaces
    what the store had for this repeater
    (:meth:`~meshterm.core.region_store.RegionStore.learn_carried`). When the request gets
    no answer, a dialog says so, with the most probable reason: the repeater ignores the
    request if it does not arrive direct, and the repeater limits the rate of these
    requests. Nothing tries the request again.

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
    """The median and best reception SNR and the last RSSI, or ``None`` if nothing was measured."""
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
    """The hash of a key at ``hash_bytes`` width, or ``fallback`` when there is no usable key."""
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
    resolve,  # noqa: ANN001 - NodeResolver, with no strict type, like the graph callbacks
    node_known: bool,
    self_name: str | None,
    hash_bytes: int,
) -> tuple[Text, Text]:
    """One route as ``(path, context)``: the path line, and the line of context under it.

    The full route reads from left to right in the direction of the graph (the contact on
    the left, our node on the right), so the row and the drawn line refer to each other.
    ``path`` is one :class:`~meshterm.ui.pathline.PathLine`, drawn exactly as the Message
    paths dialog draws its arrivals (JP, 2026-08-09): **the hops show as names**. Each hop
    that the resolver can place shows its contact name in the hue that comes from the key
    of that node, as powerline chips where the terminal can draw them. Our own end is the
    ``★`` of the whole app. Each route on this screen ends on our node, so the star says
    this in one cell and leaves the rest of the lane to the hops that are different from
    row to row. (The trace route lane and the trophy card make the same choice, and the
    graph above puts the same mark on our end.)

    A hop that has no known name shows its own hash instead, at the path-hash-mode width of
    the device (the width that the radio itself carries for each hop). It is in the grey of
    the whole app for an unknown node, because colour is the signal for "this is a name",
    exactly as in the graph above. No hop repeats its hash after its name. The hash bytes
    are in the graph, one band up, and the shared node hue connects a row to the graph,
    not a second copy of the hex.

    Names use cells that hashes do not use, and the horizontal scroll of the row is for
    this reason (refer to :meth:`NodeDetailScreen._route_row_lines`). The rows of Message
    paths make the same choice, and a name is worth it: a route reads as places, and the
    user pays only for the one row that is highlighted. ``context`` starts with the hop
    count of the route (:func:`~meshterm.ui.pathline.hops_atom`: the number that the line
    above encodes but never states, and the first number by which the user compares two
    routes). Then the bottleneck SNR, the sample count, and a ``★ best`` or
    ``device route`` tag that marks the winner and the route that the firmware learned.
    All of these are kept off the path line itself, so a long chain never pushes them
    out. Because of the count, the row always has a context line, and that is correct:
    each route has a length. Before, a row whose second line came and went with the
    evidence made the list one row taller when the highlight moved.
    """
    hash_bytes = max(hash_bytes, 1)  # an unknown mode still must have a real width to slice

    def hop_of(hop: str) -> PathHop:
        named = resolve(hop)
        if named and named != hop:
            return PathHop(named, key=hop)
        return PathHop(_hop_hash(hop, hop, hash_bytes))  # unknown: its hash, in the grey

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
    """The marker and the label under which a contracted bidirectional cluster is drawn.

    A homogeneous cluster (all the members have the same node type) gets the map marker and
    the colour of that type (``3 repeaters`` under a ``▲``). Thus the user immediately sees
    a group of that type. A mixed cluster uses the plain node dot and ``n nodes`` instead.
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
    """Fold each bidirectional cluster of 3 or more in the draw sequences into one super-node.

    A dense knot of repeaters that relay each other is a strongly connected component that
    the flow graph cannot put in order (refer to :func:`~meshterm.ui.pathgraph.bidir_clusters`).
    If it is not changed, all the members collapse onto one crowded column. Thus each such
    cluster is contracted to one synthetic node. In the ``draw`` of each route, the members
    are written again as the one cluster id (members that follow each other are merged).
    The returned map gives each cluster its marker and label.

    Only ``draw`` (the shape of the graph) changes. The ``row`` of each route, with all the
    names, and its trace ``spec`` keep each member named in order. Thus the list under the
    graph still shows the exact order that the single marker cannot show, and a trace still
    prepares the real path.

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
                continue  # a sequence of members of the same cluster is one stop
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
    """Build the selectable routes (node → us) and graph callbacks of the Routes tab, or a note.

    The routes are drawn with the contact on the left and our node on the right. That is the
    *inbound* direction in which the packets travelled to our node, because all that the
    graph knows was received, not sent. The shared
    :func:`~meshterm.ui.widgets.route_graph_style` already draws in exactly this direction.
    (It was built for the Message paths dialog, where traffic arrives at our node on the
    right.) Thus this function gives it the target as its ``source`` (the left end) and
    uses its callbacks unchanged. It only reverses the hop order of each route, so that the
    drawn line goes from the contact in to our node.

    The selectable list, strongest first, puts the old ``route`` and ``suggest`` info rows
    into the tab. First the route with the best evidence (the observed suggestion, or if
    there is none, the route that the firmware learned, or if there is none, a direct
    route). Then the route that the firmware learned, when it is different. Then the *good*
    observed alternatives. Only good alternatives are kept. An observed route stays when its
    evidence is **fresh** (its stalest hop was heard in :data:`_PATH_STALE_DAYS`) and it is
    **not an outlier** (its score is in :data:`_PATH_OUTLIER_RATIO` of the strongest
    observed route). Thus the list has the routes that the user can trust, not each chain
    that was ever heard. With no route evidence at all (no learned route, no observed path,
    not even a direct link), there is nothing true to draw. Then a muted note is shown
    instead, and the tab still offers an automatic trace.

    The live selection of the screen sets which route is white (and which nodes keep the
    hue of their name), at each render. This function only assembles the routes and the
    base callbacks.
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
    # A node that the device only ever hears directly (no relays, no learned route) still
    # gets a line: the direct route with zero hops. Thus the graph shows the direct link, not
    # only a note.
    if best_hops is None and topo.link(topo.self_id, canonical_target) is not None:
        best_hops = ()

    # The selectable routes, in order and with no duplicates (outbound hops us → target).
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

    # The legend explains the marks of the relays with a known type. Thus the real types of
    # the members decide it, before the contraction folds the members of a dense cluster
    # behind one synthetic id.
    legend = any(type_of(h) is not None for route in routes for h in route.draw)
    # Fold each bidirectional cluster of 3 or more (a knot that the flow cannot put in order)
    # into one super-node. Thus it is drawn as one marker, and its members are not put on one
    # column. The row and the spec of each route keep the members named in order. Only the
    # drawn shape contracts.
    routes, clusters = _contract_bidir_clusters(routes, type_of)

    glyph_of, byte_label_of, label_rgb_of = style(
        resolve=resolve, self_name=self_name, source=node_label, type_of=type_of, key_of=key_of
    )

    # The graph labels exactly as the graph of Message paths does (JP, 2026-08-09): the two
    # ends by name, and each relay by only the first byte of its hash. Names here were the
    # clear choice, until a busy graph drew them. Each label is above or below its own
    # marker, and a long name goes across the graph lanes on each side of it. Two cells cannot,
    # so the graph stays readable with any number of routes. The names are now in the row
    # list under the graph. The byte of a relay and its row chip refer to each other by hue,
    # and the same node is not written two times. A contracted cluster keeps its own label
    # with the count and the type, because it stands for no single hash.
    def label_of(node: str) -> str | None:
        cluster = clusters.get(node)
        return cluster.label if cluster is not None else byte_label_of(node)

    base_rgb_of = label_rgb_of

    def cluster_label_rgb_of(node: str) -> RGB:
        cluster = clusters.get(node)
        return cluster.rgb if cluster is not None else base_rgb_of(node)

    # The target has its own map glyph (▲ repeater, ■ room, ◉ sensor), the same mark that
    # the header and the map give it. route_graph_style draws the far (left) end as a plain
    # dot, because on its home screen (Message paths) that end is the origin of any message.
    # Here it is a known contact, and its type can be shown. A contracted cluster draws its
    # own type mark. All the other nodes keep the glyph of the style.
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
    """The observed alternative routes to draw: fresh, with evidence, and not outliers.

    The function makes the raw list of scenarios smaller, to the alternatives that the graph
    must show as grey alternatives:

    * Only the **observed** family. The device and direct scenarios are not routes on which
      the evidence *observed* the node arrive. The device route has its own row, and a
      direct line that the evidence never observed does not get a graph lane.
    * Only routes with real evidence (a positive score. A link that was not observed has a
      score of zero, refer to :meth:`~meshterm.services.topology.MeshTopology._score_route`).
    * Only **fresh** routes: each hop was heard in :data:`_PATH_STALE_DAYS`. Thus a route
      whose weakest link has gone quiet is removed. It does not stay as a line that may not
      carry packets now.
    * Only routes that are **not much weaker** than the best observed alternative (the score
      is in :data:`_PATH_OUTLIER_RATIO` of the strongest). Thus one route that is clearly
      the best is not hidden under many routes that are only marginal.

    Returns the remaining routes in the order that
    :meth:`~meshterm.services.topology.MeshTopology.scenarios` gave them (strongest first).
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
    """Whether each link on ``us → hops… → target`` was heard in the stale limit.

    A route is only as current as its stalest hop. If a link on it was not heard in
    :data:`_PATH_STALE_DAYS`, the chain may not carry packets now, so the full route counts
    as stale. A link with no timestamp (evidence that carries no ``when``, for example a
    firmware route) counts as fresh, because there is no age to use against it.
    """
    chain = [topo.self_id, *hops, target]
    for a, b in pairwise(chain):
        link = topo.link(a, b)
        if link is None:
            return False
        last = link.last_seen
        if last is None or getattr(last, "tzinfo", None) is None:
            continue  # evidence with no date, so no age to find it stale
        if (now - last).total_seconds() > _PATH_STALE_DAYS * 86400.0:
            return False
    return True


def _with_target_glyph(glyph_of: GlyphOf, target_glyph: tuple[str, str]) -> GlyphOf:
    """Wrap the glyph callback of the graph, so that the target end draws its own node glyph.

    The left end of the graph (``SRC_NODE``) is the node of this page, and here its type is
    known. Thus it draws the mark of the map for that type (``▲`` repeater, ``■`` room,
    ``◉`` sensor, ``●`` plain), the same as the identity header and the location preview,
    and not the generic origin dot of ``route_graph_style``. All the other nodes go through
    unchanged (our own star on the right end keeps the ``you`` mark of the style).
    """

    def wrapped(node: str) -> tuple[str, str]:
        return target_glyph if node == SRC_NODE else glyph_of(node)

    return wrapped
