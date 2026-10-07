# SPDX-License-Identifier: Apache-2.0
"""Braille charts: the only way that MeshTerm draws a row of values over time, or a meter.

All the timelines in the app render through this module: the activity pulse in the
header, the sparkline of each channel in the channel manager, the tall packet chart on
the dashboard, and the histories of the Time Machine. Thus they all obey the same three
rules:

* **Time goes from left to right.** The rightmost column is now. The history goes
  back to the left. A chronological series (oldest first) goes directly to
  :func:`timeline_rows`. The monitor and the repository keep histograms with the
  newest bucket first. These histograms go to :func:`activity_sparkline` as they are,
  and that function reverses them.
* **Grey is zero.** Each chart draws a faint baseline, one dot high, at the value
  zero. The bars grow from that line. Thus a period with no traffic shows as a flat
  line, never as a hole. When a series has negative values (for example, an SNR
  history), the baseline is at the height of zero in the chart, not at the bottom of
  the chart. Positive values go up from the baseline, and negative values go down
  from it. The only intentional break in the line is :data:`GAP`. It is a column
  that renders fully blank, down to the axis. It puts a notch between two bars that
  are next to each other (the per-day charts of the Time Machine use it to put a
  space between two days).
* **Two values in each cell.** Braille gives two dot columns in each character cell.
  Each chart uses the two columns. Thus each cell shows two sequential values, and
  the horizontal resolution of the chart is two times the number of its cells.

All the charts use the same low-level assembly of cells. A bar is an inclusive span
of dot rows, with one end on the baseline. Each character cell gets the bar style
when a dot of a bar is in it, and the faint baseline style when only the zero line
is in it. All other cells stay blank braille. (Blank braille, not a space, keeps the
grid monospace with fonts whose braille metrics are a little unusual.)

Also, this module has the three other braille conventions of the app:

* :func:`meter`: the horizontal bar for one value (the traffic counts on the
  dashboard, the SNR quality bars). It always puts two fill steps in each cell.
  Thus a meter of ``width`` cells shows ``2 × width`` levels.
* :func:`axis_caption`: the ``oldest → now`` line under a timeline. When the chart
  is wide enough, the line also gets the intermediate marks that fit.
* :func:`axis_chart`: adds a numeric y-axis to the output of :func:`timeline_rows`
  (the activity chart on the dashboard, and the per-day and rhythm charts of the
  Time Machine). Ticks mark the two edges. The platform decides if the right edge
  also prints its mark (refer to :func:`axis_chrome`). The desktop shows the mark
  on the two sides. The 53-column console keeps only the tick, and gives the cells
  to the chart. Each mark goes through :func:`compact_label`. Thus the gutter is
  never wider than three cells, also for very large counts.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from rich.text import Text

from ..platforms import Platform, on_platform

#: ``True`` if :func:`axis_chart` also shows each mark in the right gutter. These marks
#: repeat the left marks, but they help the user (JP, 2026-08-09: keep them where space
#: allows). Thus the wide desktop frame shows them, and the 53-column console uses those
#: cells for the chart instead. The ``├`` tick on the right edge stays on the two
#: platforms: when the label is removed, the tick stays. The value is bound when the
#: platform changes, the same as each constant that comes from the platform. Callers use
#: :func:`axis_chrome` to fit their chart to the shape that is bound.
_MIRROR_LABELS = True


@on_platform
def _bind_axis_chrome(platform: Platform) -> None:
    """Bind the mirror marks of the right gutter to the platform (now, and at each change)."""
    global _MIRROR_LABELS
    _MIRROR_LABELS = platform.frame_border


def axis_chrome(label_w: int) -> int:
    """The number of cells next to the chart cells in one chart row with axes, on this platform.

    These cells are the left gutter (``label_w``, the space, and the ``┤`` tick), the
    ``├`` tick on the right, and, where the platform shows the marks on the two sides,
    the space and the label of the right gutter. This is the only number that a caller
    subtracts from its render width to find ``chars``. Thus the layout always agrees
    with what :func:`axis_chart` draws.
    """
    return label_w + 3 + (label_w + 1 if _MIRROR_LABELS else 0)


#: The braille dot bit for each dot row, counted from the bottom of a cell (row 0 is
#: the lowest dot of the cell), for the left and the right columns. The Unicode
#: braille block numbers its rows from the top down (dots 1,2,3,7 on the left, and
#: 4,5,6,8 on the right). These tables reverse that order, so that all the chart
#: calculations can stay in a "height" space that goes from the bottom up.
_LEFT_BITS = (0x40, 0x04, 0x02, 0x01)
_RIGHT_BITS = (0x80, 0x20, 0x10, 0x08)

#: The style of a lit cell: a fixed Rich style name, or a callable. The callable gets
#: the values that are present in the cell (one or two values) and returns the style.
#: The Time Machine uses this hook to colour an SNR band by the quality of each slice.
CellStyle = str | Callable[[list[float]], str]


class _Gap:
    """The type of :data:`GAP`. A private singleton lets ``value is GAP`` identify it."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - only an aid for debugging
        return "GAP"


#: A timeline value that marks a hard gap. The column renders fully blank, with no bar
#: and no zero baseline. Thus it breaks the flat line of "grey is zero" on purpose.
#: ``None`` and ``0`` still draw the faint zero line (a value of no traffic is still a
#: value). Only ``GAP`` makes a hole in the line. The per-day charts of the Time Machine
#: use it to put a notch between two day bars that are next to each other. Thus two
#: neighbours of the same height never join into one solid block.
GAP = _Gap()


def chart_span(
    values: Sequence[float | None], span: tuple[float, float] | None = None
) -> tuple[float, float]:
    """The vertical range of a chart of ``values``. The range always includes zero.

    The grey baseline is zero. Thus the span is the extent of the data, extended to
    include zero:

    - Data with only positive values spans ``0 → peak`` (the baseline is at the bottom).
    - Data with only negative values spans ``floor → 0`` (the baseline is at the top,
      and the bars go down from it).
    - Mixed data spans the two.

    A caller that shows the scale of its chart in a caption must use this same span.
    Thus the label and the chart always agree.

    Args:
        values: The values of the chart. ``None`` marks an empty slot, and :data:`GAP`
            marks a hard gap. These two have no magnitude, so they are not part of the
            span.
        span: An optional wider range to include (it is also extended to include
            zero).

    Returns:
        ``(lo, hi)`` with ``lo <= 0 <= hi``. ``(0.0, 0.0)`` for an empty series.
    """
    present = [v for v in values if v is not None and v is not GAP]
    if span is not None:
        present = [*present, *span]
    lo = min([0.0, *present])
    hi = max([0.0, *present])
    return lo, hi


def timeline_rows(
    values: Sequence[float | None],
    *,
    rows: int = 1,
    span: tuple[float, float] | None = None,
    style: CellStyle = "ok",
    baseline_style: str = "faint",
    column_styles: Sequence[str] | None = None,
) -> list[Text]:
    """Render a chronological series as a braille bar chart, with the newest value at the right.

    ``values`` is in order oldest first, with one value for each dot column (two for
    each character cell). The values are scaled onto the range of :func:`chart_span`,
    which includes zero. The peak of the series fills the space above the baseline, and
    its floor fills the space below. Each value that is not zero lights at least one
    dot. Thus a single packet never disappears. ``None`` (no value) and ``0`` both draw
    only the faint zero baseline. :data:`GAP` draws nothing, and breaks the baseline
    with a clean notch.

    Args:
        values: The value for each slot, oldest first (the rightmost is "now"). If the
            number of values is odd, one silent column is added, so that only whole
            cells render. A :data:`GAP` slot renders fully blank (no bar and no
            baseline).
        rows: The height of the chart in braille rows (each has four dot rows).
        span: A wider range for the scale (refer to :func:`chart_span`). Thus some
            charts, or a chart and its caption, can use the same scale.
        style: The style of the lit cells: a Rich style name, or a callable that gets
            the values that are present in each cell (to colour each slice).
        baseline_style: The style of the zero line where nothing covers it.
        column_styles: Optional styles for each column, aligned with ``values``. They
            override ``style``. The activity charts use them to dim the history that
            an earlier session stored. A cell with two styles takes the style of its
            newer (right) column when that column is lit.

    Returns:
        ``rows`` :class:`Text` lines, top row first, ``ceil(len(values)/2)``
        characters wide.
    """
    lo, hi = chart_span(values, span)
    total = rows * 4
    base = _baseline_row(lo, hi, total)
    up = total - base  # the dot rows for a positive bar at full scale
    down = base + 1  # the dot rows for a negative bar at full scale (with the baseline row)

    bars: list[tuple[int, int] | None] = []
    for value in values:
        # Test for GAP before the numeric tests. GAP has no magnitude, thus
        # ``value == 0`` or ``value > 0`` on the sentinel can give a wrong result (or
        # raise an error).
        if value is GAP or value is None or value == 0:
            bars.append(None)
        elif value > 0:
            height = max(1, round(value / hi * up))
            bars.append((base, base + height - 1))
        else:
            height = max(1, round(value / lo * down))
            bars.append((base - height + 1, base))
    return _assemble(bars, list(values), rows, base, style, baseline_style, column_styles)


def activity_sparkline(
    histogram: Sequence[int],
    buckets: int,
    *,
    peak: float | None = None,
    style: str = "ok",
    column_styles: Sequence[str] | None = None,
) -> Text:
    """A one-row activity sparkline of a newest-first histogram, with "now" at the right.

    The bar heights are scaled to a peak, the same as :func:`timeline_rows` scales a
    chart. The peak bucket fills all four dot rows, and the other buckets draw in
    proportion. Thus the sparkline shows its shape against a ceiling, not against a
    fixed ladder of thresholds. Each bucket that is not zero still lights at least one
    dot. Thus a single packet never disappears. A silent bucket (and a time window that
    is all silent) draws only the faint zero baseline.

    The histogram comes newest first, which is the usual order of the monitor and the
    repository. This function reverses it. Thus the current bucket is at the right
    edge, and the traffic moves to the left as it gets older, the same as in each
    other MeshTerm timeline.

    ``peak`` is the ceiling of the scale. If it is ``None``, each sparkline scales
    itself to the busiest bucket of its own drawn time window. This is the plain
    relative scale, and it jumps. If ``peak`` has a value, that value is the ceiling.
    This has two results:

    - Some sparklines that get the same ``peak`` use the same scale. Thus you can
      compare their bar heights directly (a full column of channel rows, against the
      busiest channel on the screen).
    - The caller can give a ceiling that is more steady than the maximum of the time
      window.

    :func:`activity_peak` makes that steady, shared ceiling: a peak that outliers do
    not change much, with a floor, from a longer history than the history that is
    drawn. The header pulse and the channel manager both give its result to this
    function. In all other ways, the scale is a pure function of the arguments of each
    call. No hidden state is shared between instances.

    Args:
        histogram: The count for each bucket, newest first. It is padded or cut to
            ``buckets``.
        buckets: The number of buckets to draw (the number of characters is half of
            this number).
        peak: The bucket count that fills the column. ``None`` scales to the busiest
            bucket of the drawn time window. Zero (or a time window that is all silent)
            gives a flat line. If a shared peak is less than the count of a bucket,
            that bucket is clamped to full height.
        style: The style of the lit cells.
        column_styles: Optional styles for each bucket, aligned with ``histogram``
            (newest first, and reversed here with it). They override ``style``. The
            header pulse uses them to dim the buckets that an earlier session stored.

    Returns:
        A styled Rich :class:`Text` of ``buckets / 2`` braille characters.
    """
    window = (tuple(histogram) + (0,) * buckets)[:buckets]
    # If the caller gives no peak, use the peak of the time window. A one-row chart is
    # four dot rows high. Thus a bucket at the peak fills all four rows (refer to
    # ``up=total`` in timeline_rows).
    scale = peak if peak is not None else max(window, default=0)
    per_column: list[str] | None = None
    if column_styles is not None:
        padded = (list(column_styles) + [style] * buckets)[:buckets]
        per_column = list(reversed(padded))
    bars: list[tuple[int, int] | None] = []
    for count in reversed(window):
        if count <= 0 or scale <= 0:
            bars.append(None)
        else:
            height = min(4, max(1, round(count / scale * 4)))
            bars.append((0, height - 1))
    rows = _assemble(bars, [float(c) for c in reversed(window)], 1, 0, style, "faint", per_column)
    return rows[0]


#: The default percentile at which :func:`activity_peak` reads its ceiling, instead of
#: the raw maximum. A single bucket that is unusually busy is above this percentile. It
#: is clipped to full height, and it does not change the full scale. Thus one outlier
#: minute does not make all the other bars flat. The value is high enough that usual,
#: continuous traffic still gets to the top.
ACTIVITY_PERCENTILE = 90.0


def _percentile(values: Sequence[float], p: float) -> float:
    """The ``p``-th percentile of ``values``, with linear interpolation (``0.0`` when empty).

    The result is a blend of the two ranks on each side of the fractional position.
    This is the default convention of NumPy. Thus with a small sample, the mark moves
    smoothly, not in jumps of a full element.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (p / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def activity_peak(
    *histograms: Sequence[int],
    percentile: float = ACTIVITY_PERCENTILE,
    floor: float = 0.0,
) -> float:
    """A steady ceiling for the scale of :func:`activity_sparkline`, from newest-first counts.

    A scale relative to the peak shows the shape well, but it jumps. The plain maximum
    of the time window jumps each time that the busiest bucket comes into the time
    window, or goes out of it because of its age. Also, a single packet in a quiet
    time window fills the column, because it is the maximum. This function puts two
    dampers into one ceiling. Thus a full column of sparklines, or one sparkline that
    moves bucket by bucket, stays easy to read:

    * **Robustness.** The ceiling is a high ``percentile`` of the counts, not their
      maximum. Thus a single bucket that is unusually busy is above the ceiling, and
      it is clipped to full height. It does not make all the other bars smaller to
      change the scale. If you give this function a longer history than the history
      that is drawn (the six hours of the header, the pool of the channels), the
      ceiling is more steady. The percentile of a wide pool changes very little when
      one bucket scrolls out of the drawn time window. Thus the ceiling moves smoothly
      instead of in jumps.
    * **Floor.** The result is never less than ``floor``. Thus a stray packet in a
      time window that was silent for a long time draws a small bar against a scale
      that has a meaning, not a bar at full height. ``floor`` is also the scale when
      all the buckets are silent.

    We tried a third damper and removed it: **recency**, which reduced each bucket by
    ``decay ** age``. That damper decayed the counts before it took the percentile,
    but the bars that the chart draws against that ceiling were not decayed. Thus the
    units did not agree: the two agree only at age zero. With a half-life much shorter
    than the pooled time window, the damper pushed the ceiling down to the floor. (Most
    of the traffic is older than one half-life, thus the weighted percentile
    collapsed.) Then a busy earlier period that was still on the screen saturated
    completely. The depth of the pool already gives the steadiness that recency tried
    to get, and without the collapse. Thus age is not part of the ceiling now.

    Some histograms (for example, the histogram of each visible channel) go into one
    pool for one ceiling. Thus a column of rows uses one scale, and you can compare
    their bars directly. The result is a pure function of the counts that the caller
    gives. No hidden state is kept from one frame to the next. Thus the result is
    deterministic, and it changes smoothly when the data changes smoothly. Give the
    result to the ``peak`` of each sparkline.

    Args:
        histograms: One or more count series, newest first (bucket 0 is "now"). Their
            buckets that are not zero go into the pool for the ceiling. The order has
            no weight: a bucket counts the same at each age in the time window.
        percentile: The percentile of the counts that gives the ceiling (0–100).
        floor: The lowest value of the ceiling (also the scale when all the buckets
            are silent).

    Returns:
        The ceiling of the scale, ``>= floor``.
    """
    counts = [count for histogram in histograms for count in histogram if count > 0]
    return max(float(floor), _percentile(counts, percentile))


#: The fill glyphs of the meter, ``(full step, half step)``, for each profile. A
#: full-height meter lights the top three dot rows and keeps the bottom row blank
#: (``⠿`` for the two columns, ``⠇`` for the left column only). It shows as a solid
#: count bar that is a little above the bottom of the cell, so that it does not join
#: the row below it. A slim meter lights only the two middle rows (``⠶`` / ``⠆``).
#: Thus the bar floats in the middle of the cell. It can be on an unlit track of the
#: same glyph, and it does not become a solid block.
_METER_FULL = ("⠿", "⠇")
_METER_SLIM = ("⠶", "⠆")


def meter(
    fraction: float | None,
    width: int,
    *,
    style: str,
    slim: bool = False,
    track: str | None = None,
) -> Text:
    """Render one value as a horizontal braille meter, with two fill steps in each cell.

    This is the only way that MeshTerm draws a proportion as a bar. The meter always
    uses the two dot columns of each cell: a meter of ``width`` cells shows
    ``2 × width`` levels. Each value lights at least one half step, also a value that
    is clamped to the bottom of its scale. A measured bottom is still a measurement, so
    it never disappears. (To get a meter that is fully empty, give ``None``.) Two
    profiles cover the two cases of the app:

    * **Full height, no track** (the default): the top three dot rows. The bottom row
      stays blank, so that the bar is above the bottom of the cell. Unlit cells stay
      blank. This is a count bar whose length is the value (the traffic lanes on the
      dashboard).
    * **Slim, on a track** (``slim=True, track="track"``): only the two middle dot
      rows. The unlit remainder is drawn in the same glyph, dimmed to ``track``. This
      is a gauge whose value fills a visible background (the SNR quality bars). The
      half step at the boundary keeps the colour of the value, not of the track,
      because it is still part of the measurement.

    Args:
        fraction: The fill as a fraction of full scale, clamped to ``0 .. 1``.
            ``None`` draws a meter with no lit part (only the track, if there is one).
        width: The full-scale span of the meter, in character cells.
        style: The style of the lit fill.
        slim: Light only the two middle dot rows, not the top three.
        track: The style of the unlit remainder, drawn in the full-step glyph.
            ``None`` pads with spaces instead, so that the meter still has ``width``
            cells.

    Returns:
        A :class:`Text` of exactly ``width`` cells.
    """
    full_glyph, half_glyph = _METER_SLIM if slim else _METER_FULL
    if fraction is None:
        steps = 0
    else:
        frac = min(1.0, max(0.0, fraction))
        steps = max(1, round(frac * width * 2))
    full, half = divmod(steps, 2)
    bar = Text(full_glyph * full + half_glyph * half, style=style)
    rest = width - full - half
    if track is not None:
        bar.append(full_glyph * rest, style=track)
    else:
        bar.append(" " * rest)
    return bar


def axis_caption(
    chars: int,
    label_at: Callable[[float], str],
    *,
    style: str = "faint",
) -> tuple[Text, list[int]]:
    """The caption line under a timeline: the edge labels, and the marks that fit between.

    Each chart once had a caption only at its ends (``oldest … now``). This function
    also asks ``label_at`` for the labels at the quarter points. It keeps the densest
    set that fits: the quarters, else the midpoint, else only the two ends. Thus the
    user can read the timescale of a wide chart, and does not have to count cells. The
    first label is aligned left on the left edge of the chart. The last label is
    aligned right on the right edge. The labels between them are centred on the
    fraction that they describe. At least two blank cells separate each label from the
    next, so that they never touch.

    Args:
        chars: The width of the chart in character cells (the caption has the same
            width).
        label_at: Changes a position fraction (``0.0`` = the oldest column, ``1.0`` =
            now) to its label.
        style: The style of the full caption.

    Returns:
        ``(caption, tick_cells)``: the caption :class:`Text`, exactly ``chars`` cells
        wide, and the chart column to which each placed label points. The axis border
        uses these columns to put a ``┬`` notch under each label (refer to
        :func:`axis_chart`).
    """
    for segments in (4, 2, 1):
        fractions = [i / segments for i in range(segments + 1)]
        labels = [label_at(f) for f in fractions]
        cells: list[str] = [" "] * chars
        taken: list[tuple[int, int]] = []  # the [start, end) spans of placed labels, in order
        ticks: list[int] = []  # the chart column to which each label points
        ok = True
        for frac, label in zip(fractions, labels, strict=True):
            if frac == 0.0:
                start = 0
                ref = 0
            elif frac == 1.0:
                start = chars - len(label)
                ref = chars - 1
            else:
                centre = round(frac * chars)
                start = min(chars - len(label), max(0, centre - len(label) // 2))
                ref = min(chars - 1, centre)
            end = start + len(label)
            if end > chars or any(start < e + 2 and s < end + 2 for s, e in taken):
                ok = False
                break
            taken.append((start, end))
            ticks.append(ref)
            cells[start:end] = label
        if ok:
            return Text("".join(cells), style=style), ticks
    # The two edge labels also collide. Keep only the left label.
    label = label_at(0.0)[:chars]
    return Text(label.ljust(chars), style=style), [0]


def compact_label(value: float) -> str:
    """A y-axis mark that stays the size of the gutter for all counts: ``999``, ``12k``, ``2M``.

    A count above 999 becomes a rounded ``k`` or ``M`` value (JP, 2026-08-08). Thus a
    gutter mark always fits in three cells, for each count that a mesh can produce in
    practice. Without this, a day with a five-digit count makes the gutter of each chart
    wider. A signed value keeps its minus sign (the depths of the SNR band). A rounded
    value is correct for the job of the gutter: a tick shows approximately the value of
    a bar whose peak is at its dot. It is not an exact account. The headings still show
    the exact counts.
    """
    if abs(value) < 1000:
        return str(round(value))
    thousands = round(value / 1000)
    if abs(thousands) < 1000:
        return f"{thousands}k"
    return f"{round(value / 1_000_000)}M"


#: The minimum number of cells for the y-axis labels of a chart, also when its peak is
#: small (JP, 2026-10-04). When the width of the gutter came only from the widest mark,
#: the gutter grew when a busy minute changed the peak from ``9`` to ``10`` to ``100``.
#: Then the full chart moved to the side while the user looked at it. Three cells hold
#: each mark that :func:`compact_label` prints below a thousand (``563``), and each
#: compacted mark (``1k``, ``-12``). Thus the gutter does not move.
AXIS_LABEL_MIN = 3


def axis_label_w(peak: float, rows: int, *, lo: float = 0.0) -> int:
    """The gutter width for a chart of this scale: its widest printed mark, at least 3.

    If the width comes only from the compacted peak, it is too small. A ``1.5k`` peak
    compacts to ``2k``, which has two cells. But a lower tick can still be at ``563``,
    which has three cells. When stacked charts share one gutter, the callers
    take the maximum of this value for the scale of each chart. (The default gutter of
    :func:`axis_chart` is calculated in the same way.) The width is never less than
    :data:`AXIS_LABEL_MIN`.
    """
    return max([AXIS_LABEL_MIN, *(len(mark) for mark in y_axis_labels(peak, rows, lo=lo))])


def y_axis_labels(peak: float, rows: int, *, lo: float = 0.0) -> list[str]:
    """The ticked-dot value of each chart row, top row first, with repeats and zeros blank.

    The ``┤`` tick of the gutter crosses a braille row at the third dot from the bottom
    of the row. Thus each mark shows the value of a bar whose peak is at that dot. This
    is the value to which the tick visibly points, not the value at the top edge of the
    row. The mark uses the same scale (with zero included, and with the baseline row)
    that :func:`timeline_rows` uses to draw bars. The mark goes through
    :func:`compact_label`, so that it is never wider than the gutter.

    A mark is blank if it repeats the mark above it, or if it is zero. (With a low peak,
    neighbouring dots can round to the same value, or to the same ``1k``.) Thus the
    scale never shows the same number two times. A signed chart gives its floor as
    ``lo`` (``< 0``). Then the marks above the grey zero line show the heights of the
    bars that go up, and the marks below it show the depths of the bars that go down.
    Thus the gutter shows the two signs of a series that crosses zero (``+5 … −10 dB``
    for an SNR band), not only the positive peak.
    """
    total = rows * 4
    base = _baseline_row(lo, peak, total)
    labels: list[str] = []
    seen: set[str] = set()
    for i in range(rows):
        dot = (rows - 1 - i) * 4 + 2  # the dot row that the ┤ tick of this row crosses
        if dot > base:
            value = round(peak * (dot - base + 1) / (total - base))
        elif dot < base:
            value = round(lo * (base - dot + 1) / (base + 1))
        else:
            value = 0  # the tick is on the zero baseline
        mark = compact_label(value)
        if peak != lo and value and mark not in seen:
            labels.append(mark)
            seen.add(mark)
        else:
            labels.append("")
    return labels


#: The minimum number of blank cells between two labels of the column axis. Thus a tick
#: row with fewer labels shows separate marks, not a line of text where the labels touch.
_TICK_GAP = 2


def _tick_axis(
    chars: int,
    label_w: int,
    ticks: Sequence[tuple[int, str]],
    style: str,
) -> tuple[Text, str]:
    """Make the boxed border and its caption for a column chart with explicit ticks.

    Each tick is a ``(cell, label)`` that points at a chart column. The border draws a
    ``┬`` at that column, and the label is centred below it. The labels are placed from
    left to right. A label that is less than :data:`_TICK_GAP` cells from the label
    before it is removed, with its tick. Thus a crowded axis shows only the labels that
    fit, and no label prints on top of another label. The callers first reduce the
    ticks to approximately the correct number (refer to ``_day_ticks`` in the Time
    Machine). This function is the last protection against a collision.

    Args:
        chars: The width of the chart in character cells.
        label_w: The width of the y-axis gutter. The border and the caption are
            indented past it.
        ticks: The ``(cell, label)`` marks, in any order. ``cell`` is a chart column,
            counted from 0.
        style: The style of the border (and its ticks).

    Returns:
        ``(border, caption_text)``: the border :class:`Text`, and the plain caption
        string, ready to indent under the gutter.
    """
    cells = [" "] * chars
    marked: list[int] = []
    last_end = -_TICK_GAP
    for cell, label in sorted(ticks):
        cell = max(0, min(chars - 1, cell))
        start = max(0, min(chars - len(label), cell - len(label) // 2))
        if start < last_end + _TICK_GAP:
            continue  # too near the label before it: remove this mark
        cells[start : start + len(label)] = list(label)
        marked.append(cell)
        last_end = start + len(label)
    return _tick_border(chars, label_w, marked, style), "".join(cells)


def _tick_border(chars: int, label_w: int, tick_cells: Sequence[int], style: str) -> Text:
    """The boxed bottom border, with a ``┬`` notch at each tick column of the chart."""
    marked = {max(0, min(chars - 1, c)) for c in tick_cells}
    bar = "".join("┬" if i in marked else "─" for i in range(chars))
    return Text(" " * label_w + " └" + bar + "┘", style=style)


def axis_chart(
    chart_rows: list[Text],
    peak: float,
    chars: int,
    label_at: Callable[[float], str] | None = None,
    *,
    label_w: int | None = None,
    style: str = "muted",
    floor: float = 0.0,
    ticks: Sequence[tuple[int, str]] | None = None,
) -> list[Text]:
    """Add a mirrored y-axis and an x-axis caption to the output of :func:`timeline_rows`.

    The value at the ticked dot of each row is the mark in the left gutter, compacted
    through :func:`compact_label`. The mark is blank if it repeats the mark above it,
    or if it is zero. The right edge always has the matching ``├`` tick. The platform
    decides if the mark is also printed after this tick (refer to :func:`axis_chrome`):
    the desktop shows the mark again, and the console keeps only the tick. A boxed
    bottom border and an x-axis caption complete the box. The caption is indented past
    the gutter.

    The caption comes from one of two sources. A continuous chart gives ``label_at``,
    and the marks of :func:`axis_caption` (the ends and the quarters) fill the space
    that is available. A columnar chart (bars of a fixed width, one for each day or
    slot) gives ``ticks`` instead. These are explicit ``(cell, label)`` marks. Each mark
    centres a label under its column, and puts a ``┬`` notch in the border below it
    (refer to :func:`_tick_axis`). Thus a date is under the bar that it names, not at a
    fraction that has no relation to the bar.

    Args:
        chart_rows: The rows of the chart, as :func:`timeline_rows` returns them.
        peak: The full-scale ceiling of the chart (the value of its tallest bar). The
            gutter mark of each row shows the value at the dot to which its tick
            points (refer to :func:`y_axis_labels`). Thus the peak sets the size of
            the gutter, but the peak itself is not always printed.
        chars: The width of the chart in character cells.
        label_at: Changes a position fraction to the x-axis caption at that point (a
            continuous chart). It is ignored when ``ticks`` is given.
        label_w: The digit width of the gutter, when some stacked charts must use the
            same width so that their gutters align. By default, it comes from ``peak``
            (and ``floor``).
        style: The style of the gutters, the ticks, the marks, and the border.
        floor: The value at the bottom edge of a signed chart (``< 0``). Thus the
            gutter shows the two extremes of a series that crosses zero (the SNR band
            of the Time Machine). The default is ``0``: a chart with only positive
            values, with its baseline at the bottom. This is the usual case.
        ticks: Explicit ``(cell, label)`` column marks for a columnar chart. When they
            are given, they make the border and the caption instead of ``label_at``.

    Returns:
        ``len(chart_rows) + 2`` :class:`Text` lines: the rows with their axes, the
        bottom border, and the caption.
    """
    marks = y_axis_labels(peak, len(chart_rows), lo=floor)
    label_w = label_w or max([AXIS_LABEL_MIN, *(len(mark) for mark in marks)])
    out: list[Text] = []
    for mark, row in zip(marks, chart_rows, strict=True):
        line = Text(f"{mark:>{label_w}} " + ("┤" if mark else "│"), style=style)
        line.append_text(row)
        line.append("├" if mark else "│", style=style)
        if mark and _MIRROR_LABELS:
            line.append(f" {mark}", style=style)
        out.append(line)
    if ticks is not None:
        border, caption_text = _tick_axis(chars, label_w, ticks, style)
        caption = Text(" " * (label_w + 2))
        caption.append(caption_text, style="faint")
    else:
        assert label_at is not None, "axis_chart needs label_at or ticks"
        # A continuous axis puts the same ┬ notches under its labels (the ends and the
        # quarters) as a columnar axis puts under its bars. All the axes use the same
        # conventions.
        caption_body, tick_cells = axis_caption(chars, label_at)
        border = _tick_border(chars, label_w, tick_cells, style)
        caption = Text(" " * (label_w + 2))
        caption.append_text(caption_body)
    out.append(border)
    out.append(caption)
    return out


def _baseline_row(lo: float, hi: float, total: int) -> int:
    """The dot row (counted from the bottom of the chart) of the zero baseline.

    If all the data is positive, the baseline is at the bottom. If all the data is
    negative, the baseline is at the top. In a mixed span, its position is
    proportional, and it is clamped one row in from each edge. Thus the two directions
    each keep at least one dot row to draw in.

    Args:
        lo: The floor of the span (``<= 0``).
        hi: The ceiling of the span (``>= 0``).
        total: The height of the chart in dot rows.

    Returns:
        The dot row of the baseline, ``0 .. total - 1``.
    """
    if lo == 0:
        return 0
    if hi == 0:
        return total - 1
    return min(total - 2, max(1, round((total - 1) * -lo / (hi - lo))))


def _column_bits(bar: tuple[int, int] | None, floor: int, table: tuple[int, ...]) -> int:
    """The braille bits that the bar of one column lights in a cell row.

    Args:
        bar: The inclusive span of dot rows of the bar (chart coordinates), or
            ``None``.
        floor: The bottom dot row of the cell row, in chart coordinates.
        table: :data:`_LEFT_BITS` or :data:`_RIGHT_BITS`.

    Returns:
        The OR of the bits of the covered dots (0 when the bar is not in this cell
        row).
    """
    if bar is None:
        return 0
    lo, hi = max(bar[0], floor), min(bar[1], floor + 3)
    bits = 0
    for dot in range(lo, hi + 1):
        bits |= table[dot - floor]
    return bits


def _assemble(
    bars: list[tuple[int, int] | None],
    values: list[float | None],
    rows: int,
    base: int,
    style: CellStyle,
    baseline_style: str,
    column_styles: Sequence[str] | None = None,
) -> list[Text]:
    """Assemble the bar spans into styled braille rows (the shared walk through the cells).

    By construction, each bar span includes the baseline row. Thus in a cell with one
    silent column, the zero line continues through the lit character. A cell with two
    silent columns shows only the faint baseline dots, on the row that has them. All
    other cells stay blank braille, to keep the grid monospace. The baseline is set for
    each dot column, not for each cell. Thus a :data:`GAP` column can be fully blank,
    and break the zero line with a notch, while the other column of its cell keeps its
    baseline.

    Args:
        bars: The inclusive span of dot rows for each column (``None`` = no bar).
        values: The values of the columns, aligned with ``bars``. A callable ``style``
            gets them two at a time, for each cell. A :data:`GAP` value removes the
            baseline of that column, so that the notch goes down to the axis.
        rows: The height of the chart in braille rows.
        base: The dot row of the zero baseline.
        style: A fixed style, or a callable for each cell (refer to
            :data:`CellStyle`).
        baseline_style: The style of the cells that have only the baseline.
        column_styles: Style overrides for each column, aligned with ``bars``. A cell
            takes the style of its right column when that column is lit, else the
            style of its left column (the newer value gets the shared cell).

    Returns:
        ``rows`` :class:`Text` lines, top row first.
    """
    if len(bars) % 2:
        bars = [*bars, None]
        values = [*values, None]
    lines: list[Text] = []
    for row in range(rows):
        floor = (rows - 1 - row) * 4
        base_bit_left = _LEFT_BITS[base - floor] if floor <= base <= floor + 3 else 0
        base_bit_right = _RIGHT_BITS[base - floor] if floor <= base <= floor + 3 else 0
        line = Text()
        for i in range(0, len(bars), 2):
            # A GAP column has no baseline. Thus the zero line breaks there, and the
            # notch goes cleanly from the top edge down to the axis border.
            bl = 0 if values[i] is GAP else base_bit_left
            br = 0 if values[i + 1] is GAP else base_bit_right
            left = _column_bits(bars[i], floor, _LEFT_BITS)
            right = _column_bits(bars[i + 1], floor, _RIGHT_BITS)
            if left or right:
                mask = left | right | bl | br
                if column_styles is not None and i + 1 < len(column_styles):
                    cell_style = column_styles[i + 1] if right else column_styles[i]
                elif callable(style):
                    present = [v for v in values[i : i + 2] if v is not None and v is not GAP]
                    cell_style = style(present) if present else baseline_style
                else:
                    cell_style = style
                line.append(chr(0x2800 | mask), style=cell_style)
            elif bl or br:
                line.append(chr(0x2800 | bl | br), style=baseline_style)
            else:
                line.append(chr(0x2800))
        lines.append(line)
    return lines
