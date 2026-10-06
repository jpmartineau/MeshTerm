# SPDX-License-Identifier: Apache-2.0
"""Set the clock of the companion from this computer: by hand from Device config, or on connect.

Most MeshCore boards have no real-time clock. A power cycle sets them back to the epoch
that the firmware boots with. Then each message that they send has a timestamp in a year
that the mesh does not agree with. :func:`set_clock` is the one step on the device side
that corrects it. The function reads the time that the device has, writes the time of this
computer, and returns the drift that it corrected. The drift is the fact to keep, and a
successful set destroys it. The *Sync clock* action of the Device config screen (``config
sync-clock`` on the command line) calls this function, and shows an acknowledgement on the
screen. :class:`ClockSync` calls the same function itself, one time for each connection,
when the ``set_clock_on_connect`` preference asks for it. That preference is off by
default, because a write to a device that nobody asked for is a choice of the owner.

**Forward only.** MeshCore firmware never sets its clock back. It refuses a time that is
earlier than the time that it has (:class:`~meshterm.core.connection.ClockAheadError`). The
clock of a device can be ahead of the clock of this computer, because of a GPS fix,
another app, or drift. Then MeshTerm reads the clock first, and does not write it at all.
If the clock is only a few seconds ahead, it is in sync. If it is more, MeshTerm tells this
fact clearly, with the thing that resets it. MeshTerm does not try again at each connect,
because the firmware refuses each try with an error code that it calls "malformed".

The automatic set is a background step, not a startup step. The connect path gives the
device to the app and continues. The write occurs in its own task, so a slow or silent
firmware never blocks the menu. The set reports in the **log**, never on the screen.

The set occurs one time for each *connection*. The identity of the device object
identifies the connection, as in the key probe of the advert scheduler. A reconnect after
a lost link is a new :class:`~meshterm.core.connection.Device`, and it gets its own set.
A lost link is exactly the time when the board may have rebooted and lost its clock again.
The interactive session starts the service. Scripted CLI runs never start it. Thus a
one-shot command cannot change a device that it was asked only to read.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ..core.connection import ClockAheadError

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.connection import Device

#: How far ahead the clock of a device can be, and still count as in sync. Two clocks with
#: a difference of a few seconds are not a problem to report: for example, a device with a
#: GPS-disciplined clock next to a PC that is approximately one second off the true time.
#: Also, the firmware cannot remove that difference.
IN_SYNC_AHEAD_S = 60


@dataclass(frozen=True, slots=True)
class ClockSet:
    """The result of one clock set.

    Attributes:
        epoch: The UNIX time written to the device. If nothing was written, the time that
            the device already had.
        drift_s: The difference between the device clock and our clock before the set
            (device minus host, in seconds). ``None`` when the firmware did not report its
            clock.
        written: Whether the clock was written. ``False`` when the clock was already a few
            seconds ahead (in the limit of :data:`IN_SYNC_AHEAD_S`). The firmware refuses
            to set such a clock back, and the clock does not need a set.
    """

    epoch: int
    drift_s: int | None
    written: bool = True

    @property
    def set_at(self) -> datetime:
        """The written instant, in the local time of this computer."""
        return datetime.fromtimestamp(self.epoch).astimezone()

    @property
    def stamp(self) -> str:
        """The written instant, in the format that the acknowledgement and the log print."""
        return self.set_at.strftime("%Y-%m-%d %H:%M:%S")


async def set_clock(device: Device) -> ClockSet:
    """Set the clock of ``device`` to the time of this computer, and return the correction.

    The read of the device clock before the write is optional. The drift is good to have,
    but it is not necessary. A firmware that does not answer still gets its clock set.

    Args:
        device: A connected device.

    Returns:
        The instant that was written, and the drift that it corrected.

    Raises:
        ClockAheadError: If the device clock is more than :data:`IN_SYNC_AHEAD_S` ahead of
            the clock of this computer (from the first read), or if the firmware refused
            the write because it sets the clock back. In the margin, nothing is written,
            and the result tells this (``written`` false).
        All the errors that the device raises when the write fails for a different reason.
    """
    try:
        before = await device.get_time()
    except Exception:  # noqa: BLE001 - an optional read. The drift is only extra information.
        before = None
    epoch = int(time.time())
    if before is not None and before > epoch:
        # The firmware only moves its clock forward, so it can only refuse a write here. A
        # small difference is in sync. A larger difference is a fact to report, not a write
        # to try again.
        if before - epoch > IN_SYNC_AHEAD_S:
            raise ClockAheadError(before - epoch)
        return ClockSet(epoch=before, drift_s=before - epoch, written=False)
    await device.set_time(epoch)
    return ClockSet(epoch=epoch, drift_s=None if before is None else before - epoch)


class ClockSync:
    """Owns the clock set on connect for an interactive session.

    Use :meth:`start`, :meth:`stop`/:meth:`aclose`, and :meth:`on_connected`. The context
    calls :meth:`on_connected` when each connection is stable.
    """

    def __init__(self, ctx: AppContext) -> None:
        """Make an idle service for an application context.

        Args:
            ctx: The shared application context. The service reads the preference from it
                at each connection, and the logger. The service changes nothing until
                :meth:`start`.
        """
        self._ctx = ctx
        self._armed = False
        self._task: asyncio.Task | None = None
        # The identity of the last device object that was set. Thus one connection gets
        # one set, also when the connect path reports it more than once.
        self._synced: int | None = None

    @property
    def active(self) -> bool:
        """Whether the service is armed to act on connections."""
        return self._armed

    async def start(self) -> None:
        """Arm the service, and set the clock immediately if a device is already connected.

        Idempotent. The startup picker can adopt a live connection before a service
        starts. Thus the connection that already exists counts as the first connection.
        """
        if self._armed:
            return
        self._armed = True
        device = self._ctx._device
        if device is not None:
            self.on_connected(device)

    async def stop(self) -> None:
        """Disarm, and cancel a set that is still in progress. Idempotent."""
        self._armed = False
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def aclose(self) -> None:
        """Stop at the end of the session (an alias for :meth:`stop`)."""
        await self.stop()

    def on_connected(self, device: Device) -> None:
        """Note a stable connection, and set its clock in the background if the preference asks.

        Returns immediately, because the set runs in its own task. This method does nothing
        while the service is not started (scripted runs), while the preference is off, and
        for a device that this session has already set.

        Args:
            device: The device whose connection the context completed a moment ago.
        """
        if not self._armed or not self._ctx.preferences.set_clock_on_connect:
            return
        if self._synced == id(device):
            return
        self._synced = id(device)
        self._task = asyncio.ensure_future(self._sync(device))

    async def _sync(self, device: Device) -> None:
        """Run :func:`set_clock`, and write the result to the log."""
        log = self._ctx.log
        try:
            done = await set_clock(device)
        except asyncio.CancelledError:
            raise
        except ClockAheadError as exc:
            log.warning("clock sync: left the device clock alone: %s", exc)
            return
        except Exception as exc:  # noqa: BLE001 - best-effort. We must not break the link.
            log.warning("clock sync: could not set the device clock: %s", exc)
            return
        if not done.written:
            log.info(
                "clock sync: device clock already in sync (%+d s, ahead of ours; firmware "
                "never sets it back)",
                done.drift_s,
            )
        elif done.drift_s is None:
            log.info("clock sync: device clock set to %s (drift unknown)", done.stamp)
        else:
            log.info("clock sync: device clock set to %s (was %+d s off)", done.stamp, done.drift_s)
