# SPDX-License-Identifier: Apache-2.0
"""The battery poller: keep the status bar's fuel gauge current, quietly.

A session-long task that reads the connected companion's battery voltage on a slow
cadence and caches a small :class:`BatteryReading` for the header to draw (see
:func:`~meshterm.ui.widgets.battery_cell`). Like the advert scheduler and the Watchtower
it holds no device subscription — each pass just checks whether a device is connected and
skips quietly otherwise — so it is indifferent to reconnects, ticking across a link drop
and resuming once the radio is back.

Which pack it reads is platform data (``Platform.battery``). On the **PicoCalc** and the
**Cardputer Zero** the gauge is the handheld's own, published by the kernel as a standard
``power_supply`` (:func:`host_supply`) — the PicoCalc's BMS through its keyboard MCU's I2C
register block, the Cardputer's through a BQ27220 fuel gauge — so both numbers are the
device's own ground truth: ``capacity`` is a real percent and ``status`` a real charging flag.
Nothing below is estimated there — no voltage curve, no trend.

The rest of this module is the **companion** path, where the radio gives far less. Two device
facts shape what it can report:

* Firmware exposes the pack as a **terminal voltage in millivolts**, not a percentage, so
  the reading is turned into a state-of-charge estimate against a single-cell LiPo
  discharge curve (:func:`battery_percent`). Devices with no battery gauge answer with no
  usable level; those are reported as *absent* so the header shows nothing for them.
* The companion protocol exposes **no charging flag at all**. Charging is therefore *inferred*
  from the terminal voltage — but a single-cell LiPo sags tens of millivolts under each LoRa
  transmission and springs back when the radio idles, so the raw sample trend is mostly load
  noise, not charge. The inference reads it robustly (the median of the sample window's older
  half against its newer half, which discards those transient spikes) and calls charge only
  on a large, *sustained* rise, dropping straight back the moment the pack goes flat. It is
  still an estimate — the best the companion protocol allows — but one a discharging or
  resting pack no longer trips. Where a device *does* expose a firmware charging flag over the
  standard BLE Battery Level Status characteristic (no MeshCore build does today), that ground
  truth is preferred and the inference only fills in for the rest — see
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

#: Seconds between battery reads. A pack drifts slowly, so a coarse tick is plenty; it is
#: also fine enough that the charging trend (measured over :data:`_TREND_WINDOW_S`) has
#: several samples to work with.
POLL_S = 20.0

#: Millivolts at or above which a reading counts as a real battery. A device with no fuel
#: gauge answers with an empty frame or a zero level; anything below a badly-depleted
#: single cell is treated as *no battery present* and the gauge is hidden.
_BATTERY_PRESENT_FLOOR_MV = 1500

#: Recent-samples window (seconds) the charging trend is measured over.
_TREND_WINDOW_S = 180.0

#: Fewest samples the trend needs before it will call charging either way — about two minutes
#: at :data:`POLL_S`, enough to span the window and give each half a meaningful median. Below
#: this the verdict is simply *not charging*.
_TREND_MIN_SAMPLES = 6

#: Smoothed rise (millivolts: the newer half's median terminal voltage minus the older half's)
#: that *starts* a charging verdict, and the smaller rise it must stay above to *hold* one.
#: The start threshold clears the median-smoothed load-noise floor by a wide margin, so a
#: discharging or resting pack never trips it; the hold threshold lets a genuine, still-climbing
#: charge ride through the poll jitter without flicker, while a pack gone flat — topped off, or
#: unplugged — falls below it and drops straight back to *not charging*.
_CHARGING_RISE_ON_MV = 25
_CHARGING_RISE_HOLD_MV = 8

#: How many samples to retain — the trend window plus a little slack at the poll cadence.
_TREND_SAMPLES = int(_TREND_WINDOW_S / POLL_S) + 2

#: Where the kernel lists every ``power_supply``. The host's own pack is the one of type
#: ``Battery`` that isn't a peripheral's (:func:`host_supply`): the PicoCalc's ``picocalc``
#: (confirmed in P0) and the Cardputer Zero's BQ27220 gauge, ``bq27220-0``, both qualify by
#: what they are rather than by a name kept on a list.
_POWER_SUPPLIES = Path("/sys/class/power_supply")

#: How often the pack's percent is asked for before a poll gives up, and the pause between
#: asks. The Cardputer Zero's BQ27220 shares I2C bus 1 with the keyboard and the clock,
#: and while the Cap on its header is powered that bus turns noisy: measured on the device,
#: a quarter to three quarters of the gauge's reads fail with an I/O error (one in ten of
#: the clock's), against none with the header off, and some that "succeed" are garbled —
#: a ``capacity`` of 56414. The kernel's ``bq27xxx`` driver caches what it read, failure or
#: garbage alike, for five seconds, so asking again sooner only gets the same answer back:
#: the pause is just past that.
_HOST_READ_ATTEMPTS = 3
_HOST_RETRY_S = 5.5

#: How many polls in a row a host pack may fail to read before its gauge is taken down — a
#: pack doesn't change in two minutes, so the last reading stands rather than blinking out.
_HOST_MISSES_KEPT = 6

#: The most a percent may move between two polls and be believed at once. A pack doesn't
#: gain or lose this much in :data:`POLL_S`; a bigger step is held until the next poll says
#: the same, which catches a garbled read that happens to land inside 0–100.
_HOST_STEP_PCT = 5


async def _sysfs(path: Path) -> str:
    """One sysfs value, read off the event loop.

    Reading a gauge's value can make the kernel talk to it over I2C, and on a noisy bus a
    failing transfer takes its time; the loop drawing the screen shouldn't wait on it.
    """
    return (await asyncio.to_thread(path.read_text)).strip()


async def _read_percent(path: Path) -> int:
    """The pack's percent from ``capacity``, asked again while it is unreadable or garbled.

    An I/O error and a percent outside 0–100 are the same thing here — no reading — and are
    asked again after :data:`_HOST_RETRY_S`, by which time the driver reads the gauge anew.

    Raises:
        OSError: When :data:`_HOST_READ_ATTEMPTS` tries gave no percent.
    """
    for attempt in range(_HOST_READ_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_HOST_RETRY_S)
        try:
            value = int(await _sysfs(path))
        except FileNotFoundError:
            raise  # no such value: asking again won't make one
        except (OSError, ValueError):
            continue
        if 0 <= value <= 100:
            return value
    raise OSError(f"{path} gave no percent")


def host_supply(root: Path = _POWER_SUPPLIES) -> Path | None:
    """The host's own battery: the first ``Battery`` supply that isn't a peripheral's.

    A wireless mouse's or a game pad's battery is a ``power_supply`` too, and carries
    ``scope = Device``; the host's carries no scope, or ``System``. ``None`` when there is
    none, or the directory can't be read.
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


#: Single-cell LiPo terminal-voltage → state-of-charge lookup ``(millivolts, percent)``,
#: high to low. The discharge curve is far from a straight line — most of the usable charge
#: sits in a narrow band around 3.7–3.9 V — so a piecewise table read with linear
#: interpolation tracks it far better than a flat voltage-to-percent line. These are the
#: widely-used community/Battery-University rungs.
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
    """Estimate a single-cell LiPo's state of charge (0–100%) from its terminal mV.

    Reads the :data:`_LIPO_SOC` curve, interpolating linearly between the two rungs the
    voltage falls between and clamping past either end.

    Args:
        millivolts: The pack's terminal voltage in millivolts.

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
    return 0  # pragma: no cover - the clamps above cover the whole range


@dataclass(frozen=True)
class BatteryReading:
    """One cached battery snapshot for the header to draw.

    Attributes:
        millivolts: The pack's terminal voltage, as the firmware reported it.
        percent: The state-of-charge estimate (see :func:`battery_percent`).
        charging: Whether the pack appears to be taking charge (inferred from a sustained
            rise in terminal voltage — the firmware exposes no charging flag).
    """

    millivolts: int
    percent: int
    charging: bool


class BatteryService:
    """Polls the connected companion's battery and caches a reading for the header.

    Interact through the async lifecycle methods (:meth:`start`, :meth:`stop`,
    :meth:`aclose`) and read the latest snapshot with :meth:`reading`; everything else
    happens on the loop's own schedule.
    """

    def __init__(self, ctx: AppContext) -> None:
        """Initialize an idle poller bound to an application context.

        Args:
            ctx: The shared application context, read each pass for the connected device.
                Nothing is touched until :meth:`start`.
        """
        self._ctx = ctx
        self._task: asyncio.Task | None = None
        self._reading: BatteryReading | None = None
        #: Recent ``(monotonic_time, millivolts)`` samples, for the charging trend.
        self._history: deque[tuple[float, int]] = deque(maxlen=_TREND_SAMPLES)
        #: Host-pack reads failed in a row (see :data:`_HOST_MISSES_KEPT`).
        self._host_misses = 0
        #: A host percent too far from the shown one to believe yet (:data:`_HOST_STEP_PCT`).
        self._host_unconfirmed: int | None = None

    @property
    def active(self) -> bool:
        """Whether the poll loop is running."""
        return self._task is not None and not self._task.done()

    def reading(self) -> BatteryReading | None:
        """The latest battery snapshot, or ``None`` when unknown or no battery is present."""
        return self._reading

    async def start(self) -> None:
        """Start the poll loop. Idempotent; never blocks on the first read."""
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
        """Stop the loop at session end (an alias for :meth:`stop`)."""
        await self.stop()

    async def _run(self) -> None:
        """Read once at once, then tick forever: a best-effort pass, then sleep."""
        while True:
            try:
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad read must not kill the loop
                self._ctx.log.debug("battery poller: read failed: %s", exc)
            await asyncio.sleep(POLL_S)

    async def _poll(self) -> None:
        """One pass: read the pack, estimate charge, and re-derive the charging trend.

        Which pack depends on the platform (``Platform.battery``): the PicoCalc's gauge
        reports the *handheld's* cells through the kernel's standard ``power_supply``
        driver — a percent and a real charging flag, no estimation needed — while the
        regular platform polls the connected companion radio over the mesh link.
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
            # No usable level — this companion has no battery gauge; report absence so the
            # header draws nothing, and forget any stale trend from a different device.
            self._reading = None
            self._history.clear()
            return
        now = time.monotonic()
        self._history.append((now, mv))
        # Prefer a firmware-reported charging flag when the device exposes one (the standard
        # BLE Battery Level Status characteristic); only where it can't — every MeshCore device
        # today — fall back to inferring charge from the voltage trend.
        hw_charging = await device.get_hw_charging()
        charging = hw_charging if hw_charging is not None else self._charging(now)
        self._reading = BatteryReading(
            millivolts=mv,
            percent=battery_percent(mv),
            charging=charging,
        )

    async def _poll_host(self) -> None:
        """Read the host's own pack from sysfs (:func:`host_supply`).

        The driver is a standard ``power_supply``: ``capacity`` is a true percent and
        ``status`` a real charging flag (verified in P0), so none of the companion
        path's voltage-curve or trend estimation applies. An unreadable supply (driver
        missing, permissions) reports *absent* and the header simply draws no gauge —
        once it has failed :data:`_HOST_MISSES_KEPT` polls running; a failed poll after a
        good one keeps the good one. The percent is asked again while it is unreadable or
        out of range (:func:`_read_percent`), and a step bigger than
        :data:`_HOST_STEP_PCT` waits for the next poll to confirm it. ``status`` is read
        from the same driver snapshot as the percent; when it won't read, the charging
        verdict the last good one gave stands: the percent is the reading, the flag its
        garnish.
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
                self._host_unconfirmed = percent  # believed when the next poll says it too
                return
        self._host_unconfirmed = None
        try:
            charging = await _sysfs(supply / "status") == "Charging"
        except OSError:
            charging = shown.charging if shown is not None else False
        try:  # informational only; the header draws percent + charging
            mv = int(await _sysfs(supply / "voltage_now")) // 1000
        except (OSError, ValueError):
            mv = 0
        self._reading = BatteryReading(
            millivolts=mv,
            percent=percent,
            charging=charging,
        )

    def _charging(self, now: float) -> bool:
        """Infer whether the pack is charging from its recent terminal-voltage trend.

        The firmware exposes no charging flag, so the trend is the only signal — but a
        single-cell LiPo's terminal voltage sags tens of millivolts under each transmission
        and springs back when the radio idles, so the raw trend is mostly load noise.
        Charging is read *robustly*: the median voltage of the window's older half is compared
        against the median of its newer half, which discards those transient sag/recovery
        spikes, and only a large, *sustained* rise counts as charge going in. The verdict
        carries hysteresis — hard to start (:data:`_CHARGING_RISE_ON_MV`), then held while the
        pack is still clearly climbing (:data:`_CHARGING_RISE_HOLD_MV`) — so a genuine charge
        doesn't flicker, while a pack gone flat (topped off, or unplugged) drops straight back
        to *not charging*.

        Args:
            now: The current monotonic time (the just-taken sample's timestamp).

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
