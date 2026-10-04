# SPDX-License-Identifier: Apache-2.0
"""Braille charts: the one way MeshTerm draws a value-over-time row or a meter.

Every timeline in the app — the header's activity pulse, the channel manager's
per-channel sparklines, the dashboard's tall packet chart, the Time Machine's
histories — renders through this module, so they all share the same three rules:

* **Time flows left → right.** The rightmost column is *now*; history trails away
  to the left. Chronological (oldest-first) series feed :func:`timeline_rows`
  directly; the newest-first histograms the monitor and repository keep are passed
  as-is to :func:`activity_sparkline`, which reverses them into place.
* **Grey is zero.** Every chart draws a faint one-dot baseline at value zero and
  bars grow *from that line*, so a silent stretch reads as a flatline, never a
  hole. When a series carries negative values (an SNR history, say) the baseline
  sits at zero's height inside the chart — positive readings rise above it,
  negative ones hang below — rather than being nailed to the chart floor. The one
  deliberate break in the line is :data:`GAP`, a column that renders *fully blank*
  down to the axis, used to notch adjacent bars apart (the Time Machine's per-day
  charts space one day from the next this way).
* **Two readings per cell.** Braille offers two dot columns per character cell;
  every chart uses both, so each cell shows two consecutive readings and the
  chart draws at twice the horizontal resolution of the cells it occupies.

The low-level cell assembly is shared: bars are expressed as inclusive dot-row
spans anchored on the baseline, and each character cell takes the bar style when
any bar dot falls in it, the faint baseline style when only the zero line does,
and stays blank braille otherwise (blank braille, not a space, so the grid stays
monospace under fonts with slightly odd braille metrics).

Beyond timelines, the module owns the app's two other braille conventions:

* :func:`meter` — the single-value horizontal bar (the dashboard's traffic
  tallies, the SNR quality bars), always packing two fill steps per cell so a
  ``width``-cell meter resolves ``2 × width`` levels.
* :func:`axis_caption` — the ``oldest → now`` line under a timeline, which
  fills in intermediate marks whenever the chart is wide enough to fit them.
* :func:`axis_chart` — frames :func:`timeline_rows` output in a numeric y-axis
  (the dashboard's activity chart, the Time Machine's per-day and rhythm
  charts). Marks tick both edges; whether the right edge also *prints* its
  mark follows the platform (see :func:`axis_chrome`) — the desktop mirrors
  it, the 53-column console keeps the tick alone and gives the cells to the
  chart. Each mark compacts through :func:`compact_label` so the gutter never
  outgrows three cells however deep the tallies run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from rich.text import Text

from ..platforms import Platform, on_platform

#: Whether :func:`axis_chart` mirrors each mark on the right gutter too. The marks are
#: redundant but nice (JP, 2026-08-09: keep them where space allows), so the roomy
#: desktop frame mirrors them and the 53-column console spends those cells on the chart
#: instead. The right-edge ``├`` tick stays on *both* platforms — dropping the label
#: never drops the tick. Bound at platform-switch time, like every platform-derived
#: constant; :func:`axis_chrome` is how callers size their chart to whichever shape is
#: bound.
_MIRROR_LABELS = True


@on_platform
def _bind_axis_chrome(platform: Platform) -> None:
    """Bind the right-gutter mirror to the platform (runs now and on every switch)."""
    global _MIRROR_LABELS
    _MIRROR_LABELS = platform.frame_border


def axis_chrome(label_w: int) -> int:
    """Cells one framed chart row spends beside its chart cells, on this platform.

    The left gutter (``label_w`` + the space + the ``┤`` tick), the right ``├`` tick,
    and — where the platform mirrors its marks — the right gutter's space + label. THE
    number a caller subtracts from its render width to size ``chars``, so the layout
    can never disagree with what :func:`axis_chart` actually draws.
    """
    return label_w + 3 + (label_w + 1 if _MIRROR_LABELS else 0)


#: Braille dot bit for each dot row counted from the *bottom* of a cell (row 0 is
#: the cell's lowest dot), left and right columns. The Unicode braille block
#: numbers its rows top-down (dots 1,2,3,7 left / 4,5,6,8 right); these tables
#: flip that so chart math can stay in bottom-up "height" space throughout.
_LEFT_BITS = (0x40, 0x04, 0x02, 0x01)
_RIGHT_BITS = (0x80, 0x20, 0x10, 0x08)

#: How a lit cell is styled: a fixed Rich style name, or a callable given the
#: cell's present readings (one or two values) returning the style — the hook the
#: Time Machine uses to colour an SNR band by each slice's quality.
CellStyle = str | Callable[[list[float]], str]


class _Gap:
    """The type of :data:`GAP`; a private singleton, so ``value is GAP`` identifies it."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - a debugging aid only
        return "GAP"


#: A timeline value marking a hard gap: the column renders *fully blank* — no bar and
#: no zero baseline — so it breaks the "grey is zero" flatline on purpose. ``None`` and
#: ``0`` still draw the faint zero line (a silent reading is still a reading); only
#: ``GAP`` punches a hole in it, used to notch adjacent day bars apart in the Time
#: Machine's per-day charts so same-height neighbours never fuse into one solid block.
GAP = _Gap()


def chart_span(
    values: Sequence[float | None], span: tuple[float, float] | None = None
) -> tuple[float, float]:
    """The vertical range a chart of ``values`` draws over, zero always included.

    The grey baseline *is* zero, so the span is the data's extent folded around it:
    all-positive data spans ``0 → peak`` (the baseline on the floor), all-negative
    data ``floor → 0`` (the baseline on the ceiling, bars hanging), mixed data both.
    Callers that caption their chart's scale should quote this same span so the
    label and the drawing can never disagree.

    Args:
        values: The chart's readings (``None`` marks an empty slot; :data:`GAP` a
            hard gap — neither carries a magnitude, so both sit out the span).
        span: An optional wider range to honour (it too is folded around zero).

    Returns:
        ``(lo, hi)`` with ``lo <= 0 <= hi``; ``(0.0, 0.0)`` for an empty series.
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
    """Render a chronological series as a braille bar chart, newest at the right.

    ``values`` is oldest-first, one reading per dot column (two per character
    cell), scaled onto :func:`chart_span`'s zero-folded range: the series' peak
    fills the space above the baseline, its floor the space below, and any
    non-zero reading lights at least one dot so a lone packet never vanishes.
    ``None`` (no reading) and ``0`` alike draw only the faint zero baseline;
    :data:`GAP` draws nothing at all, breaking the baseline into a clean notch.

    Args:
        values: Per-slot readings, oldest first (the rightmost is "now"). An odd
            count is padded with one silent column so whole cells always render.
            A :data:`GAP` slot renders fully blank (bar and baseline both).
        rows: How many braille rows tall the chart is (four dot rows each).
        span: A wider range to scale against (see :func:`chart_span`), so several
            charts — or a chart and its caption — can share one scale.
        style: Style for lit cells: a Rich style name, or a callable given each
            cell's present readings (for per-slice colouring).
        baseline_style: Style for the zero line where nothing covers it.
        column_styles: Optional per-column styles, aligned with ``values``,
            overriding ``style`` — how the activity charts dim history recorded by
            an earlier session. A cell straddling two styles takes its newer
            (right) lit column's.

    Returns:
        ``rows`` :class:`Text` lines, top row first, ``ceil(len(values)/2)``
        characters wide.
    """
    lo, hi = chart_span(values, span)
    total = rows * 4
    base = _baseline_row(lo, hi, total)
    up = total - base  # dot rows available to a full-scale positive bar
    down = base + 1  # …and to a full-scale negative one (baseline row included)

    bars: list[tuple[int, int] | None] = []
    for value in values:
        # GAP must short-circuit ahead of the numeric tests — it has no magnitude,
        # so ``value == 0`` / ``value > 0`` would misfire (or raise) on the sentinel.
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
    """A one-row activity sparkline over a newest-first histogram, "now" rightmost.

    Bar heights scale to a *peak* the way :func:`timeline_rows` scales a chart: the
    peak bucket fills all four dot rows and the rest draw in proportion, so the
    sparkline reads its shape against a ceiling rather than a fixed threshold ladder.
    Any non-zero bucket still lights at least one dot, so a lone packet never
    vanishes; a silent bucket (and an all-silent window) draws only the faint zero
    baseline. The histogram arrives newest-first — the natural order the monitor and
    repository keep — and is reversed here, so the current bucket lands on the right
    edge and traffic slides *left* as it ages, like every other MeshTerm timeline.

    ``peak`` is the scaling ceiling. Left ``None``, each sparkline self-scales to *its
    own* drawn window's busiest bucket — the plain, jumpy relative scale. Passed a
    value, that value is the ceiling, which does two things: it lets several sparklines
    handed the *same* one share a scale so their bar heights are directly comparable
    (a whole column of per-channel rows against the busiest channel on screen), and it
    lets the caller substitute a *steadier* ceiling than the bare window maximum.
    :func:`activity_peak` builds that steady, shared ceiling — an outlier-robust,
    floored peak over a deeper history than is drawn — and both the
    header pulse and the channel manager pass its result here. The scale is otherwise a
    pure function of what each call is given: no hidden cross-instance state lives here.

    Args:
        histogram: Per-bucket counts, newest first; padded/cropped to ``buckets``.
        buckets: How many buckets to draw (half this many characters).
        peak: The bucket count that fills the column; ``None`` self-scales to the
            drawn window's busiest bucket. Zero (or an all-silent window) is a
            flatline. A shared peak below a bucket's own count clamps to full height.
        style: Style for lit cells.
        column_styles: Optional per-bucket styles aligned with ``histogram``
            (newest first, reversed here alongside it), overriding ``style`` —
            how the header pulse dims buckets recorded by an earlier session.

    Returns:
        A styled Rich :class:`Text` of ``buckets / 2`` braille characters.
    """
    window = (tuple(histogram) + (0,) * buckets)[:buckets]
    # The window's own peak when the caller names none; a one-row chart is four dot
    # rows tall, so a bucket at the peak fills all four (see timeline_rows' up=total).
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


#: Default percentile :func:`activity_peak` reads its ceiling at, in place of the raw
#: maximum: a lone freak-busy bucket sits *above* this and clips to full height instead
#: of redefining the whole scale, so one outlier minute doesn't flatten every other bar.
#: High enough that ordinary sustained traffic still reaches the top.
ACTIVITY_PERCENTILE = 90.0


def _percentile(values: Sequence[float], p: float) -> float:
    """The linear-interpolated ``p``-th percentile of ``values`` (``0.0`` when empty).

    The two ranks either side of the fractional position blended together — NumPy's
    default convention — so a small sample moves the mark smoothly instead of in
    whole-element jumps.
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
    """A steady scaling ceiling for :func:`activity_sparkline`, from newest-first counts.

    Relative-to-peak scaling reads its shape well but is jumpy: the plain window
    maximum lurches whenever the busiest bucket enters or ages out of view, and a lone
    packet in a quiet window fills the column because it *is* the maximum. This folds
    two dampers into one ceiling so a whole column of sparklines — or one that slides
    bucket by bucket — stays legible:

    * **Robustness.** The ceiling is a high ``percentile`` of the counts, not their
      maximum, so a single freak-busy bucket sits above it and clips to full height
      instead of shrinking every other bar to redefine the scale. Handing this a deeper
      history than is drawn (the header's six hours, the channel pool's) steadies it
      further: the percentile of a wide pool barely stirs as one bucket scrolls off the
      drawn window, so the ceiling *glides* rather than snapping.
    * **Floor.** The result never drops below ``floor``, so a stray packet in a
      long-silent window draws a small nub against a meaningful scale instead of
      shouting at full height. It is also the scale when everything is silent.

    A third damper — **recency**, discounting each bucket by ``decay ** age`` — was
    tried and removed. Decaying the *counts* before taking the percentile, then scaling
    the *undecayed* bars drawn against that ceiling, is a unit mismatch: the two only
    agree at age zero. With a half-life far shorter than the pooled window it starved
    the ceiling to the floor (most traffic mass sits older than one half-life, so the
    weighted percentile collapsed), and a genuinely busy earlier stretch still on screen
    then saturated wholesale. The pool depth already supplies the steadiness recency was
    reaching for, without the collapse — so age no longer enters the ceiling at all.

    Several histograms pool into one ceiling — every visible channel's, say — so a
    column of rows shares one scale and their bars stay directly comparable. The result
    is a pure function of the counts handed in: no hidden cross-frame state, so it is
    deterministic and eases on its own as the data does. Pass it to each sparkline's
    ``peak``.

    Args:
        histograms: One or more newest-first count series (bucket 0 is "now"); their
            non-zero buckets pool into the ceiling. Order carries no weight — a bucket
            counts the same however old it is in the window.
        percentile: The percentile of counts the ceiling reads (0–100).
        floor: The lowest the ceiling may fall to (also the all-silent scale).

    Returns:
        The scaling ceiling, ``>= floor``.
    """
    counts = [count for histogram in histograms for count in histogram if count > 0]
    return max(float(floor), _percentile(counts, percentile))


#: The meter's fill glyphs, ``(full step, half step)``, by profile. A *full-height*
#: meter lights the top three dot rows and leaves the bottom row blank (``⠿`` both
#: columns, ``⠇`` the left column alone), reading as a solid tally bar that lifts a
#: hair off the cell floor so it doesn't fuse with the row beneath it. A *slim* meter
#: lights only the middle two rows (``⠶`` / ``⠆``), so the bar floats mid-cell and can
#: sit over an unlit track of the same glyph without turning into a solid block.
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
    """Render a single value as a horizontal braille meter, two fill steps per cell.

    The one way MeshTerm draws a proportion as a bar. Both dot columns of every cell
    are always exploited — a ``width``-cell meter resolves ``2 × width`` levels — and
    any *reading* lights at least one half step, even one clamped to the floor of its
    scale: a measured bottom is still a measurement, so it never vanishes (``None``
    is how a truly empty meter is asked for). Two profiles cover the app's two cases:

    * **full-height, no track** (the default): the top three dot rows (the bottom
      row left blank so the bar lifts off the cell floor), unlit cells left blank —
      a tally bar whose length *is* the reading (the dashboard's traffic lanes).
    * **slim, on a track** (``slim=True, track="track"``): only the middle two dot
      rows, with the unlit remainder drawn in the same glyph dimmed to ``track`` — a
      gauge whose reading fills in a visible background (the SNR quality bars). The
      boundary half-step keeps the reading's colour, not the track's: it is still
      part of what was measured.

    Args:
        fraction: The fill as a fraction of full scale, clamped to ``0 .. 1``;
            ``None`` draws an entirely unlit meter (just the track, if any).
        width: The meter's full-scale span in character cells.
        style: Style for the lit fill.
        slim: Light only the middle two dot rows instead of all four.
        track: Style for the unlit remainder, drawn in the full-step glyph; ``None``
            pads with spaces instead, so the meter still occupies ``width`` cells.

    Returns:
        A :class:`Text` exactly ``width`` cells wide.
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
    """The caption line under a timeline: edge labels plus whatever marks fit between.

    Every chart used to caption only its ends (``oldest … now``); this asks
    ``label_at`` for the labels at the quarter points too and keeps the densest set
    that fits — quarters, else the midpoint, else just the two ends — so a wide chart
    reads its timescale without counting cells. The first label is left-aligned on the
    chart's left edge, the last right-aligned on its right edge, and interior labels
    are centred on the fraction they describe, each separated by at least two blank
    cells so they never run together.

    Args:
        chars: The chart's width in character cells (the caption matches it).
        label_at: Maps a position fraction (``0.0`` = the oldest column, ``1.0`` =
            now) to its label.
        style: Style the whole caption is drawn in.

    Returns:
        ``(caption, tick_cells)`` — the caption :class:`Text` exactly ``chars`` cells
        wide, and the chart column each placed label points at, so the axis border can
        notch a ``┬`` under it (see :func:`axis_chart`).
    """
    for segments in (4, 2, 1):
        fractions = [i / segments for i in range(segments + 1)]
        labels = [label_at(f) for f in fractions]
        cells: list[str] = [" "] * chars
        taken: list[tuple[int, int]] = []  # placed [start, end) spans, in order
        ticks: list[int] = []  # the chart column each label points at
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
    # Even the two edge labels collide: keep the left one and let it stand alone.
    label = label_at(0.0)[:chars]
    return Text(label.ljust(chars), style=style), [0]


def compact_label(value: float) -> str:
    """A y-axis mark that stays gutter-sized however deep the tally: ``999``, ``12k``, ``2M``.

    Counts above 999 compact to a rounded ``k``/``M`` (JP, 2026-08-08) so a gutter mark
    always fits three cells for any tally a mesh realistically produces, instead of a
    five-digit day widening every chart's gutter. A signed value keeps its minus (the SNR
    band's depths). Rounding suits the gutter's job — a tick quotes roughly what a bar
    peaking at its dot means, not an exact ledger; the headings still carry exact counts.
    """
    if abs(value) < 1000:
        return str(round(value))
    thousands = round(value / 1000)
    if abs(thousands) < 1000:
        return f"{thousands}k"
    return f"{round(value / 1_000_000)}M"


#: The fewest cells a chart's y-axis labels are given, however small its peak (JP,
#: 2026-10-04). Sized to the widest mark alone, the gutter grew as a busy minute pushed the
#: peak from ``9`` to ``10`` to ``100``, sliding the whole chart sideways under the reader;
#: three cells hold every mark :func:`compact_label` prints below a thousand (``563``) and
#: every compacted one (``1k``, ``-12``), so the gutter stays put.
AXIS_LABEL_MIN = 3


def axis_label_w(peak: float, rows: int, *, lo: float = 0.0) -> int:
    """The gutter width a chart of this scale needs: its widest printed mark, at least 3.

    Sizing from the compacted *peak* alone under-measures: a ``1.5k`` peak prints as the
    two-cell ``1k`` while a lower tick can still land at ``563`` — three cells. Callers
    sharing one gutter across stacked charts take the max of this over each chart's
    scale (and the default :func:`axis_chart` gutter is computed the same way). Never
    narrower than :data:`AXIS_LABEL_MIN`.
    """
    return max([AXIS_LABEL_MIN, *(len(mark) for mark in y_axis_labels(peak, rows, lo=lo))])


def y_axis_labels(peak: float, rows: int, *, lo: float = 0.0) -> list[str]:
    """Each chart row's ticked-dot value, top row first, dupes and zeros blanked.

    The gutter's ``┤`` tick crosses a braille row at the third dot up from the
    row's bottom, so each mark quotes the value of a bar peaking *at that dot* —
    the reading the tick visibly points at, not the row's top edge — using the
    same zero-folded scaling :func:`timeline_rows` draws bars with (baseline row
    and all), compacted through :func:`compact_label` so it never outgrows the
    gutter. A mark that would repeat the one above (a low peak makes
    neighbouring dots round to the same value — or to the same ``1k``) or read
    zero is left blank, so the scale never shows the same number twice. A signed
    chart passes its floor as ``lo`` (``< 0``): marks above the grey zero line
    quote rise heights, marks below it hang depths, so the gutter shows both
    signs of a zero-crossing series (an SNR band's ``+5 … −10 dB``) instead of
    only the positive peak.
    """
    total = rows * 4
    base = _baseline_row(lo, peak, total)
    labels: list[str] = []
    seen: set[str] = set()
    for i in range(rows):
        dot = (rows - 1 - i) * 4 + 2  # the dot row this row's ┤ tick crosses
        if dot > base:
            value = round(peak * (dot - base + 1) / (total - base))
        elif dot < base:
            value = round(lo * (base - dot + 1) / (base + 1))
        else:
            value = 0  # the tick sits on the zero baseline itself
        mark = compact_label(value)
        if peak != lo and value and mark not in seen:
            labels.append(mark)
            seen.add(mark)
        else:
            labels.append("")
    return labels


#: Minimum blank cells kept between two column-axis labels, so a thinned tick row
#: reads as separate marks rather than a run of touching text.
_TICK_GAP = 2


def _tick_axis(
    chars: int,
    label_w: int,
    ticks: Sequence[tuple[int, str]],
    style: str,
) -> tuple[Text, str]:
    """Build the boxed border and its caption for a column chart with explicit ticks.

    Each tick is a ``(cell, label)`` pointing at a chart column: the border draws a
    ``┬`` at that column and the label sits centred beneath it. Labels are placed
    left to right and any that would land within :data:`_TICK_GAP` cells of the one
    before it is dropped — tick and all — so a crowded axis thins to what fits
    instead of overprinting. Callers pre-thin to roughly the right count (see the
    Time Machine's ``_day_ticks``); this is the collision backstop.

    Args:
        chars: The chart's width in character cells.
        label_w: The y-axis gutter width the border and caption indent past.
        ticks: ``(cell, label)`` marks, any order; ``cell`` is a 0-based chart column.
        style: Style for the border (and its ticks).

    Returns:
        ``(border, caption_text)`` — the border :class:`Text` and the plain caption
        string, ready to indent under the gutter.
    """
    cells = [" "] * chars
    marked: list[int] = []
    last_end = -_TICK_GAP
    for cell, label in sorted(ticks):
        cell = max(0, min(chars - 1, cell))
        start = max(0, min(chars - len(label), cell - len(label) // 2))
        if start < last_end + _TICK_GAP:
            continue  # would crowd the label before it — drop this mark
        cells[start : start + len(label)] = list(label)
        marked.append(cell)
        last_end = start + len(label)
    return _tick_border(chars, label_w, marked, style), "".join(cells)


def _tick_border(chars: int, label_w: int, tick_cells: Sequence[int], style: str) -> Text:
    """The boxed bottom border, notched with a ``┬`` at each charted tick column."""
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
    """Frame :func:`timeline_rows` output with a mirrored y-axis and an x-axis caption.

    Each row's ticked-dot value marks the left gutter (blank where it would repeat
    the mark above or read zero), compacted through :func:`compact_label`. The right
    edge always carries the matching ``├`` tick; whether the mark itself is printed
    after it follows the platform (see :func:`axis_chrome`) — the desktop mirrors it,
    the console keeps the tick alone. A boxed bottom border and an x-axis caption
    indented to clear the gutter close the frame.

    The caption comes one of two ways. A *continuous* chart passes ``label_at`` and
    the ends-plus-quarters marks of :func:`axis_caption` fill in whatever fits. A
    *columnar* chart — bars of a fixed width apiece, a day or a slot each — instead
    passes ``ticks``: explicit ``(cell, label)`` marks that centre a label under its
    own column and notch the border with a ``┬`` beneath it (see :func:`_tick_axis`),
    so a date sits under the bar it names rather than at an arbitrary fraction.

    Args:
        chart_rows: The chart's rows, as returned by :func:`timeline_rows`.
        peak: The chart's full-scale ceiling (its tallest bar's value); each
            row's gutter mark quotes the value at the dot its tick points at
            (see :func:`y_axis_labels`), so the peak itself sizes the gutter
            but is not necessarily printed.
        chars: The chart's width in character cells.
        label_at: Maps a position fraction to the x-axis caption at that point
            (a continuous chart); ignored when ``ticks`` is given.
        label_w: The gutter's digit width, when several stacked charts must
            share one width so their gutters line up; sized from ``peak`` (and
            ``floor``) by default.
        style: Style for the gutters, ticks, marks, and border.
        floor: The value the bottom edge reads on a signed chart (``< 0``), so
            the gutter quotes both extremes of a zero-crossing series (the Time
            Machine's SNR band). Defaults to ``0`` — an all-positive chart whose
            baseline sits on the floor, the common case.
        ticks: Explicit ``(cell, label)`` column marks for a columnar chart; when
            given they drive the border and caption instead of ``label_at``.

    Returns:
        ``len(chart_rows) + 2`` :class:`Text` lines: the decorated rows, the
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
        # A continuous axis notches the same ┬ ticks under its ends-and-quarters
        # labels as a columnar one does under its bars — one axis grammar everywhere.
        caption_body, tick_cells = axis_caption(chars, label_at)
        border = _tick_border(chars, label_w, tick_cells, style)
        caption = Text(" " * (label_w + 2))
        caption.append_text(caption_body)
    out.append(border)
    out.append(caption)
    return out


def _baseline_row(lo: float, hi: float, total: int) -> int:
    """The dot row (from the chart bottom) the zero baseline sits on.

    All-positive data pins it to the floor, all-negative to the ceiling; a mixed
    span places it proportionally, clamped one row in from either edge so both
    directions keep at least one dot row to draw in.

    Args:
        lo: The span's floor (``<= 0``).
        hi: The span's ceiling (``>= 0``).
        total: The chart's height in dot rows.

    Returns:
        The baseline's dot row, ``0 .. total - 1``.
    """
    if lo == 0:
        return 0
    if hi == 0:
        return total - 1
    return min(total - 2, max(1, round((total - 1) * -lo / (hi - lo))))


def _column_bits(bar: tuple[int, int] | None, floor: int, table: tuple[int, ...]) -> int:
    """The braille bits one column's bar lights within a cell row.

    Args:
        bar: The bar's inclusive dot-row span (chart coordinates), or ``None``.
        floor: The cell row's bottom dot row in chart coordinates.
        table: :data:`_LEFT_BITS` or :data:`_RIGHT_BITS`.

    Returns:
        The OR of the covered dots' bits (0 when the bar misses this cell row).
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
    """Assemble bar spans into styled braille rows (the shared cell walk).

    Every bar span includes the baseline row by construction, so a half-silent
    cell keeps the zero line continuous inside the lit character; a fully silent
    cell shows just the faint baseline dots on whichever row holds them; anything
    else stays blank braille to keep the grid monospace. The baseline is decided
    per *dot column*, not per cell, so a :data:`GAP` column can go fully blank —
    breaking the zero line into a notch — while its cell-mate keeps its own.

    Args:
        bars: Per-column inclusive dot-row spans (``None`` = no bar).
        values: The readings behind the columns, aligned with ``bars`` (fed to a
            callable ``style`` two at a time, per cell); a :data:`GAP` reading
            suppresses that column's baseline so the notch reaches the axis.
        rows: Chart height in braille rows.
        base: The zero baseline's dot row.
        style: Fixed style, or per-cell callable (see :data:`CellStyle`).
        baseline_style: Style for baseline-only cells.
        column_styles: Per-column style overrides aligned with ``bars``; a cell
            takes its right column's style when that column is lit, else its
            left's (the newer reading wins the shared cell).

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
            # A GAP column carries no baseline, so the zero line breaks there and the
            # notch runs clean from the top edge down to the axis border.
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
