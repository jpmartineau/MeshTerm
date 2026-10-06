# SPDX-License-Identifier: Apache-2.0
"""The part of hold-Esc-to-quit that the session does: the dialog, its bar, and the quiesce.

:mod:`~meshterm.services.hold_to_quit` tells what the gesture is and why it exists. This
module tells what the session does with it. :class:`EscHoldWatch` is the listener of the
session. When it is told that the Esc key went down, it does these steps:

- It waits for :data:`~meshterm.services.hold_to_quit.DIALOG_S`.
- It claims the hold.
- It floats a :class:`HoldQuitDialog` over the screen that is up. Thus nothing under the
  dialog stops.
- It engages the quiesce of the app (:class:`Quiet`).

When it is told that the Esc key came up, it closes the dialog and releases the quiesce.
If the key is held until :data:`~meshterm.services.hold_to_quit.QUIT_S`, it leaves
(:meth:`~meshterm.ui.tui.session.TuiSession.leave`).

The source of the down and up events of the Esc key depends on what reads the keys. A
front end that reads the keys itself (the front end of the emulator) reports them directly.
When a terminal delivers the keys, :class:`WatchedEsc` asks the keyboard if an Esc that
arrives is still down. If it is, :class:`WatchedEsc` holds it back and watches it until the
key comes up.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any

from rich.console import Group
from rich.text import Text

from ...persistence.logging import get_logger
from ...services import hold_to_quit
from ..braillechart import meter
from .render import render_lines
from .screen import Screen, ScrollScreen

if TYPE_CHECKING:
    from .session import TuiSession

_log = get_logger()

#: Seconds between two paints while the bar fills. The bar gets one more step after a few
#: hundredths of a second. But a frame costs real time on a handheld. Ten paints each
#: second look like a smooth fill, and most of a core stays free for the work that the app
#: must finish.
BAR_TICK_S = 0.1

#: The width of the dialog, with its border: the bar has the width of the text above it.
_WIDTH = 30


class Quiet:
    """The quiesce of the app (:meth:`~meshterm.ui.tui.session.TuiSession.set_quiesce`), held.

    The quiesce is entered in its own task, because the thing that it takes (the transmit
    lock) may have to wait until a transmission finishes, and the dialog must not wait with
    it. The quiesce is released (:meth:`release`) when the user releases the Esc key. When
    the app leaves, it is never released. It stops when the event loop shuts down, after
    the device is disconnected. Thus no transmission starts while the app shuts down.
    """

    def __init__(self, enter: Callable[[], AbstractAsyncContextManager[Any]] | None) -> None:
        """Start to enter ``enter`` (``None``: there is nothing to enter, so engage at once)."""
        self._engaged = asyncio.Event()
        self._released = asyncio.Event()
        self._task = asyncio.ensure_future(self._hold(enter))

    async def _hold(self, enter: Callable[[], AbstractAsyncContextManager[Any]] | None) -> None:
        try:
            if enter is None:
                self._engaged.set()
                await self._released.wait()
                return
            async with enter():
                self._engaged.set()
                await self._released.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a failed quiesce must not stop a quit
            _log.warning("could not quiet the app before leaving: %s", exc)
        finally:
            # A leave must never wait for a quiesce that failed or was cancelled.
            self._engaged.set()

    async def engaged(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for the quiesce to engage. Return whether it did."""
        try:
            await asyncio.wait_for(self._engaged.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    def release(self) -> None:
        """Release the quiesce: the work that it held back continues."""
        self._released.set()


class HoldQuitDialog(Screen):
    """The dialog that a held Esc key opens: MeshTerm will quit soon, and it shows when.

    The bar shows the time that remains. It starts to fill when the dialog appears. It is
    full at :data:`~meshterm.services.hold_to_quit.QUIT_S` after the key press. At that
    time, the app leaves, and on the Cardputer Zero, the launcher sends its SIGTERM. The
    dialog has no buttons, because the only answer is in the hand of the user: hold the
    key, or release it. The dialog is modal, so no key press gets to the screen below it
    while it is up.
    """

    title = "Quit MeshTerm"
    floating = True
    modal = True
    border_style = "warn"
    footer_hint = "let go of Esc to stay"

    def __init__(self, since: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        """A dialog for the hold that started at ``since``, in ``clock`` time (monotonic)."""
        super().__init__()
        self._starts = since + hold_to_quit.DIALOG_S
        self._span = hold_to_quit.QUIT_S - hold_to_quit.DIALOG_S
        self._clock = clock

    @property
    def picocalc_lyra_lane(self):
        """No lane: no keyboard key has an effect, except the Esc key that the user holds."""
        from .fkeys import EMPTY_LANE

        return EMPTY_LANE

    @property
    def dialog_width(self) -> int:
        """A small box: one line and a bar."""
        return _WIDTH

    @property
    def fraction(self) -> float:
        """How full the bar is: ``0`` when the dialog appears, to ``1`` when the app leaves."""
        return min(1.0, max(0.0, (self._clock() - self._starts) / self._span))

    def render_body(self, width: int) -> list[str]:
        """``Hold Esc to quit`` above the bar. The bar fills the width of the line."""
        bar = meter(self.fraction, width, style="warn", slim=True, track="track")
        return render_lines(Group(Text("Hold Esc to quit", style="warn"), Text(""), bar), width)

    def handle(self, action: str, data: str = "") -> None:
        """Ignore all keys: the dialog responds only to a hold or a release of the Esc key."""
        return


class EscHoldWatch:
    """The :class:`~meshterm.services.hold_to_quit.Listener` of the session.

    Other threads call its three listener methods. These methods only give their work to
    the loop of the session. All the other code here runs on the loop.
    """

    def __init__(self, session: TuiSession, loop: asyncio.AbstractEventLoop) -> None:
        """Watch held Esc keys for ``session``, whose loop is ``loop``."""
        self._session = session
        self._loop = loop
        self._raise: asyncio.TimerHandle | None = None
        self._quit: asyncio.TimerHandle | None = None
        self._ticker: asyncio.Future | None = None
        #: The dialog while it is up, and the backdrop pushed under it when no screen was up.
        self._dialog: HoldQuitDialog | None = None
        self._backdrop: Screen | None = None

    # --- the listener (other threads) ------------------------------------------------

    def held(self, since: float) -> None:
        """Esc went down at ``since``."""
        self._call(self._on_held, since)

    def released(self) -> None:
        """Esc came up."""
        self._call(self._on_released)

    def terminate(self) -> None:
        """Leave now (the launcher's SIGTERM)."""
        self._call(self._session.leave)

    def _call(self, work: Callable[..., None], *args: Any) -> None:
        try:
            self._loop.call_soon_threadsafe(work, *args)
        except RuntimeError:  # the loop is closed: the app has already stopped
            pass

    # --- on the loop --------------------------------------------------------------------

    def _on_held(self, since: float) -> None:
        self._stand_down()
        delay = since + hold_to_quit.DIALOG_S - time.monotonic()
        self._raise = self._loop.call_later(max(0.0, delay), self._show, since)

    def _show(self, since: float) -> None:
        """Open the dialog, if the hold that asked for it continues."""
        self._raise = None
        if self._session.leaving or not hold_to_quit.claim(since):
            return
        self._session.quiet()
        dialog = self._dialog = HoldQuitDialog(since)
        left = since + hold_to_quit.QUIT_S - time.monotonic()
        self._quit = self._loop.call_later(max(0.0, left), self._session.leave)
        # This method pushes the dialog and :meth:`_stand_down` pops it, the same as the busy
        # card. The dialog responds to the key, not to a future. Thus a release that arrives
        # at the same moment cannot miss a dialog that is only half made. On an empty stack
        # (a tool between two screens), the dialog gets its own blank backdrop, as the busy
        # card does. It never uses the menu as its backdrop: by that time, the menu may be
        # pushed again under the dialog, and a pop of the backdrop then removes the menu.
        if self._session.top is None:
            self._backdrop = ScrollScreen("", floating=False, footer_hint="")
            self._session.push(self._backdrop)
        self._session.push(dialog)
        self._ticker = asyncio.ensure_future(self._fill(dialog))

    async def _fill(self, dialog: HoldQuitDialog) -> None:
        """Request a paint while the bar fills. The last paint shows the full bar."""
        while True:
            await asyncio.sleep(BAR_TICK_S)
            self._session.invalidate()
            if dialog.fraction >= 1.0:
                return

    def _on_released(self) -> None:
        if self._session.leaving:
            return  # the bar is full: the app leaves, and the launcher's SIGTERM comes too
        self._stand_down()
        self._session.unquiet()

    def _stand_down(self) -> None:
        """Cancel the hold that is in progress: its timers, its paints, and its dialog."""
        for timer in (self._raise, self._quit):
            if timer is not None:
                timer.cancel()
        self._raise = self._quit = None
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        if self._dialog is not None:
            self._session.pop(self._dialog)
            self._dialog = None
        if self._backdrop is not None:
            self._session.pop(self._backdrop)
            self._backdrop = None


#: Whether the keys of a terminal arrive as a VT byte stream. In such a stream, the last Esc
#: can be parsed after the key is up. This is true on all platforms except Windows, where
#: prompt_toolkit reads the events of the console.
VT_INPUT = sys.platform != "win32"

#: Seconds between two probes of the keyboard while the Esc key of a terminal is held. At
#: this interval, MeshTerm finds the release of a tap well within a tenth of a second. Each
#: probe costs almost nothing.
POLL_S = 0.03


class WatchedEsc:
    """The Esc key of a terminal, with a probe that asks the keyboard if it is still held.

    When no front end owns the keys, the Esc key arrives through the terminal like all
    other keys, and the terminal never tells when the key comes up. The keyboard tells it
    (:func:`~meshterm.services.hold_to_quit.esc_probe`). Thus an Esc that arrives while the
    key is still down is held back. Then the hold is reported and watched, the same as a
    hold that a front end with its own keys reports:

    - If the user releases the key at once, the Esc is delivered.
    - A hold of one second opens the dialog.
    - The repeats of the keyboard are part of the hold.

    Through a VT stream, a lone Esc byte gets to the screen only after prompt_toolkit waits
    to be sure that it starts no sequence (``ttimeoutlen``). Thus the user usually releases
    a tap before it arrives, and the tap is never held back. Also, the last repeat of a hold
    can arrive after the key is up. Repeats that arrive this late are ignored for the length
    of that wait. Thus a late repeat does not leave the screen under a dialog that the user
    just released.
    """

    def __init__(self, session: TuiSession, down: Callable[[], bool]) -> None:
        """Hold back the Esc of ``session`` while ``down()`` tells that the key is still held."""
        self._session = session
        self._down = down
        self._watching: asyncio.Future | None = None
        self._repeats = 0
        self._late_until = 0.0
        self._delivering = False

    def take(self) -> bool:
        """An Esc arrived. Returns whether it is held back as part of a hold."""
        if self._delivering:
            return False
        if self._watching is not None:
            self._repeats += 1
            return True  # the keyboard repeats a held key
        if time.monotonic() < self._late_until:
            return True  # a repeat of the hold that just ended, parsed late
        if not self._down() or not hold_to_quit.press():
            return False  # the key is already up, or nothing listens
        self._repeats = 0
        self._watching = asyncio.ensure_future(self._until_let_go())
        return True

    def ahead(self) -> None:
        """Another key arrived: first deliver a held-back Esc, so that the keys stay in order."""
        if self._watching is not None and hold_to_quit.interrupt():
            self._deliver()

    async def _until_let_go(self) -> None:
        while self._down():
            await asyncio.sleep(POLL_S)
        self._watching = None
        tap = hold_to_quit.release()
        if self._repeats and VT_INPUT:
            app = getattr(self._session, "_app", None)
            self._late_until = time.monotonic() + getattr(app, "ttimeoutlen", 0.5) + POLL_S
        if tap:
            self._deliver()

    def _deliver(self) -> None:
        self._delivering = True
        try:
            self._session._dispatch("escape")
        finally:
            self._delivering = False


__all__ = [
    "BAR_TICK_S",
    "POLL_S",
    "VT_INPUT",
    "EscHoldWatch",
    "HoldQuitDialog",
    "Quiet",
    "WatchedEsc",
]
