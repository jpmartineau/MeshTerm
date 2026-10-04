# SPDX-License-Identifier: Apache-2.0
"""Braille chart widget tests: orientation, the zero baseline, scaling, and styles.

The module is pure Text-in/Text-out, so every behaviour is asserted straight off
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

#: Dot bits for a column filled bottom-up to height 0–4 (left / right column).
_L = (0x00, 0x40, 0x44, 0x46, 0x47)
_R = (0x00, 0x80, 0xA0, 0xB0, 0xB8)
_BASE = chr(0x2800 | 0x40 | 0x80)  # the two-bottom-dot zero flatline
_BLANK = chr(0x2800)


def _ch(*bits: int) -> str:
    mask = 0
    for b in bits:
        mask |= b
    return chr(0x2800 | mask)


# --- chart_span ---------------------------------------------------------------------


def test_chart_span_always_folds_zero_in() -> None:
    """Positive, negative, and mixed series all keep zero inside the span."""
    assert chart_span([3, 7]) == (0.0, 7.0)
    assert chart_span([-3, -7]) == (-7.0, 0.0)
    assert chart_span([-3, 7]) == (-3.0, 7.0)
    assert chart_span([None, None]) == (0.0, 0.0)
    assert chart_span([1], span=(-5, 10)) == (-5.0, 10.0)


# --- timeline_rows: all-positive series ------------------------------------------------


def test_timeline_peak_fills_the_height() -> None:
    """The series' peak fills every dot row; readings pair up two per cell."""
    rows = timeline_rows([12, 12, 0, 0], rows=3)
    assert len(rows) == 3
    assert all(len(r.plain) == 2 for r in rows)
    assert rows[0].plain[0] == _ch(_L[4], _R[4])  # the peak pair tops out
    assert rows[0].plain[1] == _BLANK
    assert rows[2].plain[1] == _BASE  # the silent pair keeps the zero line


def test_timeline_each_dot_column_has_its_own_height() -> None:
    """Two readings sharing a cell rise independently — braille's full resolution."""
    rows = timeline_rows([12, 6], rows=3)
    assert rows[0].plain[0] == _ch(_L[4])
    assert rows[1].plain[0] == _ch(_L[4], _R[2])
    assert rows[2].plain[0] == _ch(_L[4], _R[4])


def test_timeline_never_hides_a_lone_packet() -> None:
    """A tiny non-zero value still lights a dot, in the lit style not the floor's."""
    rows = timeline_rows([1, 0, 1000, 1000], rows=3)
    bottom = rows[2]
    assert bottom.plain[0] == _ch(_L[1], _R[1])  # bar dot + the baseline continuing
    assert bottom.spans[0].style == "ok"


def test_timeline_all_silent_is_a_flatline() -> None:
    """With nothing to draw the chart is a faint floor line, not a hole."""
    rows = timeline_rows([0, 0, 0, 0, 0, 0], rows=2)
    assert rows[1].plain == _BASE * 3
    assert rows[0].plain == _BLANK * 3
    assert all(span.style == "faint" for span in rows[1].spans)


def test_timeline_pads_an_odd_tail_column() -> None:
    """An odd reading count still renders whole cells, the floor continuing under it."""
    rows = timeline_rows([4], rows=1)
    assert len(rows[0].plain) == 1
    assert rows[0].plain[0] == _ch(_L[4], _R[1])  # full bar + the padded column's floor


# --- timeline_rows: GAP (a hard break in the baseline) --------------------------------


def test_timeline_gap_breaks_the_baseline_but_zero_keeps_it() -> None:
    """A GAP column draws nothing — not even the zero line — while a 0 keeps its dot.

    The cell pairs a GAP (left) with a plain 0 (right): the left column is fully
    blank down to the floor, the right column still shows its faint baseline dot —
    the distinction that lets a day-boundary notch reach the axis while an empty
    day stays a grey flatline.
    """
    rows = timeline_rows([GAP, 0], rows=1)
    assert rows[0].plain[0] == _ch(_R[1])  # only the right (0) column's baseline dot
    assert rows[0].spans and rows[0].spans[0].style == "faint"


def test_timeline_gap_column_stays_blank_beside_a_full_bar() -> None:
    """A GAP paired with a tall bar keeps its own column blank to the axis."""
    rows = timeline_rows([GAP, 4], rows=1)  # left GAP, right full-height bar
    # Right column carries the whole bar; the left (GAP) column lights no dot at all,
    # not even the bottom baseline the bar's cell would otherwise share.
    assert rows[0].plain[0] == _ch(_R[4])
    assert not (ord(rows[0].plain[0]) & 0x40)  # no bottom-left dot


def test_chart_span_ignores_gap_columns() -> None:
    """GAP carries no magnitude, so it never widens the span."""
    assert chart_span([GAP, 3, 7]) == (0.0, 7.0)
    assert chart_span([GAP, GAP]) == (0.0, 0.0)


# --- timeline_rows: negative and mixed series ------------------------------------------


def test_timeline_negative_series_hangs_from_a_top_baseline() -> None:
    """All-negative data pins zero to the ceiling and bars grow downward."""
    rows = timeline_rows([-4, -2], rows=1)
    # Left column full hang (0..3); right hangs two dots (2..3); the baseline row
    # (the cell's top dots) is inside both bars.
    assert rows[0].plain[0] == _ch(0x40, 0x04, 0x02, 0x01, 0x10, 0x08)


def test_timeline_mixed_series_puts_zero_mid_chart() -> None:
    """With readings both sides of zero, positives rise and negatives hang."""
    rows = timeline_rows([-10, 5], rows=3, span=(-10, 10))
    # Baseline lands on dot row 6: the full -10 hangs 0..6, the +5 rises 6..8.
    assert rows[0].plain[0] == _ch(0x80)  # the rise's head, above the baseline
    assert rows[1].plain[0] == _ch(0x46, 0x18)  # hang and rise straddle the zero row
    assert rows[2].plain[0] == _ch(0x47)  # the hang's tail, below everything


def test_timeline_baseline_shows_mid_chart_in_silent_cells() -> None:
    """A silent cell of a mixed-span chart draws the zero line where zero *is*."""
    rows = timeline_rows([-10, 5, None, None], rows=3, span=(-10, 10))
    assert rows[1].plain[1] == _ch(0x02, 0x10)  # both columns' dot on row 6, faint
    assert rows[1].spans[-1].style == "faint"
    assert rows[0].plain[1] == _BLANK
    assert rows[2].plain[1] == _BLANK


# --- styles -----------------------------------------------------------------------------


def test_timeline_per_cell_style_callable_sees_the_cells_readings() -> None:
    """A callable style is fed each lit cell's present readings."""
    seen: list[list[float]] = []

    def style(values: list[float]) -> str:
        seen.append(values)
        return "warn"

    rows = timeline_rows([2, 4, None, 6], rows=1, style=style)
    assert seen == [[2, 4], [6]]
    assert all(span.style == "warn" for span in rows[0].spans)


# --- activity_sparkline -------------------------------------------------------------------


def test_sparkline_puts_now_on_the_right() -> None:
    """A newest-first histogram renders with the current bucket at the right edge."""
    # Newest bucket the sole traffic, so it is the self-scaled peak and fills the
    # column: only the final cell's *right* dot column lights (with the floor dot
    # keeping the left column's baseline).
    text = activity_sparkline((21, 0, 0, 0), 4)
    assert text.plain == _BASE + _ch(_R[4], 0x40)


def test_sparkline_scales_to_the_window_peak() -> None:
    """With no shared peak, the busiest bucket fills the column and the rest scale to it."""
    # Newest-first (16, 12, 8, 4) is a full/¾/½/¼ ramp against its own peak of 16;
    # flipped for display it climbs 1→4 dots left→right toward now.
    text = activity_sparkline((16, 12, 8, 4), 4)
    assert text.plain == _ch(_L[1], _R[2]) + _ch(_L[3], _R[4])


def test_sparkline_shares_a_peak_across_instances() -> None:
    """A passed peak scales the bars to one ceiling, so quiet windows read short.

    The same bucket draws a taller bar when it *is* the window's peak than when a
    louder sibling's peak is scaled in — the channel manager's shared-scale column.
    """
    solo = activity_sparkline((8, 0), 2)  # self-scaled: 8 is its own peak → full height
    shared = activity_sparkline((8, 0), 2, peak=32)  # 8 against 32 → a quarter, one dot
    assert solo.plain == _ch(0x40, _R[4])  # right column full (4 dots), left on baseline
    assert shared.plain == _ch(0x40, _R[1])  # right column a single dot


def test_sparkline_pads_and_crops_to_the_window() -> None:
    """A short histogram pads with silence; a long one crops to the newest buckets."""
    padded = activity_sparkline((21,), 6)
    assert padded.plain == _BASE * 2 + _ch(_R[4], 0x40)
    cropped = activity_sparkline((0, 0, 21, 21, 9, 9), 2)
    assert cropped.plain == _BASE  # only the two newest (silent) buckets survive


# --- activity_peak --------------------------------------------------------------------


def test_activity_peak_reads_a_percentile_not_the_max() -> None:
    """One freak-busy bucket sits above the ceiling instead of defining it."""
    # Ten steady buckets of 10 and a lone spike of 1000: the ceiling is the 90th
    # percentile of the counts — the steady level, not the spike.
    assert activity_peak((1000,) + (10,) * 10, percentile=90) == 10.0


def test_activity_peak_ignores_bucket_age() -> None:
    """A burst weighs the same however old it is: recency no longer enters the ceiling."""
    # The magnitude-decay recency term was removed (it starved the ceiling to the floor
    # over a deep pool), so a 20-burst sets the same ceiling now or three buckets back.
    assert activity_peak((20, 0, 0)) == activity_peak((0, 0, 20)) == 20.0


def test_activity_peak_floors_a_quiet_window() -> None:
    """The floor holds the ceiling up so a lull's stray packet stays a nub, not a column."""
    assert activity_peak((0, 0, 0), floor=3.0) == 3.0  # silent → floor
    lone = activity_peak((1,), floor=3.0)
    assert lone == 3.0  # a single packet scales against the floor, not against itself


def test_activity_peak_pools_several_histograms_into_one_ceiling() -> None:
    """Pooled channels share a scale: a quiet channel reads short beside a busy one."""
    pooled = activity_peak((10,) * 10, (1,), percentile=90)
    assert pooled == 10.0  # the busy channel's level sets the shared ceiling…
    solo = activity_peak((1,), percentile=90)
    assert solo == 1.0  # …where the quiet one alone would have set just 1


# --- y_axis_labels / axis_chart -------------------------------------------------------


def test_y_axis_labels_blanks_a_silent_chart() -> None:
    """A zero peak draws no marks at all."""
    assert y_axis_labels(0, 3) == ["", "", ""]


def test_y_axis_labels_marks_quote_the_ticked_dot() -> None:
    """Each mark reads the value at its row's third dot — where the ┤ tick points.

    peak 10 over 2 rows (8 dots): the top row's tick crosses dot 6 (a 7-dot bar,
    10·7/8 → 9), the bottom row's dot 2 (a 3-dot bar, 10·3/8 → 4) — not the rows'
    top-edge values (10 and 5), which sit a dot above where the glyph points.
    """
    assert y_axis_labels(10, 2) == ["9", "4"]


def test_y_axis_labels_blanks_a_repeated_mark() -> None:
    """A low peak rounds a lower row to the same value as the one above: left blank."""
    assert y_axis_labels(1, 3) == ["1", "", ""]


def test_axis_chart_mirrors_marks_where_the_platform_affords_them() -> None:
    """An axis chart mirrors its marks only where the platform can afford them.

    The desktop frames a chart with marks on both gutters; the console keeps the right
    ``├`` tick but spends the mirrored label's cells on the chart instead.
    """
    from meshterm.platforms import PICOCALC, set_platform
    from meshterm.ui.braillechart import axis_chrome

    rows = timeline_rows([9] * 8, rows=2)
    out = axis_chart(rows, 9, 4, lambda f: "now" if f >= 1.0 else "old")
    assert len(out) == 4  # 2 chart rows + bottom border + caption
    assert out[0].plain == "  8 ┤⣿⣿⣿⣿├ 8"  # ticked-dot values, mirrored on the desktop
    assert out[1].plain == "  3 ┤⣿⣿⣿⣿├ 3"  # (in the 3-cell minimum gutter)
    assert out[2].plain.startswith("    └") and out[2].plain.endswith("┘")
    assert axis_chrome(1) == 2 * (1 + 2)  # what a caller must budget beside the cells

    set_platform(PICOCALC)
    tight = axis_chart(timeline_rows([9] * 8, rows=2), 9, 4, lambda f: "x")
    assert tight[0].plain == "  8 ┤⣿⣿⣿⣿├"  # the tick survives; the label's cells don't
    assert tight[1].plain == "  3 ┤⣿⣿⣿⣿├"
    assert axis_chrome(1) == 1 + 3


def test_axis_chart_compacts_deep_tallies_into_the_gutter() -> None:
    """Counts above 999 read as k/M marks, and the gutter sizes to the widest printed mark.

    A 1.5k peak prints ``1k`` while a lower tick still lands three digits wide (``562``),
    so the gutter takes three cells — sized from the marks, never from the bare peak.
    """
    from meshterm.ui.braillechart import axis_label_w, compact_label

    assert compact_label(999) == "999" and compact_label(1500) == "2k"
    assert compact_label(12_345) == "12k" and compact_label(2_000_000) == "2M"
    assert compact_label(-1200) == "-1k"

    rows = timeline_rows([1500] * 8, rows=2)
    out = axis_chart(rows, 1500, 4, lambda f: "x")
    assert axis_label_w(1500, 2) == 3
    assert out[0].plain == " 1k ┤⣿⣿⣿⣿├ 1k"  # round(1500 · 7/8) → 1312 → 1k
    assert out[1].plain == "562 ┤⣿⣿⣿⣿├ 562"  # round(1500 · 3/8) → the three-digit tick


def test_y_axis_labels_signed_span_quotes_both_extremes() -> None:
    """A signed chart's marks quote rise heights above zero and hang depths below."""
    # rows=3 over +8..-4 (baseline on dot 4): the top tick reads a 7-dot rise (7),
    # the middle a 3-dot rise (3), the bottom a 3-deep hang below zero (−2).
    assert y_axis_labels(8, 3, lo=-4) == ["7", "3", "-2"]
    # A symmetric span pins the middle tick onto the baseline itself: blanked zero.
    assert y_axis_labels(6, 3, lo=-6) == ["5", "", "-4"]


def test_axis_chart_floor_frames_a_signed_chart_and_widens_the_gutter() -> None:
    """A signed floor draws negative marks and sizes the gutter for the widest one.

    ``-107`` takes four cells, one past the three every gutter is given anyway, so the
    floor is what widens it here.
    """
    rows = timeline_rows([150, -150], rows=3, span=(-150, 150))
    out = axis_chart(rows, 150, 1, lambda f: "x", floor=-150)
    assert out[0].plain.startswith(" 125 ┤")  # gutter widened to four cells by the floor
    assert out[2].plain.startswith("-107 ┤")  # a negative mark near the floor


def test_every_gutter_is_at_least_three_cells() -> None:
    """A low peak's one-digit marks still get three cells, so the chart never slides.

    Sized to the widest mark alone, the gutter grew under the reader as a busy minute
    took the peak from ``9`` to ``10`` to ``100`` (JP, 2026-10-04).
    """
    from meshterm.ui.braillechart import AXIS_LABEL_MIN, axis_label_w

    assert AXIS_LABEL_MIN == 3
    assert axis_label_w(2, 3) == axis_label_w(90, 3) == axis_label_w(900, 3) == 3
    assert axis_label_w(9000, 3) == 3  # compacted to "8k", still inside the three
    rows = timeline_rows([2] * 8, rows=2)
    assert axis_chart(rows, 2, 4, lambda f: "x")[0].plain.startswith("  2 ┤")


def test_axis_chart_honours_a_shared_label_width() -> None:
    """An explicit label_w widens the gutter past the peak's own digit count.

    Lets several stacked charts (the Time Machine's per-day pair) share one gutter
    width sized from the wider chart, so their marks line up column for column.
    """
    rows = timeline_rows([9] * 8, rows=1)
    out = axis_chart(rows, 9, 4, lambda f: "x", label_w=3)
    assert out[0].plain.startswith("  7 ┤")


# --- axis_chart column ticks ----------------------------------------------------------


def test_axis_chart_ticks_notch_the_border_and_centre_labels() -> None:
    """Explicit column ticks draw a ``┬`` on the border with the label centred beneath."""
    rows = timeline_rows([9] * 8, rows=1)
    out = axis_chart(rows, 9, 4, ticks=[(0, "A"), (3, "D")])
    assert out[1].plain == "    └┬──┬┘"  # ticks at chart cells 0 and 3
    assert out[2].plain == "     A  D"  # labels centred under their ticks


def test_axis_chart_continuous_axis_notches_ticks_under_its_labels() -> None:
    """A continuous (label_at) chart now notches ┬ ticks under its ends-and-quarters labels."""
    rows = timeline_rows([9] * 8, rows=1)
    out = axis_chart(rows, 9, 20, lambda f: "now" if f >= 1.0 else str(round(f * 100)))
    border, caption = out[1].plain, out[2].plain
    assert "┬" in border  # the continuous axis is ticked, not a plain rule
    assert border.startswith("    └") and border.endswith("┘")
    assert border[-2] == "┬"  # the rightmost tick sits at the 'now' edge
    assert "now" in caption and "0" in caption


def test_axis_chart_ticks_thin_a_label_that_would_collide() -> None:
    """A tick whose label would overprint the one before it is dropped, tick and all."""
    rows = timeline_rows([9] * 16, rows=1)
    out = axis_chart(rows, 9, 8, ticks=[(0, "aaaa"), (2, "aaaa"), (7, "b")])
    border, caption = out[1].plain, out[2].plain
    assert caption.count("aaaa") == 1  # the crowded twin was skipped
    assert "b" in caption
    assert border.count("┬") == 2  # …and its tick with it (only two survive)
