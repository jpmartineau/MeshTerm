# SPDX-License-Identifier: Apache-2.0
"""Hold Esc to quit: a way out of MeshTerm from any screen, where MeshTerm can see a held key.

* **A tap is Esc.** If the user releases the key within :data:`DIALOG_S`, the screen gets
  one Esc, at the release. MeshTerm waits for the release, because otherwise one gesture
  can go back one screen and then quit: two actions for one gesture. If the user presses
  another key while Esc is still down, MeshTerm sends the Esc first. Thus the keys stay in
  their order.
* **After one second, a dialog shows the hold.** It says ``Hold Esc to quit``, and has a
  bar that fills over the two seconds that remain until :data:`QUIT_S`. While the dialog
  is open, MeshTerm starts nothing new: it takes the transmit lock. Thus nothing new is
  transmitted, and a scoped channel send that is in progress finishes first (refer to
  :mod:`~meshterm.core.transmit_lock`). MeshTerm closes nothing.
* **If the user releases the key before the bar is full**, the dialog closes and MeshTerm
  releases the lock. No Esc arrives: the user decided not to quit, and did not ask to go
  back.
* **If the user holds the key to the end**, MeshTerm quits as Quit does, without a
  question.

**This feature works only where MeshTerm can know when a key goes up.** A terminal never
reports this. The front ends of the emulator read the keys themselves (the event stream of
the Cardputer Zero, the key events of the window). Thus they own the Esc key: they report
when it goes down and up, and they type it only when it is a press (:class:`EscKey`). For
the Esc key of a terminal, two systems answer the question "is Esc down now?":

* Windows (``GetAsyncKeyState``), and
* Linux, where the event device of the keyboard can be opened (``EVIOCGKEY``: the
  PicoCalc, a uConsole, and each machine whose user is in ``input``).

There, the session holds back an Esc that arrives while the key is still down
(:func:`esc_probe`, :class:`~meshterm.ui.tui.holdquit.WatchedEsc`). On other systems
(macOS, and each session over SSH, whose keyboard is on another machine), Esc does what it
always did.

**Why three seconds:** this is the rule of the Cardputer Zero itself. Its launcher
continues to read the keyboard while an app runs. A held Esc is its way out of the app that
runs (``cp0_esc_exit_policy.hpp`` in CardputerZero/launcher, read 2026-10-06). When Esc is
down for three seconds, the launcher sends SIGTERM to the process group of the app. Three
seconds after that, it sends SIGKILL, if Esc is still down or not.

The hint of the launcher never gets to the display, because the launcher stops its drawing
while an app owns the display. Thus only the app can show what will occur, and the app must
exit before the SIGKILL. (The packaging guide of M5 still gives an older rule: Home held for
five seconds, then SIGINT. The code does not follow that rule now.) MeshTerm uses the
timing of the launcher on all platforms. On the Cardputer, the SIGTERM of the launcher
arrives when the bar becomes full. It joins that exit, and does not interrupt it
(:func:`terminate`, :func:`leave_on_sigterm`).

The session knows the screens, and a front end or a probe knows the key. This module
connects the two, as :mod:`~meshterm.services.modifier_watch` does for Shift. The session
listens while it runs. When nothing listens (before the TUI starts, or after it ends), a
front end types Esc immediately, as any other key.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

#: The time (seconds) that Esc is held before the dialog opens. A tap is much shorter. One
#: second is long enough that the dialog never flashes open for a slow press.
DIALOG_S = 1.0

#: The time (seconds) that Esc is held before MeshTerm quits. This is the
#: ``TERMINATE_DELAY_MS`` of the Cardputer Zero launcher, the time at which its SIGTERM
#: arrives in any case. Thus on the Cardputer, the bar becomes full when the launcher says
#: so, and on all other platforms the gesture is the same.
QUIT_S = 3.0

#: The time (seconds) that the launcher waits after its SIGTERM before it sends SIGKILL
#: (``KILL_GRACE_MS``). The full exit of MeshTerm must fit in this time.
GRACE_S = 3.0

#: The text that a terminal sends for Esc.
ESC = "\x1b"


class Listener(Protocol):
    """The side of the running session: it is told when Esc goes down and up, and when to quit.

    Each method is called from a thread that is not the thread of the session (the key
    thread of a front end, or the main thread in a signal handler). Thus each method gives
    its work to the loop of the session and returns immediately.
    """

    def held(self, since: float) -> None:
        """Esc went down at ``since`` (``time.monotonic``), and the session watches it."""

    def released(self) -> None:
        """Esc came up."""

    def terminate(self) -> None:
        """Quit now, as a hold that came to its end does (SIGTERM)."""


_lock = threading.Lock()
_listener: Listener | None = None
#: The time at which the held Esc went down, or ``None`` while it is up (or while nothing
#: watches it).
_since: float | None = None
#: True while the TUI must still get the held Esc as a press, because it is not typed yet.
_owed = False
#: True when the dialog claimed the hold. Then the release of the key types nothing.
_claimed = False
#: True when the app started to quit. Then a SIGTERM has nothing more to ask for.
_leaving = False


def listen(listener: Listener) -> None:
    """Start to watch held Esc keys for ``listener`` (the session, when it starts to run)."""
    global _listener, _leaving
    with _lock:
        _listener = listener
        _leaving = False
        _forget()


def unlisten(listener: Listener) -> None:
    """Stop the watch for ``listener``. A hold that is in progress is forgotten."""
    global _listener
    with _lock:
        if _listener is listener:
            _listener = None
            _forget()


def leaving() -> None:
    """Note that the app started to quit: from now on, a SIGTERM joins that exit.

    This state stays until the next :func:`listen`, also after the end of the session. The
    reason is that the SIGTERM of the launcher can arrive while the app already does its
    teardown. An interrupt at that time stops the teardown before it is complete.
    """
    global _leaving
    with _lock:
        _leaving = True


def press(now: float | None = None) -> bool:
    """Esc went down, or repeated while it was held. Return True if the session watches the hold.

    Args:
        now: The time at which the key went down (``time.monotonic``). If it is not given,
            the current time.

    Returns:
        ``True`` when the session watches. Then the front end must not type the Esc. The
        Esc stays owed until :func:`release` or :func:`interrupt` says to type it.
        ``False`` when nothing listens: type Esc now, as any other key.
    """
    global _since, _owed, _claimed
    with _lock:
        listener = _listener
        if listener is None:
            return False
        if _since is not None:
            return True  # the repeat of the keyboard: one hold, for any length of time
        _since = time.monotonic() if now is None else now
        _owed, _claimed = True, False
        since = _since
    listener.held(since)
    return True


def release() -> bool:
    """Esc came up. Return True if the front end must type it now: a tap, not typed or claimed."""
    with _lock:
        listener = _listener
        if _since is None:
            return False
        tap = _owed and not _claimed
        _forget()
    if listener is not None:
        listener.released()
    return tap


def interrupt() -> bool:
    """Another key went down. Return True if the front end must type the held Esc before it."""
    global _owed
    with _lock:
        if _since is None or not _owed or _claimed:
            return False
        _owed = False
        return True


def claim(since: float) -> bool:
    """The dialog asks for the hold that started at ``since``. ``True`` if it is still held.

    After the claim, the hold is no longer a press: the release of the key types nothing.
    The session asks :data:`DIALOG_S` after the press. The answer decides the result when a
    release arrives at the same instant: exactly one of the two wins.
    """
    global _claimed
    with _lock:
        if _since != since:
            return False
        _claimed = True
        return True


def terminate() -> bool:
    """Ask the app to quit, as the SIGTERM of the launcher does. ``False`` when nothing can.

    Returns:
        ``True`` when a listening session was asked to quit, or when the app already quits
        (a second SIGTERM, or a SIGTERM that arrives during a quit in progress). ``False``
        when there is no session to ask. Then the caller must stop the process in its own
        way.
    """
    with _lock:
        listener, already = _listener, _leaving
    if already:
        return True
    if listener is None:
        return False
    listener.terminate()
    return True


def holding() -> bool:
    """True while an Esc is held and watched."""
    with _lock:
        return _since is not None


def leave_on_sigterm() -> None:
    """Accept SIGTERM as a quit: the end of a held Esc on the Cardputer, or a shutdown anywhere.

    While the TUI runs, MeshTerm quits as a held Esc does when its bar is full
    (:func:`terminate`). It quits from its own event loop, between two keys, when no
    transmission is in progress. A SIGTERM that arrives during an exit in progress (the bar
    became full a moment before) joins that exit. It does not raise an interrupt at the
    place where the main thread is at that time, because such an interrupt can stop the
    teardown before it is complete. Before the TUI starts, there is nothing to close, and
    the process stops as for an interrupt. When this function is not called on the main
    thread (MeshTerm runs in another program), it does not set the handler: the owner of
    that thread controls it, as Python requires.
    """

    def on_term(signum: int, frame: object) -> None:
        if not terminate():
            raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, on_term)


# --- ask the keyboard ---------------------------------------------------------------------

#: The virtual-key code of Windows for Esc.
_VK_ESCAPE = 0x1B
#: The key code of Linux for Esc (``KEY_ESC``), and ``EVIOCGKEY(96)``: the keys that an
#: event device holds down now, one bit for each key. 96 bytes cover all the key codes
#: (``KEY_MAX`` is 0x2ff). The number of the request is the generic ``_IOC`` encoding, which
#: is the same for ARM and x86.
_KEY_ESC = 1
_EVIOCGKEY = (2 << 30) | (96 << 16) | (ord("E") << 8) | 0x18
_SYS_INPUT = Path("/sys/class/input")
_DEV_INPUT = Path("/dev/input")


def esc_probe() -> Callable[[], bool] | None:
    """A function that asks if Esc is physically down now, or ``None`` where there is none.

    It is for the Esc of a terminal, which arrives with no information about when it goes
    up. The session asks when an Esc arrives. While the answer is yes, the Esc is a hold
    (refer to :class:`~meshterm.ui.tui.holdquit.WatchedEsc`). Windows answers for the
    keyboard of the machine (``GetAsyncKeyState``, which the right-Ctrl rescue also asks).
    Linux answers for each keyboard whose event device this user can open (``EVIOCGKEY``,
    which reads the key state and no events). On macOS, there is no probe, because macOS
    keeps the state of a key behind a permission prompt. A probe that fails answers no, and
    Esc is an ordinary key again.
    """
    if sys.platform == "win32":
        return _windows_probe()
    if sys.platform.startswith("linux"):
        return _evdev_probe()
    return None


def _windows_probe() -> Callable[[], bool] | None:
    try:
        from ..core import win32dll

        user32 = win32dll.user32()
    except Exception:  # noqa: BLE001 - with no probe, Esc is an ordinary key, not an error
        return None

    def down() -> bool:
        try:
            return bool(user32.GetAsyncKeyState(_VK_ESCAPE) & 0x8000)
        except Exception:  # noqa: BLE001 - a failed probe answers no
            return False

    return down


def _evdev_probe(root: Path = _SYS_INPUT, dev: Path = _DEV_INPUT) -> Callable[[], bool] | None:
    """A probe of each keyboard with an Esc key and an event device that opens (``EVIOCGKEY``)."""
    try:
        import fcntl
    except ImportError:
        return None
    devices: list[int] = []
    for entry in sorted(root.glob("event*")):
        try:
            words = (entry / "device" / "capabilities" / "key").read_text().split()
        except OSError:
            continue
        # The words of the key bitmap start with the highest. KEY_ESC is a bit of the last word.
        if not words or not int(words[-1], 16) & (1 << _KEY_ESC):
            continue
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            devices.append(os.open(dev / entry.name, flags))
        except OSError:
            continue
    if not devices:
        return None

    def down() -> bool:
        state = bytearray(96)
        for fd in devices:
            try:
                fcntl.ioctl(fd, _EVIOCGKEY, state)
            except OSError:
                continue
            if state[_KEY_ESC // 8] & (1 << _KEY_ESC % 8):
                return True
        return False

    return down


def _forget() -> None:
    """Forget the hold that is in progress (call with the lock held)."""
    global _since, _owed, _claimed
    _since, _owed, _claimed = None, False, False


class EscKey:
    """The Esc key of a front end, typed with the rules above.

    The front end calls :meth:`down` and :meth:`up` when the key goes down and up. It calls
    :meth:`before` before it types any other key. This class types the Esc each time that
    it is a press, and only then: immediately when nothing watches, or at the release for a
    tap.
    """

    def __init__(self, type_text: Callable[[str], None]) -> None:
        """Type through ``type_text``, which the front end uses to send keys to the TUI."""
        self._type = type_text

    def down(self) -> None:
        """Esc went down (or the keyboard repeated it)."""
        if not press():
            self._type(ESC)

    def up(self) -> None:
        """Esc came up."""
        if release():
            self._type(ESC)

    def before(self) -> None:
        """Another key will be typed: first, send an Esc that is still owed."""
        if interrupt():
            self._type(ESC)


__all__ = [
    "DIALOG_S",
    "ESC",
    "GRACE_S",
    "QUIT_S",
    "EscKey",
    "Listener",
    "claim",
    "esc_probe",
    "holding",
    "interrupt",
    "leave_on_sigterm",
    "leaving",
    "listen",
    "press",
    "release",
    "terminate",
    "unlisten",
]
