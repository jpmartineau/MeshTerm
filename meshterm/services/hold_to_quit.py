# SPDX-License-Identifier: Apache-2.0
"""Hold Esc to quit: the Cardputer Zero launcher's way out of an app, and MeshTerm's half of it.

The launcher hands an app the panel and the keyboard but goes on reading the keyboard
itself, and a held Esc is its door out of whatever is running (``cp0_esc_exit_policy.hpp``
in CardputerZero/launcher, read 2026-10-06): Esc down for **three seconds** and it sends
SIGTERM to the app's process group; **three seconds** after that, SIGKILL, whether or not
Esc is still down. Letting go after the SIGTERM changes nothing. The launcher also raises a
hint at half a second, but only as a flag: its own drawing is paused while an app owns the
panel, so the hint never reaches the glass. The app is the only one who can show what is
about to happen, and it has to be gone before the SIGKILL. (M5's packaging guide still
describes an older rule — Home held five seconds, then SIGINT — that the code no longer
follows.)

MeshTerm meets it like this:

* **A tap is Esc.** Let go within :data:`DIALOG_S` and the TUI gets the one Esc it was, on
  the release. It waits for the release because a press that went back a screen and then
  quit would be two things for one gesture. Another key pressed while Esc is still down
  sends the Esc first, so keys keep their order.
* **Held for a second, a box says so.** ``Hold Esc to quit``, with a bar that fills over
  the two seconds left until :data:`QUIT_S`, the launcher's own deadline. While it is up,
  MeshTerm stops *starting* things: it takes the transmit lock, so nothing new goes on the
  air and a scoped channel send already under way finishes first (see
  :mod:`~meshterm.core.transmit_lock`). Nothing is torn down.
* **Let go before the bar fills** and the box goes and the lock is given back. No Esc
  arrives: the reader changed their mind about leaving, not about going back.
* **Hold it to the end** and MeshTerm leaves the way Quit does. The launcher's SIGTERM
  arrives at the same moment and joins that exit rather than cutting into it
  (:func:`terminate`).

The emulator's front ends know the key — the device's event stream, the window's key
events, the only two places MeshTerm sees a key go *up* — and the session knows the
screens, so this module is where they meet, as :mod:`~meshterm.services.modifier_watch` is
for Shift. A front end reports Esc going down and up; the session listens while it runs
and does the rest. With nothing listening (before the TUI starts, after it ends) a front
end types Esc at once, like any other key.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Protocol

#: Seconds Esc is held before the box comes up. A tap is far shorter; a second is long
#: enough that the box never flashes up under a slow press.
DIALOG_S = 1.0

#: Seconds Esc is held before MeshTerm leaves: the launcher's own ``TERMINATE_DELAY_MS``,
#: when its SIGTERM lands anyway, so the bar runs out when the launcher says it does.
QUIT_S = 3.0

#: Seconds the launcher waits after its SIGTERM before it sends SIGKILL
#: (``KILL_GRACE_MS``). MeshTerm's whole exit has to fit in it.
GRACE_S = 3.0

#: What a terminal sends for Esc.
ESC = "\x1b"


class Listener(Protocol):
    """The running session's side: told when Esc goes down and up, and when to leave.

    Every method is called from a thread that is not the session's (a front end's key
    thread, or the main thread inside a signal handler), so each hands its work to the
    session's loop and returns at once.
    """

    def held(self, since: float) -> None:
        """Esc went down at ``since`` (``time.monotonic``) and is being watched."""

    def released(self) -> None:
        """Esc came up."""

    def terminate(self) -> None:
        """Leave now, the way a hold that ran its course does (SIGTERM)."""


_lock = threading.Lock()
_listener: Listener | None = None
#: When the Esc being held went down, or ``None`` while it is up (or nobody watches it).
_since: float | None = None
#: Whether the held Esc is still owed to the TUI as a press, not yet typed.
_owed = False
#: Whether the box has claimed the hold, so that letting go types nothing.
_claimed = False
#: Whether the app has begun to leave, so a SIGTERM has nothing left to ask for.
_leaving = False


def listen(listener: Listener) -> None:
    """Start watching held Esc keys for ``listener`` (the session, as it starts to run)."""
    global _listener, _leaving
    with _lock:
        _listener = listener
        _leaving = False
        _forget()


def unlisten(listener: Listener) -> None:
    """Stop watching for ``listener``; a hold under way is forgotten."""
    global _listener
    with _lock:
        if _listener is listener:
            _listener = None
            _forget()


def leaving() -> None:
    """Note that the app has begun to leave: a SIGTERM from now on joins that exit.

    Kept until the next :func:`listen`, past the session's end, since the launcher's SIGTERM
    can land while the app is already tearing down, and an interrupt there would cut the
    teardown short.
    """
    global _leaving
    with _lock:
        _leaving = True


def press(now: float | None = None) -> bool:
    """Esc went down, or repeated while held. Returns whether the hold is being watched.

    Args:
        now: When it went down (``time.monotonic``); now if not given.

    Returns:
        ``True`` when the session is watching, so the front end must not type the Esc —
        it is owed until :func:`release` or :func:`interrupt` says to type it. ``False``
        when nothing is listening: type Esc now, as any key.
    """
    global _since, _owed, _claimed
    with _lock:
        listener = _listener
        if listener is None:
            return False
        if _since is not None:
            return True  # the keyboard's repeat: one hold, however long
        _since = time.monotonic() if now is None else now
        _owed, _claimed = True, False
        since = _since
    listener.held(since)
    return True


def release() -> bool:
    """Esc came up. Returns whether to type it now: a tap, not yet typed or claimed."""
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
    """Another key went down. Returns whether to type the held Esc ahead of it."""
    global _owed
    with _lock:
        if _since is None or not _owed or _claimed:
            return False
        _owed = False
        return True


def claim(since: float) -> bool:
    """The box asks for the hold that began at ``since``. ``True`` if it is still held.

    Claimed, the hold is no longer a press: letting go types nothing. The session asks
    :data:`DIALOG_S` after the press, and the answer settles a release arriving at the same
    instant — exactly one of the two wins.
    """
    global _claimed
    with _lock:
        if _since != since:
            return False
        _claimed = True
        return True


def terminate() -> bool:
    """Ask the app to leave, as the launcher's SIGTERM does. ``False`` when nothing can.

    Returns:
        ``True`` when a listening session was asked to leave, or the app is leaving already
        (a second SIGTERM, or one landing on a quit under way); ``False`` when there is no
        session to ask, and the caller should stop the process its own way.
    """
    with _lock:
        listener, already = _listener, _leaving
    if already:
        return True
    if listener is None:
        return False
    listener.terminate()
    return True


def _forget() -> None:
    """Drop the hold under way (call with the lock held)."""
    global _since, _owed, _claimed
    _since, _owed, _claimed = None, False, False


class EscKey:
    """A front end's Esc key, typed by the rules above.

    The front end calls :meth:`down` and :meth:`up` as the key goes, and :meth:`before`
    ahead of typing any other key; this types the Esc whenever, and only when, it is a
    press — at once where nothing is watching, on the release for a tap.
    """

    def __init__(self, type_text: Callable[[str], None]) -> None:
        """Type through ``type_text``, the front end's way into the TUI."""
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
        """Another key is about to be typed: send an Esc still owed ahead of it."""
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
    "interrupt",
    "leaving",
    "listen",
    "press",
    "release",
    "terminate",
    "unlisten",
]
