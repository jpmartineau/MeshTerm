# SPDX-License-Identifier: Apache-2.0
"""Node detail screen tests: the mini-map, the tabbed page, and its route/topology helpers.

The screen is a pure render-and-route view, so these drive it headless — display data is
built by hand (the way :func:`~meshterm.ui.node_detail_screen.open_node_detail` assembles
it) and rendered/keyed against a fake session, the same approach as the trace and time
machine screen tests. The topology-driven helpers are exercised against a real (in-memory)
evidence graph.
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
from tests.conftest import plain as _plain  # THE strip-and-join screen reader

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
    """A tile source with no basemap, so the mini-map plots markers on a blank grid."""

    available = False

    def load_tile(self, z: int, x: int, y: int):  # noqa: ANN201
        return None


# --- the mini-map ---------------------------------------------------------------


def test_minimap_offline_renders_markers_on_a_blank_grid() -> None:
    """With no basemap the preview still fills its box and plots the node's marker."""
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
    assert len(lines) == 6  # the canvas fills exactly the requested row box
    assert mini.pending == 0 and mini.has_basemap is False  # offline: no fetches scheduled
    assert any(ch != " " for ch in _plain(lines))  # the marker drew something


def test_minimap_clamps_zoom_to_the_source_ceiling() -> None:
    """A zoom past the source's max (plus overzoom) is clamped, never runs away."""
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
    """Range in km and an 8-point compass bearing between two nearby points."""
    # ~1.11 km due north (0.01° latitude), so bearing reads N.
    assert 1.0 < haversine_km(45.5, -73.6, 45.51, -73.6) < 1.2
    assert _bearing(45.5, -73.6, 45.51, -73.6) == "N"
    assert _bearing(45.5, -73.6, 45.5, -73.59) == "E"  # due east


def test_range_text_omits_bearing_without_our_own_fix() -> None:
    """The location value carries range+bearing only when we know where we are."""
    placed = _plain([_range_text(45.5, -73.6, 45.4, -73.5).plain])
    assert "45.5000, -73.6000" in placed and "km" in placed
    bare = _range_text(45.5, -73.6, None, None)
    assert "km" not in bare.plain and "45.5000" in bare.plain


def test_signal_row_summarizes_snr_and_rssi() -> None:
    """The signal row folds median/best SNR and last RSSI, or is dropped when unmeasured."""
    row = _signal_row(SimpleNamespace(median_snr=3.2, best_snr=7.0, last_rssi=-92.0))
    assert row is not None
    assert "median" in row.plain and "best" in row.plain and "RSSI -92" in row.plain
    # A node with nothing measured (or our own node) contributes no row at all.
    assert _signal_row(SimpleNamespace(median_snr=None, best_snr=None, last_rssi=None)) is None
    assert _signal_row(None) is None


# --- the tab strip --------------------------------------------------------------


def test_tab_strip_boxes_every_tab_on_top_and_lights_the_active_one() -> None:
    """Every tab is boxed across its top; only the active tab's corners/fill read as lit."""
    group = tab_strip(["Map", "Routes"], 1, width=20)
    top, mid, bot = group.renderables
    assert mid.plain == "│  Map  │  Routes  │"  # both tabs boxed, sharing one vertical
    assert top.plain == "╭───────╭──────────╮"  # active tab's left corner opens (wins the seam)
    assert len(top.plain) == len(mid.plain) == len(bot.plain) == 20  # no fill needed yet


def test_tab_strip_bottom_is_a_continuous_rule_notched_at_the_active_tab() -> None:
    """The bottom border is flat under inactive tabs and opens only under the active one."""
    group = tab_strip(["Map", "Routes"], 1, width=20)
    top, mid, bot = group.renderables
    # flat rule under "Map" (no corner of its own), then the notch turns up (west+north,
    # ╯) into "Routes", blank across its width (open into the page), back down (north+east,
    # ╰) and on.
    assert bot.plain == "────────╯          ╰"
    # One continuous line: every cell reads accent, the active tab's colour, not a mix.
    assert {r.style for r in bot.spans} == {"accent"}

    _, _, bot_first = tab_strip(["Map", "Routes"], 0, width=20).renderables
    assert bot_first.plain == "╯       ╰───────────"  # notch now sits under the first tab


def test_tab_strip_unselected_tabs_read_darker_than_muted() -> None:
    """Inactive tab outlines use the dim ``faint`` shade, not the everyday ``muted`` one."""
    top, mid, _ = tab_strip(["Map", "Routes"], 1, width=20).renderables
    # "Map"'s own left corner and fill (columns 0-7); its right corner at column 8 is
    # shared with the active "Routes" tab and reads accent, so it's excluded here.
    map_styles = {s.style for s in top.spans if s.start < 8} | {
        s.style for s in mid.spans if s.start < 8
    }
    assert map_styles == {"faint"}


def test_tab_strip_labels_hold_position_across_the_active_index() -> None:
    """Selecting a different tab only relights corners — no label ever shifts column."""
    mid_first = tab_strip(["Map", "Routes"], 0, width=20).renderables[1]
    mid_second = tab_strip(["Map", "Routes"], 1, width=20).renderables[1]
    assert mid_first.plain == mid_second.plain == "│  Map  │  Routes  │"
    top_first = tab_strip(["Map", "Routes"], 0, width=20).renderables[0]
    assert top_first.plain == "╭───────╮──────────╮"  # first tab keeps the plain leading corner


def test_tab_strip_rule_fills_out_to_the_render_width() -> None:
    """The tab strip's bottom rule runs the full render width.

    Unlike the tabs, it isn't indented by the left margin — that margin is itself part
    of the one continuous line.
    """
    top, _, bot = tab_strip(["Map", "Routes"], 1, width=25).renderables
    assert top.plain.startswith("  ╭")  # the tab boxes sit indented...
    assert bot.plain == "──────────╯          ╰───"  # ...but the rule fills straight through
    assert len(bot.plain) == 25


def test_tab_strip_skips_the_margin_when_there_is_no_room() -> None:
    """The two-column left margin only appears when the width has slack for it."""
    top, _, _ = tab_strip(["Map", "Routes"], 1, width=20).renderables
    assert top.plain.startswith("╭")  # width 20 == the strip's natural width, no slack


def test_tab_strip_collapses_a_lone_tab_to_a_plain_heading() -> None:
    """A single tab renders as the plain accent heading, no box needed."""
    (lone,) = tab_strip(["Routes"], 0, width=40).renderables
    assert lone.plain == "── Routes ──"


# --- the folded-in route line ---------------------------------------------------


def test_route_line_names_its_hops() -> None:
    """A route line names every hop it can place.

    The pathline runs contact → relays → us, which is the Message paths reading; the
    tag trails on the context line.
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
    assert line.startswith("Far")  # the contact anchors the left, by name
    assert "Hub" in line  # the relay reads as the contact it is, not as ``3d``
    assert "3d" not in line and "f2" not in line  # no hop repeats its hash after its name
    assert line.rstrip().endswith(SELF_GLYPH)  # our own node anchors the right, as the ★
    assert "Us" not in line  # …in one cell, not a name every row would repeat
    # The context leads with the route's length, then the firmware-route tag — both off the
    # pathline itself.
    assert context.plain == "1 hop  ·  device route"


def test_route_line_greys_hops_nobody_can_name() -> None:
    """A hop nobody can name stands in its hash, in the unknown-node grey.

    A named hop takes its key-derived hue instead; a page node with no name reads the
    same as any other unnamed one, exactly as the graph draws it.
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
    assert styles.get("ab") == "node.unknown"  # no contact names it → its hash, grey
    assert styles.get("f2") == "node.unknown"  # the nameless page node greys its own hash
    hued = {
        path.plain[s.start : s.end]
        for s in path.spans
        if str(s.style) == node_style("3d63c6429436")
    }
    assert "Hub" in hued  # the Hub is a named contact: its name, in its own hue


def test_route_line_hash_width_follows_our_path_hash_mode() -> None:
    """A hash lane is as wide as the device's own path-hash mode says.

    Only the hops with no name to show spend hash cells, and they spend them at that
    width — three bytes here, rather than the 1-byte default.
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
    assert "f2c24f" in line  # the nameless page node, at 3 bytes
    assert "abcd12" in line  # the unnamed relay, at 3 bytes
    assert "Hub" in line and "3d63c6" not in line  # the named one never spends them


def test_route_line_marks_the_best_route_and_its_context() -> None:
    """The winner wears ★ best and trails its bottleneck SNR and sample count on the context row."""
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
    """A zero-hop route reads contact → us with nothing between them."""
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
    """A graph where Far is reached through the Hub, from repeated trace evidence."""
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
    """Build a routes view the way :func:`open_node_detail` does, over the given evidence."""
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
    """With route evidence the view carries selectable routes; without, a muted note stands in."""
    topo = _topo_with_route()
    view = _view(
        topo, topo.suggested("f2c24f54551e"), ("3d63c6429436",), "f2c24f54551e", "Far", [HUB, FAR]
    )
    assert view.routes and view.glyph_of is not None  # a graph, not a note
    assert view.legend is True  # the Hub is a repeater, so the type legend is earned

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
    """The graph reads node → us (the inbound direction): the contact left, our star right."""
    topo = _topo_with_route()
    view = _view(
        topo, topo.suggested("f2c24f54551e"), ("3d63c6429436",), "f2c24f54551e", "Far", [HUB, FAR]
    )
    assert view.label_of(SRC_NODE) == "Far"  # the left endpoint is the target contact
    assert view.glyph_of(DST_NODE)[0] == "★"  # the right endpoint is our own star
    assert view.label_of(DST_NODE) == "Us"  # …labelled as us


def test_routes_view_tags_every_relay_with_its_hash_byte_alone() -> None:
    """In the graph, a relay wears its first hash byte and nothing else.

    Only the two endpoints are named, whether or not a contact could name the relay, so
    a label never outgrows two cells and spills across the lanes it sits between — the
    Message paths graph's rule exactly.
    """
    topo = _topo_with_route()
    view = _view(
        topo, topo.suggested("f2c24f54551e"), ("3d63c6429436",), "f2c24f54551e", "Far", [HUB, FAR]
    )
    assert view.label_of("3d63c6429436") == "3d"  # the Hub is a known contact — still ``3d``

    unnamed = _view(topo, None, ("abcd1234ef56",), "f2c24f54551e", "Far", [FAR])
    assert unnamed.label_of("abcd1234ef56") == "ab"  # and one nobody can name reads the same


def test_routes_view_target_wears_its_node_type_glyph() -> None:
    """The left endpoint draws the target's own map mark (a repeater ▲), not a plain dot."""
    from meshterm.persistence.repository import TracedPath

    leaf = Contact(name="Leaf", public_key="27d4396a2967" + "0" * 52, key_prefix="27d4396a2967")
    # Reach the Hub (a repeater) through the Leaf, so the Hub is the drawn left endpoint.
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
    assert view.glyph_of(SRC_NODE)[0] == "▲"  # the repeater target keeps its own glyph


def test_routes_view_draws_routes_inbound_reversing_the_hop_order() -> None:
    """A drawn route runs node → us: the outbound (us-outward) hops reverse into inbound order."""
    r1 = Contact(name="R1", public_key="111111111111" + "0" * 52, key_prefix="111111111111")
    r2 = Contact(name="R2", public_key="222222222222" + "0" * 52, key_prefix="222222222222")
    topo = build_topology(
        self_id=US + "0" * 52,
        contacts=[r1, r2, FAR],
        trace_paths=[],
        packet_paths=[],
        neighbour_links=[],
    )
    route = ("111111111111", "222222222222")  # us → r1 → r2 → target, outward order
    view = _view(topo, None, route, "f2c24f54551e", "Far", [r1, r2, FAR])
    # Drawn contact→us, so the relay nearest the target leads and the one nearest us trails.
    assert view.routes[0].draw == ("222222222222", "111111111111")


def test_routes_view_draws_a_direct_line_for_a_bare_neighbour() -> None:
    """A node only ever heard directly still draws its zero-hop line, not a muted note."""
    from meshterm.persistence.repository import PacketPath

    # A single overheard frame straight from Far to us — a direct link, no relays, no route.
    topo = build_topology(
        self_id=US + "0" * 52,
        contacts=[FAR],
        trace_paths=[],
        packet_paths=[PacketPath(when=utcnow(), origin="f2c24f54551e", hops=[], snr=6.0)],
        neighbour_links=[],
    )
    view = _view(topo, topo.suggested("f2c24f54551e"), None, "f2c24f54551e", "Far", [FAR])
    assert view.routes and view.routes[0].draw == ()  # a straight endpoint-to-endpoint line


def _topo_two_alternatives():  # noqa: ANN202
    """A graph reaching Far two ways: a strong route via Hub and a far weaker one via Alt."""
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
    """The grey alternatives are the observed routes worth trusting.

    Outliers and the bare direct/device families don't earn a lane.
    """
    from meshterm.ui.node_detail_screen import _good_alternatives

    topo = _topo_two_alternatives()
    scenarios = topo.scenarios("f2c24f54551e")
    kept = {s.hops for s in _good_alternatives(topo, scenarios, "f2c24f54551e")}
    assert ("3d63c6429436",) in kept  # the strong observed route survives
    assert ("a1a1a1a1a1a1",) not in kept  # the far weaker one is trimmed as an outlier
    assert () not in kept  # the bare direct family is never a grey lane


def test_route_freshness_drops_a_route_with_a_long_quiet_hop() -> None:
    """A route counts as stale — and is dropped — once its stalest hop falls past the horizon."""
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
    """A 3+ bidirectional knot contracts to one super-node in the graph.

    The rows are untouched: each route row and its trace spec keep every member named,
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
    assert [r.draw for r in new] == [(cid,), (cid,)]  # members folded to the one cluster stop
    assert [r.spec for r in new] == ["s1", "s2"]  # traces still arm on the real path
    assert [r.path.plain for r in new] == ["A B C", "C B A"]  # the list keeps the full order


def test_contract_leaves_a_two_node_pair_and_unclustered_routes_alone() -> None:
    """A tidy two-way pair keeps its own markers (the display JP likes); nothing contracts."""
    from meshterm.ui.node_detail_screen import _contract_bidir_clusters

    routes = [
        _Route(draw=("aa", "bb"), spec="s1", path=Text("A B"), context=Text("")),
        _Route(draw=("bb", "aa"), spec="s2", path=Text("B A"), context=Text("")),
    ]
    new, clusters = _contract_bidir_clusters(routes, lambda _n: 2)
    assert clusters == {}
    assert [r.draw for r in new] == [("aa", "bb"), ("bb", "aa")]


def test_contract_labels_a_mixed_cluster_generically() -> None:
    """A mixed-type knot is labelled generically.

    Its members are different node types, so it can't wear one type mark: it reads
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
    """The location preview is gated on a real fix — no null island, no out-of-range advert."""
    from meshterm.ui.node_detail_screen import _located

    assert _located(45.5, -73.6) is True
    assert _located(None, -73.6) is False  # missing coordinate
    assert _located(0.0, 0.0) is False  # null island (no-GPS sentinel)
    assert _located(-97.0, -1041.97) is False  # the Homestead out-of-range advert


# --- the screen -----------------------------------------------------------------


def _header() -> Text:
    header = Text("▲ ", style="#a78bfa")
    header.append("Hub", style="accent")
    header.append("   repeater", style="muted")
    return header


def _screen(**over) -> NodeDetailScreen:
    """A node detail screen over hand-built display data (Info + a bare-note Routes tab)."""
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
    """The Info tab carries the vitals and its actions; Routes carries the stage + Trace."""
    screen = _screen()
    screen.note_viewport(30)
    body = _plain(screen.render_body(72))
    assert "Hub" in body and "repeater" in body  # the pinned identity header
    assert "│  Info  │" in body and "42" in body  # the vitals moved into the boxed Info tab
    assert "Time machine" in body  # the Info tab's own action
    assert "Back" not in body  # no exit row anywhere: Esc leaves
    assert "────────" in body  # the faint rule closing the stage
    assert "no route observed yet" not in body  # the Routes stage waits on its own tab

    screen.handle("tab")
    body = _plain(screen.render_body(72))
    assert "│  Routes  │" in body and "no route observed yet" in body
    assert "Trace" in body and "Back" not in body
    assert "packets" not in body  # the vitals stay on the Info tab
    # Every rendered line fits the 72-column standard.
    for line in screen.render_body(72):
        assert len(_plain([line])) <= 72
    assert len(screen.footer_hint) <= 72


def test_the_remove_row_is_tinted_and_keeps_its_tint_where_the_icon_lane_goes() -> None:
    """The destructive action announces itself in ``err`` on both platforms.

    On the desktop that is the red 🗑 leading the row. On the PicoCalc the icon lane is
    dropped whole (``command_icon`` returns ``""``), and a tint that left with the
    decoration would leave a delete row reading like any other — so it lands on the label,
    exactly as :func:`~meshterm.ui.menus.marked_label` does for a menu row.
    """
    import re

    from meshterm.platforms import PICOCALC, set_platform

    remove = _Action("remove", "🗑", "err", "Remove contact…")
    screen = _screen(info_actions=[remove])
    screen.note_viewport(30)
    line = screen._action_line(remove, selected=False, width=40)
    assert "🗑" in line and "Remove contact…" in _plain([line])

    set_platform(PICOCALC)
    screen = _screen(info_actions=[remove])
    screen.note_viewport(30)
    folded = screen._action_line(remove, selected=False, width=40)
    assert "🗑" not in folded  # the icon lane is gone...
    assert "Remove contact" in _plain([folded])
    # ...and the tint it carried is now on the words: the label is opened by a colour
    # sequence, where an untinted action's label is opened by nothing at all.
    assert re.search(r"\[[0-9;]*m *Remove contact", folded)
    plain_row = screen._action_line(
        _Action("timemachine", "⏳", "", "Time machine — 42 receptions"), False, 40
    )
    assert not re.search(r"\[[0-9;]*m *Time machine", plain_row)


def test_the_remove_row_resolves_its_own_token() -> None:
    """Enter on the remove row hands ``open_node_detail`` the token it deletes on."""
    screen = _screen(info_actions=[_Action("remove", "🗑", "err", "Remove contact…")])
    resolved: list = []
    screen.resolve = lambda value: resolved.append(value)  # type: ignore[method-assign]
    screen.note_viewport(30)
    screen.render_body(72)
    screen.handle("enter")
    assert resolved == ["remove"]


def test_node_detail_screen_cursor_and_commit() -> None:
    """↑/↓ move the cursor (kept in view); Enter resolves its key, Esc cancels."""
    screen = _screen()
    resolved: list = []
    screen.resolve = lambda value: resolved.append(value)  # type: ignore[method-assign]

    assert screen.cursor_line() is None  # nothing rendered yet
    screen.note_viewport(30)
    screen.render_body(72)
    # The cursor opens on the Info tab's first row (Time machine) and is always reported,
    # so the frame can keep it visible on a terminal too short for the pinned layout.
    assert screen.cursor_line() is not None
    screen.render_body(72)
    screen.handle("enter")
    assert resolved == ["timemachine"]

    screen.handle("escape")
    assert resolved == ["timemachine", CANCEL]


def test_the_vitals_come_back_with_edge_scroll() -> None:
    """On a frame too short for the Info tab, ↑ on the first action scrolls the vitals back.

    Walking down the actions scrolls the identity and the vitals off the top, and none of
    them can take the highlight — so before edge scroll there was no way back to them short
    of Home. ↑ on the first action now scrolls the page a line at a time to its top.
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
    assert not any("Hub" in line for line in view())  # …the identity still scrolled off
    for _ in range(10):
        press("up")  # edge scroll: the page, not the highlight
        view()
    page = view()
    assert "Hub" in page[0]  # the identity line is the top of the view again
    press("down")  # the snap-back brings the first action home before anything moves
    assert any("❯" in line and "Action 0" in line for line in view())


def test_node_detail_carries_no_exit_row() -> None:
    """The page offers no Back row: Esc leaves, and a row repeating it was retired.

    What the page *does* offer is only its own verbs, so the action cursor never has a
    do-nothing stop to arrow past on the way to them.
    """
    screen = _screen()
    resolved: list = []
    screen.resolve = lambda value: resolved.append(value)  # type: ignore[method-assign]

    screen.note_viewport(30)
    assert "Back" not in _plain(screen.render_body(72))
    screen.handle("escape")
    assert resolved == [CANCEL]


def _two_routes() -> _RoutesView:
    """A routes view with two selectable routes, the way the assembly hands them over."""
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
    """↑/↓ over the route rows moves the graph highlight and the spec a trace would arm on."""
    screen = _screen(routes=_two_routes(), tabs=[_Tab("Routes", "routes")])
    # Focusables: route 0, route 1 — with routes listed, the rows are the trace entry
    # points, so no dedicated Trace action row renders, and the tab carries no other row.
    assert screen.selected_spec() == "3d,f2,3d"
    screen.handle("down")  # onto route 1
    assert screen._route_sel == 1 and screen.selected_spec() == "a1,f2,a1"
    screen.note_viewport(30)
    body = _plain(screen.render_body(72))
    assert "via Hub …" in body and "via Alt …" in body  # both rows drew, …-marked as openers
    assert "Trace" not in body  # the auto-route stand-in only shows with no routes to list


def test_node_detail_routes_stage_spends_the_caption_and_top_air_on_the_fan() -> None:
    """On the PicoCalc the Routes stage spends the caption and the top air on the fan.

    The fan opens directly under the tab strip and runs uncaptioned, and both reclaimed
    rows go back into the graph — never into a taller gap (JP, 2026-08-09).

    The graph's own end padding leaves a whole empty canvas row above its topmost label; the
    stage draws one row over its ceiling and peels that row off, so the peel pays for itself.
    """
    from meshterm.platforms import PICOCALC, REGULAR, set_platform

    # A fan busy enough that the row ceiling — not its own ideal height — is what sizes it,
    # so the reclaimed rows show up as lanes rather than as slack.
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
        return [_plain([ln]) for ln in lines[strip_idx + 2 : rule_idx]]  # past the strip's own rule

    set_platform(PICOCALC)
    pico = _stage(22)
    assert pico[0].strip(), "no blank row between the tab strip and the fan"
    assert not any("selected route" in line for line in pico), "the caption is dropped"

    set_platform(REGULAR)
    desktop = _stage(22)
    assert not desktop[0].strip()  # the desktop keeps its air…
    assert any("node → you, as heard" in line for line in desktop)  # …and its caption
    # Both rows land in the drawing, not in the chrome: from the same viewport the console
    # grants this ceiling-bound fan two more canvas rows than the captioned desktop stage.
    drawn = [ln for ln in desktop if "node → you" not in ln]
    assert len(pico) == len(drawn) + 2


def test_node_detail_screen_context_hangs_under_the_pathline() -> None:
    """The weakest/samples/tag context draws on its own line, indented under the pathline."""
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
    assert "f2 3d aa …" in body  # the pathline, opens-marked
    assert "weakest -6.0 dB" in body and "★ best" in body  # the context, drawn too
    path_idx = next(i for i, ln in enumerate(lines) if "f2 3d aa" in _plain([ln]))
    assert _plain([lines[path_idx]]).startswith("❯ f2 3d aa")  # the pointer leads the pathline
    context_line = _plain([lines[path_idx + 1]])
    assert context_line.startswith("  weakest")  # hanging two columns under it, no pointer


def test_node_detail_screen_context_line_absent_when_theres_nothing_to_show() -> None:
    """A route with nothing to say draws just its pathline.

    No weakest SNR, sample count, or tag means no bare hanging line under it.
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
    # Nothing at all hangs under a route that earned no context: the pathline is the last
    # thing the page draws (the tab carries no action rows), not a muted context line.
    rest = _plain(lines[path_idx + 1 :]).strip()
    assert not rest.startswith("weakest") and not rest


def test_node_detail_screen_hscrolls_the_selected_pathline() -> None:
    """A pathline too wide for the lane scrolls with ←/→, on the highlighted row only.

    Moving the cursor off it abandons the scroll, and an unselected long row just
    ellipsizes.
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
    assert "f2 00 01 02" in body  # unscrolled, the chain's start shows
    assert "←→ scroll" in screen.footer_hint  # the overflow earns the footer atom

    row = next(ln for ln in _plain(screen.render_body(72)).splitlines() if ln.startswith("❯"))
    assert row.rstrip().endswith("…")  # the right edge marks the remainder, before any scroll

    screen.handle("right")
    screen.handle("right")
    body = _plain(screen.render_body(72))
    assert "f2 00 01 02" not in body  # the view has shifted away from the start
    row = next(ln for ln in body.splitlines() if ln.startswith("❯"))
    assert row.startswith("❯ …")  # …and now a mark says the line continues behind us, too

    screen.handle("left")
    body = _plain(screen.render_body(72))
    assert "f2 00 01 02" not in body  # one step back, still short of the start

    for _ in range(20):  # run the scroll to its stop
        screen.handle("right")
    row = next(ln for ln in _plain(screen.render_body(72)).splitlines() if ln.startswith("❯"))
    # The clamp lands where the tail is actually readable: the line's own end — its last hop
    # — is on screen rather than cropped behind a right-hand edge mark promising a remainder
    # the keys can no longer reach. Only the left mark is left drawn; the opens-further `…`
    # rides outside the lane, so a window filled to its edge is exactly where it goes unshown.
    assert row.rstrip().endswith("aa") and row.count("…") == 1
    screen.handle("right")  # …and the stop holds: nothing moves past it
    assert (
        next(ln for ln in _plain(screen.render_body(72)).splitlines() if ln.startswith("❯")) == row
    )

    screen.handle("down")  # onto route 1 — abandons route 0's scroll, short row can't scroll
    screen.render_body(72)
    assert "←→ scroll" not in screen.footer_hint  # route 1's short pathline earns no hint

    screen.handle("up")  # back onto route 0
    body = _plain(screen.render_body(72))
    assert "f2 00 01 02" in body  # the shift reset, back at the start
    assert "←→ scroll" in screen.footer_hint  # route 0 overflows again


def test_node_detail_route_row_cracks_a_chip_path_at_both_edges() -> None:
    """In chips, a route row cracks at both edges rather than ellipsizing.

    Each edge the route runs past breaks the chip off on a half block: the row is a
    view onto a route that continues, and a cracked segment says so where three dots
    would claim a shortened word.
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
    assert row.rstrip().endswith(CRACK_TAIL) and "…" not in row  # cut at the lane's edge
    screen.handle("right")
    screen.handle("right")
    row = selected_row()
    assert row.startswith("❯ " + CRACK_HEAD)  # the start is off to the left now, cracked
    assert row.rstrip().endswith(CRACK_TAIL) and "…" not in row


def test_node_detail_route_row_spends_no_lane_cells_on_the_opens_marker() -> None:
    """A route that fills its lane exactly stays whole — the trailing ``…`` is not its cost.

    The mark says what Enter does; it is chrome, not route. Reserving two cells for it made
    a chain that fitted crack on its own last chip (JP, 2026-08-10) — the row claiming the
    walk ran on when it had in fact arrived, the crack being nothing but the closing cap
    with half of it taken away. Now the path is fitted to the whole lane and the mark takes
    whatever is left, which on a full lane is nothing at all.
    """
    avail = 72 - 2  # the row's lane: the width less the pointer column
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
    assert row == "❯ " + "x" * avail  # whole, uncut, and no mark squeezed in
    # …and with nothing to scroll, ←→ stay inert and unadvertised.
    assert "←→ scroll" not in screen.footer_hint

    # Two cells of slack is exactly what the mark costs, so there it is drawn.
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


#: A 64-digit key whose every byte is distinct (``000102…1e1f``), so a window's first digits
#: say exactly where the lane was cut — an off-by-one shift reads ``0001…`` as ``00102…``.
_LONG_KEY = "".join(f"{i:02x}" for i in range(32))


def _key_row(screen: NodeDetailScreen, width: int) -> str:
    """Render the Info tab and read back its key lane, as one plain line."""
    screen.note_viewport(30)
    rows = [_plain([line]) for line in screen.render_body(width)]
    return next(row for row in rows if row.startswith("key"))


def test_node_detail_key_lane_hscrolls_instead_of_wrapping() -> None:
    """The key lane holds one line and slides under ←/→ rather than wrapping.

    Byte-aligned windows, faint ``…`` marks at whichever edge it continues past, and
    clamped at both ends.
    """
    screen = _screen(info_rows=[("key", highlighted_hash(_LONG_KEY, 1)), ("heard", Text("5m ago"))])

    row = _key_row(screen, 53)
    assert row.startswith("key      0001") and row.endswith("…")  # the head, more to the right
    assert "1e1f" not in row and len(row) <= 53  # the tail is off-lane, not wrapped below it
    assert "←→ scroll key" in screen.footer_hint  # …and the overflow earns its footer atom

    screen.handle("right")
    row = _key_row(screen, 53)
    # Shifted a whole four bytes: the window opens on byte 4 (``04``), marked at both edges.
    assert row.startswith("key      …0405") and row.endswith("…")

    for _ in range(6):
        screen.handle("right")  # run at the end — the shift clamps where the tail lands
    row = _key_row(screen, 53)
    assert row.endswith("1e1f")  # the key's last byte, no trailing mark: this is the end
    assert row.startswith("key      …")  # …with the head now off to the left

    for _ in range(6):
        screen.handle("left")  # and back the other way, clamping at the head
    assert _key_row(screen, 53).startswith("key      0001")


def test_node_detail_key_lane_scroll_survives_the_cursor_and_resets_on_tab() -> None:
    """The key is pinned chrome, not a cursor row.

    Walking the action rows leaves its scroll alone, while leaving the tab drops it —
    and a key that fits earns no ←→ at all.
    """
    screen = _screen(info_rows=[("key", highlighted_hash(_LONG_KEY, 1)), ("heard", Text("5m ago"))])
    _key_row(screen, 53)
    screen.handle("right")
    screen.handle("down")  # onto Back — the reader's window holds where they left it
    assert _key_row(screen, 53).startswith("key      …0405")

    screen.handle("tab")  # over to Routes…
    screen.render_body(53)
    assert "←→" not in screen.footer_hint  # nothing over-wide there to scroll
    screen.handle("tab")  # …and back: the lane opens at the head again
    assert _key_row(screen, 53).startswith("key      0001")

    # A lane wide enough for the whole key draws it whole, with neither mark nor hint.
    row = _key_row(screen, 100)
    assert row.startswith(f"key      {_LONG_KEY}") and "…" not in row
    assert "←→" not in screen.footer_hint
    screen.handle("right")  # inert where there is nothing to read past the edge
    assert _key_row(screen, 100).startswith(f"key      {_LONG_KEY}")


def test_node_detail_enter_on_a_route_row_opens_its_trace() -> None:
    """Enter on any route row arms a trace on that route — no separate Trace row needed."""
    screen = _screen(routes=_two_routes(), tabs=[_Tab("Routes", "routes")])
    resolved: list = []
    screen.resolve = lambda value: resolved.append(value)  # type: ignore[method-assign]
    screen.handle("down")  # onto route 1
    screen.handle("enter")
    assert resolved == ["trace"] and screen.selected_spec() == "a1,f2,a1"


def test_route_labels_light_through_a_coalesced_hop() -> None:
    """A route drawn from a short hop id lights the wide marker the graph folds it into.

    Route evidence reaches the page at mixed hash widths — a 1-byte trace hop (``3d``)
    beside the same relay's full id on a sibling route. The graph coalesces the short id
    into the one wide marker, so the off-route dimming must test membership against the
    *drawn* ids: keyed on the raw draw hops, the selected route's own relay rendered muted
    while the white line rode straight through it (the TSFCT SUTTON-680M report).
    """
    lit: list[str] = []

    def base_rgb(node: str) -> tuple[int, int, int]:
        lit.append(node)  # the widget only consults us for nodes on the selected route
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
    assert "3d63c6429436" in lit  # the relay it rides through keeps its hue


def test_node_detail_route_list_windows_inside_the_page() -> None:
    """The route list windows inside the page rather than growing it.

    With more routes than fit, edge markers appear and the pinned chrome stays on
    screen, however many routes a busy node has.
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
    assert len(lines) <= 30  # the body fits the viewport — nothing scrolls off
    body = _plain(lines)
    assert "↓" in body and "more" in body  # the edge marker counts the hidden routes
    assert "── Routes ──" in body  # the pinned chrome above the list never leaves
    assert "PgUp/PgDn scroll" in screen.footer_hint  # paging advertised only when needed

    # Walking the cursor to the last route slides the window down to keep it visible.
    for _ in range(11):
        screen.handle("down")
    lines = screen.render_body(72)
    assert len(lines) <= 30
    assert "route 11" in _plain(lines) and screen.cursor_line() is not None


def test_node_detail_route_cursor_clamps_at_both_ends() -> None:
    """The windowed route list does not wrap.

    ↑ on the first row and ↓ past the last stay where they are, rather than hauling
    the window end to end.
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
    """The variable-height fit walks wrapped rows into view.

    It keeps whole blocks, spends marker lines only when rows are actually hidden, and
    walks the window down to the cursor's row.
    """
    window = ListWindow()
    top, count = window.fit_blocks([2, 2, 2, 2], 5, 3)  # cursor on the last 2-line row
    assert top + count == 4 and top == 2  # slid to the tail; the last two rows fit
    assert window.page == count  # the settled capacity is the paging stride
    top, count = window.fit_blocks([1, 1], 5, 0)
    assert (top, count) == (0, 2)  # everything fits: no window, no markers


def test_node_detail_screen_tabs_switch_the_stage() -> None:
    """←→ (and Tab) swap which view fills the stage; the footer offers the switch only then."""
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
    assert "Tab/⇧Tab switch" in screen.footer_hint  # two tabs, so the switch is advertised
    # Opens on the Info tab: the vitals, the located preview's caption, and its braille
    # edge scrub all belong to it.
    body = _plain(screen.render_body(72))
    assert "│  Info  │" in body and "centred here" in body
    assert screen.consume_edge_scrub() == 2

    # The F-key chip names where the switch would take you, so it flips with the stage.
    assert screen.fkey_lane[2].label == "Routes"
    screen.handle("tab")  # switch to the Routes tab
    body = _plain(screen.render_body(72))
    assert "│  Routes  │" in body and "no route observed yet" in body
    assert screen.consume_edge_scrub() == 0  # the braille preview isn't showing now
    assert screen.fkey_lane[2].label == "Info"


def test_node_detail_single_tab_hides_the_switch_hint() -> None:
    """A page with only one view drops the ←→ tab atom from its footer."""
    screen = _screen(tabs=[_Tab("Info", "info")])  # our own node: no Routes tab
    assert "←→ tab" not in screen.footer_hint
    assert "↑↓ move" in screen.footer_hint and screen.footer_hint.endswith("Esc back")
    # Nothing to switch to, so the lane leaves the slot empty rather than dimming it.
    assert screen.fkey_lane[2] is None


def test_toggling_the_lock_rewrites_the_rows_and_keeps_the_cursor_on_it() -> None:
    """Lock becomes Unlock on the same row, and Archive is withdrawn while the lock holds."""
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
