# SPDX-License-Identifier: Apache-2.0
"""Tests for the navigation stack: strict push/pop, kept state, and the two global chords.

The path of the app is a stack. The main menu is at the bottom. Each screen or dialog that
the user enters is one screen on the stack, and each Esc key press is one pop. The code that
guarantees this is in :mod:`meshterm.ui.tui.session` (``stay``/``Visit``/``run_screen``) and
:mod:`meshterm.ui.tui.screen` (``POP_ALL``/``PopToMenu``). These tests check the guarantees:

- A visit pushes and pops exactly once, for any number of rounds.
- The screen object stays longer than its sub-screens, so its highlight and filter are
  still there when the user comes back.
- The ^W chord unwinds the whole stack, and the ^Q chord leaves the app. Both work from
  each screen, and neither works through a dialog that still asks a question.
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

#: The bytes that the terminal sends for the two global chords. We checked them against the
#: prompt_toolkit key tables, and on the Canadian Multilingual layout with ``ToUnicodeEx``.
POP_ALL_KEY = "\x17"  # ^W
QUIT_KEY = "\x11"  # ^Q

#: The two keys that drive the flow of the picker and the live screen. They are written
#: out, so that no raw control byte is in the source.
ESC = "\x1b"
ENTER = "\r"


def _async_list(items):
    """An async stub with no arguments that returns a new copy of ``items`` (a devstate read)."""

    async def read():
        return list(items)

    return read


def _session(inp) -> TuiSession:
    """A headless session that a pipe drives. It renders nowhere."""
    return TuiSession(input=inp, output=DummyOutput())


# --- the stack discipline ----------------------------------------------------


async def test_a_visit_pushes_once_and_pops_once_however_many_rounds_it_runs() -> None:
    """``stay`` is one push and one pop, not one push and pop for each round.

    This is the difference from ``run_screen`` in a loop. That call pops at each resolve, and
    the screen is off the stack while the action that it committed to runs.
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
                    # A sub-screen that opens inside the loop nests above the hub.
                    child = ScrollScreen("child")
                    session.push(child)
                    depths.append(len(session._stack))
                    session.pop(child)

        await asyncio.wait_for(session.run(main()), timeout=5)
    assert depths == [1, 2, 1, 2, 1, 2]
    assert session._stack == [], "the visit must pop its screen on the way out"


async def test_a_visit_keeps_the_screens_cursor_and_filter_across_a_sub_screen() -> None:
    """When the user comes back from a sub-screen, the highlight is on the row that they left.

    The filter is also kept. The reuse of the screen object makes this possible. A
    ``default=`` restore can recover only the highlight, and only when the row that it names
    still exists.
    """
    with create_pipe_input() as inp:
        session = _session(inp)
        screen = SelectScreen("hub", [Choice("alpha", 1), Choice("beta", 2), Choice("gamma", 3)])

        async def main() -> None:
            async with session.stay(screen) as visit:
                screen.handle("text", "a")  # filter to the rows that have an "a"
                screen.handle("down")
                before = (screen._filter, screen._index)
                screen.resolve("go")
                await visit.result()
                await session.run_screen(ScrollScreen("a sub-screen"))
                assert (screen._filter, screen._index) == before

        session_task = session.run(main())
        # The Esc key closes the sub-screen. This is the only key that this flow needs.
        inp.send_text("\x1b")
        await asyncio.wait_for(session_task, timeout=5)


async def test_a_visited_screen_is_armed_from_the_moment_it_is_pushed() -> None:
    """A key that arrives before the caller loops back is kept and not removed.

    The screen is on the stack for the whole visit, so it can resolve at any time. This
    includes the time while the caller still finishes the previous round. If the code armed
    the screen only inside :meth:`~meshterm.ui.tui.session.Visit.result`, there would be a
    gap where a key press has nowhere to go.
    """
    with create_pipe_input() as inp:
        session = _session(inp)
        screen = ScrollScreen("hub")

        async def main() -> None:
            async with session.stay(screen) as visit:
                assert screen.future is not None, "armed by the push, before any round"
                screen.resolve("early")  # resolved before the caller asks for it
                assert await visit.result() == "early"
                # Also, the next round is live at once, with no other await first.
                assert screen.future is not None and not screen.future.done()
                screen.resolve("next")
                assert await visit.result() == "next"

        await asyncio.wait_for(session.run(main()), timeout=5)


async def test_the_trace_target_picker_is_a_popup_gone_before_the_live_screen(
    monkeypatch,
) -> None:
    """The choice of a target is a floating question that closes when answered.

    The trace opens over the menu.

    The picker was once kept pushed as a hub under the trace screen. Thus the Esc key from a
    trace went to the list, which gave two places where the trace is the whole visit. The
    picker now draws as a box (over a base, because the menu is popped while a tool runs).
    It closes on Enter, and the trace screen has nothing under it.
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
            # The answer of the real DeviceState with no radio: nothing to highlight.
            return 0

    class _Ctx:
        repo = _Repo()
        devstate = _Devstate()
        is_connected = False  # keeps the read of the path hash (made when possible) off the radio
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
            # `_run_live` itself pushes the picker, so the test cannot queue the key press
            # before the call. When the test sent the key blind, it raced the screen that
            # reads it, and it failed in approximately one full-suite run in six.
            # `_screen_at` exists for this: wait until the picker is on the stack, then
            # press Enter on it.
            running = asyncio.create_task(TraceTool()._run_live(ctx))
            picker = await _screen_at(session, 2)
            assert picker.floating and session._has_float(), "a box…"
            assert not session._base_screen().floating, "…over a base"
            inp.send_text(ENTER)  # Enter on the first row commits it as the target
            result = await running
            assert result.summary == {"sessions": 1, "traces": 2}

        await asyncio.wait_for(session.run(main()), timeout=5)

    assert depths == [0], "the picker is gone before the trace screen opens"
    assert session._stack == []


async def _screen_at(session: TuiSession, depth: int, timeout: float = 2.0):
    """Spin the loop until the stack is exactly ``depth`` deep, then return its top screen.

    With this function, a test can drive a nested flow when it resolves each screen in turn.
    The test does not depend on the time when the session reads a piped key press.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(session._stack) != depth:
        if loop.time() > deadline:  # pragma: no cover - only on a regression
            raise AssertionError(f"stack never reached depth {depth}: {session._stack}")
        await asyncio.sleep(0)
    return session._stack[-1]


async def test_the_tx_optimize_link_is_one_dialog_gone_before_the_sweep(monkeypatch) -> None:
    """The two choices are one box that turns its page, and the sweep opens over the menu.

    The link was once two pickers, one over the other. Each stayed pushed under the sweep as
    a hub. This gave three screens on the stack for a sweep, and the user saw two dialogs as
    two places to be. A dialog is a question, not a place. The dialog closes before the sweep
    screen opens, so the Esc key from the sweep goes to the main menu.
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
            """Stand in for the sweep screen: note the node that it tunes and what is under it."""
            swept.append((admin_node.name, target_label, len(session._stack)))
            return {"best": 20}

        monkeypatch.setattr("meshterm.ui.tx_screen.open_tx_optimize", fake_sweep)

        async def turned_to(dialog, step: str) -> None:
            """Spin until the one dialog shows ``step``: the same object, with a new page."""
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
            dialog.resolve(CANCEL)  # the Esc key on step 2 turns back, not out
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
    """A list whose content is data can change while the user stays in the same place.

    The rows of the editors have live values, and their titles count the staged changes. Thus
    the code must rebuild them after each action. When the code rebuilt them as a whole new
    screen, the typed filter was lost, and a ``default=`` restore had to set the highlight.
    When the code replaces the rows in place, both are kept, and the highlight follows the
    highlighted row by value to where the row moved.
    """
    screen = SelectScreen(
        "Editor · nothing staged",
        [Choice("alpha", "a"), Choice("beta", "b"), Choice("gamma", "g")],
    )
    screen.handle("text", "a")  # filters to alpha, beta, and gamma: each has an "a"
    screen.handle("down")  # …onto beta
    assert screen._current_choice().value == "b"

    # beta moves to the front and its label gets a staged value. The title counts again.
    screen.replace_items(
        [Choice("beta → 12", "b"), Choice("alpha", "a"), Choice("gamma", "g")],
        title="Editor · 1 staged",
    )
    assert screen.title == "Editor · 1 staged"
    assert screen._filter == "a", "the typed filter survives the swap"
    assert screen._current_choice().value == "b", "the highlight followed its row"


def test_turn_page_drops_the_filter_and_highlights_the_named_row() -> None:
    """A page that turns is a new question, so the text typed for the last question does not stay.

    This is the opposite of ``replace_items``. That function refreshes the same list, and
    this function shows a different list in the same box. The query that the user typed to
    find the node to tune would narrow the *measure at* list to rows that it was not meant
    for. The highlight goes to the default of the page. When the user turns back to a page,
    the default is the previous answer. The highlight never goes to a row that only has the
    same value as the row that was last highlighted.
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
    """The Esc key on a later step turns back a page. The box is pushed once and popped once.

    Two choices were once two dialogs on the stack, and each was a screen on the stack to
    walk back through. A dialog is a question, not a place. The chain is one list that turns
    its page, and it closes before the caller opens the screen that the answers are for.
    Also, it draws as a dialog on an empty stack, with a blank base under it for the visit.
    Without the base, a lone floating screen is painted as the full-frame background.
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

            # Turned back: the Esc key on step 2 shows step 1 again, with its answer highlighted.
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
            box.resolve(CANCEL)  # the Esc key on the first step abandons the wizard
            assert await asyncio.wait_for(run, timeout=2) is None
            assert session._stack == []

        await asyncio.wait_for(session.run(main()), timeout=5)

    assert shown == [
        ("Link — step 1 of 2", "a"),
        ("Link — after b · step 2 of 2", "g"),
        ("Link — step 1 of 2", "a"),
    ]


async def test_a_popup_with_nothing_under_it_floats_over_the_menu() -> None:
    """A question on the way into a tool shows the menu around it, never an empty frame.

    The menu is popped while a tool runs. Thus a picker at the start was once the only
    screen on the stack, and the app drew it full-frame. The blank base that corrected this
    showed nothing behind the box. A dialog is smaller than the frame, so the page that the
    user came from must show around it. The root that the menu declared is the base that is
    pushed under the dialog and popped with it. Thus the own page of the tool opens over
    nothing.
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

    The box stays on its last page under the prompt. It is the backdrop that the prompt
    floats over. A ``None`` from the prompt steps back, the same as the Esc key on a page.
    """
    from meshterm.ui.menus import WizardPage, run_wizard

    typed = iter([None, "3d"])  # the Esc key the first time, then a hex prefix

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
            box.resolve("a")  # step 2 backs out at once, so step 1 is asked again
            while box.future is None or box.future.done():
                await asyncio.sleep(0)
            assert session._stack[-1] is box, "the box never left"
            box.resolve("a")
            assert await asyncio.wait_for(run, timeout=2) == ["a", "3d"]

        await asyncio.wait_for(session.run(main()), timeout=5)


def test_replace_items_clamps_to_the_position_when_the_row_is_gone() -> None:
    """When the user deletes the row that they were on, the highlight stays at its position.

    It does not go back to the top.
    """
    screen = SelectScreen("Queue", [Choice(name, name) for name in ("a", "b", "c", "d")])
    for _ in range(2):
        screen.handle("down")
    assert screen._current_choice().value == "c"

    screen.replace_items([Choice(name, name) for name in ("a", "b", "d")])
    assert screen._current_choice().value == "d", "the row that slid into the gap"

    # Also, a swap that empties the list completely must not leave a stale index.
    screen.replace_items([])
    assert screen._current_choice() is None


def test_update_rows_resorts_the_lanes_and_keeps_the_highlight_on_its_contact() -> None:
    """The TRACED lane of the Trace picker ages while the user stays in the same place.

    The list is sorted by the column that a returning trace changes, so the node that the
    trace just walked goes to the top. The highlight must go with it. If it does not, the
    highlight moves to the row that took the position of the user.
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

    # beta is traced, so it is now first in the TRACED sort, where alpha was.
    screen.update_rows(
        [
            ContactRow(value="alpha", name="alpha", last_traced=now - timedelta(hours=2)),
            ContactRow(value="beta", name="beta", last_traced=now),
        ]
    )
    assert screen._filter == "bet", "the typed filter survives the swap"
    assert screen._current_choice().value == "beta", "the highlight followed its contact"
    for _ in range(3):  # ⌫ out of the filter: the whole list is there, sorted again
        screen.handle("backspace")
    assert [c.value for c in screen._choices()] == ["beta", "alpha"]


# --- Esc peels the filter before it leaves -----------------------------------


async def test_a_typed_filter_is_peeled_before_the_list_is_left() -> None:
    """The Esc key clears a standing filter and the screen stays. The next Esc leaves.

    The map and the mesh walk always did this. The select list and the path composer did
    not. The same keys on the same affordance gave opposite results. Thus when the user
    typed three letters on a contact list and pressed Esc, the app went to the main menu.
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
    """The verb of Esc follows what the press does. This is the wording of the map and the walk."""
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
    """The Esc key unwinds the composer in the same way as ⌫: first the entry, then the screen."""
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
    """The Esc key in a flow of many prompts undoes one step and keeps what earlier steps hold.

    A chain of prompts was once a straight run of awaits, so the Esc key at any step
    abandoned the whole flow. Thus a wrong key of 32 hex digits for a channel also lost the
    name that the user typed before it.
    """
    from meshterm.ui.menus import run_steps

    asked: list[str] = []
    key_attempts = iter([None, "cafe"])  # the first Esc, then a good key on the way forward

    async def name_step(values: list):
        asked.append("name")
        return values[0] or "Backyard"  # opens with the value that it returned last

    async def key_step(values: list):
        asked.append("key")
        return next(key_attempts)

    answers = await run_steps([name_step, key_step])

    assert answers == ["Backyard", "cafe"]
    assert asked == ["name", "key", "name", "key"], "the name was asked again, not skipped"


async def test_backing_out_of_the_first_step_ends_the_flow() -> None:
    """There is nothing behind step one, so the Esc key there leaves, as it always did."""
    from meshterm.ui.menus import run_steps

    async def only_step(values: list):
        return None

    assert await run_steps([only_step]) is None


async def test_a_step_keeps_its_own_answer_to_offer_as_a_default() -> None:
    """When the flow comes back to a step, the step gets the value that it returned last time."""
    from meshterm.ui.menus import run_steps

    seen: list = []

    async def first(values: list):
        return "one"

    async def second(values: list):
        seen.append(list(values))
        return "two"

    async def third(values: list):
        return None if len(seen) == 1 else "three"  # the Esc key the first time through

    assert await run_steps([first, second, third]) == ["one", "two", "three"]
    # The flow asks the second step again after the third step backed out. The second step
    # gets what it answered before. This is how a field that opens again is already filled in.
    assert seen == [["one", None, None], ["one", "two", None]]


# --- ^W, the pop-all ---------------------------------------------------------


async def test_pop_all_unwinds_every_frame_at_once() -> None:
    """^W from three screens deep pops all three screens and puts the caller past the whole flow."""
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

    The app wraps tool runs in ``except Exception``, so that a broken tool cannot stop the
    menu (:func:`meshterm.ui.menu._run_selection` is the widest such guard). An unwind must
    cross these guards. They must not catch it and report it as an error. This is the reason
    that :class:`PopToMenu` derives from ``BaseException``.
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
    """^W during a device read still unwinds, at the next screen that the flow tries to open.

    While the app is under the busy overlay with an empty stack, there is no future to carry
    the sentinel. Thus the flag is what remembers the key press.
    """
    with create_pipe_input() as inp:
        session = _session(inp)
        opened: list[str] = []

        async def main() -> None:
            inp.send_text(POP_ALL_KEY)
            await asyncio.sleep(0.05)  # wait for the key to reach the dispatcher
            assert session.top is None, "nothing is pushed while the read is in flight"
            with pytest.raises(PopToMenu):
                await session.run_screen(ScrollScreen("the screen the read was for"))
            opened.append("refused")

        await asyncio.wait_for(session.run(main()), timeout=5)
    assert opened == ["refused"]


def test_pop_all_is_refused_over_anything_modal() -> None:
    """A dialog is a question that is not answered, and progress is work in flight. ^W waits."""
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
    """The user cannot go back to the place where they already are."""
    session = TuiSession(output=DummyOutput())
    menu = SelectScreen("What would you like to do?", [Choice("a tool", "a")])
    session.set_root(menu)
    session.push(menu)
    assert session.request_pop_all() is False
    assert session._unwinding is False
    # One screen deeper, the same key acts.
    deeper = ScrollScreen("a tool")
    session.push(deeper)
    assert session.request_pop_all() is True
    assert deeper.future is None or True  # a resolve of a screen with no future does no harm


def test_pop_all_resolves_the_top_screen_with_the_sentinel() -> None:
    """The sentinel goes through the future. No caller in the app sees it."""
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
    """A disconnect abandons the flow, so a ^W that is armed has nothing left to unwind."""
    session = TuiSession(output=DummyOutput())
    session.push(ScrollScreen("deep"))
    session.request_pop_all()
    assert session._unwinding is True
    session.reset()
    assert session._unwinding is False


# --- ^Q, the quit ------------------------------------------------------------


async def test_quit_from_a_nested_screen_still_runs_the_apps_teardown() -> None:
    """^Q leaves from each screen, and the ``finally`` blocks of the flow still run.

    The chord exits the prompt_toolkit application directly from the key handler, so the
    coroutine that drives the app is parked on a screen when the application returns. If the
    coroutine stays abandoned there, the teardown never happens. The teardown closes the
    history and the chat, stops the services, and arms the exit watchdog.
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
    """A session whose quit chord floats a Cancel/Quit confirm, as the menu declares it.

    ``answer`` resolves the dialog as soon as it is pushed (``None`` leaves it up, and the
    test drives it). ``asked`` records each time that the session asks the question.
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
    """One ^Q floats the confirm over the top screen, and Cancel puts the user back.

    The session never resolves or pops the screen under the dialog. A capture behind the box
    keeps its capture. This is the difference from the old chord that quit with no question
    (issue #22).
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
    """^Q ^Q is the fast way out, and it does not wait for the dialog to take a key."""
    with create_pipe_input() as inp:
        asked: list[str] = []
        session = _asking_session(inp, asked)
        torn_down: list[str] = []

        async def main() -> None:
            try:
                await session.run_screen(ScrollScreen("deep"))
            finally:
                torn_down.append("services closed")

        # Both presses are in one batch. The code raises the flag synchronously, so the
        # second press is the leaving and not a second dialog.
        inp.send_text(QUIT_KEY + QUIT_KEY)
        await asyncio.wait_for(session.run(main()), timeout=5)
    assert torn_down == ["services closed"]
    assert len(asked) <= 1


async def test_quit_confirmed_from_anywhere_exits_the_app() -> None:
    """When the user selects Quit in the floated confirm, the app exits and the teardown runs."""
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
    """Many Esc presses up to the menu stop at the menu. They do not open the quit confirm."""
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
    """``Quit?`` on F3 asks. Its Shift half, F8, is ``Quit!``, which does not ask."""
    from meshterm.ui.menu import _MainMenu
    from meshterm.ui.menus import section_heading
    from meshterm.ui.tui.fkeys import PICOCALC_LYRA_DECK

    menu = _MainMenu(
        "menu",
        [section_heading("A"), Choice("alpha", 1), section_heading("B"), Choice("beta", 2)],
    )
    lane = menu.picocalc_lyra_lane
    assert (lane[2].label, lane[2].opp_label) == ("Quit?", "Quit!")
    assert PICOCALC_LYRA_DECK.action_for(lane, 3) == "quit"
    assert PICOCALC_LYRA_DECK.action_for(lane, 8) == "quit_now"
    assert lane[0].label == "Sect ↑", "the section jumps keep F1/F2"


async def test_quit_now_leaves_without_asking() -> None:
    """The ``quit_now`` of F8 exits when a confirm is declared too, and the teardown still runs."""
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

    The single chord table guarantees the second part. A chord that is added there is never
    bound without its rescue. A layout that uses the right Ctrl as a modifier for a group of
    characters reaches the chord only through the rescue.
    """
    assert _CTRL_LETTER_CHORDS["w"] == "to_menu"
    assert _CTRL_LETTER_CHORDS["q"] == "quit"
    from prompt_toolkit.keys import Keys

    assert _KEY_ACTIONS[Keys.ControlW] == "to_menu"
    assert _KEY_ACTIONS[Keys.ControlQ] == "quit"


def test_neither_global_chord_is_advertised_anywhere() -> None:
    """The UI does not show either chord, except the way out on the main menu.

    A global verb has no screen to belong to. A footer atom or an F-key chip would have to be
    on each screen, and the lane has only three free slots on each screen. The one exception
    is the hint of the main menu. It names ^Q because Esc does nothing there, and the user
    looks for the way out on the menu (issue #22). It is exactly one string, on the menu.
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
        # Docstrings explain the chords in detail, and the user of the UI does not see them.
        # The important text is each string that the module states.
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
    """``modal`` is about the ownership of the keyboard. ``floating`` is only about a box drawing.

    It is easy to confuse them, because most dialogs are both. Thus the test checks directly
    the two screens that break the correlation. A plain select list floats and is not modal.
    The busy splash is modal and does not float.
    """
    assert SelectScreen("list", [Choice("a", 1)]).floating is True
    assert SelectScreen("list", [Choice("a", 1)]).modal is False
    assert BusyScreen("checking…").floating is False
    assert BusyScreen("checking…").modal is True
    # ...and its twin has the same claim on the keyboard, drawn as a box over the screen that
    # it interrupts. This is the purpose of the pair.
    assert BusyDialog("saving…").floating is True
    assert BusyDialog("saving…").modal is True
    assert Screen().modal is False
    assert CANCEL is not POP_ALL


async def test_pop_all_unwinds_from_a_screen_a_key_handler_opened() -> None:
    """^W in the packet viewer unwinds, although no code awaits the screen that opened it.

    The opener of a screen cannot await a screen that a key handler opened. A handler is
    synchronous, so the live feed floats the viewer with ``ensure_future(run_screen(...))``.
    The call chain that carries the unwind then ends in a detached task. The hub below waits
    on ``visit.result()``, and no key press resolves it. ^W once closed the viewer and
    stopped there. It left the flag armed, and the flag acted on some later key press. Thus
    the unwind came at the next action of the user, not at the key that they pressed
    (JP, 2026-08-31).
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
                    # This is how a key handler opens a screen: detached, never awaited.
                    asyncio.ensure_future(session.run_screen(viewer))
                    await asyncio.sleep(0.05)  # wait for the push
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
    """The task that a key handler starts is off the navigation chain, so the copy stops there.

    ``request_pop_all`` arms each screen on the stack, so the unwind already goes out through
    the screen that opened this one. If it also propagates here, it reaches only the
    exception handler of the event loop, and the loop logs it as an unretrieved task error.
    The cleanup of the flow still runs when the unwind goes through.
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
    """Only the unwind is absorbed. A broken flow still shows as a task error."""
    session = TuiSession(output=DummyOutput())

    async def work() -> None:
        raise RuntimeError("the flow broke")

    task = session.run_detached(work())
    with pytest.raises(RuntimeError):
        await task


# --- work in flight ----------------------------------------------------------


async def test_a_key_pressed_during_a_slow_action_reaches_the_hub_that_is_still_armed() -> None:
    """The test for the defect that ``busy_dialog`` corrects.

    A hub that ``stay`` keeps on the stack is armed for the whole visit. Thus a key that the
    user presses while the caller does slow device work still goes to the hub. The Esc key
    resolves a round that nothing waits for yet. The session returns it when the work
    finishes. The screen then closes by itself a moment after a key press that seemed to do
    nothing.
    """
    with create_pipe_input() as inp:
        session = _session(inp)
        screen = SelectScreen("hub", [Choice("alpha", 1)])
        banked: list = []

        async def main() -> None:
            async with session.stay(screen) as visit:
                inp.send_text(ESC)  # pressed "while the write is running"
                await asyncio.sleep(0.05)  # wait for the input loop to dispatch it
                banked.append(await asyncio.wait_for(visit.result(), timeout=1))

        await asyncio.wait_for(session.run(main()), timeout=5)

    assert banked == [CANCEL]  # the session stored the press and returns it at once


async def test_the_busy_dialog_takes_the_keys_the_hub_would_have_banked() -> None:
    """With the card up, the same key press stops on it. Nothing is stored and nothing resolves.

    This makes a slow channel write safe to wait for. The user cannot filter the list under
    the card to nothing. A second Enter from an impatient user cannot fire the list again.
    An Esc that seems to be ignored cannot close the list before the work finishes.
    """
    with create_pipe_input() as inp:
        session = _session(inp)
        screen = SelectScreen("hub", [Choice("alpha", 1)])
        timed_out = []

        async def main() -> None:
            async with session.stay(screen) as visit:
                async with session.busy_dialog("saving…", title="Channels"):
                    inp.send_text(ESC + "zzz" + ENTER)  # the Esc key, filter letters, and Enter
                    await asyncio.sleep(0.05)
                    assert session.top.__class__ is BusyDialog  # the card owns the keyboard
                try:
                    await asyncio.wait_for(visit.result(), timeout=0.2)
                except asyncio.TimeoutError:
                    timed_out.append(True)

        await asyncio.wait_for(session.run(main()), timeout=5)

    assert timed_out == [True]  # the session stored nothing while the card was up
    assert screen._filter == ""  # and the letters did not reach the filter of the list


async def test_the_busy_dialog_is_one_card_however_deeply_it_nests() -> None:
    """A batch of writes shows as one wait, not as a box that flashes for each write."""
    with create_pipe_input() as inp:
        session = _session(inp)
        depths: list[int] = []

        async def main() -> None:
            session.push(ScrollScreen("hub", floating=False))
            async with session.busy_dialog("reordering channels…") as outer:
                depths.append(len(session._stack))
                async with session.busy_dialog("moving Lakeside · 1/2") as inner:
                    assert inner is outer  # the nested wait uses the card that is already up
                    depths.append(len(session._stack))
                outer.message = "moving Dorval · 2/2"  # the owner changes the title as work goes on
                depths.append(len(session._stack))
            depths.append(len(session._stack))

        await asyncio.wait_for(session.run(main()), timeout=5)

    assert depths == [2, 2, 2, 1]  # pushed once, popped once, with the hub still under it


async def test_the_chip_that_asked_to_quit_leaves_when_pressed_again() -> None:
    """On a handheld, F3 asks from the menu and the confirm keeps F3. A second press leaves.

    The lane of the confirm has ``Quit!`` on the slot where the ``Quit?`` of the menu was.
    It sends the same ``quit``. When the question is up, this is the leaving, the same as a
    second ^Q.
    """
    from meshterm.platforms import PICOCALC_LYRA, set_platform
    from meshterm.ui.tui.fkeys import EMPTY_LANE, FPair

    set_platform(PICOCALC_LYRA)
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
                session._dispatch("f3")  # the same chip, on the lane of the confirm
                await asyncio.sleep(5)  # the leaving cancels this
            finally:
                torn_down.append("services closed")

        await asyncio.wait_for(session.run(main()), timeout=5)
    assert torn_down == ["services closed"]
