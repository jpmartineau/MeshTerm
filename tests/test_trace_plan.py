# SPDX-License-Identifier: Apache-2.0
"""The route that an automatic Trace target walks (refer to meshterm.services.trace_plan)."""

from __future__ import annotations

from datetime import timedelta

from meshterm.core.models import Contact, Hop, TraceResult, utcnow
from meshterm.persistence.repository import PacketPath
from meshterm.services.topology import build_topology
from meshterm.services.trace_plan import RoutePlan, plan_route

US = "aabb" + "00" * 30
HUB = Contact(name="Hub", public_key="3d63c6429436" + "0" * 52, key_prefix="3d63c6429436")
FAR = Contact(name="Far", public_key="f2c24f54551e" + "0" * 52, key_prefix="f2c24f54551e")
ALICE = Contact(
    name="Alice", public_key="d4e5f6a7b8c9" + "0" * 52, key_prefix="d4e5f6a7b8c9", node_type=1
)


def _topology(packets=()):  # noqa: ANN001, ANN202
    return lambda: build_topology(
        self_id=US,
        contacts=[HUB, FAR, ALICE],
        trace_paths=[],
        packet_paths=list(packets),
        neighbour_links=[],
    )


def _plan(**overrides) -> RoutePlan:  # noqa: ANN003
    args = dict(
        topology=_topology(),
        device_route=None,
        outbound=(),
        choice=None,
        silent_before=utcnow() - timedelta(hours=1),
        previous=None,
    )
    args.update(overrides)
    return plan_route(ALICE.public_key, **args)


def _walk(*nodes: str) -> TraceResult:
    """A walk that came home on ``nodes`` (the last hop is our node, with no hash)."""
    hops = [Hop(index=i, node=n, snr=1.0) for i, n in enumerate(nodes)]
    hops.append(Hop(index=len(nodes), node=None, snr=1.0))
    return TraceResult(
        target="Alice", success=True, hops=hops, round_trip_ms=200.0, path_hash_bytes=2
    )


def test_the_radio_route_comes_first() -> None:
    """With no outbound path of the user, the route that the radio holds wins."""
    assert _plan(device_route=("3d63c6429436",)) == RoutePlan(("3d63c6429436",), "device route")


def test_the_strongest_heard_relay_of_a_companion_gives_its_route() -> None:
    """Alice's packets came in through Hub five times and through Far once.

    The evidence graph counts each relay by node and gives recent packets more weight.
    Thus the route to Alice goes through Hub, the relay that hears her most.
    """
    now = utcnow()
    packets = [PacketPath(now, "d4e5f6a7b8c9", 5.0, ["3d63c6429436"]) for _ in range(5)]
    packets.append(PacketPath(now, "d4e5f6a7b8c9", 5.0, ["f2c24f54551e"]))
    plan = _plan(topology=_topology(packets), relays=False)
    assert plan.hops == ("3d63c6429436",)
    assert plan.source.startswith("best observed · ")


def test_with_no_evidence_the_last_walk_home_gives_the_route() -> None:
    """A walk to a companion turned at its relay, so that relay ends the route to it."""
    relays = _plan(previous=_walk("3d63", "d4e5", "3d63"))
    assert relays.hops == ("3d63",)
    assert relays.source.startswith("last trace · ")
    companion = _plan(previous=_walk("3d63", "f2c2", "3d63"), relays=False)
    assert companion.hops == ("3d63", "f2c2")


def test_with_nothing_known_the_plan_is_a_direct_attempt() -> None:
    """No route anywhere: no relay, and the source says so."""
    assert _plan() == RoutePlan((), "direct — no known route")
