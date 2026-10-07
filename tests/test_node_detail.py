# SPDX-License-Identifier: Apache-2.0
"""Node detail screen tests: the mini-map, the tabbed page, and its route and topology helpers.

The screen only renders data and shows routes, so these tests run it with no terminal. The
tests build the display data by hand, in the same way as
:func:`~meshterm.ui.node_detail_screen.open_node_detail`. They render the screen and send
keys to it against a fake session. The tests of the trace screen and of the time machine
screen use the same method. The helpers that use the topology run against a real evidence
graph in memory.
"""

from __future__ import annotations

from types import SimpleNamespace

from rich.text import Text

from meshterm.core.geo import haversine_km
from meshterm.core.models import Contact, utcnow
from meshterm.services.topology import build_topology
from meshterm.services.trace_runner import (
    make_name_key_resolver,
    make_node_resolver,
    make_node_type_resolver,
)
from meshterm.ui.map_render import MapMarker
from meshterm.ui.minimap import MiniMap
from meshterm.ui.node_detail_screen import (
    NodeDetailScreen,
    _Action,
    _bearing,
    _range_text,
    _Route,
    _route_line,
    _routes_view,
    _RoutesView,
    _signal_row,
    _Tab,
)
from meshterm.ui.pathgraph import DST_NODE, SRC_NODE
from meshterm.ui.pathline import CRACK_HEAD, CRACK_TAIL, SELF_GLYPH, PathHop, PathLine
from meshterm.ui.tui.screen import CANCEL, ListWindow
from meshterm.ui.widgets import highlighted_hash, route_graph_style, tab_strip
from tests.conftest import plain as _plain  # the only helper that strips and joins the screen text

US = "aaaaaaaaaaaa"
HUB = Contact(
    name="Hub", public_key="3d63c6429436" + "0" * 52, key_prefix="3d63c6429436", node_type=2
)
FAR = Contact(name="Far", public_key="f2c24f54551e" + "0" * 52, key_prefix="f2c24f54551e")


class _FakeSession:
    def __init__(self) -> None:
        self.repaints = 0

    def invalidate(self) -> None:
        self.repaints += 1


class _OfflineSource:
    """A tile source with no basemap, so the mini-map draws markers on a blank grid."""

    available = False

    def load_tile(self, z: int, x: int, y: int):  # noqa: ANN201
        return None


# --- the mini-map ---------------------------------------------------------------


def test_minimap_offline_renders_markers_on_a_blank_grid() -> None:
    """With no basemap the preview still fills its box and draws the marker of the node."""
    mini = MiniMap(
        _FakeSession(),
        _OfflineSource(),
        14,
        center_lat=45.5,
        center_lon=-73.6,
        zoom=13,
        markers=[MapMarker(label="Hub", lat=45.5, lon=-73.6, is_repeater=True, key="3d63aa")],
    )
    lines = mini.render(40, 6)
    assert len(lines) == 6  # the canvas fills exactly the box of rows that the test requested
    assert mini.pending == 0 and mini.has_basemap is False  # offline: no downloads are planned
    assert any(ch != " " for ch in _plain(lines))  # the marker drew something


def test_minimap_clamps_zoom_to_the_source_ceiling() -> None:
    """A zoom above the maximum of the source (plus overzoom) is clamped. It does not grow."""
    mini = MiniMap(
        _FakeSession(),
        _OfflineSource(),
        10,
        center_lat=0.0,
        center_lon=0.0,
        zoom=99,
        markers=[],
    )
    assert mini._zoom == 12  # max_tile_zoom (10) + overzoom (2)


# --- geographic helpers ---------------------------------------------------------


def test_distance_and_bearing_are_sane() -> None:
    """The range in km and an 8-point compass bearing between two nearby points are correct."""
    # Approximately 1.11 km due north (0.01° latitude), so the bearing is N.
    assert 1.0 < haversine_km(45.5, -73.6, 45.51, -73.6) < 1.2
    assert _bearing(45.5, -73.6, 45.51, -73.6) == "N"
    assert _bearing(45.5, -73.6, 45.5, -73.59) == "E"  # due east


def test_range_text_omits_bearing_without_our_own_fix() -> None:
    """The location value has the range and the bearing only when our node has a position."""
    placed = _plain([_range_text(45.5, -73.6, 45.4, -73.5).plain])
    assert "45.5000, -73.6000" in placed and "km" in placed
    bare = _range_text(45.5, -73.6, None, None)
    assert "km" not in bare.plain and "45.5000" in bare.plain


def test_signal_row_summarizes_snr_and_rssi() -> None:
    """The signal row has the median SNR, the best SNR, and the last RSSI.

    If there is no measurement, the row is absent.
    """
    row = _signal_row(SimpleNamespace(median_snr=3.2, best_snr=7.0, last_rssi=-92.0))
    assert row is not None
    assert "median" in row.plain and "best" in row.plain and "RSSI -92" in row.plain
    # A node with no measurement (or our node) gives no row.
    assert _signal_row(SimpleNamespace(median_snr=None, best_snr=None, last_rssi=None)) is None
    assert _signal_row(None) is None


# --- the tab strip --------------------------------------------------------------


def test_tab_strip_boxes_every_tab_on_top_and_lights_the_active_one() -> None:
    """Each tab has a box across its top. Only the corners and fill of the active tab are lit."""
    group = tab_strip(["Map", "Routes"], 1, width=20)
    top, mid, bot = group.renderables
    assert mid.plain == "│  Map  │  Routes  │"  # both tabs have a box, with one shared vertical
    assert top.plain == "╭───────╭──────────╮"  # the left corner of the active tab opens (it wins)
    assert len(top.plain) == len(mid.plain) == len(bot.plain) == 20  # no fill is necessary yet


def test_tab_strip_bottom_is_a_continuous_rule_notched_at_the_active_tab() -> None:
    """The bottom border is flat under the inactive tabs and opens only under the active tab."""
    group = tab_strip(["Map", "Routes"], 1, width=20)
    top, mid, bot = group.renderables
    # A flat rule is under "Map" (it has no corner of its own). Then the notch turns up
    # (west and north, ╯) into "Routes". It is blank across the width of "Routes" (open into
    # the page). Then it goes down again (north and east, ╰) and continues.
    assert bot.plain == "────────╯          ╰"
    # It is one continuous line: each cell has the accent, the colour of the active tab. The
    # colours are not mixed.
    assert {r.style for r in bot.spans} == {"accent"}

    _, _, bot_first = tab_strip(["Map", "Routes"], 0, width=20).renderables
    assert bot_first.plain == "╯       ╰───────────"  # the notch is now under the first tab


def test_tab_strip_unselected_tabs_read_darker_than_muted() -> None:
    """The outlines of the inactive tabs use the dim ``faint`` shade, not the ``muted`` shade."""
    top, mid, _ = tab_strip(["Map", "Routes"], 1, width=20).renderables
    # The own left corner and fill of "Map" (columns 0-7). Its right corner at column 8 is
    # shared with the active "Routes" tab and has the accent, so the test does not use it.
    map_styles = {s.style for s in top.spans if s.start < 8} | {
        s.style for s in mid.spans if s.start < 8
    }
    assert map_styles == {"faint"}


def test_tab_strip_labels_hold_position_across_the_active_index() -> None:
    """If the user selects a different tab, only the corners change light. No label moves."""
    mid_first = tab_strip(["Map", "Routes"], 0, width=20).renderables[1]
    mid_second = tab_strip(["Map", "Routes"], 1, width=20).renderables[1]
    assert mid_first.plain == mid_second.plain == "│  Map  │  Routes  │"
    top_first = tab_strip(["Map", "Routes"], 0, width=20).renderables[0]
    assert top_first.plain == "╭───────╮──────────╮"  # the first tab keeps the plain leading corner


def test_tab_strip_rule_fills_out_to_the_render_width() -> None:
    """The bottom rule of the tab strip runs the full render width.

    Unlike the tabs, the left margin does not indent it. The margin is part of the one
    continuous line.
    """
    top, _, bot = tab_strip(["Map", "Routes"], 1, width=25).renderables
    assert top.plain.startswith("  ╭")  # the tab boxes have an indent...
    assert bot.plain == "──────────╯          ╰───"  # ...but the rule goes straight through
    assert len(bot.plain) == 25


def test_tab_strip_skips_the_margin_when_there_is_no_room() -> None:
    """The left margin of two columns is there only when the width has space for it."""
    top, _, _ = tab_strip(["Map", "Routes"], 1, width=20).renderables
    assert top.plain.startswith("╭")  # width 20 is the natural width of the strip: no space


def test_tab_strip_collapses_a_lone_tab_to_a_plain_heading() -> None:
    """A single tab renders as the plain accent heading, with no box."""
    (lone,) = tab_strip(["Routes"], 0, width=40).renderables
    assert lone.plain == "── Routes ──"


# --- the folded-in route line ---------------------------------------------------


def test_route_line_names_its_hops() -> None:
    """A route line names each hop that it can place.

    The path line goes contact → relays → us, which is the same direction as on the Message
    paths screen. The tag is at the end of the context line.
    """
    path, context = _route_line(
        "Far",
        FAR.public_key,
        ("3d63c6429436",),
        "device",
        None,
        0,
        resolve=make_node_resolver([HUB]),
        node_known=True,
        self_name="Us",
        hash_bytes=1,
    )
    line = path.plain
    assert line.startswith("Far")  # the contact is at the left, with its name
    assert "Hub" in line  # the relay shows as the contact that it is, not as ``3d``
    assert "3d" not in line and "f2" not in line  # no hop repeats its hash after its name
    assert line.rstrip().endswith(SELF_GLYPH)  # our node is at the right, as the ★
    assert "Us" not in line  # …in one cell, not as a name that each row repeats
    # The context starts with the length of the route, then the tag of the firmware route.
    # Both come from the path line.
    assert context.plain == "1 hop  ·  device route"


def test_route_line_greys_hops_nobody_can_name() -> None:
    """A hop that nobody can name shows its hash, in the grey of an unknown node.

    A hop with a name has the hue that comes from its key. A page node with no name looks
    the same as each other node with no name, as the graph draws it.
    """
    from meshterm.ui.theme import node_style

    path, _context = _route_line(
        "f2c24f54551e",
        FAR.public_key,
        ("3d63c6429436", "abcd1234ef56"),
        "",
        None,
        0,
        resolve=make_node_resolver([HUB]),
        node_known=False,
        self_name="Us",
        hash_bytes=1,
    )
    styles = {path.plain[s.start : s.end]: str(s.style) for s in path.spans}
    assert styles.get("ab") == "node.unknown"  # no contact names it, so its hash is grey
    assert styles.get("f2") == "node.unknown"  # the page node with no name has a grey hash
    hued = {
        path.plain[s.start : s.end]
        for s in path.spans
        if str(s.style) == node_style("3d63c6429436")
    }
    assert "Hub" in hued  # the Hub is a contact with a name: its name has its own hue


def test_route_line_hash_width_follows_our_path_hash_mode() -> None:
    """A hash lane has the width that the path hash mode of the device sets.

    Only the hops that have no name to show use hash cells, and they use the cells at that
    width. Here the width is three bytes, not the default of one byte.
    """
    path, _context = _route_line(
        "f2c24f54551e",
        FAR.public_key,
        ("3d63c6429436", "abcd1234ef56"),
        "",
        None,
        0,
        resolve=make_node_resolver([HUB]),
        node_known=False,
        self_name="Us",
        hash_bytes=3,
    )
    line = path.plain
    assert "f2c24f" in line  # the page node with no name, at 3 bytes
    assert "abcd12" in line  # the relay with no name, at 3 bytes
    assert "Hub" in line and "3d63c6" not in line  # the hop with a name does not use them


def test_route_line_marks_the_best_route_and_its_context() -> None:
    """The best route has ★ best, and the context row ends with its weakest SNR and sample count."""
    _path, context = _route_line(
        "Far",
        FAR.public_key,
        ("3d63c6429436",),
        "best",
        6.5,
        4,
        resolve=make_node_resolver([HUB]),
        node_known=True,
        self_name="Us",
        hash_bytes=1,
    )
    line = context.plain
    assert "★ best" in line and "weakest" in line and "6.5" in line and "4×" in line


def test_route_line_direct_route_has_no_relay() -> None:
    """A zero-hop route is contact → us, with nothing between them."""
    path, _context = _route_line(
        "Far",
        FAR.public_key,
        (),
        "best",
        None,
        0,
        resolve=make_node_resolver([HUB]),
        node_known=True,
        self_name="Us",
        hash_bytes=1,
    )
    assert path.plain == f"Far → {SELF_GLYPH}"


# --- the routes view (list + graph callbacks) -----------------------------------


def _topo_with_route():  # noqa: ANN202
    """A graph where Far is reached through the Hub, from evidence of repeated traces."""
    from meshterm.persistence.repository import TracedPath

    walks = [
        TracedPath(when=utcnow(), hops=[("3d", 12.0), ("f2", -5.0), ("3d", -5.5), (None, 12.0)])
        for _ in range(4)
    ]
    return build_topology(
        self_id=US + "0" * 52,
        contacts=[HUB, FAR],
        trace_paths=walks,
        packet_paths=[],
        neighbour_links=[],
    )


def _view(topo, suggested, device_route, target, node_label, contacts, name_key=None):  # noqa: ANN001
    """Build a routes view in the same way as :func:`open_node_detail`, from the given evidence."""
    return _routes_view(
        topo,
        topo.scenarios(target, device_route=device_route),
        suggested,
        device_route,
        target,
        target,
        1,
        resolve=make_node_resolver(contacts),
        type_of=make_node_type_resolver(contacts),
        key_of=make_name_key_resolver(contacts),
        style=route_graph_style,
        self_name="Us",
        node_label=node_label,
        name_key=name_key or (target + "0" * 52),
        node_known=True,
        hash_bytes=1,
    )


def test_routes_view_draws_evidence_and_notes_its_absence() -> None:
    """With route evidence the view has routes to select. Without it, a muted note replaces them."""
    topo = _topo_with_route()
    view = _view(
        topo, topo.suggested("f2c24f54551e"), ("3d63c6429436",), "f2c24f54551e", "Far", [HUB, FAR]
    )
    assert view.routes and view.glyph_of is not None  # a graph, not a note
    assert view.legend is True  # the Hub is a repeater, so the type legend is necessary

    empty = build_topology(
        self_id=US + "0" * 52, contacts=[FAR], trace_paths=[], packet_paths=[], neighbour_links=[]
    )
    note = _routes_view(
        empty,
        empty.scenarios("f2c24f54551e"),
        None,
        None,
        "f2c24f54551e",
        "f2c24f54551e",
        1,
        resolve=make_node_resolver([FAR]),
        type_of=make_node_type_resolver([FAR]),
        key_of=make_name_key_resolver([FAR]),
        style=route_graph_style,
        self_name="Us",
        node_label="Far",
        name_key=FAR.public_key,
        node_known=True,
        hash_bytes=1,
    )
    assert not note.routes and "no route observed" in note.note


def test_routes_view_puts_the_contact_on_the_left_and_us_on_the_right() -> None:
    """The graph goes node → us (the inbound direction): the contact left, our star right."""
    topo = _topo_with_route()
    view = _view(
        topo, topo.suggested("f2c24f54551e"), ("3d63c6429436",), "f2c24f54551e", "Far", [HUB, FAR]
    )
    assert view.label_of(SRC_NODE) == "Far"  # the left end is the target contact
    assert view.glyph_of(DST_NODE)[0] == "★"  # the right end is the star of our node
    assert view.label_of(DST_NODE) == "Us"  # …with the label for our node


def test_routes_view_tags_every_relay_with_its_hash_byte_alone() -> None:
    """In the graph, a relay has only its first hash byte as its label.

    Only the two ends have names, also when a contact can name the relay. Thus a label is
    never longer than two cells, and it does not go across the lanes that are next to it.
    This is the same rule as the graph of the Message paths screen.
    """
    topo = _topo_with_route()
    view = _view(
        topo, topo.suggested("f2c24f54551e"), ("3d63c6429436",), "f2c24f54551e", "Far", [HUB, FAR]
    )
    assert view.label_of("3d63c6429436") == "3d"  # the Hub is a known contact: the label is ``3d``

    unnamed = _view(topo, None, ("abcd1234ef56",), "f2c24f54551e", "Far", [FAR])
    assert unnamed.label_of("abcd1234ef56") == "ab"  # a relay with no name has the same label


def test_routes_view_target_wears_its_node_type_glyph() -> None:
    """The left end shows the target's own map mark (a repeater ▲), not a plain dot."""
    from meshterm.persistence.repository import TracedPath

    leaf = Contact(name="Leaf", public_key="27d4396a2967" + "0" * 52, key_prefix="27d4396a2967")
    # Reach the Hub (a repeater) through the Leaf, so the Hub is the left end in the drawing.
    walks = [
        TracedPath(when=utcnow(), hops=[("27d4", 8.0), ("3d", 6.0), ("27d4", 6.0), (None, 8.0)])
        for _ in range(3)
    ]
    topo = build_topology(
        self_id=US + "0" * 52,
        contacts=[HUB, leaf],
        trace_paths=walks,
        packet_paths=[],
        neighbour_links=[],
    )
    view = _view(
        topo,
        topo.suggested("3d63c6429436"),
        None,
        "3d63c6429436",
        "Hub",
        [HUB, leaf],
        name_key=HUB.public_key,
    )
    assert view.glyph_of(SRC_NODE)[0] == "▲"  # the repeater that is the target keeps its glyph


def test_routes_view_draws_routes_inbound_reversing_the_hop_order() -> None:
    """A drawn route goes node → us: the outbound hops (from us) reverse to inbound order."""
    r1 = Contact(name="R1", public_key="111111111111" + "0" * 52, key_prefix="111111111111")
    r2 = Contact(name="R2", public_key="222222222222" + "0" * 52, key_prefix="222222222222")
    topo = build_topology(
        self_id=US + "0" * 52,
        contacts=[r1, r2, FAR],
        trace_paths=[],
        packet_paths=[],
        neighbour_links=[],
    )
    route = ("111111111111", "222222222222")  # us → r1 → r2 → target, outbound order
    view = _view(topo, None, route, "f2c24f54551e", "Far", [r1, r2, FAR])
    # The drawing goes contact → us, so the relay nearest the target is first and the relay
    # nearest us is last.
    assert view.routes[0].draw == ("222222222222", "111111111111")


def test_routes_view_draws_a_direct_line_for_a_bare_neighbour() -> None:
    """A node that was only heard directly still has its zero-hop line, not a muted note."""
    from meshterm.persistence.repository import PacketPath

    # One packet that we heard straight from Far to us: a direct link, with no relays and no route.
    topo = build_topology(
        self_id=US + "0" * 52,
        contacts=[FAR],
        trace_paths=[],
        packet_paths=[PacketPath(when=utcnow(), origin="f2c24f54551e", hops=[], snr=6.0)],
        neighbour_links=[],
    )
    view = _view(topo, topo.suggested("f2c24f54551e"), None, "f2c24f54551e", "Far", [FAR])
    assert view.routes and view.routes[0].draw == ()  # a straight line from end to end


def _topo_two_alternatives():  # noqa: ANN202
    """A graph that reaches Far two ways: a strong route through Hub and a weak one through Alt."""
    from meshterm.persistence.repository import TracedPath

    alt = Contact(name="Alt", public_key="a1a1a1a1a1a1" + "0" * 52, key_prefix="a1a1a1a1a1a1")
    now = utcnow()
    strong = [
        TracedPath(when=now, hops=[("3d", 12.0), ("f2", 10.0), ("3d", 10.0), (None, 12.0)])
        for _ in range(8)
    ]
    weak = [TracedPath(when=now, hops=[("a1", -14.0), ("f2", -14.0), ("a1", -14.0), (None, -14.0)])]
    topo = build_topology(
        self_id=US + "0" * 52,
        contacts=[HUB, alt, FAR],
        trace_paths=strong + weak,
        packet_paths=[],
        neighbour_links=[],
    )
    return topo


def test_good_alternatives_keeps_observed_routes_and_drops_outliers() -> None:
    """The grey alternatives are the observed routes that are good enough to trust.

    Outliers and the bare direct and device families do not get a lane.
    """
    from meshterm.ui.node_detail_screen import _good_alternatives

    topo = _topo_two_alternatives()
    scenarios = topo.scenarios("f2c24f54551e")
    kept = {s.hops for s in _good_alternatives(topo, scenarios, "f2c24f54551e")}
    assert ("3d63c6429436",) in kept  # the strong observed route stays
    assert ("a1a1a1a1a1a1",) not in kept  # the much weaker route is removed as an outlier
    assert () not in kept  # the bare direct family is never a grey lane


def test_route_freshness_drops_a_route_with_a_long_quiet_hop() -> None:
    """A route is stale, and MeshTerm removes it, when its stalest hop is past the horizon."""
    from datetime import timedelta

    from meshterm.persistence.repository import TracedPath
    from meshterm.ui.node_detail_screen import _route_is_fresh

    now = utcnow()
    hops = [("3d", 12.0), ("f2", -5.0), ("3d", -5.5), (None, 12.0)]
    fresh = build_topology(
        self_id=US + "0" * 52,
        contacts=[HUB, FAR],
        trace_paths=[TracedPath(when=now, hops=hops)],
        packet_paths=[],
        neighbour_links=[],
    )
    assert _route_is_fresh(fresh, ("3d63c6429436",), "f2c24f54551e", now) is True

    stale = build_topology(
        self_id=US + "0" * 52,
        contacts=[HUB, FAR],
        trace_paths=[TracedPath(when=now - timedelta(days=60), hops=hops)],
        packet_paths=[],
        neighbour_links=[],
    )
    assert _route_is_fresh(stale, ("3d63c6429436",), "f2c24f54551e", now) is False


def test_contract_bidir_clusters_folds_a_knot_but_keeps_the_rows() -> None:
    """A bidirectional knot of 3 or more nodes contracts to one super-node in the graph.

    The rows do not change. Each route row and its trace spec keep the name of each member,
    in order.
    """
    from meshterm.ui.node_detail_screen import _contract_bidir_clusters

    routes = [
        _Route(draw=("aa", "bb", "cc"), spec="s1", path=Text("A B C"), context=Text("")),
        _Route(draw=("cc", "bb", "aa"), spec="s2", path=Text("C B A"), context=Text("")),
    ]
    new, clusters = _contract_bidir_clusters(routes, lambda _n: 2)  # all repeaters
    ((cid, cluster),) = clusters.items()
    assert cluster.label == "3 repeaters" and cluster.glyph == "▲"
    assert [r.draw for r in new] == [(cid,), (cid,)]  # the members are one cluster stop
    assert [r.spec for r in new] == ["s1", "s2"]  # traces still use the real path
    assert [r.path.plain for r in new] == ["A B C", "C B A"]  # the list keeps the full order


def test_contract_leaves_a_two_node_pair_and_unclustered_routes_alone() -> None:
    """A tidy two-way pair keeps its own markers (JP likes this display). Nothing contracts."""
    from meshterm.ui.node_detail_screen import _contract_bidir_clusters

    routes = [
        _Route(draw=("aa", "bb"), spec="s1", path=Text("A B"), context=Text("")),
        _Route(draw=("bb", "aa"), spec="s2", path=Text("B A"), context=Text("")),
    ]
    new, clusters = _contract_bidir_clusters(routes, lambda _n: 2)
    assert clusters == {}
    assert [r.draw for r in new] == [("aa", "bb"), ("bb", "aa")]


def test_contract_labels_a_mixed_cluster_generically() -> None:
    """A knot of mixed types has a generic label.

    Its members are different node types, so it cannot have one type mark. Its label is
    ``n nodes`` under the plain dot.
    """
    from meshterm.ui.node_detail_screen import _contract_bidir_clusters

    routes = [
        _Route(draw=("aa", "bb", "cc"), spec="", path=Text(""), context=Text("")),
        _Route(draw=("cc", "bb", "aa"), spec="", path=Text(""), context=Text("")),
    ]
    _new, clusters = _contract_bidir_clusters(routes, lambda n: {"aa": 2, "bb": 3, "cc": 4}[n[:2]])
    ((_cid, cluster),) = clusters.items()
    assert cluster.label == "3 nodes" and cluster.glyph == "●"


def test_located_accepts_real_fixes_and_rejects_junk() -> None:
    """The location preview needs a real position. It rejects null island and bad adverts."""
    from meshterm.ui.node_detail_screen import _located

    assert _located(45.5, -73.6) is True
    assert _located(None, -73.6) is False  # a coordinate is missing
    assert _located(0.0, 0.0) is False  # null island (the value for no GPS)
    assert _located(-97.0, -1041.97) is False  # the out-of-range advert from Homestead


# --- the screen -----------------------------------------------------------------


def _header() -> Text:
    header = Text("▲ ", style="#a78bfa")
    header.append("Hub", style="accent")
    header.append("   repeater", style="muted")
    return header


def _screen(**over) -> NodeDetailScreen:
    """A node detail screen with display data that the test builds by hand.

    It has an Info tab and a Routes tab that shows only a note.
    """
    kwargs = dict(
        title="Node — Hub",
        header=_header(),
        info_rows=[
            ("key", highlighted_hash("3d63c6429436" + "0" * 52, 1)),
            ("heard", Text("5m ago")),
            ("packets", Text("42")),
        ],
        tabs=[_Tab("Info", "info"), _Tab("Routes", "routes")],
        minimap=None,
        map_caption=None,
        routes=_RoutesView(note="no route observed yet — trace to discover one"),
        info_actions=[_Action("timemachine", "⏳", "", "Time machine — 42 receptions")],
        trace_action=_Action("trace", "🎯", "", "Trace — auto route …"),
    )
    kwargs.update(over)
    return NodeDetailScreen(**kwargs)


def test_node_detail_screen_renders_its_sections() -> None:
    """The Info tab has the vitals and its actions. The Routes tab has the stage and Trace."""
    screen = _screen()
    screen.note_viewport(30)
    body = _plain(screen.render_body(72))
    assert "Hub" in body and "repeater" in body  # the pinned identity header
    assert "│  Info  │" in body and "42" in body  # the vitals are in the Info tab, which has a box
    assert "Time machine" in body  # the Info tab's own action
    assert "Back" not in body  # no exit row anywhere: Esc leaves
    assert "────────" in body  # the faint rule that closes the stage
    assert "no route observed yet" not in body  # the Routes stage is only on its own tab

    screen.handle("tab")
    body = _plain(screen.render_body(72))
    assert "│  Routes  │" in body and "no route observed yet" in body
    assert "Trace" in body and "Back" not in body
    assert "packets" not in body  # the vitals stay on the Info tab
    # Each rendered line fits the 72-column standard.
    for line in screen.render_body(72):
        assert len(_plain([line])) <= 72
    assert len(screen.footer_hint) <= 72


def test_the_remove_row_is_tinted_and_keeps_its_tint_where_the_icon_lane_goes() -> None:
    """The destructive action has the ``err`` style on both platforms.

    On the desktop, this is the red 🗑 at the start of the row. On the PicoCalc, the whole
    icon lane is removed (``command_icon`` returns ``""``). If the tint went with the icon,
    a delete row looked the same as each other row. Thus the tint goes on the label, as
    :func:`~meshterm.ui.menus.marked_label` does for a menu row.
    """
    import re

    from meshterm.platforms import PICOCALC_LYRA, set_platform

    remove = _Action("remove", "🗑", "err", "Remove contact…")
    screen = _screen(info_actions=[remove])
    screen.note_viewport(30)
    line = screen._action_line(remove, selected=False, width=40)
    assert "🗑" in line and "Remove contact…" in _plain([line])

    set_platform(PICOCALC_LYRA)
    screen = _screen(info_actions=[remove])
    screen.note_viewport(30)
    folded = screen._action_line(remove, selected=False, width=40)
    assert "🗑" not in folded  # the icon lane is gone...
    assert "Remove contact" in _plain([folded])
    # ...and the tint of the icon is now on the words: a colour sequence starts the label.
    # Nothing starts the label of an action with no tint.
    assert re.search(r"\[[0-9;]*m *Remove contact", folded)
    plain_row = screen._action_line(
        _Action("timemachine", "⏳", "", "Time machine — 42 receptions"), False, 40
    )
    assert not re.search(r"\[[0-9;]*m *Time machine", plain_row)


def test_the_remove_row_resolves_its_own_token() -> None:
    """The Enter key on the remove row gives ``open_node_detail`` the token that it deletes with."""
    screen = _screen(info_actions=[_Action("remove", "🗑", "err", "Remove contact…")])
    resolved: list = []
    screen.resolve = lambda value: resolved.append(value)  # type: ignore[method-assign]
    screen.note_viewport(30)
    screen.render_body(72)
    screen.handle("enter")
    assert resolved == ["remove"]


def test_node_detail_screen_cursor_and_commit() -> None:
    """↑/↓ move the highlight (it stays visible). Enter resolves its key. Esc cancels."""
    screen = _screen()
    resolved: list = []
    screen.resolve = lambda value: resolved.append(value)  # type: ignore[method-assign]

    assert screen.cursor_line() is None  # the screen did not render yet
    screen.note_viewport(30)
    screen.render_body(72)
    # The highlight starts on the first row of the Info tab (Time machine), and the screen
    # always reports it. Thus the frame can keep it visible on a terminal that is too short
    # for the pinned layout.
    assert screen.cursor_line() is not None
    screen.render_body(72)
    screen.handle("enter")
    assert resolved == ["timemachine"]

    screen.handle("escape")
    assert resolved == ["timemachine", CANCEL]


def test_the_vitals_come_back_with_edge_scroll() -> None:
    """On a frame that is too short for the Info tab, ↑ on the first action scrolls the vitals back.

    When the user moves down the actions, the identity and the vitals scroll off the top.
    None of them can have the highlight. Before edge scroll, the only way back to them was
    the Home key. Now ↑ on the first action scrolls the page one line at a time to its top.
    """
    from meshterm.ui.tui import frame

    actions = [_Action(f"a{i}", "⏳", "", f"Action {i}") for i in range(4)]
    screen = _screen(info_actions=actions)

    def press(action: str) -> None:
        if not screen.edge_scroll(action):
            screen.handle(action)

    def view() -> list[str]:
        screen.note_viewport(5)
        visible, _above, _below = frame._visible_slice(screen, screen.render_body(60), 5)
        return [_plain([line]) for line in visible]

    view()
    for _ in range(3):
        press("down")
        view()
    for _ in range(3):
        press("up")
        view()
    assert "Action 0" in "\n".join(view())  # back on the first action…
    assert not any("Hub" in line for line in view())  # …the identity is still scrolled off
    for _ in range(10):
        press("up")  # edge scroll: the page moves, not the highlight
        view()
    page = view()
    assert "Hub" in page[0]  # the identity line is the top of the viewport again
    press("down")  # the snap-back shows the first action again before anything moves
    assert any("❯" in line and "Action 0" in line for line in view())


def test_node_detail_carries_no_exit_row() -> None:
    """The page has no Back row. Esc leaves, and we removed the row that repeated it.

    The page has only its own verbs. Thus the highlight on the actions never stops on a row
    that does nothing.
    """
    screen = _screen()
    resolved: list = []
    screen.resolve = lambda value: resolved.append(value)  # type: ignore[method-assign]

    screen.note_viewport(30)
    assert "Back" not in _plain(screen.render_body(72))
    screen.handle("escape")
    assert resolved == [CANCEL]


def _two_routes() -> _RoutesView:
    """A routes view with two routes to select, in the form that the assembly code returns."""
    return _RoutesView(
        routes=[
            _Route(draw=("3d63c6429436",), spec="3d,f2,3d", path=Text("via Hub"), context=Text("")),
            _Route(draw=("a1a1a1a1a1a1",), spec="a1,f2,a1", path=Text("via Alt"), context=Text("")),
        ],
        glyph_of=lambda n: ("●", "#ffffff"),
        label_of=lambda n: n[:2],
        label_rgb_of=lambda n: (200, 200, 200),
    )


def test_node_detail_screen_route_selection_arms_the_trace() -> None:
    """↑/↓ on the route rows moves the graph highlight and the spec that a trace uses."""
    screen = _screen(routes=_two_routes(), tabs=[_Tab("Routes", "routes")])
    # The rows that can have the highlight are route 0 and route 1. When the page lists
    # routes, the rows are the places where a trace starts. Thus no separate Trace action
    # row renders, and the tab has no other row.
    assert screen.selected_spec() == "3d,f2,3d"
    screen.handle("down")  # onto route 1
    assert screen._route_sel == 1 and screen.selected_spec() == "a1,f2,a1"
    screen.note_viewport(30)
    body = _plain(screen.render_body(72))
    assert "via Hub …" in body and "via Alt …" in body  # both rows drew, with … as the opener mark
    assert "Trace" not in body  # the row for the auto route shows only when no routes are listed


def test_node_detail_routes_stage_spends_the_caption_and_top_air_on_the_fan() -> None:
    """On the PicoCalc the Routes stage uses the caption row and the top air row for the fan.

    The fan starts directly under the tab strip and has no caption. Both rows that MeshTerm
    gets back go into the graph, never into a taller gap (JP, 2026-08-09).

    The own end padding of the graph leaves one empty canvas row above its top label. The
    stage draws one row over its ceiling and removes that row. Thus the removal does not
    cost a row.
    """
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform

    # A fan with so many routes that the row ceiling, not its own ideal height, sets its
    # size. Thus the rows that MeshTerm gets back show as lanes and not as free space.
    busy = _RoutesView(
        routes=[
            _Route(draw=(h * 12,), spec=f"{h}{h},f2", path=Text(f"via {h}{h}"), context=Text(""))
            for h in "123456"
        ],
        glyph_of=lambda n: ("●", "#ffffff"),
        label_of=lambda n: n[:2],
        label_rgb_of=lambda n: (200, 200, 200),
    )

    def _stage(viewport: int) -> list[str]:
        screen = _screen(routes=busy, tabs=[_Tab("Info", "info"), _Tab("Routes", "routes")])
        screen.handle("tab")  # onto Routes
        screen.note_viewport(viewport)
        lines = screen.render_body(49)

        def is_strip(ln):
            return "Routes" in _plain([ln]) and "│" in _plain([ln])

        strip_idx = next(i for i, ln in enumerate(lines) if is_strip(ln))
        rule_idx = next(
            i for i, ln in enumerate(lines) if set(_plain([ln])) == {"─"} and i > strip_idx
        )
        return [_plain([ln]) for ln in lines[strip_idx + 2 : rule_idx]]  # after the strip rule

    set_platform(PICOCALC_LYRA)
    pico = _stage(22)
    assert pico[0].strip(), "no blank row between the tab strip and the fan"
    assert not any("selected route" in line for line in pico), "the caption is dropped"

    set_platform(REGULAR)
    desktop = _stage(22)
    assert not desktop[0].strip()  # the desktop keeps its air…
    assert any("node → you, as heard" in line for line in desktop)  # …and its caption
    # Both rows go into the drawing, not into the chrome. From the same viewport, the
    # console gives this fan, which the ceiling limits, two more canvas rows than the desktop
    # stage that has a caption.
    drawn = [ln for ln in desktop if "node → you" not in ln]
    assert len(pico) == len(drawn) + 2


def _mark() -> Text:
    mark = Text("▲ ", style="type.repeater")
    mark.append("Hub", style="#ff8800")
    return mark


def _busy_routes() -> _RoutesView:
    """Four routes through a shared relay: the row ceiling, not its height, sets the fan size."""
    return _RoutesView(
        routes=[
            _Route(
                draw=(h * 12, "f2" * 6),
                spec=f"{h}{h},f2",
                path=Text(f"via {h}{h}"),
                context=Text(""),
            )
            for h in "1234"
        ],
        glyph_of=lambda n: ("●", "#ffffff"),
        label_of=lambda n: n[:2],
        label_rgb_of=lambda n: (200, 200, 200),
    )


def test_a_short_frame_wears_the_identity_in_the_title_bar() -> None:
    """On the Cardputer the title bar shows ``▲ Hub`` in the node colours. The identity line goes.

    The glyph replaces the word ``Node``, and the name has its own hue. The line under the
    bar said the same, so it said it two times (JP, 2026-10-04). A tall frame keeps both
    as they were.
    """
    from meshterm.platforms import CARDPUTER_ZERO, set_platform
    from meshterm.ui.tui import frame

    set_platform(CARDPUTER_ZERO)
    screen = _screen(mark=_mark())
    screen.note_viewport(12)  # the body of the Cardputer, under the bar and over the lane
    body = _plain(screen.render_body(53))
    assert screen.title == "▲ Hub"
    assert "repeater" not in body  # the identity line is gone…
    assert body.splitlines()[0].startswith("──┤ Info ├")  # …the tab strip is first on the page
    bar = frame._title_bar(screen, 53, False, False)
    start = bar.plain.index("▲")
    styles = {str(s.style) for s in bar.spans if s.start <= start < s.end}
    assert "type.repeater" in styles
    hub = bar.plain.index("Hub")
    assert "#ff8800" in {str(s.style) for s in bar.spans if s.start <= hub < s.end}

    tall = _screen(mark=_mark())
    tall.note_viewport(30)
    body = _plain(tall.render_body(53))
    assert tall.title == "Node — Hub" and "repeater" in body
    # If there is no mark to show, a short frame keeps its title and the identity line.
    bare = _screen()
    bare.note_viewport(12)
    assert "repeater" in _plain(bare.render_body(53)) and bare.title == "Node — Hub"


def test_a_short_frame_gives_the_route_graph_the_rows_it_freed() -> None:
    """There is no identity line and no rule under the graph, so both rows go to the graph.

    Between the tab strip and the route list there is only the fan. The fan gets each row
    that the guaranteed list window leaves.
    """
    from meshterm.platforms import CARDPUTER_ZERO, set_platform

    set_platform(CARDPUTER_ZERO)
    screen = _screen(mark=_mark(), routes=_busy_routes())
    screen.handle("tab")  # onto Routes
    screen.note_viewport(12)
    lines = [_plain([line]) for line in screen.render_body(53)]
    assert "Routes ├" in lines[0]
    first_route = next(i for i, line in enumerate(lines) if "via 11" in line)
    graph = lines[1:first_route]
    assert not any(set(line.strip()) == {"─"} for line in graph)  # no rule under the fan
    assert len(graph) == 12 - 1 - 4  # all rows but the strip and the four rows of the list
    assert [line.strip() for line in lines[first_route:]] == [
        "❯ via 11 …",
        "via 22 …",
        "via 33 …",
        "via 44 …",
    ]


def test_node_detail_screen_context_hangs_under_the_pathline() -> None:
    """The context (weakest, samples, tag) has its own line, with an indent under the path line."""
    routes = _RoutesView(
        routes=[
            _Route(
                draw=("3d63c6429436",),
                spec="3d,f2,3d",
                path=Text("f2 3d aa"),
                context=Text("weakest -6.0 dB  ·  3×  ·  ★ best"),
            ),
        ],
        glyph_of=lambda n: ("●", "#ffffff"),
        label_of=lambda n: n[:2],
        label_rgb_of=lambda n: (200, 200, 200),
    )
    screen = _screen(routes=routes, tabs=[_Tab("Routes", "routes")])
    screen.note_viewport(30)
    lines = screen.render_body(72)
    body = _plain(lines)
    assert "f2 3d aa …" in body  # the path line, with the opener mark
    assert "weakest -6.0 dB" in body and "★ best" in body  # the context is also drawn
    path_idx = next(i for i, ln in enumerate(lines) if "f2 3d aa" in _plain([ln]))
    assert _plain([lines[path_idx]]).startswith("❯ f2 3d aa")  # the pointer starts the path line
    context_line = _plain([lines[path_idx + 1]])
    assert context_line.startswith("  weakest")  # it hangs two columns under it, with no pointer


def test_node_detail_screen_context_line_absent_when_theres_nothing_to_show() -> None:
    """A route that has no context draws only its path line.

    If there is no weakest SNR, sample count, or tag, there is no empty hanging line under it.
    """
    routes = _RoutesView(
        routes=[_Route(draw=(), spec="", path=Text("f2 aa"), context=Text(""))],
        glyph_of=lambda n: ("●", "#ffffff"),
        label_of=lambda n: n[:2],
        label_rgb_of=lambda n: (200, 200, 200),
    )
    screen = _screen(routes=routes, tabs=[_Tab("Routes", "routes")])
    screen.note_viewport(30)
    lines = screen.render_body(72)
    path_idx = next(i for i, ln in enumerate(lines) if "f2 aa" in _plain([ln]))
    # Nothing hangs under a route that has no context. The path line is the last item that
    # the page draws (the tab has no action rows). It is not a muted context line.
    rest = _plain(lines[path_idx + 1 :]).strip()
    assert not rest.startswith("weakest") and not rest


def test_node_detail_screen_hscrolls_the_selected_pathline() -> None:
    """A path line that is too wide for the lane scrolls with ←/→, on the highlighted row only.

    If the highlight moves off the row, the scroll stops. A long row that is not selected
    has an ellipsis.
    """
    long_path = Text("f2 " + " ".join(f"{i:02x}" for i in range(40)) + " aa")
    routes = _RoutesView(
        routes=[
            _Route(draw=("3d",), spec="s0", path=long_path.copy(), context=Text("")),
            _Route(draw=("a1",), spec="s1", path=Text("f2 3d aa"), context=Text("")),
        ],
        glyph_of=lambda n: ("●", "#ffffff"),
        label_of=lambda n: n[:2],
        label_rgb_of=lambda n: (200, 200, 200),
    )
    screen = _screen(routes=routes, tabs=[_Tab("Routes", "routes")])
    screen.note_viewport(30)
    body = _plain(screen.render_body(72))
    assert "f2 00 01 02" in body  # with no scroll, the start of the chain shows
    assert "←→ scroll" in screen.footer_hint  # the overflow causes the footer atom

    row = next(ln for ln in _plain(screen.render_body(72)).splitlines() if ln.startswith("❯"))
    assert row.rstrip().endswith("…")  # the right edge marks the remainder, before a scroll

    screen.handle("right")
    screen.handle("right")
    body = _plain(screen.render_body(72))
    assert "f2 00 01 02" not in body  # the view moved away from the start
    row = next(ln for ln in body.splitlines() if ln.startswith("❯"))
    assert row.startswith("❯ …")  # …and now a mark says that the line also continues behind

    screen.handle("left")
    body = _plain(screen.render_body(72))
    assert "f2 00 01 02" not in body  # one step back, but not yet at the start

    for _ in range(20):  # run the scroll to its stop
        screen.handle("right")
    row = next(ln for ln in _plain(screen.render_body(72)).splitlines() if ln.startswith("❯"))
    # The clamp stops where the tail is readable: the end of the line, its last hop, is on
    # the screen. It is not cut behind a mark at the right edge that promises more text that
    # the keys cannot reach. Only the left mark is drawn. The `…` that shows that Enter
    # opens more is outside the lane, so a window that is full to its edge is the case where
    # it is not shown.
    assert row.rstrip().endswith("aa") and row.count("…") == 1
    screen.handle("right")  # …and the stop holds: nothing moves past it
    assert (
        next(ln for ln in _plain(screen.render_body(72)).splitlines() if ln.startswith("❯")) == row
    )

    screen.handle("down")  # onto route 1: the scroll of route 0 stops, a short row cannot scroll
    screen.render_body(72)
    assert "←→ scroll" not in screen.footer_hint  # the short path line of route 1 has no hint

    screen.handle("up")  # back onto route 0
    body = _plain(screen.render_body(72))
    assert "f2 00 01 02" in body  # the shift is reset, back at the start
    assert "←→ scroll" in screen.footer_hint  # route 0 overflows again


def test_node_detail_route_row_cracks_a_chip_path_at_both_edges() -> None:
    """In chips, a route row has a crack at both edges instead of an ellipsis.

    At each edge that the route goes past, the chip breaks on a half block. The row shows
    part of a route that continues, and a cracked segment says this. Three dots mean
    that a word is shortened.
    """
    chips = PathLine(
        [PathHop(f"NODE{i:02d}", key=f"{i:02x}aa") for i in range(12)], mode="powerline"
    ).text()
    routes = _RoutesView(
        routes=[
            _Route(draw=("3d",), spec="s0", path=chips.copy(), context=Text("")),
            _Route(draw=("a1",), spec="s1", path=Text("f2 3d aa"), context=Text("")),
        ],
        glyph_of=lambda n: ("●", "#ffffff"),
        label_of=lambda n: n[:2],
        label_rgb_of=lambda n: (200, 200, 200),
    )
    screen = _screen(routes=routes, tabs=[_Tab("Routes", "routes")])
    screen.note_viewport(30)

    def selected_row() -> str:
        return next(ln for ln in _plain(screen.render_body(72)).splitlines() if ln.startswith("❯"))

    row = selected_row()
    assert row.rstrip().endswith(CRACK_TAIL) and "…" not in row  # cut at the edge of the lane
    screen.handle("right")
    screen.handle("right")
    row = selected_row()
    assert row.startswith("❯ " + CRACK_HEAD)  # the start is now off to the left, with a crack
    assert row.rstrip().endswith(CRACK_TAIL) and "…" not in row


def test_node_detail_route_row_spends_no_lane_cells_on_the_opens_marker() -> None:
    """A route that fills its lane exactly stays whole. The ``…`` does not use lane cells.

    The mark says what the Enter key does. It is chrome, not route. MeshTerm reserved two
    cells for it, and a chain that fitted had a crack on its last chip (JP, 2026-08-10).
    The row said that the walk continued when it had arrived. The crack was only the closing
    cap with half of it removed. Now the path is fitted to the whole lane, and the mark uses
    the cells that are left. On a full lane, no cells are left.
    """
    avail = 72 - 2  # the lane of the row: the width less the pointer column
    exact = Text("x" * avail)
    routes = _RoutesView(
        routes=[_Route(draw=("3d",), spec="s0", path=exact.copy(), context=Text(""))],
        glyph_of=lambda n: ("●", "#ffffff"),
        label_of=lambda n: n[:2],
        label_rgb_of=lambda n: (200, 200, 200),
    )
    screen = _screen(routes=routes, tabs=[_Tab("Routes", "routes")])
    screen.note_viewport(30)
    row = next(ln for ln in _plain(screen.render_body(72)).splitlines() if ln.startswith("❯"))
    assert row == "❯ " + "x" * avail  # whole, not cut, and no mark pushed in
    # …and if there is nothing to scroll, ←→ do nothing and the hint does not show them.
    assert "←→ scroll" not in screen.footer_hint

    # The mark needs two cells, and two cells are free here, so the screen draws it.
    roomy = _RoutesView(
        routes=[_Route(draw=("3d",), spec="s0", path=Text("x" * (avail - 2)), context=Text(""))],
        glyph_of=lambda n: ("●", "#ffffff"),
        label_of=lambda n: n[:2],
        label_rgb_of=lambda n: (200, 200, 200),
    )
    screen = _screen(routes=roomy, tabs=[_Tab("Routes", "routes")])
    screen.note_viewport(30)
    row = next(ln for ln in _plain(screen.render_body(72)).splitlines() if ln.startswith("❯"))
    assert row == "❯ " + "x" * (avail - 2) + " …"


#: A key of 64 digits where each byte is different (``000102…1e1f``). Thus the first digits
#: of a window show where the lane was cut. A shift that is wrong by one shows ``0001…`` as
#: ``00102…``.
_LONG_KEY = "".join(f"{i:02x}" for i in range(32))


def _key_row(screen: NodeDetailScreen, width: int) -> str:
    """Render the Info tab and read its key lane, as one plain line."""
    screen.note_viewport(30)
    rows = [_plain([line]) for line in screen.render_body(width)]
    return next(row for row in rows if row.startswith("key"))


def test_node_detail_key_lane_hscrolls_instead_of_wrapping() -> None:
    """The key lane has one line and slides under ←/→ instead of wrapping.

    The windows are aligned to bytes. A faint ``…`` mark is at each edge where the key
    continues. The slide clamps at both ends.
    """
    screen = _screen(info_rows=[("key", highlighted_hash(_LONG_KEY, 1)), ("heard", Text("5m ago"))])

    row = _key_row(screen, 53)
    assert row.startswith("key      0001") and row.endswith("…")  # the head, with more at the right
    assert "1e1f" not in row and len(row) <= 53  # the tail is outside the lane, not wrapped
    assert "←→ scroll key" in screen.footer_hint  # …and the overflow causes its footer atom

    screen.handle("right")
    row = _key_row(screen, 53)
    # The shift is four whole bytes: the window starts on byte 4 (``04``), with a mark at
    # both edges.
    assert row.startswith("key      …0405") and row.endswith("…")

    for _ in range(6):
        screen.handle("right")  # run to the end: the shift clamps where the tail ends
    row = _key_row(screen, 53)
    assert row.endswith("1e1f")  # the last byte of the key, with no mark: this is the end
    assert row.startswith("key      …")  # …and the head is now off to the left

    for _ in range(6):
        screen.handle("left")  # then the other way, and it clamps at the head
    assert _key_row(screen, 53).startswith("key      0001")


def test_node_detail_key_lane_scroll_survives_the_cursor_and_resets_on_tab() -> None:
    """The key is pinned chrome, not a row that can have the highlight.

    If the user moves over the action rows, the key keeps its scroll. If the user leaves the
    tab, the scroll resets. A key that fits has no ←→ in the hint.
    """
    screen = _screen(info_rows=[("key", highlighted_hash(_LONG_KEY, 1)), ("heard", Text("5m ago"))])
    _key_row(screen, 53)
    screen.handle("right")
    screen.handle("down")  # onto Back: the window stays where the user left it
    assert _key_row(screen, 53).startswith("key      …0405")

    screen.handle("tab")  # over to Routes…
    screen.render_body(53)
    assert "←→" not in screen.footer_hint  # nothing there is too wide to scroll
    screen.handle("tab")  # …and back: the lane starts at the head again
    assert _key_row(screen, 53).startswith("key      0001")

    # A lane that is wide enough for the whole key shows it whole, with no mark and no hint.
    row = _key_row(screen, 100)
    assert row.startswith(f"key      {_LONG_KEY}") and "…" not in row
    assert "←→" not in screen.footer_hint
    screen.handle("right")  # does nothing, because no text is past the edge
    assert _key_row(screen, 100).startswith(f"key      {_LONG_KEY}")


def test_node_detail_enter_on_a_route_row_opens_its_trace() -> None:
    """The Enter key on a route row prepares a trace on that route. No Trace row is necessary."""
    screen = _screen(routes=_two_routes(), tabs=[_Tab("Routes", "routes")])
    resolved: list = []
    screen.resolve = lambda value: resolved.append(value)  # type: ignore[method-assign]
    screen.handle("down")  # onto route 1
    screen.handle("enter")
    assert resolved == ["trace"] and screen.selected_spec() == "a1,f2,a1"


def test_route_labels_light_through_a_coalesced_hop() -> None:
    """A route that has a short hop id lights the wide marker that the graph merges it into.

    The route evidence comes to the page with different hash widths. For example, a trace
    hop of 1 byte (``3d``) is on one route, and the full id of the same relay is on a sibling
    route. The graph merges the short id into the one wide marker. Thus the dimming of the
    nodes that are not on the route must check membership against the drawn ids. When it
    used the raw draw hops, the relay of the selected route was muted, and the white line
    went straight through it (the report from TSFCT SUTTON-680M).
    """
    lit: list[str] = []

    def base_rgb(node: str) -> tuple[int, int, int]:
        lit.append(node)  # the widget calls this only for nodes on the selected route
        return (200, 200, 200)

    routes = _RoutesView(
        routes=[
            _Route(
                draw=("bf61f2fb1d9e", "3d63c6429436"),
                spec="s0",
                path=Text("wide"),
                context=Text(""),
            ),
            _Route(draw=("bf61f2fb1d9e", "3d"), spec="s1", path=Text("short"), context=Text("")),
        ],
        glyph_of=lambda n: ("●", "#ffffff"),
        label_of=lambda n: n[:2],
        label_rgb_of=base_rgb,
    )
    screen = _screen(routes=routes, tabs=[_Tab("Routes", "routes")])
    screen.note_viewport(30)
    screen.handle("down")  # select the short-hop route
    screen.render_body(72)
    assert "3d63c6429436" in lit  # the relay that the route goes through keeps its hue


def test_node_detail_route_list_windows_inside_the_page() -> None:
    """The route list scrolls in a list window inside the page. It does not make the page taller.

    If there are more routes than fit, edge markers show, and the pinned chrome stays
    visible. This is true for each number of routes of a busy node.
    """
    routes = _RoutesView(
        routes=[
            _Route(
                draw=(f"{i:x}{i:x}" * 6,),
                spec=f"s{i}",
                path=Text(f"route {i}"),
                context=Text(""),
            )
            for i in range(12)
        ],
        glyph_of=lambda n: ("●", "#ffffff"),
        label_of=lambda n: n[:2],
        label_rgb_of=lambda n: (200, 200, 200),
    )
    screen = _screen(routes=routes, tabs=[_Tab("Routes", "routes")])
    screen.note_viewport(30)
    lines = screen.render_body(72)
    assert len(lines) <= 30  # the body fits the viewport, so nothing scrolls off
    body = _plain(lines)
    assert "↓" in body and "more" in body  # the edge marker counts the routes that are hidden
    assert "── Routes ──" in body  # the pinned chrome above the list stays
    assert "PgUp/PgDn scroll" in screen.footer_hint  # the hint shows paging only when necessary

    # If the highlight moves to the last route, the window slides down to keep it visible.
    for _ in range(11):
        screen.handle("down")
    lines = screen.render_body(72)
    assert len(lines) <= 30
    assert "route 11" in _plain(lines) and screen.cursor_line() is not None


def test_node_detail_route_cursor_clamps_at_both_ends() -> None:
    """The route list window does not wrap around.

    ↑ on the first row and ↓ after the last row stay where they are. They do not move the
    window from one end to the other end.
    """
    routes = _RoutesView(
        routes=[
            _Route(
                draw=(f"{i:x}{i:x}" * 6,),
                spec=f"s{i}",
                path=Text(f"route {i}"),
                context=Text(""),
            )
            for i in range(12)
        ],
        glyph_of=lambda n: ("●", "#ffffff"),
        label_of=lambda n: n[:2],
        label_rgb_of=lambda n: (200, 200, 200),
    )
    screen = _screen(routes=routes, tabs=[_Tab("Routes", "routes")])
    screen.note_viewport(30)
    screen.render_body(72)
    screen.handle("up")
    assert screen._row_index == 0
    for _ in range(20):
        screen.handle("down")
    assert screen._row_index == 11


def test_fit_blocks_walks_wrapped_rows_into_view() -> None:
    """The fit for rows of different heights moves wrapped rows into the viewport.

    It keeps whole blocks. It uses marker lines only when rows are hidden. It moves the
    window down to the highlighted row.
    """
    window = ListWindow()
    top, count = window.fit_blocks([2, 2, 2, 2], 5, 3)  # the highlight is on the last 2-line row
    assert top + count == 4 and top == 2  # it slid to the tail, and the last two rows fit
    assert window.page == count  # the capacity that the fit set is the size of a page step
    top, count = window.fit_blocks([1, 1], 5, 0)
    assert (top, count) == (0, 2)  # everything fits: no window, no markers


def test_node_detail_screen_tabs_switch_the_stage() -> None:
    """←→ (and Tab) change the tab that fills the stage. The footer shows the switch only then."""
    mini = MiniMap(
        _FakeSession(),
        _OfflineSource(),
        14,
        center_lat=45.5,
        center_lon=-73.6,
        zoom=13,
        markers=[MapMarker(label="Hub", lat=45.5, lon=-73.6, key="3d63aa")],
    )
    screen = _screen(
        minimap=mini,
        map_caption=Text("Hub · centred here", style="faint"),
    )
    screen.note_viewport(30)
    assert "Tab/⇧Tab switch" in screen.footer_hint  # there are two tabs, so the hint has the switch
    # The screen starts on the Info tab. The vitals, the caption of the preview with a
    # position, and its braille edge scrub are all part of this tab.
    body = _plain(screen.render_body(72))
    assert "│  Info  │" in body and "centred here" in body
    assert screen.consume_edge_scrub() == 2

    # The F-key chip names the tab that the switch goes to, so it changes with the stage.
    assert screen.picocalc_lyra_lane[2].label == "Routes"
    screen.handle("tab")  # switch to the Routes tab
    body = _plain(screen.render_body(72))
    assert "│  Routes  │" in body and "no route observed yet" in body
    assert screen.consume_edge_scrub() == 0  # the braille preview is not visible now
    assert screen.picocalc_lyra_lane[2].label == "Info"


def test_node_detail_single_tab_hides_the_switch_hint() -> None:
    """A page with only one tab does not have the ←→ tab atom in its footer."""
    screen = _screen(tabs=[_Tab("Info", "info")])  # our node: no Routes tab
    assert "←→ tab" not in screen.footer_hint
    assert "↑↓ move" in screen.footer_hint and screen.footer_hint.endswith("Esc back")
    # There is no tab to switch to, so the lane leaves the slot empty. It does not dim it.
    assert screen.picocalc_lyra_lane[2] is None


def test_toggling_the_lock_rewrites_the_rows_and_keeps_the_cursor_on_it() -> None:
    """Lock becomes Unlock on the same row, and Archive is not offered while the lock is on."""
    tm = _Action("timemachine", "⏳", "", "Time machine — 42 receptions")
    delete = _Action("remove", "🗑", "err", "Delete contact…")
    screen = _screen(
        info_actions=[
            tm,
            _Action("lock", "🔒", "", "Lock contact"),
            _Action("archive", "💾", "", "Archive contact"),
            delete,
        ]
    )
    resolved: list = []
    screen.resolve = lambda value: resolved.append(value)  # type: ignore[method-assign]
    screen.note_viewport(30)
    screen.render_body(72)
    screen.handle("down")
    screen.handle("enter")
    assert resolved == ["lock"]

    screen.replace_info_actions([tm, _Action("unlock", "🔓", "", "Unlock contact"), delete])
    screen.render_body(72)
    screen.handle("enter")
    assert resolved == ["lock", "unlock"]
    assert [a.key for a in screen._info_actions] == ["timemachine", "unlock", "remove"]


def test_routes_legend_steps_aside_on_a_short_frame() -> None:
    """The legend is under the fan, except on a short frame such as the frame of the Cardputer.

    On a short frame, the rows go to the graph and the route list, as for the fan of the
    message paths.
    """
    view = _RoutesView(
        routes=[_Route(draw=("3d" * 6,), spec="3d,f2", path=Text("via 3d"), context=Text(""))],
        glyph_of=lambda n: ("▲", "#ffffff"),
        label_of=lambda n: n[:2],
        label_rgb_of=lambda n: (200, 200, 200),
        legend=True,
    )

    def stage(viewport: int) -> str:
        screen = _screen(routes=view, tabs=[_Tab("Info", "info"), _Tab("Routes", "routes")])
        screen.handle("tab")  # onto Routes
        screen.note_viewport(viewport)
        return _plain(screen.render_body(53))

    assert "▲ repeater" in stage(22)  # the body of the PicoCalc
    assert "▲ repeater" not in stage(11)  # the body of the Cardputer
