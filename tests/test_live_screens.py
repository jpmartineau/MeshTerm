# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the live Trace and TX-optimize screens.

These tests run the pure logic of the two full-screen tools: the state machines of the
trace and the sweep, the key handling, and the rendering. They use fake sessions and
injected runners, so they run fast and with no terminal. ``test_tui`` uses the same method.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from rich.cells import cell_len

from meshterm.core.models import (
    Contact,
    Hop,
    TraceResult,
    TraceStats,
    TxLevelResult,
    TxOptResult,
    utcnow,
)
from meshterm.services.topology import PathScenario, build_topology
from meshterm.ui.braillechart import _METER_SLIM
from meshterm.ui.theme import snr_style
from meshterm.ui.trace_screen import (
    _BAR_WIDTH,
    OPEN_TROPHY_CASE,
    TraceScreen,
    _scenario_detail,
    _scenario_path,
    snr_bar,
)
from meshterm.ui.tx_screen import TxSweepScreen
from tests.conftest import plain as _plain  # the only helper that strips and joins the screen text

# The fill glyphs of the slim meter (the SNR bar draws through the shared braille meter).
_BAR_FULL, _BAR_HALF = _METER_SLIM


class _FakeSession:
    """The session functions that the screens use directly: paints and the dialog stack."""

    def __init__(self) -> None:
        self.repaints = 0
        self.stack: list = []
        #: Each button_dialog that the screen floated, as ``(prompt, buttons, kwargs)``. The
        #: next one resolves with ``dialog_answer`` (``None`` is the Esc or idle-cancel path).
        self.dialogs: list = []
        self.dialog_answer = None

    def invalidate(self) -> None:
        self.repaints += 1

    def push(self, screen) -> None:  # noqa: ANN001
        self.stack.append(screen)

    def pop(self, screen=None) -> None:  # noqa: ANN001
        if screen is None:
            self.stack.pop()
        elif screen in self.stack:
            self.stack.remove(screen)

    async def button_dialog(self, prompt, buttons, **kwargs):  # noqa: ANN001
        self.dialogs.append((prompt, buttons, kwargs))
        return self.dialog_answer

    def run_detached(self, work):  # noqa: ANN001, ANN201
        """Start the flow of a key handler as a task, as ``TuiSession.run_detached`` does."""
        return asyncio.ensure_future(work)


def _trace(*snrs: float, success: bool = True, target: str = "Alice") -> TraceResult:
    """A trace with one hop for each SNR reading."""
    hops = [Hop(index=i, node=f"{i:02x}{i:02x}", snr=snr) for i, snr in enumerate(snrs)]
    return TraceResult(
        target=target, success=success, hops=hops, round_trip_ms=200.0, path_hash_bytes=2
    )


# --- snr_bar -------------------------------------------------------------------


def _lit_plain(bar) -> str:  # noqa: ANN001
    """The prefix of the reading that has colour, without the dim track that is not lit.

    The part that is not lit also uses :data:`_BAR_FULL`. It has the ``track`` style, which
    is dim, and not a different glyph. Thus, to find the lit part and the part that is not
    lit, the test must read the style of each character, not only its glyph.
    """
    if bar.style == "track":
        return ""
    track_spans = [s for s in bar.spans if s.style == "track"]
    end = min((s.start for s in track_spans), default=len(bar.plain))
    return bar.plain[:end]


def _filled_steps(bar) -> int:  # noqa: ANN001
    """Count the fill steps of a rendered bar: two for each full braille cell, one for a half."""
    plain = _lit_plain(bar)
    return plain.count(_BAR_FULL) * 2 + plain.count(_BAR_HALF)


def test_snr_bar_scales_with_signal_quality() -> None:
    """A stronger signal fills more of the track. None gives a track that is not lit at all."""
    weak = snr_bar(-12.0)
    strong = snr_bar(8.0)
    assert _filled_steps(weak) < _filled_steps(strong)
    assert len(weak.plain) == len(strong.plain) == _BAR_WIDTH  # the track width is constant
    none_bar = snr_bar(None)
    assert _filled_steps(none_bar) == 0
    assert none_bar.style == "track"  # the whole track is dim, not a run of faint dots


def test_snr_bar_clamps_out_of_range_readings() -> None:
    """Readings beyond the display range clamp to the ends. They do not overflow or underflow."""
    assert _filled_steps(snr_bar(99.0)) == _filled_steps(snr_bar(10.0))
    assert _filled_steps(snr_bar(-99.0)) == 1  # a hop that was heard always shows something


def test_snr_bar_packs_two_steps_per_character() -> None:
    """16 steps of resolution fit in 8 characters: full cells, then one half at the end."""
    one_step = snr_bar(-13.4375)  # frac = 1/16 of the -15..+10 span
    assert one_step.plain[0] == _BAR_HALF
    assert one_step.plain[1:] == _BAR_FULL * (_BAR_WIDTH - 1)  # track that is not lit, same glyph
    track_spans = [(s.start, s.end) for s in one_step.spans if s.style == "track"]
    assert track_spans == [(1, _BAR_WIDTH)]

    three_steps = snr_bar(-10.3125)  # frac = 3/16 → one full cell, one half
    assert three_steps.plain[:2] == _BAR_FULL + _BAR_HALF
    assert three_steps.plain[2:] == _BAR_FULL * (_BAR_WIDTH - 2)


def test_snr_bar_unlit_track_dims_to_a_distinct_style() -> None:
    """The track that is not lit has the ``track`` style.

    It does not have the colour of the reading or the old ``faint`` dots.
    """
    bar = snr_bar(-10.3125)
    assert "·" not in bar.plain  # there is no plain-dot placeholder now
    track_span = next(s for s in bar.spans if s.style == "track")
    assert bar.plain[track_span.start : track_span.end] == _BAR_FULL * (_BAR_WIDTH - 2)
    assert bar.style == snr_style(-10.3125)  # the lit prefix still has the colour of the reading

    assert snr_bar(10.0).plain == _BAR_FULL * _BAR_WIDTH  # top of range: every cell full


# --- _previous_outbound -----------------------------------------------------------


def _walk(*nodes: str, success: bool = True) -> TraceResult:
    """A stored walk where the hop hashes are ``nodes`` (and the last hop is us, with no hash)."""
    hops = [Hop(index=i, node=n, snr=1.0) for i, n in enumerate(nodes)]
    hops.append(Hop(index=len(nodes), node=None, snr=1.0))
    return TraceResult(
        target="Alice", success=success, hops=hops, round_trip_ms=200.0, path_hash_bytes=2
    )


def test_previous_outbound_extracts_the_proven_route() -> None:
    """The first half of the last successful boomerang is the outbound leg to use again.

    We checked on hardware that the device almost never has a learned route (contacts
    report flood). Thus this stored evidence is what auto mode walks for a target that
    has more than one hop.
    """
    from meshterm.ui.trace_screen import _previous_outbound

    walk = _walk("3d63", "f2c2", "aabb", "f2c2", "3d63")
    assert _previous_outbound(walk, "aabb" + "00" * 30) == ("3d63", "f2c2")


def test_previous_outbound_direct_walk_yields_no_repeaters() -> None:
    """A direct answer (the target only) gives an empty outbound leg: destination only again."""
    from meshterm.ui.trace_screen import _previous_outbound

    assert _previous_outbound(_walk("aabb"), "aabb" + "00" * 30) == ()


def test_previous_outbound_rejects_unusable_history() -> None:
    """MeshTerm never reuses failures, asymmetric walks, and walks that turned elsewhere."""
    from meshterm.ui.trace_screen import _previous_outbound

    target = "aabb" + "00" * 30
    assert _previous_outbound(None, target) is None
    assert _previous_outbound(_walk("3d63", "aabb", "3d63", success=False), target) is None
    # Asymmetric: it came home a different way, so it is not a boomerang to this target.
    assert _previous_outbound(_walk("3d63", "aabb", "f2c2"), target) is None
    # Palindromic, but it turned at some other node and not at our target.
    assert _previous_outbound(_walk("3d63", "9999", "3d63"), target) is None


# --- _previous_walk ---------------------------------------------------------------


def test_previous_walk_returns_the_whole_proven_route() -> None:
    """The last successful path walk is returned with no change, because it is already a whole spec.

    A boomerang in target mode has a shape that MeshTerm must recognise. A path walk has no
    such shape. The route that the mesh carried from end to end is the route to offer again,
    with the hop hashes at the width that MeshTerm transmitted.
    """
    from meshterm.ui.trace_screen import _previous_walk

    walk = _walk("3d63", "f2c2", "27ab")
    assert _previous_walk(walk) == ("3d63", "f2c2", "27ab")


def test_previous_walk_rejects_unusable_history() -> None:
    """MeshTerm never reuses an empty history, a failed walk, or a walk with no hop to address."""
    from meshterm.ui.trace_screen import _previous_walk

    assert _previous_walk(None) is None
    assert _previous_walk(_walk("3d63", success=False)) is None
    assert _previous_walk(_walk()) is None  # only the last hop (us), which has no hash


# --- TraceScreen ----------------------------------------------------------------


def _trace_screen(
    trace=None,
    compose_path=None,
    explore=None,
    pick_width=None,
    pick_samples=None,
    previous=None,
    mode="target",
    samples=1,
    auto_spec=None,
    auto_source="",
    open_trophy_case=None,
) -> tuple[TraceScreen, _FakeSession]:
    session = _FakeSession()

    async def default_trace(path_spec, on_trace):  # noqa: ANN001
        on_trace(_trace(5.0, 2.0))

    async def default_flow(current):  # noqa: ANN001
        return current

    screen = TraceScreen(
        "Alice" if mode == "target" else "(path)",
        mode=mode,
        device_label="Us",
        device_hash="aabb" + "00" * 30,
        resolve=lambda label: label,
        session=session,
        trace=trace or default_trace,
        compose_path=compose_path or default_flow,
        explore=(explore or default_flow) if mode == "target" else None,
        pick_width=pick_width or default_flow,
        pick_samples=pick_samples or default_flow,
        width_bytes=lambda: 2,
        sample_count=lambda: samples,
        pace_s=0.0,  # tests never sleep. The tests check the pacing through the statuses
        previous=previous,
        auto_spec=auto_spec or (lambda: ""),
        auto_source=auto_source,
        open_trophy_case=open_trophy_case,
    )
    screen.note_viewport(40)  # the frame records this before each real paint
    return screen, session


async def test_trace_screen_one_trace_per_enter_accumulates() -> None:
    """At the default sample count, each Enter key press transmits exactly one trace.

    Repeated samples are a decision of the user, unless the user chooses a bigger sample
    count. Thus three key presses give three traces and a median of three samples.
    """
    screen, _ = _trace_screen()
    for _ in range(3):
        screen.start_trace()
        await screen._worker
    body = _plain(screen.render_body(100))
    assert "success rate" in body and "3/3" in body
    assert "#3" in body  # numbering with the newest first
    assert "Per-hop medians" in body
    assert "burst" not in body.lower()  # no burst setting is offered anywhere


async def test_trace_screen_runs_the_chosen_sample_count() -> None:
    """One Enter key press runs the whole sample count that the user chose. It stores each trace."""
    ran: list[str] = []

    async def trace(path_spec, on_trace):  # noqa: ANN001
        ran.append(path_spec)
        on_trace(_trace(5.0))

    screen, session = _trace_screen(trace=trace, samples=3)
    screen.start_trace()
    await screen._worker
    assert len(ran) == 3
    assert len(screen._traces) == 3
    assert session.stack == []  # the dialog was popped when the run ended


async def test_trace_screen_multi_trace_reports_progress_and_abort_keeps_landed() -> None:
    """A run of many traces shows its count. If the user aborts, the traces that arrived stay."""
    release = asyncio.Event()
    ran = 0

    async def trace(path_spec, on_trace):  # noqa: ANN001
        nonlocal ran
        ran += 1
        on_trace(_trace(5.0))
        if ran == 2:
            await release.wait()  # hold the run in the middle, at the second trace

    screen, session = _trace_screen(trace=trace, samples=5)
    screen.start_trace()
    await asyncio.sleep(0)
    dialog = session.stack[0]
    assert "2/5" in dialog.status  # the dialog shows the count of the run
    assert "2/5" in screen.footer_hint
    assert "2/5" in _plain(screen.render_body(100))  # the spinner row of the log also shows it
    screen.cancel()
    with pytest.raises(asyncio.CancelledError):
        await screen._worker
    assert len(screen._traces) == 2  # the traces that were already stored stay
    assert session.stack == []


async def test_trace_results_are_page_that_edge_scroll_reaches() -> None:
    """After a trace, ↓ on the last action scrolls down the results to the oldest trace.

    The results were once in a list window of their own, and the controls set its size. The
    page was then exactly the height of the screen, so the frame had nothing to scroll. ↓ on
    Trace did nothing while ``↓ n more`` was under it.
    """
    from meshterm.ui.tui import frame

    screen, _ = _trace_screen()
    for _ in range(12):  # a log long enough to push the actions off the top
        screen.start_trace()
        await screen._worker

    def view() -> list[str]:
        lines = screen.render_body(72)
        visible, _above, _below = frame._visible_slice(screen, lines, 16)
        return [_plain([line]).strip() for line in visible]

    def press(action: str) -> None:
        if not screen.edge_scroll(action):
            screen.handle(action)

    body = _plain(screen.render_body(72))
    assert "more" not in body  # each result is on the page, and no list window hides one
    view()
    press("down")  # Trace is the last action: the highlight stays on it…
    view()
    for _ in range(30):
        press("down")  # …and each more ↓ scrolls the page one line
        view()
    page = view()
    assert page[-1].startswith("#1")  # the oldest trace is the end of the page
    assert not any("❯" in line for line in page)  # the actions scrolled off the top
    press("up")  # the snap-back: Trace returns before anything moves
    assert any("❯" in line and "Trace" in line for line in view())


def _hop_rows_screen() -> TraceScreen:
    """A trace screen with one walk that goes out through two relays that have names, and home."""
    names = {"3d63": "Lakeside", "f2c2": "Mont-Royal Summit Relay"}
    screen, _ = _trace_screen()
    screen._resolve = lambda hop: names.get(hop, hop)
    screen._on_trace(
        TraceResult(
            target="Alice",
            success=True,
            hops=[Hop(0, "3d63", 5.0), Hop(1, "f2c2", -3.5), Hop(2, None, 1.0)],
            round_trip_ms=200.0,
            path_hash_bytes=2,
        )
    )
    return screen


def _hop_rows(screen: TraceScreen, width: int) -> list[str]:
    """The rows of the per-hop medians as drawn, without the heading and the trace log."""
    lines = [_plain([line]) for line in screen.render_body(width)]
    start = next(i for i, line in enumerate(lines) if line.strip() == "Per-hop medians") + 1
    end = next(i for i, line in enumerate(lines) if line.strip() == "Traces") - 1
    return lines[start:end]


def test_hop_rows_name_their_link_on_one_line_where_it_fits() -> None:
    """Each hop is ``n  origin → destination  reading  meter``, with names and never a hash."""
    rows = _hop_rows(_hop_rows_screen(), 100)
    assert len(rows) == 3  # one line for each hop
    assert "-3.5 dB" in rows[1] and _BAR_FULL in rows[1]
    assert not any("(3d" in row or "(f2" in row for row in rows)
    # Only the ends of the route are bare. Each end where the route continues keeps its arrow.
    assert rows[0].startswith("0 ★ → Lakeside → ")  # our end is the bare star
    assert rows[1].startswith("1 → Lakeside → Mont-Royal Summit Relay → ")
    assert rows[2].startswith("2 → Mont-Royal Summit Relay → ★ ")
    # A table of four columns: each reading ends in the same column.
    assert len({row.index(" dB") for row in rows}) == 1


def test_hop_rows_square_only_the_routes_own_ends(powerline) -> None:  # noqa: ANN001
    """As chips, the first hop opens square and the last hop closes square. Nothing else is square.

    A hop is one link of the walk, so its far ends are nodes where the route continues.
    These have the notch and the point, the marks that a path line uses for "this goes on".
    """
    from meshterm.ui.pathline import POWERLINE_SEP

    powerline(True)
    rows = _hop_rows(_hop_rows_screen(), 50)  # chips are wider: each hop folds
    paths = [row[2:].rstrip() for row in rows[::2]]
    assert [p.startswith(POWERLINE_SEP) for p in paths] == [False, True, True]
    assert [p.endswith(POWERLINE_SEP) for p in paths] == [True, True, False]


def test_hop_rows_fold_under_their_path_and_slide_where_it_does_not() -> None:
    """If the width is too narrow for a row, each hop has two lines. ←→ slide only the paths.

    The reading and its meter are at the right on the line under the path. A path that goes
    past the edge has a crack there and slides. The hop number stays pinned, and a path that
    already fits does not move.
    """
    screen = _hop_rows_screen()
    rows = _hop_rows(screen, 30)
    assert len(rows) == 6  # two lines for each hop, all the same
    assert rows[2].startswith("1 → Lakeside → Mont-Royal")
    assert rows[2].rstrip().endswith("…")  # cut where it continues
    assert "-3.5 dB" in rows[3] and cell_len(rows[3].rstrip()) == 30  # flush right
    assert "←→ scroll" in screen.footer_hint

    screen.handle("down")  # the highlight on the actions pins the page…
    screen.handle("right")
    assert screen.cursor_line() is None  # …and when the paths slide, the pin is released
    screen.handle("right")
    rows = _hop_rows(screen, 30)
    assert rows[2].startswith("1 …") and rows[2].rstrip().endswith("Summit Relay →")
    assert rows[0].startswith("0 ★ → Lakeside →")  # a path that is whole on screen does not move
    screen.handle("right")  # already at the tail: clamped
    assert _hop_rows(screen, 30)[2] == rows[2]
    screen.handle("left")
    screen.handle("left")
    assert _hop_rows(screen, 30)[2].startswith("1 → Lakeside")

    _hop_rows(screen, 100)  # wide enough again: nothing to slide, so the hint does not show it
    assert "←→ scroll" not in screen.footer_hint


async def test_trace_screen_seeds_route_from_previous_trace() -> None:
    """Before a new reply, the stored route shows, with the mark "previous"."""
    old = _trace(4.0)
    old.timestamp = utcnow() - timedelta(hours=3)
    screen, _ = _trace_screen(previous=old)
    body = _plain(screen.render_body(100))
    assert "(previous" in body
    # A new success replaces the route that the screen started with, and removes the mark.
    screen._on_trace(_trace(6.0))
    body = _plain(screen.render_body(100))
    assert "(previous" not in body


async def test_previous_stamp_sits_on_its_own_line() -> None:
    """The (previous · …) mark renders under the route. It is never pushed beside the route."""
    old = _trace(4.0)
    old.timestamp = utcnow() - timedelta(hours=3)
    screen, _ = _trace_screen(previous=old)
    stamp_line = next(ln for ln in screen.render_body(100) if "(previous" in ln)
    assert "→" not in stamp_line  # the route stays on the line above


async def test_trace_screen_only_one_trace_at_a_time() -> None:
    """The Enter key during a trace in progress does nothing. The running flag stops a new run."""
    started = 0
    release = asyncio.Event()

    async def trace(path_spec, on_trace):  # noqa: ANN001
        nonlocal started
        started += 1
        await release.wait()

    screen, _ = _trace_screen(trace=trace)
    screen.start_trace()
    assert screen._running
    screen.handle("enter")  # the screen ignores it while a trace runs
    release.set()
    await screen._worker
    assert started == 1
    assert not screen._running


async def test_trace_screen_failure_reads_inline() -> None:
    """A trace that failed shows its error in the log area. The screen does not crash."""

    async def trace(path_spec, on_trace):  # noqa: ANN001
        raise RuntimeError("no route")

    screen, _ = _trace_screen(trace=trace)
    screen.start_trace()
    await screen._worker
    assert "trace failed: no route" in _plain(screen.render_body(100))


async def test_trace_screen_composer_updates_the_spec() -> None:
    """If the user commits Compose path, the flow runs. Its result is the path of the next trace."""
    asked: list[str] = []

    async def compose(current: str):  # noqa: ANN001
        asked.append(current)
        return "3d,f2,3d"  # one forced hop: outbound, target, then the mirrored return

    screen, _ = _trace_screen(compose_path=compose)
    for _ in range(len(screen._actions) - 1):
        screen.handle("up")  # the highlight starts on Trace, the last row. Move it up to Compose
    screen.handle("enter")
    await asyncio.sleep(0)
    assert asked == [""]
    assert screen._path_spec == "3d,f2,3d"
    body = _plain(screen.render_body(100))
    assert "3d,f2,3d" in body
    # the planned route previews the outbound leg and the return leg (resolved, and dim).
    # Our ends have no words: only the star that stands for us
    assert "route  ★ → 3d → f2 → 3d → ★" in body
    assert body.count("3d") >= 2


async def test_compose_seeds_from_the_visible_route_not_the_empty_spec() -> None:
    """Compose starts from the route that is on the screen, not from the stored spec.

    At the first open, the user composed nothing, but the screen already shows the plan that
    MeshTerm resolved. The composer must start with the hops of that plan. Thus the user
    edits the route that the user sees and does not start from a blank path.
    """
    asked: list[str] = []

    async def compose(current: str):  # noqa: ANN001
        asked.append(current)
        return None  # check only the seed. The (auto) plan does not change

    screen, _ = _trace_screen(
        mode="path",
        compose_path=compose,
        auto_spec=lambda: "3d,f2",
        auto_source="last walk",
    )
    screen._index = screen._actions.index("compose")
    screen.handle("enter")
    await asyncio.sleep(0)
    assert asked == ["3d,f2"]  # it starts from the auto plan that is visible, not ""
    assert screen._path_spec == ""  # None keeps the plan auto (nothing is pinned)


async def test_trace_screen_explore_adopts_a_scenario_path() -> None:
    """If the user commits Explore paths, the flow runs. An adopted path sets the spec."""

    async def adopt(current: str):  # noqa: ANN001
        return "3d63,f2c2"

    screen, _ = _trace_screen(explore=adopt)
    for _ in range(3):
        screen.handle("up")  # Trace → Sample count → Path width → Explore paths
    screen.handle("enter")
    await asyncio.sleep(0)
    assert screen._path_spec == "3d63,f2c2"

    async def keep(current: str):  # noqa: ANN001
        return None

    screen._explore = keep
    screen.handle("enter")  # the highlight is still on Explore paths
    await asyncio.sleep(0)
    assert screen._path_spec == "3d63,f2c2"  # None leaves the spec untouched


async def test_trace_screen_action_labels_share_one_column() -> None:
    """The label of each action starts in the same column, with a wide mark or a narrow mark.

    ``⚡`` is an emoji. It uses two cells, and ``✎``, ``⚙``, ``#``, and ``▶`` use one. Thus a
    fixed prefix ``"icon "`` starts the label of Explore one column to the right of the
    other labels.
    """
    screen, _ = _trace_screen()
    labels = ["Compose path", "Explore paths", "Path width", "Sample count", "Trace —"]
    columns = set()
    for row in _plain(screen.render_body(100)).splitlines():
        for label in labels:
            if label in row:
                columns.add(cell_len(row[: row.index(label)]))
    assert len(columns) == 1, columns


def _words_start(row: str) -> int:
    """The cell where the words of an action row start, after its pointer and its mark.

    The function measures in cells the text before the first letter or digit. It never
    uses a character index. ``⚡`` is one character that uses two cells, so a count of
    characters says that a row that is not aligned is aligned. Also, ``#``, the mark of the
    sample count, is not a letter.
    """
    return cell_len(row[: next(i for i, ch in enumerate(row) if ch.isalnum())])


@pytest.mark.parametrize("platform_name", ["regular", "picocalc-lyra"])
def test_every_action_row_starts_its_words_in_one_cell_in_both_modes(platform_name: str) -> None:
    """Each action that the screen can draw, in each mode, lit or not, has one word column.

    The rendered test above reads only target mode, and only the rows that it names. Thus
    it never measured the ``⇄ Reverse path`` row of the path walk, which exists only in
    path mode. This test asks the screen for its own action list. It also checks the other
    half of the contract: the one column is the same in both modes. ``_ACTION_ICONS``
    declares ``⚡`` also in path mode, where no row draws it. Thus when the user changes a
    walk to a target (or back), the labels do not move to the side.

    The test measures the column from the marks that the rows draw. Each of these marks
    must be in ``_ACTION_ICONS``. If a new action has a mark that the tuple does not know,
    an icon of two cells goes into a column of one cell. On the PicoCalc the whole lane is
    removed, so each word starts straight after the pointer.
    """
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
    from meshterm.ui.trace_screen import _ACTION_ICONS

    platform = {"regular": REGULAR, "picocalc-lyra": PICOCALC_LYRA}[platform_name]
    set_platform(platform)
    starts: set[int] = set()
    marks: set[str] = set()
    for mode in ("target", "path"):
        screen, _ = _trace_screen(mode=mode)
        for key in screen._actions:
            for selected in (False, True):
                row = screen._action_text(key, selected).plain
                starts.add(_words_start(row))
                if platform is REGULAR:  # the mark is between the pointer and its gap
                    marks.add(row[2:].split(" ", 1)[0])
    pointer = cell_len("❯ ")
    if platform is REGULAR:
        # Check the declarations first: a wide mark that is not declared uses its own gap
        # ("⚡Explore"). This is the diagnosis that the developer must read, not the width
        # premise that it also breaks.
        assert marks == set(_ACTION_ICONS), "every drawn mark is declared, and nothing else"
        assert {cell_len(mark) for mark in marks} == {1, 2}, "a list of one width proves nothing"
        assert starts == {pointer + 2 + 1}, starts  # the widest mark, then its space
    else:
        assert starts == {pointer}, starts  # no icon lane, and no padding is left


async def test_trace_screen_action_cursor_commits_the_selected_row() -> None:
    """↑↓ move over the action rows. Enter commits the row that has the highlight.

    The rows are only the own verbs of the screen. No exit row ends them, so Trace is the
    last stop.
    """
    opened: list[str] = []

    async def width_flow(current):  # noqa: ANN001
        opened.append(f"width:{current}")
        return "3d63,f2c2,3d63"

    screen, _ = _trace_screen(pick_width=width_flow)
    body = _plain(screen.render_body(100))
    # The menu order of the actions: build first, then tune, then transmit.
    labels = [
        "Compose path",
        "Explore paths",
        "Path width — 2 bytes per hop",
        "Sample count — 1 trace",
        "Trace — one transmission",
    ]
    positions = [body.index(label) for label in labels]
    assert positions == sorted(positions)
    screen.handle("up")  # Trace → Sample count
    screen.handle("up")  # → Path width
    screen.handle("enter")
    await asyncio.sleep(0)
    assert opened == ["width:"]
    assert screen._path_spec == "3d63,f2c2,3d63"


async def test_trace_screen_sample_count_row_opens_its_dialog() -> None:
    """If the user commits Sample count, the flow floats. If it resolves to None, the spec stays."""
    opened: list[str] = []

    async def samples_flow(current):  # noqa: ANN001
        opened.append(current)
        return None

    screen, _ = _trace_screen(pick_samples=samples_flow)
    screen._path_spec = "3d,f2,3d"
    screen.handle("up")  # Trace → Sample count
    screen.handle("enter")
    await asyncio.sleep(0)
    assert opened == ["3d,f2,3d"]
    assert screen._path_spec == "3d,f2,3d"  # the count is not a spec, so nothing changes


async def test_trace_screen_hotkeys_are_retired() -> None:
    """The old shortcuts w, p, and x are removed. If the user types them, no flow floats."""
    opened: list[str] = []

    async def flow(current):  # noqa: ANN001
        opened.append(current)
        return None

    screen, _ = _trace_screen(compose_path=flow, explore=flow, pick_width=flow, pick_samples=flow)
    for key in ("p", "x", "w", "s"):
        screen.handle("text", key)
    await asyncio.sleep(0)
    assert opened == []
    assert not screen._running


async def test_trace_screen_carries_no_exit_row() -> None:
    """The screen offers only its own verbs. Esc is the way out, and Enter is never the way out."""
    screen, _ = _trace_screen()
    screen.future = asyncio.get_running_loop().create_future()
    assert "Back" not in _plain(screen.render_body(100))
    screen.handle("down")  # the highlight stays on Trace and does not go to an exit row
    assert not screen.future.done()
    screen.handle("escape")
    assert screen.future.result() is None


async def test_composer_commit_parks_the_cursor_on_trace() -> None:
    """If the user commits the composer, the highlight goes to Trace, so a plain Enter walks it."""

    async def compose(current):  # noqa: ANN001
        return "3d,f2,3d"

    screen, _ = _trace_screen(compose_path=compose)
    screen._index = screen._actions.index("compose")
    screen.handle("enter")
    await asyncio.sleep(0)
    assert screen._actions[screen._index] == "trace"

    # Only the composer moves the highlight: the width flow keeps it where it is.
    async def width(current):  # noqa: ANN001
        return "3d63,f2c2,3d63"

    other, _ = _trace_screen(pick_width=width)
    other._index = other._actions.index("width")
    other.handle("enter")
    await asyncio.sleep(0)
    assert other._actions[other._index] == "width"


async def test_composer_escape_leaves_the_cursor_where_it_was() -> None:
    """If the user leaves the composer with Esc, the highlight does not move."""

    async def compose(current):  # noqa: ANN001
        return None  # the result of the composer when the user presses Esc

    screen, _ = _trace_screen(compose_path=compose)
    screen._index = screen._actions.index("compose")
    screen.handle("enter")
    await asyncio.sleep(0)
    assert screen._actions[screen._index] == "compose"


# --- the rule on Trace that forbids cheating -----------------------------------------


async def _settle(screen) -> None:  # noqa: ANN001
    """Let the task of a dialog that was just committed run, then the trace worker it started."""
    for _ in range(5):
        await asyncio.sleep(0)
    if screen._worker is not None:
        await screen._worker


async def test_ineligible_walk_gates_trace_behind_an_amber_confirm() -> None:
    """A walk that crosses a link again in the same direction floats Cancel and Trace.

    Cancel has no side effect.
    """
    ran: list[str] = []

    async def trace(path_spec, on_trace):  # noqa: ANN001
        ran.append(path_spec)
        on_trace(_trace(5.0))

    screen, session = _trace_screen(trace=trace, mode="path")
    screen._path_spec = "3d,f2,3d,f2"  # it goes 3d → f2 two times in the same direction
    screen._index = screen._actions.index("trace")
    screen.handle("enter")  # dialog_answer None means Cancel or Esc
    await _settle(screen)
    prompt, buttons, kwargs = session.dialogs[0]
    assert [label for label, _ in buttons] == ["Cancel", "Trace"]
    assert kwargs["default"] == 1  # Trace is on the right and is the default: Enter commits it
    assert kwargs["border_style"] == "warn"  # the amber caution tier
    assert "trail" in prompt.plain  # the user sees the graph-theory name
    assert ran == []  # cancelled: nothing was transmitted

    session.dialog_answer = "trace"
    screen.handle("enter")
    await _settle(screen)
    assert ran == ["3d,f2,3d,f2"]  # confirmed: the walk is still transmitted


async def test_eligible_walk_traces_without_a_confirm() -> None:
    """An out-and-back walk crosses links again only the other way, so there is no dialog."""
    screen, session = _trace_screen(mode="path")
    screen._path_spec = "3d,f2,3d"
    screen._index = screen._actions.index("trace")
    screen.handle("enter")
    await screen._worker
    assert session.dialogs == []
    assert len(screen._traces) == 1


# --- the new-record dialog -----------------------------------------------------------


async def test_record_run_floats_the_new_record_dialog() -> None:
    """A run that placed floats one dialog. Close (the default) keeps the screen open."""

    async def trace(path_spec, on_trace):  # noqa: ANN001
        on_trace(_trace(5.0), ["Most nodes — 4 nodes"])

    screen, session = _trace_screen(trace=trace)
    screen.future = asyncio.get_running_loop().create_future()
    screen.start_trace()
    await screen._worker
    await _settle(screen)  # the task for the announcement floats after the run settles
    prompt, buttons, kwargs = session.dialogs[0]
    assert "Most nodes — 4 nodes" in prompt.plain
    assert [label for label, _ in buttons] == ["Trophy case", "Close"]
    assert kwargs["default"] == 1  # Close is the default: Enter closes the dialog
    assert kwargs["title"] == "New record"
    assert not screen.future.done()  # Close or Esc: the screen stays open
    assert screen._run_placed == {}  # announced once, and not again at the next run


async def test_record_dialog_trophy_case_opens_over_the_trace_it_was_earned_on() -> None:
    """If the user selects Trophy case, it opens above the trace screen, which stays open under it.

    Before, the code resolved the whole screen with a sentinel. The trace went away first, and
    the user came to the main menu. Navigation is now a strict stack: a sub-view nests. Esc
    from the trophy case is one pop back to the trace that earned the record, and ^W
    leaves the whole excursion.
    """
    opened: list[str] = []

    async def trace(path_spec, on_trace):  # noqa: ANN001
        on_trace(_trace(5.0), ["Most nodes — 4 nodes", "Longest distance — 12.4 km"])

    async def open_trophy_case() -> None:
        opened.append("trophy case")

    screen, session = _trace_screen(trace=trace, samples=2, open_trophy_case=open_trophy_case)
    session.dialog_answer = OPEN_TROPHY_CASE
    screen.future = asyncio.get_running_loop().create_future()
    screen.start_trace()
    await screen._worker
    await _settle(screen)
    _prompt, _buttons, kwargs = session.dialogs[0]
    assert kwargs["title"] == "2 new records"  # one dialog for the whole run
    assert len(session.dialogs) == 1  # for each number of samples that scored
    assert opened == ["trophy case"]
    assert not screen.future.done(), "the trace screen stays up under the trophy case"


async def test_escaping_mid_run_skips_the_record_dialog() -> None:
    """If the user presses Esc while the run has records, the screen leaves with no dialog.

    No dialog follows the user out of the screen.
    """
    release = asyncio.Event()

    async def trace(path_spec, on_trace):  # noqa: ANN001
        on_trace(_trace(5.0), ["Most nodes — 4 nodes"])
        await release.wait()

    screen, session = _trace_screen(trace=trace, samples=2)
    screen.future = asyncio.get_running_loop().create_future()
    screen.start_trace()
    await asyncio.sleep(0)
    screen.handle("escape")  # it resolves the screen and cancels the run
    with pytest.raises(asyncio.CancelledError):
        await screen._worker
    for _ in range(3):
        await asyncio.sleep(0)
    assert session.dialogs == []


def test_planned_route_dims_only_the_mirrored_return_leg() -> None:
    """A palindromic spec in target mode dims its second half. Walks that the user writes never dim.

    Both ends are ours, so both ends are bare: the line starts and ends with a lone arrow.
    """
    screen, _ = _trace_screen()

    def faint_cells(text) -> int:  # noqa: ANN001
        return sum(span.end - span.start for span in text.spans if "faint" in str(span.style))

    screen._path_spec = "3d,f2,3d"  # a symmetric boomerang: the mirror is dim
    symmetric = screen._planned_route().text()
    assert symmetric.plain == "★ → 3d → f2 → 3d → ★"
    screen._path_spec = "3d,f2,27"  # an old walk that the user wrote: each hop is the user's
    custom = screen._planned_route().text()
    assert custom.plain == "★ → 3d → f2 → 27 → ★"
    assert faint_cells(symmetric) > faint_cells(custom)

    # In path mode, even a walk that goes there and back the same way is fully composed by
    # the user. Thus a palindrome must not look "not yours to compose".
    walk, _ = _trace_screen(mode="path")
    walk._path_spec = "3d,f2,3d"
    assert faint_cells(walk._planned_route().text()) == faint_cells(custom)


def test_route_lane_wraps_at_hop_boundaries_under_its_own_column() -> None:
    """A route that is too wide for the lane breaks between hops, never inside a hop.

    Each continuation line hangs under the value column (never at column zero). The line
    that it continues from ends with the ``→`` cue. Each name stays whole: the lane has
    the names of nodes, and the ``path`` lane under it has the hex.
    """
    names = {"3d": "Hilltop-Repeater", "f2": "Mile-End-Rooftop", "27": "Beaubien-Sud"}
    screen, _ = _trace_screen(mode="path")
    screen._resolve = lambda h: names.get(h, h)
    screen._path_spec = "3d,f2,27"
    lines = [line.plain for line in screen._route_value(None, 60)]
    assert len(lines) > 1  # it did wrap at this width
    assert all(len(line) <= 60 for line in lines)
    assert all(line.startswith(" " * 7) for line in lines[1:])  # hanging, not at column 0
    assert all(line.rstrip().endswith("→") for line in lines[:-1])
    for hop, name in names.items():  # the names are whole, and no hash follows any of them
        assert any(name in line for line in lines)
        assert all(f"({hop})" not in line for line in lines)


def test_path_lane_breaks_the_wire_spec_after_a_comma() -> None:
    """The spec lane stays the exact wire string. It folds only at its commas."""
    screen, _ = _trace_screen(mode="path")
    screen._path_spec = "3d63ab99,7f21cd01,27aa1122,f2c20099"
    lines = [line.plain for line in screen._path_value(None, 44)]
    assert len(lines) > 1
    assert all(len(line) <= 44 for line in lines)
    assert lines[0].endswith(",")  # the comma is the cue, not an arrow
    spec = "".join(line.strip() for line in lines)
    assert spec.startswith(screen._path_spec)  # character for character, then the hop count


def test_summary_appends_the_displayed_hop_count() -> None:
    """The path row ends with the number of nodes that the displayed route goes through."""
    screen, _ = _trace_screen()
    screen._path_spec = "3d,f2,3d"
    assert "· 3 hops" in _plain(screen.render_body(100))
    screen._on_trace(_trace(5.0, 2.0))  # a live route of 2 hops now has a higher rank than the plan
    assert "· 2 hops" in _plain(screen.render_body(100))


def test_auto_resolved_route_renders_with_its_provenance() -> None:
    """If the user composed no path, the auto route shows as the plan, with a label for its source.

    The screen draws exactly what Trace will put on the air, because both read the same
    resolver. Thus the user can see the forced boomerang, and where it came from, before the
    user commits a transmission.
    """
    screen, _ = _trace_screen(
        auto_spec=lambda: "3d63,f2c2,aabb,f2c2,3d63", auto_source="last trace · Jul 09 14:32"
    )
    plan = screen._planned_route().text().plain
    assert "→ 3d63 → f2c2 → aabb → f2c2 → 3d63 →" in plan
    # The source hangs on its own line under the route. It is not inside the path.
    route = "\n".join(line.plain for line in screen._route_value(None, 100))
    assert route.endswith("(auto · last trace · Jul 09 14:32)")
    body = _plain(screen.render_body(100))
    assert "auto · last trace · Jul 09 14:32" in body  # the path row of the summary
    assert "· 5 hops" in body


def test_composed_path_outranks_the_auto_route() -> None:
    """A spec that the user composed replaces the auto plan in the display and on the wire."""
    screen, _ = _trace_screen(auto_spec=lambda: "aabb", auto_source="device route")
    screen._path_spec = "3d63,aabb,3d63"
    plan = screen._planned_route().text().plain
    assert "→ 3d63 → aabb → 3d63 →" in plan
    assert "(auto ·" not in plan
    assert screen._effective_spec() == ("3d63,aabb,3d63", False)


def test_unaddressable_target_reads_as_path_less_auto() -> None:
    """If there is nothing to force (no target hash), the summary says this and shows no path."""
    screen, _ = _trace_screen()  # auto_spec resolves ""
    assert "auto — path-less (unknown target)" in _plain(screen.render_body(100))


def test_trace_log_section_hidden_until_there_is_something_to_log() -> None:
    """If the screen is idle with no traces, the Traces heading (and its old hint) do not show."""
    screen, _ = _trace_screen()
    assert "Traces" not in _plain(screen.render_body(100))


async def test_trace_screen_path_mode_gates_trace_and_drops_explore() -> None:
    """Path mode has no Explore row, and Trace does nothing until a path exists."""
    screen, _ = _trace_screen(mode="path")
    body = _plain(screen.render_body(100))
    assert "Explore paths" not in body
    assert "Trace — compose a path first" in body
    assert "none — compose a path" in body
    screen.handle("enter")  # the highlight starts on Trace, but there is nothing to walk
    assert not screen._running and screen._worker is None
    screen._path_spec = "3d,f2"
    assert "Trace — one transmission" in _plain(screen.render_body(100))
    screen.handle("enter")
    assert screen._running
    await screen._worker
    assert len(screen._traces) == 1


async def test_trace_screen_path_mode_arms_from_the_previous_walk() -> None:
    """The last stored walk is not only displayed. It is the path that Enter walks.

    The auto route in path mode (the previous successful walk) must prepare Trace in the same
    way as a composed spec. It must show as the plan with its source. Thus the screen never
    shows a route that it then refuses to trace.
    """
    screen, _ = _trace_screen(
        mode="path", auto_spec=lambda: "3d,f2", auto_source="last walk · Jul 09 14:32"
    )
    body = _plain(screen.render_body(100))
    assert "Trace — one transmission" in body  # ready, not "compose a path first"
    assert "auto · last walk · Jul 09 14:32" in body  # the path row of the summary
    plan = screen._planned_route().text().plain
    assert "→ 3d → f2 →" in plan
    assert "(auto · last walk · Jul 09 14:32)" in body  # hanging under the route
    screen.handle("enter")  # the highlight starts on Trace
    assert screen._running
    await screen._worker
    assert len(screen._traces) == 1


def test_reverse_is_a_path_mode_only_action() -> None:
    """A target route is a symmetric boomerang, so only path mode offers Reverse.

    In path mode the row is in the build-path group, between Compose and the trace settings.
    Thus the whole group "define the path" is together.
    """
    target, _ = _trace_screen(mode="target")
    assert "reverse" not in target._actions
    assert "Reverse path" not in _plain(target.render_body(100))

    walk, _ = _trace_screen(mode="path")
    assert "reverse" in walk._actions
    body = _plain(walk.render_body(100))
    assert body.index("Compose path") < body.index("Reverse path") < body.index("Path width")


async def test_reverse_flips_a_path_walk_and_restarts_the_run() -> None:
    """Reverse flips the hop order and clears the forward run, as each path change does."""
    screen, _ = _trace_screen(mode="path")
    screen._path_spec = "3d,f2,27"
    screen.start_trace()
    await screen._worker
    assert screen._traces  # a forward reading exists
    screen._index = screen._actions.index("reverse")
    screen.handle("enter")
    assert screen._path_spec == "27,f2,3d"  # from end to end
    assert screen._traces == []  # the forward aggregates are cleared
    assert screen._total_traces == 1  # the session count stays after the restart
    assert "→ 27 → f2 → 3d →" in screen._planned_route().text().plain


def test_reverse_adopts_and_flips_the_visible_auto_walk() -> None:
    """If only the auto walk shows, Reverse pins its mirror as the path to walk."""
    screen, _ = _trace_screen(mode="path", auto_spec=lambda: "3d,f2", auto_source="last walk")
    assert screen._path_spec == ""  # the user composed nothing yet, so the plan is auto
    screen._index = screen._actions.index("reverse")
    screen.handle("enter")
    assert screen._path_spec == "f2,3d"  # the auto walk that shows, flipped and pinned
    assert "→ f2 → 3d →" in screen._planned_route().text().plain


def test_reverse_is_inert_without_a_reversible_path() -> None:
    """If there is no path (or only one hop, which is its own mirror), there is nothing to flip."""
    screen, _ = _trace_screen(mode="path")  # no spec, no auto walk
    assert "Reverse path — compose a path first" in _plain(screen.render_body(100))
    screen._index = screen._actions.index("reverse")
    screen.handle("enter")
    assert screen._path_spec == ""
    screen._path_spec = "3d"  # one hop reverses to itself
    screen.handle("enter")
    assert screen._path_spec == "3d"


async def test_adopting_a_new_path_restarts_the_measurement() -> None:
    """A different spec clears the aggregates and the log, as a screen that was just opened.

    The old numbers described the old route. Only the total for the life of the screen
    survives the reset. The owner reports this total as the trace count of the session.
    """

    async def compose(current: str):  # noqa: ANN001
        return "3d,f2,3d"

    screen, _ = _trace_screen(compose_path=compose)
    screen.start_trace()
    await screen._worker
    assert "Traces" in _plain(screen.render_body(100))
    for _ in range(4):
        screen.handle("up")  # Trace → Sample count → Path width → Explore → Compose
    screen.handle("enter")
    await asyncio.sleep(0)
    assert screen._path_spec == "3d,f2,3d"
    assert screen._traces == []
    body = _plain(screen.render_body(100))
    assert "Traces" not in body and "Per-hop medians" not in body
    assert screen._total_traces == 1  # the session count is not written again


async def test_keeping_the_same_path_keeps_the_stats() -> None:
    """Flows that resolve to None or to the unchanged spec never change the collected run."""

    async def keep(current: str):  # noqa: ANN001
        return current  # for example, the width dialog that renders again to an identical spec

    screen, _ = _trace_screen(compose_path=keep)
    screen._path_spec = "3d,f2,3d"
    screen.start_trace()
    await screen._worker
    for _ in range(4):
        screen.handle("up")  # Trace → Sample count → Path width → Explore → Compose
    screen.handle("enter")
    await asyncio.sleep(0)
    assert screen._path_spec == "3d,f2,3d"
    assert len(screen._traces) == 1


async def test_trace_screen_opens_idle_until_enter() -> None:
    """If the user selects a target, the screen must not transmit by itself until Enter."""
    screen, session = _trace_screen()
    assert not screen._running and screen._worker is None
    assert "press Enter to trace" in _plain(screen.render_body(100))
    screen.handle("enter")
    assert screen._running
    await screen._worker
    assert session.stack == []  # the tracing dialog was popped when the trace ended


async def test_trace_screen_floats_the_tracing_dialog() -> None:
    """A trace pushes the dialog that can abort it for the time of the trace.

    It pops the dialog when the trace ends, in each case.
    """
    release = asyncio.Event()

    async def trace(path_spec, on_trace):  # noqa: ANN001
        on_trace(_trace(5.0))
        await release.wait()

    screen, session = _trace_screen(trace=trace)
    screen.start_trace()
    await asyncio.sleep(0)
    assert len(session.stack) == 1
    dialog = session.stack[0]
    assert dialog.last is not None  # the reply that arrived shows on the dialog
    body = _plain(dialog.render_body(60))
    assert "Abort" in body
    release.set()
    await screen._worker
    assert session.stack == []


async def test_tracing_dialog_abort_cancels_the_trace() -> None:
    """Enter or Esc on the tracing dialog cancels the trace in progress, through the screen."""
    release = asyncio.Event()

    async def trace(path_spec, on_trace):  # noqa: ANN001
        await release.wait()

    screen, session = _trace_screen(trace=trace)
    screen.start_trace()
    await asyncio.sleep(0)
    session.stack[0].handle("escape")
    with pytest.raises(asyncio.CancelledError):
        await screen._worker
    assert session.stack == []
    assert not screen._running


async def test_trace_screen_escape_cancels_the_inflight_trace() -> None:
    """Esc resolves the screen and cancels a trace that is still measuring."""
    release = asyncio.Event()

    async def trace(path_spec, on_trace):  # noqa: ANN001
        await release.wait()

    screen, _ = _trace_screen(trace=trace)
    screen.start_trace()
    screen.future = asyncio.get_running_loop().create_future()
    screen.handle("escape")
    assert screen.future.result() is None
    with pytest.raises(asyncio.CancelledError):
        await screen._worker


# --- TxSweepScreen -----------------------------------------------------------------


def _level(tx: int, snr: float | None, successes: int = 2, samples: int = 2) -> TxLevelResult:
    return TxLevelResult(
        tx_power=tx,
        samples=samples,
        successes=successes,
        target_snr=snr,
        score=snr if snr is not None else float("-inf"),
        stats=TraceStats.from_traces("Alice", []),
    )


def _sweep_screen(apply_winner=None, run_sweep=None) -> tuple[TxSweepScreen, _FakeSession]:
    session = _FakeSession()
    screen = TxSweepScreen(
        admin_label="Repeater",
        target_label="Alice",
        device_label="us",
        device_hash="00" * 32,
        resolve=lambda h: h,
        session=session,
        tx_min=12,
        tx_max=28,
        step=3,
        samples=3,
        run_sweep=run_sweep or (lambda: None),
        apply_winner=apply_winner or (lambda: None),
    )
    return screen, session


def _result(best_tx: int = 19, best_snr: float | None = 8.8) -> TxOptResult:
    return TxOptResult(
        target="Alice",
        admin_node="Repeater",
        path="a1b2,d4e5",
        original_tx=20,
        best_tx=best_tx,
        best_snr=best_snr,
        best_success_rate=1.0,
        applied=False,
        levels=[_level(best_tx, best_snr)],
    )


def test_sweep_screen_opens_armed_and_idle_on_sweep() -> None:
    """The screen opens with the highlight on Sweep, and nothing is transmitted."""
    ran: list[bool] = []
    screen, _ = _sweep_screen(run_sweep=lambda: ran.append(True))
    body = _plain(screen.render_body(100))
    assert "Sweep — up to" in body and "Route — direct to Repeater" in body
    assert not screen.running and ran == []
    screen.handle("enter")  # the highlight starts on Sweep
    assert ran == [True]


def test_sweep_screen_quotes_the_transmission_budget() -> None:
    """The worst case on the Sweep row follows the window, the step, and the samples."""
    screen, _ = _sweep_screen()
    # 12–28 with step 3 gives 7 coarse levels (28 is added). Refine has a maximum of 4.
    # Verify is 1. The total is 12 × 3.
    assert screen.estimated_traces() == 36
    screen.samples = 1
    screen.step = 1
    # The sweep measures each level first (17), with no refine grid and one verify batch.
    assert screen.estimated_traces() == 18
    assert "up to 18 paced transmissions" in _plain(screen.render_body(100))


def test_sweep_screen_stars_the_running_best() -> None:
    """Each level that arrived renders in ascending order of TX, with a star on the current best."""
    screen, _ = _sweep_screen()
    screen.on_phase("coarse")
    screen.on_level(1, 7, _level(12, -2.0))
    screen.on_level(2, 7, _level(18, 7.5))
    assert screen.phase_label() == "coarse sweep · level 2/7"
    # The route lane has ★ for our ends, so the star that marks the winner is the one in the
    # levels table. That row also has the reading of the level.
    starred = next(
        line
        for line in _plain(screen.render_body(100)).splitlines()
        if "★" in line and "dB" in line
    )
    assert "18" in starred


def test_sweep_screen_shows_failed_levels_distinctly() -> None:
    """A level where nothing got through shows as a no-reply row, not as an empty bar."""
    screen, _ = _sweep_screen()
    screen.on_level(1, 7, _level(28, None, successes=0))
    assert "✗ no reply" in _plain(screen.render_body(100))


def test_sweep_screen_completion_offers_the_winner() -> None:
    """When the sweep completes, the highlight goes to the new Apply row. Enter offers it again."""
    offered: list[bool] = []
    screen, _ = _sweep_screen(apply_winner=lambda: offered.append(True))
    assert "apply" not in screen._actions  # nothing to apply while the sweep measures
    screen.complete(_result())
    body = _plain(screen.render_body(100))
    assert "best" in body and "TX 19" in body
    assert "Apply winner — set TX 19 on Repeater" in body
    screen.handle("enter")  # the highlight moved to Apply by itself
    assert offered == [True]


def test_sweep_screen_apply_updates_status_and_retires_the_row() -> None:
    """If the winner is marked as applied, the status line changes and the Apply row goes."""
    offered: list[bool] = []
    screen, _ = _sweep_screen(apply_winner=lambda: offered.append(True))
    screen.complete(_result())
    screen.mark_applied()
    body = _plain(screen.render_body(100))
    assert "✓ TX 19 set on Repeater" in body
    assert "Apply winner" not in body
    screen.handle("enter")
    assert offered == []


def test_sweep_screen_no_result_never_offers_apply() -> None:
    """A sweep where nothing got through shows a warning and never adds an Apply row."""
    screen, _ = _sweep_screen()
    screen.complete(_result(best_snr=None))
    body = _plain(screen.render_body(100))
    assert "no traces reached" in body
    assert "Apply winner" not in body


def test_sweep_screen_failure_keeps_measured_levels_on_screen() -> None:
    """An error in the middle of a sweep is reported. The levels that were measured stay visible."""
    screen, _ = _sweep_screen()
    screen.on_level(1, 7, _level(12, -2.0))
    screen.fail("link lost")
    body = _plain(screen.render_body(100))
    assert "sweep failed: link lost" in body
    assert "-2.0" in body


def test_sweep_screen_new_sweep_clears_the_old_evidence() -> None:
    """Another sweep is a new measurement, so the chart and the outcome reset."""
    screen, _ = _sweep_screen()
    screen.on_level(1, 7, _level(12, -2.0))
    screen.complete(_result())

    async def go() -> None:
        task = asyncio.ensure_future(asyncio.sleep(0))
        screen.sweep_started(task)
        assert screen.running
        body = _plain(screen.render_body(100))
        assert "TX 19" not in body and "-2.0" not in body
        await task

    asyncio.run(go())


def test_sweep_screen_escape_resolves() -> None:
    """Esc resolves the screen (the controller then cancels the sweep and restores the setting)."""
    screen, _ = _sweep_screen()

    class _Fut:
        def __init__(self) -> None:
            self.value = None
            self._done = False

        def done(self) -> bool:
            return self._done

        def set_result(self, value) -> None:  # noqa: ANN001
            self.value = value
            self._done = True

    screen.future = _Fut()
    screen.handle("escape")
    assert screen.future.done()


def test_parse_tx_range_clamps_and_rejects() -> None:
    """The typed window accepts two numbers in order, and clamps to the remote range."""
    from meshterm.ui.tx_screen import parse_tx_range

    assert parse_tx_range("14-24") == (14, 24)
    assert parse_tx_range("14 24") == (14, 24)
    assert parse_tx_range("2-99") == (12, 28)  # it clamps to the firmware window
    assert parse_tx_range("24-14") is None
    assert parse_tx_range("banana") is None


# --- the scenario rows of explore paths --------------------------------------------

_US = "aaaaaaaaaaaa"
_HUB = Contact(name="Hub", public_key="3d63c6429436" + "0" * 52, key_prefix="3d63c6429436")
_FAR = Contact(name="Far", public_key="f2c24f54551e" + "0" * 52, key_prefix="f2c24f54551e")


def _scenario_topo() -> object:
    return build_topology(
        self_id=_US + "0" * 52,
        contacts=[_HUB, _FAR],
        trace_paths=[],
        packet_paths=[],
        neighbour_links=[],
    )


def test_scenario_path_leads_and_ends_with_us_and_the_target() -> None:
    """A scenario path starts with us and ends with the target.

    The path line draws the whole boomerang leg, not only the stored hops between. Our end
    is bare, because each candidate starts from us.
    """
    topo = _scenario_topo()
    scenario = PathScenario(
        label="observed path",
        hops=("3d63c6429436",),
        source="observed",
        score=1.0,
    )
    text = _scenario_path(scenario, topo, "f2c24f54551e", device_label="Hub-Me", width_bytes=1)
    assert text.plain == "★ → Hub → Far"


def test_scenario_path_direct_scenario_still_names_both_endpoints() -> None:
    """The path line of a direct scenario (no repeaters) is only our star and the target."""
    topo = _scenario_topo()
    scenario = PathScenario(label="direct", hops=(), source="direct", score=0.0)
    text = _scenario_path(scenario, topo, "f2c24f54551e", device_label="Hub-Me", width_bytes=1)
    assert text.plain == "★ → Far"


def test_scenario_path_cuts_a_long_candidate_rather_than_eliding_its_middle() -> None:
    """Each row is cut as the highlighted row is cut at shift zero. It does not use ``⋯``.

    The elision in the middle exists to keep the two ends of a route. Here both ends are the
    same on each row by design (our ``★``, and the one target that the screen is about). If
    the elision ran, it used cells for what the user already knows, and it took them from the
    front of the candidates, which is the only part that is different. A cut also stops the
    row from changing when the highlight goes to it (JP, 2026-08-10).
    """
    topo = _scenario_topo()
    scenario = PathScenario(
        label="observed path",
        hops=("3d63c6429436",),
        source="observed",
        score=1.0,
    )
    full = _scenario_path(scenario, topo, "f2c24f54551e", device_label="Hub-Me", width_bytes=1)
    narrow = _scenario_path(
        scenario,
        topo,
        "f2c24f54551e",
        device_label="Hub-Me",
        width_bytes=1,
        width=full.cell_len - 3,
    )
    assert narrow.cell_len <= full.cell_len - 3
    assert "⋯" not in narrow.plain  # nothing is elided from the middle…
    assert narrow.plain.startswith("★ → Hub")  # …the head is whole, and the tail is cut


def test_scenario_detail_leads_with_the_hop_count() -> None:
    """The stats line under a candidate starts with the length of the route."""
    scenario = PathScenario(
        label="observed path",
        hops=("3d63c6429436", "f2c24f54551e"),
        source="observed",
        score=1.0,
        weakest_snr=-4.0,
        samples=2,
    )
    assert _scenario_detail(scenario).plain.startswith("2 hops  ·  ")
    # The source tag of the direct shot is this same word, so the atom takes it in. Thus the
    # row does not show "direct  ·  direct".
    direct = PathScenario(label="direct", hops=(), source="direct", score=0.0, samples=1)
    assert direct.label == "direct" and _scenario_detail(direct).plain.count("direct") == 1


def test_scenario_detail_carries_provenance_snr_and_samples() -> None:
    """The source tag (device or direct), the weakest SNR, and the sample count show, in order."""
    scenario = PathScenario(
        label="device route",
        hops=("3d63c6429436",),
        source="device",
        score=2.0,
        weakest_snr=-6.0,
        samples=3,
    )
    detail = _scenario_detail(scenario)
    assert "device route" in detail.plain
    assert "weakest -6.0 dB" in detail.plain
    assert "3×" in detail.plain


def test_scenario_detail_observed_carries_no_provenance_tag() -> None:
    """The detail line of an observed candidate has no tag, because the route is the point."""
    scenario = PathScenario(
        label="observed path",
        hops=("3d63c6429436",),
        source="observed",
        score=1.0,
        weakest_snr=-4.0,
        samples=2,
    )
    detail = _scenario_detail(scenario)
    assert "observed" not in detail.plain
    assert "weakest -4.0 dB" in detail.plain


def test_scenario_detail_unscored_scenario_reads_unobserved() -> None:
    """A scenario with no evidence (score 0, no samples) shows as unobserved."""
    scenario = PathScenario(label="direct", hops=(), source="direct", score=0.0)
    detail = _scenario_detail(scenario)
    assert "direct" in detail.plain
    assert "unobserved" in detail.plain
