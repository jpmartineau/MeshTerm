# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the records of the trophy case: walk scoring, derivation, and storage.

The scoring layer only measures. The UI owns the radio. Thus all the tests run with no UI:
they use trace results that the test builds by hand, synthetic positions, and a temporary
repository for the leaderboard invariants.
"""

from __future__ import annotations

from meshterm.core.models import Hop, TraceResult
from meshterm.persistence.repository import Repository
from meshterm.services.records import (
    CATEGORY_BY_ID,
    compute_walk_stats,
    first_repeated_edge,
    max_hops,
    walk_from_trace,
    walk_scores,
)

#: Positions near Montréal, approximately 1.1 km apart. Thus the distance scores are easy to check.
SELF_POS = (45.500, -73.600)
HUB_ID, FAR_ID, LEAF_ID = "3d63c6429436", "f2c24f54551e", "27d4396a2967"
POSITIONS = {
    HUB_ID: (45.510, -73.600),  # approximately 1.11 km north of us
    FAR_ID: (45.510, -73.585),  # north-east of us
    LEAF_ID: (45.490, -73.600),  # approximately 1.11 km south of us
}


def _result(*snrs: float, rtt: float = 250.0, nodes=None) -> TraceResult:  # noqa: ANN001
    """A successful walk reply: one hop for each SNR. The last hop has no hash, and it is us."""
    hops = []
    for i, snr in enumerate(snrs):
        node = None if i == len(snrs) - 1 else (nodes[i] if nodes else f"{i:02x}")
        hops.append(Hop(index=i, node=node, snr=snr))
    return TraceResult(target="walk", success=True, hops=hops, round_trip_ms=rtt)


# --- scoring ---------------------------------------------------------------------


def test_walk_stats_measure_distance_reach_and_area() -> None:
    """The circuit us→Hub→Far→us has positions from end to end.

    The stats give exact km, far, and area.
    """
    stats = compute_walk_stats(
        (HUB_ID, FAR_ID),
        _result(8.0, 6.0, 7.0).hops,
        rtt_ms=250.0,
        positions=POSITIONS,
        self_pos=SELF_POS,
    )
    assert stats.km_complete
    assert stats.km_travelled > 2.0  # up approximately 1.1 km, across, and back home
    assert stats.far_km is not None and 1.0 < stats.far_km < 2.5
    assert stats.area_km2 is not None and stats.area_km2 > 0.3
    assert stats.distinct_nodes == 2 and not stats.repeats
    assert stats.min_snr == 6.0


def test_walk_stats_unpositioned_segments_score_a_lower_bound() -> None:
    """A segment with no position gives a lower bound for the score.

    A hop with no position adds 0 km and clears the complete flag.
    """
    positions = {HUB_ID: POSITIONS[HUB_ID]}  # Far is not on the map
    stats = compute_walk_stats(
        (HUB_ID, FAR_ID),
        _result(8.0, 6.0, 7.0).hops,
        rtt_ms=None,
        positions=positions,
        self_pos=SELF_POS,
    )
    assert not stats.km_complete
    assert 1.0 < stats.km_travelled < 1.3  # only the us→Hub leg, which has positions, counts


def test_longest_leg_measures_one_link_and_names_its_ends() -> None:
    """The longest leg measures one link and names its two ends.

    The longest leg is the longest single segment of the circuit. The result has the two
    nodes that the leg spans. Hub is approximately 1.11 km north and Leaf is approximately
    1.11 km south. Thus the Hub→Leaf crossing (approximately 2.2 km) is longer than both
    legs that touch us. It is a link between two hops, and neither of them is ours.
    """
    stats = compute_walk_stats(
        (HUB_ID, LEAF_ID),
        _result(8.0, 6.0, 7.0).hops,
        rtt_ms=None,
        positions=POSITIONS,
        self_pos=SELF_POS,
    )
    assert stats.leg_km is not None and 2.0 < stats.leg_km < 2.5
    assert stats.leg_link == (HUB_ID, LEAF_ID)
    # The far point measures reach and not span: the furthest node is only half as far away.
    assert stats.far_km is not None and stats.far_km < stats.leg_km
    assert stats.as_dict()["leg_link"] == [HUB_ID, LEAF_ID]  # in the JSON form for storage


def test_longest_leg_names_our_own_end_as_none() -> None:
    """The longest leg names our own end as ``None``.

    A leg that leaves or comes home has ``None`` for our end. The UI draws it as the ★.
    """
    stats = compute_walk_stats(
        (HUB_ID,),
        _result(8.0, 7.0).hops,
        rtt_ms=None,
        positions=POSITIONS,
        self_pos=SELF_POS,
    )
    assert stats.leg_link == (None, HUB_ID)  # us → Hub, the outbound half


def test_longest_leg_needs_two_positioned_ends() -> None:
    """The longest leg needs two ends with positions.

    If no segment has a position at both ends, the score is nothing, and not a false zero.
    """
    stats = compute_walk_stats(
        (HUB_ID,),
        _result(8.0, 7.0).hops,
        rtt_ms=None,
        positions=POSITIONS,
        self_pos=None,
    )
    assert stats.leg_km is None and stats.leg_link is None
    assert "long_leg" not in walk_scores(stats)


def test_cross_category_scores_score_every_game_at_once() -> None:
    """Cross-category scores give a score for each game at one time.

    One walk competes on each board where it can. This is the every-board-at-once rule.
    """
    stats = compute_walk_stats(
        (HUB_ID, FAR_ID, HUB_ID),
        _result(8.0, -11.0, 7.0, 9.0).hops,
        rtt_ms=100.0,
        positions=POSITIONS,
        self_pos=SELF_POS,
    )
    scores = walk_scores(stats)
    assert scores["grand_tour"] == 2.0  # two different nodes, and a repeat visit is allowed
    assert "clean_trail" not in scores  # Hub repeats: it is disqualified, and the score is not zero
    assert scores["thin_thread"] == -11.0  # the weakest link that is left
    assert scores["long_haul"] > 3.0
    assert scores["far_point"] > 1.0
    assert scores["long_leg"] > 0.5


def test_category_titles_are_plain_and_ids_are_stable() -> None:
    """The ids are database keys, and they do not change.

    The titles are plain text for the trophy case.
    """
    assert CATEGORY_BY_ID["grand_tour"].title == "Most nodes"
    assert CATEGORY_BY_ID["thin_thread"].title == "Weakest link"
    assert CATEGORY_BY_ID["long_haul"].title == "Longest distance"
    # Each id still resolves. The stored keys did not change when the titles were renamed.
    assert set(CATEGORY_BY_ID) == {
        "long_haul",
        "far_point",
        "long_leg",
        "grand_tour",
        "clean_trail",
        "thin_thread",
        "big_loop",
    }


def test_max_hops_follows_the_64_byte_path_field() -> None:
    """The width limits the walk: 64 hops at 1 byte, 16 hops at 4 bytes."""
    assert max_hops(1) == 64
    assert max_hops(2) == 32
    assert max_hops(4) == 16


# --- the no-cheat rule (a record walk must be a trail) -------------------------------


def test_first_repeated_edge_flags_only_a_same_direction_recross() -> None:
    """Only a crossing of a link two times in the same direction is a repeated edge.

    a→b two times breaks the trail. The return leg b→a of a boomerang never breaks it.
    """
    assert first_repeated_edge(("a", "b", "c")) is None
    # A target boomerang goes back over each link in the other direction, by design.
    # It is still a trail.
    assert first_repeated_edge(("a", "b", "t", "b", "a")) is None
    assert first_repeated_edge(("a", "b", "a", "b")) == ("a", "b")
    assert first_repeated_edge(("A ", "b", "a", "B")) == ("a", "b")  # case and spaces do not matter
    assert first_repeated_edge(()) is None


def test_walk_scores_disqualify_a_non_trail_from_every_board() -> None:
    """A walk that is not a trail is disqualified from each board.

    A walk that uses a link two times in the same direction adds km and hops at no cost.
    Each board says no to it.
    """
    stats = compute_walk_stats(
        (HUB_ID, FAR_ID, HUB_ID, FAR_ID),  # the walk uses Hub→Far two times
        _result(8.0, 6.0, 7.0, 5.0, 9.0).hops,
        rtt_ms=100.0,
        positions=POSITIONS,
        self_pos=SELF_POS,
    )
    assert stats.km_travelled > 0  # it would have a score
    assert walk_scores(stats) == {}  # but the arbiter disqualifies it completely


# --- deriving a walk from a trace ---------------------------------------------------


def test_walk_from_trace_derives_spec_route_and_stats() -> None:
    """A successful reply gives its spec, its canonical route, and its stats.

    The user can walk the spec again.
    """
    result = _result(8.0, 6.0, 7.0, nodes=["3d", "f2"])  # two relays, then us
    canon = {"3d": HUB_ID, "f2": FAR_ID}
    derived = walk_from_trace(
        result,
        canonical=lambda h: canon.get(h),
        positions=POSITIONS,
        self_pos=SELF_POS,
    )
    assert derived is not None
    spec, route, stats = derived
    assert spec == "3d,f2"  # the reply hops, joined. The user can walk it again exactly.
    assert route == (HUB_ID, FAR_ID)  # canonical, for a stable display and stable geometry
    assert stats.km_complete and stats.far_km is not None


def test_walk_from_trace_ignores_a_failure_or_a_reply_with_no_relays() -> None:
    """A failed trace, or a trace that only came back from us, has no score."""
    failed = TraceResult(target="walk", success=False, hops=[])
    assert walk_from_trace(failed, canonical=lambda h: h, positions={}, self_pos=None) is None
    # A reply direct to us has only the hop home, which has no hash. There is no relay to score.
    direct = TraceResult(target="walk", success=True, hops=[Hop(index=0, node=None, snr=9.0)])
    assert walk_from_trace(direct, canonical=lambda h: h, positions={}, self_pos=None) is None


# --- the leaderboards ----------------------------------------------------------------


def test_leaderboard_keeps_five_dedupes_and_prunes_the_worst(tmp_path) -> None:  # noqa: ANN001
    """The placement, the removal of duplicate specs, and the pruning keep the top-5 invariant."""
    repo = Repository(tmp_path / "t.db")
    try:
        for i in range(6):
            repo.record_discovery(
                "grand_tour",
                1,
                f"a{i},b{i}",
                (f"a{i}", f"b{i}"),
                score=float(i),
                stats={},
                app_version="0.1.0",
            )
        rows = repo.discoveries("grand_tour", width_bytes=1)
        assert len(rows) == 5
        assert min(r.score for r in rows) == 1.0  # the walk with score 0 dropped out
        # A new walk of a stored spec can only improve its row.
        assert (
            repo.record_discovery(
                "grand_tour",
                1,
                "a5,b5",
                ("a5", "b5"),
                score=2.0,
                stats={},
                app_version="0.1.0",
            )
            is None
        )
        improved = repo.record_discovery(
            "grand_tour",
            1,
            "a5,b5",
            ("a5", "b5"),
            score=9.0,
            stats={"hop_count": 2},
            app_version="0.1.1",
        )
        assert improved is not None
        rows = repo.discoveries("grand_tour", width_bytes=1)
        assert len(rows) == 5
        top = max(rows, key=lambda r: r.score)
        assert top.score == 9.0 and top.app_version == "0.1.1"
    finally:
        repo.close()


def test_leaderboard_ascends_for_thin_thread(tmp_path) -> None:  # noqa: ANN001
    """The leaderboard for thin thread ascends.

    Weakest link keeps the lowest scores. A strong link never gets a place.
    """
    repo = Repository(tmp_path / "t.db")
    try:
        for i, snr in enumerate((-2.0, -8.0, -5.0, -11.0, -3.0)):
            repo.record_discovery(
                "thin_thread",
                1,
                f"c{i}",
                (f"c{i}",),
                score=snr,
                stats={},
                app_version="0.1.0",
                ascending=True,
            )
        strong = repo.record_discovery(
            "thin_thread",
            1,
            "c9",
            ("c9",),
            score=7.0,
            stats={},
            app_version="0.1.0",
            ascending=True,
        )
        assert strong is None  # +7 dB is a very good link and a very bad record
        rows = repo.discoveries("thin_thread", width_bytes=1)
        assert len(rows) == 5 and max(r.score for r in rows) == -2.0
    finally:
        repo.close()


def test_record_deletion_by_row_category_and_wholesale(tmp_path) -> None:  # noqa: ANN001
    """The three levels of deletion: one record, one category, and all records."""
    repo = Repository(tmp_path / "t.db")
    try:
        for category in ("grand_tour", "long_haul"):
            for width in (1, 2):
                repo.record_discovery(
                    category,
                    width,
                    f"{category[:2]},{width}",
                    ("x",),
                    score=1.0,
                    stats={},
                    app_version="0.1.0",
                )
        first = repo.discoveries("grand_tour", width_bytes=1)[0]
        assert repo.delete_discovery(first.id)
        assert not repo.delete_discovery(first.id)  # the record is already deleted
        assert repo.delete_discoveries("grand_tour") == 1  # the width-2 row
        assert repo.discoveries("grand_tour") == []
        assert repo.delete_discoveries() == 2  # long_haul at the two widths
        assert repo.discoveries() == []
    finally:
        repo.close()
