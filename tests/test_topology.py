# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the observed-topology graph, path scenarios, and the path composer.

The graph takes data in and gives data out, so these tests run headless. The tests build
the evidence rows by hand, in the same form as the rows that the repository returns. The
assertions cover canonicalization, the folding of bidirectional links, the order of link
strength, the ranking of scenarios, and the step-by-step state machine of the composer.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

import meshterm.ui.pathline as pathline
from meshterm.core.connection import DeviceCommandError, MockDevice
from meshterm.core.models import Contact, NeighbourInfo, Observation, utcnow
from meshterm.persistence.repository import (
    NeighbourLink,
    PacketPath,
    Repository,
    TracedPath,
)
from meshterm.services.path_probe import ProbeCandidate, probe_paths
from meshterm.services.topology import build_topology, collapse_width, render_custom_spec
from meshterm.services.trace_runner import make_node_resolver
from meshterm.ui.path_composer import AUTO_SPEC, FetchNeighbours, PathComposerScreen
from meshterm.ui.pathline import CURSOR_GLYPH, SELF_GLYPH
from meshterm.ui.tui.screen import CANCEL

US = "aaaaaaaaaaaa"
REPEATER = Contact(name="Hub", public_key="3d63c6429436" + "0" * 52, key_prefix="3d63c6429436")
FAR = Contact(name="Far", public_key="f2c24f54551e" + "0" * 52, key_prefix="f2c24f54551e")
LEAF = Contact(name="Leaf", public_key="27d4396a2967" + "0" * 52, key_prefix="27d4396a2967")


def _topo(trace_paths=(), packet_paths=(), neighbour_links=(), contacts=None):  # noqa: ANN001
    return build_topology(
        self_id=US + "0" * 52,
        contacts=list(contacts) if contacts is not None else [REPEATER, FAR, LEAF],
        trace_paths=list(trace_paths),
        packet_paths=list(packet_paths),
        neighbour_links=list(neighbour_links),
    )


def _traced(*hops, age_hours: float = 0.0) -> TracedPath:
    """A trace walk from ``(node_hash, snr)`` pairs. The final hop has no hash and is us."""
    return TracedPath(when=utcnow() - timedelta(hours=age_hours), hops=list(hops))


# --- graph construction ---------------------------------------------------------


def test_trace_walk_yields_bidirectional_links_at_canonical_ids() -> None:
    """A boomerang trace folds the readings out and back onto the same undirected links."""
    topo = _topo(trace_paths=[_traced(("3d", 12.5), ("f2", -5.5), ("3d", -6.5), (None, 11.75))])
    # The walk crossed us↔Hub twice (first hop out, last hop home), and Hub↔Far twice too.
    near = topo.link(topo.self_id, "3d63c6429436")
    far = topo.link("3d63c6429436", "f2c24f54551e")
    assert near is not None and near.samples == 2
    assert sorted(near.snrs) == [11.75, 12.5]
    assert far is not None and far.samples == 2
    assert sorted(far.snrs) == [-6.5, -5.5]


def test_canonicalization_maps_prefixes_and_rejects_junk() -> None:
    """A hash of any width collapses onto a contact id. Ambiguous and non-hex values stay."""
    topo = _topo()
    assert topo.canonical("3d") == "3d63c6429436"
    assert topo.canonical("3d63c6429436" + "0" * 20) == "3d63c6429436"
    assert topo.canonical("hop0") is None  # an old artefact of the simulator, not hex
    ambiguous = _topo(contacts=[REPEATER, Contact(name="Twin", public_key="3d99" + "0" * 60)])
    assert ambiguous.canonical("3d") == "3d"  # two contacts match, so keep the short id


def test_coalesce_folds_a_short_hop_into_its_only_wide_match() -> None:
    """A bare hop and the wide id of the same node collapse to one node, with pooled evidence."""
    twin = Contact(name="Twin", public_key="27e4" + "0" * 60)  # makes a bare "27" ambiguous
    walks = [
        _traced(("27", 5.0), ("f2", -3.0), ("27", -3.5), (None, 5.0)),  # us→27→Far
        _traced(("27d4", 7.0), ("f2", -4.0), ("27d4", -4.5), (None, 7.0)),  # us→27d4→Far
    ]
    topo = _topo(trace_paths=walks, contacts=[REPEATER, FAR, LEAF, twin])
    ids = {node for link in topo.links() for node in (link.a, link.b)}
    assert "27" not in ids  # the stub of 1 byte folded onto its only wide match in the graph
    assert "27d4396a2967" in ids
    link = topo.link(topo.self_id, "27d4396a2967")
    assert link is not None and link.samples == 4  # the readings out and back of both walks, pooled


def test_coalesce_leaves_a_genuinely_ambiguous_stub_alone() -> None:
    """A short hop that opens two nodes in the graph is not guessed onto one of them."""
    twin = Contact(name="Twin", public_key="27e41e2d7cb5" + "0" * 52, key_prefix="27e41e2d7cb5")
    walks = [  # both 27d4… and 27e4… are in the graph, and also a bare 27 that cannot be decided
        _traced(("27d4", 5.0), (None, 5.0)),
        _traced(("27e4", 5.0), (None, 5.0)),
        _traced(("27", 5.0), (None, 5.0)),
    ]
    topo = _topo(trace_paths=walks, contacts=[REPEATER, FAR, LEAF, twin])
    ids = {node for link in topo.links() for node in (link.a, link.b)}
    assert {"27", "27d4396a2967", "27e41e2d7cb5"} <= ids  # the stub stays a separate node


def test_coalesce_folds_an_ambiguous_stub_its_neighbourhood_elects() -> None:
    """The neighbours of a stub that the hash cannot place decide where it goes.

    ``3d`` opens both ``3d63…`` and ``3d99…``, so the prefix alone is a random choice. But
    the stub has links to Far and Leaf, and only ``3d63…`` is heard with either of them.
    There are two votes that discriminate and none against. Thus the stub folds and its
    evidence is pooled.
    """
    twin = Contact(name="Twin", public_key="3d99" + "0" * 60)
    walks = [
        _traced(("3d63", 5.0), ("f2", 5.0), ("3d63", 5.0), (None, 5.0)),
        _traced(("3d63", 5.0), ("27d4", 5.0), ("3d63", 5.0), (None, 5.0)),
        _traced(("3d99", 5.0), (None, 5.0)),  # the rival is in the graph, but not out here
        _traced(("3d", 5.0), ("f2", 5.0), ("3d", 5.0), (None, 5.0)),
        _traced(("3d", 5.0), ("27d4", 5.0), ("3d", 5.0), (None, 5.0)),
    ]
    topo = _topo(trace_paths=walks, contacts=[REPEATER, FAR, LEAF, twin])
    ids = {node for link in topo.links() for node in (link.a, link.b)}
    assert "3d" not in ids  # elected onto the node that its neighbours agree it must be
    assert {"3d63c6429436", "3d9900000000"} <= ids  # the rival is unchanged
    link = topo.link(topo.self_id, "3d63c6429436")
    assert link is not None and link.samples == 8  # the readings of the stub came with it


def test_coalesce_leaves_a_contested_stub_standing() -> None:
    """If the neighbours point both ways, the hash pools two nodes, so the stub stays."""
    twin = Contact(name="Twin", public_key="3d99" + "0" * 60)
    edge = Contact(name="Edge", public_key="b1" + "0" * 62)
    walks = [
        _traced(("3d63", 5.0), ("f2", 5.0), ("3d63", 5.0), (None, 5.0)),
        _traced(("3d63", 5.0), ("27d4", 5.0), ("3d63", 5.0), (None, 5.0)),
        _traced(("3d99", 5.0), ("b1", 5.0), ("3d99", 5.0), (None, 5.0)),
        _traced(("3d", 5.0), ("f2", 5.0), ("3d", 5.0), (None, 5.0)),
        _traced(("3d", 5.0), ("27d4", 5.0), ("3d", 5.0), (None, 5.0)),
        _traced(("3d", 5.0), ("b1", 5.0), ("3d", 5.0), (None, 5.0)),
    ]
    topo = _topo(trace_paths=walks, contacts=[REPEATER, FAR, LEAF, twin, edge])
    ids = {node for link in topo.links() for node in (link.a, link.b)}
    # 2 votes to 1 is a plurality, not a verdict. The margin keeps the stub a separate node.
    assert {"3d", "3d63c6429436", "3d9900000000"} <= ids


def test_coalesce_ignores_neighbours_both_candidates_share() -> None:
    """A shared neighbour is local density, not evidence. Only neighbours that discriminate vote."""
    twin = Contact(name="Twin", public_key="3d99" + "0" * 60)
    walks = [
        _traced(("3d63", 5.0), ("f2", 5.0), ("3d63", 5.0), (None, 5.0)),
        _traced(("3d99", 5.0), ("f2", 5.0), ("3d99", 5.0), (None, 5.0)),
        _traced(("3d63", 5.0), ("27d4", 5.0), ("3d63", 5.0), (None, 5.0)),
        _traced(("3d99", 5.0), ("27d4", 5.0), ("3d99", 5.0), (None, 5.0)),
        _traced(("3d", 5.0), ("f2", 5.0), ("3d", 5.0), (None, 5.0)),
        _traced(("3d", 5.0), ("27d4", 5.0), ("3d", 5.0), (None, 5.0)),
    ]
    topo = _topo(trace_paths=walks, contacts=[REPEATER, FAR, LEAF, twin])
    ids = {node for link in topo.links() for node in (link.a, link.b)}
    # Each neighbour of the stub is a neighbour of both candidates, so nothing can show which
    # is which. Thus the code does not guess that the stub is the busier candidate.
    assert {"3d", "3d63c6429436", "3d9900000000"} <= ids


def test_contact_routes_and_packet_paths_feed_the_graph() -> None:
    """Routes that the firmware learned and packet paths from the RX log are both evidence."""
    routed = Contact(
        name="Far",
        public_key=FAR.public_key,
        key_prefix=FAR.key_prefix,
        route_hops=("3d63c6429436",),
        last_seen=utcnow(),
    )
    packet = PacketPath(when=utcnow(), origin="27d4396a2967", snr=8.0, hops=["3d63c6429436"])
    topo = _topo(packet_paths=[packet], contacts=[REPEATER, routed, LEAF])
    assert topo.link(topo.self_id, "3d63c6429436") is not None  # both sources touch it
    route_link = topo.link("3d63c6429436", "f2c24f54551e")
    assert route_link is not None and route_link.sources == {"route"}
    packet_link = topo.link("27d4396a2967", "3d63c6429436")
    assert packet_link is not None and packet_link.sources == {"packet"}
    # Only the final link (last relay → us) has the reception SNR.
    assert topo.link(topo.self_id, "3d63c6429436").snrs == [8.0]
    assert packet_link.snrs == []


def test_neighbour_reports_feed_the_graph_as_fetched_evidence() -> None:
    """A table from a repeater adds links at its vantage point, tagged ``neighbour``."""
    reported = [
        NeighbourLink(when=utcnow(), repeater="3d63c6429436", neighbour="f2c2", snr=7.0),
        NeighbourLink(when=utcnow(), repeater="3d63c6429436", neighbour="beefbeef", snr=None),
    ]
    topo = _topo(neighbour_links=reported)
    link = topo.link("3d63c6429436", "f2c24f54551e")  # 'f2c2' canonicalizes onto Far
    assert link is not None and link.sources == {"neighbour"}
    assert link.snrs == [7.0]  # the SNR that the repeater measured is on the link
    # A neighbour that no contact matches still counts. It is a discovery of a new node.
    unknown = topo.link("3d63c6429436", "beefbeef")
    assert unknown is not None and unknown.snrs == []
    suggested = [s.node for s in topo.next_hops("3d63c6429436")]
    assert "f2c24f54551e" in suggested and "beefbeef" in suggested


# --- suggestions and scenarios ----------------------------------------------------


def test_next_hops_sorts_by_link_strength_and_excludes_used_nodes() -> None:
    """The suggestions rank the fresh link with many observations first, and keep the exclusions."""
    strong = [_traced(("3d", 12.0), (None, 12.0)) for _ in range(6)]
    weak = [_traced(("27d4", -9.0), (None, -9.0), age_hours=24 * 21)]
    topo = _topo(trace_paths=strong + weak)
    nodes = [s.node for s in topo.next_hops(topo.self_id)]
    assert nodes == ["3d63c6429436", "27d4396a2967"]
    excluded = topo.next_hops(topo.self_id, exclude=frozenset({"3d63c6429436"}))
    assert [s.node for s in excluded] == ["27d4396a2967"]


def test_scenarios_rank_observed_route_and_pin_device_route_first() -> None:
    """The device route is first, then the observed evidence, then the direct try (unobserved)."""
    walks = [_traced(("3d", 12.0), ("f2", -5.0), ("3d", -5.5), (None, 12.0)) for _ in range(4)]
    topo = _topo(trace_paths=walks)
    scenarios = topo.scenarios("f2c24f54551e", device_route=("3d63c6429436",))
    assert scenarios[0].source == "device"
    assert scenarios[0].hops == ("3d63c6429436",)
    labels = [s.source for s in scenarios]
    assert "direct" in labels  # the baseline is always offered
    direct = next(s for s in scenarios if s.source == "direct")
    assert direct.score == 0.0  # never observed, so it ranks on measurement only
    # The observed route (the same hops as the device route) is removed as a duplicate.
    assert sum(1 for s in scenarios if s.hops == ("3d63c6429436",)) == 1


def test_suggested_returns_the_strongest_observed_route_only() -> None:
    """suggested() is the answer of the data: the best observed multi-hop route to the target."""
    walks = [_traced(("3d", 12.0), ("f2", -5.0), ("3d", -5.5), (None, 12.0)) for _ in range(4)]
    topo = _topo(trace_paths=walks)
    best = topo.suggested("f2c24f54551e")
    assert best is not None
    assert best.source == "observed"  # never the device or direct families
    assert best.hops == ("3d63c6429436",)  # us → Hub → Far
    assert best.score > 0 and best.samples >= 4  # supported by evidence


def test_suggested_is_none_without_an_observed_repeater_route() -> None:
    """A target that was reached only directly (or not at all) has no route to suggest."""
    # One direct trace to the Hub: us → Hub → us, with no repeater between.
    topo = _topo(trace_paths=[_traced(("3d", 9.0), (None, 9.0))])
    assert topo.suggested("3d63c6429436") is None  # direct only: nothing to suggest
    assert topo.suggested("f2c24f54551e") is None  # never reached at all


def test_scenarios_return_disjoint_alternatives_cheapest_first() -> None:
    """Two independent routes to a target both appear, ranked by strength of evidence."""
    # Two repeaters, each a separate route of one hop to Far. The link of Hub is stronger.
    walks = [_traced(("3d", 12.0), ("f2", -3.0), ("3d", -3.0), (None, 12.0)) for _ in range(3)] + [
        _traced(("27", 4.0), ("f2", -8.0), ("27", -8.0), (None, 4.0)) for _ in range(3)
    ]
    topo = _topo(trace_paths=walks)
    observed = [s for s in topo.scenarios("f2c24f54551e") if s.source == "observed" and s.hops]
    assert [s.hops for s in observed[:2]] == [("3d63c6429436",), ("27d4396a2967",)]
    assert observed[0].score > observed[1].score  # the stronger route ranks first


def test_best_routes_stays_fast_on_a_densely_connected_core() -> None:
    """A well-heard core must not stop the route search. This search avoids that fault.

    A best-first walk over partial paths floods a dense core. Each prefix that wanders
    through the core is cheaper than the one weak link that a distant target is behind. Thus
    the frontier grows to millions of dead-end prefixes (tens of seconds for a busy node).
    The k-shortest search of Yen stays polynomial. Here a clique of 20 nodes has the target
    on a single weak link. The search takes milliseconds (the old frontier took seconds), and
    it still finds the route out.
    """
    import time

    now = utcnow()
    core = [f"c0de{i:08x}" for i in range(20)]  # 20 distinct 12-hex core nodes
    target = "fa11faceface"
    links = []
    us = US + "0" * 52
    # Each node in the core hears each other node (a full clique), and hears us. This is a
    # frontier as dense as possible, where the search can get lost.
    for i, a in enumerate([us, *core]):
        for b in [*core][i:]:
            if a != b:
                links.append(NeighbourLink(when=now, repeater=a, neighbour=b, snr=8.0))
    # The target is reachable only across one weak link from the last node of the core.
    links.append(NeighbourLink(when=now, repeater=core[-1], neighbour=target, snr=-12.0))

    topo = _topo(neighbour_links=links)
    started = time.perf_counter()
    scenarios = topo.scenarios(target)
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0  # milliseconds in practice. The old frontier took seconds or more
    observed = [s for s in scenarios if s.source == "observed" and s.hops]
    assert observed  # the route through the core to the target was found
    assert observed[0].hops[-1] == core[-1]  # it arrives through the one weak link


def test_scenario_spec_ends_at_target_and_collapses_width() -> None:
    """A spec goes out to the target, then mirrors the hops back, at one width."""
    walks = [_traced(("3d", 12.0), ("f2", -5.0), ("3d", -5.5), (None, 12.0))]
    topo = _topo(trace_paths=walks)
    scenario = next(s for s in topo.scenarios("f2c24f54551e") if s.hops == ("3d63c6429436",))
    assert scenario.spec("f2c24f54551e", 2) == "3d63,f2c2,3d63"
    assert collapse_width("3d", "f2c24f54551e", ceiling=2) == 1  # the narrowest hash decides


def test_render_custom_spec_is_verbatim_at_a_uniform_width() -> None:
    """An asymmetric walk renders exactly its hops, with no target added and no mirror."""
    hops = ("3d63c6429436", "f2c24f54551e", "27d4396a2967")
    assert render_custom_spec(hops, 2) == "3d63,f2c2,27d4"
    # A hop that is known only narrowly makes the whole spec as narrow as that hop.
    assert render_custom_spec(("3d63c6429436", "be"), 2) == "3d,be"
    assert render_custom_spec((), 2) == ""  # no hops, no spec (never a false "auto")


# --- repository round-trip ---------------------------------------------------------


def test_repository_round_trips_packet_paths_and_candidates(tmp_path) -> None:  # noqa: ANN001
    """The repository stores the path of a packet observation. Candidates go to path_candidates."""
    repo = Repository(tmp_path / "t.db")
    run = repo.start_run("monitor", {})
    repo.record_observation(
        run, Observation(node="27d4396a2967", kind="packet", snr=7.5, path="3d63,f2c2")
    )
    repo.record_observation(run, Observation(node="27d4396a2967", kind="advert", snr=3.0))
    packets = repo.packet_paths()
    assert len(packets) == 1
    assert packets[0].hops == ["3d63", "f2c2"]
    assert packets[0].origin == "27d4396a2967"
    # Packet rows never change the reception statistics of a node.
    heard = repo.heard_nodes()
    assert len(heard) == 1 and heard[0].count == 1 and heard[0].median_snr == 3.0

    probe_run = repo.start_run("trace", {"mode": "probe"})
    repo.record_path_candidate(
        probe_run, "Far", "3d,f2", bottleneck_snr=-5.0, success_rate=1.0, median_rtt_ms=320.0
    )
    row = repo._conn.execute("SELECT * FROM path_candidates").fetchone()
    assert row["target"] == "Far" and row["success_rate"] == 1.0
    repo.close()


def test_repository_neighbour_snapshots_keep_latest_per_pair(tmp_path) -> None:  # noqa: ANN001
    """A table that MeshTerm gets again replaces the old snapshot. It does not stack as evidence."""
    repo = Repository(tmp_path / "n.db")
    run = repo.start_run("trace", {"mode": "neighbours"})
    heard = utcnow() - timedelta(hours=2)
    repo.record_neighbours(
        run,
        "3d63c6429436",
        [
            NeighbourInfo(node="f2c24f54", snr=-5.0, heard_at=heard),
            NeighbourInfo(node="beefbeef", snr=2.0, heard_at=None),
        ],
    )
    links = {link.neighbour: link for link in repo.neighbour_links()}
    assert len(links) == 2
    assert links["f2c24f54"].snr == -5.0
    assert links["f2c24f54"].when == heard  # the recency from the repeater remains
    assert links["beefbeef"].when is not None  # no heard_at, so it uses fetched_at

    repo.record_neighbours(run, "3d63c6429436", [NeighbourInfo(node="f2c24f54", snr=9.0)])
    links = {link.neighbour: link for link in repo.neighbour_links()}
    assert len(links) == 2  # still one link per pair
    assert links["f2c24f54"].snr == 9.0  # the new snapshot replaced the old one
    repo.close()


async def test_mock_fetch_neighbours_is_login_gated_like_real_firmware() -> None:
    """Without a login, the mock ignores the request (an error). After a login, it gives the table.

    The table includes a node that was not heard before.
    """
    device = MockDevice()
    await device.connect()
    contacts = await device.get_contacts()
    yagi = next(c for c in contacts if c.name == "Yagi-Repeater")

    with pytest.raises(DeviceCommandError):
        await device.fetch_neighbours(yagi)  # the same as v1.15 firmware: it ignores guests

    assert await device.admin_login(yagi, "admin")
    entries = await device.fetch_neighbours(yagi)
    nodes = {e.node for e in entries}
    assert "e5f6a7b8" in nodes  # a node that is not in the contact list: a discovery
    assert all(e.heard_at is not None for e in entries)

    alice = next(c for c in contacts if c.name == "Alice")
    assert await device.admin_login(alice, "admin")
    with pytest.raises(DeviceCommandError):
        await device.fetch_neighbours(alice)  # a chat node has no neighbour table


# --- path probe ----------------------------------------------------------------------


async def test_probe_paths_ranks_reliability_first() -> None:
    """A path that always answers ranks above a path with a stronger SNR that loses traces."""
    from meshterm.core.models import Hop, TraceResult

    class _Device:
        async def run_trace(self, target, *, path=None, timeout=10.0):  # noqa: ANN001
            if path == "good":
                return TraceResult(
                    target=target,
                    success=True,
                    hops=[Hop(index=0, node="3d", snr=2.0)],
                    round_trip_ms=300.0,
                )
            return TraceResult(target=target, success=False)

    outcomes = await probe_paths(
        _Device(),
        "Far",
        [ProbeCandidate(label="flaky", spec="bad"), ProbeCandidate(label="solid", spec="good")],
        samples=2,
        cooldown_s=0.0,
    )
    assert [o.candidate.label for o in outcomes] == ["solid", "flaky"]
    assert outcomes[0].stats.success_rate == 1.0


async def test_probe_paths_defaults_to_one_trace_per_candidate() -> None:
    """By default, MeshTerm measures each candidate with a single transmission.

    This is the rule that avoids the blacklist. Repeaters penalize nodes that send bursts of
    traffic. Thus a probe of N paths must cost exactly N traces, unless the caller chooses more.
    """
    from meshterm.core.models import Hop, TraceResult

    transmitted: list[str] = []

    class _Device:
        async def run_trace(self, target, *, path=None, timeout=10.0):  # noqa: ANN001
            transmitted.append(path)
            return TraceResult(
                target=target,
                success=True,
                hops=[Hop(index=0, node="3d", snr=2.0)],
                round_trip_ms=300.0,
            )

    outcomes = await probe_paths(
        _Device(),
        "Far",
        [ProbeCandidate(label="a", spec="3d"), ProbeCandidate(label="b", spec="f2")],
        cooldown_s=0.0,
    )
    assert transmitted == ["3d", "f2"]  # one trace per candidate, in order
    assert all(o.stats.samples == 1 for o in outcomes)


# --- path composer ---------------------------------------------------------------------


def _composer(  # noqa: ANN001
    topo, hops=None, fetch_nodes=frozenset(), target=True, cursor=None, resolve=None
):
    """A composer over ``topo``, pinned on Far (target mode) or with no target."""
    pinned = (
        dict(
            target_id="f2c24f54551e",
            target_hash="f2c24f54551e" + "0" * 52,
            target_label="Far",
        )
        if target
        else {}
    )
    screen = PathComposerScreen(
        device_label="Us",
        device_hash=US + "0" * 52,
        topology=topo,
        width_bytes=1,
        hops=list(hops or []),
        cursor=cursor,
        fetch_nodes=fetch_nodes,
        **({"resolve": resolve} if resolve is not None else {}),
        **pinned,
    )
    screen.note_viewport(30)  # the frame records the budget of the dialog before each paint
    return screen


def _rows_plain(screen: PathComposerScreen) -> str:
    return "\n".join(screen.render_body(90))


def test_composer_suggests_from_tail_and_appends_on_enter() -> None:
    """The strongest neighbour of the tail of the path is the first suggestion. Enter adds it."""
    walks = [_traced(("3d", 12.0), ("f2", -5.0), ("3d", -5.5), (None, 12.0))]
    screen = _composer(_topo(trace_paths=walks))
    body = _rows_plain(screen)
    assert "Hub" in body and "auto" in body.lower()  # the suggestion and the "Auto" action row
    screen.handle("enter")  # top row: the Hub suggestion
    assert screen._hops == ["3d63c6429436"]
    # The target is excluded from the new tail, so Far never appears as a hop.
    assert all(s.node != "f2c24f54551e" for s in screen._suggestions())


def test_composer_windows_rows_under_the_pinned_route_preview() -> None:
    """A short dialog shows a window of the rows. The route preview and highlight stay visible."""
    import re

    walks = [_traced((f"{i + 16:02x}", 3.0), (None, 3.0)) for i in range(10)]
    screen = _composer(_topo(trace_paths=walks, contacts=[FAR]))
    screen.note_viewport(9)  # a short budget for the dialog: preview, heading, and a few rows
    body = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(screen.render_body(90)))
    assert SELF_GLYPH in body  # the route preview is pinned and does not scroll out
    assert "↓" in body and "more" in body  # the edge shows a count of the hidden rows
    screen.handle("end")  # highlight to the last action row, and the window follows
    body = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(screen.render_body(90)))
    assert "Auto" in body and "↑" in body


def test_composer_typed_hex_adds_a_custom_hop_and_backspace_removes() -> None:
    """Hex of even length that the user types in the filter becomes a custom hop to add."""
    screen = _composer(_topo())
    for ch in "beef":
        screen.handle("text", ch)
    screen.handle("enter")  # the "+ add hop beef" row is first
    assert screen._hops == ["beef"]
    screen.handle("backspace")  # the entry is empty, so it removes the last hop
    assert screen._hops == []


def test_composer_cursor_inserts_and_deletes_mid_path() -> None:
    """The ←/→ keys move the insertion slot. An add puts a hop at the slot.

    The ⌫ key removes the hop to the left of the slot.
    """
    screen = _composer(_topo(), hops=["27d4396a2967", "f2c24f54551e"], target=False)
    assert screen.cursor == 2  # opens on the last arrow, so an insert is an append
    screen.handle("right")
    assert screen.cursor == 2  # clamped at the end…
    screen.handle("left")
    screen.handle("left")
    screen.handle("left")
    assert screen.cursor == 0  # …and at home
    for ch in "beef":  # insert at home: the hop goes before the hops that were set first
        screen.handle("text", ch)
    screen.handle("enter")
    assert screen._hops == ["beef", "27d4396a2967", "f2c24f54551e"]
    assert screen.cursor == 1  # the slot moved past the hop that it inserted
    screen.handle("backspace")  # ⌫ removes the hop to the left of the slot, and the slot follows
    assert screen._hops == ["27d4396a2967", "f2c24f54551e"] and screen.cursor == 0
    # A reopen (the round trip to fetch neighbours) starts at the position that was set.
    assert _composer(_topo(), hops=["27d4396a2967"], cursor=1).cursor == 1
    assert _composer(_topo(), hops=["27d4396a2967"], cursor=99).cursor == 1  # clamped


def test_composer_suggestions_follow_the_cursor_anchor() -> None:
    """The heading and the list move to the node at the left of the insertion slot when it moves."""
    import re

    walks = [_traced(("3d", 12.0), ("f2", -5.0), ("3d", -5.5), (None, 12.0))]
    screen = _composer(_topo(trace_paths=walks), hops=["3d63c6429436"], target=False)

    def body() -> str:
        return re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(screen.render_body(90)))

    assert "Next hop from Hub" in body()  # the slot is at the end, so the anchor is the tail
    screen.handle("left")
    assert "Next hop from Us" in body()
    # An insert of the right neighbour of the slot gives a self-loop, so Hub is not proposed.
    assert all(s.node != "3d63c6429436" for s in screen._suggestions())


def test_composer_preview_stands_the_cursor_in_the_route() -> None:
    """The composer preview shows the insertion slot in the route itself.

    The insertion point is a hop: the ``+`` slot that the next node takes. The ←/→ keys
    move it, one position at a time.
    """
    screen = _composer(_topo(), hops=["3d63c6429436"], target=False)

    def slot_at() -> int:
        labels = [hop.label for hop in screen._route_preview().hops]
        assert labels.count(CURSOR_GLYPH) == 1  # always exactly one slot
        return labels.index(CURSOR_GLYPH)

    assert slot_at() == 2  # opens after us and the composed hop, so an insert is an append
    screen.handle("left")
    assert slot_at() == 1  # moved home, before the hop that it now precedes


def test_composer_warns_when_the_walk_repeats_a_link() -> None:
    """A walk over a link twice in one direction shows the yellow not-a-trail note.

    A correction of the path removes the note.
    """
    clean = _composer(_topo(), hops=["3d63c6429436", "f2c24f54551e"], target=False)
    assert "not a trail" not in _rows_plain(clean)
    screen = _composer(
        _topo(),
        hops=["3d63c6429436", "f2c24f54551e"] * 2,  # …→ 3d → f2 again
        target=False,
    )
    body = _rows_plain(screen)
    assert "⚠" in body and "not a trail" in body and "records ignore" in body
    screen.handle("backspace")  # remove the second f2, and the repeat is gone
    assert "not a trail" not in _rows_plain(screen)


def test_composer_dialog_only_ever_grows() -> None:
    """The box ratchets in both dimensions, so it does not jump when the path changes."""
    screen = _composer(_topo(), hops=[], target=False)
    assert screen.grow_only is True
    # ratchet_viewport and ratchet_width are the grow-only contract: they rise and never drop.
    assert screen.ratchet_viewport(6) == 6
    assert screen.ratchet_viewport(14) == 14  # a longer suggestion list makes the box bigger
    assert screen.ratchet_viewport(4) == 14  # a shorter list after it keeps the taller box
    assert screen.ratchet_width(40) == 40
    assert screen.ratchet_width(64) == 64  # a longer route preview makes the box wider
    assert screen.ratchet_width(40) == 64  # narrower content after it keeps the wider box


def test_composer_target_mode_warning_covers_the_mirrored_return() -> None:
    """The check runs over the whole boomerang. A repeated outbound section starts the warning."""
    clean = _composer(_topo(), hops=["3d63c6429436"])
    assert "not a trail" not in _rows_plain(clean)  # the plain boomerang is a trail
    screen = _composer(
        _topo(),
        hops=["3d63c6429436", "27d4396a2967", "3d63c6429436", "27d4396a2967"],
    )
    assert "not a trail" in _rows_plain(screen)


async def test_composer_fetch_row_resolves_a_fetch_request() -> None:
    """On a repeater that MeshTerm can fetch from, Enter on the fetch row sends a request."""
    import asyncio

    screen = _composer(_topo(), hops=["3d63c6429436"], fetch_nodes=frozenset({"3d63c6429436"}))
    screen.future = asyncio.get_running_loop().create_future()
    body = _rows_plain(screen)
    assert "Fetch neighbours from" in body and "Hub" in body
    screen.handle("enter")  # an empty graph has no suggestions, so fetch is the top row
    result = screen.future.result()
    assert isinstance(result, FetchNeighbours) and result.node == "3d63c6429436"
    assert screen.hops == ["3d63c6429436"]  # kept, so the owner can reopen it mid-path


def test_composer_hides_fetch_row_off_fetchable_tails() -> None:
    """The fetch row follows the tail of the path. At our own node there is nothing to ask."""
    screen = _composer(_topo(), fetch_nodes=frozenset({"3d63c6429436"}))
    assert "Fetch neighbours" not in _rows_plain(screen)  # the tail is us, not the repeater


def test_composer_use_row_names_the_action_not_the_path() -> None:
    """The commit row does not write the route again. The preview two rows up is the route.

    A repeat as raw hex (JP, 2026-08-10) said the same thing in a worse way, and the row
    grew by one hop each time the path grew. Only the unarmed case needs words: a row that
    cannot commit must say why.
    """
    from tests.conftest import plain

    walks = [_traced(("3d", 12.0), (None, 12.0))]

    def use_row(screen: PathComposerScreen) -> str:
        body = plain(screen.render_body(90))
        row = next(ln for ln in body.splitlines() if "Use this path" in ln)
        return row.replace("❯", "").strip()

    armed = _composer(_topo(trace_paths=walks), hops=["3d63c6429436"])
    assert use_row(armed) == "✓ Use this path"
    assert armed._spec() == "3d,f2,3d"  # …the spec is still what Enter commits

    # An unarmed row must still say why it cannot commit.
    empty = _composer(_topo(trace_paths=walks), hops=[])
    empty._spec = lambda: ""  # type: ignore[method-assign]
    assert use_row(empty) == "✓ Use this path  (add a hop first)"


async def test_composer_commits_spec_auto_and_cancel() -> None:
    """Use resolves the spec with its mirrored return leg. Auto resolves empty. Esc cancels.

    Only the Esc key leaves. The action group ends at Auto, so End goes to the last row that
    does something. It does not go to a row that only presses Esc for the user.
    """
    import asyncio

    walks = [_traced(("3d", 12.0), (None, 12.0))]

    screen = _composer(_topo(trace_paths=walks), hops=["3d63c6429436"])
    screen.future = asyncio.get_running_loop().create_future()
    screen.handle("end")  # jump to the last action row (Auto)…
    screen.handle("up")  # …up to "Use this path"
    screen.handle("enter")
    assert screen.future.result() == "3d,f2,3d"

    auto = _composer(_topo(trace_paths=walks))
    auto.future = asyncio.get_running_loop().create_future()
    auto.handle("end")  # "Auto — let the device route" closes the group
    auto.handle("enter")
    assert auto.future.result() == AUTO_SPEC

    cancelled = _composer(_topo(trace_paths=walks))
    cancelled.future = asyncio.get_running_loop().create_future()
    cancelled.handle("escape")
    assert cancelled.future.result() is CANCEL


def test_composer_path_mode_opens_star_to_star_without_auto(monkeypatch) -> None:  # noqa: ANN001
    """With no target, the preview is only ``★ → + → ★`` and there is no Auto action.

    Both ends are our own node, bare and faded. A walk always leaves us and comes home to
    us, and the user cannot compose or remove an end. The automatic grey shows this. The
    empty slot is between the ends. With no destination, the device has nothing to route to.
    """
    monkeypatch.setattr(pathline, "powerline_enabled", lambda: False)  # assert the words
    screen = _composer(_topo(), target=False)
    assert screen.title == "Compose path"  # no target to name
    preview = screen._route_preview().text()
    # The preview has no name and no hash of ours. The slot shows where the first hop goes.
    assert preview.plain == f"{SELF_GLYPH} → {CURSOR_GLYPH} → {SELF_GLYPH}"
    stars = [
        str(span.style)
        for span in preview.spans
        if preview.plain[span.start : span.end] == SELF_GLYPH
    ]
    assert stars == ["faint", "faint"]  # read-only, like each chip that the screen manages
    assert "Auto" not in _rows_plain(screen)


def test_composer_names_an_ambiguous_hop_through_the_owning_screens_resolver() -> None:
    """A short hop that the topology does not name still shows a name, with its hash beside it.

    A hop of 1 byte that matches the prefix of two contacts is ambiguous. The topology does
    not guess, and it keeps the bare hash. Thus the suggestion list proposed unclear hex for
    nodes that the trace screen behind it named without a problem. The resolver of the
    owning screen is the fallback, so both surfaces show the same thing.
    """
    twin = Contact(name="Twin", public_key="3d99" + "0" * 60)  # makes a bare "3d" ambiguous
    topo = _topo(trace_paths=[_traced(("3d", 12.0), (None, 12.0))], contacts=[REPEATER, twin])
    assert topo.display_name("3d") is None  # two contacts match, so the graph does not choose

    import re

    blind = _composer(topo, target=False)
    assert "Hub" not in _rows_plain(blind)  # without a fallback: bare hex, as before

    named = _composer(topo, target=False, resolve=lambda h: "Hub" if h == "3d" else h)
    body = re.sub(r"\x1b\[[0-9;]*m", "", _rows_plain(named))
    assert "Hub (3d)" in body  # the name, and the hash that addresses it beside the name


def test_composer_merges_a_stub_and_its_full_id_into_one_suggestion() -> None:
    """One node that the graph holds twice is one row, in its text and in its address.

    ``3d`` matches the prefix of two contacts, so the topology keeps it as a separate vertex
    and does not guess. It is separate from the ``3d63c6429436`` where its wider sightings
    went. When both have a name, the list proposes the same node twice. Thus they fold
    onto the longer id, and the evidence is pooled. The twin with the ``3d`` prefix has a
    different name, so it stays a separate row. A shared prefix alone never merges two nodes.
    """
    twin = Contact(name="Twin", public_key="3d99" + "0" * 60, key_prefix="3d9900000000")
    walks = [
        _traced(("3d", 6.0), (None, 6.0)),  # the ambiguous stub
        _traced(("3d63c6429436", 8.0), (None, 8.0)),  # the same node, with its full id
        _traced(("3d9900000000", 4.0), (None, 4.0)),  # a different node behind 3d
    ]
    topo = _topo(trace_paths=walks, contacts=[REPEATER, twin])
    assert topo.display_name("3d") is None  # ambiguous: the graph does not fold it itself
    assert {s.node for s in topo.next_hops(topo.self_id)} == {
        "3d",
        "3d63c6429436",
        "3d9900000000",
    }

    screen = _composer(topo, target=False, resolve=make_node_resolver([REPEATER, twin]))
    rows = screen._suggestions()
    assert [r.node for r in rows] == ["3d63c6429436", "3d9900000000"]
    assert rows[0].link.samples == 4  # the readings out and back of both walks, pooled
    assert rows[0].link.median_snr == 7.0  # 6.0 and 8.0 twice each


def test_composer_steps_off_a_merged_node_with_all_its_evidence() -> None:
    """From the merged node, the composer sees all that was heard by either of its ids.

    When the stub folds away, its links must not fold away with it. If they fold away, the
    merge silently removes from the composer all that was observed only under the stub.
    """
    twin = Contact(name="Twin", public_key="3d99" + "0" * 60, key_prefix="3d9900000000")
    walks = [
        _traced(("3d", 6.0), ("f2", -5.0), ("3d", -5.5), (None, 6.0)),  # Far, through the stub
        _traced(("3d63c6429436", 8.0), (None, 8.0)),  # the full id
        _traced(("3d9900000000", 4.0), (None, 4.0)),  # the other 3d node
    ]
    topo = _topo(trace_paths=walks, contacts=[REPEATER, FAR, twin])
    screen = _composer(
        topo, target=False, hops=["3d63c6429436"], resolve=make_node_resolver([REPEATER, FAR, twin])
    )
    # Far was heard only through the stub, and the screen still offers it from the full id.
    assert [s.node for s in screen._suggestions()] == ["f2c24f54551e"]


async def test_composer_path_mode_suggests_through_anything_and_commits_verbatim() -> None:
    """A path walk routes through any node and commits exactly the hops that the user composed."""
    import asyncio

    walks = [_traced(("3d", 12.0), ("f2", -5.0), ("3d", -5.5), (None, 12.0))]
    screen = _composer(_topo(trace_paths=walks), hops=["3d63c6429436"], target=False)
    # There is no pinned target. The tail (Hub) hears Far, so Far is a plain hop suggestion.
    assert any(s.node == "f2c24f54551e" for s in screen._suggestions())
    screen.handle("enter")  # the only suggestion: Far joins the walk
    # A return leg can use an outbound repeater again (only the tail is not permitted).
    assert any(s.node == "3d63c6429436" for s in screen._suggestions())
    # Out through Hub, home directly from Far: nothing is appended or mirrored.
    assert screen._spec() == "3d,f2"

    loop = asyncio.get_running_loop()
    screen.future = loop.create_future()
    screen.handle("end")  # "Use this path": path mode ends the group there, with no Auto row
    screen.handle("enter")
    assert screen.future.result() == "3d,f2"

    # With no hops, there is nothing to commit. Use must do nothing, because a resolve of ""
    # looks like Auto (routed by the device).
    empty = _composer(_topo(trace_paths=walks), target=False)
    empty.future = loop.create_future()
    assert "(add a hop first)" in _rows_plain(empty)
    empty.handle("end")
    empty.handle("up")
    empty.handle("enter")
    assert not empty.future.done()


# --- the prefix settle is a scan of a sorted range, not a sweep of the graph ---------


def test_extensions_matches_a_naive_scan_over_awkward_shapes() -> None:
    """The range that a binary search finds must be the same as the range from a scan of each node.

    ``_extensions`` answers the question "which ids strictly extend this short one". It
    does a binary search of a list in lexicographic order, because its caller asks once for
    each node, and the caller of that caller restarts the whole pass after each merge. On a
    real mesh, the scan was approximately 24 million ``len`` calls and sixteen seconds. The
    shapes below are the shapes that a graph of hex prefixes produces: chains, shared stems,
    a full-width id, and near-misses on each side of the run that a loose range includes.
    """
    from meshterm.services.topology import MeshTopology

    nodes = {
        "6",
        "65",
        "6532",
        "6532eb",
        "6532eb00aa11",
        "6533",
        "653300ff",
        "66",
        "6600",
        "a1",
        "a1b2",
        "a1b3",
        "b0",
        "ffffffffffff",
    }
    ordered = sorted(nodes)
    for short in sorted(nodes):
        naive = sorted(
            other
            for other in nodes
            if other != short and len(other) > len(short) and other.startswith(short)
        )
        assert sorted(MeshTopology._extensions(short, ordered)) == naive, short


def test_extensions_ignores_a_full_width_id() -> None:
    """An id of twelve hex digits is canonical. It is never the prefix of another id."""
    from meshterm.services.topology import MeshTopology

    ordered = sorted({"6532eb00aa11", "6532eb00aa1122", "65"})
    assert MeshTopology._extensions("6532eb00aa11", ordered) == []


def test_prefix_settle_folds_a_chain_but_not_a_fork() -> None:
    """The end-to-end behaviour that the fast lookup must keep.

    ``65`` -> ``6532`` -> ``6532eb...`` is one node with three names, so the graph holds it
    once at the end. If a sibling really forks the stem, ``65`` is no longer an abbreviation.
    It is then a random choice between two real nodes. It must stay as it is, with a short
    name that is correct, and not be folded onto either node.
    """
    from meshterm.core.models import utcnow
    from meshterm.services.topology import MeshTopology

    def graph(hops):
        topo = MeshTopology(self_id="local", contacts=[])
        now = utcnow()
        for hop in hops:
            topo.add_walk(["local", hop], when=now, source="trace")
        topo.coalesce_prefixes()
        return {end for link in topo._links for end in link}

    chain = graph(("65", "6532", "6532eb00aa11"))
    assert chain == {"local", "6532eb00aa11"}, f"chain not folded: {sorted(chain)}"

    fork = graph(("65", "6532", "6532eb00aa11", "653300ff2211"))
    assert "6532eb00aa11" in fork and "653300ff2211" in fork
    assert "6532" not in fork, "an unambiguous link in the chain should still fold"
    assert "65" in fork, "a stub opening two real nodes must not be guessed onto one"


def test_prefix_settle_is_repeatable_whatever_order_the_nodes_arrived_in() -> None:
    """The same evidence must always settle to the same graph.

    The passes offer the ids that have too few bytes, the shortest first. For some time, the
    ids of the same width came out in the order in which the node set iterated. CPython
    randomizes the hash of strings in each process, so two runs over the same evidence could
    do the merges in different orders. If one fold changes what a later fold can see, the
    graphs are different. A feed of the same walks in reversed order causes the same problem,
    because a set that was built with a different insertion sequence iterates in a different
    order. The settled endpoints must not depend on the order.
    """
    from meshterm.core.models import utcnow
    from meshterm.services.topology import MeshTopology

    walks = [
        ["local", "a1", "b2c3"],
        ["local", "a1b2", "b2c3d4"],
        ["local", "a1b2c3d4e5f6"],
        ["local", "b2c3d4e5f601"],
        ["local", "a1b2c3", "c4"],
        ["local", "c4d5e6f70011"],
        ["local", "b2", "a1b2c3d4e5f6"],
    ]

    def settle(order):
        topo = MeshTopology(self_id="local", contacts=[])
        now = utcnow()
        for walk in order:
            topo.add_walk(list(walk), when=now, source="trace")
        topo.coalesce_prefixes()
        return sorted(topo._links)

    assert settle(walks) == settle(list(reversed(walks)))


def test_a_merge_leaves_the_neighbour_table_exactly_as_a_fresh_one() -> None:
    """The settle keeps one table across each merge and does not build it again.

    This is correct only while the incremental update is exact. A stale neighbour gives
    bad evidence to the corroboration vote. A node that stays after its last link folded away
    gives a merge candidate that no longer exists. Thus after each merge, the kept
    table must be equal to a table that is derived from the changed graph.
    """
    from meshterm.core.models import utcnow
    from meshterm.services.topology import MeshTopology

    topo = MeshTopology(self_id="local", contacts=[])
    now = utcnow()
    for walk in (
        ["local", "a1", "b2c3d4e5f601"],
        ["local", "a1b2c3d4e5f6"],
        ["a1", "c4d5e6f70011"],
        ["a1b2", "a1b2c3d4e5f6"],
        ["b2c3d4e5f601", "c4d5e6f70011"],
        ["local", "d7"],
        ["d7e8f9001122", "local"],
    ):
        topo.add_walk(list(walk), when=now, source="trace")

    adjacency = topo._adjacency()
    merges = 0
    while True:
        shorts = sorted((n for n in adjacency if len(n) < 12), key=lambda n: (len(n), n))
        ordered = sorted(adjacency)
        merge = topo._next_prefix_merge(shorts, ordered) or topo._next_corroborated_merge(
            shorts, ordered, adjacency
        )
        if merge is None:
            break
        topo._merge_node(*merge, adjacency=adjacency)
        merges += 1
        assert adjacency == topo._adjacency(), f"table drifted after merging {merge}"
    assert merges, "the fixture should exercise at least one merge"


def test_a_stub_whose_only_link_was_its_owner_takes_the_owner_out_with_it() -> None:
    """The incremental table must handle this corner: both ends can leave at once.

    ``a1`` is heard only with ``a1b2c3d4e5f6``. There is one link, between the two names of
    one node. When the stub folds, that link collapses to a self-loop and is discarded. The
    owner then has no links and leaves the graph too. Neither node can stay in the node set
    that the next pass reads.
    """
    from meshterm.core.models import utcnow
    from meshterm.services.topology import MeshTopology

    topo = MeshTopology(self_id="local", contacts=[])
    topo.add_walk(["a1", "a1b2c3d4e5f6"], when=utcnow(), source="trace")
    adjacency = topo._adjacency()
    topo._merge_node("a1", "a1b2c3d4e5f6", adjacency=adjacency)
    assert topo._links == {}
    assert adjacency == {}


def test_composer_row_cursor_clamps_at_both_ends() -> None:
    """The list window of the suggestions does not wrap.

    The ↑ key on the first row and the ↓ key past the last row stay where they are. They do
    not move the window from one end to the other.
    """
    walks = [_traced(("3d", 12.0), ("f2", -5.0), ("3d", -5.5), (None, 12.0))]
    screen = _composer(_topo(trace_paths=walks))
    screen.render_body(90)
    last = len(screen._rows()) - 1
    screen.handle("up")
    assert screen._index == 0
    for _ in range(last + 5):
        screen.handle("down")
    assert screen._index == last
