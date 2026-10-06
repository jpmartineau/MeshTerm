# SPDX-License-Identifier: Apache-2.0
"""The weekly flood advert: keep our node in the contact lists of the mesh, at most once a week.

A MeshCore companion never sends an advert by itself. It has no advert timer (a repeater
has one), and it does not even announce itself at boot. Thus if the owner of a node never
presses *Send advert*, the node slowly falls out of the contact lists of all the other
nodes. When the ``weekly_flood_advert`` preference is on (it is off by default), this task
sends one flood advert from the connected device after that device went a full week without
one. The task runs for the full session. :class:`~meshterm.core.advert_store.AdvertStore`
keeps the week for each device, in real time:

* The week starts at the **latest** of three instants: the time when the preference was
  turned on, the last flood advert from that device (automatic *or* sent by hand), and the
  first time that MeshTerm connected to the device. Thus when the user turns the preference
  on, MeshTerm never sends an advert, and a manual flood advert moves the next automatic
  one a full week later.
* If an advert was due and the app stopped before it sent the advert, the advert stays
  due. It goes out on the next run that connects that device.

To be due is not sufficient to transmit. Each repeater in range relays a flood advert, thus
the advert waits for the air to be **quiet**:

* **Online first.** Each connection (a start of the app, and each reconnect after the link
  is lost) must *hear* at least one packet before a silence counts. A link that hears
  nothing proves nothing about the mesh.
* **Then a quiet period.** No packet heard *and* nothing of ours sent, for the
  ``advert_quiet_s`` preference (30 s by default) plus a random 0–5 s. The random part is
  drawn again each time that the silence breaks. Without a new draw, two devices that wait
  through the same packets wait the same time after each packet, and collide each time. A
  new draw at each break separates them. A busy mesh can hold the advert back for a time
  with no limit: the advert waits.

Each packet that the event hub delivers counts as heard, because all of them are
receptions. Our own transmissions are read from the shared transmit clock
(:mod:`meshterm.core.transmit_gate`), which each send updates.

The scheduler has no device subscription of its own, and never opens the device. Each tick
checks for a connected device, and if there is none, it does nothing. Thus the scheduler
continues to tick while the link is down, and it continues its work after the reconnect
flow of the session has done its work. The interactive session starts the scheduler.
Scripted CLI runs never start it, thus a one-shot command cannot start a background
transmission.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ..core import transmit_gate

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.connection import Device
    from ..core.events import MeshEvent

#: The time (seconds) between two ticks. The quiet period is measured to approximately
#: this precision. That is sufficient for a period of 5–60 s with some seconds of random
#: time on top.
TICK_S = 1.0

#: The largest random extension of the quiet period, in seconds (a uniform draw from 0).
JITTER_S = 5.0

#: The time (seconds) between two reads of the advert store while an advert is not due
#: yet. The week is days long, thus a coarse check costs nothing in accuracy, and the file
#: is not read at each tick.
DUE_CHECK_S = 30.0


#: :attr:`AdvertStatus.state` while no device is connected: no week to count, nothing to send.
OFFLINE = "offline"

#: :attr:`AdvertStatus.state` while the week of the connected device still runs.
COUNTING = "counting"

#: :attr:`AdvertStatus.state` when the advert is due, while this connection has not heard
#: a packet yet.
LISTENING = "listening"

#: :attr:`AdvertStatus.state` when the advert is due and a packet was heard, while the air
#: is not quiet yet.
QUIET = "quiet"


@dataclass(frozen=True, slots=True)
class AdvertStatus:
    """The status of the weekly advert, which a screen can show.

    Attributes:
        state: :data:`OFFLINE`, :data:`COUNTING`, :data:`LISTENING`, or :data:`QUIET`.
        due_at: The time at which the advert of the connected device becomes due, while
            :data:`COUNTING`.
    """

    state: str
    due_at: datetime | None = None


class AdvertScheduler:
    """Owns the weekly-advert loop for an interactive session.

    Use the async lifecycle methods (:meth:`start`, :meth:`stop`, :meth:`aclose`). All
    other work occurs on the schedule of the loop.
    """

    def __init__(
        self,
        ctx: AppContext,
        *,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize an idle scheduler that is bound to an application context.

        Args:
            ctx: The shared application context. Each tick reads the connected device, the
                preferences, and the advert store from it. The scheduler does nothing
                before :meth:`start`.
            rng: The source of the random extension of the quiet period (tests set a
                fixed one).
            clock: A monotonic clock in seconds. It is the same clock as the transmit
                clock, against which the silence is compared (tests replace it with a
                clock that they can move forward).
        """
        self._ctx = ctx
        self._rng = rng or random.Random()
        self._clock = clock
        self._task: asyncio.Task | None = None
        self._unsubscribe: Callable[[], None] | None = None
        # The connection to which the state below belongs: the Device object itself, not
        # its id(). Thus the address of a freed Device can never look like a new link. A
        # reconnect is a new Device, thus all starts again: the key, the due check, and the
        # proof of life.
        self._device: Device | None = None
        self._key = ""
        self._due = False
        self._due_at: datetime | None = None
        self._checked_at: float | None = None
        # The connection that last heard a packet, and the time at which anything was last
        # heard.
        self._heard_on: Device | None = None
        self._last_heard: float | None = None
        # The silence break for which the current random extension was drawn, and the draw.
        self._break_at: float | None = None
        self._jitter = 0.0

    @property
    def active(self) -> bool:
        """True while the scheduler loop runs."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start to listen and to tick. Idempotent. It never touches the device itself."""
        if self.active:
            return
        self._unsubscribe = self._ctx.events.subscribe(self._on_event)
        self._task = asyncio.ensure_future(self._run())
        self._ctx.log.info("advert scheduler started")

    async def stop(self) -> None:
        """Stop the loop and the event subscription. Idempotent."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._ctx.log.info("advert scheduler stopped")

    async def aclose(self) -> None:
        """Stop the loop at the end of the session (an alias for :meth:`stop`)."""
        await self.stop()

    def status(self) -> AdvertStatus | None:
        """The current status of the weekly advert, or ``None`` when there is nothing to show.

        ``None`` while the scheduler does not run, while the preference is off, and in the
        second between a connection and the first check of it. This method reads only what
        the loop already keeps. Thus a screen can ask at each paint, and the file is not
        touched. When a :meth:`recheck` is pending, the last reading stays until the tick
        replaces it.
        """
        ctx = self._ctx
        if not self.active or not ctx.preferences.weekly_flood_advert:
            return None
        device = ctx._device
        if device is None:
            return AdvertStatus(OFFLINE)
        if device is not self._device:
            return None
        if not self._due:
            return AdvertStatus(COUNTING, self._due_at) if self._due_at is not None else None
        return AdvertStatus(LISTENING if self._heard_on is not device else QUIET)

    def recheck(self) -> None:
        """Read the preference and the store again at the next tick, not at the next due check.

        This is for a screen that will report :meth:`status`. Without this call, the screen
        shows what the loop read last, up to :data:`DUE_CHECK_S` ago: possibly before a
        switch that the user applied a moment ago.
        """
        self._checked_at = None

    def _on_event(self, _event: MeshEvent) -> None:
        """Note a heard packet: it breaks the silence, and proves that this connection is live."""
        self._last_heard = self._clock()
        self._heard_on = self._ctx._device

    async def _run(self) -> None:
        """Tick with no end: sleep, then do one best-effort pass."""
        while True:
            await asyncio.sleep(TICK_S)
            try:
                await self._pass()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad pass must not stop the loop
                self._ctx.log.debug("advert scheduler: pass failed: %s", exc)

    async def _pass(self) -> None:
        """One tick: keep the clock current, and send the weekly advert when due and quiet."""
        ctx = self._ctx
        device = ctx._device
        if device is None:
            self._device = None
            return
        if device is not self._device:
            self._device, self._key, self._due, self._checked_at = device, "", False, None
            self._due_at = None
        if not self._key:
            self._key = await self._public_key()
            if not self._key:
                return
            ctx.advert_store.arm(self._key)  # a new device starts its week

        now = self._clock()
        if self._checked_at is None or now - self._checked_at >= DUE_CHECK_S:
            self._checked_at = now
            self._check_due()
        if not self._due or self._heard_on is not device or not self._quiet(now):
            return
        if transmit_gate.remaining(flood_advert=True) > 0:
            return  # a flood advert of ours went out a moment ago. The next tick decides again.
        # Read the file, not the cached verdict: a flood advert sent by hand after the last
        # check started the week again, and the preference may be off now.
        self._check_due()
        if not self._due:
            return
        try:
            await device.send_advert(True)
        except Exception:
            self._due = False  # decided again at the next due check, not at each tick
            raise
        ctx.advert_store.mark_flood(self._key)
        self._due = False
        self._checked_at = None  # the next tick reads the new week
        ctx.log.info("advert scheduler: sent the weekly flood advert")

    def _check_due(self) -> None:
        """Read the preference and the store again, and decide if the advert is due."""
        ctx = self._ctx
        on = bool(ctx.preferences.weekly_flood_advert)
        ctx.advert_store.sync_enabled(on)
        self._due_at = ctx.advert_store.due_at(self._key) if on else None
        due = on and ctx.advert_store.due(self._key)
        if due and not self._due:
            ctx.log.info(
                "advert scheduler: weekly flood advert due; waiting for %s s of quiet",
                ctx.preferences.advert_quiet_s,
            )
        self._due = due

    def _quiet(self, now: float) -> bool:
        """True if the air was quiet for long enough. Draw a new extension at each break."""
        sent = transmit_gate.current().last_sent
        breaks = [t for t in (self._last_heard, sent) if t is not None]
        if not breaks:
            return False
        break_at = max(breaks)
        if break_at != self._break_at:
            self._break_at = break_at
            self._jitter = self._rng.uniform(0.0, JITTER_S)
        return now - break_at >= float(self._ctx.preferences.advert_quiet_s) + self._jitter

    async def _public_key(self) -> str:
        """The public key of the connected device, from the session cache (best effort)."""
        try:
            info = await self._ctx.devstate.self_info()
        except Exception:  # noqa: BLE001 - best-effort identity probe. The next tick tries again.
            return ""
        return str(info.get("public_key") or "")
