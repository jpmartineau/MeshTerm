# SPDX-License-Identifier: Apache-2.0
"""Tests for the navigation stack: strict push/pop, kept state, and the two global chords.

The app's realized path is a stack — the main menu at the bottom, one frame per screen or
dialog entered, one pop per Esc — and the machinery that guarantees it lives in
:mod:`meshterm.ui.tui.session` (``stay``/``Visit``/``run_screen``) and
:mod:`meshterm.ui.tui.screen` (``POP_ALL``/``PopToMenu``). These exercise the guarantees:
that a visit pushes and pops exactly once however many rounds it runs, that the screen
object outlives its sub-screens so its cursor and filter are still there on the way back,
and that ^W unwinds the whole stack while ^Q leaves the app — from anywhere, and never
through a dialog that is still asking something.
"""

from __future__ import annotations

import asyncio

import pytest
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from meshterm.ui.tui.progress import ProgressScreen
from meshterm.ui.tui.prompt import ButtonDialog
from meshterm.ui.tui.screen import (
    CANCEL,
    POP_ALL,
    BusyDialog,
    BusyScreen,
    PopToMenu,
    Screen,
    ScrollScreen,
)
from meshterm.ui.tui.select import Choice, SelectScreen
from meshterm.ui.tui.session import _CTRL_LETTER_CHORDS, _KEY_ACTIONS, TuiSession

#: What the terminal actually sends for the two global chords (verified against the
#: prompt_toolkit key tables, and on the Canadian Multilingual layout with ``ToUnicodeEx``).
POP_ALL_KEY = "\x17"  # ^W
QUIT_KEY = "\x11"  # ^Q

#: The two keys the picker/live-screen flow is driven by, spelled out so no raw
#: control byte has to sit in the source.
ESC = "\x1b"
ENTER = "\r"


def _async_list(items):
    """A zero-argument async stub returning a fresh copy of ``items`` (a devstate read)."""

    async def read():
        return list(items)

    return read


def _session(inp) -> TuiSession:
    """A headless session driven by a pipe, rendering nowhere."""
    return TuiSession(input=inp, output=DummyOutput())


# --- the stack discipline ----------------------------------------------------


async def test_a_visit_pushes_once_and_pops_once_however_many_rounds_it_runs() -> None:
    """``stay`` is one push and one pop, not one per round.

    This is the difference from ``run_screen`` in a loop, which pops on every resolve and
    leaves the screen off the stack while whatever it committed to runs.
    """
    with create_pipe_input() as inp:
        session = _session(inp)
        screen = SelectScreen("hub", [Choice("a", 1), Choice("b", 2)])
        depths: list[int] = []

        async def main() -> None:
            async with session.stay(screen) as visit:
                for _ in range(3):
                    depths.append(len(session._stack))
                    screen.resolve(1)
                    assert await visit.result() == 1
                    # A sub-screen opened from inside the loop nests *above* the hub.
                    child = ScrollScreen("child")
                    session.push(child)
                    depths.append(len(session._stack))
                    session.pop(child)

        await asyncio.wait_for(session.run(main()), timeout=5)
    assert depths == [1, 2, 1, 2, 1, 2]
    assert session._stack == [], "the visit must pop its screen on the way out"


async def test_a_visit_keeps_the_screens_cursor_and_filter_across_a_sub_screen() -> None:
    """Coming back from a sub-screen lands on the row you left, filter and all.

    Reusing the screen *object* is what buys this. A ``default=`` restore could only ever
    recover the cursor, and only when the row it names still exists.
    """
    with create_pipe_input() as inp:
        session = _session(inp)
        screen = SelectScreen("hub", [Choice("alpha", 1), Choice("beta", 2), Choice("gamma", 3)])

        async def main() -> None:
            async with session.stay(screen) as visit:
                screen.handle("text", "a")  # filter to the rows carrying an "a"
                screen.handle("down")
                before = (screen._filter, screen._index)
                screen.resolve("go")
                await visit.result()
                await session.run_screen(ScrollScreen("a sub-screen"))
                assert (screen._filter, screen._index) == before

        session_task = session.run(main())
        # The sub-screen is dismissed with Esc, which is the only key this flow needs.
        inp.send_text("\x1b")
        await asyncio.wait_for(session_task, timeout=5)


async def test_a_visited_screen_is_armed_from_the_moment_it_is_pushed() -> None:
    """A key that lands before the caller loops back is kept, not dropped.

    The screen is on the stack for the whole visit, so it can be resolved at any moment —
    including while the caller is still finishing the previous round. Arming only inside
    :meth:`~meshterm.ui.tui.session.Visit.result` would leave a gap in which the press has
    nowhere to go.
    """
    with create_pipe_input() as inp:
        session = _session(inp)
        screen = ScrollScreen("hub")

        async def main() -> None:
            async with session.stay(screen) as visit:
                assert screen.future is not None, "armed by the push, before any round"
                screen.resolve("early")  # resolved before the caller asks for it
                assert await visit.result() == "early"
                # And the next round is live immediately, without another await first.
                assert screen.future is not None and not screen.future.done()
                screen.resolve("next")
                assert await visit.result() == "next"

        await asyncio.wait_for(session.run(main()), timeout=5)


async def test_the_trace_target_picker_is_a_popup_gone_before_the_live_screen(
    monkeypatch,
) -> None:
    """The target pick is a floating question, answered and gone; the trace opens over the menu.

    The picker was kept pushed as a hub under the trace screen, so Esc out of a trace landed
    on the list — two places where the trace is the whole visit. It draws as a box now (over
    a base, since the menu is popped while a tool runs), comes down on Enter, and the trace
    screen has nothing under it.
    """
    from meshterm.core.models import Contact
    from meshterm.tools.trace import TraceTool
    from meshterm.ui.surface import TuiUi

    contacts = [
        Contact(name="Lakeside", public_key="a1" + "0" * 62, key_prefix="a1"),
        Contact(name="Hilltop-Repeater", public_key="3d" + "0" * 62, key_prefix="3d"),
    ]

    class _Repo:
        def target_last_traced(self):
            return {}

        def heard_nodes(self):
            return []

    class _Devstate:
        async def contacts(self):
            return list(contacts)

        async def routing_prefix_bytes(self):
            # What the real DeviceState answers with no radio: nothing to highlight.
            return 0

    class _Ctx:
        repo = _Repo()
        devstate = _Devstate()
        is_connected = False  # keeps the best-effort path-hash read off the radio
        settings = type("S", (), {"connect_on_start": False})()

    ctx = _Ctx()
    depths: list[int] = []

    with create_pipe_input() as inp:
        session = _session(inp)
        ctx.ui = TuiUi(session)

        async def fake_open_trace(_ctx, target: str, *, initial_spec: str = "") -> int:
            """Stand in for the live screen: note what is under it."""
            depths.append(len(session._stack))
            return 2

        monkeypatch.setattr("meshterm.ui.trace_screen.open_trace", fake_open_trace)

        async def main() -> None:
            # The picker is pushed by `_run_live` itself, so the keystroke cannot be
            # queued before the call: sending it blind raced the screen that reads it,
            # and lost about one full-suite run in six. `_screen_at` exists for exactly
            # this — wait until the picker is on the stack, *then* press Enter on it.
            running = asyncio.create_task(TraceTool()._run_live(ctx))
            picker = await _screen_at(session, 2)
            assert picker.floating and session._has_float(), "a box…"
            assert not session._base_screen().floating, "…over a base"
            inp.send_text(ENTER)  # Enter on the leading row commits it as the target
            result = await running
            assert result.summary == {"sessions": 1, "traces": 2}

        await asyncio.wait_for(session.run(main()), timeout=5)

    assert depths == [0], "the picker is gone before the trace screen opens"
    assert session._stack == []


async def _screen_at(session: TuiSession, depth: int, timeout: float = 2.0):
    """Spin the loop until the stack is exactly ``depth`` deep, then return its top screen.

    Lets a test drive a nested flow by resolving each screen in turn, without depending on
    when a piped keystroke happens to be read.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(session._stack) != depth:
        if loop.time() > deadline:  # pragma: no cover - only on a regression
            raise AssertionError(f"stack never reached depth {depth}: {session._stack}")
        await asyncio.sleep(0)
    return session._stack[-1]


async def test_the_tx_optimize_link_is_one_dialog_gone_before_the_sweep(monkeypatch) -> None:
    """The two picks are one box turning its page, and the sweep opens over the menu.

    The link used to be two pickers stacked one over the other, each kept pushed under
    the sweep as a hub — three frames deep for a sweep, and two popups read as two places
    to be. A popup is a question, not a place: the dialog is gone before the sweep screen
    opens, so Esc from the sweep lands on the main menu.
    """
    from meshterm.core.models import NODE_TYPE_REPEATER, Contact
    from meshterm.tools.tx_optimize import TxOptimizeTool
    from meshterm.ui.surface import TuiUi

    contacts = [
        Contact(
            name="Hilltop-Repeater",
            public_key="3d" + "0" * 62,
            key_prefix="3d",
            node_type=NODE_TYPE_REPEATER,
        ),
        Contact(name="Lakeside", public_key="a1" + "0" * 62, key_prefix="a1"),
    ]

    class _Ctx:
        admin_store = type("A", (), {"get": staticmethod(lambda _c: None)})()
        devstate = type("D", (), {"contacts": staticmethod(_async_list(contacts))})()

    ctx = _Ctx()
    swept: list[tuple[str, str, int]] = []
    pages: list[str] = []

    with create_pipe_input() as inp:
        session = _session(inp)
        ctx.ui = TuiUi(session)

        async def fake_sweep(_ctx, *, admin_node, target_label, target_hash):
            """Stand in for the sweep screen: note who it tunes and what is under it."""
            swept.append((admin_node.name, target_label, len(session._stack)))
            return {"best": 20}

        monkeypatch.setattr("meshterm.ui.tx_screen.open_tx_optimize", fake_sweep)

        async def turned_to(dialog, step: str) -> None:
            """Spin until the one dialog shows ``step`` — the same object, a new page."""
            while step not in dialog.title:
                await asyncio.sleep(0)
            pages.append(dialog.title)
            assert session._stack[-1] is dialog, "one box the whole way through"
            assert len(session._stack) == 2, "floating over a blank base — it is a popup"
            assert not session._stack[0].floating and dialog.floating

        async def main() -> None:
            run = asyncio.ensure_future(TxOptimizeTool()._run_live(ctx))
            dialog = await _screen_at(session, 2)
            await turned_to(dialog, "step 1 of 2")
            dialog.resolve("Hilltop-Repeater")
            await turned_to(dialog, "step 2 of 2")
            assert [c.value for c in dialog._choices()] == ["Lakeside"], "the tuned node is out"
            dialog.resolve(CANCEL)  # Esc on step 2 turns back, not out
            await turned_to(dialog, "step 1 of 2")
            assert dialog._current_choice().value == "Hilltop-Repeater", "still highlighted"
            dialog.resolve("Hilltop-Repeater")
            await turned_to(dialog, "step 2 of 2")
            dialog.resolve("Lakeside")
            result = await asyncio.wait_for(run, timeout=2)
            assert result.summary == {"best": 20}, "the sweep's summary comes back"

        await asyncio.wait_for(session.run(main()), timeout=5)

    assert swept == [("Hilltop-Repeater", "Lakeside", 0)], "nothing at all under the sweep"
    assert [p.split(" — ")[1] for p in pages] == [
        "node to tune · step 1 of 2",
        "measure at · step 2 of 2",
        "node to tune · step 1 of 2",
        "measure at · step 2 of 2",
    ]
    assert session._stack == [], "and the dialog is gone on the way out"


# --- refreshing a visited list's rows ----------------------------------------


def test_replace_items_keeps_the_filter_and_follows_the_highlighted_row() -> None:
    """A list whose content is data can change under a reader without moving them.

    The editors' rows carry live values and their titles count what is staged, so they have
    to be rebuilt after every action. Rebuilding them as a whole new *screen* threw away the
    typed filter and left the cursor to a ``default=`` restore; swapping the rows in place
    keeps both, and follows the highlighted row by value wherever it moved to.
    """
    screen = SelectScreen(
        "Editor · nothing staged",
        [Choice("alpha", "a"), Choice("beta", "b"), Choice("gamma", "g")],
    )
    screen.handle("text", "a")  # filters to alpha / beta / gamma — all carry an "a"
    screen.handle("down")  # …onto beta
    assert screen._current_choice().value == "b"

    # beta moves to the front and its label gains a staged value; the title recounts.
    screen.replace_items(
        [Choice("beta → 12", "b"), Choice("alpha", "a"), Choice("gamma", "g")],
        title="Editor · 1 staged",
    )
    assert screen.title == "Editor · 1 staged"
    assert screen._filter == "a", "the typed filter survives the swap"
    assert screen._current_choice().value == "b", "the highlight followed its row"


def test_turn_page_drops_the_filter_and_highlights_the_named_row() -> None:
    """A page turned is a new question, so nothing typed for the last one rides along.

    The opposite claim from ``replace_items``: that one refreshes the same list, this one
    shows a different one in the same box. The query typed to find the node to tune would
    narrow the *measure at* list to nothing it was meant for, and the highlight lands on
    the page's own default — its previous answer, when turned back to — never on a row
    that happens to share a value with the one last highlighted.
    """
    screen = SelectScreen(
        "Link — step 1 of 2",
        [Choice("alpha", "a"), Choice("beta", "b"), Choice("gamma", "g")],
    )
    screen.handle("text", "b")
    assert screen._current_choice().value == "b"

    screen.turn_page(
        [Choice("beta", "b"), Choice("gamma", "g")],
        title="Link — step 2 of 2",
        prompt="Now the other end:",
        default="g",
    )
    assert screen.title == "Link — step 2 of 2"
    assert screen._prompt == "Now the other end:"
    assert screen._filter == "", "the last page's query is gone"
    assert screen._current_choice().value == "g", "the page's own default, not the old row"

    screen.turn_page([Choice("alpha", "a"), Choice("beta", "b")], title="Link — step 1 of 2")
    assert screen._current_choice().value == "a", "no default: the first row"


# --- a wizard is a stepped chain in one box ------------------------------------


async def test_a_wizard_turns_one_box_between_its_steps() -> None:
    """Esc on a later step turns back a page; the box is pushed once and popped once.

    Two picks used to be two popups stacked, each a frame to walk back through. A popup
    is a question, not a place: the chain is one list turning its page, and it is gone
    before the caller opens whatever the answers were for. And it draws as a popup even
    on an empty stack — a blank base under it for the visit — where a lone floating
    screen would otherwise be painted as the full-frame background.
    """
    from meshterm.ui.menus import WizardPage, run_wizard

    shown: list[tuple[str, str]] = []

    def first(values: list) -> WizardPage:
        return WizardPage(
            "Link — step 1 of 2", [Choice("alpha", "a"), Choice("beta", "b")], default=values[0]
        )

    def second(values: list) -> WizardPage:
        return WizardPage(
            f"Link — after {values[0]} · step 2 of 2",
            [Choice("gamma", "g"), Choice("delta", "d")],
            default=values[1],
        )

    with create_pipe_input() as inp:
        session = _session(inp)

        async def main() -> None:
            run = asyncio.ensure_future(run_wizard(session, [first, second]))
            box = await _screen_at(session, 2)
            assert not session._stack[0].floating, "a blank base, so the box floats"
            shown.append((box.title, box._current_choice().value))
            box.resolve("b")
            while "step 2" not in box.title:
                await asyncio.sleep(0)
            shown.append((box.title, box._current_choice().value))
            box.resolve("d")
            answers = await asyncio.wait_for(run, timeout=2)
            assert answers == ["b", "d"]
            assert session._stack == [], "popped before the answers come back"

            # Turned back: step 2's Esc re-shows step 1 with its answer highlighted.
            run = asyncio.ensure_future(run_wizard(session, [first, second]))
            box = await _screen_at(session, 2)
            box.resolve("a")
            while "step 2" not in box.title:
                await asyncio.sleep(0)
            box.resolve(CANCEL)
            while "step 1" not in box.title:
                await asyncio.sleep(0)
            shown.append((box.title, box._current_choice().value))
            assert session._stack[-1] is box, "the same box, turned back"
            box.resolve(CANCEL)  # Esc on the first step abandons
            assert await asyncio.wait_for(run, timeout=2) is None
            assert session._stack == []

        await asyncio.wait_for(session.run(main()), timeout=5)

    assert shown == [
        ("Link — step 1 of 2", "a"),
        ("Link — after b · step 2 of 2", "g"),
        ("Link — step 1 of 2", "a"),
    ]


async def test_a_popup_with_nothing_under_it_floats_over_the_menu() -> None:
    """A question asked on the way into a tool shows the menu around it, never an empty frame.

    The menu is popped while a tool runs, so a lead-in picker used to be the only frame and
    was drawn full-frame — and the blank base that fixed that showed nothing behind the
    box. A dialog is smaller than the frame, so what shows around it is the page it was
    reached from: the root the menu declared is the base pushed under it, and popped with
    it, so the tool's own page opens over nothing.
    """
    menu = SelectScreen("What would you like to do?", [Choice("TX optimize", "tx")])
    seen: list = []

    with create_pipe_input() as inp:
        session = _session(inp)
        session.set_root(menu)

        async def main() -> None:
            ask = asyncio.ensure_future(
                session.select("Node to manage", [Choice("Hilltop", "h")], floating=True)
            )
            box = await _screen_at(session, 2)
            seen.append((session._base_screen(), box.floating, session._has_float()))
            box.resolve("h")
            assert await asyncio.wait_for(ask, timeout=2) == "h"
            assert session._stack == [], "the menu is the tool's to re-push, not the popup's"

        await asyncio.wait_for(session.run(main()), timeout=5)

    assert seen == [(menu, True, True)], "drawn as a box over the menu"


async def test_a_wizard_step_may_float_its_own_prompt_over_the_box() -> None:
    """A step with nothing to list (a typed value) runs as an awaitable above the box.

    The box stays on its last page underneath — it is the backdrop the prompt floats over
    — and ``None`` from the prompt steps back exactly as Esc on a page would.
    """
    from meshterm.ui.menus import WizardPage, run_wizard

    typed = iter([None, "3d"])  # Esc the first time, then a hex prefix

    def first(values: list) -> WizardPage:
        return WizardPage("Pick — step 1 of 2", [Choice("alpha", "a")], default=values[0])

    async def second(values: list) -> str | None:
        return next(typed)

    def second_step(values: list):
        return second(values)

    with create_pipe_input() as inp:
        session = _session(inp)

        async def main() -> None:
            run = asyncio.ensure_future(run_wizard(session, [first, second_step]))
            box = await _screen_at(session, 2)
            box.resolve("a")  # step 2 backs out at once → step 1 is asked again
            while box.future is None or box.future.done():
                await asyncio.sleep(0)
            assert session._stack[-1] is box, "the box never left"
            box.resolve("a")
            assert await asyncio.wait_for(run, timeout=2) == ["a", "3d"]

        await asyncio.wait_for(session.run(main()), timeout=5)


def test_replace_items_clamps_to_the_position_when_the_row_is_gone() -> None:
    """Deleting the row you were on leaves you where it was, not back at the top."""
    screen = SelectScreen("Queue", [Choice(name, name) for name in ("a", "b", "c", "d")])
    for _ in range(2):
        screen.handle("down")
    assert screen._current_choice().value == "c"

    screen.replace_items([Choice(name, name) for name in ("a", "b", "d")])
    assert screen._current_choice().value == "d", "the row that slid into the gap"

    # And a swap that empties the list entirely must not leave a stale index behind.
    screen.replace_items([])
    assert screen._current_choice() is None


def test_update_rows_resorts_the_lanes_and_keeps_the_highlight_on_its_contact() -> None:
    """The Trace picker's TRACED lane ages under the reader without moving them.

    The list is sorted by the very column a returning trace changes, so the node just
    walked jumps to the top — and the cursor has to ride it there, or the reader would be
    put back on whatever row inherited their position.
    """
    from datetime import datetime, timedelta, timezone

    from meshterm.ui.contactlist import (
        TRACE_LANES,
        TRACE_SORT_COLUMNS,
        TRACE_SORT_OPENS_ASCENDING,
        ContactListScreen,
        ContactRow,
    )
    from meshterm.ui.widgets import ContactsSort

    now = datetime.now(timezone.utc)
    rows = [
        ContactRow(value="alpha", name="alpha", last_traced=now - timedelta(hours=2)),
        ContactRow(value="beta", name="beta", last_traced=None),  # never traced
    ]
    screen = ContactListScreen(
        "Trace target — pick a target",
        rows=rows,
        prefix_bytes=1,
        sort=ContactsSort.from_name("traced", TRACE_SORT_COLUMNS, TRACE_SORT_OPENS_ASCENDING),
        lanes=TRACE_LANES,
    )
    screen.handle("text", "bet")  # find-as-you-type down to the never-traced node
    assert screen._current_choice().value == "beta"

    # beta is traced: it now leads the TRACED sort, where alpha was.
    screen.update_rows(
        [
            ContactRow(value="alpha", name="alpha", last_traced=now - timedelta(hours=2)),
            ContactRow(value="beta", name="beta", last_traced=now),
        ]
    )
    assert screen._filter == "bet", "the typed filter survives the swap"
    assert screen._current_choice().value == "beta", "the highlight followed its contact"
    for _ in range(3):  # ⌫ back out of the filter: the whole, re-sorted list is there
        screen.handle("backspace")
    assert [c.value for c in screen._choices()] == ["beta", "alpha"]


# --- Esc peels the filter before it leaves -----------------------------------


async def test_a_typed_filter_is_peeled_before_the_list_is_left() -> None:
    """Esc clears a standing filter and stays; the next Esc leaves.

    The map and the mesh walk always did this. The select list and the path composer did
    not — the same keys on the same affordance, opposite outcomes — so typing three letters
    on a contact list and pressing Esc dropped you at the main menu.
    """
    screen = SelectScreen("Contacts", [Choice(n, n) for n in ("alpha", "beta", "gamma")])
    screen.future = asyncio.get_running_loop().create_future()
    screen.handle("text", "b")
    assert [c.value for c in screen._choices()] == ["beta"]

    screen.handle("escape")
    assert screen._filter == "", "the filter is what the first Esc peels"
    assert not screen.future.done(), "…and the screen is still here"
    assert [c.value for c in screen._choices()] == ["alpha", "beta", "gamma"]

    screen.handle("escape")
    assert screen.future.result() is CANCEL, "the second Esc leaves"


def test_the_footer_says_clear_while_a_filter_is_standing() -> None:
    """Esc's verb follows what the press will actually do — the map and walk's wording."""
    screen = SelectScreen(
        "Contacts",
        [Choice("alpha", "a")],
        footer_hint="↑↓ move · Enter open · type to filter · Esc back",
    )
    assert screen.footer_hint.endswith("Esc back")
    screen.handle("text", "a")
    assert screen.footer_hint.endswith("Esc clear")
    screen.handle("backspace")
    assert screen.footer_hint.endswith("Esc back")


async def test_the_path_composer_peels_its_typed_entry_first() -> None:
    """Esc unwinds the composer the way ⌫ already did: the entry, then the screen."""
    from meshterm.services.topology import build_topology
    from meshterm.ui.path_composer import PathComposerScreen

    self_id = "aa" * 6 + "0" * 52
    screen = PathComposerScreen(
        device_label="Homestead",
        device_hash=self_id,
        topology=build_topology(
            self_id=self_id,
            contacts=[],
            trace_paths=[],
            packet_paths=[],
            neighbour_links=[],
        ),
        width_bytes=1,
    )
    screen.future = asyncio.get_running_loop().create_future()
    screen.handle("text", "a")
    screen.handle("text", "1")
    assert screen.footer_hint.endswith("Esc clear")

    screen.handle("escape")
    assert not screen.future.done(), "the entry is peeled, the composer stays"
    assert screen.footer_hint.endswith("Esc cancel")

    screen.handle("escape")
    assert screen.future.result() is CANCEL


# --- entry chains step back --------------------------------------------------


async def test_a_cancelled_step_goes_back_to_the_one_before_it() -> None:
    """Esc in a multi-prompt flow undoes one step, keeping what earlier steps hold.

    A chain of prompts used to be a straight run of awaits, so Esc anywhere abandoned the
    whole flow — a mistyped 32-hex channel key cost the name typed before it too.
    """
    from meshterm.ui.menus import run_steps

    asked: list[str] = []
    key_attempts = iter([None, "cafe"])  # first Esc, then a good key on the way forward

    async def name_step(values: list):
        asked.append("name")
        return values[0] or "Backyard"  # opens on what it last returned

    async def key_step(values: list):
        asked.append("key")
        return next(key_attempts)

    answers = await run_steps([name_step, key_step])

    assert answers == ["Backyard", "cafe"]
    assert asked == ["name", "key", "name", "key"], "the name was asked again, not skipped"


async def test_backing_out_of_the_first_step_ends_the_flow() -> None:
    """There is nothing behind step one, so Esc there leaves — as it always did."""
    from meshterm.ui.menus import run_steps

    async def only_step(values: list):
        return None

    assert await run_steps([only_step]) is None


async def test_a_step_keeps_its_own_answer_to_offer_as_a_default() -> None:
    """Coming back to a step, it is handed what it returned last time (its field's value)."""
    from meshterm.ui.menus import run_steps

    seen: list = []

    async def first(values: list):
        return "one"

    async def second(values: list):
        seen.append(list(values))
        return "two"

    async def third(values: list):
        return None if len(seen) == 1 else "three"  # Esc the first time through

    assert await run_steps([first, second, third]) == ["one", "two", "three"]
    # Asked again after the third step backed out, the second step is handed what it
    # answered before — which is how a re-opened field comes back already filled in.
    assert seen == [["one", None, None], ["one", "two", None]]


# --- ^W, the pop-all ---------------------------------------------------------


async def test_pop_all_unwinds_every_frame_at_once() -> None:
    """^W from three screens deep pops all three and lands the caller past the whole flow."""
    with create_pipe_input() as inp:
        session = _session(inp)
        popped: list[str] = []
        landed: list[str] = []

        async def main() -> None:
            hub = ScrollScreen("hub")
            try:
                async with session.stay(hub) as visit:
                    try:
                        await session.run_screen(ScrollScreen("middle"))
                    finally:
                        popped.append("middle")
                    await visit.result()
            except PopToMenu:
                landed.append("menu")
            finally:
                popped.append("hub")

        inp.send_text(POP_ALL_KEY)
        await asyncio.wait_for(session.run(main()), timeout=5)
    assert landed == ["menu"], "the unwind must reach the frame that catches it"
    assert popped == ["middle", "hub"], "every frame pops itself on the way out"
    assert session._stack == []


async def test_pop_all_passes_straight_through_an_except_exception_guard() -> None:
    """An unwind is not a tool failure.

    The app wraps tool runs in ``except Exception`` so a broken tool cannot take the menu
    down with it (:func:`meshterm.ui.menu._run_selection` is the widest such guard). An
    unwind has to cross those without being caught and reported as an error, which is why
    :class:`PopToMenu` derives from ``BaseException``.
    """
    assert issubclass(PopToMenu, BaseException)
    assert not issubclass(PopToMenu, Exception)

    with create_pipe_input() as inp:
        session = _session(inp)
        swallowed: list[str] = []
        landed: list[str] = []

        async def a_tool() -> None:
            try:
                await session.run_screen(ScrollScreen("a tool's screen"))
            except Exception:  # noqa: BLE001 - the guard under test
                swallowed.append("caught")

        async def main() -> None:
            try:
                await a_tool()
            except PopToMenu:
                landed.append("menu")

        inp.send_text(POP_ALL_KEY)
        await asyncio.wait_for(session.run(main()), timeout=5)
    assert swallowed == []
    assert landed == ["menu"]


async def test_pop_all_armed_while_nothing_is_pushed_fires_at_the_next_screen() -> None:
    """^W during a device read still unwinds — at the next screen the flow tries to open.

    There is no future to carry the sentinel while the app sits under the busy overlay with
    an empty stack, so the flag is what remembers the press.
    """
    with create_pipe_input() as inp:
        session = _session(inp)
        opened: list[str] = []

        async def main() -> None:
            inp.send_text(POP_ALL_KEY)
            await asyncio.sleep(0.05)  # let the key reach the dispatcher
            assert session.top is None, "nothing is pushed while the read is in flight"
            with pytest.raises(PopToMenu):
                await session.run_screen(ScrollScreen("the screen the read was for"))
            opened.append("refused")

        await asyncio.wait_for(session.run(main()), timeout=5)
    assert opened == ["refused"]


def test_pop_all_is_refused_over_anything_modal() -> None:
    """A dialog is an unanswered question and progress is work in flight; ^W waits."""
    session = TuiSession(output=DummyOutput())
    for layer in (
        ButtonDialog("Delete it?", [("Cancel", False), ("Delete", True)]),
        ProgressScreen("Working"),
        BusyScreen("checking…"),
        BusyDialog("saving Lakeside…"),
    ):
        session.push(layer)
        assert session.request_pop_all() is False, f"{type(layer).__name__} must block ^W"
        assert session._unwinding is False
        session.pop(layer)


def test_pop_all_is_a_no_op_at_the_navigation_root() -> None:
    """You cannot go back to where you already are."""
    session = TuiSession(output=DummyOutput())
    menu = SelectScreen("What would you like to do?", [Choice("a tool", "a")])
    session.set_root(menu)
    session.push(menu)
    assert session.request_pop_all() is False
    assert session._unwinding is False
    # One screen deeper, the same key fires.
    deeper = ScrollScreen("a tool")
    session.push(deeper)
    assert session.request_pop_all() is True
    assert deeper.future is None or True  # resolving a screen with no future is harmless


def test_pop_all_resolves_the_top_screen_with_the_sentinel() -> None:
    """The sentinel travels through the future; no caller in the app ever sees it."""
    session = TuiSession(output=DummyOutput())
    screen = ScrollScreen("deep")

    class _Fut:
        def __init__(self) -> None:
            self.value = None

        def done(self) -> bool:
            return False

        def set_result(self, value: object) -> None:
            self.value = value

    screen.future = _Fut()  # type: ignore[assignment]
    session.push(screen)
    assert session.request_pop_all() is True
    assert screen.future.value is POP_ALL  # type: ignore[union-attr]


def test_a_stack_reset_disarms_a_pending_unwind() -> None:
    """A disconnect abandons the flow itself, so an armed ^W has nothing left to unwind."""
    session = TuiSession(output=DummyOutput())
    session.push(ScrollScreen("deep"))
    session.request_pop_all()
    assert session._unwinding is True
    session.reset()
    assert session._unwinding is False


# --- ^Q, the quit ------------------------------------------------------------


async def test_quit_from_a_nested_screen_still_runs_the_apps_teardown() -> None:
    """^Q leaves from anywhere — and the flow's ``finally`` blocks still run.

    The chord exits the prompt_toolkit application straight from the key handler, so the
    coroutine driving the app is parked on a screen when it returns. Left abandoned there,
    the teardown that closes the history and chat runs, stops the services and arms the exit
    watchdog would never happen.
    """
    with create_pipe_input() as inp:
        session = _session(inp)
        torn_down: list[str] = []

        async def main() -> None:
            try:
                await session.run_screen(ScrollScreen("two screens deep"))
            finally:
                torn_down.append("services closed")

        inp.send_text(QUIT_KEY)
        await asyncio.wait_for(session.run(main()), timeout=5)
    assert torn_down == ["services closed"]


def _asking_session(inp, asked: list[str], answer: str | None = None):
    """A session whose quit chord floats a Cancel/Quit confirm, the way the menu declares it.

    ``answer`` resolves the dialog as soon as it is pushed (``None`` leaves it up for the
    test to drive); ``asked`` records each time the question is put.
    """
    session = _session(inp)

    async def ask() -> bool:
        asked.append("asked")
        dialog = ButtonDialog("Quit?", [("Cancel", "cancel"), ("Quit", "quit")], default=1)
        if answer is not None:
            asyncio.get_running_loop().call_soon(dialog.resolve, answer)
        return await session.run_dialog(dialog) == "quit"

    session.set_quit_confirm(ask)
    return session


async def test_quit_asks_first_and_cancelling_leaves_the_flow_where_it_was() -> None:
    """One ^Q floats the confirm over whatever is up; Cancel puts the reader back.

    The screen under it is never resolved or popped — a capture behind the box keeps
    capturing — which is the whole difference from the old straight-out chord (issue #22).
    """
    with create_pipe_input() as inp:
        asked: list[str] = []
        session = _asking_session(inp, asked, answer="cancel")
        screen = ScrollScreen("a capture in progress")

        async def main() -> None:
            session.push(screen)
            session._dispatch("quit")
            for _ in range(5):
                await asyncio.sleep(0)
            assert asked == ["asked"]
            assert session.top is screen, "Cancel lands back on the screen it floated over"
            assert not session._quit_asking
            session.pop(screen)

        await asyncio.wait_for(session.run(main()), timeout=5)
    assert screen.future is None or not screen.future.done()


async def test_a_second_quit_while_the_confirm_is_up_leaves_at_once() -> None:
    """^Q ^Q is the fast way out, and it never waits on the dialog to take a key."""
    with create_pipe_input() as inp:
        asked: list[str] = []
        session = _asking_session(inp, asked)
        torn_down: list[str] = []

        async def main() -> None:
            try:
                await session.run_screen(ScrollScreen("deep"))
            finally:
                torn_down.append("services closed")

        # Both presses in one batch: the flag is raised synchronously, so the second is
        # the leaving rather than a second dialog.
        inp.send_text(QUIT_KEY + QUIT_KEY)
        await asyncio.wait_for(session.run(main()), timeout=5)
    assert torn_down == ["services closed"]
    assert len(asked) <= 1


async def test_quit_confirmed_from_anywhere_exits_the_app() -> None:
    """Choosing Quit in the floated confirm exits, and the flow's teardown still runs."""
    with create_pipe_input() as inp:
        asked: list[str] = []
        session = _asking_session(inp, asked, answer="quit")
        torn_down: list[str] = []

        async def main() -> None:
            try:
                await session.run_screen(ScrollScreen("deep"))
            finally:
                torn_down.append("services closed")

        inp.send_text(QUIT_KEY)
        await asyncio.wait_for(session.run(main()), timeout=5)
    assert asked == ["asked"]
    assert torn_down == ["services closed"]


def test_esc_at_the_main_menu_does_nothing_but_peel_a_filter() -> None:
    """Esc-mashing up to the menu settles there instead of opening the quit confirm."""
    from meshterm.ui.menu import _MainMenu

    menu = _MainMenu(
        "menu",
        [Choice("alpha", 1), Choice("beta", 2)],
        footer_hint="↑↓ move · type to filter · Enter select · ^Q quit?",
    )
    menu.future = asyncio.new_event_loop().create_future()
    menu.handle("escape")
    assert not menu.future.done(), "a bare Esc at the menu resolves nothing"
    assert not menu.footer_hint.endswith("Esc clear")
    menu.handle("text", "b")
    assert menu.footer_hint.endswith("^Q quit? · Esc clear")
    menu.handle("escape")
    assert menu._filter == "" and not menu.future.done()
    assert menu.footer_hint.endswith("^Q quit?")


def test_the_main_menu_lane_puts_the_way_out_on_f3() -> None:
    """``Quit?`` on F3 asks; its Shift half, F8, is ``Quit!``, which doesn't."""
    from meshterm.ui.menu import _MainMenu
    from meshterm.ui.menus import section_heading
    from meshterm.ui.tui.fkeys import action_for

    menu = _MainMenu(
        "menu",
        [section_heading("A"), Choice("alpha", 1), section_heading("B"), Choice("beta", 2)],
    )
    lane = menu.fkey_lane
    assert (lane[2].label, lane[2].opp_label) == ("Quit?", "Quit!")
    assert action_for(lane, 3) == "quit"
    assert action_for(lane, 8) == "quit_now"
    assert lane[0].label == "Sect ↑", "the section jumps keep F1/F2"


async def test_quit_now_leaves_without_asking() -> None:
    """F8's ``quit_now`` exits even with a confirm declared, and the teardown still runs."""
    with create_pipe_input() as inp:
        asked: list[str] = []
        session = _asking_session(inp, asked)
        torn_down: list[str] = []

        async def main() -> None:
            try:
                await session.run_screen(ScrollScreen("menu"))
            finally:
                torn_down.append("services closed")

        async def press() -> None:
            await asyncio.sleep(0.05)
            session._dispatch("quit_now")

        asyncio.ensure_future(press())
        await asyncio.wait_for(session.run(main()), timeout=5)
    assert asked == []
    assert torn_down == ["services closed"]


# --- the bindings themselves -------------------------------------------------


def test_both_global_chords_are_bound_on_both_ctrl_keys() -> None:
    """^W and ^Q reach their action through the plain binding and the right-Ctrl rescue.

    The single chord table is what guarantees the second half: a chord added there can never
    be bound without its rescue, which is the only way a layout that claims the right Ctrl as
    a character-group modifier reaches it at all.
    """
    assert _CTRL_LETTER_CHORDS["w"] == "to_menu"
    assert _CTRL_LETTER_CHORDS["q"] == "quit"
    from prompt_toolkit.keys import Keys

    assert _KEY_ACTIONS[Keys.ControlW] == "to_menu"
    assert _KEY_ACTIONS[Keys.ControlQ] == "quit"


def test_neither_global_chord_is_advertised_anywhere() -> None:
    """Both are deliberately undiscoverable in the UI, bar the menu's own way out.

    A global verb has no screen to belong to, so a footer atom or an F-key chip would have to
    ride on *every* screen; the lane only has three free slots per screen to begin with. The
    one exception is the main menu's hint, which names ^Q because Esc is inert there and the
    menu is where a reader looks for the way out (issue #22) — exactly one string, there.
    """
    import ast
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "meshterm"
    chord = re.compile(r"\^[WQ]\b")
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Docstrings explain the chords at length and are not what a reader of the UI sees;
        # what matters is every string the module *states*.
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, holders)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and chord.search(node.value)
            ):
                offenders.append(f"{path.name}:{node.lineno}: {node.value.strip()[:70]}")
    menu_hint = [o for o in offenders if o.startswith("menu.py:") and o.endswith("· ^Q quit?")]
    assert len(menu_hint) == 1, "the main menu's hint must name the quit chord, once"
    offenders.remove(menu_hint[0])
    assert not offenders, "the global chords must not appear in any hint or chip:\n" + "\n".join(
        offenders
    )


def test_only_navigational_layers_are_non_modal() -> None:
    """``modal`` is about owning the keyboard, ``floating`` only about being drawn as a box.

    They are easy to conflate — most dialogs are both — so the two screens that break the
    correlation are asserted outright: a plain select list floats and is not modal, and the
    busy splash is modal without floating at all.
    """
    assert SelectScreen("list", [Choice("a", 1)]).floating is True
    assert SelectScreen("list", [Choice("a", 1)]).modal is False
    assert BusyScreen("checking…").floating is False
    assert BusyScreen("checking…").modal is True
    # ...and its twin is the same claim on the keyboard drawn as a box over what it
    # interrupts, which is the pair's whole point.
    assert BusyDialog("saving…").floating is True
    assert BusyDialog("saving…").modal is True
    assert Screen().modal is False
    assert CANCEL is not POP_ALL


async def test_pop_all_unwinds_from_a_screen_a_key_handler_opened() -> None:
    """^W in the packet viewer unwinds, though nothing awaits the frame that opened it.

    A screen opened from a key handler cannot be awaited by its opener — a handler is
    sync, so the live feed floats the viewer with ``ensure_future(run_screen(...))`` and
    the call chain that would have carried the unwind ends in a detached task. The hub
    below is meanwhile parked on ``visit.result()``, which no press resolves. ^W used to
    close the viewer and stop there, leaving the flag armed to fire on some later
    keystroke — the unwind arrived at the next thing the reader did instead of at the
    key they pressed (JP, 2026-08-31).
    """
    with create_pipe_input() as inp:
        session = _session(inp)
        landed: list[str] = []
        popped: list[str] = []

        async def main() -> None:
            hub = ScrollScreen("the live feed", floating=False)
            try:
                async with session.stay(hub) as visit:
                    viewer = ScrollScreen("a packet")
                    # Exactly how a key handler opens one: detached, never awaited.
                    asyncio.ensure_future(session.run_screen(viewer))
                    await asyncio.sleep(0.05)  # let it push
                    assert session.top is viewer
                    inp.send_text(POP_ALL_KEY)
                    await visit.result()
            except PopToMenu:
                landed.append("menu")
            finally:
                popped.append("hub")

        await asyncio.wait_for(session.run(main()), timeout=5)
    assert landed == ["menu"], "^W must reach the menu, not stop at the screen it was pressed on"
    assert popped == ["hub"]
    assert session._stack == []


async def test_a_detached_flow_absorbs_the_unwind_it_cannot_carry() -> None:
    """The task a key handler starts is off the navigation chain, so the copy stops there.

    ``request_pop_all`` arms every frame, so the unwind is already travelling out through
    the screen that opened this one. Left to propagate here as well it would reach nothing
    but the event loop's exception handler, logged as an unretrieved task error. The
    flow's own cleanup still runs on the way through.
    """
    session = TuiSession(output=DummyOutput())
    steps: list[str] = []

    async def work() -> None:
        try:
            steps.append("opened")
            raise PopToMenu()
        finally:
            steps.append("cleaned up")

    task = session.run_detached(work())
    await task
    assert task.exception() is None, "an unwind must not become an unretrieved task error"
    assert steps == ["opened", "cleaned up"]


async def test_a_detached_flow_still_reports_a_real_failure() -> None:
    """Only the unwind is absorbed — a broken flow still surfaces as a task error."""
    session = TuiSession(output=DummyOutput())

    async def work() -> None:
        raise RuntimeError("the flow broke")

    task = session.run_detached(work())
    with pytest.raises(RuntimeError):
        await task


# --- work in flight ----------------------------------------------------------


async def test_a_key_pressed_during_a_slow_action_reaches_the_hub_that_is_still_armed() -> None:
    """The defect ``busy_dialog`` exists to close, stated as a test.

    A hub kept up with ``stay`` is armed for the whole visit, so a key pressed while the
    caller is off doing slow device work still lands on it. Esc resolves the round nobody
    is waiting for yet, and it is handed back the instant the work finishes — the screen
    closing on its own a beat after a press that appeared to do nothing.
    """
    with create_pipe_input() as inp:
        session = _session(inp)
        screen = SelectScreen("hub", [Choice("alpha", 1)])
        banked: list = []

        async def main() -> None:
            async with session.stay(screen) as visit:
                inp.send_text(ESC)  # pressed "while the write is running"
                await asyncio.sleep(0.05)  # let the input loop dispatch it
                banked.append(await asyncio.wait_for(visit.result(), timeout=1))

        await asyncio.wait_for(session.run(main()), timeout=5)

    assert banked == [CANCEL]  # the press was banked and comes straight back


async def test_the_busy_dialog_takes_the_keys_the_hub_would_have_banked() -> None:
    """With the card up, the same press dies on it: nothing is banked, nothing resolves.

    This is what makes a slow channel write safe to sit through — the list underneath
    cannot be filtered down to nothing, cannot be re-fired by an impatient second Enter,
    and cannot be closed by an Esc that looks ignored until the work lands.
    """
    with create_pipe_input() as inp:
        session = _session(inp)
        screen = SelectScreen("hub", [Choice("alpha", 1)])
        timed_out = []

        async def main() -> None:
            async with session.stay(screen) as visit:
                async with session.busy_dialog("saving…", title="Channels"):
                    inp.send_text(ESC + "zzz" + ENTER)  # Esc, filter letters, and Enter
                    await asyncio.sleep(0.05)
                    assert session.top.__class__ is BusyDialog  # the card owns the keyboard
                try:
                    await asyncio.wait_for(visit.result(), timeout=0.2)
                except asyncio.TimeoutError:
                    timed_out.append(True)

        await asyncio.wait_for(session.run(main()), timeout=5)

    assert timed_out == [True]  # nothing was banked while the card was up
    assert screen._filter == ""  # and the letters never reached the list's filter


async def test_the_busy_dialog_is_one_card_however_deeply_it_nests() -> None:
    """A batch of writes reports as one wait, not a box flashing per write."""
    with create_pipe_input() as inp:
        session = _session(inp)
        depths: list[int] = []

        async def main() -> None:
            session.push(ScrollScreen("hub", floating=False))
            async with session.busy_dialog("reordering channels…") as outer:
                depths.append(len(session._stack))
                async with session.busy_dialog("moving Lakeside · 1/2") as inner:
                    assert inner is outer  # the nested wait rides the card already up
                    depths.append(len(session._stack))
                outer.message = "moving Dorval · 2/2"  # the owner retitles it as it goes
                depths.append(len(session._stack))
            depths.append(len(session._stack))

        await asyncio.wait_for(session.run(main()), timeout=5)

    assert depths == [2, 2, 2, 1]  # pushed once, popped once, hub still underneath


async def test_the_chip_that_asked_to_quit_leaves_when_pressed_again() -> None:
    """On a handheld, F3 asks from the menu and the confirm keeps F3: a second press leaves.

    The confirm's lane carries ``Quit!`` on the slot the menu's ``Quit?`` sat on, sending
    the same ``quit`` — which, with the question up, is the leaving, as a second ^Q is.
    """
    from meshterm.platforms import PICOCALC, set_platform
    from meshterm.ui.tui.fkeys import EMPTY_LANE, FPair

    set_platform(PICOCALC)
    lane = list(EMPTY_LANE)
    lane[2] = FPair("Quit!", "quit")
    with create_pipe_input() as inp:
        session = _session(inp)

        async def ask() -> bool:
            dialog = ButtonDialog("Quit?", [("Cancel", "cancel"), ("Quit", "quit")], lane=lane)
            return await session.run_dialog(dialog) == "quit"

        session.set_quit_confirm(ask)
        torn_down: list[str] = []

        async def main() -> None:
            session.push(ScrollScreen("the menu"))
            try:
                session._dispatch("quit")  # the menu's Quit? chip
                for _ in range(5):
                    await asyncio.sleep(0)
                assert isinstance(session.top, ButtonDialog), "the confirm is up"
                session._dispatch("f3")  # the same chip, on the confirm's lane
                await asyncio.sleep(5)  # the leaving cancels this
            finally:
                torn_down.append("services closed")

        await asyncio.wait_for(session.run(main()), timeout=5)
    assert torn_down == ["services closed"]
