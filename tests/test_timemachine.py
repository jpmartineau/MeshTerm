# SPDX-License-Identifier: Apache-2.0
"""Time Machine tests: the history queries, the bucketing math, and the pages.

The tests build the pages against a real (temporary) repository. The repository has
canned observations. Then the tests render the pages with no screen. The tests for the
dashboard use the same method.
"""

from __future__ import annotations

import re
from datetime import timedelta
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

from rich.text import Text

from meshterm.core.models import Observation, utcnow
from meshterm.persistence.repository import Repository
from meshterm.ui.braillechart import GAP
from meshterm.ui.timemachine_screen import (
    TimeMachineScreen,
    _day_centers,
    _day_columns,
    _day_ticks,
    _hour_ticks,
    _mesh_sections,
    _node_sections,
    _self_sections,
    _snr_cell_style,
    bucket_medians,
    bucketize,
)

NODE = "3d" * 6


class _FakeSession:
    def __init__(self) -> None:
        self.repaints = 0

    def invalidate(self) -> None:
        self.repaints += 1


def _seeded_repo(tmp_path: Path) -> Repository:
    """A repository with one node that sends often (SNR fades), one newcomer, and one packet row."""
    repo = Repository(tmp_path / "tm.db")
    run = repo.start_run("monitor", {}, None)
    now = utcnow()
    for hours_ago in range(48, 0, -2):
        repo.record_observation(
            run,
            Observation(
                node=NODE,
                name="Hub",
                kind="advert",
                snr=8.0 if hours_ago > 24 else -2.0,
                rssi=-95.0,
                observed_at=now - timedelta(hours=hours_ago),
            ),
        )
    repo.record_observation(
        run,
        Observation(node="f7" * 6, name="Newcomer", observed_at=now - timedelta(hours=3)),
    )
    # A packet row: it is in the daily totals, but not in the reception for each node.
    repo.record_observation(
        run,
        Observation(
            node=NODE, kind="packet", snr=1.0, path="", observed_at=now - timedelta(hours=1)
        ),
    )
    return repo


def _plain(renderables, width: int = 90) -> str:
    """Render the section renderables at ``width`` and return them as plain text."""
    from rich.console import Group

    from meshterm.ui.tui.render import render_lines
    from tests.conftest import plain

    return plain(render_lines(Group(*renderables), width))


# --- the repository queries -------------------------------------------------------------


def test_node_observations_window_and_packet_exclusion(tmp_path: Path) -> None:
    """The feed for one node has the oldest row first, a window, and no packet rows."""
    repo = _seeded_repo(tmp_path)
    everything = repo.node_observations(NODE)
    assert len(everything) == 24  # 48 h of adverts, one in each 2 h. The packet row is not in it
    assert everything[0].observed_at < everything[-1].observed_at
    day = repo.node_observations(NODE, since=utcnow() - timedelta(days=1))
    assert 0 < len(day) < len(everything)
    repo.close()


def test_first_seen_orders_arrivals_and_windows(tmp_path: Path) -> None:
    """Arrivals give the first timestamp of each node, with the newest arrival first."""
    repo = _seeded_repo(tmp_path)
    arrivals = repo.first_seen()
    assert [node for node, _n, _f in arrivals] == ["f7" * 6, NODE]
    assert arrivals[0][1] == "Newcomer"
    recent = repo.first_seen(since=utcnow() - timedelta(days=1))
    assert [node for node, _n, _f in recent] == ["f7" * 6]
    repo.close()


def test_daily_activity_counts_packets_and_nodes(tmp_path: Path) -> None:
    """The daily totals count every row, but they count only nodes that are identified."""
    repo = _seeded_repo(tmp_path)
    days = repo.daily_activity()
    assert sum(d[1] for d in days) == 26  # 24 adverts + newcomer + the packet row
    assert max(d[2] for d in days) == 2  # a maximum of Hub and Newcomer on one day
    assert days == sorted(days)  # oldest first
    repo.close()


def test_hourly_activity_groups_by_local_hour(tmp_path: Path) -> None:
    """The rhythm feed puts each observation in its local hour, with a window if the caller asks."""
    repo = Repository(tmp_path / "hh.db")
    run = repo.start_run("monitor", {}, None)
    base = utcnow().replace(hour=5, minute=0, second=0, microsecond=0)
    for minutes in (0, 10, 20):
        repo.record_observation(
            run, Observation(node="aa" * 6, observed_at=base + timedelta(minutes=minutes))
        )
    repo.record_observation(run, Observation(node="aa" * 6, observed_at=base.replace(hour=17)))
    # The code changes each UTC instant to the zone of the machine before it makes the
    # buckets. The test finds the expected local hours in the same way, so that it is true
    # in any zone.
    early, late = base.astimezone().hour, base.replace(hour=17).astimezone().hour
    counts = repo.hourly_activity()
    assert counts[early] == 3 and counts[late] == 1 and sum(counts) == 4
    # The window filter uses the raw UTC column, so only the observation at 17:00 UTC stays.
    assert sum(repo.hourly_activity(since=base.replace(hour=6))) == 1
    repo.close()


def test_hourly_series_groups_by_clock_hour(tmp_path: Path) -> None:
    """The 24 h feed puts observations in buckets for the local clock hour: packets and nodes."""
    repo = Repository(tmp_path / "hs.db")
    run = repo.start_run("monitor", {}, None)
    now = utcnow()
    base = now.replace(minute=0, second=0, microsecond=0)  # the start of the current UTC hour
    three_h = base - timedelta(hours=3)  # the start of a busy hour, three hours back
    for node in ("aa" * 6, "bb" * 6):
        repo.record_observation(
            run, Observation(node=node, observed_at=three_h + timedelta(minutes=5))
        )
    repo.record_observation(
        run, Observation(node="aa" * 6, observed_at=three_h + timedelta(minutes=20))
    )
    repo.record_observation(
        run,
        Observation(
            node="cc" * 6, kind="packet", path="", observed_at=three_h + timedelta(minutes=40)
        ),
    )
    repo.record_observation(
        run, Observation(node="aa" * 6, observed_at=base + timedelta(minutes=1))
    )
    series = repo.hourly_series(now - timedelta(days=1))
    by_hour = {iso: (pkts, nodes) for iso, pkts, nodes in series}
    # The keys are local wall-clock hours, so the test makes the expected keys with the same
    # conversion.
    three_h_key = three_h.astimezone().strftime("%Y-%m-%dT%H")
    base_key = base.astimezone().strftime("%Y-%m-%dT%H")
    assert by_hour[three_h_key] == (4, 2)  # 3 adverts and a packet row, for 2 nodes
    assert by_hour[base_key] == (1, 1)  # only the one advert of this hour
    assert series == sorted(series)  # oldest first
    repo.close()


# --- the math ---------------------------------------------------------------------------


def test_bucketize_and_medians_split_the_span() -> None:
    """Counts and medians go in the correct slices, and the edges are clamped."""
    now = utcnow()
    start = now - timedelta(hours=4)
    stamps = [start, now - timedelta(hours=3), now - timedelta(minutes=1), now]
    assert bucketize(stamps, start, now, 4) == [1, 1, 0, 2]
    pairs = [(start, 0.0), (start, 10.0), (now, -4.0)]
    medians = bucket_medians(pairs, start, now, 2)
    assert medians == [5.0, -4.0]


def test_snr_band_hangs_negative_readings_below_zero() -> None:
    """Medians of SNR that are all negative hang from a grey zero ceiling.

    The colour shows the quality.
    """
    from meshterm.ui.braillechart import chart_span, timeline_rows

    medians = [-12.0, None, -4.0, -8.0]
    assert chart_span(medians) == (-12.0, 0.0)  # zero is always on the scale
    rows = timeline_rows(medians, rows=2, style=_snr_cell_style)
    assert len(rows) == 2 and len(rows[0].plain) == 2
    # The worst reading (−12) hangs the full height. The tail of −8 goes into the bottom row
    # (right column only), and the shallower −4 does not go into it.
    assert rows[1].plain[0] != chr(0x2800)  # the −12 column reaches the bottom row
    assert rows[1].plain[1] == chr(0x2808)  # the tail of −8, with no dots of −4 (left column)
    # The cells use the shared SNR palette, and their own readings set the colours.
    assert rows[0].spans[0].style == _snr_cell_style([-12.0])
    assert rows[0].spans[1].style == _snr_cell_style([-4.0, -8.0])


def test_day_columns_always_hits_the_exact_width() -> None:
    """The stretched output always has exactly 2 × chars columns, with the remainder.

    A plain floor division (the old implementation) loses the remainder. Then the bars are
    shorter than the axis border, and the caption has the size for the full width. Thus the
    chart looks shifted to the left of the place where its own axis ends.
    """
    for n, chars in [(7, 92), (3, 10), (1, 20), (14, 40), (30, 45), (60, 30)]:
        cols = _day_columns(list(range(n)), chars)
        assert len(cols) == chars * 2, (n, chars)


def test_fill_days_shows_gap_days_as_zero_bars() -> None:
    """A quiet day between busy days is in the result with zero counts. It is not skipped."""
    from datetime import datetime

    from meshterm.ui.timemachine_screen import _fill_days

    # The day keys are local calendar days. Thus the test sets the bounds of the fill with
    # local wall-clock instants (a naive time, which .astimezone() reads as local). The
    # result does not depend on the zone.
    active = [("2026-07-02", 50, 5), ("2026-07-05", 80, 8)]
    now = datetime(2026, 7, 6, 12).astimezone()
    filled = _fill_days(active, datetime(2026, 7, 1).astimezone(), now)
    assert [iso for iso, _p, _n in filled] == [
        "2026-07-02",
        "2026-07-03",
        "2026-07-04",
        "2026-07-05",
        "2026-07-06",
    ]
    counts = {iso: packets for iso, packets, _n in filled}
    assert counts["2026-07-03"] == 0 and counts["2026-07-04"] == 0  # the gap, which is now visible
    assert counts["2026-07-02"] == 50 and counts["2026-07-05"] == 80  # the busy days stay


def test_fill_days_never_invents_days_before_recording_began() -> None:
    """If the floor of a window is before the first recorded day, the result starts at that day.

    It does not start at the floor.
    """
    from datetime import datetime

    from meshterm.ui.timemachine_screen import _fill_days

    active = [("2026-07-10", 10, 1)]
    now = datetime(2026, 7, 12, 12).astimezone()
    filled = _fill_days(active, datetime(2026, 6, 12).astimezone(), now)  # a 30 d floor
    assert filled[0][0] == "2026-07-10"  # not the 2026-06-12 floor
    assert filled[-1][0] == "2026-07-12"  # but it still goes to today


def test_fill_hours_shows_gap_hours_as_zero_bars() -> None:
    """A quiet hour between busy hours is in the result with zero counts. It is not skipped."""
    from datetime import datetime

    from meshterm.ui.timemachine_screen import _fill_hours

    # The hour keys are local wall-clock. Thus the test sets the bounds of the fill with
    # local instants (a naive time, which .astimezone() reads as local). The result does not
    # depend on the zone.
    active = [("2026-07-12T08", 30, 3), ("2026-07-12T11", 20, 2)]
    since = datetime(2026, 7, 12, 6, 30).astimezone()  # the floor is 06:00
    now = datetime(2026, 7, 12, 12, 15).astimezone()  # but the first hour recorded is used
    filled = _fill_hours(active, since, now)
    assert [iso for iso, _p, _n in filled] == [f"2026-07-12T{h:02d}" for h in range(8, 13)]
    counts = {iso: pkts for iso, pkts, _n in filled}
    assert counts["2026-07-12T09"] == 0 and counts["2026-07-12T10"] == 0  # the gap, now visible
    assert counts["2026-07-12T08"] == 30 and counts["2026-07-12T11"] == 20  # busy hours stay


def test_day_centers_land_under_each_bar() -> None:
    """The centre cell of a day is in the span of its own bar, so that a tick points to it."""
    chars = 60
    n = 7
    centers = _day_centers(n, chars)
    assert len(centers) == n
    base = chars / n
    for i, cell in enumerate(centers):
        assert i * base <= cell <= (i + 1) * base  # inside the character span of day i
    assert centers == sorted(centers) and centers[-1] <= chars - 1


def test_day_ticks_shorten_dates_and_align_to_bars() -> None:
    """If the ticks fit, there is one tick for each day: the month is only on the first tick."""
    days = [(f"2026-07-{d:02d}", 10, 3) for d in range(1, 8)]  # Jul 1..7
    chars = 60
    ticks = _day_ticks(days, chars)
    assert [cell for cell, _ in ticks] == _day_centers(len(days), chars)  # under the bars
    labels = [label for _, label in ticks]
    assert labels[0] == "Jul 1"  # the month is printed one time, on the first tick
    assert labels[1:] == ["2", "3", "4", "5", "6", "7"]  # bare day numbers after it


def test_day_ticks_reprint_the_month_on_a_rollover() -> None:
    """The first day of a month always has its month name, so the boundary is clear."""
    days = [(iso, 1, 1) for iso in ("2026-06-29", "2026-06-30", "2026-07-01", "2026-07-02")]
    labels = [label for _, label in _day_ticks(days, 60)]
    assert labels == ["Jun 29", "30", "Jul 1", "2"]


def test_day_ticks_thin_to_fit_keeping_both_ends() -> None:
    """If there are more days than the labels can fit, the code keeps a subset with equal spacing.

    The first and the last tick are in the subset.
    """
    days = [(f"2026-06-{d:02d}", 1, 1) for d in range(1, 31)]  # 30 days
    chars = 40
    ticks = _day_ticks(days, chars)
    centers = _day_centers(len(days), chars)
    assert 2 <= len(ticks) < len(days)  # fewer ticks, but not only two
    cols = [cell for cell, _ in ticks]
    assert cols == sorted(cols) and all(cell in centers for cell in cols)  # under the bars
    assert cols[0] == centers[0] and cols[-1] == centers[-1]  # both ends stay after the thinning


def test_day_ticks_measure_real_label_widths_not_the_longest() -> None:
    """Bare day numbers pack closer than a budget for the longest label allows.

    Thirty day labels of one or two digits leave room for more ticks than a fixed
    reservation of the width of ``"Jun 30"`` (more than six cells for each label). The fit
    uses the true width of each label, so the axis does not have too few ticks.
    """
    days = [(f"2026-06-{d:02d}", 1, 1) for d in range(1, 31)]  # 30 days, mostly 1 or 2 cells
    chars = 40
    ticks = _day_ticks(days, chars)
    assert len(ticks) >= chars // 8 + 2  # more than the approximately 5 for the longest label
    # No two labels overprint: each is at the axis gap from the previous label.
    from meshterm.ui.braillechart import _TICK_GAP

    last_end = -_TICK_GAP
    for cell, label in ticks:
        start = max(0, min(chars - len(label), cell - len(label) // 2))
        assert start >= last_end + _TICK_GAP
        last_end = start + len(label)


def test_hour_ticks_close_on_now_and_read_clock_times() -> None:
    """Hourly ticks have local HH:00 clock times, with fewer ticks if necessary.

    The newest tick is 'now'.
    """
    import re as _re

    hours = [(f"2026-07-12T{h:02d}", 1, 1) for h in range(24)]  # 24 full hours
    chars = 60
    ticks = _hour_ticks(hours, chars)
    centers = _day_centers(len(hours), chars)
    assert ticks[-1] == (centers[-1], "now")  # the newest bar ends with 'now'
    others = [label for _, label in ticks[:-1]]
    assert others and all(_re.fullmatch(r"\d{2}:00", label) for label in others)  # HH:00 times
    cols = [cell for cell, _ in ticks]
    assert cols == sorted(cols) and all(cell in centers for cell in cols)  # aligned under bars


def test_day_columns_widths_differ_by_at_most_one_dot_and_interleave() -> None:
    """An uneven split puts the wider days through the whole chart, not on one side.

    13 days over 30 chars (60 dots) use 12 boundary notches. That leaves 48 dots of bar,
    which cannot divide equally: bars get 3 or 4 dots. The widths must never differ by more
    than one dot column. The wide days must mix with the narrow days. They must not collect
    at either end. (The old split in char units put each wide day on the left and each
    narrow day on the right.)
    """
    cols = _day_columns([1] * 13, 30)
    assert len(cols) == 60
    notches = [i for i, c in enumerate(cols) if c is GAP]  # one for each day boundary
    assert len(notches) == 12
    edges = [-1, *notches, len(cols)]  # each bar is between two notches
    widths = [b - a - 1 for a, b in pairwise(edges)]
    assert len(widths) == 13 and set(widths) == {3, 4}
    first_wide = widths.index(4)
    last_wide = len(widths) - 1 - widths[::-1].index(4)
    assert any(w == 3 for w in widths[first_wide:last_wide])  # narrow days are between wide days


def test_day_columns_notch_the_boundaries_and_keep_the_first_bar_flush() -> None:
    """Notches are only between days. The first bar starts at the axis.

    The braille glyphs already have a left margin. Thus a GAP dot at the start looks like
    double padding at the axis border, but the right edge (the bar is against the closing
    border) has single padding. The edges must be the same.
    """
    cols = _day_columns([2, 8, 4], 12)  # 24 dots − 2 notches = 22 of bar: 7+7+8
    assert cols[0] == 2  # against the axis, with no notch at the start
    assert cols[:7] == [2] * 7
    assert cols[7] is GAP
    assert cols[8:15] == [8] * 7
    assert cols[15] is GAP
    assert cols[16:] == [4] * 8  # and against the right border


def test_day_columns_skips_the_notch_for_a_single_character_day() -> None:
    """A day that is exactly one character wide stays fully lit. There is nothing to space apart."""
    cols = _day_columns([5, 9], 2)  # 2 days, 2 chars -> exactly 1 char (2 dots) each
    assert GAP not in cols
    assert cols == [5, 5, 9, 9]


def test_day_columns_skips_notches_when_days_outnumber_characters() -> None:
    """If there are more days than character columns, each day is less than one character.

    There are no notches.
    """
    cols = _day_columns(list(range(50)), 30)  # 50 days in 30 chars (<=60 dots)
    assert len(cols) == 60
    assert GAP not in cols


def test_day_chart_notch_reaches_the_axis_but_a_zero_day_keeps_its_baseline() -> None:
    """The notch column is blank down to the axis, and a day with value zero stays a grey line.

    The chart has a tall day, an empty day, and a tall day. The boundary notches (dots 7 and
    15) make their whole dot column blank, so that the gap goes to the axis border with no
    break. The very first dot of the first bar is lit (against the axis). The empty day
    draws the faint zero baseline across its own columns.
    """
    from meshterm.ui.braillechart import timeline_rows

    cols = _day_columns([8, 0, 8], 12)  # 22 dots of bar (7+7+8), notches at 7 and 15
    bottom = timeline_rows(cols, rows=2)[-1]
    assert ord(bottom.plain[0]) & 0x40  # the first bar is on the axis: its bottom-left dot is lit
    # Cell 3 has the first boundary: a bar dot on its left, and the blank notch on its
    # right. It has no bottom-right dot, so the gap goes to the axis border below with no
    # break.
    assert not (ord(bottom.plain[3]) & 0x80)
    # The empty middle day (dots 8..14) draws the faint zero baseline, in grey.
    zero_cells = bottom.plain[4:7]
    assert all(ord(c) & (0x40 | 0x80) for c in zero_cells)  # the baseline dots are there
    assert ord(bottom.plain[7]) & 0x40  # the last baseline dot of the day
    assert not (ord(bottom.plain[7]) & 0x80)  # then the second notch, which is blank
    zero_spans = [s for s in bottom.spans if 4 <= s.start < 8]
    assert zero_spans and all(s.style == "faint" for s in zero_spans)


# --- the pages --------------------------------------------------------------------------


def test_node_page_renders_all_sections(tmp_path: Path) -> None:
    """The volume, the SNR band, the rhythm, and the summary of records all render."""
    repo = _seeded_repo(tmp_path)
    ctx = SimpleNamespace(repo=repo)
    body = _plain(_node_sections(ctx, NODE, "Hub", None, 90))
    assert "Volume" in body and "24 receptions" in body
    assert "SNR" in body and "-2.0" in body and "+8.0" in body
    assert "Rhythm" in body and "10-min" in body  # the finest slice that 90 columns allow
    assert "Record" in body and "median" in body
    assert "advert 24" in body  # the kinds, with no packet rows
    repo.close()


def test_node_page_rhythm_spans_the_day_and_shares_the_volume_gutter(tmp_path: Path) -> None:
    """The rhythm of a node is the same as the rhythm of the mesh.

    It is a sweep of the full day, and its gutter is aligned to Volume.
    """
    repo = _seeded_repo(tmp_path)
    ctx = SimpleNamespace(repo=repo)
    lines = _plain(_node_sections(ctx, NODE, "Hub", None, 90), width=90).split("\n")

    def border_col(heading: str) -> int:
        start = next(i for i, line in enumerate(lines) if heading in line)
        border = next(line for line in lines[start:] if "└" in line)
        return border.index("└")

    assert border_col("Volume") == border_col("Rhythm")  # a shared gutter, so the left edges align
    rhythm = next(i for i, line in enumerate(lines) if "Rhythm" in line)
    caption = next(lines[i + 1] for i in range(rhythm, len(lines)) if "└" in lines[i])
    assert "24 h" in caption  # the sweep of the full day ends with 24 h, like the mesh rhythm


def test_time_axis_shortens_to_dates_on_wide_windows_and_times_on_narrow() -> None:
    """A window of many days has marks with bare dates. A window of one day has marks with times."""
    from datetime import datetime, timedelta, timezone

    from meshterm.ui.timemachine_screen import _time_axis

    start = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)
    wide = _time_axis(start, start + timedelta(days=10))
    narrow = _time_axis(start, start + timedelta(hours=12))
    assert wide(1.0) == "now" and narrow(1.0) == "now"
    assert any(ch.isalpha() for ch in wide(0.0))  # a month name, for example "Jul 1"
    assert ":" in narrow(0.0)  # a clock time, for example "08:00"


def test_node_page_empty_window_offers_widening(tmp_path: Path) -> None:
    """A window with nothing recorded says so. The page does not draw empty charts."""
    repo = _seeded_repo(tmp_path)
    ctx = SimpleNamespace(repo=repo)
    body = _plain(_node_sections(ctx, "no" * 6, "ghost", timedelta(days=1), 90))
    assert "Nothing recorded in this window" in body
    repo.close()


def test_mesh_page_renders_days_rhythm_arrivals_and_ledger(tmp_path: Path) -> None:
    """The overview shows the daily charts, the rhythm, the newcomers, and the ledger."""
    repo = _seeded_repo(tmp_path)
    ctx = SimpleNamespace(repo=repo)
    body = _plain(_mesh_sections(ctx, None, 90, prefix_bytes=2))
    assert "Packets per day" in body and "Nodes per day" in body
    assert "Rhythm" in body and "10-min" in body  # the finest slice that 90 columns allow
    assert "Arrivals" in body and "Newcomer" in body
    # Arrivals are aligned lanes. The key is in its own column (with the label KEY, as the
    # lexicon says). The old prefix "first heard" in each row is now only once, in the
    # column header.
    assert "f7" * 6 in body
    assert "KEY" in body and "HASH" not in body
    assert "FIRST HEARD" in body and "first heard" not in body
    assert "Ledger" in body and "26 observations" in body and "2 nodes" in body
    repo.close()


def test_mesh_page_arrivals_key_lane_flexes_with_width(tmp_path: Path) -> None:
    """The key lane of the arrivals changes with the width of the terminal.

    A full key that MeshTerm can resolve shows more than the 12 hex digits that the
    database stores. MeshTerm cuts it at a byte boundary and adds an ellipsis. A narrow
    terminal sets the lane to its old fixed width as the minimum.
    """
    from meshterm.ui.timemachine_screen import _PICK_HASH_W

    repo = _seeded_repo(tmp_path)
    ctx = SimpleNamespace(repo=repo)
    full_key = "f7" * 32

    def resolve_key(node):
        return full_key if node == "f7" * 6 else node

    wide = _plain(_mesh_sections(ctx, None, 100, 1, lambda n: n, resolve_key), width=100)
    arrivals_row = next(ln for ln in wide.split("\n") if "Newcomer" in ln)
    shown = re.search(r"(f7)+…?", arrivals_row).group(0)
    assert len(shown.rstrip("…")) > 12  # more than the 12 hex digits in the database
    assert len(shown.rstrip("…")) % 2 == 0  # cut at a byte boundary
    assert "…" in shown  # a key of 64 hex digits cannot fit whole

    narrow = _plain(_mesh_sections(ctx, None, 60, 1, lambda n: n, resolve_key), width=60)
    narrow_row = next(ln for ln in narrow.split("\n") if "Newcomer" in ln)
    narrow_shown = re.search(r"(f7)+…?", narrow_row).group(0)
    assert len(narrow_shown) < len(shown)  # the lane follows the width of the terminal

    tight = _plain(_mesh_sections(ctx, None, 40, 1, lambda n: n, resolve_key), width=40)
    tight_row = next(ln for ln in tight.split("\n") if "Newcomer" in ln)
    tight_shown = re.search(r"(f7)+…?", tight_row).group(0)
    assert len(tight_shown) <= _PICK_HASH_W  # the minimum is the old fixed lane
    repo.close()


def test_mesh_page_day_chart_axis_matches_the_bar_width(tmp_path: Path) -> None:
    """The bottom border of a day chart has exactly as many columns as its bars.

    A rounding mismatch here (the old ``_day_columns``) made the border and the caption for
    a chart that was wider than the bars that MeshTerm drew. Thus the bars looked shifted to
    the left of an axis that was drawn for more columns than there were. The border now has
    ``┬`` tick marks under the columns with dates, so the test counts the dashes and the
    ticks together as its interior.
    """
    repo = _seeded_repo(tmp_path)
    ctx = SimpleNamespace(repo=repo)
    lines = _plain(_mesh_sections(ctx, None, 90), width=90).split("\n")
    heading_idx = next(i for i, line in enumerate(lines) if "Packets per day" in line)
    border_idx = next(
        i
        for i, line in enumerate(lines[heading_idx:], heading_idx)
        if re.fullmatch(r"\s*└[─┬]+┘", line)
    )
    border_interior = len(re.search(r"└([─┬]+)┘", lines[border_idx]).group(1))
    row_line = lines[border_idx - 1]
    content = re.search(r"[┤│](.*?)[├│]", row_line).group(1)
    assert len(content) == border_interior
    repo.close()


def test_mesh_page_day_axis_ticks_sit_under_dated_columns(tmp_path: Path) -> None:
    """The day chart marks its border with ``┬`` and gives the ticks labels with dates."""
    repo = _seeded_repo(tmp_path)
    ctx = SimpleNamespace(repo=repo)
    lines = _plain(_mesh_sections(ctx, None, 90), width=90).split("\n")
    heading_idx = next(i for i, line in enumerate(lines) if "Packets per day" in line)
    border = next(line for line in lines[heading_idx:] if re.fullmatch(r"\s*└[─┬]+┘", line))
    caption = lines[lines.index(border) + 1]
    tick_cols = [i for i, ch in enumerate(border) if ch == "┬"]
    assert tick_cols, "the day axis should carry tick marks"
    # The seeded history ends today, so the tick of the newest bar is 'today'.
    assert "today" in caption
    # Each tick has a label that is not blank near it (a label is centred on its tick).
    assert any(not caption[max(0, c - 3) : c + 4].isspace() for c in tick_cols)
    repo.close()


def test_mesh_page_24h_window_charts_hours_not_days(tmp_path: Path) -> None:
    """The 24 h window makes buckets for each hour. Its name is '24 h', not '1 day'."""
    repo = _seeded_repo(tmp_path)
    ctx = SimpleNamespace(repo=repo)
    body = _plain(_mesh_sections(ctx, timedelta(days=1), 90))
    assert "Packets per hour" in body and "Nodes per hour" in body
    assert "local hours" in body
    assert "per day" not in body  # no day columns in this window (the rhythm says "time of day")
    repo.close()


def test_mesh_page_rhythm_left_edge_aligns_with_the_day_charts(tmp_path: Path) -> None:
    """The rhythm has the same y-axis gutter as the day charts, so all three left edges are aligned.

    The rhythm keeps its own narrower width (only the gutter is shared). Thus the test
    checks the column of the ``└`` at the start of the border, not the whole width.
    """
    repo = _seeded_repo(tmp_path)
    ctx = SimpleNamespace(repo=repo)
    lines = _plain(_mesh_sections(ctx, None, 90), width=90).split("\n")

    def border_col(heading: str) -> int:
        start = next(i for i, line in enumerate(lines) if heading in line)
        border = next(line for line in lines[start:] if "└" in line)
        return border.index("└")

    assert border_col("Packets per day") == border_col("Rhythm")
    assert border_col("Nodes per day") == border_col("Rhythm")
    repo.close()


def test_picker_row_lanes_align_under_the_header() -> None:
    """The lanes of a picker row are aligned under the header.

    The heard age has the hot colour. The hash prefix is lit in the hue of its key. The
    placeholder for a node with no name stays muted.
    """
    from meshterm.core.models import HeardNode
    from meshterm.ui.contactlist import ContactRow, _header, _lane
    from meshterm.ui.widgets import ContactsSort

    node = HeardNode(
        node="3d" * 6,
        name=None,
        count=42,
        median_snr=None,
        best_snr=None,
        last_rssi=None,
        last_seen=utcnow(),
    )
    row = _lane(
        ContactRow(
            value=node.node,
            name=node.name,
            key=node.node or "",
            last_seen=node.last_seen,
            count=node.count,
        ),
        10,
        2,
        16,
    )
    plain = row.plain
    assert "unknown" in plain and "3d" * 6 in plain and "42" in plain
    # The HEARD age of a node that was just heard is hot (white). The placeholder for a node
    # with no name is muted.
    assert any(span.style == "#ffffff" for span in row.spans)
    unknown_at = plain.index("unknown")
    assert any(span.style == "muted" and span.start <= unknown_at < span.end for span in row.spans)
    # A node with no name is not identified, so its whole key lane is grey. The prefix is
    # never lit (colour marks an identified node, the same as in the path graph).
    from meshterm.ui.theme import node_style

    hash_at = plain.index("3d" * 6)
    assert any(
        span.style == "node.unknown" and span.start == hash_at and span.end == hash_at + 12
        for span in row.spans
    )
    assert not any(span.style == node_style("3d" * 6) for span in row.spans)
    # The header labels are over their lanes (+2 is for the column of the pointer). The lanes
    # are now NAME · HEARD · PKTS · KEY, and the key is at the end of the row.
    header = _header(10, ContactsSort.from_name("heard")).plain
    assert header.index("NAME") == plain.index("unknown") + 2
    assert header.index("KEY") == plain.index("3d" * 6) + 2
    assert header.index("HEARD") < header.index("PKTS") < header.index("KEY")


def test_picker_header_marks_the_active_sort_column() -> None:
    """The sort column has a triangle for the direction. A toggle flips ▲/▼ on this column only."""
    from meshterm.ui.contactlist import _header
    from meshterm.ui.widgets import ContactsSort

    heard_desc = _header(12, ContactsSort("heard", ascending=False)).plain
    assert "HEARD ▼" in heard_desc and "▲" not in heard_desc
    # Ascending flips the glyph of the same column. The other columns do not change.
    assert "HEARD ▲" in _header(12, ContactsSort("heard", ascending=True)).plain
    # A different active column moves the mark. Packets opens descending.
    packets = _header(12, ContactsSort.from_name("packets")).plain
    assert "PKTS ▼" in packets and "HEARD" in packets and "HEARD ▼" not in packets
    # The key column (ring id "hash") is also sortable, so it has the mark when it is active.
    hash_sorted = _header(12, ContactsSort("hash", ascending=True)).plain
    assert "KEY ▲" in hash_sorted and "PKTS ▲" not in hash_sorted


def test_picker_header_highlights_only_the_active_sort_column() -> None:
    """The label and the triangle of the active column are lit cyan. The other lanes stay muted."""
    from meshterm.ui.contactlist import _SORT_ACTIVE, _header
    from meshterm.ui.widgets import ContactsSort

    header = _header(12, ContactsSort.from_name("packets"))
    lit = [header.plain[s.start : s.end] for s in header.spans if s.style == _SORT_ACTIVE]
    # Only the active PKTS lane (label and triangle) has the highlight.
    assert any("PKTS" in seg and "▼" in seg for seg in lit)
    assert not any(any(other in seg for other in ("NAME", "HEARD", "KEY")) for seg in lit)


def test_a_list_draws_exactly_the_lanes_it_declares() -> None:
    """The set of lanes decides everything: the header, the row, and the widths read the same tuple.

    The test checks three sets in one run, because none of them must be a special case. The
    ordinary list, the extra ``TRACED`` of the Trace picker, and the ``NAME · ARCHIVED ·
    KEY`` of the Archived list all come from the same two builders.
    """
    from meshterm.ui.contactlist import (
        ARCHIVED_LANES,
        ARCHIVED_SORT_COLUMNS,
        ARCHIVED_SORT_OPENS_ASCENDING,
        DEFAULT_LANES,
        TRACE_LANES,
        TRACE_SORT_COLUMNS,
        TRACE_SORT_OPENS_ASCENDING,
        ContactRow,
        _header,
        _lane,
    )
    from meshterm.ui.widgets import ContactsSort

    # The ordinary list: no TRACED column, and the lanes are NAME·HEARD·PKTS·KEY.
    plain = _header(10, ContactsSort.from_name("heard"), DEFAULT_LANES).plain
    assert "TRACED" not in plain and "ARCHIVED" not in plain
    assert plain.index("NAME") < plain.index("HEARD") < plain.index("PKTS") < plain.index("KEY")

    # The Trace picker: TRACED is between NAME and HEARD, and it opens as the active sort.
    sort = ContactsSort.from_name("traced", TRACE_SORT_COLUMNS, TRACE_SORT_OPENS_ASCENDING)
    header = _header(10, sort, TRACE_LANES).plain
    assert header.index("NAME") < header.index("TRACED") < header.index("HEARD")
    assert "TRACED ▲" in header  # an age lane opens ascending: the newest first

    # The Archived list: one lane in the middle, and the two lanes that it removes are gone.
    arch_sort = ContactsSort.from_name(
        "archived", ARCHIVED_SORT_COLUMNS, ARCHIVED_SORT_OPENS_ASCENDING
    )
    arch = _header(10, arch_sort, ARCHIVED_LANES).plain
    assert arch.index("NAME") < arch.index("ARCHIVED") < arch.index("KEY")
    assert "HEARD" not in arch and "PKTS" not in arch

    row = ContactRow(
        value="x",
        name="Poly",
        key="3d" * 6,
        last_seen=utcnow() - timedelta(minutes=90),
        last_traced=utcnow() - timedelta(minutes=2),
        archived_at=utcnow() - timedelta(days=3),
    )
    # The traced age ("2m") is left of the heard age ("1h") in the lanes of the picker.
    traced_lane = _lane(row, 10, 1, 12, TRACE_LANES).plain
    assert traced_lane.index("2m") < traced_lane.index("1h")
    # Without the lane, only the heard age shows.
    assert "2m" not in _lane(row, 10, 1, 12, DEFAULT_LANES).plain
    # The Archived row shows its archive age, and it shows neither of the removed lanes.
    archived_lane = _lane(row, 10, 1, 12, ARCHIVED_LANES).plain
    assert "3d" in archived_lane
    assert "1h" not in archived_lane and "2m" not in archived_lane


def test_traced_sort_orders_most_recent_first_never_last() -> None:
    """The traced sort opens with the most recently traced node first.

    Nodes that were never traced are at the bottom.
    """
    from meshterm.ui.contactlist import (
        TRACE_LANES,
        TRACE_SORT_COLUMNS,
        TRACE_SORT_OPENS_ASCENDING,
        ContactRow,
        _ordered,
    )
    from meshterm.ui.widgets import ContactsSort

    now = utcnow()
    rows = [
        ContactRow(value="stale", name="Stale", last_traced=now - timedelta(hours=5)),
        ContactRow(value="never", name="Never", last_traced=None),
        ContactRow(value="fresh", name="Fresh", last_traced=now - timedelta(minutes=1)),
    ]
    sort = ContactsSort.from_name("traced", TRACE_SORT_COLUMNS, TRACE_SORT_OPENS_ASCENDING)
    assert sort.column == "traced" and sort.ascending is True  # an age lane opens ascending
    # Ascending (the default): the newest trace on top, and nodes that were never traced last.
    assert [r.value for r in _ordered(rows, sort, TRACE_LANES)] == ["fresh", "stale", "never"]
    # Descending flips the traced order, but "never" does not go in the fresh block. The
    # metric is an age, so "never" has +inf and stays at the far end in both directions.
    sort.ascending = False
    assert [r.value for r in _ordered(rows, sort, TRACE_LANES)] == ["never", "stale", "fresh"]


def _picker(listed, *, prefix_bytes=0, sort=None, type_of=None, resolve_key=None, width=80):
    """Build a picker over ``listed``, render it, and return it. The rows fit ``width``."""
    from meshterm.ui.contactlist import SORT_COLUMNS, SORT_OPENS_ASCENDING
    from meshterm.ui.timemachine_screen import TimeMachinePickerScreen
    from meshterm.ui.widgets import ContactsSort

    default_sort = ContactsSort.from_name("heard", SORT_COLUMNS, SORT_OPENS_ASCENDING)
    screen = TimeMachinePickerScreen(
        listed=listed,
        prefix_bytes=prefix_bytes,
        sort=sort if sort is not None else default_sort,
        prompt="",
        **({"type_of": type_of} if type_of is not None else {}),
        **({"resolve_key": resolve_key} if resolve_key is not None else {}),
    )
    screen.render_body(width)
    return screen


def _picker_order(screen) -> list:
    """The node ids that the picker lists now, in display order (without the overview rows)."""
    from meshterm.ui.timemachine_screen import MESH, SELF

    return [c.value[0] for c in screen._choices() if c.value not in (MESH, SELF)]


def _heard_nodes() -> list:
    """Three heard nodes with different names, counts, and ages for the sort tests."""
    from meshterm.core.models import HeardNode

    now = utcnow()

    def node(node_id: str, name: str, count: int, mins: int) -> HeardNode:
        return HeardNode(
            node=node_id,
            name=name,
            count=count,
            median_snr=None,
            best_snr=None,
            last_rssi=None,
            last_seen=now - timedelta(minutes=mins),
        )

    # Carla is the newest, Alice is the busiest, and Bob is the oldest. Thus each column has
    # a different order.
    return [
        (node("aa" * 6, "Alice", 90, 30), "Alice"),
        (node("bb" * 6, "Bob", 5, 90), "Bob"),
        (node("cc" * 6, "Carla", 40, 2), "Carla"),
    ]


def test_picker_ctrl_arrows_sort_by_column_and_direction() -> None:
    """Ctrl+←/→ select the column (in its natural direction).

    Ctrl+↑/↓ force ascending or descending.
    """
    listed = _heard_nodes()
    screen = _picker(listed)

    # It opens with the most recently heard first: Carla (2m), Alice (30m), Bob (90m).
    assert _picker_order(screen) == ["cc" * 6, "aa" * 6, "bb" * 6]

    # Ctrl+→ from heard goes to packets, which opens descending: Alice 90, Carla 40, Bob 5.
    screen.handle("ctrl_right")
    screen.render_body(80)
    assert screen._sort.column == "packets" and screen._sort.ascending is False
    assert _picker_order(screen) == ["aa" * 6, "cc" * 6, "bb" * 6]

    # Ctrl+↑ forces ascending on packets: Bob 5, Carla 40, Alice 90.
    screen.handle("ctrl_up")
    screen.render_body(80)
    assert screen._sort.ascending is True
    assert _picker_order(screen) == ["bb" * 6, "cc" * 6, "aa" * 6]

    # Ctrl+← steps back to heard. Ctrl+↓ makes it descending (oldest first): Bob, Alice, Carla.
    screen.handle("ctrl_left")
    screen.handle("ctrl_down")
    screen.render_body(80)
    assert screen._sort.column == "heard" and screen._sort.ascending is False
    assert _picker_order(screen) == ["bb" * 6, "aa" * 6, "cc" * 6]


def test_picker_ctrl_arrows_reach_a_hash_sort() -> None:
    """A fourth column in the ring sorts by node id. Ctrl+↓ flips it f→0."""
    listed = _heard_nodes()  # the ids sort aa < bb < cc, which is different from each other column
    screen = _picker(listed)

    screen.handle("ctrl_right")  # heard -> packets
    screen.handle("ctrl_right")  # packets -> hash, opening ascending (0 → f)
    screen.render_body(80)
    assert screen._sort.column == "hash" and screen._sort.ascending is True
    assert _picker_order(screen) == ["aa" * 6, "bb" * 6, "cc" * 6]

    screen.handle("ctrl_down")  # descending: f → 0
    screen.render_body(80)
    assert _picker_order(screen) == ["cc" * 6, "bb" * 6, "aa" * 6]


def test_picker_glyph_reflects_resolved_node_type() -> None:
    """A node with no stored type uses the type of its contact for the glyph at the start."""
    from meshterm.core.models import NODE_TYPE_REPEATER
    from meshterm.ui.timemachine_screen import MESH, SELF
    from meshterm.ui.widgets import DEFAULT_GLYPH, NODE_GLYPHS

    listed = _heard_nodes()  # the stored node_type of each node is None
    repeater = listed[0][0].node  # the id of Alice, which the resolver knows as a repeater

    screen = _picker(listed, type_of=lambda node: NODE_TYPE_REPEATER if node == repeater else None)
    glyphs = {
        c.value[0]: c.label.plain[0] for c in screen._choices() if c.value not in (MESH, SELF)
    }
    # The repeater that the resolver found has ▲. A node that the resolver cannot place keeps
    # the glyph ● of a plain node.
    assert glyphs[repeater] == NODE_GLYPHS[NODE_TYPE_REPEATER][0]
    assert glyphs[listed[1][0].node] == DEFAULT_GLYPH[0]


def test_picker_stored_node_type_wins_over_the_resolver() -> None:
    """A stored advert type sets the glyph, also if the contact resolver gives another type."""
    from datetime import timedelta

    from meshterm.core.models import NODE_TYPE_REPEATER, NODE_TYPE_SENSOR, HeardNode
    from meshterm.ui.timemachine_screen import MESH, SELF
    from meshterm.ui.widgets import NODE_GLYPHS

    sensor = HeardNode(
        node="ab" * 6,
        name="Probe",
        count=3,
        median_snr=None,
        best_snr=None,
        last_rssi=None,
        last_seen=utcnow() - timedelta(minutes=1),
        node_type=NODE_TYPE_SENSOR,
    )
    screen = _picker([(sensor, "Probe")], type_of=lambda _node: NODE_TYPE_REPEATER)
    glyph = next(c.label.plain[0] for c in screen._choices() if c.value not in (MESH, SELF))
    assert glyph == NODE_GLYPHS[NODE_TYPE_SENSOR][0]


def test_picker_resort_keeps_the_highlight_on_its_node_and_the_filter() -> None:
    """A new sort moves the highlight with the same node, and an active filter stays."""
    listed = _heard_nodes()
    screen = _picker(listed)

    # Highlight Bob (the choices in heard order: mesh, You, Carla, Alice, Bob).
    screen._index = 4
    assert screen._current_choice().value[0] == "bb" * 6

    # Filter to Bob, then sort by name: the highlight and the filter both stay.
    screen._filter = "bob"
    screen.handle("ctrl_left")  # heard -> name
    screen.render_body(80)
    assert screen._sort.column == "name"
    assert screen._filter == "bob"
    assert screen._current_choice().value[0] == "bb" * 6


def test_picker_name_lane_is_content_sized_and_hash_lane_flexes() -> None:
    """The name lane has the size of its content, and the hash lane takes the space that is left.

    The columns are anchored on the left. The name lane has the width of its content and
    does not move when the window gets wider. The extra width goes to the hash lane, so
    that the screen shows more of each key.
    """
    from meshterm.ui.contactlist import _GAP, _LEAD

    screen = _picker(_heard_nodes(), width=72)
    # The fixed lead is the chrome and the width of each declared lane with its gap.
    lead = _LEAD + sum(_GAP + lane.width for lane in screen._lanes)
    # The widest name here is the "unknown" fallback (7 cells), and the lane has this size.
    name_w, hash_w = screen._name_w, screen._hash_w
    assert name_w == len("unknown")
    assert hash_w == 72 - lead - name_w
    # Make the terminal wider: the name lane does not move, and the hash lane takes the extra
    # width.
    screen.render_body(120)
    assert screen._name_w == name_w
    assert screen._hash_w == 120 - lead - name_w
    assert screen._hash_w > hash_w


def test_screen_cycles_windows_and_caches(tmp_path: Path) -> None:
    """`w` moves to the next window (with a new title), and each window renders one time."""
    calls: list = []

    def build(window, width, scope):
        calls.append(window)
        from rich.text import Text

        return [Text("page")]

    screen = TimeMachineScreen(session=_FakeSession(), label="Hub", build=build)
    assert "7 d" in screen.title
    screen.render_body(80)
    screen.render_body(80)
    assert len(calls) == 1  # cached for each window and width
    screen.handle("text", "w")
    assert "30 d" in screen.title
    screen.render_body(80)
    assert len(calls) == 2

    # The F-key chip does the same action under another name. It is the only way for the
    # user to learn that `w` exists on a platform that draws no hint line. The chip names the
    # span to which it goes, because the title already shows where the user is.
    assert screen.picocalc_lyra_lane[2].label == "all"
    screen.handle("window")
    assert "all time" in screen.title
    assert screen.picocalc_lyra_lane[2].label == "24 h"  # the ring goes back to the start


def test_picocalc_window_ring_stops_at_30_days() -> None:
    """The ring on a handheld skips all time: `w` goes 24 h → 7 d → 30 d → 24 h.

    All time is the only span for which the scan grows with the whole history. It is for
    the desktop only (refer to ``_bind_windows``). The chip cycles through the three spans
    that remain.
    """
    from meshterm.platforms import PICOCALC_LYRA, set_platform

    set_platform(PICOCALC_LYRA)
    screen = TimeMachineScreen(
        session=_FakeSession(), label="Hub", build=lambda window, width, scope: []
    )
    assert "7 d" in screen.title  # it opens on 7 d, as on each platform
    screen.handle("text", "w")
    assert "30 d" in screen.title
    assert screen.picocalc_lyra_lane[2].label == "24 h"  # never "all" on this ring
    screen.handle("window")
    assert "24 h" in screen.title  # it went past the place of all time


# --- the page for our node --------------------------------------------------------------


def _activity_repo(tmp_path: Path) -> Repository:
    """A repository with the outbound activity of our own node.

    It has traces (with a route, with a timeout, and one path walk), channel messages and
    direct messages (some with an ack, and one inbound), and a sample of the tx power.
    """
    from meshterm.core.models import (
        PATH_TRACE_TARGET,
        ChatMessage,
        Hop,
        TraceResult,
    )

    repo = Repository(tmp_path / "self.db")
    now = utcnow()
    trun = repo.start_run("trace", {}, None)

    def trace(target: str, ok: bool, snrs: list, mins: int) -> TraceResult:
        return TraceResult(
            target=target,
            success=ok,
            hops=[Hop(index=i, node=None, snr=s) for i, s in enumerate(snrs)],
            round_trip_ms=120.0 if ok else None,
            tx_power=20,
            path_hash_bytes=1,
            timestamp=now - timedelta(minutes=mins),
        )

    repo.record_trace(trun, trace("Hub-A", True, [5.0, 8.0], 40))  # min 5.0, 2 hops
    repo.record_trace(trun, trace("Hub-A", True, [2.0], 30))  # min 2.0, 1 hop
    repo.record_trace(trun, trace("Hub-B", True, [-3.0, 1.0, 4.0], 20))  # min -3.0, 3 hops
    repo.record_trace(trun, trace("Hub-B", False, [], 15))  # timed out, no SNR
    repo.record_trace(trun, trace(PATH_TRACE_TARGET, True, [6.0], 10))  # a path walk: no target

    crun = repo.start_run("chat", {}, None)

    def msg(outbound, is_channel, peer, acked, mins, channel_id=None):
        return ChatMessage(
            text="hi",
            outbound=outbound,
            is_channel=is_channel,
            channel_id=channel_id,
            channel_idx=0 if is_channel else None,
            peer=peer,
            peer_name=peer,
            snr=None,
            acked=acked,
            created_at=now - timedelta(minutes=mins),
        )

    repo.record_chat_message(msg(True, True, None, None, 35, channel_id="public"), run_id=crun)
    repo.record_chat_message(msg(True, True, None, None, 25, channel_id="public"), run_id=crun)
    repo.record_chat_message(msg(True, False, "aa" * 6, True, 22))  # dm sent, acked
    repo.record_chat_message(msg(True, False, "bb" * 6, False, 18))  # dm sent, not acked
    repo.record_chat_message(msg(True, False, "aa" * 6, None, 12))  # dm sent, ack pending
    repo.record_chat_message(msg(False, False, "aa" * 6, None, 8), run_id=crun)  # inbound: left out

    repo._conn.execute(
        "INSERT INTO tx_samples "
        "(run_id, target, tx_power, median_min_snr, success_rate, samples, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (trun, "Hub-A", 20, 3.0, 0.8, 5, (now - timedelta(minutes=20)).isoformat()),
    )
    repo._conn.commit()
    return repo


def test_self_transmissions_unions_traces_and_outbound_messages(tmp_path: Path) -> None:
    """The timeline of our node is each trace and each outbound message, oldest first.

    The inbound messages are not in it.
    """
    repo = _activity_repo(tmp_path)
    stamps = repo.self_transmissions()
    assert len(stamps) == 5 + 5  # 5 traces and 5 outbound messages (the one inbound is not in it)
    assert stamps == sorted(stamps)  # oldest first
    window = repo.self_transmissions(since=utcnow() - timedelta(minutes=21))
    assert 0 < len(window) < len(stamps)  # only the recent tail
    repo.close()


def test_self_trace_reach_carries_outcomes(tmp_path: Path) -> None:
    """Reach rows have the oldest first: ``(when, success, min_snr, hop_count)``.

    A timeout has no SNR.
    """
    repo = _activity_repo(tmp_path)
    reach = repo.self_trace_reach()
    assert len(reach) == 5
    assert [r[0] for r in reach] == sorted(r[0] for r in reach)
    homed = [r for r in reach if r[1]]
    timed_out = [r for r in reach if not r[1]]
    assert len(homed) == 4 and len(timed_out) == 1
    assert timed_out[0][2] is None  # nothing came back to measure
    assert min(r[2] for r in homed) == -3.0  # the bottleneck of the Hub-B path
    repo.close()


def test_self_activity_ledger_tallies(tmp_path: Path) -> None:
    """The ledger counts traces, different targets (not the path walk), messages, acks, and tx."""
    repo = _activity_repo(tmp_path)
    led = repo.self_activity_ledger()
    assert led.trace_total == 5 and led.trace_ok == 4
    assert led.trace_targets == 2  # Hub-A and Hub-B. The (path) walk has no target
    assert led.msg_channel == 2 and led.msg_dm == 3
    assert led.dm_acked == 1 and led.dm_ackable == 2  # 2 dm sends have a result, and 1 has an ack
    assert led.dm_peers == 2  # aa and bb
    assert led.tx_samples == 1
    # The window filter makes each table smaller in the same way.
    recent = repo.self_activity_ledger(since=utcnow() - timedelta(minutes=16))
    assert recent.trace_total < led.trace_total
    repo.close()


def test_self_sections_render_the_page_and_empty_window(tmp_path: Path) -> None:
    """The page for our node shows Activity, Reach, Rhythm, and Ledger.

    An empty window tells the user to make the window wider.
    """
    repo = _activity_repo(tmp_path)
    ctx = SimpleNamespace(repo=repo)
    text = _plain(_self_sections(ctx, timedelta(days=1), 72), 72)
    assert "Activity" in text and "Reach" in text
    assert "Rhythm" in text and "Ledger" in text
    assert "4 of 5 traces home" in text
    assert "2 channel · 3 direct" in text
    # A window that is shorter than the age of the most recent transmission is empty, and the
    # page says so.
    empty = _plain(_self_sections(ctx, timedelta(minutes=1), 72), 72)
    assert "Nothing sent in this window." in empty
    repo.close()


def test_self_row_leads_the_node_list_as_a_lane() -> None:
    """The lane of our node shows our name and (you). The heard and pkts lanes are faint.

    The lane also shows the hash of our key.
    """
    from meshterm.ui.contactlist import ContactRow, _lane
    from meshterm.ui.timemachine_screen import SELF

    named = _lane(
        ContactRow(value=SELF, name="Homestead", key="3d" * 32, you=True),
        name_w=30,
        prefix_bytes=1,
        hash_w=30,
    ).plain
    assert named.startswith("★")
    assert "Homestead" in named and "(you)" in named
    assert "—" in named  # heard and packets have nothing to show for us
    assert "3d3d" in named  # the hash of our key, with its routing prefix lit
    # If no device can be reached: a bare "you" name and a "?" hash, with no "(you)" tag.
    anon = _lane(
        ContactRow(value=SELF, name=None, key="", you=True),
        name_w=30,
        prefix_bytes=0,
        hash_w=30,
    ).plain
    assert "you" in anon and "(you)" not in anon
    assert anon.rstrip().endswith("?")


def test_picker_lists_you_first_in_the_node_block() -> None:
    """Our node is first in the node list (after the mesh row) with any sort."""
    from meshterm.ui.timemachine_screen import MESH, SELF

    screen = _picker(_heard_nodes(), width=72)
    choices = screen._choices()
    # The mesh overview is first. Our node is the first node, before each heard node.
    assert choices[0].value == MESH
    assert choices[1].value == SELF
    # A new sort changes the order of only the heard block, and our node stays first in it.
    screen.handle("ctrl_right")
    screen.render_body(72)
    choices = screen._choices()
    assert choices[0].value == MESH and choices[1].value == SELF


def test_picker_hash_lane_shows_a_heard_nodes_full_key_when_a_contact_holds_it() -> None:
    """A heard node shows its full key when a contact has it.

    The database stores a node as a prefix of 12 hex digits. On a wide screen, the lane
    shows more than those twelve digits, and the stored id still selects the node.
    """
    from meshterm.core.models import HeardNode

    stored = "3d63c6429436"  # the prefix of 12 hex digits that the observations keep
    full = stored + "ab" * 26  # the whole public key that a contact has
    node = HeardNode(
        node=stored,
        name="Rep",
        count=7,
        median_snr=None,
        best_snr=None,
        last_rssi=None,
        last_seen=utcnow(),
    )
    screen = _picker([(node, "Rep")], resolve_key=lambda n: full if n == stored else n, width=100)
    # The heard row (after the mesh, header, and self rows) shows much more than the stored
    # prefix.
    row = next(c for c in screen._choices() if c.value == (stored, "Rep"))
    assert stored + "ab" * 10 in row.label.plain  # 32 hex digits, much more than the stored 12
    # Its value still has the stored id of 12 hex digits, so the page query that it opens
    # does not change.
    assert row.value == (stored, "Rep")


def test_picker_hash_lane_shows_a_captured_full_key_without_a_device() -> None:
    """A full key that was captured with the observation shows offline.

    No contact resolver is necessary.
    """
    from meshterm.core.models import HeardNode

    stored = "3d63c6429436"
    full = stored + "cd" * 26
    node = HeardNode(
        node=stored,
        name="Rep",
        count=7,
        median_snr=None,
        best_snr=None,
        last_rssi=None,
        last_seen=utcnow(),
        public_key=full,
    )
    # No resolve_key is passed: only the stored public_key sets the hash lane.
    screen = _picker([(node, "Rep")], width=100)
    row = next(c for c in screen._choices() if c.value == (stored, "Rep"))
    assert stored + "cd" * 10 in row.label.plain


# -- the scope cycle ----------------------------------------------------------------------


def _flood_repo(tmp_path: Path) -> Repository:
    """Stored floods with three scopes, and a direct packet that has no scope.

    The repository has two ``yul`` floods, one unscoped flood, one flood with a scope for a
    region that nobody here knows (``qc``, which is not in the names of :func:`_scope_ctx`),
    and a direct message. Each is a real packet. The repository must keep the code of its
    scope through storage, so that the region can be named again.
    """
    from tests.test_scope_display import _DIRECT, _FLOOD, _scoped

    repo = Repository(tmp_path / "floods.db")
    run = repo.start_run("monitor", {}, None)
    now = utcnow()
    for hours_ago, raw in (
        (30, _scoped("yul")),
        (5, _scoped("yul")),
        (4, _FLOOD),
        (3, _scoped("qc")),
        (2, _DIRECT),
    ):
        repo.record_observation(
            run,
            Observation(
                node=None,
                kind="packet",
                path="",
                observed_at=now - timedelta(hours=hours_ago),
                raw=dict(raw),
            ),
        )
    return repo


def _scope_ctx(repo: Repository) -> SimpleNamespace:
    from tests.test_scope_display import _knows

    return SimpleNamespace(repo=repo, region_store=SimpleNamespace(scope_of=_knows("yul", "mtl")))


def test_flood_frames_come_back_with_what_their_scope_is_read_from(tmp_path: Path) -> None:
    """The result has only floods, oldest first.

    Each flood can still resolve to its region after storage.
    """
    from tests.test_scope_display import _knows

    repo = _flood_repo(tmp_path)
    frames = repo.flood_frames()
    assert [raw["route_typename"] for _when, raw in frames] == [
        "TC_FLOOD",
        "TC_FLOOD",
        "FLOOD",
        "TC_FLOOD",
    ]
    assert _knows("yul")(frames[0][1]).region == "yul"
    assert len(repo.flood_frames(since=utcnow() - timedelta(hours=24))) == 3
    repo.close()


def test_the_mesh_page_offers_each_scope_its_window_holds(tmp_path: Path) -> None:
    """The ring is the ring of the window.

    A 24 h window has one yul flood, not the one that is 30 h old.
    """
    from meshterm.ui.timemachine_screen import _MeshFloods

    floods = _MeshFloods(_scope_ctx(_flood_repo(tmp_path)))
    assert floods.keys(timedelta(days=1)) == {("unscoped",), ("region", "yul"), ("unknown",)}
    assert len(floods.stamps(timedelta(days=7), ("region", "yul"))) == 2
    assert len(floods.stamps(timedelta(days=1), ("region", "yul"))) == 1


def test_s_cycles_the_mesh_pages_scope_and_retitles_it(tmp_path: Path) -> None:
    """The scope cycle is all → unscoped → yul → unknown → all.

    Each page renders for its own scope.
    """
    from meshterm.ui.timemachine_screen import _MeshFloods

    floods = _MeshFloods(_scope_ctx(_flood_repo(tmp_path)))
    built: list = []

    def build(window, width, scope):  # noqa: ANN001, ANN202
        built.append(scope)
        return [Text("page")]

    screen = TimeMachineScreen(
        session=_FakeSession(), label="the whole mesh", build=build, scopes=floods.keys
    )
    assert "s scope" in screen.footer_hint
    titles = []
    for _ in range(4):
        screen.handle("text", "s")
        screen.render_body(80)
        titles.append(screen.title.removeprefix("the whole mesh · 7 d"))
    assert titles == [" · unscoped", " · scope yul", " · unknown scope", ""]
    assert built == [("unscoped",), ("region", "yul"), ("unknown",), None]
    screen.handle("scope")  # the action of the F2 chip is the same cycle
    assert screen.short_title == "the whole mesh · 7 d · unscoped"


def test_the_scope_chip_is_the_word_alone_in_all_six_cells() -> None:
    """The chip has no ``▸`` at the start: the page that a key press goes to gets all six cells."""
    from rich.cells import cell_len

    from meshterm.ui.scopering import scope_chip

    assert scope_chip(None) == "all"
    assert scope_chip(("region", "yul")) == "yul"
    assert scope_chip(("region", "harbor")) == "harbor"  # whole, where it was "▸ har…"
    for key in (("unscoped",), ("unknown",), ("region", "lakeshore")):
        chip = scope_chip(key)
        assert cell_len(chip) == 6 and chip.endswith("…") and "▸" not in chip


def test_the_rhythm_heading_names_its_slices_only_where_the_line_has_room() -> None:
    """The heading names its slices only if the line has room.

    On 53 columns, the slice width goes. The heading does not wrap to a second row.
    """
    from meshterm.ui.timemachine_screen import _rhythm_heading

    what = "receptions by local time of day"
    assert _rhythm_heading(what, 20, 72).plain == f"Rhythm  ·  {what} · 20-min slices"
    assert _rhythm_heading(what, 60, 72).plain.endswith("· 1 h slices")
    assert _rhythm_heading(what, 20, 53).plain == f"Rhythm  ·  {what}"


def test_a_page_with_no_scope_offers_no_cycle() -> None:
    """The page of a node is its adverts, which have no scope: no hint, no chip, no cycle."""
    screen = TimeMachineScreen(
        session=_FakeSession(), label="Hub", build=lambda window, width, scope: []
    )
    assert "scope" not in screen.footer_hint
    screen.handle("text", "s")
    assert screen.title == "Hub · 7 d"


def test_a_narrowed_mesh_page_charts_the_scopes_floods_and_drops_the_node_counts(
    tmp_path: Path,
) -> None:
    """A mesh page for one scope shows the floods per day, their rhythm, and a ledger of the scope.

    It has no node counts and no arrivals.
    """
    from meshterm.ui.timemachine_screen import _mesh_scope_sections, _MeshFloods

    floods = _MeshFloods(_scope_ctx(_flood_repo(tmp_path)))
    week = timedelta(days=7)
    text = _plain(_mesh_scope_sections(floods.stamps(week, ("region", "yul")), week, 80), 80)
    assert "Floods per day" in text and "Rhythm" in text and "2 floods" in text
    assert "Nodes per day" not in text and "Arrivals" not in text
    day = _plain(
        _mesh_scope_sections(
            floods.stamps(timedelta(days=1), ("unscoped",)), timedelta(days=1), 80
        ),
        80,
    )
    assert "Floods per hour" in day and "1 flood " in day
    empty = _plain(_mesh_scope_sections([], week, 80), 80)
    assert "Nothing recorded in this scope" in empty
