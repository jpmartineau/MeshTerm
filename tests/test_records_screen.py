# SPDX-License-Identifier: Apache-2.0
"""Trophy-case dialog tests: one record's full story, headless.

These tests work like the other screen tests. ``render_body`` only returns lines, and
``handle`` only changes state. Thus the tests can check the stats lanes, the route, and the
renamed *Trace this path* action without a terminal.
"""

from __future__ import annotations

import asyncio
import io
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from rich.cells import cell_len
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core.admin_store import AdminStore
from meshterm.core.config import Settings
from meshterm.core.device_store import DeviceStore
from meshterm.persistence.repository import DiscoveredPath, Repository
from meshterm.services.records import CATEGORIES, CATEGORY_BY_ID
from meshterm.ui.records_screen import (
    RecordScreen,
    WalkVertex,
    discipline_label,
    discipline_lane,
    open_records,
)
from meshterm.ui.surface import TuiUi
from meshterm.ui.tui import frame
from meshterm.ui.tui.prompt import TypedConfirmDialog
from meshterm.ui.tui.screen import CANCEL
from meshterm.ui.tui.select import SelectScreen
from meshterm.ui.tui.session import TuiSession
from meshterm.ui.widgets import node_marker, self_marker
from tests.conftest import plain as _plain  # the only function that strips and joins a screen

HUB_ID, FAR_ID = "3d63c6429436", "f2c24f54551e"


def _loop_shape() -> list[WalkVertex]:
    """A circuit of three points (us and two hops with positions). It encloses an area."""
    sglyph, scolor = self_marker()
    hglyph, hcolor = node_marker(2)  # a repeater
    nglyph, ncolor = node_marker(1)  # a plain node
    return [
        WalkVertex(0.0, 0.0, sglyph, scolor, True),
        WalkVertex(2.0, 1.0, hglyph, hcolor, False, label=HUB_ID[:2]),
        WalkVertex(1.0, 3.0, nglyph, ncolor, False, label=FAR_ID[:2]),
    ]


def _resolve(hop: str) -> str:
    return {HUB_ID: "Hilltop-Repeater", FAR_ID: "Far"}.get(hop, hop)


def _record(**kw) -> DiscoveredPath:
    defaults = dict(
        id=1,
        category="grand_tour",
        width_bytes=1,
        spec="3d,f2",
        route=(HUB_ID, FAR_ID),
        score=2.0,
        stats={
            "hop_count": 2,
            "distinct_nodes": 2,
            "repeats": False,
            "min_snr": 6.0,
            "km_travelled": 3.2,
            "km_complete": True,
            "far_km": 1.5,
            "rtt_ms": 250.0,
        },
        app_version="0.1.0",
        discovered_at=datetime(2026, 7, 12, 14, 30, tzinfo=timezone.utc),
    )
    defaults.update(kw)
    return DiscoveredPath(**defaults)


def _dialog(record: DiscoveredPath, rank: int = 1, **kw) -> RecordScreen:
    return RecordScreen(
        record,
        CATEGORY_BY_ID[record.category],
        rank,
        resolve=_resolve,
        device_label="Homestead",
        device_hash=None,
        **kw,
    )


def test_record_dialog_shows_stats_route_and_the_trace_action() -> None:
    """The score is in the unit of the category, the route has names and us at both ends."""
    screen = _dialog(_record())
    info = _plain(screen.render_body(60))
    assert "2 nodes" in info  # the score, in the unit of the discipline
    assert "Trace this path" in info  # the renamed action (it was "Walk again")
    assert "Walk again" not in info
    assert "Delete record" in info
    assert "Back" not in info  # no exit row: the Esc key leaves the page
    screen.handle("tab")
    route = _plain(screen.render_body(60))
    assert "Hilltop-Repeater" in route  # a relay with a name on the route
    assert "Homestead" in route  # us, at both ends of the route that was walked
    assert "Trace this path" in route and "Delete record" not in route  # Enter here traces


def test_record_dialog_draws_the_walk_as_a_route_graph() -> None:
    """The screen draws the walk on the route graph: us as a star at both ends, over a caption."""
    screen = _dialog(_record())
    screen.note_viewport(40)  # the frame records this before each real paint
    screen.handle("tab")  # the Route tab
    lines = _plain(screen.render_body(60)).splitlines()
    legend = next(i for i, line in enumerate(lines) if "▲ repeater" in line)
    assert "you → … → you" in lines[legend - 1] and "labels = hash byte" in lines[legend - 1]
    graph = "\n".join(lines[_strip_end(lines) : legend - 1])  # the drawing, up to its caption
    assert graph.count("★") == 2  # our node marks both endpoints of the round trip
    assert any("⠀" <= ch <= "⣿" for ch in graph)  # the edges are drawn in braille


def test_record_graph_is_only_as_tall_as_one_lane_needs() -> None:
    """A walk is one path: the marker row, and a label row only where a label is placed.

    The shared widget has a default minimum height of 5 rows. It keeps room for a fan of
    alternatives, but a walk never fans. Thus the walk was in rows of blank canvas, and it
    moved the route and the actions down the card with no benefit.
    """
    for record in (_record(), _record(route=(HUB_ID, FAR_ID, HUB_ID, FAR_ID, HUB_ID))):
        lines = _dialog(record)._graph_lines(60)  # for any number of relays: still one lane
        assert 0 < len(lines) <= 3
        assert all(_plain([line]).strip() for line in lines)  # no blank row in the lines


def test_record_dialog_route_runs_unlabelled_across_the_whole_card() -> None:
    """The route is the path widget at full width, with no ``route`` lane of twelve cells.

    The caption of the graph above is ``you → … → you``, so the line is the route. The label
    lane only took cells from the hops. The line wraps at hop boundaries, so a long walk
    folds and is not truncated. Our own two ends use the ★ that the graph above already
    uses for us.
    """
    dialog = _dialog(_record(route=tuple([HUB_ID, FAR_ID] * 4)))
    dialog.note_viewport(40)  # the frame records this before each real paint
    dialog.handle("tab")  # the Route tab
    body = _plain(dialog.render_body(60)).splitlines()
    # The route closes the stage of the Route tab. It runs from under the legend of the
    # graph to the blank line before the action row.
    legend_at = next(i for i, line in enumerate(body) if "▲ repeater" in line)
    route = body[legend_at + 1 : body.index("", legend_at)]
    assert not route[0].startswith("route")  # no label lane
    assert route[0].startswith("★")  # our end opens the walk with the star of the app…
    assert route[-1].endswith("★")  # …and closes it with the same star
    assert "Homestead" not in "".join(route)  # never our name, and never our key
    assert "Hilltop-Repeater" in "".join(route)  # the hops have their full names
    assert len(route) > 1  # it folded and was not truncated…
    # …and each fold hangs under the step.
    assert all(line.startswith("  ") for line in route[1:])


def test_record_dialog_shows_the_node_type_legend() -> None:
    """The standard node-type legend is under the graph, so that the user can read its markers."""
    screen = _dialog(_record())
    screen.note_viewport(40)  # the frame records this before each real paint
    screen.handle("tab")  # the Route tab
    body = _plain(screen.render_body(60))
    assert "★ you" in body and "▲ repeater" in body and "◉ sensor" in body


def test_record_dialog_legend_is_one_line_on_a_handheld_and_breaks_between_entries() -> None:
    """At 53 cells the whole legend fits on one line. When narrower, a mark stays with its name.

    The word ``room`` instead of ``room server`` makes the legend 52 cells, which is inside
    the 53 cells of the PicoCalc and the Cardputer. Where the legend must still break, it
    breaks between entries. A break inside an entry leaves a glyph at the end of a line and
    its name at the start of the next line.
    """
    screen = _dialog(_record())
    screen.note_viewport(40)  # the frame records this before each real paint
    screen.handle("tab")  # the Route tab
    entries = ("★ you", "▲ repeater", "● companion", "■ room", "◉ sensor")
    body = _plain(screen.render_body(53)).splitlines()
    assert any(all(entry in line for entry in entries) for line in body)
    assert not any("room server" in line for line in body)

    narrow = _plain(screen.render_body(40))
    for entry in entries:
        assert entry in narrow
    assert all(cell_len(line) <= 40 for line in narrow.splitlines())


def test_record_dialog_legend_steps_aside_on_a_short_frame() -> None:
    """On the 11 rows of the Cardputer, the Route tab keeps its graph and route, not the legend."""
    screen = _dialog(_record())
    screen.handle("tab")  # the Route tab
    screen.note_viewport(11)
    body = _plain(screen.render_body(53))
    assert "you → … → you" in body and "▲ repeater" not in body


def test_record_graph_marks_a_repeater_relay_with_its_triangle() -> None:
    """A relay of the repeater type draws ▲, not the generic ● dot."""
    dialog = _dialog(_record(), type_of=lambda h: 2 if h == HUB_ID else None)
    graph = _plain(dialog._graph_lines(60))
    assert "▲" in graph  # the repeater relay, with its glyph from the map


def test_record_graph_collapses_a_revisited_route_to_distinct_nodes() -> None:
    """A revisited route collapses to its distinct nodes in the record graph.

    A boomerang route turns back. The depth graph cannot stack a node on itself, so it
    places each node once. Thus it draws the same graph as the distinct route.
    """
    looping = _dialog(_record(route=(HUB_ID, FAR_ID, HUB_ID)))
    distinct = _dialog(_record(route=(HUB_ID, FAR_ID)))
    assert looping._graph_lines(60) == distinct._graph_lines(60)
    graph = _plain(looping._graph_lines(60))
    assert "3d" in graph and "f2" in graph  # both distinct relays have a label, none is piled


def test_record_dialog_title_names_the_discipline_and_rank() -> None:
    """The title bar shows only the type of page. The header line names the record."""
    screen = _dialog(_record(), rank=3)
    assert screen.title == "Record"
    assert (
        _plain(screen.render_body(60)).splitlines()[0].endswith("Most nodes — 3rd place: 2 nodes")
    )


def test_record_dialog_marks_incomplete_distance_as_a_lower_bound() -> None:
    """A Longest-distance walk with a hop that has no position shows ``≥``, never an exact value."""
    record = _record(
        category="long_haul",
        score=8.4,
        stats={"km_travelled": 8.4, "km_complete": False, "hop_count": 3, "distinct_nodes": 3},
    )
    body = _plain(_dialog(record).render_body(60))
    assert "≥ 8.4 km" in body


def test_record_dialog_enter_commits_the_trace_action() -> None:
    """The highlight starts on Trace this path, so the Enter key returns ``"trace"``."""
    dialog = _dialog(_record())
    captured: list = []
    dialog.resolve = captured.append  # type: ignore[method-assign]
    assert dialog._actions[dialog._index] == "trace"
    dialog.handle("enter")
    assert captured == ["trace"]


def test_record_dialog_down_moves_to_delete() -> None:
    """The ↑ and ↓ keys move through the action rows in order."""
    dialog = _dialog(_record())
    dialog.handle("down")
    assert dialog._actions[dialog._index] == "delete"


def test_record_dialog_actions_start_both_labels_in_the_same_cell() -> None:
    """*Trace this path* and *Delete record…* share one icon column, on both platforms.

    ``👣`` is two cells and ``🗑`` is one. When each mark was measured alone, the words of
    the delete row started one column to the left of the words of the trace row. The test
    measures in cells, not characters, because the eye compares the cell where the words
    start. If the platform draws no icon lane, the marks go and their padding must go too.
    Then both labels start right after the pointer.
    """
    from meshterm.platforms import PICOCALC_LYRA, set_platform

    def starts() -> set[int]:
        lines = _plain(_dialog(_record()).render_body(60)).splitlines()
        rows = [
            line[: line.index(word)]
            for word in ("Trace this path", "Delete record…")
            for line in lines
            if word in line
        ]
        assert len(rows) == 2, lines
        return {cell_len(head) for head in rows}

    regular = starts()
    assert len(regular) == 1  # one column for both words…
    assert regular == {2 + 2 + 1}  # …after the pointer, the lane of two cells, and its space

    set_platform(PICOCALC_LYRA)
    assert starts() == {2}  # no lane, no padding that remains: the words follow the pointer


def test_record_dialog_names_the_far_point() -> None:
    """The name of the node that the walk reached is next to the far-point distance."""
    body = _plain(_dialog(_record(), far_label="Far").render_body(60))
    assert re.search(r"far point\s+1\.5 km\s+Far", body)


def test_record_dialog_names_the_link_the_longest_leg_spanned() -> None:
    """The leg lane has its distance and the two nodes between which the leg crossed."""
    record = _record(
        category="long_leg",
        score=12.4,
        stats={
            "hop_count": 2,
            "distinct_nodes": 2,
            "km_travelled": 21.0,
            "km_complete": True,
            "leg_km": 12.4,
            "leg_link": [HUB_ID, FAR_ID],
        },
    )
    body = _plain(_dialog(record).render_body(60))
    assert re.search(r"longest leg\s+12\.4 km\s+Hilltop-Repeater → Far", body)


def test_record_dialog_stars_our_own_end_of_the_longest_leg() -> None:
    """A leg that leaves home has the ★ of the app at our end, never our name."""
    record = _record(
        category="long_leg",
        score=12.4,
        stats={"hop_count": 1, "distinct_nodes": 1, "leg_km": 12.4, "leg_link": [None, HUB_ID]},
    )
    lane = next(
        line
        for line in _plain(_dialog(record).render_body(60)).splitlines()
        if "longest leg" in line
    )
    assert "★ → Hilltop-Repeater" in lane and "Homestead" not in lane


def test_record_dialog_skips_the_leg_lane_for_a_record_stored_without_one() -> None:
    """A record that was set before the discipline existed has no leg to show."""
    body = _plain(_dialog(_record()).render_body(60))
    assert "longest leg" not in body


def test_record_dialog_shows_reliability_when_the_far_node_has_history() -> None:
    """The reliability lane shows the percentage and the sample count.

    Without history, the lane is absent.
    """
    with_rate = _plain(_dialog(_record(), reliability=(0.875, 7, 8)).render_body(60))
    assert re.search(r"reliability\s+88%", with_rate)
    assert "7/8" in with_rate
    without = _plain(_dialog(_record()).render_body(60))
    assert "reliability" not in without


def _is_braille(ch: str) -> bool:
    return chr(0x2800) <= ch <= chr(0x28FF)


def _strip_end(lines: list[str]) -> int:
    """The index after the rows of the tab strip.

    A strip of two tabs closes on its bottom rule. This is the one row that turns up into
    the wall of the active tab (``╯``). A lone tab collapses to a ``── Stats ──`` heading of
    one row.
    """
    for i, line in enumerate(lines):
        if "╯" in line or "── " in line:
            return i + 1
    raise AssertionError(f"no tab strip in {lines[:5]}")


def test_record_opens_on_info_with_route_and_area_tabs_beside_it() -> None:
    """A walk that the screen can draw has three tabs (Info, Route, and Area) and opens on Info.

    The organization is the same as on the node page. The strip is the title of the page, so
    the stats start directly under it (identity first), and the actions of the page are at
    the foot. The screen does not draw the route or the ground on this tab, because each has
    a tab of its own.
    """
    screen = _dialog(_record(), far_label="Far", shape=_loop_shape())
    lines = _plain(screen.render_body(58)).splitlines()
    strip = "\n".join(lines[: _strip_end(lines)])
    assert "Info" in strip and "Route" in strip and "Area" in strip, "all three boxed"
    body = [line for line in lines[_strip_end(lines) :] if line]
    assert body[0].startswith("spec") and body[1].startswith("recorded")  # identity first
    assert body[2].startswith("walk")  # the score is in the header, not in a lane
    assert "Trace this path" in "\n".join(body) and "Delete record" in "\n".join(body)
    assert not any(_is_braille(ch) for line in body for ch in line), "no drawing here"
    assert "Hilltop-Repeater" not in "\n".join(body), "the route has a tab of its own"
    assert "Far" in "\n".join(body)  # the far-point name is a stat, not a label of the drawing


def test_record_header_names_the_discipline_the_standing_and_the_metric() -> None:
    """One line above the tabs: ``<mark> <discipline> — <nth> place: <score>``.

    This is the identity header of the node page, for a record. The line writes the
    discipline in the same way as its board heading. The mark has padding to the measured
    lane, so a ``🛣`` of one cell and a ``🎯`` of two cells start their titles in the same
    cell. The score lane that repeated the metric on Info does not exist now.
    """
    from meshterm.ui.records_screen import discipline_label, discipline_lane

    screen = _dialog(_record(), shape=_loop_shape())
    lines = _plain(screen.render_body(58)).splitlines()
    grand_tour = CATEGORY_BY_ID["grand_tour"]
    expected = f"{discipline_label(grand_tour, discipline_lane())} — 1st place: 2 nodes"
    assert lines[0] == expected  # the first row, before the space above the strip
    assert "score" not in _plain(screen.render_body(58))
    for _ in range(3):  # the header stays in place on each tab
        screen.handle("tab")
        assert _plain(screen.render_body(58)).splitlines()[0] == expected

    partial = _record(
        category="long_haul",
        score=8.4,
        stats={"km_travelled": 8.4, "km_complete": False, "hop_count": 3, "distinct_nodes": 3},
    )
    head = _plain(_dialog(partial, rank=5).render_body(58)).splitlines()[0]
    assert head.endswith("Longest distance — 5th place: ≥ 8.4 km")  # the board spelling of a bound

    # The mark lane is the lane of the board heading: the titles of the two marks start in one cell.
    long_haul = CATEGORY_BY_ID["long_haul"]
    assert cell_len(discipline_label(long_haul, discipline_lane()).split("Longest")[0]) == cell_len(
        discipline_label(grand_tour, discipline_lane()).split("Most")[0]
    )


def test_record_route_tab_draws_the_walk_and_enter_traces_it() -> None:
    """The Route tab has the graph over the route line, with Trace this path as its one row.

    The rule of the node page for its Routes tab is that Enter on the visible route arms its
    trace. Thus the tab has that action only. The Delete action of the page stays on Info.
    """
    screen = _dialog(_record(), far_label="Far", shape=_loop_shape())
    screen.note_viewport(40)  # the frame records this before each real paint
    screen.handle("tab")
    lines = _plain(screen.render_body(58)).splitlines()
    stage = [line for line in lines[_strip_end(lines) :] if line]
    assert "▲ repeater" in "\n".join(stage)  # the legend of the graph
    assert any(line.startswith("★") and line.endswith("★") for line in stage)  # the route line
    assert stage[-1].endswith("Trace this path — reopen in Trace path")
    assert "Delete record" not in "\n".join(stage)
    assert "spec" not in "\n".join(stage), "the stats have a tab of their own"
    captured: list = []
    screen.resolve = captured.append  # type: ignore[method-assign]
    screen.handle("enter")
    assert captured == ["trace"]


def test_record_area_tab_fills_the_stage_with_hash_byte_pins() -> None:
    """⇧Tab turns to the Area tab: the ground, with pins and labels, sized to the viewport.

    The drawing was once beside the stats, in a third of the width, with no labels, because
    there was no cell for a label. Now it has its own tab and uses all the rows that the strip
    leaves. Thus the user can match a pin with the graph node on the Route tab by the shared
    byte, and a taller terminal gives a bigger shape.
    """
    screen = _dialog(_record(), far_label="Far", shape=_loop_shape())
    screen.note_metrics(40, 24)
    screen.handle("shift_tab")  # Info → Area, the last tab
    lines = _plain(screen.render_body(58)).splitlines()
    stage = [line for line in lines[_strip_end(lines) :] if line]
    block = "\n".join(stage)
    assert any(_is_braille(ch) for ch in block)  # the area, in braille
    assert "★" in block  # us, pinned at the origin
    assert "▲" in block  # the repeater vertex, with its glyph from the map
    assert HUB_ID[:2] in block and FAR_ID[:2] in block  # the hash byte of each hop is by its pin
    assert stage[-1] == "north up · labels = hash byte"  # the caption explains the drawing
    assert "Trace this path" not in block  # a picture: no action rows
    assert len(lines) <= 24, "the Area tab never outgrows the viewport"

    screen.note_metrics(40, 40)
    taller = _plain(screen.render_body(58)).splitlines()
    assert len(taller) > len(lines), "a taller viewport is a taller drawing"

    screen.handle("tab")  # …and round to Info again
    assert "Delete record" in _plain(screen.render_body(58))


def test_record_area_drawing_spends_the_width_it_is_given() -> None:
    """The drawing is as wide as the page. A wider page gives a bigger shape, not padding."""

    def drawn_width(width: int) -> int:
        screen = _dialog(_record(), shape=_loop_shape())
        # Enough rows, so that the proportions of the shape depend on the width, not the height.
        screen.note_metrics(40, 40)
        screen.handle("shift_tab")  # the Area tab
        lines = _plain(screen.render_body(width)).splitlines()
        return max(len(line.rstrip()) for line in lines[_strip_end(lines) : -1])

    assert drawn_width(72) > drawn_width(40)


def test_record_area_tab_says_so_when_too_narrow_to_draw() -> None:
    """Below the minimum width of the drawing, the Area tab says why it is empty."""
    screen = _dialog(_record(), far_label="Far", shape=_loop_shape())
    screen.handle("shift_tab")  # the Area tab
    body = _plain(screen.render_body(12))
    assert not any(_is_braille(ch) for ch in body)
    assert "no room" in body  # cut at the edge like each row, but it is there


def test_record_without_a_shape_has_no_area_tab() -> None:
    """If no hop has a position, there is no Area tab. Info and Route still switch."""
    screen = _dialog(_record())
    lines = _plain(screen.render_body(60)).splitlines()
    strip = "\n".join(lines[: _strip_end(lines)])
    assert "Info" in strip and "Route" in strip and "Area" not in strip
    screen.handle("shift_tab")
    assert screen._tab == "Route"


def test_record_footer_names_only_what_each_tab_answers() -> None:
    """Each tab hints only the keys that it answers.

    Info hints the cursor keys. Route hints only Enter (it has one row). Area hints only the
    switch and Esc.
    """
    screen = _dialog(_record(), shape=_loop_shape())
    assert screen.footer_hint == "Tab/⇧Tab switch · ↑↓ move · Enter select · Esc back"
    screen.note_metrics(40, 10)  # taller than the viewport: the pager has its atom
    assert "PgUp/PgDn scroll" in screen.footer_hint
    screen.handle("tab")
    screen.note_metrics(10, 24)
    assert screen.footer_hint == "Tab/⇧Tab switch · Enter select · Esc back"
    screen.handle("tab")
    assert screen.footer_hint == "Tab/⇧Tab switch · Esc back"
    screen.handle("down")  # inert on the Area tab
    assert screen._index == 0


def test_record_dialog_scrolls_free_of_the_action_cursor() -> None:
    """PgUp, PgDn, Home, and End scroll the card. The highlight of the actions does not follow."""
    dialog = _dialog(_record(), shape=_loop_shape())
    dialog.note_metrics(40, 10)  # a body taller than the viewport
    assert dialog.cursor_line() is None  # opens at the top, free of the highlight
    dialog.handle("pagedown")
    assert dialog.scroll > 0 and not dialog._follow
    dialog.handle("end")
    assert dialog.scroll == 30  # total 40 − viewport 10
    dialog.handle("home")
    assert dialog.scroll == 0


def test_record_dialog_arrows_follow_the_action_cursor() -> None:
    """An arrow key starts the highlight-follow again, so the selected action stays visible."""
    dialog = _dialog(_record())
    dialog.handle("pagedown")  # into free scroll
    assert not dialog._follow
    dialog.handle("down")
    assert dialog._follow
    dialog.render_body(60)  # the frame renders before it reads the cursor
    assert dialog.cursor_line() is not None


# --- the browser loop: dialogs float over the trophy case, never over a blank frame ---


@pytest.fixture()
def tui_ctx(tmp_path: Path) -> AppContext:
    """A context with a mock backend and a headless TUI session (no prompt_toolkit app)."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "records.db")
    ctx = AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    ctx.ui = TuiUi(TuiSession())
    yield ctx
    ctx.repo.close()


async def _step_until(predicate, *, limit: int = 200):
    """Yield to the event loop until ``predicate()`` is true. Return its (last) value."""
    value = predicate()
    for _ in range(limit):
        if value:
            return value
        await asyncio.sleep(0)
        value = predicate()
    return value


def _trophy_case(session, *, other_than=None):
    """The trophy-case browser on top of the stack, if it is the top screen."""
    top = session.top
    if isinstance(top, SelectScreen) and top.title == "Trophy case" and top is not other_than:
        return top
    return None


async def test_the_trophy_case_fills_the_frame_over_a_trace_too(tui_ctx) -> None:
    """The trophy case is a place, so it is full-frame from each way in.

    From the menu, it is the only screen, and the fallback of the compositor drew it
    full-frame. When the user opened it from a trace, the trace screen was still pushed,
    and the same list came up as a box that fits its content. The reason is that a bare
    ``SelectScreen`` floats by default.
    """
    from meshterm.ui.tui.screen import ScrollScreen

    ctx = tui_ctx
    session = ctx.ui.session
    trace = ScrollScreen("", title="Trace — Lakeside", floating=False)
    session.push(trace)

    task = asyncio.ensure_future(open_records(ctx))
    try:
        browser = await _step_until(lambda: _trophy_case(session))
        assert browser is not None, "the trophy case list never opened"
        assert not browser.floating
        assert session._base_screen() is browser, "the page fills the frame, not a box over it"
        assert not session._has_float()
        browser.handle("escape")
        await task
        assert session._base_screen() is trace, "and Esc lands back on the trace"
    finally:
        if not task.done():
            task.cancel()
        session.pop(trace)


async def test_delete_all_confirm_floats_over_the_browser(tui_ctx) -> None:
    """The trophy case stays drawn full-frame behind the "Delete all records" confirm.

    Regression: the code ran the browser and then popped it before the confirm opened. Thus
    the confirm floated over a blank frame, and the background was "cleared". Now the
    browser never leaves the stack (``session.stay``). It is the full-frame base while the
    red typed-delete gate floats over it. When the user backs out, the same screen returns,
    with its highlight, and not a new screen.
    """
    ctx = tui_ctx
    session = ctx.ui.session
    ctx.repo.record_discovery(
        "grand_tour",
        1,
        "3d,f2",
        (HUB_ID, FAR_ID),
        score=2.0,
        stats={"hop_count": 2, "distinct_nodes": 2},
        app_version="0.1.0",
    )

    task = asyncio.ensure_future(open_records(ctx))
    try:
        browser = await _step_until(lambda: _trophy_case(session))
        assert browser is not None, "the trophy case list never opened"

        browser.resolve(("del_all", None, 0, None))  # select "Delete all records"
        confirm = await _step_until(
            lambda: session._float_layers()[0] if session._has_float() else None
        )
        assert isinstance(confirm, TypedConfirmDialog)  # the red typed-delete gate
        assert session._base_screen() is browser  # the browser is still the full-frame base

        confirm.resolve(CANCEL)  # the Esc key: back out without a delete
        again = await _step_until(lambda: _trophy_case(session))
        assert again is browser, "nothing was deleted, so the same screen carries on"
        again.handle("escape")  # the Esc key leaves the trophy case
        result = await task
    finally:
        if not task.done():
            task.cancel()

    assert result == {"records": 1}  # the user backed out, so the record remains


async def test_delete_a_disciplines_records_is_a_popup_over_the_browser(tui_ctx) -> None:
    """The choice of a discipline to delete floats a dialog over the browser, not a full list.

    The discipline picker is a floating :class:`SelectScreen`. The browser stays on the
    stack, so the picker draws as a centred dialog over the visible trophy case
    (``_base_screen`` stays the browser). It does not replace the whole frame.
    """
    ctx = tui_ctx
    session = ctx.ui.session
    ctx.repo.record_discovery(
        "grand_tour",
        1,
        "3d,f2",
        (HUB_ID, FAR_ID),
        score=2.0,
        stats={"hop_count": 2, "distinct_nodes": 2},
        app_version="0.1.0",
    )

    task = asyncio.ensure_future(open_records(ctx))
    try:
        browser = await _step_until(lambda: _trophy_case(session))
        assert browser is not None

        browser.resolve(("del_cat", None, 0, None))  # "Delete a discipline's records…"
        picker = await _step_until(
            lambda: session._float_layers()[0] if session._has_float() else None
        )
        assert isinstance(picker, SelectScreen)  # a floating dialog, not the frame
        assert picker.title == "Delete a discipline's records"
        assert session._base_screen() is browser  # the browser stays behind it

        picker.resolve(CANCEL)  # the Esc key: abandon the choice
        again = await _step_until(lambda: _trophy_case(session))
        assert again is browser, "the pick was abandoned, so the same screen carries on"
        again.handle("escape")  # the Esc key
        result = await task
    finally:
        if not task.done():
            task.cancel()

    assert result == {"records": 1}


async def test_a_board_row_spends_its_cells_on_the_walk(tui_ctx) -> None:
    """A record row starts with the rank, the day, and the score, then the walk with bare ends.

    Each record is a boomerang. When the row named us at both ends, it said the same thing
    twice and used more cells than the whole score lane. Thus the ends are ``★``, and the
    score lane has the width of the widest score of this board, not a fixed twelve.
    """
    ctx = tui_ctx
    session = ctx.ui.session
    ctx.repo.record_discovery(
        "grand_tour",
        1,
        "3d,f2",
        (HUB_ID, FAR_ID, HUB_ID),
        score=2.0,
        stats={"hop_count": 3, "distinct_nodes": 2},
        app_version="0.1.0",
    )

    task = asyncio.ensure_future(open_records(ctx))
    try:
        browser = await _step_until(lambda: _trophy_case(session))
        assert browser is not None
        label = next(
            choice.label
            for choice in browser._choices()
            if isinstance(choice.value, tuple) and choice.value[0] == "open"
        )
        row = label.plain
        assert row.startswith("#1 ")  # the standing on the board
        assert "2 nodes" in row  # the score, in the unit of the discipline
        assert row.count("★") == 2  # our two ends, bare: the row does not name us twice
        assert "Homestead" not in row  # …so the device name is not on the line
        # Also, those ends are us, in the white of "you". We compose nothing here, so
        # there is no "not yours" fade to use.
        stars = [s for s in label.spans if row[s.start : s.end] == "★"]
        assert [str(s.style) for s in stars] == ["you", "you"]
        assert "3d" in row and "f2" in row  # the hops, at the hash width of the record
        # The lanes before the walk keep to what they must show: a score lane with the width
        # of the widest score of this board (no dead padding), and the day without the minute
        # that the dialog shows. Each cell that they do not use is a hop that the route can show.
        assert "2 nodes  ★" in row
        assert ":" not in row[: row.index("★")]
    finally:
        if not task.done():
            task.cancel()


async def test_the_board_pins_its_discipline_heading_and_description(tui_ctx) -> None:
    """When the list scrolls into a board, its ``── discipline ──`` heading and description pin.

    Each discipline heading has a word-wrapped description under it. The description says
    what that board scores, and this is what the user needs when the list is scrolled
    part of the way down the board. The two pin as one block. When each separator was a
    candidate for pinning, the last muted line under a heading won the top row alone, and
    the heading never stuck. This was worst for the first discipline, because the first
    scrolled row is under its description.
    """
    ctx = tui_ctx
    session = ctx.ui.session
    for i in range(6):  # one full board, so its records outlast a short viewport
        ctx.repo.record_discovery(
            "grand_tour",
            1,
            f"3d,{i:02x}",
            (HUB_ID, FAR_ID),
            score=float(i + 1),
            stats={"hop_count": 2, "distinct_nodes": 2},
            app_version="0.1.0",
        )

    task = asyncio.ensure_future(open_records(ctx))
    try:
        browser = await _step_until(lambda: _trophy_case(session))
        assert browser is not None
        # Only the six disciplines are landmarks. Each block starts with its own heading and
        # has the description that is under it. Nothing else is a candidate.
        browser.render_body(72)
        blocks = [
            [_plain([line]).strip() for line in rows] for _idx, rows in browser._sticky_headers
        ]
        lane = discipline_lane()
        assert [rows[0] for rows in blocks] == [
            f"── {discipline_label(c, lane)} ──" for c in CATEGORIES
        ]
        # Also, each of them starts its title in the same column. The marks do not all have
        # the same width (🛣 and 🕸 are one cell, and the others are two), so the lane pads.
        starts = {cell_len(discipline_label(c, lane)) - cell_len(c.title) for c in CATEGORIES}
        assert len(starts) == 1
        assert all(len(rows) >= 2 for rows in blocks)  # each carries its description too
        # A highlight deep in the one board that has records scrolls its heading off the top
        # of a short viewport. The heading is the first item that pins, and its description
        # is under it.
        for _ in range(4):
            browser.handle("down")
        visible, above, _below = frame._visible_slice(browser, browser.render_body(72), 10)
        board = CATEGORY_BY_ID["grand_tour"]
        assert _plain([visible[0]]).strip() == f"── {discipline_label(board, lane)} ──"
        assert _plain([visible[1]]).strip().startswith(board.description[:20])
        assert above is True
        browser.resolve(None)  # the Esc key
        await task
    finally:
        if not task.done():
            task.cancel()


async def test_trace_this_path_nests_above_the_browser_and_comes_back_to_it(
    tui_ctx, monkeypatch
) -> None:
    """Trace this path opens over the trophy case, and the Esc key from it lands back on the record.

    The stack was once flattened. The browser closed first, and the whole flow returned when
    the trace closed. This put the user on the main menu. It was an escape that the code
    built by hand, from a stack that had no other way out. Navigation is a strict stack now,
    so a sub-view nests like each other sub-view, and the ^W key leaves the whole excursion at
    once.
    """
    ctx = tui_ctx
    session = ctx.ui.session
    ctx.repo.record_discovery(
        "grand_tour",
        1,
        "3d,f2",
        (HUB_ID, FAR_ID),
        score=2.0,
        stats={"hop_count": 2, "distinct_nodes": 2},
        app_version="0.1.0",
    )
    walked: list[str] = []

    async def fake_trace_path(ctx_, spec=""):  # noqa: ANN001
        walked.append(spec)
        # The browser is still on the stack below while the trace runs.
        assert any(isinstance(s, SelectScreen) and s.title == "Trophy case" for s in session._stack)
        return 0

    monkeypatch.setattr("meshterm.ui.trace_screen.open_trace_path", fake_trace_path)

    task = asyncio.ensure_future(open_records(ctx))
    try:
        browser = await _step_until(lambda: _trophy_case(session))
        assert browser is not None
        record = ctx.repo.discoveries("grand_tour")[0]
        browser.resolve(("open", CATEGORY_BY_ID["grand_tour"], 1, record))
        dialog = await _step_until(
            lambda: session.top if isinstance(session.top, RecordScreen) else None
        )
        assert session._base_screen() is dialog, "the record is a page, full-frame"
        dialog.resolve("trace")  # "Trace this path"
        # The trace ran and the browser is back on top. It is the same object, not a new one.
        again = await _step_until(lambda: _trophy_case(session))
        assert again is browser
        again.handle("escape")  # only the Esc key leaves the trophy case
        result = await task
    finally:
        if not task.done():
            task.cancel()

    assert walked == ["3d,f2"]  # the spec of the record went directly to Trace path
    assert result == {"records": 1}


async def test_browser_cuts_every_row_the_way_it_cuts_the_highlighted_one(tui_ctx) -> None:
    """The browser cuts unselected walks and does not elide the middle.

    The highlight only adds the shift. Both ends of the walk of a record are the same ``★``
    on each row (a record is a boomerang). The ``⋯`` rescue saves the two endpoints of a
    route from a right truncation. Here the rescue gives nothing back, and it uses cells
    that the front of the walk needs. The front is the part that is different from row to
    row. Thus each row has the whole line and the browser cuts it at the lane, the same as
    the highlighted row at shift zero.
    """
    ctx = tui_ctx
    long_route = tuple(f"{byte:02x}c24f54551e" for byte in range(0x20, 0x2C))
    for rank, route in enumerate((long_route, long_route[::-1]), start=1):
        ctx.repo.record_discovery(
            "grand_tour",
            1,
            f"{route[0][:2]},f2",
            route,
            score=float(20 - rank),
            stats={"hop_count": len(route), "distinct_nodes": len(route)},
            app_version="0.1.0",
        )

    task = asyncio.ensure_future(open_records(ctx))
    try:
        browser = await _step_until(lambda: _trophy_case(ctx.ui.session))
        assert browser is not None
        rows = [_plain([line]) for line in browser.render_body(72)]
        walks = [row for row in rows if "★" in row]
        assert len(walks) == 2, rows  # one highlighted, one not
        assert not any("⋯" in row for row in walks)  # nothing elides the middle now
        # Highlighted or not, each walk starts at our star right after the score lane…
        assert all(re.search(r"#\d+ .*nodes  ★ → ", row) for row in walks), walks
        # …and runs on past the lane. It does not close on a second endpoint that was saved.
        assert all(row.rstrip().endswith("…") for row in walks), walks
        browser.resolve(None)  # the Esc key
        await task
    finally:
        if not task.done():
            task.cancel()
