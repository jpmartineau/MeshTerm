# SPDX-License-Identifier: Apache-2.0
"""Multi-path probing: measure the candidate routes to a target, and rank the routes that work.

The topology graph (:mod:`~meshterm.services.topology`) proposes routes from the evidence
that MeshTerm heard. This module tests them. By default, MeshTerm traces each candidate
outbound path one time, because repeaters penalize nodes that send traffic in bursts, and
can blacklist them. Each trace is stored exactly as a normal trace run. The total for each
candidate is stored as a ``path_candidates`` row. The results come back in the order that
the TX optimizer uses for its levels: reliability first, then the bottleneck SNR to break
a tie, then the round-trip time. The caller (the live trace screen) shows the ranking and
offers to adopt the winner. The measurement only proposes a route, and the user decides.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..core.connection import Device
from ..core.models import TraceResult, TraceStats

ProbeProgress = Callable[[int, int, TraceResult], None]
PersistTrace = Callable[[TraceResult], None]
PersistCandidate = Callable[["ProbeOutcome"], None]


@dataclass(slots=True)
class ProbeCandidate:
    """One route to measure.

    Attributes:
        label: A short description of the source of the route (shown in the results).
        spec: The forced-path spec to trace: hex hashes with commas between them,
            outbound only, which end at the hash of the target itself. The reply goes
            back along it in reverse.
    """

    label: str
    spec: str


@dataclass(slots=True)
class ProbeOutcome:
    """The measured result of one candidate.

    Attributes:
        candidate: The route that was measured.
        stats: The trace statistics, aggregated over its traces.
    """

    candidate: ProbeCandidate
    stats: TraceStats

    @property
    def sort_key(self) -> tuple:
        """A ranking key that sorts the best path first.

        Reliability, then bottleneck SNR, then RTT: the same order of priority that the
        TX optimizer uses.
        """
        snr = self.stats.median_min_snr
        rtt = self.stats.median_rtt_ms
        return (
            -self.stats.success_rate,
            -(snr if snr is not None else float("-inf")),
            rtt if rtt is not None else float("inf"),
        )


async def probe_paths(
    device: Device,
    target: str,
    candidates: list[ProbeCandidate],
    *,
    samples: int = 1,
    cooldown_s: float = 1.0,
    on_result: ProbeProgress | None = None,
    persist_trace: Callable[[TraceResult], Awaitable[None] | None] | None = None,
    persist_candidate: PersistCandidate | None = None,
) -> list[ProbeOutcome]:
    """Trace each candidate path, and return the results with the best result first.

    Runs the candidates in sequence (there is one radio, and the duty cycle applies), with
    one trace for each candidate by default. Callbacks do the storage, so that this
    function only measures. The caller owns the run row, and it decides where the traces
    and the candidate totals go. When the task around this function is cancelled, the
    function stops in the middle of a candidate. All the data stored before then stays
    stored.

    Args:
        device: The connected device to trace through.
        target: The destination (for the label of :class:`TraceStats`).
        candidates: The routes to measure, in the order to try them.
        samples: The number of traces for each candidate. The default of one is on
            purpose, because repeaters can blacklist nodes that send traffic in bursts.
            Increase it only when the airtime budget clearly permits it.
        cooldown_s: The pause between traces (and between candidates).
        on_result: An optional callback ``(candidate_index, done_in_candidate, result)``,
            called when each trace completes. For example, it moves the probe dialog
            forward.
        persist_trace: An optional callback that stores each trace (sync or async).
        persist_candidate: An optional callback, called with the finished
            :class:`ProbeOutcome` of each candidate (for example, to store a
            ``path_candidates`` row).

    Returns:
        One :class:`ProbeOutcome` for each candidate, in the order of
        :attr:`ProbeOutcome.sort_key`.
    """
    from . import trace_runner

    outcomes: list[ProbeOutcome] = []
    for index, candidate in enumerate(candidates):
        if index and cooldown_s > 0:
            # ``run_traces`` pauses between its own samples, and never after the last
            # one. Thus, at the default of one trace for each candidate, it does not
            # pause at all. Without this pause, the sweep traces all the candidates one
            # after the other, and the repeaters hear a burst. Because of that traffic
            # pattern, the repeaters ignore the node, and then each later trace comes
            # back empty.
            await asyncio.sleep(cooldown_s)
        results = await trace_runner.run_traces(
            device,
            target,
            samples=samples,
            path=candidate.spec,
            cooldown_s=cooldown_s,
            on_result=(
                (lambda done, _total, result, i=index: on_result(i, done, result))
                if on_result is not None
                else None
            ),
            persist=persist_trace,
        )
        outcome = ProbeOutcome(candidate=candidate, stats=TraceStats.from_traces(target, results))
        outcomes.append(outcome)
        if persist_candidate is not None:
            persist_candidate(outcome)
    return sorted(outcomes, key=lambda o: o.sort_key)
