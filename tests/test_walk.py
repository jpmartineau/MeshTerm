# SPDX-License-Identifier: Apache-2.0
"""Mesh walk tests: the focus-and-walk browser over a hand-built topology.

The tests run the screen headless against a fake session. This is the same approach as in
the dashboard tests. render_body only returns lines, and handle() only changes state. Thus
the tests can check the walk, the backtrack, the find, and the split of the canvas and the
list without a terminal.
"""

from __future__ import annotations

from datetime import timedelta

from meshterm.core.models import Contact, utcnow
from meshterm.services.topology import MeshTopology
from meshterm.ui.pathline import (
    CRACK_HEAD,
    CRACK_TAIL,
    ELIDE_HEAD,
    SELF_GLYPH,
    PathLine,
)
from meshterm.ui.tui.render import render_to_ansi
from meshterm.ui.walk_screen import _HSCROLL_STEP, WalkScreen
from tests.conftest import plain as _plain  # the only function that strips and joins a screen

US = "aa" * 6
Hub = Contact(name="Hilltop-Repeater", public_key="3d" * 32, node_type=2)
ALICE = Contact(name="Alice", public_key="b2" * 32, last_seen=utcnow() - timedelta(minutes=5))


class _FakeSession:
    def __init__(self) -> None:
        self.repaints = 0

    def invalidate(self) -> None:
        self.repaints += 1


def _topo(*, with_island: bool = False) -> MeshTopology:
    """Us, Hub, and Alice as a chain of two rings, with an optional separate island pair."""
    topo = MeshTopology(US, contacts=[Hub, ALICE])
    hub = topo.canonical(Hub.public_key)
    alice = topo.canonical(ALICE.public_key)
    when = utcnow()
    topo.add_walk([topo.self_id, hub], snrs=[6.0], when=when, source="trace")
    topo.add_walk([hub, alice], snrs=[-2.0], when=when, source="packet")
    if with_island:
        topo.add_walk(["c3" * 6, "d4" * 6], snrs=[1.0], when=when, source="neighbour")
    return topo


def _screen(topo: MeshTopology, cell_h: int = 24) -> WalkScreen:
    screen = WalkScreen(
        session=_FakeSession(),
        topo=topo,
        contacts={
            topo.canonical(Hub.public_key): Hub,
            topo.canonical(ALICE.public_key): ALICE,
        },
        self_label="Homestead",
    )
    screen.note_viewport(cell_h)  # the frame records this before each real paint
    return screen


# --- rendering ----------------------------------------------------------------------------


def test_walk_opens_focused_on_us_with_the_link_list() -> None:
    """The default focus is our own node: the canvas, the legend, and its links below."""
    screen = _screen(_topo())
    body = _plain(screen.render_body(80))
    assert "Homestead" in body and "this device" in body
    assert "Links" in body and "strongest observed first" in body
    assert "Hilltop-Repeater" in body  # our one neighbour, as a row that the user can select
    assert "edge = SNR" in body  # the legend of the canvas
    # The title is only the name of the screen. The body under it shows the focus and the
    # size of the neighbourhood.
    assert screen.title == "Mesh walk"


def test_walk_link_rows_carry_snr_evidence_and_onward_count() -> None:
    """A neighbour row shows the SNR, the samples, the source tags, the age, and onward links."""
    screen = _screen(_topo())
    body = _plain(screen.render_body(80))
    row = next(line for line in body.split("\n") if "Hilltop-Repeater" in line and "❯" in line)
    assert "+6.0" in row  # the median SNR of the link
    assert "1×" in row  # samples
    assert "T" in row  # the tag of trace evidence
    assert "⋯ 1" in row  # one link onward (Hub to Alice)
    assert "3d" * 6 in row  # the whole id of 12 hex digits, not a truncated prefix…
    assert "…" not in row  # …and nothing in it is elided


def test_walk_link_row_shrinks_the_key_before_the_name() -> None:
    """A long name keeps its letters. The key gives space first, at a byte boundary.

    If the row cannot hold both a long name and the full id of 12 hex digits, the key is
    truncated to its lit hash and a ``…`` (an even count of whole bytes). The name does not
    lose characters, and the hash prefix is never the part that is removed.
    """
    long_repeater = Contact(
        name="Repeater-Downtown-01", public_key="d4c3b2a1f0e9" + "00" * 26, node_type=2
    )
    topo = MeshTopology(US, contacts=[long_repeater])
    node = topo.canonical(long_repeater.public_key)
    topo.add_walk([topo.self_id, node], snrs=[4.0], when=utcnow(), source="trace")
    screen = WalkScreen(
        session=_FakeSession(),
        topo=topo,
        contacts={node: long_repeater},
        self_label="Homestead",
        prefix_bytes=1,  # the first byte (``d4``) is the hash that addresses the node
    )
    screen.note_viewport(24)
    row = next(
        line
        for line in _plain(screen.render_body(68)).split("\n")
        if "Repeater-Downtown-01" in line and "❯" in line  # the list row, not a label of the canvas
    )
    assert "Repeater-Downtown-01" in row  # the name is whole, not clipped
    assert "…" in row  # the key is the lane that gave space
    assert "d4c3" in row  # its lit hash (and a byte or two more) remains
    assert "d4c3b2a1f0e9" not in row  # …but not the whole id, because it was truncated


def test_walk_graph_labels_names_in_full_when_the_canvas_has_room() -> None:
    """The canvas draws the name of a neighbour whole, not clipped to a short fixed limit.

    The fan pulls to the west of the edge, and each label is clamped to the room that is
    there. Thus a long contact name keeps its letters where the canvas can hold it.
    """
    long_names = [
        Contact(name="Lakesidetechnique", public_key="e8" * 32, node_type=2),
        Contact(name="Repeater-Downtown-01", public_key="d4" * 32, node_type=2),
    ]
    topo = MeshTopology(US, contacts=long_names)
    when = utcnow()
    for c, snr in zip(long_names, (8.0, 1.0), strict=True):
        topo.add_walk(
            [topo.self_id, topo.canonical(c.public_key)], snrs=[snr], when=when, source="trace"
        )
    screen = WalkScreen(
        session=_FakeSession(),
        topo=topo,
        contacts={topo.canonical(c.public_key): c for c in long_names},
        self_label="Homestead",
    )
    screen.note_viewport(24)
    # The canvas rows are all the rows above the legend line.
    lines = _plain(screen.render_body(80)).split("\n")
    canvas = "\n".join(lines[: next(i for i, ln in enumerate(lines) if "edge = SNR" in ln)])
    assert "Lakesidetechnique" in canvas  # 17 characters, drawn whole: no "Lakesidetechniq…"
    assert "Repeater-Downtown-01" in canvas  # 20 characters, whole


def test_walk_selected_link_lights_the_route_that_reaches_it() -> None:
    """A selected link draws its edge and the approach to the focus at full strength.

    Each link is stale, so an edge that is not highlighted fades to half. The edge of the
    selected neighbour and the came_from → focus approach are drawn with no fade. This is
    the lit route. The edge of a neighbour that is not selected stays faded.
    """
    from meshterm.ui.walk_screen import _snr_rgb

    stale = utcnow() - timedelta(days=10)  # older than a week, so _freshness is 0.5
    hub = Contact(name="Hub", public_key="3d" * 32, node_type=2)
    alice = Contact(name="Alice", public_key="b2" * 32)
    bob = Contact(name="Bob", public_key="c4" * 32)
    topo = MeshTopology(US, contacts=[hub, alice, bob])
    y = topo.canonical(hub.public_key)
    topo.add_walk([topo.self_id, y], snrs=[10.0], when=stale, source="trace")  # approach: green
    # amber, then red
    topo.add_walk([y, topo.canonical(alice.public_key)], snrs=[0.0], when=stale, source="trace")
    topo.add_walk([y, topo.canonical(bob.public_key)], snrs=[-15.0], when=stale, source="trace")
    screen = WalkScreen(
        session=_FakeSession(),
        topo=topo,
        contacts={topo.canonical(c.public_key): c for c in (hub, alice, bob)},
        self_label="Homestead",
    )
    screen.note_viewport(24)
    screen.render_body(80)
    screen.handle("enter")  # walk to Hub: now came_from is us, the approach edge
    # Put the selection on Alice (a fan neighbour, not the ⌫-back row).
    while screen._rows()[screen._index] != topo.canonical(alice.public_key):
        screen.handle("down")

    ansi_lines = screen.render_body(80)
    legend = next(i for i, ln in enumerate(ansi_lines) if "edge = SNR" in _plain([ln]))
    canvas = "".join(ansi_lines[:legend])

    def code(rgb: tuple[int, int, int]) -> str:
        return f"38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"

    def scaled(rgb: tuple[int, int, int], f: float) -> tuple[int, int, int]:
        return tuple(max(0, min(255, round(c * f))) for c in rgb)

    assert code(_snr_rgb(0.0)) in canvas  # the edge of Alice, amber at full strength
    assert code(scaled(_snr_rgb(-15.0), 0.5)) in canvas  # the edge of Bob stays faded…
    assert code(_snr_rgb(-15.0)) not in canvas  # …and never reaches full strength


def test_walk_empty_graph_renders_guidance() -> None:
    """With no evidence, the screen explains how the walk fills up."""
    topo = MeshTopology(US, contacts=[])
    screen = _screen(topo)
    body = _plain(screen.render_body(80))
    assert "no evidence to draw yet" in body
    assert "trace" in body


# --- walking ------------------------------------------------------------------------------


def test_walk_enter_walks_and_grows_the_trail() -> None:
    """Enter focuses the highlighted neighbour, and the breadcrumb trail shows the walk."""
    topo = _topo()
    screen = _screen(topo)
    screen.render_body(80)
    screen.handle("enter")  # walk to Hub (our only neighbour)
    assert screen._focus == topo.canonical(Hub.public_key)
    body = _plain(screen.render_body(80))
    assert "★ › Hilltop-Repeater" in body  # the trail, with our end as the star of the app
    # The focus line shows name (hash). The glyph shows the type, not a kind in words.
    assert "Hilltop-Repeater (3d)" in body and "1 hop out" in body
    assert "Alice" in body  # the onward neighbour of Hub is now a row
    # The screen does not offer the node that we walked in from as a link, because ⌫ is the
    # way back. Thus the list has only ways onward, and it opens on the strongest of them.
    assert screen._rows() == [topo.canonical(ALICE.public_key)]
    assert "⌫ back" not in body


def test_walk_backspace_steps_back_along_the_trail() -> None:
    """⌫ pops the trail one step. At the start of the trail, it does nothing."""
    topo = _topo()
    screen = _screen(topo)
    screen.render_body(80)
    screen.handle("enter")
    assert len(screen._trail) == 2
    screen.handle("backspace")
    assert screen._trail == [topo.self_id]
    screen.handle("backspace")  # already home: it does nothing
    assert screen._trail == [topo.self_id]


def test_walk_the_node_walked_in_from_is_not_offered_back_as_a_link() -> None:
    """The way back is ⌫, not a row. The screen does not offer a walk that undoes the last walk.

    Thus the list has only ways onward. This also makes row 0 the strongest link out of
    this node. Row 0 is where each reset of the highlight goes.
    """
    topo = _topo()
    screen = _screen(topo)
    screen.render_body(80)
    screen.handle("enter")  # us → Hub

    assert topo.self_id not in screen._rows()
    assert screen._rows() == screen._fan_nodes()  # the canvas draws the same set
    assert screen._index == 0
    strongest = max(
        screen._links_of(screen._focus),
        key=lambda pair: pair[1].median_snr if pair[1].median_snr is not None else -99,
    )[0]
    assert screen._rows()[screen._index] != topo.self_id
    assert strongest in (topo.self_id, screen._rows()[0])  # us is stronger, and excluded

    screen.handle("backspace")  # the way back, with the key
    assert screen._trail == [topo.self_id]


def test_walk_walking_to_an_earlier_node_drops_the_loop() -> None:
    """A walk back to an earlier node removes the loop.

    When the walk visits a node that is already on the trail again, the stack is truncated
    to the first appearance of that node. This removes the circular section that the walk
    made to get back there.
    """
    topo = MeshTopology(US, contacts=[Hub, ALICE])
    hub = topo.canonical(Hub.public_key)
    alice = topo.canonical(ALICE.public_key)
    when = utcnow()
    topo.add_walk([topo.self_id, hub], snrs=[6.0], when=when, source="trace")
    topo.add_walk([hub, alice], snrs=[-2.0], when=when, source="packet")
    topo.add_walk([topo.self_id, alice], snrs=[3.0], when=when, source="trace")  # closes the loop
    screen = _screen(topo)

    def walk_to(node: str) -> None:
        screen.render_body(80)
        screen._index = screen._rows().index(node)
        screen.handle("enter")

    walk_to(hub)  # us → Hub
    walk_to(alice)  # Hub → Alice
    assert screen._trail == [topo.self_id, hub, alice]
    # Alice → us closes the loop. It is a real link onward (it is not the node that we
    # arrived from, which is Hub), so the screen offers it. When the user takes it, the
    # section that the walk made to get here is removed.
    walk_to(topo.self_id)
    assert screen._trail == [topo.self_id]


def test_walk_trail_crops_its_head_not_its_tail_when_narrow() -> None:
    """A trail that is too long for the line is cropped at its start, as in the trophy case."""
    screen = _screen(_topo())
    us = screen._topo.self_id
    hub = screen._topo.canonical(Hub.public_key)
    alice = screen._topo.canonical(ALICE.public_key)
    screen._trail = [us, hub, alice]
    width = 20  # too narrow for the whole "Homestead › Hilltop-Repeater › Alice"
    text = screen._trail_text(width).plain
    assert text[0] in ("…", CRACK_HEAD)  # cut, not elided: no whole hop paid for the mark
    assert "⋯" not in text
    assert text.endswith("Alice")  # the focus is always kept
    assert "Homestead" not in text  # the start was cropped, not the tail
    assert len(text) == width  # a crop ends at the width, so it is already flush right


def test_walk_trail_crop_keeps_the_cells_an_elision_would_have_spent() -> None:
    """The purpose of the crop: the cells of the lane go to the walk, not to a removed hop."""
    screen, width = _walked_chain(8)
    cropped = screen._trail_text(width)
    elided = PathLine(
        [screen._trail_hop(node) for node in screen._trail], separator=" › "
    ).ellipsized(width, elide=ELIDE_HEAD)
    assert cropped.cell_len == width  # the crop fills the lane…
    assert elided.cell_len < width  # …where the removal of a whole hop left cells unused
    # The crop keeps the tail of the hop where it ended. The elision removed the whole hop.
    assert cropped.plain.startswith("…-05 ") and "-05" not in elided.plain


def test_walk_trail_sits_left_until_it_overflows() -> None:
    """A trail that fits is left-aligned. Only an elided trail goes to the right edge."""
    screen = _screen(_topo())
    screen._trail = [screen._topo.self_id]
    assert screen._trail_text(40).plain == SELF_GLYPH  # us, in one cell


def test_walk_trail_scrolled_back_to_its_head_sits_left_again() -> None:
    """The alignment follows the head, not the scroll. A visible start puts the line at the left."""
    screen, width = _walked_chain(8)
    steps = screen._trail_max_scroll(width) // _HSCROLL_STEP
    for step in range(1, steps):  # each stop before the head is still cropped
        screen.handle("left")
        cut = _plain([render_to_ansi(screen._trail_text(width), width, no_wrap=True)])
        assert cut[0] in ("…", CRACK_HEAD) and len(cut) == width, step

    screen.handle("left")  # the step that makes the start of the walk visible again
    assert screen._trail_scroll == steps * _HSCROLL_STEP
    home = _plain([render_to_ansi(screen._trail_text(width), width, no_wrap=True)])
    assert home.startswith(SELF_GLYPH)  # flush left, with no padding before the head


def test_walk_trail_names_carry_their_node_hues() -> None:
    """Trail names use the hue of the key of each node (ours is the white "you" style).

    The focus is bold.
    """
    from meshterm.ui.theme import node_style

    screen = _screen(_topo())
    us = screen._topo.self_id
    hub = screen._topo.canonical(Hub.public_key)
    screen._trail = [us, hub]
    text = screen._trail_text(80)
    plain = text.plain
    hub_at = plain.index("Hilltop-Repeater")
    assert any(
        s.start <= hub_at < s.end and "bold" in str(s.style) and node_style(hub) in str(s.style)
        for s in text.spans
    )


def test_walk_locate_refocuses_us_and_home_end_walk_the_list() -> None:
    """^Y resets the walk to our own node from anywhere. Home and End go to the ends of the list."""
    # A hub with spokes from one of our neighbours: a focus away from us, with a list to move in.
    topo = MeshTopology(US, contacts=[Hub])
    hub = topo.canonical(Hub.public_key)
    when = utcnow()
    topo.add_walk([topo.self_id, hub], snrs=[6.0], when=when, source="trace")
    for i in range(5):
        topo.add_walk([hub, f"{i:02x}" * 6], snrs=[5.0 - i], when=when, source="trace")
    screen = _screen(topo)
    screen.render_body(80)
    screen.handle("enter")  # us → Hub
    screen.render_body(80)
    rows = screen._rows()
    assert len(rows) > 1 and topo.self_id not in rows

    # Home and End move the highlight. They do not abandon the walk.
    screen.handle("end")
    assert screen._index == len(rows) - 1 and screen._trail == [topo.self_id, hub]
    screen.handle("home")
    assert screen._index == 0 and screen._trail == [topo.self_id, hub]

    screen.handle("locate")
    assert screen._trail == [topo.self_id]


def test_walk_echoes_the_find_query_above_the_matches_it_narrows() -> None:
    """The PicoCalc draws no hint line, so the query that the hint has moves into the body."""
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform

    try:
        set_platform(REGULAR)
        desktop = _screen(_topo())
        desktop.render_body(80)
        for ch in "al":
            desktop.handle("text", ch)
        body = _plain(desktop.render_body(80))
        assert "/al" not in body and "find: al" in desktop.footer_hint

        set_platform(PICOCALC_LYRA)
        device = _screen(_topo())
        device.render_body(53)
        for ch in "al":
            device.handle("text", ch)
        lines = [_plain(line) for line in device.render_body(53)]
        assert len(lines) <= 24  # still inside the viewport that the test gave
        # The echo is directly above the heading of the list that it narrows. The list, not
        # the canvas, gives the row for it.
        echo = next(i for i, line in enumerate(lines) if line.strip() == "/al")
        assert lines[echo + 1].startswith("Matches")
        # When the find is cleared, the row goes with it.
        for _ in "al":
            device.handle("backspace")
        assert not any(
            line.strip().startswith("/") for line in (_plain(ln) for ln in device.render_body(53))
        )
    finally:
        set_platform(REGULAR)


def test_walk_fkey_lane_gives_you_its_own_slot_over_the_pagers_ends() -> None:
    """``You`` is a verb of this screen (F3), not an end of the list that the pager scrolls."""
    from meshterm.ui.tui.fkeys import PICOCALC_LYRA_DECK

    screen = _screen(_topo())
    screen.render_body(80)
    lane = screen.picocalc_lyra_lane

    assert [pair.label if pair else None for pair in lane] == [
        None,
        None,
        "You",
        "Page ↓",
        "Page ↑",
    ]
    assert PICOCALC_LYRA_DECK.action_for(lane, 3) == "locate"
    assert lane[2].opp_label == ""  # nothing is the opposite of going home
    # Also, the Shift bank of the pager has the same meaning as on each other screen: the
    # two ends of the list.
    assert lane[4].opp_label == "Top" and PICOCALC_LYRA_DECK.action_for(lane, 10) == "home"
    assert lane[3].opp_label == "Bottom" and PICOCALC_LYRA_DECK.action_for(lane, 9) == "end"


def test_walk_you_dims_once_the_focus_is_already_us() -> None:
    """A dim chip means that the action exists here but is not available now.

    Here there is nothing to come back from.
    """
    screen = _screen(_topo())
    screen.render_body(80)
    assert screen.picocalc_lyra_lane[2].enabled is False  # opens on us, with a trail of one

    screen.handle("enter")  # walked away: now there is something to come back from
    assert screen.picocalc_lyra_lane[2].enabled is True

    screen.handle("locate")
    assert screen.picocalc_lyra_lane[2].enabled is False
    screen.handle("text", "a")  # a find narrows the list away from our own neighbours too
    assert screen.picocalc_lyra_lane[2].enabled is True


def test_walk_the_fan_is_all_east_and_holds_no_came_from() -> None:
    """Each node that the canvas draws is a way onward, east of the focus. No node is behind it."""
    topo = _topo()
    screen = _screen(topo)
    screen.render_body(80)
    screen.handle("enter")  # focus Hub, and we came from us
    alice = topo.canonical(ALICE.public_key)
    fx, _fy = screen._focus_pos(80, 12)
    placed = screen._place_neighbours(80, 12, screen._fan_nodes(), False)
    assert topo.self_id not in placed  # the node that we came from is behind us, not on screen
    assert list(placed) == [alice]
    assert placed[alice][0] > fx  # the fan is east of the focus
    assert fx <= (80 * 2) // 3  # and the focus is at the left


def test_walk_fan_rim_leaves_spread_east_not_curling_back() -> None:
    """The fan is a flat arc. The top and bottom leaves also reach far to the east.

    On the old circular arc, only the leaf due east reached the far side. The rim leaves
    curled back toward the focus (to approximately 0.31 of the reach), and the corners were
    empty. The wedge makes the horizontal reach flat, so a rim leaf is east of the midpoint
    between the anchor of the fan and its tip due east. Thus the fan spreads across the width.
    """
    fan = [f"{i + 0x20:02x}" * 6 for i in range(5)]
    topo = MeshTopology(US, contacts=[])
    when = utcnow()
    for i, node in enumerate(fan):
        topo.add_walk([topo.self_id, node], snrs=[5.0 - i], when=when, source="trace")
    screen = _screen(topo)
    ax, _ay, _name = screen._focus_anchor(80, 14)
    placed = screen._place_neighbours(80, 14, fan, False)
    tip = max(x for x, _y in placed.values())  # the leaf due east, the far tip of the fan
    midpoint = ax + (tip - ax) / 2
    assert placed[fan[0]][0] > midpoint  # the top leaf reaches past halfway east…
    assert placed[fan[-1]][0] > midpoint  # …and so does the bottom leaf


def _hub_topo(spokes: int) -> MeshTopology:
    """Us at the centre of a hub with ``spokes`` neighbours, with decreasing strengths."""
    topo = MeshTopology(US, contacts=[])
    when = utcnow()
    for i in range(spokes):
        node = f"{i:02x}" * 6
        for _ in range(spokes - i):  # more samples is stronger, so the order is fixed
            topo.add_walk([topo.self_id, node], snrs=[5.0], when=when, source="trace")
    return topo


def test_walk_collapses_the_weak_links_into_one_ellipsis_marker() -> None:
    """Past the capacity of the area, the weaker neighbours fold into one ``…`` node."""
    screen = WalkScreen(session=_FakeSession(), topo=_hub_topo(14), contacts={}, self_label="us")
    screen.note_viewport(20)
    body = _plain(screen.render_body(80))
    assert "weaker" in body  # the label of the collapsed marker is "+n weaker"
    canvas_part = body.split("Links")[0]
    assert "…" in canvas_part
    # The list still names each neighbour. The list is where the user selects.
    assert len(screen._rows()) == 14


def test_walk_fan_stays_sparse_and_collapses_the_rest() -> None:
    """The fan is sparse by design. Even a small hub collapses its weakest links."""
    screen = WalkScreen(session=_FakeSession(), topo=_hub_topo(8), contacts={}, self_label="us")
    screen.note_viewport(20)
    canvas_part = _plain(screen.render_body(80)).split("Links")[0]
    assert "…" in canvas_part and "weaker" in canvas_part  # not all eight are drawn


def test_walk_labels_non_selected_nodes_to_the_right_of_their_icon() -> None:
    """The name of a fan node is directly to the right of its marker, in the reading direction."""
    from meshterm.ui.mapcanvas import MapCanvas

    screen = _screen(_topo())
    canvas = MapCanvas(80, 12)
    canvas.marker(40, 20, "●", (255, 255, 255))  # a marker with room to its east
    screen._label_right(canvas, 40, 20, "Bravo", (200, 200, 200))
    marker_cx = 40 >> 1
    assert canvas._label_cells  # the name was placed
    assert all(cx > marker_cx for cx, _cy in canvas._label_cells)  # each cell is east


def test_walk_selecting_a_collapsed_row_lights_the_ellipsis_with_its_name() -> None:
    """A highlight on a weak (collapsed) row shows its name at the ``…`` marker."""
    screen = WalkScreen(session=_FakeSession(), topo=_hub_topo(14), contacts={}, self_label="us")
    screen.note_viewport(20)
    screen.render_body(80)
    screen._index = len(screen._rows()) - 1  # the weakest row, which is collapsed
    body = _plain(screen.render_body(80))
    weakest = screen._rows()[-1][:8]
    canvas_part = body.split("Links")[0]
    assert weakest in canvas_part  # the ellipsis marker has the label of the selection
    assert "weaker" not in canvas_part  # ...and replaces the "+n weaker" count


def test_walk_body_fits_the_viewport_and_windows_the_list() -> None:
    """The screen never grows past the frame. Only the link list scrolls, and it has marks."""
    screen = WalkScreen(session=_FakeSession(), topo=_hub_topo(16), contacts={}, self_label="us")
    screen.note_viewport(22)
    lines = screen.render_body(80)
    assert len(lines) <= 22  # canvas, chrome, and list window together are the viewport
    body = _plain(lines)
    assert "↓" in body and "more" in body  # the window has a mark for the rows below
    assert "Links" in body


def test_walk_pgdn_pages_the_highlight_by_the_list_window() -> None:
    """PgUp and PgDn move by the size of the list window, and the window follows the highlight."""
    screen = WalkScreen(session=_FakeSession(), topo=_hub_topo(16), contacts={}, self_label="us")
    screen.note_viewport(22)
    screen.render_body(80)
    stride = screen._list.page
    assert stride >= 1
    screen.handle("pagedown")
    assert screen._index == min(15, stride)
    for _ in range(6):
        screen.handle("pagedown")
    assert screen._index == 15  # clamped at the last row
    body = _plain(screen.render_body(80))
    assert "↑" in body and "more" in body  # the rows scrolled off above have a count


# --- find ---------------------------------------------------------------------------------


def test_walk_find_lists_matches_and_teleports() -> None:
    """Typing filters all the known nodes. Enter focuses the match and restarts the trail."""
    topo = _topo()
    screen = _screen(topo)
    screen.render_body(80)
    for ch in "ali":
        screen.handle("text", ch)
    assert "find: ali" in screen.footer_hint
    body = _plain(screen.render_body(80))
    assert "Matches" in body and "Alice" in body
    assert "2 hops out" in body  # the match row shows how far away the node is
    assert screen.title == "Mesh walk"  # the title does not change. The list is the answer

    screen.handle("enter")
    alice = topo.canonical(ALICE.public_key)
    assert screen._focus == alice
    assert screen._trail == [alice]  # a jump restarts the trail
    assert screen._filter == ""


def test_walk_find_marks_islands() -> None:
    """A match with no path to us shows island, and its focus says why."""
    screen = _screen(_topo(with_island=True))
    screen.render_body(80)
    for ch in "c3":
        screen.handle("text", ch)
    body = _plain(screen.render_body(80))
    assert "island" in body
    screen.handle("enter")
    body = _plain(screen.render_body(80))
    assert "island — no observed path to you" in body


def test_walk_esc_peels_find_then_dismisses() -> None:
    """The Esc key first clears an active find. The next Esc leaves the screen."""
    import asyncio

    screen = _screen(_topo())
    screen.render_body(80)
    screen.handle("text", "a")

    async def drive() -> object:
        screen.future = asyncio.get_running_loop().create_future()
        screen.handle("escape")  # peels the filter
        assert screen._filter == "" and not screen.future.done()
        screen.handle("escape")  # leaves the screen
        return await screen.future

    assert asyncio.run(drive()) is None


def _chain(length: int) -> MeshTopology:
    """Us, hop-01, hop-02, and so on. Each name is long enough to overflow a narrow trail."""
    contacts = [
        Contact(name=f"Repeater-{i:02d}", public_key=f"{i:02x}" * 32) for i in range(1, length + 1)
    ]
    topo = MeshTopology(US, contacts=contacts)
    when = utcnow()
    previous = topo.self_id
    for contact in contacts:
        node = topo.canonical(contact.public_key)
        topo.add_walk([previous, node], snrs=[4.0], when=when, source="trace")
        previous = node
    return topo


def _walked_chain(length: int, *, width: int = 46):
    """A screen walked to the end of a chain of `length` hops, rendered at `width`."""
    topo = _chain(length)
    contacts = {
        topo.canonical(f"{i:02x}" * 32): Contact(
            name=f"Repeater-{i:02d}", public_key=f"{i:02x}" * 32
        )
        for i in range(1, length + 1)
    }
    screen = WalkScreen(
        session=_FakeSession(), topo=topo, contacts=contacts, self_label="Homestead"
    )
    screen.note_viewport(24)
    for _ in range(length):
        screen.render_body(width)
        rows = screen._rows()
        # Always step onward, never back through the node that we came from.
        screen._index = next(i for i, node in enumerate(rows) if node not in screen._trail)
        screen.handle("enter")
    screen.render_body(width)
    return screen, width


def test_walk_trail_slides_a_window_over_the_walk_and_cracks_both_edges() -> None:
    """The ← key reads back over a long walk, and the → key returns.

    Each edge where the walk goes on has a crack.
    """
    screen, width = _walked_chain(8)
    trail = _plain([render_to_ansi(screen._trail_text(width), width, no_wrap=True)])

    # At rest the window is at the tail. The focus shows, and the start is cropped off at the
    # left. The end of the walk needs no mark, because the walk really ends there.
    assert "Repeater-08" in trail and "Homestead" not in trail
    assert trail[0] in ("…", CRACK_HEAD) and len(trail) == width
    assert trail[-1] not in ("…", CRACK_TAIL) and "⋯" not in trail
    assert screen._trail_scroll == 0

    screen.handle("left")
    slid = _plain([render_to_ansi(screen._trail_text(width), width, no_wrap=True)])
    assert screen._trail_scroll == _HSCROLL_STEP  # the step of the app, in cells and not hops
    # The walk moved out of view on the right, and the line says so in the same way as on the
    # left: with the cut mark, never the ⋯. Nothing here is elided. The lane ran out.
    assert "Repeater-08" not in slid and "Repeater-07" in slid
    assert slid[-1] in ("…", CRACK_TAIL) and "⋯" not in slid
    assert len(slid) == width

    screen.handle("right")
    assert screen._trail_scroll == 0
    assert _plain([render_to_ansi(screen._trail_text(width), width, no_wrap=True)]) == trail


def test_walk_trail_scroll_stops_at_the_head_and_resets_with_the_trail() -> None:
    """The ← key stops where the start of the walk is visible. A change to the trail rewinds it."""
    screen, width = _walked_chain(8)
    limit = screen._trail_max_scroll(width)
    assert limit > 0

    for _ in range(limit + 5):  # a held ← stores no extra steps to undo
        screen.handle("left")
    assert screen._trail_scroll == limit
    head = _plain([render_to_ansi(screen._trail_text(width), width, no_wrap=True)])
    assert head.lstrip().startswith(SELF_GLYPH)  # scrolled far enough to see the start

    screen.handle("backspace")  # a step back is a change to the trail
    assert screen._trail_scroll == 0
    screen.render_body(width)
    screen.handle("left")
    screen.handle("locate")  # a return home is also a change
    assert screen._trail_scroll == 0


def test_walk_trail_scroll_is_inert_on_a_walk_that_fits() -> None:
    """A trail with nothing hidden has nothing to scroll, and the hint does not offer it."""
    screen = _screen(_topo())
    screen.render_body(80)
    assert screen._trail_max_scroll(80) == 0
    assert "←→ trail" not in screen.footer_hint
    assert "type to find" in screen.footer_hint

    screen.handle("left")
    assert screen._trail_scroll == 0

    walked, width = _walked_chain(8)
    assert "←→ trail" in walked.footer_hint  # the hint shows it only where it does something
    assert len(walked.footer_hint) <= 72


def test_walk_find_narrows_the_canvas_fan_but_not_the_walk() -> None:
    """Typing thins the ways onward. The focus and the node that the walk came from stay."""
    bob = Contact(name="Bob-Tower", public_key="c7" * 32)
    topo = MeshTopology(US, contacts=[Hub, ALICE, bob])
    hub, alice = topo.canonical(Hub.public_key), topo.canonical(ALICE.public_key)
    bob_id = topo.canonical(bob.public_key)
    when = utcnow()
    topo.add_walk([topo.self_id, hub], snrs=[6.0], when=when, source="trace")
    topo.add_walk([hub, alice], snrs=[-2.0], when=when, source="packet")
    topo.add_walk([hub, bob_id], snrs=[1.0], when=when, source="packet")

    screen = WalkScreen(
        session=_FakeSession(),
        topo=topo,
        contacts={hub: Hub, alice: ALICE, bob_id: bob},
        self_label="Homestead",
    )
    screen.note_viewport(24)
    screen.render_body(80)
    screen._index = screen._rows().index(hub)
    screen.handle("enter")  # focus Hub, came from us

    canvas = _plain(screen._canvas_lines(80, 12, None))
    assert "Alice" in canvas and "Bob-Tower" in canvas  # the whole fan, with no filter

    for ch in "ali":
        screen.handle("text", ch)
    canvas = _plain(screen._canvas_lines(80, 12, None))
    assert "Alice" in canvas  # the way onward that the query matches
    assert "Bob-Tower" not in canvas  # and not the way that it does not match
    # The focus is not a candidate. It is the walk so far, and it stays for each query.
    assert "Hilltop-Repeater" in canvas


def test_walk_marks_wear_their_node_type_colour_not_the_key_hue() -> None:
    """A mark is a glyph and a colour: the same mark as the map and the route graph use."""
    from meshterm.ui.marks import NODE_MARK, REPEATER_MARK, SELF_MARK
    from meshterm.ui.theme import mark_rgb, node_style

    topo = _topo()
    screen = _screen(topo)
    hub, alice = topo.canonical(Hub.public_key), topo.canonical(ALICE.public_key)

    assert screen._glyph(hub) == (REPEATER_MARK[0], "type.repeater")
    assert screen._glyph(alice) == (NODE_MARK[0], "type.node")
    assert screen._marker_rgb(hub) == mark_rgb("type.repeater")
    assert screen._marker_rgb(topo.self_id) == mark_rgb(SELF_MARK[1])
    # Identity has its own lane and keeps it: the name has the hue of the key.
    assert screen._marker_rgb(hub) != screen._label_rgb(hub)
    assert screen._label_rgb(hub) == _plain_rgb(node_style(hub))
    # The legend is a sample of those marks, so it shows the colour and the shape.
    legend = screen._legend()
    hues = {
        legend.plain[span.start : span.end]: str(span.style)
        for span in legend.spans
        if span.end - span.start == 1
    }
    assert hues[REPEATER_MARK[0]] == "type.repeater"
    assert hues[NODE_MARK[0]] == "type.node"
    assert hues[SELF_MARK[0]] == SELF_MARK[1]


def _plain_rgb(style: str) -> tuple[int, int, int]:
    from meshterm.ui.mapcanvas import parse_hex

    return parse_hex(style.rsplit("#", 1)[-1])


def test_walk_fan_rows_leave_a_blank_line_between_each_and_around() -> None:
    """One marker in each row, a blank row between the rows, and a blank row above and below."""
    screen = _screen(_hub_topo(4))
    rows = screen._fan_rows(11, 5)
    assert rows == [1, 3, 5, 7, 9]  # 2n+1 = 11 rows exactly: 1 top, 1 bottom, 1 between
    # The focus is level with the middle of the block, not the middle of the canvas. Thus the
    # edges that leave it fan out in a symmetric way.
    _fx, fy = screen._focus_pos(80, 11, 5)
    assert fy >> 2 == rows[len(rows) // 2]
    # A fan with fewer nodes than rows keeps the space. The block is centred in the rows.
    assert screen._fan_rows(11, 3) == [3, 5, 7]


def test_walk_fan_gives_up_its_outer_rows_before_it_gives_up_a_node() -> None:
    """A blank row is worth a row until it costs a node. Below seven, the padding goes."""
    screen = _screen(_hub_topo(4))
    # Tall enough for seven with a blank row at the top and bottom (2·7+1 = 15): keep them.
    assert screen._fan_capacity(15) == 7
    assert screen._fan_rows(15, 7) == [1, 3, 5, 7, 9, 11, 13]
    # One row less than that: the outer rows go to the seventh marker.
    assert screen._fan_capacity(14) == 7
    assert screen._fan_rows(14, 7)[0] == 0
    # More room: the padding grows. It is not used for more nodes than fit.
    assert screen._fan_rows(19, 7)[0] == 3


def test_walk_collapsed_marker_stands_in_for_the_selected_weak_link() -> None:
    """A selected collapsed node makes the ``…`` marker be that node: mark, name, and rank."""
    screen = _screen(_hub_topo(12))
    screen.render_body(80)
    rows = screen._rows()
    capacity = screen._fan_capacity(14)
    hidden = rows[capacity - 1 :]
    assert len(hidden) > 1

    # When nothing is selected, the marker is the ellipsis with no name. It counts the nodes
    # that it holds.
    plain_canvas = _plain(screen._canvas_lines(80, 14, rows[0]))
    assert f"… +{len(hidden)} weaker" in plain_canvas
    assert "…" in plain_canvas

    # When it is selected, it has the type mark and the name of that node. It also has the
    # rank of the node among the collapsed nodes, in grey. Thus the count that the label had
    # is not lost.
    target = hidden[1]
    canvas = screen._canvas_lines(80, 14, target)
    body = _plain(canvas)
    # The rank is west of the mark. Thus the name still ends where each other name on the
    # fan ends, and the count reads as a note on the marker.
    glyph, _colour = screen._glyph(target)
    assert f"(2/{len(hidden)}) {glyph} {screen._label(target)}" in body
    assert "weaker" not in body
    # The name keeps its own hue and the counter is grey: two runs in one row.
    from meshterm.ui.theme import mark_rgb

    row = next(line for line in canvas if screen._label(target) in _plain([line]))
    assert _code(screen._label_rgb(target)) in row
    assert _code(mark_rgb("node.unknown")) in row


def _code(rgb: tuple[int, int, int]) -> str:
    return f"38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def _long_name_screen(names: list[str]) -> WalkScreen:
    """Us at the centre of a fan of `names`, with the strongest first."""
    contacts = [
        Contact(name=n, public_key=f"{i + 0x20:02x}" * 32, node_type=2) for i, n in enumerate(names)
    ]
    topo = MeshTopology(US, contacts=contacts)
    when = utcnow()
    for i, c in enumerate(contacts):
        node = topo.canonical(c.public_key)
        for _ in range(len(contacts) - i):
            topo.add_walk([topo.self_id, node], snrs=[8.0 - i], when=when, source="trace")
    screen = WalkScreen(
        session=_FakeSession(),
        topo=topo,
        contacts={topo.canonical(c.public_key): c for c in contacts},
        self_label="Homestead",
    )
    screen.note_viewport(24)
    return screen


def test_walk_only_the_long_named_marker_gives_up_reach_for_its_name() -> None:
    """A long name makes its own row lose some reach to the east. The rest of the fan stays."""
    long_name = "Sensor-Rooftop-Longueuil-East"  # 29 cells: more than any fixed margin
    screen = _long_name_screen([long_name, "Bob", "Carol"])
    body = _plain(screen._canvas_lines(80, 11, None))
    assert long_name in body  # written whole, with no trailing …

    nodes = screen._fan_nodes()
    placed = screen._place_neighbours(80, 11, nodes, False)
    # The same fan with a short name in that slot: only the marker with the long name moved.
    short = _long_name_screen(["Ann", "Bob", "Carol"])
    baseline = short._place_neighbours(80, 11, short._fan_nodes(), False)
    assert placed[nodes[0]][0] < baseline[short._fan_nodes()[0]][0]
    assert [placed[n][0] for n in nodes[1:]] == [baseline[n][0] for n in short._fan_nodes()[1:]]


def test_walk_fan_keeps_its_reach_when_a_name_would_swallow_the_canvas() -> None:
    """Past a limit, the labels are clipped instead. A fan on top of the focus draws nothing."""
    screen = _long_name_screen(["X" * 60, "Bob", "Carol"])
    ax, _ay, _n = screen._focus_anchor(53, 11, 3)
    placed = screen._place_neighbours(53, 11, screen._fan_nodes(), False)
    assert min(x for x, _y in placed.values()) > ax  # still a fan, not a pile on the anchor
    assert "…" in _plain(screen._canvas_lines(53, 11, None))  # the label gave space instead


def test_walk_collapsed_slot_is_laid_out_for_its_resting_label_not_the_selection() -> None:
    """A walk over the collapsed nodes must not move the fan, so the … slot ignores them.

    The geometry of the slot comes from the ``+n weaker`` that it shows at rest. The name of
    a stand-in node is fitted into the room that remains, and it is truncated if it must be.
    """
    screen = _long_name_screen(
        ["Ann", "Bob", "Carol", "Dee", "Eve", "Fay", "Gil", "Hal", "Sensor-Rooftop-Longueuil"]
    )
    screen.render_body(80)
    rows = screen._rows()
    hidden = rows[screen._fan_capacity(11) - 1 :]
    assert len(hidden) > 1

    at_rest = screen._canvas_lines(80, 11, rows[0])
    marks = [_plain([line]).index("…") for line in at_rest if "…" in _plain([line])]
    for node in hidden:
        moved = screen._canvas_lines(80, 11, node)
        glyph, _c = screen._glyph(node)
        row = next(line for line in moved if screen._label(node)[:8] in _plain([line]))
        assert _plain([row]).index(glyph) == marks[0]  # the marker did not move


def test_walk_the_highlighted_row_is_the_node_the_canvas_lights() -> None:
    """The list and the picture read the same index from the same list, at each position.

    The list was once rendered directly from the link table, but the highlight indexed the
    rows. The table still had the node that the walk came from. Thus, from the position of
    that node, each row named one link and drew another link. The highlight was one link
    away from the link that the canvas lit.
    """
    topo = _hub_topo(6)
    screen = _screen(topo)
    screen.render_body(80)
    screen._index = 1
    screen.handle("enter")  # walk out, so there is a came-from node to leave out
    screen.render_body(80)

    rows = screen._rows()
    assert topo.self_id not in rows  # …and it is not in the rows
    for index in range(len(rows)):
        screen._index = index
        body = _plain(screen.render_body(80))
        picked = next(line for line in body.split("\n") if line.startswith("❯"))
        assert screen._label(rows[index]) in picked
        # Also, the canvas lights that same node, not its neighbour in the table.
        canvas = _plain(screen._canvas_lines(80, 12, rows[index]))
        assert screen._label(rows[index]) in canvas


def test_walk_collapsed_stand_in_wears_the_selection_white() -> None:
    """A collapsed node is drawn only while it is selected, so its colour is white."""
    # The contacts have names, so the hue of the name is different from white.
    screen = _long_name_screen([f"Node-{i:02d}" for i in range(12)])
    screen.render_body(80)
    rows = screen._rows()
    target = rows[screen._fan_capacity(14) - 1 + 1]
    assert screen._label_rgb(target) != (255, 255, 255)

    canvas = screen._canvas_lines(80, 14, target)
    row = next(line for line in canvas if screen._label(target) in _plain([line]))
    assert _code((255, 255, 255)) in row  # the same white that the selection of a drawn marker has
    assert _code(screen._label_rgb(target)) not in row


def test_walk_link_cursor_clamps_at_both_ends() -> None:
    """The list window of the links does not wrap.

    The ↑ key on the first row and the ↓ key past the last row stay where they are. They do
    not move the window from one end to the other.
    """
    screen = WalkScreen(session=_FakeSession(), topo=_hub_topo(14), contacts={}, self_label="us")
    screen.note_viewport(20)
    screen.render_body(80)
    last = len(screen._rows()) - 1
    screen.handle("up")
    assert screen._index == 0
    for _ in range(last + 5):
        screen.handle("down")
    assert screen._index == last
