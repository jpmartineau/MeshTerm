# SPDX-License-Identifier: Apache-2.0
"""Tests for the Repository layer that stores data."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from meshterm.core.models import PATH_TRACE_TARGET, Hop, TraceResult
from meshterm.persistence.repository import Repository


@pytest.fixture()
def repo(tmp_path: Path) -> Repository:
    """A Repository on a temporary database. It closes when the test ends."""
    r = Repository(tmp_path / "test.db")
    yield r
    r.close()


def _make_trace(
    target: str,
    *hops: tuple[str | None, float],
    success: bool = True,
    rtt: float | None = 42.0,
    tx: int | None = 11,
    ts: datetime | None = None,
) -> TraceResult:
    """Build a trace. The arguments set each field that the storage uses."""
    return TraceResult(
        target=target,
        success=success,
        hops=[Hop(i, node, snr) for i, (node, snr) in enumerate(hops)],
        round_trip_ms=rtt,
        tx_power=tx,
        timestamp=ts or datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
    )


# -- record + hydrate round-trip -------------------------------------------


def test_record_and_hydrate_preserves_all_fields(repo: Repository) -> None:
    """Each TraceResult field stays the same after a write and a read."""
    ts = datetime(2026, 6, 1, 8, 30, 0, tzinfo=timezone.utc)
    trace = _make_trace(
        "Alice", ("relay", 3.5), ("Alice", -2.0), (None, 1.0), rtt=88.5, tx=7, ts=ts
    )
    run = repo.start_run("trace", {})
    repo.record_trace(run, trace)

    got = repo.recent_traces("Alice")[0]
    assert got.target == "Alice"
    assert got.success is True
    assert got.round_trip_ms == 88.5
    assert got.tx_power == 7
    assert got.timestamp == ts
    assert [(h.index, h.node, h.snr) for h in got.hops] == [
        (0, "relay", 3.5),
        (1, "Alice", -2.0),
        (2, None, 1.0),
    ]


def test_failed_trace_round_trips(repo: Repository) -> None:
    """A failed trace (no hops, no RTT) is stored, and it reads back correctly."""
    trace = _make_trace("Bob", success=False, rtt=None, tx=None)
    run = repo.start_run("trace", {})
    repo.record_trace(run, trace)

    got = repo.recent_traces("Bob")[0]
    assert got.success is False
    assert got.hops == []
    assert got.round_trip_ms is None
    assert got.tx_power is None
    assert got.path_hash_bytes is None  # no hashes give a width


def test_hydrated_traces_recover_the_path_hash_width(repo: Repository) -> None:
    """The stored hop hashes have the width of the trace, so the hydration recovers it.

    If the width is lost, the route line of a previous trace shows each hash at the wrong
    length. This includes the full 64-character public key of our device.
    """
    run = repo.start_run("trace", {})
    trace = _make_trace("Alice", ("3d63", 3.5), ("f2c2", -2.0), (None, 1.0))
    repo.record_trace(run, trace)

    assert repo.latest_trace("Alice").path_hash_bytes == 2
    assert repo.recent_traces("Alice")[0].path_hash_bytes == 2


# -- recent_traces ---------------------------------------------------------


def test_recent_traces_returns_newest_first(repo: Repository) -> None:
    """Traces return in reverse order of insertion."""
    run = repo.start_run("trace", {})
    for i in range(5):
        repo.record_trace(run, _make_trace("X", ("hop", float(i))))

    traces = repo.recent_traces("X")
    snrs = [t.hops[0].snr for t in traces]
    assert snrs == [4.0, 3.0, 2.0, 1.0, 0.0]


def test_recent_traces_respects_limit(repo: Repository) -> None:
    """The limit parameter sets the maximum number of traces that return."""
    run = repo.start_run("trace", {})
    for _ in range(10):
        repo.record_trace(run, _make_trace("X", ("h", 1.0)))

    assert len(repo.recent_traces("X", limit=3)) == 3


def test_recent_traces_filters_by_target(repo: Repository) -> None:
    """Only the traces to the requested target return."""
    run = repo.start_run("trace", {})
    repo.record_trace(run, _make_trace("Alice", ("r", 1.0)))
    repo.record_trace(run, _make_trace("Bob", ("r", 2.0)))
    repo.record_trace(run, _make_trace("Alice", ("r", 3.0)))

    got = repo.recent_traces("Alice")
    assert len(got) == 2
    assert all(t.target == "Alice" for t in got)


def test_recent_traces_includes_failures(repo: Repository) -> None:
    """Successful traces and failed traces both return, because a failure gives information."""
    run = repo.start_run("trace", {})
    repo.record_trace(run, _make_trace("X", ("h", 1.0), success=True))
    repo.record_trace(run, _make_trace("X", success=False))

    got = repo.recent_traces("X")
    assert len(got) == 2
    statuses = {t.success for t in got}
    assert statuses == {True, False}


def test_recent_traces_empty_target(repo: Repository) -> None:
    """A query for a target with no stored traces returns an empty list."""
    assert repo.recent_traces("NoSuchNode") == []


# -- traced_targets --------------------------------------------------------


def test_traced_targets_recency_ordered_and_capped(repo: Repository) -> None:
    """Targets return with the most recently traced first, with no duplicates, up to a limit.

    The total count of traces must not matter. An old target with many traces (Alice here)
    ranks by its latest trace. Thus old favourites and mock names that remain go out of the
    short "Recently traced" list of the picker. They do not stay at the top for ever.
    """
    run = repo.start_run("trace", {})
    for _ in range(8):
        repo.record_trace(run, _make_trace("Alice", ("r", 1.0)))
    for name in ["Bob", "Carol", "Dave", "Erin", "Frank"]:
        repo.record_trace(run, _make_trace(name, ("r", 1.0)))
    repo.record_trace(run, _make_trace("Bob", ("r", 1.0)))  # the user traced Bob again just now

    assert repo.traced_targets() == ["Bob", "Frank", "Erin", "Dave", "Carol"]
    assert repo.traced_targets(limit=2) == ["Bob", "Frank"]


def test_traced_targets_empty_db(repo: Repository) -> None:
    """If there are no traces, the list is empty."""
    assert repo.traced_targets() == []


def test_traced_targets_excludes_path_walks(repo: Repository) -> None:
    """Path walks are stored under the ``(path)`` sentinel.

    They never become targets that the user can select.
    """
    run = repo.start_run("trace", {})
    repo.record_trace(run, _make_trace("Alice", ("r", 1.0)))
    repo.record_trace(run, _make_trace(PATH_TRACE_TARGET, ("r", 2.0)))

    assert repo.traced_targets() == ["Alice"]
    # But the walks still supply the line of the previous route on the path screen.
    latest = repo.latest_trace(PATH_TRACE_TARGET)
    assert latest is not None and latest.target == PATH_TRACE_TARGET


def test_target_trace_counts_tallies_successes_and_totals(repo: Repository) -> None:
    """For each target, the count is (successes, total) of all traces.

    It includes failures, but not path walks.
    """
    run = repo.start_run("trace", {})
    repo.record_trace(run, _make_trace("Alice", ("r", 1.0), success=True))
    repo.record_trace(run, _make_trace("Alice", success=False))
    repo.record_trace(run, _make_trace("Alice", ("r", 2.0), success=True))
    repo.record_trace(run, _make_trace("Bob", ("r", 1.0), success=True))
    repo.record_trace(run, _make_trace(PATH_TRACE_TARGET, ("r", 3.0), success=True))

    counts = repo.target_trace_counts()
    assert counts["Alice"] == (2, 3)  # two of three traces returned
    assert counts["Bob"] == (1, 1)
    assert PATH_TRACE_TARGET not in counts  # a path walk has no target


def test_target_last_traced_keeps_latest_per_target(repo: Repository) -> None:
    """For each target, the time of the most recent trace.

    It counts failures, but not path walks.
    """
    run = repo.start_run("trace", {})
    early = datetime(2026, 1, 10, 9, 0, tzinfo=timezone.utc)
    late = datetime(2026, 1, 12, 18, 0, tzinfo=timezone.utc)
    repo.record_trace(run, _make_trace("Alice", ("r", 1.0), ts=early))
    repo.record_trace(run, _make_trace("Alice", success=False, ts=late))  # newer, and it timed out
    repo.record_trace(run, _make_trace("Bob", ("r", 1.0), ts=early))
    repo.record_trace(run, _make_trace(PATH_TRACE_TARGET, ("r", 3.0), ts=late))

    last = repo.target_last_traced()
    assert last["Alice"] == late  # the newer trace wins, also when it failed
    assert last["Bob"] == early
    assert PATH_TRACE_TARGET not in last  # a path walk has no target


# -- latest_trace edge cases -----------------------------------------------


def test_latest_trace_success_only_skips_failures(repo: Repository) -> None:
    """success_only=True skips failed traces, also when they are newer."""
    run = repo.start_run("trace", {})
    repo.record_trace(run, _make_trace("X", ("h", 1.0), success=True))
    repo.record_trace(run, _make_trace("X", success=False))

    got = repo.latest_trace("X", success_only=True)
    assert got is not None
    assert got.success is True


def test_latest_trace_success_only_false_includes_failures(repo: Repository) -> None:
    """success_only=False returns the newest trace, with any result."""
    run = repo.start_run("trace", {})
    repo.record_trace(run, _make_trace("X", ("h", 1.0), success=True))
    repo.record_trace(run, _make_trace("X", success=False))

    got = repo.latest_trace("X", success_only=False)
    assert got is not None
    assert got.success is False


# -- runs ------------------------------------------------------------------


def test_run_lifecycle(repo: Repository) -> None:
    """A run changes its status from running to ok, and gets a summary."""
    run_id = repo.start_run("route-map", {"target": "Alice"}, profile="field")
    runs = repo.list_runs()
    assert runs[0].status == "running"
    assert runs[0].tool == "route-map"
    assert runs[0].profile == "field"
    assert runs[0].finished_at is None

    repo.finish_run(run_id, "ok", {"stability": 0.85})
    runs = repo.list_runs()
    assert runs[0].status == "ok"
    assert runs[0].finished_at is not None
    assert runs[0].summary == {"stability": 0.85}


def test_finish_run_error_without_summary(repo: Repository) -> None:
    """A run that has an error stores its status and no summary."""
    run_id = repo.start_run("trace", {})
    repo.finish_run(run_id, "error")

    runs = repo.list_runs()
    assert runs[0].status == "error"
    assert runs[0].summary is None


def test_list_runs_respects_limit(repo: Repository) -> None:
    """list_runs returns no more results than the requested limit."""
    for i in range(10):
        repo.start_run("trace", {"i": i})

    assert len(repo.list_runs(limit=3)) == 3


def test_list_runs_newest_first(repo: Repository) -> None:
    """Runs return in reverse order of insertion."""
    repo.start_run("first", {})
    repo.start_run("second", {})
    repo.start_run("third", {})

    tools = [r.tool for r in repo.list_runs()]
    assert tools == ["third", "second", "first"]
