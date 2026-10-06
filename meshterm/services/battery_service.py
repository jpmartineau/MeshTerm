# SPDX-License-Identifier: Apache-2.0
"""The battery poller: keep the fuel gauge in the status bar current, in the background.

This module is a task that runs for the full session. At a slow interval, it reads the
battery voltage of the connected companion. It caches a small :class:`BatteryReading`,
which the header draws (refer to :func:`~meshterm.ui.widgets.battery_cell`). The task has
no device subscription, the same as the advert scheduler and the Watchtower. Each pass
checks if a device is connected, and if not, it does nothing. Thus reconnects have no
effect on the task: it continues to tick while the link is down, and it reads again when
the device is back.

The platform data (``Platform.battery``) sets which pack the task reads. On the
**PicoCalc** and the **Cardputer Zero**, the gauge is the gauge of the handheld. The kernel
publishes it as a standard ``power_supply`` (:func:`host_supply`). On the PicoCalc, the
value comes from the BMS, through the I2C register block of the keyboard MCU. On the
Cardputer, it comes from a BQ27220 fuel gauge. Thus the handheld itself measures both
numbers: ``capacity`` is a real percent, and ``status`` is a real charging flag. On these
handhelds, the code below estimates nothing: no voltage curve and no trend.

The rest of this module is the **companion** path, where the device gives much less
information. Two facts about the device control what this path can report:

* The firmware gives the pack as a **terminal voltage in millivolts**, not as a
  percentage. Thus the module changes the reading into an estimate of the state of charge,
  with a discharge curve for a single-cell LiPo (:func:`battery_percent`). A device with no
  battery gauge answers with no usable level. The module reports such a device as
  *absent*, so the header shows nothing for it.
* The companion protocol gives **no charging flag at all**. Thus the module *infers* the
  charging state from the terminal voltage. But the voltage of a single-cell LiPo falls by
  tens of millivolts during each LoRa transmission, and comes back up when the radio is
  idle. Thus the trend of the raw samples is mostly noise from the load, not charge. The
  inference uses a robust method: it compares the median of the older half of the sample
  window with the median of its newer half, and this comparison ignores the short spikes.
  The inference reports charging only for a large rise that *continues*. As soon as the
  voltage of the pack stops rising, it reports not charging again.

  The result is still an estimate (the best estimate that the companion protocol makes
  possible). But a pack that discharges or rests does not cause a false charging state
  now. A device can give a firmware charging flag through the standard BLE Battery Level
  Status characteristic (no MeshCore build does this today). If it does, the module uses
  that measured flag, and the inference is only for the other devices. Refer to
  :meth:`~meshterm.core.connection.Device.get_hw_charging`.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING

from ..platforms import get_platform

if TYPE_CHECKING:
    from ..context import AppContext

#: The time (seconds) between two battery reads. The charge of a pack changes slowly, thus a
#: long interval is sufficient. The interval is also short enough that the charging trend
#: (measured over :data:`_TREND_WINDOW_S`) has several samples to use.
POLL_S = 20.0

#: The minimum level (millivolts) at which a reading counts as a real battery. A device with
#: no fuel gauge answers with an empty protocol frame or a zero level. A value below that of
#: a very discharged single cell means *no battery present*, and the gauge is hidden.
_BATTERY_PRESENT_FLOOR_MV = 1500

#: The time window (seconds) of recent samples, over which the charging trend is measured.
_TREND_WINDOW_S = 180.0

#: The minimum number of samples before the trend gives a verdict, charging or not charging.
#: At :data:`POLL_S`, that is approximately two minutes. This is sufficient to fill the
#: window and to give each half a useful median. Below this number, the verdict is *not
#: charging*.
_TREND_MIN_SAMPLES = 6

#: The smoothed rise (millivolts: the median terminal voltage of the newer half minus that
#: of the older half) that *starts* a charging verdict, and the smaller rise that *keeps*
#: the verdict. The start threshold is much higher than the noise floor of the load after
#: the median smoothing. Thus a pack that discharges or rests never starts the verdict.
#: With the hold threshold, a real charge that continues to rise stays charging through the
#: jitter of the polls, and the verdict does not flicker. But when the voltage of a pack
#: stops rising (full, or unplugged), the rise falls below this threshold, and the verdict
#: goes back to *not charging* immediately.
_CHARGING_RISE_ON_MV = 25
_CHARGING_RISE_HOLD_MV = 8

#: The number of samples to keep: the trend window, plus a small margin at the poll interval.
_TREND_SAMPLES = int(_TREND_WINDOW_S / POLL_S) + 2

#: The directory where the kernel lists each ``power_supply``. The pack of the host is the
#: supply of type ``Battery`` that is not the supply of a peripheral (:func:`host_supply`).
#: The PicoCalc supply ``picocalc`` (confirmed in P0) and the Cardputer Zero BQ27220 gauge,
#: ``bq27220-0``, both qualify by their type, not by a name in a list.
_POWER_SUPPLIES = Path("/sys/class/power_supply")

#: The number of times that a poll asks for the percent of the pack before it stops, and
#: the pause between two requests. The BQ27220 of the Cardputer Zero shares I2C bus 1 with
#: the keyboard and the clock. While the Cap on its header has power, that bus is noisy.
#: Measurements on the handheld gave these results: one quarter to three quarters of the
#: reads of the gauge fail with an I/O error (one in ten of the reads of the clock). With
#: the header off, no read fails. Some reads that "succeed" are garbled, for example a
#: ``capacity`` of 56414. The ``bq27xxx`` driver of the kernel caches what it read for five
#: seconds, a failure or a garbled value the same. Thus a new request before that time gets
#: the same answer, and the pause is a little longer than five seconds.
_HOST_READ_ATTEMPTS = 3
_HOST_RETRY_S = 5.5

#: The number of consecutive polls that can fail to read a host pack before its gauge is
#: removed. A pack does not change in two minutes. Thus the last reading stays, and the
#: gauge does not blink out.
_HOST_MISSES_KEPT = 6

#: The maximum change of a percent between two polls that MeshTerm accepts immediately. A
#: pack does not gain or lose this much in :data:`POLL_S`. MeshTerm holds a larger step
#: until the next poll gives the same value. This catches a garbled read that is in the
#: range 0–100 by chance.
_HOST_STEP_PCT = 5


async def _sysfs(path: Path) -> str:
    """One sysfs value, read outside the event loop.

    A read of a gauge value can make the kernel communicate with the gauge over I2C. On a
    noisy bus, a transfer that fails takes time. The loop that draws the screen must not
    wait for it.
    """
    return (await asyncio.to_thread(path.read_text)).strip()


async def _read_percent(path: Path) -> int:
    """The percent of the pack from ``capacity``, asked again while it is not read or garbled.

    Here, an I/O error and a percent outside 0–100 have the same meaning: no reading. The
    function asks again after :data:`_HOST_RETRY_S`. At that time, the driver reads the
    gauge again.

    Raises:
        OSError: When :data:`_HOST_READ_ATTEMPTS` tries gave no percent.
    """
    for attempt in range(_HOST_READ_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_HOST_RETRY_S)
        try:
            value = int(await _sysfs(path))
        except FileNotFoundError:
            raise  # the value does not exist: a new request cannot make it
        except (OSError, ValueError):
            continue
        if 0 <= value <= 100:
            return value
    raise OSError(f"{path} gave no percent")


def host_supply(root: Path = _POWER_SUPPLIES) -> Path | None:
    """The battery of the host: the first ``Battery`` supply that is not that of a peripheral.

    The battery of a wireless mouse or of a game pad is also a ``power_supply``, and it has
    ``scope = Device``. The battery of the host has no ``scope`` value, or ``System``.
    ``None`` when there is no such battery, or when the directory cannot be read.
    """
    try:
        supplies = sorted(root.iterdir())
    except OSError:
        return None
    for supply in supplies:
        try:
            kind = (supply / "type").read_text().strip()
        except OSError:
            continue
        try:
            scope = (supply / "scope").read_text().strip()
        except OSError:
            scope = ""
        if kind == "Battery" and scope != "Device":
            return supply
    return None


#: A lookup table for a single-cell LiPo, terminal voltage → state of charge,
#: ``(millivolts, percent)``, from high to low. The discharge curve is not a straight line:
#: most of the usable charge is in a narrow band at approximately 3.7–3.9 V. Thus a
#: piecewise table with linear interpolation follows the curve much better than one straight
#: line from voltage to percent. These steps are the values that the community and Battery
#: University use widely.
_LIPO_SOC: tuple[tuple[int, int], ...] = (
    (4200, 100),
    (4150, 95),
    (4110, 90),
    (4080, 85),
    (4020, 80),
    (3980, 75),
    (3950, 70),
    (3910, 65),
    (3870, 60),
    (3850, 55),
    (3840, 50),
    (3820, 45),
    (3800, 40),
    (3790, 35),
    (3770, 30),
    (3750, 25),
    (3730, 20),
    (3710, 15),
    (3690, 10),
    (3610, 5),
    (3270, 0),
)


def battery_percent(millivolts: int) -> int:
    """Estimate the state of charge (0–100%) of a single-cell LiPo from its terminal mV.

    The function reads the :data:`_LIPO_SOC` curve. It does a linear interpolation between
    the two steps that the voltage is between. Past either end, it clamps the value.

    Args:
        millivolts: The terminal voltage of the pack in millivolts.

    Returns:
        The estimated charge as a whole percent in ``[0, 100]``.
    """
    mv = int(millivolts)
    if mv >= _LIPO_SOC[0][0]:
        return 100
    if mv <= _LIPO_SOC[-1][0]:
        return 0
    for (v_hi, p_hi), (v_lo, p_lo) in zip(_LIPO_SOC, _LIPO_SOC[1:], strict=False):
        if v_lo <= mv <= v_hi:
            frac = (mv - v_lo) / (v_hi - v_lo)
            return round(p_lo + (p_hi - p_lo) * frac)
    return 0  # pragma: no cover - the clamps above cover the full range


@dataclass(frozen=True)
class BatteryReading:
    """One cached battery snapshot, which the header draws.

    Attributes:
        millivolts: The terminal voltage of the pack, as the firmware reported it.
        percent: The estimate of the state of charge (refer to :func:`battery_percent`).
        charging: True if the pack seems to take charge. This value is inferred from a
            rise in terminal voltage that continues, because the firmware gives no
            charging flag.
    """

    millivolts: int
    percent: int
    charging: bool


class BatteryService:
    """Poll the battery of the connected companion, and cache a reading for the header.

    Use the async lifecycle methods (:meth:`start`, :meth:`stop`, :meth:`aclose`), and read
    the latest snapshot with :meth:`reading`. All other work occurs on the schedule of the
    loop.
    """

    def __init__(self, ctx: AppContext) -> None:
        """Initialize an idle poller that is bound to an application context.

        Args:
            ctx: The shared application context. Each pass reads the connected device from
                it. The poller does nothing before :meth:`start`.
        """
        self._ctx = ctx
        self._task: asyncio.Task | None = None
        self._reading: BatteryReading | None = None
        #: The recent ``(monotonic_time, millivolts)`` samples, for the charging trend.
        self._history: deque[tuple[float, int]] = deque(maxlen=_TREND_SAMPLES)
        #: The number of consecutive reads of the host pack that failed (refer to
        #: :data:`_HOST_MISSES_KEPT`).
        self._host_misses = 0
        #: A host percent that is too far from the shown percent to accept yet
        #: (:data:`_HOST_STEP_PCT`).
        self._host_unconfirmed: int | None = None

    @property
    def active(self) -> bool:
        """True while the poll loop runs."""
        return self._task is not None and not self._task.done()

    def reading(self) -> BatteryReading | None:
        """The latest battery snapshot, or ``None`` when it is unknown or no battery is present."""
        return self._reading

    async def start(self) -> None:
        """Start the poll loop. Idempotent. It never waits for the first read."""
        if self.active:
            return
        self._task = asyncio.ensure_future(self._run())
        self._ctx.log.info("battery poller started")

    async def stop(self) -> None:
        """Stop the poll loop. Idempotent."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._ctx.log.info("battery poller stopped")

    async def aclose(self) -> None:
        """Stop the loop at the end of the session (an alias for :meth:`stop`)."""
        await self.stop()

    async def _run(self) -> None:
        """Read one time immediately, then tick with no end: a best-effort pass, then a sleep."""
        while True:
            try:
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad read must not stop the loop
                self._ctx.log.debug("battery poller: read failed: %s", exc)
            await asyncio.sleep(POLL_S)

    async def _poll(self) -> None:
        """One pass: read the pack, estimate the charge, and calculate the charging trend again.

        The platform (``Platform.battery``) sets which pack the pass reads. The PicoCalc
        gauge reports the cells of the *handheld* through the standard ``power_supply``
        driver of the kernel. That driver gives a percent and a real charging flag, and no
        estimate is necessary. The regular platform polls the connected companion over the
        mesh link.
        """
        if get_platform().battery == "host":
            await self._poll_host()
            return
        ctx = self._ctx
        if not ctx.is_connected:
            return
        device = await ctx.device()
        info = await device.get_battery()
        mv = int(info.get("level") or 0)
        if mv < _BATTERY_PRESENT_FLOOR_MV:
            # No usable level: this companion has no battery gauge. Report that the battery
            # is absent, so that the header draws nothing. Also forget an old trend from a
            # different device.
            self._reading = None
            self._history.clear()
            return
        now = time.monotonic()
        self._history.append((now, mv))
        # When the device gives a charging flag from its firmware (the standard BLE Battery
        # Level Status characteristic), use that flag. Only when the device cannot give it
        # (all MeshCore devices today), infer the charge from the voltage trend.
        hw_charging = await device.get_hw_charging()
        charging = hw_charging if hw_charging is not None else self._charging(now)
        self._reading = BatteryReading(
            millivolts=mv,
            percent=battery_percent(mv),
            charging=charging,
        )

    async def _poll_host(self) -> None:
        """Read the pack of the host from sysfs (:func:`host_supply`).

        The driver is a standard ``power_supply``: ``capacity`` is a true percent, and
        ``status`` is a real charging flag (checked in P0). Thus the voltage curve and the
        trend estimate of the companion path do not apply. A supply that cannot be read
        (no driver, or no permission) reports *absent*, and the header draws no gauge. But
        this occurs only after :data:`_HOST_MISSES_KEPT` consecutive polls failed. A failed
        poll after a good poll keeps the good reading.

        The method asks for the percent again while it cannot be read or is out of range
        (:func:`_read_percent`). A step that is larger than :data:`_HOST_STEP_PCT` waits
        for the next poll to confirm it. ``status`` is read from the same driver snapshot
        as the percent. When ``status`` cannot be read, the charging verdict of the last
        good reading stays. The percent is the important value, and the flag is only
        extra information.
        """
        supply = host_supply(_POWER_SUPPLIES)
        try:
            if supply is None:
                raise OSError("no battery power_supply")
            percent = await _read_percent(supply / "capacity")
        except OSError:
            self._host_misses += 1
            if self._host_misses > _HOST_MISSES_KEPT:
                self._reading = None
            return
        self._host_misses = 0
        shown = self._reading
        if shown is not None and abs(percent - shown.percent) > _HOST_STEP_PCT:
            if percent != self._host_unconfirmed:
                self._host_unconfirmed = percent  # accepted when the next poll gives it too
                return
        self._host_unconfirmed = None
        try:
            charging = await _sysfs(supply / "status") == "Charging"
        except OSError:
            charging = shown.charging if shown is not None else False
        try:  # only for information. The header draws the percent and the charging flag.
            mv = int(await _sysfs(supply / "voltage_now")) // 1000
        except (OSError, ValueError):
            mv = 0
        self._reading = BatteryReading(
            millivolts=mv,
            percent=percent,
            charging=charging,
        )

    def _charging(self, now: float) -> bool:
        """Infer from the recent trend of the terminal voltage if the pack is charging.

        The firmware gives no charging flag, thus the trend is the only signal. But the
        terminal voltage of a single-cell LiPo falls by tens of millivolts during each
        transmission, and comes back up when the radio is idle. Thus the raw trend is mostly
        noise from the load. The method reads the charge with a *robust* method. It compares
        the median voltage of the older half of the window with the median of its newer
        half. This comparison ignores the short spikes of each fall and recovery, and only
        a large rise that *continues* counts as charge that goes in.

        The verdict has hysteresis. It is difficult to start (:data:`_CHARGING_RISE_ON_MV`),
        and then it stays while the pack still clearly rises
        (:data:`_CHARGING_RISE_HOLD_MV`). Thus a real charge does not flicker. But when the
        voltage of a pack stops rising (full, or unplugged), the verdict goes back to *not
        charging* immediately.

        Args:
            now: The current monotonic time (the timestamp of the sample that was taken
                last).

        Returns:
            The charging estimate for this reading.
        """
        charging = self._reading.charging if self._reading is not None else False
        recent = [mv for t, mv in self._history if now - t <= _TREND_WINDOW_S]
        if len(recent) < _TREND_MIN_SAMPLES:
            return False
        mid = len(recent) // 2
        rise = median(recent[mid:]) - median(recent[:mid])
        return rise >= (_CHARGING_RISE_HOLD_MV if charging else _CHARGING_RISE_ON_MV)
