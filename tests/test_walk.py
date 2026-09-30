# SPDX-License-Identifier: Apache-2.0
"""Mesh walk tests: the focus-and-walk browser over a hand-built topology.

The screen is driven headless against a fake session, the same approach as the
dashboard tests: render_body is pure lines-out, handle() is pure state, so walking,
backtracking, find, and the canvas/list split are all assertable without a terminal.
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
from tests.conftest import plain as _plain  # THE strip-and-join screen reader

US = "aa" * 6
Hub = Contact(name="Hilltop-Repeater", public_key="3d" * 32, node_type=2)
ALICE = Contact(name="Alice", public_key="b2" * 32, last_seen=utcnow() - timedelta(minutes=5))


class _FakeSession:
    def __init__(self) -> None:
        self.repaints = 0

    def invalidate(self) -> None:
        self.repaints += 1


def _topo(*, with_island: bool = False) -> MeshTopology:
    """Us — Hub — Alice as a two-ring chain, optionally plus a detached island pair."""
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
    screen.note_viewport(cell_h)  # the frame records this before every real paint
    return screen


# --- rendering ----------------------------------------------------------------------------


def test_walk_opens_focused_on_us_with_the_link_list() -> None:
    """The default focus is our own node: canvas, legend, and its links beneath."""
    screen = _screen(_topo())
    body = _plain(screen.render_body(80))
    assert "Homestead" in body and "this device" in body
    assert "Links" in body and "strongest observed first" in body
    assert "Hilltop-Repeater" in body  # our one neighbour, as a selectable row
    assert "edge = SNR" in body  # the canvas legend
    # The screen's name, and nothing else: the focus, and how big the neighbourhood is,
    # are what the body under it is for.
    assert screen.title == "Mesh walk"


def test_walk_link_rows_carry_snr_evidence_and_onward_count() -> None:
    """A neighbour row reads SNR, samples, source tags, age, and its onward links."""
    screen = _screen(_topo())
    body = _plain(screen.render_body(80))
    row = next(line for line in body.split("\n") if "Hilltop-Repeater" in line and "❯" in line)
    assert "+6.0" in row  # the link's median SNR
    assert "1×" in row  # samples
    assert "T" in row  # trace evidence tag
    assert "⋯ 1" in row  # one link onward (Hub — Alice)
    assert "3d" * 6 in row  # the whole 12-hex id, not a truncated prefix…
    assert "…" not in row  # …and nothing about it elided


def test_walk_link_row_shrinks_the_key_before_the_name() -> None:
    """A long name keeps its letters; the key gives ground first, on a byte boundary.

    When the row can't hold both a long name and the full 12-hex id, the key truncates to
    its lit hash plus a ``…`` (an even, whole-byte count) rather than the name losing
    characters — and the hash prefix is never the part dropped.
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
        prefix_bytes=1,  # the first byte (``d4``) is the addressed hash
    )
    screen.note_viewport(24)
    row = next(
        line
        for line in _plain(screen.render_body(68)).split("\n")
        if "Repeater-Downtown-01" in line and "❯" in line  # the list row, not a canvas label
    )
    assert "Repeater-Downtown-01" in row  # the name shows whole, not clipped
    assert "…" in row  # the key is the lane that gave ground
    assert "d4c3" in row  # its lit hash (and a byte or two more) survives
    assert "d4c3b2a1f0e9" not in row  # …but not the whole id — it was truncated


def test_walk_graph_labels_names_in_full_when_the_canvas_has_room() -> None:
    """A neighbour's name is drawn whole on the canvas, not clipped to a flat short cap.

    The fan pulls west of the edge and each label clamps to the room actually there, so a
    long contact name keeps its letters wherever the canvas can hold it.
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
    # The canvas rows are everything above the legend line.
    lines = _plain(screen.render_body(80)).split("\n")
    canvas = "\n".join(lines[: next(i for i, ln in enumerate(lines) if "edge = SNR" in ln)])
    assert "Lakesidetechnique" in canvas  # 17 chars, drawn whole — no "Lakesidetechniq…"
    assert "Repeater-Downtown-01" in canvas  # 20 chars, whole


def test_walk_selected_link_lights_the_route_that_reaches_it() -> None:
    """Selecting a link draws its edge *and* the approach into the focus at full strength.

    With every link stale (so an un-highlighted edge fades to half), the selected
    neighbour's edge and the came_from → focus approach are drawn undimmed — the lit
    route — while an unselected neighbour's edge stays faded.
    """
    from meshterm.ui.walk_screen import _snr_rgb

    stale = utcnow() - timedelta(days=10)  # older than a week → _freshness 0.5
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
    screen.handle("enter")  # walk to Hub — now came_from is us, the approach edge
    # Land the selection on Alice (a fan neighbour, not the ⌫-back row).
    while screen._rows()[screen._index] != topo.canonical(alice.public_key):
        screen.handle("down")

    ansi_lines = screen.render_body(80)
    legend = next(i for i, ln in enumerate(ansi_lines) if "edge = SNR" in _plain([ln]))
    canvas = "".join(ansi_lines[:legend])

    def code(rgb: tuple[int, int, int]) -> str:
        return f"38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"

    def scaled(rgb: tuple[int, int, int], f: float) -> tuple[int, int, int]:
        return tuple(max(0, min(255, round(c * f))) for c in rgb)

    assert code(_snr_rgb(0.0)) in canvas  # Alice's edge, full-strength amber
    assert code(scaled(_snr_rgb(-15.0), 0.5)) in canvas  # Bob's edge stays faded…
    assert code(_snr_rgb(-15.0)) not in canvas  # …and never reaches full strength


def test_walk_empty_graph_renders_guidance() -> None:
    """With no evidence at all the screen explains how the walk fills up."""
    topo = MeshTopology(US, contacts=[])
    screen = _screen(topo)
    body = _plain(screen.render_body(80))
    assert "no evidence to draw yet" in body
    assert "trace" in body


# --- walking ------------------------------------------------------------------------------


def test_walk_enter_walks_and_grows_the_trail() -> None:
    """Enter focuses the highlighted neighbour; the breadcrumb trail reads the walk."""
    topo = _topo()
    screen = _screen(topo)
    screen.render_body(80)
    screen.handle("enter")  # walk to Hub (our only neighbour)
    assert screen._focus == topo.canonical(Hub.public_key)
    body = _plain(screen.render_body(80))
    assert "★ › Hilltop-Repeater" in body  # the trail, our end on the app-wide star
    # The focus line reads name (hash) — the glyph carries the type, not a spelled-out kind.
    assert "Hilltop-Repeater (3d)" in body and "1 hop out" in body
    assert "Alice" in body  # Hub's onward neighbour is now a row
    # The node we walked in from is not offered back as a link — ⌫ is the way back — so
    # the list holds only ways *onward* and opens on the strongest of them.
    assert screen._rows() == [topo.canonical(ALICE.public_key)]
    assert "⌫ back" not in body


def test_walk_backspace_steps_back_along_the_trail() -> None:
    """⌫ pops the trail one step; at the trail's start it does nothing."""
    topo = _topo()
    screen = _screen(topo)
    screen.render_body(80)
    screen.handle("enter")
    assert len(screen._trail) == 2
    screen.handle("backspace")
    assert screen._trail == [topo.self_id]
    screen.handle("backspace")  # already home — inert
    assert screen._trail == [topo.self_id]


def test_walk_the_node_walked_in_from_is_not_offered_back_as_a_link() -> None:
    """The way back is ⌫, not a row: a walk that just undoes the last one isn't on offer.

    It leaves the list holding only ways onward, which is also what makes row 0 — where
    every reset of the cursor lands — reliably the strongest link out of here.
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

    screen.handle("backspace")  # the way back, taken by key
    assert screen._trail == [topo.self_id]


def test_walk_walking_to_an_earlier_node_drops_the_loop() -> None:
    """Walking back to an earlier node drops the loop.

    Revisiting a node already on the trail truncates the stack to its first
    appearance, dropping the circular stretch walked to get back there.
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
    # Alice → us closes the loop. It is a real link onward (not the node we arrived from,
    # which is Hub), so it is on offer — and taking it drops the stretch walked to get here.
    walk_to(topo.self_id)
    assert screen._trail == [topo.self_id]


def test_walk_trail_crops_its_head_not_its_tail_when_narrow() -> None:
    """A trail too long for the line is cropped at its *start*, the trophy case's crop."""
    screen = _screen(_topo())
    us = screen._topo.self_id
    hub = screen._topo.canonical(Hub.public_key)
    alice = screen._topo.canonical(ALICE.public_key)
    screen._trail = [us, hub, alice]
    width = 20  # too narrow for the whole "Homestead › Hilltop-Repeater › Alice"
    text = screen._trail_text(width).plain
    assert text[0] in ("…", CRACK_HEAD)  # cut, not elided: no whole hop bought the mark
    assert "⋯" not in text
    assert text.endswith("Alice")  # the focus is always kept
    assert "Homestead" not in text  # the start was cropped, not the tail
    assert len(text) == width  # a crop lands on the width, so it is flush right already


def test_walk_trail_crop_keeps_the_cells_an_elision_would_have_spent() -> None:
    """The crop's whole point: cells the lane has go to the walk, not to a dropped hop."""
    screen, width = _walked_chain(8)
    cropped = screen._trail_text(width)
    elided = PathLine(
        [screen._trail_hop(node) for node in screen._trail], separator=" › "
    ).ellipsized(width, elide=ELIDE_HEAD)
    assert cropped.cell_len == width  # the crop fills the lane…
    assert elided.cell_len < width  # …where dropping a whole hop left cells on the floor
    # The crop keeps the tail of the hop it landed in; the elision dropped it whole.
    assert cropped.plain.startswith("…-05 ") and "-05" not in elided.plain


def test_walk_trail_sits_left_until_it_overflows() -> None:
    """A trail that fits is left-aligned; only an elided one snaps to the right edge."""
    screen = _screen(_topo())
    screen._trail = [screen._topo.self_id]
    assert screen._trail_text(40).plain == SELF_GLYPH  # us, in one cell


def test_walk_trail_scrolled_back_to_its_head_sits_left_again() -> None:
    """Alignment follows the head, not the scroll: a visible start hangs the line left."""
    screen, width = _walked_chain(8)
    steps = screen._trail_max_scroll(width) // _HSCROLL_STEP
    for step in range(1, steps):  # every stop short of the head is still cropped
        screen.handle("left")
        cut = _plain([render_to_ansi(screen._trail_text(width), width, no_wrap=True)])
        assert cut[0] in ("…", CRACK_HEAD) and len(cut) == width, step

    screen.handle("left")  # the step that brings the walk's start back into view
    assert screen._trail_scroll == steps * _HSCROLL_STEP
    home = _plain([render_to_ansi(screen._trail_text(width), width, no_wrap=True)])
    assert home.startswith(SELF_GLYPH)  # flush left, no padding before the head


def test_walk_trail_names_carry_their_node_hues() -> None:
    """Trail names take the per-node key hue (ours the white you-style), the focus bold."""
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
    """^U resets the walk to our own node from anywhere; Home/End are the list's ends."""
    # A hub of spokes off one of our neighbours: a walked-away focus with a list to move in.
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

    # Home/End move the highlight — they no longer abandon the walk.
    screen.handle("end")
    assert screen._index == len(rows) - 1 and screen._trail == [topo.self_id, hub]
    screen.handle("home")
    assert screen._index == 0 and screen._trail == [topo.self_id, hub]

    screen.handle("locate")
    assert screen._trail == [topo.self_id]


def test_walk_echoes_the_find_query_above_the_matches_it_narrows() -> None:
    """The PicoCalc draws no hint line, so the query it carries moves into the body."""
    from meshterm.platforms import PICOCALC, REGULAR, set_platform

    try:
        set_platform(REGULAR)
        desktop = _screen(_topo())
        desktop.render_body(80)
        for ch in "al":
            desktop.handle("text", ch)
        body = _plain(desktop.render_body(80))
        assert "/al" not in body and "find: al" in desktop.footer_hint

        set_platform(PICOCALC)
        device = _screen(_topo())
        device.render_body(53)
        for ch in "al":
            device.handle("text", ch)
        lines = [_plain(line) for line in device.render_body(53)]
        assert len(lines) <= 24  # still inside the viewport it was handed
        # The echo sits directly above the heading of the list it narrows — the list, not
        # the canvas, is what cedes the row for it.
        echo = next(i for i, line in enumerate(lines) if line.strip() == "/al")
        assert lines[echo + 1].startswith("Matches")
        # Clearing the find takes the row back with it.
        for _ in "al":
            device.handle("backspace")
        assert not any(
            line.strip().startswith("/") for line in (_plain(ln) for ln in device.render_body(53))
        )
    finally:
        set_platform(REGULAR)


def test_walk_fkey_lane_gives_you_its_own_slot_over_the_pagers_ends() -> None:
    """``You`` is a verb of this screen (F3), not an end of the list the pager scrolls."""
    from meshterm.ui.tui.fkeys import PICOCALC_DECK

    screen = _screen(_topo())
    screen.render_body(80)
    lane = screen.picocalc_lane

    assert [pair.label if pair else None for pair in lane] == [
        None,
        None,
        "You",
        "Page ↓",
        "Page ↑",
    ]
    assert PICOCALC_DECK.action_for(lane, 3) == "locate"
    assert lane[2].opp_label == ""  # nothing is the opposite of going home
    # And the pager's Shift bank means what it means everywhere else: the list's two ends.
    assert lane[4].opp_label == "Top" and PICOCALC_DECK.action_for(lane, 10) == "home"
    assert lane[3].opp_label == "Bottom" and PICOCALC_DECK.action_for(lane, 9) == "end"


def test_walk_you_dims_once_the_focus_is_already_us() -> None:
    """Dim says *a thing here, just not right now* — there is nowhere to come back from."""
    screen = _screen(_topo())
    screen.render_body(80)
    assert screen.picocalc_lane[2].enabled is False  # opens on us, trail of one

    screen.handle("enter")  # walked away: now there is
    assert screen.picocalc_lane[2].enabled is True

    screen.handle("locate")
    assert screen.picocalc_lane[2].enabled is False
    screen.handle("text", "a")  # a find narrows the list off our own neighbours too
    assert screen.picocalc_lane[2].enabled is True


def test_walk_the_fan_is_all_east_and_holds_no_came_from() -> None:
    """Everything drawn is a way onward, east of the focus — nothing ducks in behind it."""
    topo = _topo()
    screen = _screen(topo)
    screen.render_body(80)
    screen.handle("enter")  # focus Hub; we came from us
    alice = topo.canonical(ALICE.public_key)
    fx, _fy = screen._focus_pos(80, 12)
    placed = screen._place_neighbours(80, 12, screen._fan_nodes(), False)
    assert topo.self_id not in placed  # the node we came from is behind us, not on screen
    assert list(placed) == [alice]
    assert placed[alice][0] > fx  # the fan is east of the focus
    assert fx <= (80 * 2) // 3  # and the focus itself leans left


def test_walk_fan_rim_leaves_spread_east_not_curling_back() -> None:
    """The fan is a flattened arc: the top/bottom leaves reach well east too.

    On the old circular arc only the due-east leaf reached the far side; the rim leaves
    curled back toward the focus (to ~0.31 of the reach), leaving the corners empty. The
    wedge flattens the horizontal reach, so a rim leaf lands east of the midpoint between
    the fan's anchor and its due-east tip — the fan spreads across the width.
    """
    fan = [f"{i + 0x20:02x}" * 6 for i in range(5)]
    topo = MeshTopology(US, contacts=[])
    when = utcnow()
    for i, node in enumerate(fan):
        topo.add_walk([topo.self_id, node], snrs=[5.0 - i], when=when, source="trace")
    screen = _screen(topo)
    ax, _ay, _name = screen._focus_anchor(80, 14)
    placed = screen._place_neighbours(80, 14, fan, False)
    tip = max(x for x, _y in placed.values())  # the due-east leaf, the fan's far tip
    midpoint = ax + (tip - ax) / 2
    assert placed[fan[0]][0] > midpoint  # the topmost leaf reaches past halfway east…
    assert placed[fan[-1]][0] > midpoint  # …and so does the bottommost


def _hub_topo(spokes: int) -> MeshTopology:
    """Us at the centre of a ``spokes``-neighbour hub, strengths descending."""
    topo = MeshTopology(US, contacts=[])
    when = utcnow()
    for i in range(spokes):
        node = f"{i:02x}" * 6
        for _ in range(spokes - i):  # more samples = stronger, so the order is fixed
            topo.add_walk([topo.self_id, node], snrs=[5.0], when=when, source="trace")
    return topo


def test_walk_collapses_the_weak_links_into_one_ellipsis_marker() -> None:
    """Beyond the area's capacity, weaker neighbours fold into a single ``…`` node."""
    screen = WalkScreen(session=_FakeSession(), topo=_hub_topo(14), contacts={}, self_label="us")
    screen.note_viewport(20)
    body = _plain(screen.render_body(80))
    assert "weaker" in body  # the collapsed marker is labelled "+n weaker"
    canvas_part = body.split("Links")[0]
    assert "…" in canvas_part
    # The list still names every neighbour — selection is the list's job.
    assert len(screen._rows()) == 14


def test_walk_fan_stays_sparse_and_collapses_the_rest() -> None:
    """The fan is kept deliberately sparse: even a modest hub sheds its weakest links."""
    screen = WalkScreen(session=_FakeSession(), topo=_hub_topo(8), contacts={}, self_label="us")
    screen.note_viewport(20)
    canvas_part = _plain(screen.render_body(80)).split("Links")[0]
    assert "…" in canvas_part and "weaker" in canvas_part  # not all eight are drawn


def test_walk_labels_non_selected_nodes_to_the_right_of_their_icon() -> None:
    """A fan node is named just to the right of its marker — the walk's reading way."""
    from meshterm.ui.mapcanvas import MapCanvas

    screen = _screen(_topo())
    canvas = MapCanvas(80, 12)
    canvas.marker(40, 20, "●", (255, 255, 255))  # a marker with room to its east
    screen._label_right(canvas, 40, 20, "Bravo", (200, 200, 200))
    marker_cx = 40 >> 1
    assert canvas._label_cells  # the name landed
    assert all(cx > marker_cx for cx, _cy in canvas._label_cells)  # every cell east


def test_walk_selecting_a_collapsed_row_lights_the_ellipsis_with_its_name() -> None:
    """Highlighting a weak (collapsed) row surfaces its name at the ``…`` marker."""
    screen = WalkScreen(session=_FakeSession(), topo=_hub_topo(14), contacts={}, self_label="us")
    screen.note_viewport(20)
    screen.render_body(80)
    screen._index = len(screen._rows()) - 1  # the weakest row, surely collapsed
    body = _plain(screen.render_body(80))
    weakest = screen._rows()[-1][:8]
    canvas_part = body.split("Links")[0]
    assert weakest in canvas_part  # the ellipsis marker took the selection's label
    assert "weaker" not in canvas_part  # ...replacing the "+n weaker" count


def test_walk_body_fits_the_viewport_and_windows_the_list() -> None:
    """The screen never outgrows the frame; only the link list scrolls, marked."""
    screen = WalkScreen(session=_FakeSession(), topo=_hub_topo(16), contacts={}, self_label="us")
    screen.note_viewport(22)
    lines = screen.render_body(80)
    assert len(lines) <= 22  # canvas + chrome + list window == the viewport
    body = _plain(lines)
    assert "↓" in body and "more" in body  # the window marks the rows below
    assert "Links" in body


def test_walk_pgdn_pages_the_highlight_by_the_list_window() -> None:
    """PgUp/PgDn stride by the list window, and the window follows the highlight."""
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
    assert "↑" in body and "more" in body  # rows scrolled off above are counted


# --- find ---------------------------------------------------------------------------------


def test_walk_find_lists_matches_and_teleports() -> None:
    """Typing filters every known node; Enter focuses the match and restarts the trail."""
    topo = _topo()
    screen = _screen(topo)
    screen.render_body(80)
    for ch in "ali":
        screen.handle("text", ch)
    assert "find: ali" in screen.footer_hint
    body = _plain(screen.render_body(80))
    assert "Matches" in body and "Alice" in body
    assert "2 hops out" in body  # the match row says how far away it sits
    assert screen.title == "Mesh walk"  # the title holds still; the list is the answer

    screen.handle("enter")
    alice = topo.canonical(ALICE.public_key)
    assert screen._focus == alice
    assert screen._trail == [alice]  # a teleport restarts the trail
    assert screen._filter == ""


def test_walk_find_marks_islands() -> None:
    """A match with no path to us reads island, and focusing it says why."""
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
    """Esc clears an active find first; the next Esc leaves the screen."""
    import asyncio

    screen = _screen(_topo())
    screen.render_body(80)
    screen.handle("text", "a")

    async def drive() -> object:
        screen.future = asyncio.get_running_loop().create_future()
        screen.handle("escape")  # peels the filter
        assert screen._filter == "" and not screen.future.done()
        screen.handle("escape")  # dismisses
        return await screen.future

    assert asyncio.run(drive()) is None


def _chain(length: int) -> MeshTopology:
    """Us — hop-01 — hop-02 — … , every name long enough to overflow a narrow trail."""
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
    """A screen walked to the end of a `length`-hop chain, rendered at `width`."""
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
        # Always step *onward*, never back through the node we came from.
        screen._index = next(i for i, node in enumerate(rows) if node not in screen._trail)
        screen.handle("enter")
    screen.render_body(width)
    return screen, width


def test_walk_trail_slides_a_window_over_the_walk_and_cracks_both_edges() -> None:
    """← reads back over a long walk; → returns; each edge it continues past is cracked."""
    screen, width = _walked_chain(8)
    trail = _plain([render_to_ansi(screen._trail_text(width), width, no_wrap=True)])

    # At rest the window sits at the tail: the focus shows, the start is cropped off the
    # left, and the walk's own end needs no mark because the walk really does end there.
    assert "Repeater-08" in trail and "Homestead" not in trail
    assert trail[0] in ("…", CRACK_HEAD) and len(trail) == width
    assert trail[-1] not in ("…", CRACK_TAIL) and "⋯" not in trail
    assert screen._trail_scroll == 0

    screen.handle("left")
    slid = _plain([render_to_ansi(screen._trail_text(width), width, no_wrap=True)])
    assert screen._trail_scroll == _HSCROLL_STEP  # the app's own step, cells not hops
    # Walk moved out of view on the right, and says so in the same language as the left:
    # the cut mark, never the ⋯ — nothing here is elided, the lane just ran out.
    assert "Repeater-08" not in slid and "Repeater-07" in slid
    assert slid[-1] in ("…", CRACK_TAIL) and "⋯" not in slid
    assert len(slid) == width

    screen.handle("right")
    assert screen._trail_scroll == 0
    assert _plain([render_to_ansi(screen._trail_text(width), width, no_wrap=True)]) == trail


def test_walk_trail_scroll_stops_at_the_head_and_resets_with_the_trail() -> None:
    """← parks where the walk's start comes into view; any change to the trail rewinds it."""
    screen, width = _walked_chain(8)
    limit = screen._trail_max_scroll(width)
    assert limit > 0

    for _ in range(limit + 5):  # holding ← banks nothing to undo
        screen.handle("left")
    assert screen._trail_scroll == limit
    head = _plain([render_to_ansi(screen._trail_text(width), width, no_wrap=True)])
    assert head.lstrip().startswith(SELF_GLYPH)  # scrolled far enough to see where it set out

    screen.handle("backspace")  # stepping back is a change to the trail
    assert screen._trail_scroll == 0
    screen.render_body(width)
    screen.handle("left")
    screen.handle("locate")  # and so is going home
    assert screen._trail_scroll == 0


def test_walk_trail_scroll_is_inert_on_a_walk_that_fits() -> None:
    """A trail with nothing hidden has nothing to scroll — and the hint doesn't offer it."""
    screen = _screen(_topo())
    screen.render_body(80)
    assert screen._trail_max_scroll(80) == 0
    assert "←→ trail" not in screen.footer_hint
    assert "type to find" in screen.footer_hint

    screen.handle("left")
    assert screen._trail_scroll == 0

    walked, width = _walked_chain(8)
    assert "←→ trail" in walked.footer_hint  # advertised exactly where it does something
    assert len(walked.footer_hint) <= 72


def test_walk_find_narrows_the_canvas_fan_but_not_the_walk() -> None:
    """Typing thins the ways onward; the focus and the node walked from hold their place."""
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
    assert "Alice" in canvas and "Bob-Tower" in canvas  # the whole fan, unfiltered

    for ch in "ali":
        screen.handle("text", ch)
    canvas = _plain(screen._canvas_lines(80, 12, None))
    assert "Alice" in canvas  # the way onward the query is about
    assert "Bob-Tower" not in canvas  # and the one it isn't
    # The focus is not a candidate: it is the walk so far, and holds through any query.
    assert "Hilltop-Repeater" in canvas


def test_walk_marks_wear_their_node_type_colour_not_the_key_hue() -> None:
    """A mark is glyph *and* colour: the same one the map and the route graph pin with."""
    from meshterm.ui.marks import NODE_MARK, REPEATER_MARK, SELF_MARK
    from meshterm.ui.theme import mark_rgb, node_style

    topo = _topo()
    screen = _screen(topo)
    hub, alice = topo.canonical(Hub.public_key), topo.canonical(ALICE.public_key)

    assert screen._glyph(hub) == (REPEATER_MARK[0], "type.repeater")
    assert screen._glyph(alice) == (NODE_MARK[0], "type.node")
    assert screen._marker_rgb(hub) == mark_rgb("type.repeater")
    assert screen._marker_rgb(topo.self_id) == mark_rgb(SELF_MARK[1])
    # Identity has its own lane and keeps it: the *name* is what carries the key hue.
    assert screen._marker_rgb(hub) != screen._label_rgb(hub)
    assert screen._label_rgb(hub) == _plain_rgb(node_style(hub))
    # The legend is a sample of those very marks, so it keys colour as well as shape.
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
    """One marker per row, a blank row between each, and a blank above and below."""
    screen = _screen(_hub_topo(4))
    rows = screen._fan_rows(11, 5)
    assert rows == [1, 3, 5, 7, 9]  # 2n+1 = 11 rows exactly: 1 top, 1 bottom, 1 between
    # The focus is level with the block's middle, not the canvas's, so the edges leaving
    # it fan out symmetrically.
    _fx, fy = screen._focus_pos(80, 11, 5)
    assert fy >> 2 == rows[len(rows) // 2]
    # A fan shorter than its rows keeps the air; the block just centres in what it has.
    assert screen._fan_rows(11, 3) == [3, 5, 7]


def test_walk_fan_gives_up_its_outer_rows_before_it_gives_up_a_node() -> None:
    """Air is worth a row until it costs a node — below seven, the padding goes."""
    screen = _screen(_hub_topo(4))
    # Tall enough for seven with a blank row top and bottom (2·7+1 = 15): keep them.
    assert screen._fan_capacity(15) == 7
    assert screen._fan_rows(15, 7) == [1, 3, 5, 7, 9, 11, 13]
    # One row short of that: the outer rows go to the seventh marker instead.
    assert screen._fan_capacity(14) == 7
    assert screen._fan_rows(14, 7)[0] == 0
    # Roomier still: the padding simply grows, it is not spent on more nodes than fit.
    assert screen._fan_rows(19, 7)[0] == 3


def test_walk_collapsed_marker_stands_in_for_the_selected_weak_link() -> None:
    """Selecting a collapsed node makes the ``…`` marker *be* it: mark, name, and rank."""
    screen = _screen(_hub_topo(12))
    screen.render_body(80)
    rows = screen._rows()
    capacity = screen._fan_capacity(14)
    hidden = rows[capacity - 1 :]
    assert len(hidden) > 1

    # Unselected, the marker is the anonymous ellipsis and counts what it swallowed.
    plain_canvas = _plain(screen._canvas_lines(80, 14, rows[0]))
    assert f"… +{len(hidden)} weaker" in plain_canvas
    assert "…" in plain_canvas

    # Selected, it wears that node's own type mark and name, with its rank among the
    # collapsed in grey — so the count the label used to carry is never simply lost.
    target = hidden[1]
    canvas = screen._canvas_lines(80, 14, target)
    body = _plain(canvas)
    # The rank sits *west* of the mark, so the name still ends where every other name on
    # the fan ends and the count reads as an annotation on the marker.
    glyph, _colour = screen._glyph(target)
    assert f"(2/{len(hidden)}) {glyph} {screen._label(target)}" in body
    assert "weaker" not in body
    # The name keeps its own hue and the counter is grey — two runs, one row.
    from meshterm.ui.theme import mark_rgb

    row = next(line for line in canvas if screen._label(target) in _plain([line]))
    assert _code(screen._label_rgb(target)) in row
    assert _code(mark_rgb("node.unknown")) in row


def _code(rgb: tuple[int, int, int]) -> str:
    return f"38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def _long_name_screen(names: list[str]) -> WalkScreen:
    """Us at the centre of a fan of `names`, strongest first."""
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
    """A long name costs *its own* row some easting, and leaves the rest of the fan put."""
    long_name = "Sensor-Rooftop-Longueuil-East"  # 29 cells: past any fixed margin
    screen = _long_name_screen([long_name, "Bob", "Carol"])
    body = _plain(screen._canvas_lines(80, 11, None))
    assert long_name in body  # spelled whole, no trailing …

    nodes = screen._fan_nodes()
    placed = screen._place_neighbours(80, 11, nodes, False)
    # The same fan with a short name in that slot: only the long-named marker moved.
    short = _long_name_screen(["Ann", "Bob", "Carol"])
    baseline = short._place_neighbours(80, 11, short._fan_nodes(), False)
    assert placed[nodes[0]][0] < baseline[short._fan_nodes()[0]][0]
    assert [placed[n][0] for n in nodes[1:]] == [baseline[n][0] for n in short._fan_nodes()[1:]]


def test_walk_fan_keeps_its_reach_when_a_name_would_swallow_the_canvas() -> None:
    """Past a point the labels clip instead: a fan pulled onto the focus draws nothing."""
    screen = _long_name_screen(["X" * 60, "Bob", "Carol"])
    ax, _ay, _n = screen._focus_anchor(53, 11, 3)
    placed = screen._place_neighbours(53, 11, screen._fan_nodes(), False)
    assert min(x for x, _y in placed.values()) > ax  # still a fan, not a pile on the anchor
    assert "…" in _plain(screen._canvas_lines(53, 11, None))  # the label gave way instead


def test_walk_collapsed_slot_is_laid_out_for_its_resting_label_not_the_selection() -> None:
    """Walking the collapsed nodes must not shuffle the fan, so the … slot ignores them.

    Its geometry comes from the ``+n weaker`` it wears at rest; a stand-in's name is fitted
    into whatever room that leaves and truncates if it must.
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
        assert _plain([row]).index(glyph) == marks[0]  # the marker never budged


def test_walk_the_highlighted_row_is_the_node_the_canvas_lights() -> None:
    """The list and the picture read the same index off the same list — at every position.

    The list used to render straight out of the link table while the cursor indexed the
    *rows*. The table still carried the node the walk came from, so from that node's
    position on every row named one link and drew another, and the highlight sat one link
    away from the one the canvas was lighting.
    """
    topo = _hub_topo(6)
    screen = _screen(topo)
    screen.render_body(80)
    screen._index = 1
    screen.handle("enter")  # walk out, so there *is* a came-from to leave out
    screen.render_body(80)

    rows = screen._rows()
    assert topo.self_id not in rows  # …and it is not among the rows
    for index in range(len(rows)):
        screen._index = index
        body = _plain(screen.render_body(80))
        picked = next(line for line in body.split("\n") if line.startswith("❯"))
        assert screen._label(rows[index]) in picked
        # And the canvas lights that same node, not its neighbour in the table.
        canvas = _plain(screen._canvas_lines(80, 12, rows[index]))
        assert screen._label(rows[index]) in canvas


def test_walk_collapsed_stand_in_wears_the_selection_white() -> None:
    """A collapsed node is only ever drawn while selected, so white is simply what it is."""
    # Named contacts, so the name's own hue is something white can be told apart from.
    screen = _long_name_screen([f"Node-{i:02d}" for i in range(12)])
    screen.render_body(80)
    rows = screen._rows()
    target = rows[screen._fan_capacity(14) - 1 + 1]
    assert screen._label_rgb(target) != (255, 255, 255)

    canvas = screen._canvas_lines(80, 14, target)
    row = next(line for line in canvas if screen._label(target) in _plain([line]))
    assert _code((255, 255, 255)) in row  # the same white a drawn marker's selection takes
    assert _code(screen._label_rgb(target)) not in row


def test_walk_link_cursor_clamps_at_both_ends() -> None:
    """The windowed link list does not wrap.

    ↑ on the first row and ↓ past the last stay where they are, rather than hauling
    the window end to end.
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
