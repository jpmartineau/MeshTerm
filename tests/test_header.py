# SPDX-License-Identifier: Apache-2.0
"""Header tests: the activity sparkline that fills the width of the one-line status bar."""

from __future__ import annotations

from types import SimpleNamespace

from meshterm.ui.menu import _header


def _ctx(histogram=(), unread=0, alerts=0, battery=None) -> SimpleNamespace:
    """A minimal substitute that has only what the header reads (the simulator path).

    ``battery`` is the cached reading of the poller (an object with ``percent`` and
    ``charging``), or ``None`` for a companion with no pack. ``None`` is the default, so
    most tests see the old header with no battery.
    """
    return SimpleNamespace(
        mock=True,
        chat=SimpleNamespace(unread_total=lambda: unread),
        monitor=SimpleNamespace(
            activity_histogram=lambda: tuple(histogram),
            activity_session_flags=lambda: (True,) * len(tuple(histogram)),
        ),
        watchtower=SimpleNamespace(unacked_count=lambda: alerts),
        battery=SimpleNamespace(reading=lambda: battery),
    )


def test_header_shows_the_watchtower_badge() -> None:
    """Alerts that are not acknowledged show as the triangle badge. With none, there is no badge."""
    assert "▲ 2" in _header(_ctx(alerts=2), {}, 80).plain
    assert "▲" not in _header(_ctx(alerts=0), {}, 80).plain


def test_header_segments_style_each_run_on_its_own() -> None:
    """The styles of a segment are side by side. They are never in layers, one under the next.

    The code builds the segments as separate ``Text`` objects and joins them (refer to
    :func:`~meshterm.ui.menu._header_segments`). It is easy to use ``style=`` in the
    constructor, but then that style becomes the base style of the object. The base style
    covers all the text that the code appends after it. Then the version string would
    render as brand under muted, and the count of each badge would get the error red of
    its glyph.
    """
    header = _header(_ctx(unread=3, alerts=2), {}, 120)
    covering = {
        header.plain[span.start : span.end]: span.style
        for span in header.spans
        if span.style in ("brand", "err")
    }
    assert covering.get("MeshTerm") == "brand"  # not "MeshTerm v1.2.3"
    assert set(covering) == {"MeshTerm", "●", "▲"}  # the count of each badge is not in it


def test_header_sparkline_fills_the_row_exactly() -> None:
    """The sparkline fills the row exactly.

    Each cell that the fixed segments leave is sparkline. There are no short rows and no
    overflow.
    """
    for width in (60, 100):
        header = _header(_ctx(histogram=(21,) * 360), {}, width)
        assert header.cell_len == width
        assert "⣿" in header.plain  # cells at the maximum, to the edge


def test_header_puts_now_at_the_right_edge() -> None:
    """The header puts now at the right edge.

    The traffic of the current minute is drawn in the last cell of the row, and not in the
    first cell.
    """
    header = _header(_ctx(histogram=(21,) + (0,) * 359), {}, 80).plain
    spark = [ch for ch in header if 0x2800 <= ord(ch) <= 0x28FF]
    # The newest bucket is the right dot column of the last cell. Its left column is the
    # minute before, which is silent, except for the baseline dot that continues.
    assert spark[-1] == chr(0x2800 | 0xB8 | 0x40)
    assert all(ch == chr(0x2800 | 0x40 | 0x80) for ch in spark[:-1])  # the older cells are flat


def test_header_wider_terminal_shows_deeper_history() -> None:
    """A wider terminal shows a deeper history.

    A burst 90 minutes ago is not in a narrow header, but it is visible in a wide header.
    One dot column is one minute. For 90 minutes back, the sparkline needs 45 cells. Thus
    the burst shows as a lit cell only when the row has that much room.
    """
    histogram = tuple(0 if i != 90 else 21 for i in range(360))

    def burst_cells(row: str) -> list[str]:
        # Each dot that is lit above the bottom dot row (bits other than dots 7 and 8)
        # is the burst. The flat baseline and blank braille never set those bits.
        return [ch for ch in row if 0x2800 <= ord(ch) <= 0x28FF and ord(ch) & 0x3F]

    assert not burst_cells(_header(_ctx(histogram=histogram), {}, 60).plain)
    assert burst_cells(_header(_ctx(histogram=histogram), {}, 120).plain)


def test_header_scales_to_the_window_peak() -> None:
    """The pulse scales to the peak of the window.

    The pulse scales relative to a peak, and not with a fixed ladder of thresholds. A ramp
    of 16, 12, 8, 4 climbs one dot for each step against the robust ceiling that
    ``activity_peak`` computes for it. Here the ceiling is approximately 14.8, which is a
    90th percentile of the counts, with a floor. Thus the four newest minutes show the
    heights 1, 2, 3, 4 from left to right, in the last two cells of the row.
    """
    histogram = (16, 12, 8, 4) + (0,) * 356
    header = _header(_ctx(histogram=histogram), {}, 80).plain
    spark = [ch for ch in header if 0x2800 <= ord(ch) <= 0x28FF]
    left = (0x00, 0x40, 0x44, 0x46, 0x47)
    right = (0x00, 0x80, 0xA0, 0xB0, 0xB8)
    assert spark[-2:] == [chr(0x2800 | left[1] | right[2]), chr(0x2800 | left[3] | right[4])]


def test_header_tight_row_drops_to_compact_separators() -> None:
    """A tight row changes to compact separators.

    When wide separators would make the pulse too small, they become narrow (' · ').
    """
    roomy = _header(_ctx(), {}, 100).plain
    assert "  ·  " in roomy
    tight = _header(_ctx(unread=12, alerts=3), {}, 60).plain
    assert "  ·  " not in tight and " · " in tight
    # The compact separators give real room to the sparkline, also on a row of 60 columns
    # with many badges.
    spark = [ch for ch in tight if 0x2800 <= ord(ch) <= 0x28FF]
    assert len(spark) >= 14


def test_header_survives_a_sliver_terminal() -> None:
    """The header survives a terminal that is only a sliver.

    When the fixed segments already overflow the width, the sparkline is not drawn.
    """
    header = _header(_ctx(histogram=(9,) * 360), {}, 10)
    # There is no room left, so there is no braille at all. Nothing goes past the edge.
    assert all(not (0x2800 <= ord(ch) <= 0x28FF) for ch in header.plain)


def _spark(row: str) -> list[str]:
    return [ch for ch in row if 0x2800 <= ord(ch) <= 0x28FF]


def test_header_pins_the_battery_gauge_to_the_right_edge() -> None:
    """The header pins the battery gauge to the right edge.

    A companion with a pack shows its charge in the corner. The pulse gives the room to it.
    """
    reading = SimpleNamespace(percent=87, charging=False)
    header = _header(_ctx(histogram=(21,) * 360, battery=reading), {}, 80)
    assert header.cell_len == 80  # the header still fills the row exactly, with the gauge
    assert header.plain.rstrip().endswith("87%")  # the gauge is flush right
    # The pulse gave real cells for the room. It has fewer sparkline cells than a bare row,
    # also when the count includes the full-cell glyph of the gauge.
    with_batt = len(_spark(header.plain))
    bare = len(_spark(_header(_ctx(histogram=(21,) * 360), {}, 80).plain))
    assert with_batt < bare


def test_header_hides_the_gauge_without_a_battery() -> None:
    """If there is no pack, there is no gauge. The pulse keeps the whole row that it always had."""
    header = _header(_ctx(histogram=(21,) * 360, battery=None), {}, 80)
    assert "%" not in header.plain
    assert header.cell_len == 80
