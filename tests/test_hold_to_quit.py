# SPDX-License-Identifier: Apache-2.0
"""Hold Esc to quit, in each place where MeshTerm can tell that a key is held.

The tests cover these parts:

- The rules of the gesture (:mod:`meshterm.services.hold_to_quit`).
- The box of the session and its quiesce (:mod:`meshterm.ui.tui.holdquit`).
- The two front ends that see a key go up.
- The Esc key of a terminal, which MeshTerm watches through the keyboard.
- SIGTERM.
- The exit of the radio node, which must be inside the grace time of the Cardputer
  launcher.
"""

from __future__ import annotations

import asyncio
import signal
import struct
import sys
import threading
import time
import types
from contextlib import asynccontextmanager

import pytest
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from meshterm.core.radionode import _forget_edge_threads
from meshterm.core.transmit_lock import TransmitLock
from meshterm.emulator import framebuffer
from meshterm.emulator.devices import CARDPUTER_ZERO_DEVICE, PICOCALC_LYRA_DEVICE
from meshterm.emulator.window import EmulatorWindow
from meshterm.services import hold_to_quit
from meshterm.ui.tui import holdquit
from meshterm.ui.tui import session as session_mod
from meshterm.ui.tui.holdquit import HoldQuitDialog
from meshterm.ui.tui.screen import ScrollScreen
from meshterm.ui.tui.session import TuiSession

ESC = "\x1b"


class _Heard:
    """A listener that stores each call that it receives."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def held(self, since: float) -> None:
        self.calls.append(("held", since))

    def released(self) -> None:
        self.calls.append(("released",))

    def terminate(self) -> None:
        self.calls.append(("terminate",))


@pytest.fixture(autouse=True)
def _nobody_listening():
    """Each test starts and ends with no listener and no key held."""
    hold_to_quit._listener = None
    hold_to_quit._leaving = False
    hold_to_quit._forget()
    yield
    hold_to_quit._listener = None
    hold_to_quit._leaving = False
    hold_to_quit._forget()


@pytest.fixture
def quick(monkeypatch):
    """The times of the gesture, made shorter for the tests: the box at 0.1 s, the exit at 0.3 s."""
    monkeypatch.setattr(hold_to_quit, "DIALOG_S", 0.1)
    monkeypatch.setattr(hold_to_quit, "QUIT_S", 0.3)
    monkeypatch.setattr(holdquit, "BAR_TICK_S", 0.02)


# --- the rules --------------------------------------------------------------------------


def test_with_nobody_listening_esc_is_typed_as_it_goes_down() -> None:
    """If nothing listens, Esc is typed when it goes down.

    Before the TUI runs, or on a handheld that does not have the rule, Esc is the same as
    each other key.
    """
    typed: list[str] = []
    esc = hold_to_quit.EscKey(typed.append)
    esc.down()
    esc.down()  # the repeat of the keyboard, which always typed
    esc.up()
    assert typed == [ESC, ESC]


def test_a_tap_is_typed_on_its_release_and_a_repeat_is_part_of_the_hold() -> None:
    """A tap is typed when it comes up, and the repeats of the keyboard are one hold.

    Until the key comes up, the tap is not typed yet.
    """
    heard = _Heard()
    hold_to_quit.listen(heard)
    typed: list[str] = []
    esc = hold_to_quit.EscKey(typed.append)
    esc.down()
    esc.down()
    assert typed == [], "a held Esc is owed, not typed"
    assert [c[0] for c in heard.calls] == ["held"], "one hold, however often it repeats"
    esc.up()
    assert typed == [ESC]
    assert heard.calls[-1] == ("released",)


def test_another_key_sends_the_held_esc_ahead_of_itself_once() -> None:
    """Another key sends the held Esc ahead of itself, one time.

    The keys keep their order: Esc, then the key that the user pressed while Esc was still
    down.
    """
    hold_to_quit.listen(_Heard())
    typed: list[str] = []
    esc = hold_to_quit.EscKey(typed.append)
    esc.down()
    esc.before()
    typed.append("a")
    esc.before()
    typed.append("b")
    esc.up()
    assert typed == [ESC, "a", "b"]


def test_a_claimed_hold_types_nothing_when_let_go() -> None:
    """A claimed hold types nothing when the user lets go.

    When the box is up, the user decides about leaving the app, and not about going back.
    """
    heard = _Heard()
    hold_to_quit.listen(heard)
    assert hold_to_quit.press(now=5.0)
    assert hold_to_quit.claim(5.0)
    assert hold_to_quit.release() is False


def test_a_release_and_the_box_settle_on_exactly_one() -> None:
    """A release and the box can ask for the same hold. Exactly one of them wins."""
    hold_to_quit.listen(_Heard())
    hold_to_quit.press(now=1.0)
    assert hold_to_quit.release() is True
    assert hold_to_quit.claim(1.0) is False, "a box asking for a hold already let go"
    hold_to_quit.press(now=2.0)
    assert hold_to_quit.claim(1.0) is False, "a box asking for an earlier hold"


def test_sigterm_asks_the_listener_once_and_joins_an_exit_under_way() -> None:
    """SIGTERM asks a running session to leave one time.

    It also joins an exit that is in progress.
    """
    assert hold_to_quit.terminate() is False, "nothing to ask: the caller stops its own way"
    heard = _Heard()
    hold_to_quit.listen(heard)
    assert hold_to_quit.terminate() is True
    assert heard.calls == [("terminate",)]
    hold_to_quit.leaving()
    hold_to_quit.unlisten(heard)
    assert hold_to_quit.terminate() is True, "a SIGTERM landing on a teardown joins it"
    assert heard.calls == [("terminate",)]
    hold_to_quit.listen(heard)
    assert hold_to_quit._leaving is False, "a new session starts with nothing leaving"


# --- the session ------------------------------------------------------------------------


class _Air:
    """A substitute for the transmit lock of the device, as the quiesce of the menu holds it."""

    def __init__(self) -> None:
        self.lock = TransmitLock()
        self.held = False

    @asynccontextmanager
    async def quiet(self):
        async with self.lock.held():
            self.held = True
            try:
                yield
            finally:
                self.held = False


def _running(inp, air: _Air | None = None) -> TuiSession:
    session = TuiSession(input=inp, output=DummyOutput())
    if air is not None:
        session.set_quiesce(air.quiet)
    return session


async def _until(condition, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


async def test_a_tap_reaches_the_screen_as_esc(quick) -> None:
    """A tap reaches the screen as the Esc that it was."""
    with create_pipe_input() as inp:
        session = _running(inp)
        esc = hold_to_quit.EscKey(inp.send_text)
        results: list[object] = []

        async def main() -> None:
            async def tap() -> None:
                await asyncio.sleep(0.02)
                esc.down()
                await asyncio.sleep(0.02)
                esc.up()

            asyncio.ensure_future(tap())
            results.append(await session.run_screen(ScrollScreen("a page")))

        await asyncio.wait_for(session.run(main()), timeout=5)
    assert results == [None], "the page took the tap as its Esc and closed"


async def test_a_hold_raises_the_box_and_letting_go_undoes_it(quick) -> None:
    """A hold of one second raises the box and quiets the air.

    When the user lets go, the app undoes both.
    """
    with create_pipe_input() as inp:
        air = _Air()
        session = _running(inp, air)
        esc = hold_to_quit.EscKey(inp.send_text)
        page = ScrollScreen("a capture in progress")

        async def main() -> None:
            session.push(page)
            esc.down()
            await _until(lambda: isinstance(session.top, HoldQuitDialog))
            await _until(lambda: air.held)
            assert session.top.modal, "the box owns the keyboard"
            esc.up()
            await _until(lambda: session.top is page)
            await _until(lambda: not air.held)
            await asyncio.sleep(0.4)  # after the point where the hold quits the app
            assert not session.leaving
            assert page.future is None, "no Esc reached the page under the box"
            session.pop(page)

        await asyncio.wait_for(session.run(main()), timeout=5)


async def test_a_box_over_an_empty_stack_takes_its_backdrop_away_with_it(quick) -> None:
    """A box over an empty stack takes its backdrop away with it.

    A tool runs between two screens. The box brings a blank page to float over, and then
    both go.
    """
    with create_pipe_input() as inp:
        session = _running(inp)
        esc = hold_to_quit.EscKey(inp.send_text)

        async def main() -> None:
            esc.down()
            await _until(lambda: isinstance(session.top, HoldQuitDialog))
            assert len(session._stack) == 2, "a backdrop under the box, so it draws as a box"
            esc.up()
            await _until(lambda: session.top is None)

        await asyncio.wait_for(session.run(main()), timeout=5)


async def test_a_hold_to_the_end_leaves_and_keeps_the_air_quiet(quick) -> None:
    """If the user holds Esc to the end, the app leaves, and nothing can transmit."""
    with create_pipe_input() as inp:
        air = _Air()
        session = _running(inp, air)
        esc = hold_to_quit.EscKey(inp.send_text)
        torn_down: list[bool] = []

        async def main() -> None:
            try:
                esc.down()
                await session.run_screen(ScrollScreen("deep"))
            finally:
                torn_down.append(air.held)

        started = time.monotonic()
        await asyncio.wait_for(session.run(main()), timeout=5)
        assert time.monotonic() - started >= hold_to_quit.QUIT_S - 0.05
    assert torn_down == [True], "the teardown runs with nothing able to transmit"
    assert session.leaving
    esc.up()  # a release after the end changes nothing


async def test_sigterm_waits_for_a_transmission_under_way(quick) -> None:
    """SIGTERM waits for a transmission that is in progress.

    A scoped channel send closes its window before the app goes.
    """
    with create_pipe_input() as inp:
        air = _Air()
        session = _running(inp, air)
        order: list[str] = []

        async def transmission() -> None:
            async with air.lock.held():
                order.append("sending")
                assert hold_to_quit.terminate()
                await asyncio.sleep(0.15)
                order.append("scope restored")

        async def main() -> None:
            try:
                asyncio.ensure_future(transmission())
                await session.run_screen(ScrollScreen("chat"))
            finally:
                order.append("teardown")

        await asyncio.wait_for(session.run(main()), timeout=5)
    assert order == ["sending", "scope restored", "teardown"]


async def test_a_quiesce_that_never_takes_hold_does_not_keep_the_app(quick, monkeypatch) -> None:
    """A quiesce that never takes hold does not keep the app.

    A transmission that never ends cannot stop the app from leaving.
    """
    monkeypatch.setattr(session_mod, "QUIESCE_WAIT_S", 0.1)
    with create_pipe_input() as inp:
        air = _Air()
        session = _running(inp, air)

        async def wedged() -> None:
            async with air.lock.held():
                await asyncio.sleep(60)

        async def main() -> None:
            asyncio.ensure_future(wedged())
            await asyncio.sleep(0)
            hold_to_quit.terminate()
            await session.run_screen(ScrollScreen("chat"))

        await asyncio.wait_for(session.run(main()), timeout=5)
    assert session.leaving


def test_the_box_s_bar_runs_from_its_appearance_to_the_quit() -> None:
    """The bar is empty when the box appears and full when the app leaves."""
    now = [100.0]
    dialog = HoldQuitDialog(100.0, clock=lambda: now[0])
    assert dialog.fraction == 0.0
    now[0] = 100.0 + hold_to_quit.DIALOG_S
    assert dialog.fraction == 0.0
    now[0] = 100.0 + (hold_to_quit.DIALOG_S + hold_to_quit.QUIT_S) / 2
    assert dialog.fraction == pytest.approx(0.5)
    now[0] = 100.0 + hold_to_quit.QUIT_S + 1
    assert dialog.fraction == 1.0
    assert dialog.fkey_lane is not None
    dialog.handle("enter")  # no key has an effect on it


# --- the Esc key of a terminal, watched through the keyboard -------------------------------------


class _Keyboard:
    """The physical state of Esc, as a probe reports it."""

    def __init__(self) -> None:
        self.esc = False

    def down(self) -> bool:
        return self.esc


def _watched(inp, keyboard: _Keyboard, air: _Air | None = None) -> TuiSession:
    session = _running(inp, air)
    session.set_esc_probe(keyboard.down)
    return session


class _Keys(ScrollScreen):
    """A page that stores each key that it receives."""

    def __init__(self) -> None:
        super().__init__("a page")
        self.got: list[str] = []

    def handle(self, action: str, data: str = "") -> None:
        self.got.append(data or action)


async def test_a_terminal_s_esc_already_let_go_is_an_esc_at_once(quick) -> None:
    """An Esc of a terminal whose key is already up is an ordinary Esc."""
    with create_pipe_input() as inp:
        keyboard = _Keyboard()
        session = _watched(inp, keyboard)
        page = _Keys()

        async def main() -> None:
            session.push(page)
            session._dispatch("escape")
            assert page.got == ["escape"]
            session.pop(page)

        await asyncio.wait_for(session.run(main()), timeout=5)


async def test_a_terminal_s_esc_still_down_is_held_until_it_comes_up(quick) -> None:
    """An Esc of a terminal that is still down is held back.

    MeshTerm delivers it when the key comes up.
    """
    with create_pipe_input() as inp:
        keyboard = _Keyboard()
        session = _watched(inp, keyboard)
        page = _Keys()

        async def main() -> None:
            session.push(page)
            keyboard.esc = True
            session._dispatch("escape")
            session._dispatch("escape")  # the repeat of the keyboard
            assert page.got == []
            keyboard.esc = False
            await _until(lambda: page.got == ["escape"])
            await asyncio.sleep(0.05)
            assert page.got == ["escape"], "one press, however often it repeated"
            session.pop(page)

        await asyncio.wait_for(session.run(main()), timeout=5)


async def test_a_key_typed_during_a_terminal_s_hold_follows_the_esc(quick) -> None:
    """A key that the user types while the Esc of a terminal is held goes in behind the Esc."""
    with create_pipe_input() as inp:
        keyboard = _Keyboard()
        session = _watched(inp, keyboard)
        page = _Keys()

        async def main() -> None:
            session.push(page)
            keyboard.esc = True
            session._dispatch("escape")
            session._dispatch("text", "a")
            assert page.got == ["escape", "a"]
            keyboard.esc = False
            await asyncio.sleep(0.1)
            assert page.got == ["escape", "a"]
            session.pop(page)

        await asyncio.wait_for(session.run(main()), timeout=5)


@pytest.mark.parametrize(("vt", "late"), [(True, []), (False, ["escape"])])
async def test_a_terminal_s_held_esc_raises_the_box_and_a_late_repeat_is_dropped(
    quick, monkeypatch, vt: bool, late: list[str]
) -> None:
    """A held Esc of a terminal raises the box, and the app removes a late repeat.

    A VT stream can have its last repeat parsed after the key is up. Windows never does
    this.
    """
    monkeypatch.setattr(holdquit, "VT_INPUT", vt)
    with create_pipe_input() as inp:
        keyboard = _Keyboard()
        air = _Air()
        session = _watched(inp, keyboard, air)
        page = _Keys()

        async def main() -> None:
            session.push(page)
            keyboard.esc = True
            session._dispatch("escape")
            await _until(lambda: isinstance(session.top, HoldQuitDialog))
            await _until(lambda: air.held)
            session._dispatch("escape")  # a repeat, under the box
            keyboard.esc = False
            await _until(lambda: session.top is page)
            await _until(lambda: not air.held)
            session._dispatch("escape")  # the last repeat of a VT stream, or a new press
            assert page.got == late, "letting go of a hold goes back nowhere"
            session.pop(page)

        await asyncio.wait_for(session.run(main()), timeout=5)


async def test_a_terminal_s_esc_held_to_the_end_leaves(quick) -> None:
    """An Esc of a terminal that is held to the end leaves, as an Esc that MeshTerm owns does."""
    with create_pipe_input() as inp:
        keyboard = _Keyboard()
        session = _watched(inp, keyboard)

        async def main() -> None:
            keyboard.esc = True
            session._dispatch("escape")
            await session.run_screen(ScrollScreen("deep"))

        await asyncio.wait_for(session.run(main()), timeout=5)
    assert session.leaving


def test_an_evdev_probe_asks_every_keyboard_with_an_esc_key(tmp_path, monkeypatch) -> None:
    """An evdev probe asks each keyboard that has an Esc key.

    The probe opens only the devices whose key bitmap has KEY_ESC. The state comes from
    EVIOCGKEY.
    """
    sysfs, dev = tmp_path / "sys", tmp_path / "dev"
    dev.mkdir()
    for name, words in (("event0", "0"), ("event1", "e 0 0 fffffffffffffffe"), ("event2", "10")):
        caps = sysfs / name / "device" / "capabilities"
        caps.mkdir(parents=True)
        (caps / "key").write_text(words + "\n")
        (dev / name).write_bytes(b"")
    asked: list[int] = []
    held = {"esc": False}

    def ioctl(fd: int, request: int, state: bytearray) -> None:
        assert request == hold_to_quit._EVIOCGKEY
        asked.append(fd)
        state[0] = 0b10 if held["esc"] else 0

    monkeypatch.setitem(sys.modules, "fcntl", types.SimpleNamespace(ioctl=ioctl))
    down = hold_to_quit._evdev_probe(sysfs, dev)
    assert down is not None
    assert down() is False
    assert len(asked) == 1, "event0 and event2 have no Esc key and were never opened"
    held["esc"] = True
    assert down() is True


def test_no_keyboard_that_opens_means_no_probe(tmp_path, monkeypatch) -> None:
    """If no keyboard opens, there is no probe, and Esc is ordinary."""
    monkeypatch.setitem(sys.modules, "fcntl", types.SimpleNamespace(ioctl=None))
    assert hold_to_quit._evdev_probe(tmp_path / "sys", tmp_path / "dev") is None


# --- the front ends ---------------------------------------------------------------------


def _events(path, *events: tuple[int, int]) -> None:
    record = struct.Struct("llHHi")
    path.write_bytes(b"".join(record.pack(0, 0, 1, code, value) for code, value in events))


def _reader(path, typed: list[str]):
    device = object.__new__(framebuffer.Device)
    device._keyboard = str(path)
    device._closing = False
    device._type = typed.append
    return device


def test_the_device_types_a_tap_on_its_release_and_keys_after_it(tmp_path) -> None:
    """The key reader of the device types a tap on release.

    It types the tap ahead of a key that the user pressed during the tap.
    """
    hold_to_quit.listen(_Heard())
    path = tmp_path / "keyboard"
    # Esc down, its repeat, A down while Esc is held, A up, Esc up.
    _events(path, (1, 1), (1, 2), (30, 1), (30, 0), (1, 0))
    typed: list[str] = []
    _reader(path, typed)._read_keys()
    assert typed == [ESC, "a"]


def test_the_device_types_esc_at_once_with_nobody_listening(tmp_path) -> None:
    """If nothing listens, the device types its Esc and the repeats at once."""
    path = tmp_path / "keyboard"
    _events(path, (1, 1), (1, 2), (1, 0))
    typed: list[str] = []
    _reader(path, typed)._read_keys()
    assert typed == [ESC, ESC]


def test_sigterm_is_a_quit_or_an_interrupt() -> None:
    """SIGTERM is a quit while the TUI runs, and an interrupt before the TUI runs."""
    before = signal.getsignal(signal.SIGTERM)
    try:
        hold_to_quit.leave_on_sigterm()
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)  # nothing runs, so there is nothing to leave
        heard = _Heard()
        hold_to_quit.listen(heard)
        handler(signal.SIGTERM, None)
        assert heard.calls == [("terminate",)]
    finally:
        signal.signal(signal.SIGTERM, before)


class _Root:
    """The timer calls of Tk, which the test runs by hand."""

    def __init__(self) -> None:
        self.pending: dict[str, object] = {}

    def after(self, _ms: int, work) -> str:
        name = f"after#{len(self.pending)}"
        self.pending[name] = work
        return name

    def after_cancel(self, name: str) -> None:
        self.pending.pop(name, None)

    def run(self) -> None:
        for name in list(self.pending):
            self.pending.pop(name)()


class _Event:
    def __init__(self, keysym: str) -> None:
        self.keysym, self.char, self.state = keysym, "", 0


def _window(device, typed: list[str]) -> tuple[EmulatorWindow, _Root]:
    root = _Root()
    window = object.__new__(EmulatorWindow)
    window._device = device
    window._tk = (root, None, None, None)
    window._type = typed.append
    window._esc = hold_to_quit.EscKey(typed.append)
    window._esc_down = False
    window._esc_letting_go = None
    window._shifted = False
    return window, root


def test_the_window_reads_an_x_server_s_repeat_as_one_hold(monkeypatch) -> None:
    """The window reads the repeat of an X server as one hold.

    X repeats a held key as release-press pairs. Only a release with no press after it
    lets go.
    """
    monkeypatch.setattr(EmulatorWindow, "_set_shift", lambda self, down: None)
    heard = _Heard()
    hold_to_quit.listen(heard)
    typed: list[str] = []
    window, root = _window(CARDPUTER_ZERO_DEVICE, typed)
    window._on_press(_Event("Escape"))
    for _ in range(3):
        window._on_release(_Event("Escape"))
        window._on_press(_Event("Escape"))
    assert root.pending == {}
    assert [c[0] for c in heard.calls] == ["held"]
    window._on_release(_Event("Escape"))
    root.run()
    assert typed == [ESC]
    assert heard.calls[-1] == ("released",)
    window._on_press(_Event("Escape"))
    window._on_focus_out(None)
    assert typed == [ESC, ESC], "an Esc still down as the window loses focus is let go"


def test_the_picocalc_window_holds_esc_too() -> None:
    """The gesture belongs to MeshTerm, and not to one launcher. Each emulated device accepts it."""
    hold_to_quit.listen(_Heard())
    typed: list[str] = []
    window, root = _window(PICOCALC_LYRA_DEVICE, typed)
    window._on_press(_Event("Escape"))
    assert typed == [], "held, not yet a press"
    window._on_release(_Event("Escape"))
    root.run()
    assert typed == [ESC]


# --- the radio node's exit --------------------------------------------------------------


def test_the_node_skips_the_wait_on_its_irq_thread() -> None:
    """The node does not wait for its IRQ thread.

    The library waits 2 s for a thread that is in a poll of 30 s. The node does not wait.
    """

    class _Manager:
        def __init__(self) -> None:
            self._edge_stop_events = {23: threading.Event()}
            self._edge_threads = {23: object()}

    class _Radio:
        _gpio_manager = _Manager()

    radio = _Radio()
    _forget_edge_threads(radio)
    assert radio._gpio_manager._edge_stop_events[23].is_set()
    assert radio._gpio_manager._edge_threads == {}
    _forget_edge_threads(object())  # a library that keeps them elsewhere is not changed
