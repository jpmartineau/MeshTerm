# SPDX-License-Identifier: Apache-2.0
"""Braille chart widget tests: orientation, the zero baseline, scaling, and styles.

The module takes Text in and gives Text out. Thus each test checks a behaviour directly on
the rendered braille bitmasks.
"""

from __future__ import annotations

from meshterm.ui.braillechart import (
    GAP,
    activity_peak,
    activity_sparkline,
    axis_chart,
    chart_span,
    timeline_rows,
    y_axis_labels,
)

#: Dot bits for a column that is filled from the bottom to a height of 0–4 (left and right
#: column).
_L = (0x00, 0x40, 0x44, 0x46, 0x47)
_R = (0x00, 0x80, 0xA0, 0xB0, 0xB8)
_BASE = chr(0x2800 | 0x40 | 0x80)  # the flat zero line, with two dots at the bottom
_BLANK = chr(0x2800)


def _ch(*bits: int) -> str:
    mask = 0
    for b in bits:
        mask |= b
    return chr(0x2800 | mask)


# --- chart_span ---------------------------------------------------------------------


def test_chart_span_always_folds_zero_in() -> None:
    """Positive, negative, and mixed series all keep zero in the span."""
    assert chart_span([3, 7]) == (0.0, 7.0)
    assert chart_span([-3, -7]) == (-7.0, 0.0)
    assert chart_span([-3, 7]) == (-3.0, 7.0)
    assert chart_span([None, None]) == (0.0, 0.0)
    assert chart_span([1], span=(-5, 10)) == (-5.0, 10.0)


# --- timeline_rows: all-positive series ------------------------------------------------


def test_timeline_peak_fills_the_height() -> None:
    """The peak of the series fills each dot row. The readings make pairs of two in each cell."""
    rows = timeline_rows([12, 12, 0, 0], rows=3)
    assert len(rows) == 3
    assert all(len(r.plain) == 2 for r in rows)
    assert rows[0].plain[0] == _ch(_L[4], _R[4])  # the peak pair reaches the top
    assert rows[0].plain[1] == _BLANK
    assert rows[2].plain[1] == _BASE  # the silent pair keeps the zero line


def test_timeline_each_dot_column_has_its_own_height() -> None:
    """Two readings in one cell rise independently. This is the full resolution of braille."""
    rows = timeline_rows([12, 6], rows=3)
    assert rows[0].plain[0] == _ch(_L[4])
    assert rows[1].plain[0] == _ch(_L[4], _R[2])
    assert rows[2].plain[0] == _ch(_L[4], _R[4])


def test_timeline_never_hides_a_lone_packet() -> None:
    """A small value that is not zero still lights a dot, in the lit style and not the floor's."""
    rows = timeline_rows([1, 0, 1000, 1000], rows=3)
    bottom = rows[2]
    assert bottom.plain[0] == _ch(_L[1], _R[1])  # the bar dot and the baseline that continues
    assert bottom.spans[0].style == "ok"


def test_timeline_all_silent_is_a_flatline() -> None:
    """With nothing to draw, the chart is a faint floor line and not an empty space."""
    rows = timeline_rows([0, 0, 0, 0, 0, 0], rows=2)
    assert rows[1].plain == _BASE * 3
    assert rows[0].plain == _BLANK * 3
    assert all(span.style == "faint" for span in rows[1].spans)


def test_timeline_pads_an_odd_tail_column() -> None:
    """An odd number of readings still renders whole cells, and the floor continues under it."""
    rows = timeline_rows([4], rows=1)
    assert len(rows[0].plain) == 1
    assert rows[0].plain[0] == _ch(_L[4], _R[1])  # the full bar and the floor of the padded column


# --- timeline_rows: GAP (a hard break in the baseline) --------------------------------


def test_timeline_gap_breaks_the_baseline_but_zero_keeps_it() -> None:
    """A GAP column draws nothing, not even the zero line. A 0 keeps its dot.

    The cell pairs a GAP (left) with a plain 0 (right). The left column is blank down to
    the floor. The right column still shows its faint baseline dot. This difference lets
    the notch of a day boundary reach the axis, while an empty day stays a grey flat line.
    """
    rows = timeline_rows([GAP, 0], rows=1)
    assert rows[0].plain[0] == _ch(_R[1])  # only the baseline dot of the right (0) column
    assert rows[0].spans and rows[0].spans[0].style == "faint"


def test_timeline_gap_column_stays_blank_beside_a_full_bar() -> None:
    """A GAP next to a tall bar keeps its own column blank down to the axis."""
    rows = timeline_rows([GAP, 4], rows=1)  # left GAP, right bar of full height
    # The right column has the whole bar. The left (GAP) column lights no dot at all, not
    # even the bottom baseline dot that the cell of the bar would otherwise share.
    assert rows[0].plain[0] == _ch(_R[4])
    assert not (ord(rows[0].plain[0]) & 0x40)  # no bottom-left dot


def test_chart_span_ignores_gap_columns() -> None:
    """GAP has no magnitude, so it never makes the span wider."""
    assert chart_span([GAP, 3, 7]) == (0.0, 7.0)
    assert chart_span([GAP, GAP]) == (0.0, 0.0)


# --- timeline_rows: negative and mixed series ------------------------------------------


def test_timeline_negative_series_hangs_from_a_top_baseline() -> None:
    """Data that is all negative puts zero at the ceiling, and the bars grow downward."""
    rows = timeline_rows([-4, -2], rows=1)
    # The left column hangs the full height (0..3). The right column hangs two dots (2..3).
    # The baseline row (the top dots of the cell) is inside both bars.
    assert rows[0].plain[0] == _ch(0x40, 0x04, 0x02, 0x01, 0x10, 0x08)


def test_timeline_mixed_series_puts_zero_mid_chart() -> None:
    """With readings on both sides of zero, positive bars rise and negative bars hang."""
    rows = timeline_rows([-10, 5], rows=3, span=(-10, 10))
    # The baseline is on dot row 6. The full -10 hangs 0..6, and the +5 rises 6..8.
    assert rows[0].plain[0] == _ch(0x80)  # the top of the rise, above the baseline
    assert rows[1].plain[0] == _ch(0x46, 0x18)  # the hang and the rise cross the zero row
    assert rows[2].plain[0] == _ch(0x47)  # the end of the hang, below all other dots


def test_timeline_baseline_shows_mid_chart_in_silent_cells() -> None:
    """A silent cell of a mixed-span chart draws the zero line where zero is."""
    rows = timeline_rows([-10, 5, None, None], rows=3, span=(-10, 10))
    assert rows[1].plain[1] == _ch(0x02, 0x10)  # the dot of each column on row 6, faint
    assert rows[1].spans[-1].style == "faint"
    assert rows[0].plain[1] == _BLANK
    assert rows[2].plain[1] == _BLANK


# --- styles -----------------------------------------------------------------------------


def test_timeline_per_cell_style_callable_sees_the_cells_readings() -> None:
    """A callable style gets the readings that are present in each lit cell."""
    seen: list[list[float]] = []

    def style(values: list[float]) -> str:
        seen.append(values)
        return "warn"

    rows = timeline_rows([2, 4, None, 6], rows=1, style=style)
    assert seen == [[2, 4], [6]]
    assert all(span.style == "warn" for span in rows[0].spans)


# --- activity_sparkline -------------------------------------------------------------------


def test_sparkline_puts_now_on_the_right() -> None:
    """A histogram with the newest bucket first renders with the current bucket at the right."""
    # The newest bucket has all the traffic, so it is the self-scaled peak and fills the
    # column. Only the right dot column of the last cell lights. The floor dot keeps the
    # baseline of the left column.
    text = activity_sparkline((21, 0, 0, 0), 4)
    assert text.plain == _BASE + _ch(_R[4], 0x40)


def test_sparkline_scales_to_the_window_peak() -> None:
    """With no shared peak, the busiest bucket fills the column and the others scale to it."""
    # With the newest first, (16, 12, 8, 4) is a ramp of full, ¾, ½, and ¼ against its own
    # peak of 16. When the chart reverses it for display, it climbs from 1 to 4 dots, from
    # left to right toward now.
    text = activity_sparkline((16, 12, 8, 4), 4)
    assert text.plain == _ch(_L[1], _R[2]) + _ch(_L[3], _R[4])


def test_sparkline_shares_a_peak_across_instances() -> None:
    """A peak that the caller passes scales the bars to one ceiling, so quiet windows read short.

    The same bucket draws a taller bar when it is the peak of its window than when the
    scale includes the peak of a louder sibling. This is the shared-scale column of the
    channel manager.
    """
    solo = activity_sparkline((8, 0), 2)  # self-scaled: 8 is its own peak, so full height
    shared = activity_sparkline((8, 0), 2, peak=32)  # 8 against 32 is a quarter, one dot
    assert solo.plain == _ch(0x40, _R[4])  # right column full (4 dots), left on baseline
    assert shared.plain == _ch(0x40, _R[1])  # right column with one dot


def test_sparkline_pads_and_crops_to_the_window() -> None:
    """A short histogram gets padding of silence. A long one is cropped to the newest buckets."""
    padded = activity_sparkline((21,), 6)
    assert padded.plain == _BASE * 2 + _ch(_R[4], 0x40)
    cropped = activity_sparkline((0, 0, 21, 21, 9, 9), 2)
    assert cropped.plain == _BASE  # only the two newest (silent) buckets remain


# --- activity_peak --------------------------------------------------------------------


def test_activity_peak_reads_a_percentile_not_the_max() -> None:
    """One bucket that is extremely busy goes above the ceiling and does not set it."""
    # Ten steady buckets of 10 and one spike of 1000: the ceiling is the 90th percentile
    # of the counts. It is the steady level, not the spike.
    assert activity_peak((1000,) + (10,) * 10, percentile=90) == 10.0


def test_activity_peak_ignores_bucket_age() -> None:
    """A burst has the same weight at any age: recency does not change the ceiling."""
    # We removed the magnitude-decay recency term, because it reduced the ceiling to the
    # floor over a deep pool. Thus a burst of 20 sets the same ceiling now or three buckets ago.
    assert activity_peak((20, 0, 0)) == activity_peak((0, 0, 20)) == 20.0


def test_activity_peak_floors_a_quiet_window() -> None:
    """The floor keeps the ceiling up, so one packet in a lull is a small nub, not a column."""
    assert activity_peak((0, 0, 0), floor=3.0) == 3.0  # silent: the floor
    lone = activity_peak((1,), floor=3.0)
    assert lone == 3.0  # a single packet scales against the floor, not against itself


def test_activity_peak_pools_several_histograms_into_one_ceiling() -> None:
    """Pooled channels share a scale: a quiet channel reads short next to a busy one."""
    pooled = activity_peak((10,) * 10, (1,), percentile=90)
    assert pooled == 10.0  # the level of the busy channel sets the shared ceiling…
    solo = activity_peak((1,), percentile=90)
    assert solo == 1.0  # …but the quiet channel alone would set only 1


# --- y_axis_labels / axis_chart -------------------------------------------------------


def test_y_axis_labels_blanks_a_silent_chart() -> None:
    """A zero peak draws no marks."""
    assert y_axis_labels(0, 3) == ["", "", ""]


def test_y_axis_labels_marks_quote_the_ticked_dot() -> None:
    """Each mark shows the value at the third dot of its row, where the ┤ tick points.

    Example: peak 10 over 2 rows (8 dots). The tick of the top row crosses dot 6 (a 7-dot
    bar, 10·7/8 → 9). The tick of the bottom row crosses dot 2 (a 3-dot bar, 10·3/8 → 4).
    The marks do not show the values at the top edges of the rows (10 and 5), because those
    values are one dot above the place where the glyph points.
    """
    assert y_axis_labels(10, 2) == ["9", "4"]


def test_y_axis_labels_blanks_a_repeated_mark() -> None:
    """A low peak rounds a lower row to the same value as the row above it. The mark is blank."""
    assert y_axis_labels(1, 3) == ["1", "", ""]


def test_axis_chart_mirrors_marks_where_the_platform_affords_them() -> None:
    """An axis chart mirrors its marks only where the platform has room for them.

    The desktop frames a chart with marks in both gutters. The console keeps the right
    ``├`` tick, but it uses the cells of the mirrored label for the chart.
    """
    from meshterm.platforms import PICOCALC_LYRA, set_platform
    from meshterm.ui.braillechart import axis_chrome

    rows = timeline_rows([9] * 8, rows=2)
    out = axis_chart(rows, 9, 4, lambda f: "now" if f >= 1.0 else "old")
    assert len(out) == 4  # 2 chart rows + bottom border + caption
    assert out[0].plain == "  8 ┤⣿⣿⣿⣿├ 8"  # the values of the ticked dots, mirrored on the desktop
    assert out[1].plain == "  3 ┤⣿⣿⣿⣿├ 3"  # (in the minimum gutter of 3 cells)
    assert out[2].plain.startswith("    └") and out[2].plain.endswith("┘")
    assert axis_chrome(1) == 2 * (1 + 2)  # the budget that a caller must keep beside the cells

    set_platform(PICOCALC_LYRA)
    tight = axis_chart(timeline_rows([9] * 8, rows=2), 9, 4, lambda f: "x")
    assert tight[0].plain == "  8 ┤⣿⣿⣿⣿├"  # the tick stays, but the cells of the label do not
    assert tight[1].plain == "  3 ┤⣿⣿⣿⣿├"
    assert axis_chrome(1) == 1 + 3


def test_axis_chart_compacts_deep_tallies_into_the_gutter() -> None:
    """Counts above 999 read as k and M marks. The gutter has the width of the widest mark.

    A peak of 1.5k prints ``1k``, but a lower tick is still three digits wide (``562``).
    Thus the gutter takes three cells. The width comes from the marks and not from the bare
    peak.
    """
    from meshterm.ui.braillechart import axis_label_w, compact_label

    assert compact_label(999) == "999" and compact_label(1500) == "2k"
    assert compact_label(12_345) == "12k" and compact_label(2_000_000) == "2M"
    assert compact_label(-1200) == "-1k"

    rows = timeline_rows([1500] * 8, rows=2)
    out = axis_chart(rows, 1500, 4, lambda f: "x")
    assert axis_label_w(1500, 2) == 3
    assert out[0].plain == " 1k ┤⣿⣿⣿⣿├ 1k"  # round(1500 · 7/8) → 1312 → 1k
    assert out[1].plain == "562 ┤⣿⣿⣿⣿├ 562"  # round(1500 · 3/8) → the tick with three digits


def test_y_axis_labels_signed_span_quotes_both_extremes() -> None:
    """The marks of a signed chart show the heights of rises and the depths of hangs."""
    # rows=3 over +8..-4 (baseline on dot 4): the top tick reads a rise of 7 dots (7), the
    # middle tick a rise of 3 dots (3), and the bottom tick a hang of 3 dots below zero (−2).
    assert y_axis_labels(8, 3, lo=-4) == ["7", "3", "-2"]
    # A symmetric span puts the middle tick on the baseline itself: a blank zero.
    assert y_axis_labels(6, 3, lo=-6) == ["5", "", "-4"]


def test_axis_chart_floor_frames_a_signed_chart_and_widens_the_gutter() -> None:
    """A signed floor draws negative marks and makes the gutter wide enough for the widest one.

    ``-107`` takes four cells. Each gutter has three cells in any case, so here the floor
    makes the gutter wider.
    """
    rows = timeline_rows([150, -150], rows=3, span=(-150, 150))
    out = axis_chart(rows, 150, 1, lambda f: "x", floor=-150)
    assert out[0].plain.startswith(" 125 ┤")  # the floor makes the gutter four cells wide
    assert out[2].plain.startswith("-107 ┤")  # a negative mark near the floor


def test_every_gutter_is_at_least_three_cells() -> None:
    """The one-digit marks of a low peak still get three cells, so the chart never slides.

    The gutter once had the width of the widest mark only. It grew while the user watched
    it, when a busy minute took the peak from ``9`` to ``10`` to ``100`` (JP, 2026-10-04).
    """
    from meshterm.ui.braillechart import AXIS_LABEL_MIN, axis_label_w

    assert AXIS_LABEL_MIN == 3
    assert axis_label_w(2, 3) == axis_label_w(90, 3) == axis_label_w(900, 3) == 3
    assert axis_label_w(9000, 3) == 3  # compacted to "8k", still in the three cells
    rows = timeline_rows([2] * 8, rows=2)
    assert axis_chart(rows, 2, 4, lambda f: "x")[0].plain.startswith("  2 ┤")


def test_axis_chart_honours_a_shared_label_width() -> None:
    """An explicit label_w makes the gutter wider than the digit count of the peak.

    Thus several stacked charts (the pair for each day on the Time Machine) can share one
    gutter width that comes from the wider chart, and their marks align column for column.
    """
    rows = timeline_rows([9] * 8, rows=1)
    out = axis_chart(rows, 9, 4, lambda f: "x", label_w=3)
    assert out[0].plain.startswith("  7 ┤")


# --- axis_chart column ticks ----------------------------------------------------------


def test_axis_chart_ticks_notch_the_border_and_centre_labels() -> None:
    """Explicit column ticks draw a ``┬`` on the border, with the label centred under it."""
    rows = timeline_rows([9] * 8, rows=1)
    out = axis_chart(rows, 9, 4, ticks=[(0, "A"), (3, "D")])
    assert out[1].plain == "    └┬──┬┘"  # ticks at chart cells 0 and 3
    assert out[2].plain == "     A  D"  # the labels are centred under their ticks


def test_axis_chart_continuous_axis_notches_ticks_under_its_labels() -> None:
    """A continuous (label_at) chart puts ┬ ticks under its labels at the ends and the quarters."""
    rows = timeline_rows([9] * 8, rows=1)
    out = axis_chart(rows, 9, 20, lambda f: "now" if f >= 1.0 else str(round(f * 100)))
    border, caption = out[1].plain, out[2].plain
    assert "┬" in border  # the continuous axis has ticks, and is not a plain rule
    assert border.startswith("    └") and border.endswith("┘")
    assert border[-2] == "┬"  # the tick at the right is at the 'now' edge
    assert "now" in caption and "0" in caption


def test_axis_chart_ticks_thin_a_label_that_would_collide() -> None:
    """The chart removes a tick and its mark when its label would overprint the one before it."""
    rows = timeline_rows([9] * 16, rows=1)
    out = axis_chart(rows, 9, 8, ticks=[(0, "aaaa"), (2, "aaaa"), (7, "b")])
    border, caption = out[1].plain, out[2].plain
    assert caption.count("aaaa") == 1  # the chart skipped the crowded twin
    assert "b" in caption
    assert border.count("┬") == 2  # …and its tick mark with it (only two remain)
