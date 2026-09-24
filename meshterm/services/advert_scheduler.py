# SPDX-License-Identifier: Apache-2.0
"""The weekly flood advert: keep this node on the mesh's contact lists, once a week at most.

A MeshCore companion never advertises by itself — unlike a repeater it has no advert
timer, and it does not even announce itself at boot — so a node whose owner never presses
*Send advert* gradually falls out of everyone else's contact list. When the
``weekly_flood_advert`` preference is on (it is off by default), this session-long task
sends one flood advert from the connected device once that device has gone a full week
without one. The week is kept per device, in real time, by
:class:`~meshterm.core.advert_store.AdvertStore`:

* it runs from the **latest** of three instants — the preference being turned on, the last
  flood advert from that device (automatic *or* sent by hand), and the first time MeshTerm
  connected to it — so switching the preference on never sends, and a manual flood advert
  pushes the next automatic one a whole week out;
* a due advert the app did not live to send stays due, and goes out on the next run that
  connects that device.

Being due is not enough to transmit. A flood advert is rebroadcast by every repeater in
range, so it waits for the air to be **quiet**:

* **online first** — each connection (a launch, and every reconnect after a link drop)
  must *hear* at least one packet before any silence counts, because a link that hears
  nothing proves nothing about the mesh;
* **then a quiet spell** — no packet heard *and* nothing of ours sent for the
  ``advert_quiet_s`` preference (30 s by default) plus a random 0–5 s. The random part is
  drawn afresh every time the silence breaks: two devices waiting through the same packets
  would otherwise wait the same time after each one and collide every time, where a fresh
  draw per break parts them. A busy mesh can hold the advert back indefinitely; it waits.

Every packet the event hub delivers counts as heard — they are all receptions — and our
own transmissions are read off the shared transmit clock
(:mod:`meshterm.core.transmit_gate`), which every send marks.

The scheduler holds no device subscription of its own and never opens the radio: each
tick checks for a connected device and skips quietly without one, so it keeps ticking
across a link drop and resumes once the session's reconnect flow has done its job. The
interactive session starts it; scripted CLI runs never do, so a one-shot command cannot
fire a background transmission.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..core import transmit_gate

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.connection import Device
    from ..core.events import MeshEvent

#: Seconds between ticks. The quiet spell is measured to about this precision, which is
#: plenty against a 5–60 s spell drawn with seconds of randomness on top.
TICK_S = 1.0

#: The widest random extension of the quiet spell, in seconds (drawn uniformly from 0).
JITTER_S = 5.0

#: Seconds between re-reads of the advert store while an advert is not yet due. The week
#: is days long, so a coarse check costs nothing in accuracy and spares the file a read on
#: every tick.
DUE_CHECK_S = 30.0


class AdvertScheduler:
    """Owns the weekly-advert loop for an interactive session.

    Interact through the async lifecycle methods (:meth:`start`, :meth:`stop`,
    :meth:`aclose`); everything else happens on the loop's own schedule.
    """

    def __init__(
        self,
        ctx: AppContext,
        *,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize an idle scheduler bound to an application context.

        Args:
            ctx: The shared application context, read each tick for the connected device,
                the preferences and the advert store. Nothing is touched until
                :meth:`start`.
            rng: The source of the quiet spell's random extension (tests pin it).
            clock: A monotonic clock in seconds — the transmit clock's own, which the
                silence is compared against (tests substitute one they can advance).
        """
        self._ctx = ctx
        self._rng = rng or random.Random()
        self._clock = clock
        self._task: asyncio.Task | None = None
        self._unsubscribe: Callable[[], None] | None = None
        # The connection the state below belongs to: the Device object itself, held rather
        # than its id() so a freed one's address can never pass for a fresh link. A
        # reconnect is a new Device, so it starts over — key, due check, proof of life.
        self._device: Device | None = None
        self._key = ""
        self._due = False
        self._checked_at: float | None = None
        # Which connection last heard a packet, and when anything was last heard.
        self._heard_on: Device | None = None
        self._last_heard: float | None = None
        # The silence break the current random extension was drawn for, and the draw.
        self._break_at: float | None = None
        self._jitter = 0.0

    @property
    def active(self) -> bool:
        """Whether the scheduler loop is running."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Start listening and ticking. Idempotent; never touches the radio itself."""
        if self.active:
            return
        self._unsubscribe = self._ctx.events.subscribe(self._on_event)
        self._task = asyncio.ensure_future(self._run())
        self._ctx.log.info("advert scheduler started")

    async def stop(self) -> None:
        """Stop the loop and the listening. Idempotent."""
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
        """Stop the loop at session end (an alias for :meth:`stop`)."""
        await self.stop()

    def _on_event(self, _event: MeshEvent) -> None:
        """Note a packet heard: it breaks the silence, and proves this connection is live."""
        self._last_heard = self._clock()
        self._heard_on = self._ctx._device

    async def _run(self) -> None:
        """Tick forever: sleep, then make one best-effort pass."""
        while True:
            await asyncio.sleep(TICK_S)
            try:
                await self._pass()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad pass must not kill the loop
                self._ctx.log.debug("advert scheduler: pass failed: %s", exc)

    async def _pass(self) -> None:
        """One tick: keep the clock current, and send the weekly advert once due and quiet."""
        ctx = self._ctx
        device = ctx._device
        if device is None:
            self._device = None
            return
        if device is not self._device:
            self._device, self._key, self._due, self._checked_at = device, "", False, None
        if not self._key:
            self._key = await self._public_key()
            if not self._key:
                return
            ctx.advert_store.arm(self._key)  # a device never seen before starts its week

        now = self._clock()
        if self._checked_at is None or now - self._checked_at >= DUE_CHECK_S:
            self._checked_at = now
            self._check_due()
        if not self._due or self._heard_on is not device or not self._quiet(now):
            return
        if transmit_gate.remaining(flood_advert=True) > 0:
            return  # a flood advert of ours went out moments ago; the next tick re-judges
        # The file, not the cached verdict: a flood advert sent by hand since the last
        # check restarted the week, and the preference may have been switched off.
        self._check_due()
        if not self._due:
            return
        try:
            await device.send_advert(True)
        except Exception:
            self._due = False  # judged again at the next due check, not on every tick
            raise
        ctx.advert_store.mark_flood(self._key)
        self._due = False
        ctx.log.info("advert scheduler: sent the weekly flood advert")

    def _check_due(self) -> None:
        """Re-read the preference and the store, and settle whether the advert is due."""
        ctx = self._ctx
        on = bool(ctx.preferences.weekly_flood_advert)
        ctx.advert_store.sync_enabled(on)
        due = on and ctx.advert_store.due(self._key)
        if due and not self._due:
            ctx.log.info(
                "advert scheduler: weekly flood advert due; waiting for %s s of quiet",
                ctx.preferences.advert_quiet_s,
            )
        self._due = due

    def _quiet(self, now: float) -> bool:
        """Whether the air has been quiet long enough, drawing a fresh extension per break."""
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
        """The connected device's public key, from the session cache (best-effort)."""
        try:
            info = await self._ctx.devstate.self_info()
        except Exception:  # noqa: BLE001 - identity probe is best-effort; retry next tick
            return ""
        return str(info.get("public_key") or "")
