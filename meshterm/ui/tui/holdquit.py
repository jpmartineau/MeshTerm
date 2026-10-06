# SPDX-License-Identifier: Apache-2.0
"""The session's half of hold-Esc-to-quit: the box, its bar, and what holding it engages.

:mod:`~meshterm.services.hold_to_quit` says what the gesture is and why; this is what the
session does with it. :class:`EscHoldWatch` is the session's listener: told that Esc went
down, it waits out :data:`~meshterm.services.hold_to_quit.DIALOG_S`, claims the hold, floats
a :class:`HoldQuitDialog` over whatever is up — detached, the way the quit confirm floats,
so nothing under it stops — and engages the app's :class:`Quiet`; told that Esc came up, it
takes the box down and gives the quiet back. Held to
:data:`~meshterm.services.hold_to_quit.QUIT_S`, it leaves
(:meth:`~meshterm.ui.tui.session.TuiSession.leave`).
"""

from __future__ import annotations

import asyncio
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

#: Seconds between repaints while the bar fills. The bar gains a step every few hundredths
#: of a second, but a frame costs real time on a handheld, and ten a second reads as a
#: smooth fill while leaving most of a core to whatever the app is finishing up.
BAR_TICK_S = 0.1

#: The box's width, border included: the bar is the width of the text above it.
_WIDTH = 30


class Quiet:
    """The app's quiesce (:meth:`~meshterm.ui.tui.session.TuiSession.set_quiesce`), held.

    Entered in a task of its own, since what it takes — the transmit lock — may have to
    wait for a transmission to finish, and the box must not wait with it. Given back
    (:meth:`release`) when the reader lets go of Esc. When the app leaves it is never given
    back: it goes when the event loop is shut down, after the device has been disconnected,
    so nothing starts transmitting while the app tears down.
    """

    def __init__(self, enter: Callable[[], AbstractAsyncContextManager[Any]] | None) -> None:
        """Start entering ``enter`` (``None``: nothing to enter, engaged at once)."""
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
            # Never keep a leave waiting on a quiesce that failed or was cancelled.
            self._engaged.set()

    async def engaged(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for the quiesce to take hold; whether it did."""
        try:
            await asyncio.wait_for(self._engaged.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    def release(self) -> None:
        """Give the quiesce back: what it held up goes ahead."""
        self._released.set()


class HoldQuitDialog(Screen):
    """The box a held Esc raises: that MeshTerm is about to quit, and how soon.

    The bar is the time left: it starts filling the moment the box appears and is full at
    :data:`~meshterm.services.hold_to_quit.QUIT_S` from the press, when the app leaves —
    and when, on the Cardputer Zero, the launcher sends its SIGTERM. No buttons, since the
    only answer is in the reader's hand: keep holding, or let go. Modal, so no key reaches
    the screen beneath while it is up.
    """

    title = "Quit MeshTerm"
    floating = True
    modal = True
    border_style = "warn"
    footer_hint = "let go of Esc to stay"

    def __init__(self, since: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        """A box for the hold that began at ``since`` (``clock``'s time, ``time.monotonic``)."""
        super().__init__()
        self._starts = since + hold_to_quit.DIALOG_S
        self._span = hold_to_quit.QUIT_S - hold_to_quit.DIALOG_S
        self._clock = clock

    @property
    def picocalc_lyra_lane(self):
        """No lane: nothing on the keyboard but the Esc in the reader's hand does anything."""
        from .fkeys import EMPTY_LANE

        return EMPTY_LANE

    @property
    def dialog_width(self) -> int:
        """A small box: one line and a bar."""
        return _WIDTH

    @property
    def fraction(self) -> float:
        """How full the bar is, ``0`` when the box appears to ``1`` when the app leaves."""
        return min(1.0, max(0.0, (self._clock() - self._starts) / self._span))

    def render_body(self, width: int) -> list[str]:
        """``Hold Esc to quit`` over the bar, which fills the line's width."""
        bar = meter(self.fraction, width, style="warn", slim=True, track="track")
        return render_lines(Group(Text("Hold Esc to quit", style="warn"), Text(""), bar), width)

    def handle(self, action: str, data: str = "") -> None:
        """Swallow every key: the box answers to Esc being held or let go, and nothing else."""
        return


class EscHoldWatch:
    """The session's :class:`~meshterm.services.hold_to_quit.Listener`.

    Its three listener methods are called from other threads and only hand their work to
    the session's loop; everything else here runs on the loop.
    """

    def __init__(self, session: TuiSession, loop: asyncio.AbstractEventLoop) -> None:
        """Watch held Esc keys for ``session``, whose loop is ``loop``."""
        self._session = session
        self._loop = loop
        self._raise: asyncio.TimerHandle | None = None
        self._quit: asyncio.TimerHandle | None = None
        self._ticker: asyncio.Future | None = None
        #: The box while it is up, and the backdrop pushed under it when nothing was.
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
        except RuntimeError:  # the loop has closed: the app is gone already
            pass

    # --- on the loop --------------------------------------------------------------------

    def _on_held(self, since: float) -> None:
        self._stand_down()
        delay = since + hold_to_quit.DIALOG_S - time.monotonic()
        self._raise = self._loop.call_later(max(0.0, delay), self._show, since)

    def _show(self, since: float) -> None:
        """Raise the box, if the hold that asked for it is still held."""
        self._raise = None
        if self._session.leaving or not hold_to_quit.claim(since):
            return
        self._session.quiet()
        dialog = self._dialog = HoldQuitDialog(since)
        left = since + hold_to_quit.QUIT_S - time.monotonic()
        self._quit = self._loop.call_later(max(0.0, left), self._session.leave)
        # Pushed here and popped by :meth:`_stand_down`, the way the busy card is: the box
        # answers to the key, not to a future, and a release landing in the same breath
        # has nothing half-made to miss. On an empty stack (a tool between two screens) it
        # gets a blank backdrop of its own, as the busy card does, never the menu: the menu
        # may be pushed again under it by then, and popping it would take the menu away.
        if self._session.top is None:
            self._backdrop = ScrollScreen("", floating=False, footer_hint="")
            self._session.push(self._backdrop)
        self._session.push(dialog)
        self._ticker = asyncio.ensure_future(self._fill(dialog))

    async def _fill(self, dialog: HoldQuitDialog) -> None:
        """Repaint while the bar fills; the last repaint shows it full."""
        while True:
            await asyncio.sleep(BAR_TICK_S)
            self._session.invalidate()
            if dialog.fraction >= 1.0:
                return

    def _on_released(self) -> None:
        if self._session.leaving:
            return  # the bar ran out: the app is on its way, and the launcher's SIGTERM too
        self._stand_down()
        self._session.unquiet()

    def _stand_down(self) -> None:
        """Cancel the hold under way: its timers, its repaints, and its box."""
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


__all__ = ["BAR_TICK_S", "EscHoldWatch", "HoldQuitDialog", "Quiet"]
