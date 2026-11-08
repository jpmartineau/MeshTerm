# SPDX-License-Identifier: Apache-2.0
"""The route that an automatic *Trace target* walks, for the menu and for the CLI.

A trace is one walked path, and the protocol has no destination field. Thus a trace to a
target must force a full boomerang: out to the target on a route, and back home on its
mirror. The radio rarely has a route to give. The firmware learns the route of a contact
only from addressed traffic in the two directions, and most repeater contacts flood. This
module chooses the route from all that MeshTerm knows, in this order:

1. The route of the radio, when it starts with an outbound path of the user (or when the
   user has no outbound path).
2. The best route through an outbound path of the user.
3. The route of the radio.
4. The strongest route that MeshTerm observed (traces, relay chains, neighbour tables).
5. The route out of the last walk to the target that came home.
6. No relay: a direct attempt, which says clearly that it is one.

The menu (:mod:`meshterm.ui.trace_screen`) and the CLI (:mod:`meshterm.tools.trace`)
both call :func:`plan_route`. Thus ``meshterm trace NTon1`` walks the same route that the
menu shows for NTon1.

A target that does not relay (a companion, a room server, or a sensor, refer to
:func:`~meshterm.core.models.relays_traces`) has the same route. Only the walk changes:
it turns at the last relay of the route (refer to
:func:`~meshterm.services.topology.render_forced_spec`). Thus a route that comes home is
also a route that the radio can use to send to the node.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..core.models import TraceResult
from .topology import MeshTopology, forced_outbound


@dataclass(frozen=True, slots=True)
class RoutePlan:
    """The route that an automatic trace walks to a target, and where it came from.

    Attributes:
        hops: The relays from our node to the target, in order from our node outward,
            without the two ends. Empty means no relay (a direct attempt).
        source: Where the route came from, in short words for the screen
            (``device route``, ``best observed · 3× · −7 dB``, ``last trace · Nov 08 01:42``).
    """

    hops: tuple[str, ...]
    source: str


def previous_outbound(
    previous: TraceResult | None, target_hash: str, *, relays: bool = True
) -> tuple[str, ...] | None:
    """Get the route out of the last walk to a target that came home.

    A trace in target mode walks the symmetric boomerang. Thus its stored hop hashes (the
    last hop, with no hash, is our node) are a palindrome of odd length. For a target that
    relays, the middle hop is the target, and the route is the first half. For a target
    that does not relay, the middle hop is the last relay, and the route is the first half
    with it (refer to :func:`~meshterm.services.topology.forced_outbound`). When the stored
    walk has that shape, its route is a route that the mesh already proved, ready to force
    again.

    Args:
        previous: The most recent successful stored trace, if one exists.
        target_hash: The full hex hash of the target, to make sure that the walk turned at
            this target (the hop hashes are prefixes of it).
        relays: Whether the target relays a trace.

    Returns:
        The route in order, from our node outward (empty means that the target answered
        directly). ``None`` when there is no stored walk, or when it is not a boomerang
        that this function can recognize.
    """
    if previous is None or not previous.success:
        return None
    tokens = [h.node for h in previous.hops if h.node]
    return forced_outbound(tokens, target_hash, relays=relays)


def best_observed(topo: MeshTopology, target_hash: str) -> tuple[tuple[str, ...], str] | None:
    """The strongest route to ``target_hash`` in the evidence, if the data has one.

    This is the observed-topology counterpart of the route that the firmware learned. The
    device often knows no route to a contact, because repeater contacts flood. In that
    case, the evidence of the recorder (traces, relay chains heard from other traffic,
    neighbour tables that MeshTerm got) may still give a route for which there is proof
    that it carried traffic. The ranking is done by
    :meth:`~meshterm.services.topology.MeshTopology.suggested`. It counts each node once,
    whatever width its hash had, and it gives recent evidence more weight. This function
    returns the canonical outbound hops of the winner, with a short source line
    (``best observed · 3× · −7 dB``) for the route line and the summary. It returns
    ``None`` when no route is better than a direct attempt.

    Args:
        topo: The newly built evidence graph.
        target_hash: The hex hash of the target (any width ≥ 1 byte).

    Returns:
        ``(outbound_hops, provenance)`` for the suggested route, or ``None``.
    """
    scenario = topo.suggested(topo.canonical(target_hash) or target_hash[:12])
    if scenario is None:
        return None
    source = f"best observed · {scenario.samples}×"
    if scenario.weakest_snr is not None:
        source += f" · {scenario.weakest_snr:+.0f} dB"
    return scenario.hops, source


def best_outbound(
    topo: MeshTopology,
    target_hash: str,
    outbound: tuple[tuple[str, ...], ...],
    silent_before: datetime,
    choice: Any,
) -> tuple[tuple[str, ...], str] | None:
    """The best route to ``target_hash`` through an outbound path of the user, if one exists.

    The ranking is :meth:`~meshterm.services.topology.MeshTopology.outbound_scenarios`: the
    best score from the link after the path, the order of the user in a near tie, and no
    path whose first hop went silent. The source line names the path and where the list
    came from (``outbound path 1 · Foulab · 3× · −7 dB``).

    Args:
        topo: The newly built evidence graph.
        target_hash: The hex hash of the target (any width ≥ 1 byte).
        outbound: The outbound paths that apply, as canonical ids.
        silent_before: The time before which a first hop counts as silent.
        choice: The :class:`~meshterm.core.outbound_store.OutboundChoice` that gave the
            paths, for the name of the site or the fixed device.

    Returns:
        ``(outbound_hops, provenance)``, or ``None`` when no outbound path reaches the
        target.
    """
    target_id = topo.canonical(target_hash) or target_hash[:12]
    found = topo.outbound_scenarios(target_id, outbound, silent_before=silent_before)
    if not found:
        return None
    best = found[0]
    source = f"outbound path {best.rank}"
    if choice is not None and choice.name:
        source += f" · {choice.name}"
    if best.samples:
        source += f" · {best.samples}×"
    if best.weakest_snr is not None:
        source += f" · {best.weakest_snr:+.0f} dB"
    return best.hops, source


def plan_route(
    target_hash: str,
    *,
    topology: Callable[[], MeshTopology],
    device_route: tuple[str, ...] | None,
    outbound: tuple[tuple[str, ...], ...],
    choice: Any,
    silent_before: datetime,
    previous: TraceResult | None,
    relays: bool = True,
) -> RoutePlan:
    """Choose the route that an automatic trace walks to a target (refer to the module).

    The outbound paths of the user come before all the evidence, with one exception: a
    device route that starts with one of them. The firmware learns its route from floods
    that our node received, and a radio link is not symmetric (refer to
    :mod:`meshterm.core.outbound_store`).

    Args:
        target_hash: The hex hash of the target (any width ≥ 1 byte).
        topology: Builds the evidence graph. It is a callable, because the build reads all
            the stored evidence, and the route of the radio often makes it unnecessary.
        device_route: The route that the radio holds for the target, as canonical ids, or
            ``None`` when it holds none.
        outbound: The outbound paths that apply now, as canonical ids.
        choice: The :class:`~meshterm.core.outbound_store.OutboundChoice` that gave the
            paths, or ``None``.
        silent_before: The time before which the first hop of an outbound path counts as
            silent.
        previous: The most recent successful stored trace to the target, if one exists.
        relays: Whether the target relays a trace. It changes only how the last walk is
            read back.

    Returns:
        The route, and where it came from.
    """
    device_first = device_route is not None and (
        not outbound or any(device_route[: len(path)] == path for path in outbound)
    )
    if device_first:
        assert device_route is not None
        return RoutePlan(device_route, "device route")
    if outbound:
        through = best_outbound(topology(), target_hash, outbound, silent_before, choice)
        if through is not None:
            return RoutePlan(*through)
    if device_route is not None:
        return RoutePlan(device_route, "device route")
    observed = best_observed(topology(), target_hash)
    if observed is not None:
        # The firmware has no route, but the evidence of the recorder gives one: the
        # strongest observed path. Because of this, Trace target *suggests* a route from
        # the data. It does not use a flood with no path by default, which fails after the
        # first neighbour.
        return RoutePlan(*observed)
    hops = previous_outbound(previous, target_hash, relays=relays)
    if hops is not None and previous is not None:
        stamp = previous.timestamp.astimezone().strftime("%b %d %H:%M")
        return RoutePlan(hops, f"last trace · {stamp}")
    return RoutePlan((), "direct — no known route")
