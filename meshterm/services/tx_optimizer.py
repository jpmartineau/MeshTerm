# SPDX-License-Identifier: Apache-2.0
"""Remote-admin TX-power optimization.

This module tunes the transmit power of a *remote* node on which we have admin rights.
That node is one hop before a selected target. The module tunes it to get the strongest
signal that the **target** receives from it. We force a path (the same as the trace tool),
so that each measurement uses the same admin-node→target link. Then we read the SNR that
the target reports at the end of that path.

Radio links are noisy, and the real response is not monotonic. With too little power, the
target cannot hear the node. With too much power, the front end of the target saturates.
Thus the search is robust by design:

1. A **coarse sweep** across the TX range. It runs several traces at each level. It scores
   each level first by its trace success rate, and second by the *median* SNR at the
   target.
2. A **local refine** that measures the integer levels around the coarse winner.
3. A **verify** pass that measures the best candidate again, with more samples. Thus one
   lucky (or unlucky) reading cannot decide the optimum.

The selection is lexicographic: **reliability first, then SNR**. When two SNRs are almost
equal, the selection prefers the *lower* TX power, because more power for a fraction of a
dB only adds interference and uses duty cycle. When ``apply`` is set, the module writes
the selected optimum back to the admin node (the purpose of the run is to leave the node
tuned).
"""

from __future__ import annotations

import asyncio
import statistics
from collections.abc import Callable

from ..core.connection import REMOTE_TX_MAX, REMOTE_TX_MIN, Device
from ..core.models import Contact, TraceResult, TraceStats, TxLevelResult, TxOptResult
from . import trace_runner

LevelCallback = Callable[[int, int, TxLevelResult], None]
PersistLevel = Callable[[TxLevelResult], None]
PhaseCallback = Callable[[str], None]

#: The search phases, in order, as reported to ``on_phase``: the coarse grid sweep, the
#: integer levels around the coarse winner, and the new measurement of the leader with more
#: samples.
PHASES = ("coarse", "refine", "verify")

#: When two SNRs at the target are almost equal (within this number of dB of the best), the
#: lower TX power wins.
DEFAULT_SNR_TOLERANCE_DB = 1.0


def trace_target_snr(trace: TraceResult, target_hash: str) -> float | None:
    """Return the SNR that the *target* received on a single (round-trip) trace.

    We trace out to the target and back, so that a node that we can reach answers, not the
    far target. Thus the target is not the last hop: it is the turn-around point, and we
    find it by its hash, not by its position. The SNR of the matched hop is the signal that
    the target heard from the admin node before it. That is exactly the link that the
    module tunes.

    Args:
        trace: A single trace result.
        target_hash: The path hash of the target node (the first bytes of its public key).

    Returns:
        The received SNR of the target in dB, or ``None`` if the trace failed or the
        target hop was not in the reply.
    """
    if not trace.success:
        return None
    needle = target_hash.lower().removeprefix("0x")
    for hop in trace.hops:
        if hop.node is None:
            continue
        node = hop.node.lower().removeprefix("0x")
        # Match in both directions: the firmware may report hashes at a width that is
        # different from the width that we used to address the path.
        if node.startswith(needle) or needle.startswith(node):
            return hop.snr
    return None


def build_level(tx: int, target: str, target_hash: str, traces: list[TraceResult]) -> TxLevelResult:
    """Aggregate the traces measured at one TX level into a :class:`TxLevelResult`.

    Args:
        tx: The TX power at which these traces were taken.
        target: The target node (for the embedded :class:`TraceStats`).
        target_hash: The path hash of the target, used to find its hop in each trace.
        traces: Each trace that ran at this level.

    Returns:
        The robust aggregate of the level. ``target_snr`` is the median of the successful
        traces. Thus one outlier reading changes it very little.
    """
    successes = [t for t in traces if t.success]
    snrs = [snr for t in successes if (snr := trace_target_snr(t, target_hash)) is not None]
    target_snr = statistics.median(snrs) if snrs else None
    return TxLevelResult(
        tx_power=tx,
        samples=len(traces),
        successes=len(successes),
        target_snr=target_snr,
        score=target_snr if target_snr is not None else float("-inf"),
        stats=TraceStats.from_traces(target, traces),
    )


def select_best(
    levels: list[TxLevelResult], *, snr_tolerance: float = DEFAULT_SNR_TOLERANCE_DB
) -> TxLevelResult:
    """Select the optimal level: reliability first, then SNR, then the lowest TX.

    From the levels with the highest trace success rate, take the levels whose median
    target SNR is within ``snr_tolerance`` of the best. Return the level that uses the
    least power. The tolerance band makes the selection robust against measurement noise.
    A level with more power and an SNR that is a fraction higher does not win if a level
    with less power is very near.

    Args:
        levels: The measured levels (must not be empty).
        snr_tolerance: The band (dB) within which SNRs count as equal.

    Returns:
        The selected :class:`TxLevelResult`.
    """
    best_rate = max(lv.success_rate for lv in levels)
    contenders = [lv for lv in levels if lv.success_rate >= best_rate - 1e-9]
    best_snr = max(_snr_or_floor(lv) for lv in contenders)
    near = [lv for lv in contenders if _snr_or_floor(lv) >= best_snr - snr_tolerance]
    return min(near, key=lambda lv: lv.tx_power)


def _snr_or_floor(level: TxLevelResult) -> float:
    """Return the target SNR of a level, or negative infinity if no trace got through."""
    return level.target_snr if level.target_snr is not None else float("-inf")


async def optimize_tx_power(
    device: Device,
    target: str,
    admin_node: Contact,
    path: str,
    *,
    tx_min: int = REMOTE_TX_MIN,
    tx_max: int = REMOTE_TX_MAX,
    coarse_step: int = 3,
    samples_per_level: int = 3,
    refine: bool = True,
    verify: bool = True,
    apply: bool = True,
    cooldown_s: float = 1.0,
    snr_tolerance: float = DEFAULT_SNR_TOLERANCE_DB,
    on_level: LevelCallback | None = None,
    on_phase: PhaseCallback | None = None,
    persist_level: PersistLevel | None = None,
    persist_trace: Callable | None = None,
) -> TxOptResult:
    """Tune the TX power of ``admin_node`` for the best signal at ``target``.

    The caller must already be logged in to ``admin_node`` (refer to
    :meth:`~meshterm.core.connection.Device.admin_login`).

    Args:
        device: The connected local device. It sends the traces and controls the remote
            node.
        target: The node whose received SNR the function makes as high as possible (the
            last hop of ``path``).
        admin_node: The remote node whose TX power is tuned (the hop before ``target``).
        path: The one-way forced path out to ``target`` (comma-separated hashes, with the
            target at the end). Internally, the function traces it as a round trip, out
            and back, so that a node that we can reach answers. The far target must only
            forward the packet.
        tx_min: The lowest TX power to try.
        tx_max: The highest TX power to try.
        coarse_step: The step between two levels of the coarse sweep.
        samples_per_level: The number of traces averaged at each level (and added again
            on verify).
        refine: True to measure the integer levels around the coarse winner.
        verify: True to measure the best candidate again, to reject an outlier.
        apply: Leave the winning TX power on the node at the end. If False, set the power
            back to the value before the sweep, if that value could be read.
        cooldown_s: The delay between two traces (for duty-cycle safety).
        snr_tolerance: The band (dB) for the tie-break to the lower power (refer to
            :func:`select_best`).
        on_level: An optional progress callback ``(completed, total, level_result)``.
        on_phase: An optional callback that tells each search phase when it starts (one
            of :data:`PHASES`). Thus a live screen can say *what type* of measurement
            occurs.
        persist_level: An optional callback to store the aggregated result of each level.
        persist_trace: An optional callback to store each trace.

    Returns:
        A :class:`TxOptResult`.

    Raises:
        ValueError: If the resolved TX range is empty.
    """
    if tx_min > tx_max:
        raise ValueError(f"Empty TX range: {tx_min}..{tx_max}")

    original_tx = await device.get_remote_tx_power(admin_node)

    # Trace out to the target and back. Then the reply comes from a node near us (the
    # first hop), not from the far target, which must only forward the packet. The
    # received SNR of the target is read from its hop at the turn-around point of the
    # round trip.
    outbound = [h for h in path.split(",") if h]
    target_hash = outbound[-1] if outbound else path
    trace_path = _round_trip_path(outbound)

    # Keep the raw traces for each level. Thus a verify pass can *add* samples to a level
    # and aggregate again, and does not discard what we measured before.
    traces_by_tx: dict[int, list[TraceResult]] = {}
    levels: dict[int, TxLevelResult] = {}

    coarse = coarse_levels(tx_min, tx_max, coarse_step)
    total_estimate = len(coarse) + (2 * coarse_step if refine else 0) + (1 if verify else 0)
    completed = 0

    async def measure_level(tx: int) -> TxLevelResult:
        """Run a batch of traces at ``tx`` (added to earlier ones), and refresh its aggregate."""
        nonlocal completed
        if completed and cooldown_s > 0:
            # ``run_traces`` waits between its own samples, but not after the last sample.
            # Without this sleep, the first trace of a level follows the last trace of the
            # previous level with no gap. That is a burst at each boundary between levels.
            await asyncio.sleep(cooldown_s)
        await device.set_remote_tx_power(admin_node, tx)
        batch = await trace_runner.run_traces(
            device,
            target,
            samples=samples_per_level,
            path=trace_path,
            cooldown_s=cooldown_s,
            persist=persist_trace,
        )
        traces_by_tx.setdefault(tx, []).extend(batch)
        level = build_level(tx, target, target_hash, traces_by_tx[tx])
        levels[tx] = level
        if persist_level is not None:
            persist_level(level)
        completed += 1
        if on_level is not None:
            on_level(completed, max(total_estimate, completed), level)
        return level

    try:
        if on_phase is not None:
            on_phase("coarse")
        for tx in coarse:
            await measure_level(tx)

        best = select_best(list(levels.values()), snr_tolerance=snr_tolerance)

        if refine:
            if on_phase is not None:
                on_phase("refine")
            lo = max(tx_min, best.tx_power - coarse_step + 1)
            hi = min(tx_max, best.tx_power + coarse_step - 1)
            for tx in range(lo, hi + 1):
                if tx not in levels:
                    await measure_level(tx)
            best = select_best(list(levels.values()), snr_tolerance=snr_tolerance)

        if verify:
            # Measure the leader again with more samples. If it was an outlier, the larger
            # sample will pull it back, and a more stable neighbour can take its place.
            if on_phase is not None:
                on_phase("verify")
            await measure_level(best.tx_power)
            best = select_best(list(levels.values()), snr_tolerance=snr_tolerance)

        # "No result" = no trace got through at any level (a winner with no meaning). In
        # that case, and when apply is off, leave the node at the power of the start.
        got_result = best.successes > 0
        applied = apply and got_result
        if applied:
            final_tx = best.tx_power
        elif original_tx is not None:
            final_tx = original_tx
        else:
            final_tx = best.tx_power  # no value to restore. Nothing better is possible.
        await device.set_remote_tx_power(admin_node, final_tx)
    except BaseException:
        # After a failure of any type, try to leave the node at the power of the start.
        if original_tx is not None:
            try:
                await device.set_remote_tx_power(admin_node, original_tx)
            except Exception:  # noqa: BLE001 - a best-effort restore. Do not hide the cause.
                pass
        raise

    return TxOptResult(
        target=target,
        admin_node=admin_node.name,
        path=path,
        original_tx=original_tx,
        best_tx=best.tx_power,
        best_snr=best.target_snr,
        best_success_rate=best.success_rate,
        applied=applied,
        levels=list(levels.values()),
    )


def _round_trip_path(outbound: list[str]) -> str:
    """Build a trace path out and back from a one-way path to the target.

    When the trace goes out to the target and then back, the *last* hop is a node near us
    (the first outbound hop), which can answer reliably. The far target must only forward
    the packet, and never starts the reply. This follows the MeshCore ``A,B,A`` trace
    convention. The hop of the target (the turn-around point) still has the SNR that it
    heard from the admin node, and we read that value.

    Args:
        outbound: The one-way path hops, with the target at the end.

    Returns:
        The round-trip path as a comma-separated hash string (for example, ``"3f,f2"``
        becomes ``"3f,f2,3f"``). A single-hop path is returned with no change.
    """
    if len(outbound) < 2:
        return ",".join(outbound)
    return ",".join(outbound + outbound[-2::-1])


def coarse_levels(tx_min: int, tx_max: int, step: int) -> list[int]:
    """Build the grid of the coarse sweep. The grid always has both endpoints.

    This function is public, so that the live sweep screen can show the worst-case number
    of transmissions of a commit before anything is transmitted.

    Args:
        tx_min: The lowest TX power.
        tx_max: The highest TX power.
        step: The spacing of the grid.

    Returns:
        The ascending list of TX levels to sample in the coarse phase.
    """
    levels = list(range(tx_min, tx_max + 1, max(1, step)))
    if levels[-1] != tx_max:
        levels.append(tx_max)
    return levels
