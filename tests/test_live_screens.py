# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the live Trace and TX-optimize screens.

These drive the two full-screen tools' pure logic — trace/sweep state machines, key
handling, and rendering — against fake sessions and injected runners, so they run fast
and headless (the same approach as ``test_tui``).
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
from tests.conftest import plain as _plain  # THE strip-and-join screen reader

# The slim meter's fill glyphs (the SNR bar draws through the shared braille meter).
_BAR_FULL, _BAR_HALF = _METER_SLIM


class _FakeSession:
    """The session capabilities the screens use directly: repaints and the dialog stack."""

    def __init__(self) -> None:
        self.repaints = 0
        self.stack: list = []
        #: Every button_dialog floated, as ``(prompt, buttons, kwargs)``; the next
        #: one resolves with ``dialog_answer`` (``None`` = the Esc/idle-cancel path).
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
        """Start a key handler's flow as a task, as ``TuiSession.run_detached`` does."""
        return asyncio.ensure_future(work)


def _trace(*snrs: float, success: bool = True, target: str = "Alice") -> TraceResult:
    """Build a trace with one hop per SNR reading."""
    hops = [Hop(index=i, node=f"{i:02x}{i:02x}", snr=snr) for i, snr in enumerate(snrs)]
    return TraceResult(
        target=target, success=success, hops=hops, round_trip_ms=200.0, path_hash_bytes=2
    )


# --- snr_bar -------------------------------------------------------------------


def _lit_plain(bar) -> str:  # noqa: ANN001
    """The reading's own coloured prefix, stripped of the dimmed unlit track.

    The unlit remainder reuses :data:`_BAR_FULL` too (dimmed to the ``track`` style
    instead of a distinct glyph), so telling lit from unlit means reading which
    style each character actually landed in, not just which glyph it is.
    """
    if bar.style == "track":
        return ""
    track_spans = [s for s in bar.spans if s.style == "track"]
    end = min((s.start for s in track_spans), default=len(bar.plain))
    return bar.plain[:end]


def _filled_steps(bar) -> int:  # noqa: ANN001
    """Count a rendered bar's fill steps — two per full braille cell, one per half."""
    plain = _lit_plain(bar)
    return plain.count(_BAR_FULL) * 2 + plain.count(_BAR_HALF)


def test_snr_bar_scales_with_signal_quality() -> None:
    """A stronger signal fills more of the track; None renders an entirely unlit one."""
    weak = snr_bar(-12.0)
    strong = snr_bar(8.0)
    assert _filled_steps(weak) < _filled_steps(strong)
    assert len(weak.plain) == len(strong.plain) == _BAR_WIDTH  # track width is constant
    none_bar = snr_bar(None)
    assert _filled_steps(none_bar) == 0
    assert none_bar.style == "track"  # the whole track dims, not a separate faint dot run


def test_snr_bar_clamps_out_of_range_readings() -> None:
    """Readings beyond the display range clamp to the ends instead of over/underflowing."""
    assert _filled_steps(snr_bar(99.0)) == _filled_steps(snr_bar(10.0))
    assert _filled_steps(snr_bar(-99.0)) == 1  # a heard hop always shows something


def test_snr_bar_packs_two_steps_per_character() -> None:
    """16 steps of resolution pack into 8 characters: full cells, then one trailing half."""
    one_step = snr_bar(-13.4375)  # frac = 1/16 of the -15..+10 span
    assert one_step.plain[0] == _BAR_HALF
    assert one_step.plain[1:] == _BAR_FULL * (_BAR_WIDTH - 1)  # unlit track, same glyph
    track_spans = [(s.start, s.end) for s in one_step.spans if s.style == "track"]
    assert track_spans == [(1, _BAR_WIDTH)]

    three_steps = snr_bar(-10.3125)  # frac = 3/16 → one full cell, one half
    assert three_steps.plain[:2] == _BAR_FULL + _BAR_HALF
    assert three_steps.plain[2:] == _BAR_FULL * (_BAR_WIDTH - 2)


def test_snr_bar_unlit_track_dims_to_a_distinct_style() -> None:
    """The unlit track renders in ``track``, not the reading's colour or old ``faint`` dots."""
    bar = snr_bar(-10.3125)
    assert "·" not in bar.plain  # no more plain-dot placeholder
    track_span = next(s for s in bar.spans if s.style == "track")
    assert bar.plain[track_span.start : track_span.end] == _BAR_FULL * (_BAR_WIDTH - 2)
    assert bar.style == snr_style(-10.3125)  # the lit prefix still carries the reading's colour

    assert snr_bar(10.0).plain == _BAR_FULL * _BAR_WIDTH  # top of range: every cell full


# --- _previous_outbound -----------------------------------------------------------


def _walk(*nodes: str, success: bool = True) -> TraceResult:
    """A stored walk whose hop hashes are ``nodes`` (plus the final hash-less us)."""
    hops = [Hop(index=i, node=n, snr=1.0) for i, n in enumerate(nodes)]
    hops.append(Hop(index=len(nodes), node=None, snr=1.0))
    return TraceResult(
        target="Alice", success=success, hops=hops, round_trip_ms=200.0, path_hash_bytes=2
    )


def test_previous_outbound_extracts_the_proven_route() -> None:
    """The last successful boomerang's first half is the reusable outbound leg.

    Verified on hardware that the device itself almost never has a learned route
    (contacts report flood), so this stored evidence is what auto mode actually
    walks for a multi-hop target.
    """
    from meshterm.ui.trace_screen import _previous_outbound

    walk = _walk("3d63", "f2c2", "aabb", "f2c2", "3d63")
    assert _previous_outbound(walk, "aabb" + "00" * 30) == ("3d63", "f2c2")


def test_previous_outbound_direct_walk_yields_no_repeaters() -> None:
    """A direct answer (target only) extracts an empty outbound leg — dest-only again."""
    from meshterm.ui.trace_screen import _previous_outbound

    assert _previous_outbound(_walk("aabb"), "aabb" + "00" * 30) == ()


def test_previous_outbound_rejects_unusable_history() -> None:
    """Failures, asymmetric walks, and walks that turned elsewhere are never reused."""
    from meshterm.ui.trace_screen import _previous_outbound

    target = "aabb" + "00" * 30
    assert _previous_outbound(None, target) is None
    assert _previous_outbound(_walk("3d63", "aabb", "3d63", success=False), target) is None
    # Asymmetric: came home a different way — not a boomerang to this target.
    assert _previous_outbound(_walk("3d63", "aabb", "f2c2"), target) is None
    # Palindromic, but it turned at some other node, not our target.
    assert _previous_outbound(_walk("3d63", "9999", "3d63"), target) is None


# --- _previous_walk ---------------------------------------------------------------


def test_previous_walk_returns_the_whole_proven_route() -> None:
    """The last successful path walk comes back verbatim — it's a whole spec already.

    Unlike a target-mode boomerang there is no shape to recognise: whatever route
    the mesh carried end to end is the route to offer again, hop hashes at the
    width they were transmitted.
    """
    from meshterm.ui.trace_screen import _previous_walk

    walk = _walk("3d63", "f2c2", "27ab")
    assert _previous_walk(walk) == ("3d63", "f2c2", "27ab")


def test_previous_walk_rejects_unusable_history() -> None:
    """No history, a failed walk, or one with no addressable hops is never reused."""
    from meshterm.ui.trace_screen import _previous_walk

    assert _previous_walk(None) is None
    assert _previous_walk(_walk("3d63", success=False)) is None
    assert _previous_walk(_walk()) is None  # only the hash-less final hop (us)


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
        pace_s=0.0,  # tests never sleep; pacing is asserted through the statuses
        previous=previous,
        auto_spec=auto_spec or (lambda: ""),
        auto_source=auto_source,
        open_trophy_case=open_trophy_case,
    )
    screen.note_viewport(40)  # the frame records this before every real paint
    return screen, session


async def test_trace_screen_one_trace_per_enter_accumulates() -> None:
    """At the default sample count, each Enter transmits exactly one trace.

    Repeat sampling stays a human decision unless a bigger sample count is chosen
    explicitly, so three keypresses mean three traces and a three-sample median.
    """
    screen, _ = _trace_screen()
    for _ in range(3):
        screen.start_trace()
        await screen._worker
    body = _plain(screen.render_body(100))
    assert "success rate" in body and "3/3" in body
    assert "#3" in body  # newest-first numbering
    assert "Per-hop medians" in body
    assert "burst" not in body.lower()  # no burst configuration is offered anywhere


async def test_trace_screen_runs_the_chosen_sample_count() -> None:
    """One Enter runs the whole chosen sample count, every trace recorded."""
    ran: list[str] = []

    async def trace(path_spec, on_trace):  # noqa: ANN001
        ran.append(path_spec)
        on_trace(_trace(5.0))

    screen, session = _trace_screen(trace=trace, samples=3)
    screen.start_trace()
    await screen._worker
    assert len(ran) == 3
    assert len(screen._traces) == 3
    assert session.stack == []  # the dialog was popped with the run


async def test_trace_screen_multi_trace_reports_progress_and_abort_keeps_landed() -> None:
    """A multi-trace run counts itself off; aborting keeps what already landed."""
    release = asyncio.Event()
    ran = 0

    async def trace(path_spec, on_trace):  # noqa: ANN001
        nonlocal ran
        ran += 1
        on_trace(_trace(5.0))
        if ran == 2:
            await release.wait()  # hold the run mid-flight on the second trace

    screen, session = _trace_screen(trace=trace, samples=5)
    screen.start_trace()
    await asyncio.sleep(0)
    dialog = session.stack[0]
    assert "2/5" in dialog.status  # the dialog counts the run off
    assert "2/5" in screen.footer_hint
    assert "2/5" in _plain(screen.render_body(100))  # the log spinner row too
    screen.cancel()
    with pytest.raises(asyncio.CancelledError):
        await screen._worker
    assert len(screen._traces) == 2  # already-recorded traces are kept
    assert session.stack == []


async def test_trace_screen_seeds_route_from_previous_trace() -> None:
    """Before any fresh reply, the stored route shows, marked as previous."""
    old = _trace(4.0)
    old.timestamp = utcnow() - timedelta(hours=3)
    screen, _ = _trace_screen(previous=old)
    body = _plain(screen.render_body(100))
    assert "(previous" in body
    # A fresh success replaces the seeded route and drops the marker.
    screen._on_trace(_trace(6.0))
    body = _plain(screen.render_body(100))
    assert "(previous" not in body


async def test_previous_stamp_sits_on_its_own_line() -> None:
    """The (previous · …) marker renders under the route, never squeezed beside it."""
    old = _trace(4.0)
    old.timestamp = utcnow() - timedelta(hours=3)
    screen, _ = _trace_screen(previous=old)
    stamp_line = next(ln for ln in screen.render_body(100) if "(previous" in ln)
    assert "→" not in stamp_line  # the route stays on the line above


async def test_trace_screen_only_one_trace_at_a_time() -> None:
    """Enter during an in-flight trace is a no-op; the running flag gates re-entry."""
    started = 0
    release = asyncio.Event()

    async def trace(path_spec, on_trace):  # noqa: ANN001
        nonlocal started
        started += 1
        await release.wait()

    screen, _ = _trace_screen(trace=trace)
    screen.start_trace()
    assert screen._running
    screen.handle("enter")  # ignored while running
    release.set()
    await screen._worker
    assert started == 1
    assert not screen._running


async def test_trace_screen_failure_reads_inline() -> None:
    """A failed trace reports its error in the log area instead of crashing the screen."""

    async def trace(path_spec, on_trace):  # noqa: ANN001
        raise RuntimeError("no route")

    screen, _ = _trace_screen(trace=trace)
    screen.start_trace()
    await screen._worker
    assert "trace failed: no route" in _plain(screen.render_body(100))


async def test_trace_screen_composer_updates_the_spec() -> None:
    """Committing Compose path runs the flow; its result becomes the next trace's path."""
    asked: list[str] = []

    async def compose(current: str):  # noqa: ANN001
        asked.append(current)
        return "3d,f2,3d"  # one forced hop: outbound, target, then the mirrored return

    screen, _ = _trace_screen(compose_path=compose)
    for _ in range(len(screen._actions) - 1):
        screen.handle("up")  # the cursor opens on Trace, the last row; walk it up to Compose
    screen.handle("enter")
    await asyncio.sleep(0)
    assert asked == [""]
    assert screen._path_spec == "3d,f2,3d"
    body = _plain(screen.render_body(100))
    assert "3d,f2,3d" in body
    # the planned route previews outbound *and* the resolved, dimmed return leg,
    # our own ends spending no words — just the star that stands for us
    assert "route  ★ → 3d → f2 → 3d → ★" in body
    assert body.count("3d") >= 2


async def test_compose_seeds_from_the_visible_route_not_the_empty_spec() -> None:
    """Opening Compose resumes from the route on screen, not the stored spec.

    On first open nothing has been composed, but the screen already shows the
    auto-resolved plan; the composer must open seeded with that plan's hops so the
    user edits the route they see rather than starting from a blank path.
    """
    asked: list[str] = []

    async def compose(current: str):  # noqa: ANN001
        asked.append(current)
        return None  # observe the seed only; leave the (auto) plan untouched

    screen, _ = _trace_screen(
        mode="path",
        compose_path=compose,
        auto_spec=lambda: "3d,f2",
        auto_source="last walk",
    )
    screen._index = screen._actions.index("compose")
    screen.handle("enter")
    await asyncio.sleep(0)
    assert asked == ["3d,f2"]  # seeded from the visible auto plan, not ""
    assert screen._path_spec == ""  # None keeps the plan auto (nothing pinned)


async def test_trace_screen_explore_adopts_a_scenario_path() -> None:
    """Committing Explore paths runs the flow; adopting sets the spec, None keeps it."""

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
    screen.handle("enter")  # the cursor is still on Explore paths
    await asyncio.sleep(0)
    assert screen._path_spec == "3d63,f2c2"  # None leaves the spec untouched


async def test_trace_screen_action_labels_share_one_column() -> None:
    """Every action's label starts in the same column, wide mark or narrow.

    ``⚡`` is an emoji — two cells where ``✎``/``⚙``/``#``/``▶`` are one — so a fixed
    ``"icon "`` prefix would start Explore's label a column right of the rest.
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
    """The display cell an action row's words begin in — past its pointer and its mark.

    Measured in *cells* over the text before the first letter or digit, never as a
    character index: ``⚡`` is one character drawn in two cells, so a character count
    would call a misaligned row aligned (and ``#``, the sample-count mark, is no letter).
    """
    return cell_len(row[: next(i for i, ch in enumerate(row) if ch.isalnum())])


@pytest.mark.parametrize("platform_name", ["regular", "picocalc-lyra"])
def test_every_action_row_starts_its_words_in_one_cell_in_both_modes(platform_name: str) -> None:
    """Every action the screen can draw, in either mode, lit or not, shares one word column.

    The rendered test above reads target mode only, and only the rows it names — so the
    path walk's ``⇄ Reverse path`` row, which exists nowhere else, was never measured. This
    asks the screen for its own action list instead, which also pins the other half of the
    contract: the one column holds *across* modes. ``_ACTION_ICONS`` declares ``⚡`` even in
    path mode, where no row draws it, so toggling a walk into a target (or back) never nudges
    the labels sideways under the reader.

    The column is measured from the marks the rows actually draw, and every one of those
    must be in ``_ACTION_ICONS`` — a new action with a mark the tuple doesn't know about is
    how a two-cell icon lands in a one-cell column. On the PicoCalc the lane is dropped
    whole, so every word starts straight after the pointer.
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
                if platform is REGULAR:  # the mark sits between the pointer and its gap
                    marks.add(row[2:].split(" ", 1)[0])
    pointer = cell_len("❯ ")
    if platform is REGULAR:
        # Declared-ness first: an undeclared wide mark eats its own gap ("⚡Explore"), and
        # that is the diagnosis to read — not the width premise it would also break.
        assert marks == set(_ACTION_ICONS), "every drawn mark is declared, and nothing else"
        assert {cell_len(mark) for mark in marks} == {1, 2}, "a list of one width proves nothing"
        assert starts == {pointer + 2 + 1}, starts  # the widest mark, then its space
    else:
        assert starts == {pointer}, starts  # no icon lane, no padding left behind


async def test_trace_screen_action_cursor_commits_the_selected_row() -> None:
    """↑↓ move over the action rows; Enter commits the one under the cursor.

    The rows are the screen's own verbs and nothing else — no exit row closes them, so
    Trace is the last stop and ↓ from it wraps straight back to Compose path.
    """
    opened: list[str] = []

    async def width_flow(current):  # noqa: ANN001
        opened.append(f"width:{current}")
        return "3d63,f2c2,3d63"

    screen, _ = _trace_screen(pick_width=width_flow)
    body = _plain(screen.render_body(100))
    # The menu order the actions read in: build first, tune, then transmit.
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
    """Committing Sample count floats the flow; its None resolution keeps the spec."""
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
    assert screen._path_spec == "3d,f2,3d"  # the count is not a spec: nothing changes


async def test_trace_screen_hotkeys_are_retired() -> None:
    """The old w/p/x shortcuts are gone: typing must not float any flow."""
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
    """The screen offers only its own verbs; Esc is the way out, and Enter never is."""
    screen, _ = _trace_screen()
    screen.future = asyncio.get_running_loop().create_future()
    assert "Back" not in _plain(screen.render_body(100))
    screen.handle("down")  # Trace wraps to Compose path, not onto an exit row
    assert not screen.future.done()
    screen.handle("escape")
    assert screen.future.result() is None


async def test_composer_commit_parks_the_cursor_on_trace() -> None:
    """Committing the composer hands the cursor to Trace, so plain Enter walks it."""

    async def compose(current):  # noqa: ANN001
        return "3d,f2,3d"

    screen, _ = _trace_screen(compose_path=compose)
    screen._index = screen._actions.index("compose")
    screen.handle("enter")
    await asyncio.sleep(0)
    assert screen._actions[screen._index] == "trace"

    # The hand-back is the composer's alone: the width flow keeps the cursor put.
    async def width(current):  # noqa: ANN001
        return "3d63,f2c2,3d63"

    other, _ = _trace_screen(pick_width=width)
    other._index = other._actions.index("width")
    other.handle("enter")
    await asyncio.sleep(0)
    assert other._actions[other._index] == "width"


async def test_composer_escape_leaves_the_cursor_where_it_was() -> None:
    """Backing out of the composer with Esc moves nothing."""

    async def compose(current):  # noqa: ANN001
        return None  # the composer's Esc resolution

    screen, _ = _trace_screen(compose_path=compose)
    screen._index = screen._actions.index("compose")
    screen.handle("enter")
    await asyncio.sleep(0)
    assert screen._actions[screen._index] == "compose"


# --- the no-cheat rule on Trace ------------------------------------------------------


async def _settle(screen) -> None:  # noqa: ANN001
    """Let a just-committed dialog task run, then any trace worker it started."""
    for _ in range(5):
        await asyncio.sleep(0)
    if screen._worker is not None:
        await screen._worker


async def test_ineligible_walk_gates_trace_behind_an_amber_confirm() -> None:
    """A same-direction link recross floats Cancel/Trace and cancels cleanly."""
    ran: list[str] = []

    async def trace(path_spec, on_trace):  # noqa: ANN001
        ran.append(path_spec)
        on_trace(_trace(5.0))

    screen, session = _trace_screen(trace=trace, mode="path")
    screen._path_spec = "3d,f2,3d,f2"  # rides 3d → f2 twice the same way
    screen._index = screen._actions.index("trace")
    screen.handle("enter")  # dialog_answer None = Cancel/Esc
    await _settle(screen)
    prompt, buttons, kwargs = session.dialogs[0]
    assert [label for label, _ in buttons] == ["Cancel", "Trace"]
    assert kwargs["default"] == 1  # Trace on the right and default: Enter commits it
    assert kwargs["border_style"] == "warn"  # the amber caution tier
    assert "trail" in prompt.plain  # the graph-theory name reaches the user
    assert ran == []  # cancelled: nothing transmitted

    session.dialog_answer = "trace"
    screen.handle("enter")
    await _settle(screen)
    assert ran == ["3d,f2,3d,f2"]  # confirmed: the walk still flies


async def test_eligible_walk_traces_without_a_confirm() -> None:
    """An out-and-back walk recrosses links the other way only — no dialog, no nag."""
    screen, session = _trace_screen(mode="path")
    screen._path_spec = "3d,f2,3d"
    screen._index = screen._actions.index("trace")
    screen.handle("enter")
    await screen._worker
    assert session.dialogs == []
    assert len(screen._traces) == 1


# --- the new-record dialog -----------------------------------------------------------


async def test_record_run_floats_the_new_record_dialog() -> None:
    """A run that placed floats one dialog; Close (the default) keeps the screen up."""

    async def trace(path_spec, on_trace):  # noqa: ANN001
        on_trace(_trace(5.0), ["Most nodes — 4 nodes"])

    screen, session = _trace_screen(trace=trace)
    screen.future = asyncio.get_running_loop().create_future()
    screen.start_trace()
    await screen._worker
    await _settle(screen)  # the announcement task floats after the run settles
    prompt, buttons, kwargs = session.dialogs[0]
    assert "Most nodes — 4 nodes" in prompt.plain
    assert [label for label, _ in buttons] == ["Trophy case", "Close"]
    assert kwargs["default"] == 1  # Close is the default: Enter simply dismisses
    assert kwargs["title"] == "New record"
    assert not screen.future.done()  # Close/Esc: the screen stays up
    assert screen._run_placed == {}  # announced once, not again on the next run


async def test_record_dialog_trophy_case_opens_over_the_trace_it_was_earned_on() -> None:
    """Choosing Trophy case opens it *above* the trace screen, which stays up underneath.

    It used to resolve the whole screen with a sentinel so the trace unwound away first and
    the reader landed on the main menu. Navigation is a strict stack now: a sub-view nests,
    Esc from the trophy case is one pop back onto the trace that earned the record, and ^W is
    what leaves the whole excursion.
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
    assert len(session.dialogs) == 1  # however many samples scored
    assert opened == ["trophy case"]
    assert not screen.future.done(), "the trace screen stays up under the trophy case"


async def test_escaping_mid_run_skips_the_record_dialog() -> None:
    """Esc while records are banked leaves quietly — no popup chases the user out."""
    release = asyncio.Event()

    async def trace(path_spec, on_trace):  # noqa: ANN001
        on_trace(_trace(5.0), ["Most nodes — 4 nodes"])
        await release.wait()

    screen, session = _trace_screen(trace=trace, samples=2)
    screen.future = asyncio.get_running_loop().create_future()
    screen.start_trace()
    await asyncio.sleep(0)
    screen.handle("escape")  # resolves the screen and cancels the run
    with pytest.raises(asyncio.CancelledError):
        await screen._worker
    for _ in range(3):
        await asyncio.sleep(0)
    assert session.dialogs == []


def test_planned_route_dims_only_the_mirrored_return_leg() -> None:
    """A palindromic target-mode spec dims its second half; hand walks never dim.

    Both ends are ours, so both go bare: the line opens and closes on a lone arrow.
    """
    screen, _ = _trace_screen()

    def faint_cells(text) -> int:  # noqa: ANN001
        return sum(span.end - span.start for span in text.spans if "faint" in str(span.style))

    screen._path_spec = "3d,f2,3d"  # symmetric boomerang: the mirror is dimmed
    symmetric = screen._planned_route().text()
    assert symmetric.plain == "★ → 3d → f2 → 3d → ★"
    screen._path_spec = "3d,f2,27"  # a stale hand walk: every hop is the user's
    custom = screen._planned_route().text()
    assert custom.plain == "★ → 3d → f2 → 27 → ★"
    assert faint_cells(symmetric) > faint_cells(custom)

    # In path mode even a there-and-back-the-same-way walk is fully hand-composed,
    # so a palindrome must NOT read as "not yours to compose".
    walk, _ = _trace_screen(mode="path")
    walk._path_spec = "3d,f2,3d"
    assert faint_cells(walk._planned_route().text()) == faint_cells(custom)


def test_route_lane_wraps_at_hop_boundaries_under_its_own_column() -> None:
    """A route too wide for the lane breaks between hops, never inside one.

    Every continuation hangs under the value column (never back at column zero), the
    line it continues from ends on the ``→`` cue, and every name survives whole — the
    lane names nodes and leaves the hex to the ``path`` lane below it.
    """
    names = {"3d": "Hilltop-Repeater", "f2": "Mile-End-Rooftop", "27": "Beaubien-Sud"}
    screen, _ = _trace_screen(mode="path")
    screen._resolve = lambda h: names.get(h, h)
    screen._path_spec = "3d,f2,27"
    lines = [line.plain for line in screen._route_value(None, 60)]
    assert len(lines) > 1  # it really did wrap at this width
    assert all(len(line) <= 60 for line in lines)
    assert all(line.startswith(" " * 7) for line in lines[1:])  # hanging, not column 0
    assert all(line.rstrip().endswith("→") for line in lines[:-1])
    for hop, name in names.items():  # names whole, and no hash trailing any of them
        assert any(name in line for line in lines)
        assert all(f"({hop})" not in line for line in lines)


def test_path_lane_breaks_the_wire_spec_after_a_comma() -> None:
    """The spec lane stays the verbatim wire string, folding only at its commas."""
    screen, _ = _trace_screen(mode="path")
    screen._path_spec = "3d63ab99,7f21cd01,27aa1122,f2c20099"
    lines = [line.plain for line in screen._path_value(None, 44)]
    assert len(lines) > 1
    assert all(len(line) <= 44 for line in lines)
    assert lines[0].endswith(",")  # the comma is the cue, not an arrow
    spec = "".join(line.strip() for line in lines)
    assert spec.startswith(screen._path_spec)  # character for character, hop count after


def test_summary_appends_the_displayed_hop_count() -> None:
    """The path row ends with how many nodes the displayed route passes through."""
    screen, _ = _trace_screen()
    screen._path_spec = "3d,f2,3d"
    assert "· 3 hops" in _plain(screen.render_body(100))
    screen._on_trace(_trace(5.0, 2.0))  # a live 2-hop route now outranks the plan
    assert "· 2 hops" in _plain(screen.render_body(100))


def test_auto_resolved_route_renders_with_its_provenance() -> None:
    """With no composed path, the auto route shows as the plan, labelled with its source.

    What the screen draws is exactly what Trace will put on the air (both read the
    same resolver), so the user can see the forced boomerang — and where it came
    from — before committing a transmission.
    """
    screen, _ = _trace_screen(
        auto_spec=lambda: "3d63,f2c2,aabb,f2c2,3d63", auto_source="last trace · Jul 09 14:32"
    )
    plan = screen._planned_route().text().plain
    assert "→ 3d63 → f2c2 → aabb → f2c2 → 3d63 →" in plan
    # The provenance hangs on its own line under the route, not inside the path itself.
    route = "\n".join(line.plain for line in screen._route_value(None, 100))
    assert route.endswith("(auto · last trace · Jul 09 14:32)")
    body = _plain(screen.render_body(100))
    assert "auto · last trace · Jul 09 14:32" in body  # the summary's path row
    assert "· 5 hops" in body


def test_composed_path_outranks_the_auto_route() -> None:
    """A hand-composed spec replaces the auto plan everywhere — display and wire."""
    screen, _ = _trace_screen(auto_spec=lambda: "aabb", auto_source="device route")
    screen._path_spec = "3d63,aabb,3d63"
    plan = screen._planned_route().text().plain
    assert "→ 3d63 → aabb → 3d63 →" in plan
    assert "(auto ·" not in plan
    assert screen._effective_spec() == ("3d63,aabb,3d63", False)


def test_unaddressable_target_reads_as_path_less_auto() -> None:
    """With nothing to force (no target hash), the summary says so instead of lying."""
    screen, _ = _trace_screen()  # auto_spec resolves ""
    assert "auto — path-less (unknown target)" in _plain(screen.render_body(100))


def test_trace_log_section_hidden_until_there_is_something_to_log() -> None:
    """Idle with no traces, the Traces heading (and its old hint) don't render."""
    screen, _ = _trace_screen()
    assert "Traces" not in _plain(screen.render_body(100))


async def test_trace_screen_path_mode_gates_trace_and_drops_explore() -> None:
    """Path mode: no Explore row, and Trace stays inert until a path exists."""
    screen, _ = _trace_screen(mode="path")
    body = _plain(screen.render_body(100))
    assert "Explore paths" not in body
    assert "Trace — compose a path first" in body
    assert "none — compose a path" in body
    screen.handle("enter")  # the cursor opens on Trace, but there is nothing to walk
    assert not screen._running and screen._worker is None
    screen._path_spec = "3d,f2"
    assert "Trace — one transmission" in _plain(screen.render_body(100))
    screen.handle("enter")
    assert screen._running
    await screen._worker
    assert len(screen._traces) == 1


async def test_trace_screen_path_mode_arms_from_the_previous_walk() -> None:
    """The last stored walk isn't just displayed — it's the path Enter walks.

    Path mode's auto route (the previous successful walk) must arm Trace exactly
    like a composed spec, and read as the plan with its provenance, so the screen
    never shows a route it then refuses to trace.
    """
    screen, _ = _trace_screen(
        mode="path", auto_spec=lambda: "3d,f2", auto_source="last walk · Jul 09 14:32"
    )
    body = _plain(screen.render_body(100))
    assert "Trace — one transmission" in body  # armed, not "compose a path first"
    assert "auto · last walk · Jul 09 14:32" in body  # the summary's path row
    plan = screen._planned_route().text().plain
    assert "→ 3d → f2 →" in plan
    assert "(auto · last walk · Jul 09 14:32)" in body  # hanging under the route
    screen.handle("enter")  # the cursor opens on Trace
    assert screen._running
    await screen._worker
    assert len(screen._traces) == 1


def test_reverse_is_a_path_mode_only_action() -> None:
    """A target route is a symmetric boomerang, so only path mode offers Reverse.

    In path mode the row sits in the build-path group, between Compose and the
    trace-settings, so the whole "define the path" cluster reads together.
    """
    target, _ = _trace_screen(mode="target")
    assert "reverse" not in target._actions
    assert "Reverse path" not in _plain(target.render_body(100))

    walk, _ = _trace_screen(mode="path")
    assert "reverse" in walk._actions
    body = _plain(walk.render_body(100))
    assert body.index("Compose path") < body.index("Reverse path") < body.index("Path width")


async def test_reverse_flips_a_path_walk_and_restarts_the_run() -> None:
    """Reverse flips the hop order and clears the forward run, like any path change."""
    screen, _ = _trace_screen(mode="path")
    screen._path_spec = "3d,f2,27"
    screen.start_trace()
    await screen._worker
    assert screen._traces  # a forward reading stands
    screen._index = screen._actions.index("reverse")
    screen.handle("enter")
    assert screen._path_spec == "27,f2,3d"  # end-for-end
    assert screen._traces == []  # the forward aggregates cleared
    assert screen._total_traces == 1  # the session count survives the restart
    assert "→ 27 → f2 → 3d →" in screen._planned_route().text().plain


def test_reverse_adopts_and_flips_the_visible_auto_walk() -> None:
    """With only the auto walk showing, Reverse pins its mirror as the path to walk."""
    screen, _ = _trace_screen(mode="path", auto_spec=lambda: "3d,f2", auto_source="last walk")
    assert screen._path_spec == ""  # nothing composed yet; the plan is auto
    screen._index = screen._actions.index("reverse")
    screen.handle("enter")
    assert screen._path_spec == "f2,3d"  # the shown auto walk, flipped and pinned
    assert "→ f2 → 3d →" in screen._planned_route().text().plain


def test_reverse_is_inert_without_a_reversible_path() -> None:
    """No path (or a single hop, its own mirror) leaves nothing to flip."""
    screen, _ = _trace_screen(mode="path")  # no spec, no auto walk
    assert "Reverse path — compose a path first" in _plain(screen.render_body(100))
    screen._index = screen._actions.index("reverse")
    screen.handle("enter")
    assert screen._path_spec == ""
    screen._path_spec = "3d"  # a lone hop reverses to itself
    screen.handle("enter")
    assert screen._path_spec == "3d"


async def test_adopting_a_new_path_restarts_the_measurement() -> None:
    """A different spec clears the aggregates and log, like a freshly opened screen.

    The old numbers described the old route; only the screen-lifetime total (what
    the owner reports as the session's trace count) survives the reset.
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
    assert screen._total_traces == 1  # the session count is not rewritten


async def test_keeping_the_same_path_keeps_the_stats() -> None:
    """Flows that resolve None or the unchanged spec never touch the accumulated run."""

    async def keep(current: str):  # noqa: ANN001
        return current  # e.g. the width dialog re-rendering to an identical spec

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
    """Selecting a target must never transmit by itself: nothing flies until Enter."""
    screen, session = _trace_screen()
    assert not screen._running and screen._worker is None
    assert "press Enter to trace" in _plain(screen.render_body(100))
    screen.handle("enter")
    assert screen._running
    await screen._worker
    assert session.stack == []  # the tracing dialog was popped with the trace


async def test_trace_screen_floats_the_tracing_dialog() -> None:
    """A trace pushes the abortable dialog for its duration and pops it however it ends."""
    release = asyncio.Event()

    async def trace(path_spec, on_trace):  # noqa: ANN001
        on_trace(_trace(5.0))
        await release.wait()

    screen, session = _trace_screen(trace=trace)
    screen.start_trace()
    await asyncio.sleep(0)
    assert len(session.stack) == 1
    dialog = session.stack[0]
    assert dialog.last is not None  # the landed reply echoes on the dialog
    body = _plain(dialog.render_body(60))
    assert "Abort" in body
    release.set()
    await screen._worker
    assert session.stack == []


async def test_tracing_dialog_abort_cancels_the_trace() -> None:
    """Enter/Esc on the tracing dialog cancels the in-flight trace via the screen."""
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
    """The screen opens with the cursor on Sweep and nothing transmitted."""
    ran: list[bool] = []
    screen, _ = _sweep_screen(run_sweep=lambda: ran.append(True))
    body = _plain(screen.render_body(100))
    assert "Sweep — up to" in body and "Route — direct to Repeater" in body
    assert not screen.running and ran == []
    screen.handle("enter")  # the cursor opens on Sweep
    assert ran == [True]


def test_sweep_screen_quotes_the_transmission_budget() -> None:
    """The Sweep row's worst case tracks the window, step, and samples."""
    screen, _ = _sweep_screen()
    # 12–28 step 3 → 7 coarse levels (28 appended); refine ≤ 4; verify 1 → 12 × 3.
    assert screen.estimated_traces() == 36
    screen.samples = 1
    screen.step = 1
    # Every level measured up front (17), no refine grid, one verify batch.
    assert screen.estimated_traces() == 18
    assert "up to 18 paced transmissions" in _plain(screen.render_body(100))


def test_sweep_screen_stars_the_running_best() -> None:
    """Each landed level renders ascending by TX with the current best starred."""
    screen, _ = _sweep_screen()
    screen.on_phase("coarse")
    screen.on_level(1, 7, _level(12, -2.0))
    screen.on_level(2, 7, _level(18, 7.5))
    assert screen.phase_label() == "coarse sweep · level 2/7"
    # The route lane wears ★ for our own ends, so the star that marks the winner is the
    # one in the levels table — a row that also carries the level's reading.
    starred = next(
        line
        for line in _plain(screen.render_body(100)).splitlines()
        if "★" in line and "dB" in line
    )
    assert "18" in starred


def test_sweep_screen_shows_failed_levels_distinctly() -> None:
    """A level nothing got through at reads as a no-reply row, not an empty bar."""
    screen, _ = _sweep_screen()
    screen.on_level(1, 7, _level(28, None, successes=0))
    assert "✗ no reply" in _plain(screen.render_body(100))


def test_sweep_screen_completion_offers_the_winner() -> None:
    """Completion lands the cursor on the new Apply row; Enter re-offers the dialog."""
    offered: list[bool] = []
    screen, _ = _sweep_screen(apply_winner=lambda: offered.append(True))
    assert "apply" not in screen._actions  # nothing to apply while measuring
    screen.complete(_result())
    body = _plain(screen.render_body(100))
    assert "best" in body and "TX 19" in body
    assert "Apply winner — set TX 19 on Repeater" in body
    screen.handle("enter")  # the cursor parked itself on Apply
    assert offered == [True]


def test_sweep_screen_apply_updates_status_and_retires_the_row() -> None:
    """Marking the winner applied flips the status line and removes the Apply row."""
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
    """A sweep where nothing got through warns and never grows an Apply row."""
    screen, _ = _sweep_screen()
    screen.complete(_result(best_snr=None))
    body = _plain(screen.render_body(100))
    assert "no traces reached" in body
    assert "Apply winner" not in body


def test_sweep_screen_failure_keeps_measured_levels_on_screen() -> None:
    """A mid-sweep error is reported while the levels already measured stay visible."""
    screen, _ = _sweep_screen()
    screen.on_level(1, 7, _level(12, -2.0))
    screen.fail("link lost")
    body = _plain(screen.render_body(100))
    assert "sweep failed: link lost" in body
    assert "-2.0" in body


def test_sweep_screen_new_sweep_clears_the_old_evidence() -> None:
    """Starting another sweep is a new measurement: chart and outcome reset."""
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
    """Esc resolves the screen (the controller then cancels and restores)."""
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
    """The typed window accepts two ordered numbers and clamps to the remote range."""
    from meshterm.ui.tx_screen import parse_tx_range

    assert parse_tx_range("14-24") == (14, 24)
    assert parse_tx_range("14 24") == (14, 24)
    assert parse_tx_range("2-99") == (12, 28)  # clamped to the firmware window
    assert parse_tx_range("24-14") is None
    assert parse_tx_range("banana") is None


# --- explore-paths scenario rows --------------------------------------------------

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
    """A scenario path leads and ends with us and the target.

    The pathline draws the whole boomerang leg, not just the stored intermediate hops,
    with our own end bare since every candidate starts from us.
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
    """A direct (no-repeaters) scenario's pathline is just our star and the target."""
    topo = _scenario_topo()
    scenario = PathScenario(label="direct", hops=(), source="direct", score=0.0)
    text = _scenario_path(scenario, topo, "f2c24f54551e", device_label="Hub-Me", width_bytes=1)
    assert text.plain == "★ → Far"


def test_scenario_path_cuts_a_long_candidate_rather_than_eliding_its_middle() -> None:
    """Every row is cut the way the highlighted one is at shift zero — no ``⋯`` rescue.

    The middle-elide exists to save a route's two endpoints, and here both endpoints are
    the same two on every row by construction (our own ``★``, the one target the screen is
    about), so it would spend cells on what the reader knows and take them off the
    candidates' *front* — the only part that differs. Cutting also stops the row being
    redrawn the moment the cursor lands on it (JP, 2026-08-10).
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
    assert "⋯" not in narrow.plain  # nothing elided out of the middle…
    assert narrow.plain.startswith("★ → Hub")  # …the head is intact, the tail is what went


def test_scenario_detail_leads_with_the_hop_count() -> None:
    """The stats line under a candidate opens with how long the route is."""
    scenario = PathScenario(
        label="observed path",
        hops=("3d63c6429436", "f2c24f54551e"),
        source="observed",
        score=1.0,
        weakest_snr=-4.0,
        samples=2,
    )
    assert _scenario_detail(scenario).plain.startswith("2 hops  ·  ")
    # The direct shot's provenance tag *was* this same word, so the atom absorbs it rather
    # than the row reading "direct  ·  direct".
    direct = PathScenario(label="direct", hops=(), source="direct", score=0.0, samples=1)
    assert direct.label == "direct" and _scenario_detail(direct).plain.count("direct") == 1


def test_scenario_detail_carries_provenance_snr_and_samples() -> None:
    """The device/direct provenance tag, weakest SNR, and sample count all show, in order."""
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
    """An observed candidate's detail line skips the tag — the route itself is the point."""
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
    """A scenario with no evidence at all (score 0, no samples) reads plainly unobserved."""
    scenario = PathScenario(label="direct", hops=(), source="direct", score=0.0)
    detail = _scenario_detail(scenario)
    assert "direct" in detail.plain
    assert "unobserved" in detail.plain
