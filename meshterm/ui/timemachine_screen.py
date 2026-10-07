# SPDX-License-Identifier: Apache-2.0
"""The Time Machine: the recorded history of the mesh, to explore node by node.

This module is the interactive form of the ``timemachine`` tool. The database has stored
each heard packet since the first session. But before this screen, nothing showed that
history beyond the two-hour time window of the dashboard. This screen is the place where
the user digs into that past. A picker offers the whole mesh or any node that MeshTerm
ever heard. Each subject renders as a scrollable page of braille charts and stats over a
time window that the user can change. ``w`` cycles 24 h → 7 d → 30 d → all time. On a
handheld (a platform with an F-key lane), the ring stops at 30 d, and its F3 chip cycles
the same three spans:

* A **node** shows its reception volume over the time window, its median-SNR band
  (coloured by quality), its hour-of-day rhythm (when does this node talk?), and the
  roll-up stats: first and last heard, medians, and extremes.
* The **whole mesh** shows the packets per day and the nodes per day across the
  history, the hour-of-day rhythm of the full mesh, the arrivals of the time window
  (nodes heard for the first time ever, in aligned name, hash, and first-heard lanes),
  and the all-time totals.

The whole-mesh page also cycles its **scope** on ``s`` (F2 on the PicoCalc): all,
unscoped, each region heard in the time window from A to Z, and unknown scope. This is
the ring of the dashboard, over the recorded history. A narrowed page charts the floods
of that scope per day (or per hour), their rhythm, and its own ledger. The node counts
and the arrivals are not shown, because MeshTerm reads them from adverts, and adverts
have no scope. A flood that was stored before MeshTerm kept the route of floods counts
only under "all". The node page and the page of our node have no scope to narrow. The page
of a node shows its adverts and telemetry, and what our node sends is traces, which go
direct.

The charts are in time order: the oldest at the left and now at the right, which is the
timeline direction of the app. They draw through :mod:`~meshterm.ui.braillechart`, so
the grey baseline always marks zero: the readings of the SNR band hang below it or rise
above it by their real sign. All the content is stored data. No device is necessary, and
nothing transmits.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, timedelta
from statistics import median
from typing import TYPE_CHECKING

from rich.console import Group, RenderableType
from rich.text import Text

from ..core.models import utcnow
from ..platforms import Platform, on_platform
from .braillechart import (
    _TICK_GAP,
    GAP,
    axis_chart,
    axis_chrome,
    axis_label_w,
    chart_span,
    timeline_rows,
)
from .contactlist import SORT_COLUMNS, SORT_OPENS_ASCENDING, ContactListScreen, ContactRow
from .menus import command_label, fit_cells, section_heading
from .scopering import ScopeKey, ring, scope_atom, scope_chip, scope_key, step
from .theme import name_style, snr_style
from .tui.render import render_lines
from .tui.screen import CANCEL, Screen
from .tui.select import Choice, Separator
from .widgets import (
    ContactsSort,
    _recency_style,
    age_seconds,
    body_heading,
    format_ago,
    highlighted_hash,
)

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.models import HeardNode
    from ..services.trace_runner import NodeResolver

#: All the history time windows that the screen knows (``None`` = all that was ever
#: recorded). A session offers the time windows of the platform, in :data:`_WINDOWS` below.
_ALL_WINDOWS: tuple[tuple[str, timedelta | None], ...] = (
    ("24 h", timedelta(days=1)),
    ("7 d", timedelta(days=7)),
    ("30 d", timedelta(days=30)),
    ("all time", None),
)

#: The offered time windows. On a handheld (a platform with an F-key lane), the ring stops
#: at 30 d (JP decided this for the PicoCalc on 2026-08-08).
#: All time is the one span whose scan grows with the full history, so it stays only on
#: the desktop. On a handheld, the F3 chip cycles the three spans that remain. Bound
#: when the platform changes.
_WINDOWS: tuple[tuple[str, timedelta | None], ...] = _ALL_WINDOWS


@on_platform
def _bind_windows(platform: Platform) -> None:
    """Bind the ring of offered time windows to the platform (runs now and at each change)."""
    global _WINDOWS
    _WINDOWS = _ALL_WINDOWS[:3] if platform.footer_fkeys else _ALL_WINDOWS


#: The picker sentinel for the whole-mesh overview page.
MESH = ("mesh",)

#: The picker sentinel for the page of our node (the outbound ledger, refer to
#: :func:`_self_sections`).
SELF = ("self",)

#: The height of the volume and rhythm charts, in braille rows.
_CHART_ROWS = 2

#: The height of the SNR band, in braille rows. It is one more than the volume charts,
#: because the zero anchor (readings hang below the grey zero line by their real depth)
#: uses some dots to show the true values, and the extra row gives that range back.
_SNR_ROWS = 3


def bucketize(stamps: list[datetime], start: datetime, end: datetime, buckets: int) -> list[int]:
    """Put the timestamps into ``buckets`` equal slices of ``[start, end]``, oldest first."""
    counts = [0] * max(1, buckets)
    span = max(1.0, (end - start).total_seconds())
    for stamp in stamps:
        index = int((stamp - start).total_seconds() / span * buckets)
        counts[min(buckets - 1, max(0, index))] += 1
    return counts


#: The candidate slice widths of the Rhythm chart in minutes, finest first. The chart
#: uses the finest width whose full day fits the screen width (JP, 2026-08-09). A fixed
#: 15-minute slice was once too wide for the 53 columns of the PicoCalc. Now the standard
#: desktop keeps its 15-minute sweep (48 cells and the two gutters). The console uses a
#: coarser slice only as much as its width makes necessary, and a terminal that is wide
#: enough gets the finer sweeps. If the hour chart is also too wide, it draws anyway, as
#: the spec says.
_RHYTHM_SLICES: tuple[int, ...] = (1, 5, 10, 15, 20, 30, 60)


def _slice_note(minutes: int) -> str:
    """The heading atom for the rhythm slice width: ``20-min slices``, ``1 h slices``."""
    return "1 h slices" if minutes >= 60 else f"{minutes}-min slices"


def _rhythm_heading(what: str, minutes: int, width: int) -> Text:
    """The Rhythm heading: what it counts, then its slice width when the line has space.

    The slice width is the part that the user needs least, because the axis below the
    chart marks the hours of the day. Thus when the full heading is too wide for one line
    (on the 53 columns of the PicoCalc and the Cardputer, JP 2026-10-04), the slice width
    is removed, and the rest stays on one row.
    """
    heading = body_heading("Rhythm", f"{what} · {_slice_note(minutes)}")
    return heading if heading.cell_len <= width else body_heading("Rhythm", what)


def _rhythm_slots(stamps: list[datetime], minutes: int) -> list[int]:
    """Put the timestamps into their local time-of-day slice, with ``minutes`` for each slot."""
    slots = [0] * (24 * 60 // minutes)
    for stamp in stamps:
        local = stamp.astimezone()
        slots[(local.hour * 60 + local.minute) // minutes] += 1
    return slots


def _fold_slots(minute_grid: list[int], minutes: int) -> list[int]:
    """Put the minute-of-day base grid of the repository into slices of ``minutes`` width.

    A minute divides each step of :data:`_RHYTHM_SLICES`. Thus the mesh page slices the
    one :meth:`~meshterm.persistence.repository.Repository.rhythm_activity` scan again on
    the client side, and does not query again for each candidate width.
    """
    per = max(1, minutes)
    return [sum(minute_grid[i : i + per]) for i in range(0, len(minute_grid), per)]


def _fit_rhythm(
    slots_at: Callable[[int], list[int]], width: int, base_w: int
) -> tuple[list[int], int, int]:
    """Select the finest rhythm slice whose chart fits ``width``: ``(slots, minutes, label_w)``.

    ``base_w`` is the gutter width that the other charts of the section already use. The
    scale of the rhythm is added to it (the tally of a slice can be more than the tally of
    one volume bucket). The fit is measured against the row that a chart draws: the two
    gutters as the platform draws them (:func:`~meshterm.ui.braillechart.axis_chrome`),
    and the cells. The coarsest slice is returned also when it does not fit. The ladder
    has no further step, and a clipped hour chart is better than no rhythm.
    """
    slots: list[int] = [0]
    label_w = base_w
    for minutes in _RHYTHM_SLICES:
        slots = slots_at(minutes)
        label_w = max(base_w, axis_label_w(max(slots), _CHART_ROWS))
        if axis_chrome(label_w) + len(slots) // 2 <= width:
            return slots, minutes, label_w
    return slots, _RHYTHM_SLICES[-1], label_w


def bucket_medians(
    pairs: list[tuple[datetime, float]], start: datetime, end: datetime, buckets: int
) -> list[float | None]:
    """The median of the timestamped readings in each bucket (``None`` for an empty bucket)."""
    grouped: list[list[float]] = [[] for _ in range(max(1, buckets))]
    span = max(1.0, (end - start).total_seconds())
    for stamp, value in pairs:
        index = int((stamp - start).total_seconds() / span * buckets)
        grouped[min(buckets - 1, max(0, index))].append(value)
    return [median(values) if values else None for values in grouped]


def _snr_cell_style(values: list[float]) -> str:
    """Colour one SNR band cell by the quality of its readings (the shared SNR palette)."""
    return snr_style(sum(values) / len(values))


def _time_axis(start: datetime, end: datetime) -> Callable[[float], str]:
    """A compact axis labeller over a real time span, which ends on ``now``.

    A time window wider than two days shows its marks as bare dates (``Jul 4``). A
    narrower one, where the calendar day almost does not change, shows them as times
    (``18:30``). This is the same "shorten what you can" rule that the dates of the day
    charts use. Thus the volume and SNR axes stay legible, and do not repeat a full
    ``Jul 04 18:30`` stamp at each tick.
    """
    wide = (end - start).total_seconds() > 2 * 86400

    def label_at(frac: float) -> str:
        if frac >= 1.0:
            return "now"
        when = (start + (end - start) * frac).astimezone()
        return f"{when:%b} {when.day}" if wide else f"{when:%H:%M}"

    return label_at


def _quarter_axis(frac: float) -> str:
    """The labeller of the rhythm charts: the local hour at ``frac`` of a full-day sweep.

    Whatever slice width the ladder selected, the slices go from midnight to midnight.
    Thus the fraction maps onto the full ``0 → 24 h`` day (the right edge ends on
    ``24 h``), and the intermediate marks fall on clean six-hour boundaries.
    """
    return f"{round(frac * 24)} h"


class TimeMachineScreen(Screen):
    """The history page of one subject: scrollable sections, and ``w`` cycles the time window.

    A page whose history has a scope (the page of the whole mesh) also cycles the scope
    on ``s``.
    """

    floating = False

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The keys of the page. ``s scope`` shows only when there is a scope to narrow to."""
        scope = " · s scope" if len(self._ring()) > 1 else ""
        return f"↑↓ PgUp/PgDn scroll · w window{scope} · Esc back"

    @property
    def picocalc_lyra_lane(self):
        """The shared pager, and the time window cycle on F3.

        ``w`` is the main purpose of this screen: the same history at several spans. It
        is exactly the type of affordance that is lost on a platform with no hint line to
        show it. On the PicoCalc, the chip is the only place where the user can learn that
        the key exists. Thus it gets the free F3 slot, although the letter itself is easy
        to press. (We tried one chip for each span on F1–F3, then removed them. JP,
        2026-08-09: the cycle was better.)

        The chip names the span that a press *goes to*, not the span on the screen. The
        title already shows where the user is. A chip that repeats it makes the same
        claim two times, and never tells the user what a press does. The chip has only
        the span, with no ``▸`` before it (JP, 2026-10-04), so it has all six cells.
        ``all time`` still shortens to ``all``. It is the only span whose name does not
        fit, and the ring of this platform may not offer it (refer to
        :func:`_bind_windows`).
        """
        from .tui.fkeys import FPair, default_lane

        lane = list(default_lane(nav=self.content_overflows))
        nxt, _delta = _WINDOWS[(self._window_index + 1) % len(_WINDOWS)]
        lane[2] = FPair("all" if nxt == "all time" else nxt, "window")
        # The scope cycle is next to it, with the same rule: the chip names where a press
        # goes.
        views = self._ring()
        if len(views) > 1:
            lane[1] = FPair(scope_chip(step(views, self._scope)), "scope")
        return lane

    def __init__(
        self,
        *,
        session,  # noqa: ANN001 - TuiSession, imported late to prevent an import cycle
        label: str,
        build: Callable[[timedelta | None, int, ScopeKey | None], list[RenderableType]],
        scopes: Callable[[timedelta | None], set[ScopeKey]] | None = None,
    ) -> None:
        """Create the page over its section builder.

        Args:
            session: The running TUI session (to paint again when the time window
                changes).
            label: The display name of the subject (it is the title of the screen).
            build: Renders the sections for ``(window, width, scope)``. It is called one
                time for each combination, and the result is cached. The data is stored
                history, so no query is necessary again at each paint. ``scope`` is
                ``None`` for all packets.
            scopes: The scope views that the history of a time window holds, for a page
                that can narrow to one of them. ``None`` offers no scope cycle.
        """
        super().__init__()
        self._session = session
        self._label = label
        self._build = build
        self._scopes = scopes
        self._window_index = 1  # open on 7 d: enough depth to show a shape, and still fast
        #: The scope view on the screen. ``None`` is all packets.
        self._scope: ScopeKey | None = None
        self._cache: dict[tuple[int, int, ScopeKey | None], list[str]] = {}
        self._set_title()

    def _ring(self) -> list[ScopeKey | None]:
        """The scope views offered in the current time window (only ``[None]`` if none)."""
        if self._scopes is None:
            return [None]
        _name, delta = _WINDOWS[self._window_index]
        return ring(self._scopes(delta), self._scope)

    def _set_title(self) -> None:
        name, _delta = _WINDOWS[self._window_index]
        self.title = f"{self._label} · {name}"
        self.short_title = ""
        if self._scope is not None:
            self.title += f" · {scope_atom(self._scope)}"
            self.short_title = f"{self._label} · {name} · {scope_atom(self._scope, bare=True)}"

    def handle(self, action: str, data: str = "") -> None:
        """Scroll, cycle the time window, or leave.

        The time window cycles on the ``w`` key or on the ``window`` action that the F-key
        lane dispatches. It is one behaviour with two ways in, so the chip is not a second
        implementation of the letter. The ring that it cycles is the ring of the platform
        (refer to :func:`_bind_windows`).
        """
        if action == "up":
            self.scroll_lines(-1)
        elif action == "down":
            self.scroll_lines(1)
        elif action == "pageup":
            self.scroll_pages(-1)
        elif action in ("pagedown", "space"):
            self.scroll_pages(1)
        elif action in ("home", "ctrl_home"):
            self.scroll_to_top()
        elif action in ("end", "ctrl_end"):
            self.scroll_to_bottom()
        elif action == "window" or (action == "text" and data.lower() == "w"):
            self._window_index = (self._window_index + 1) % len(_WINDOWS)
            self._set_title()
            self.scroll_to_top()
            self._session.invalidate()
        elif action == "scope" or (action == "text" and data.lower() == "s"):
            views = self._ring()
            if len(views) > 1:
                self._scope = step(views, self._scope)
                self._set_title()
                self.scroll_to_top()
                self._session.invalidate()
        elif action == "escape":
            self.resolve(None)

    def render_body(self, width: int) -> list[str]:
        """Render (or use again) the sections of the current time window."""
        key = (self._window_index, width, self._scope)
        lines = self._cache.get(key)
        if lines is None:
            _name, delta = _WINDOWS[self._window_index]
            lines = render_lines(Group(*self._build(delta, width, self._scope)), width)
            self._cache[key] = lines
        self._scroll_total = max(1, len(lines))
        return lines


# --- the node page -----------------------------------------------------------------------


def _when_label(when: datetime) -> str:
    """A compact local timestamp for the chart captions: ``Jul 04 18:30``."""
    return when.astimezone().strftime("%b %d %H:%M")


def _node_sections(
    ctx: AppContext, node_id: str, label: str, window: timedelta | None, width: int
) -> list[RenderableType]:
    """Build the history page of one node: volume, SNR band, rhythm, and the roll-up."""
    now = utcnow()
    since = now - window if window is not None else None
    observations = ctx.repo.node_observations(node_id, since=since)
    if not observations:
        return [
            Text(),
            Text("Nothing recorded in this window.", style="muted"),
            Text("Press w to widen it.", style="muted"),
        ]
    start = since or observations[0].observed_at
    stamps = [o.observed_at for o in observations]
    snr_pairs = [(o.observed_at, float(o.snr)) for o in observations if o.snr is not None]

    # Volume, SNR, and the rhythm share one y-axis gutter width (the same as the charts
    # of the mesh page), so that their left edges align. A provisional width finds the
    # peaks that set the gutter size. Then the real width puts Volume and SNR into
    # buckets again, flush with the gutter. The rhythm puts each reception into local
    # time-of-day slices at the finest width that the ladder fits (refer to
    # :data:`_RHYTHM_SLICES`). The tally of a slice can be more than the tally of one
    # volume bucket, so its peak is also part of the size calculation.
    def _layout(label_w: int) -> tuple[int, int]:
        # A chart row uses axis_chrome(label_w) next to its cells: the two gutters, as
        # the platform draws them (the desktop mirrors its marks, the console does not).
        chars = max(20, width - axis_chrome(label_w))
        return chars, chars * 2

    chars, buckets = _layout(1)
    volume = bucketize(stamps, start, now, buckets)
    lo, hi = chart_span(bucket_medians(snr_pairs, start, now, buckets)) if snr_pairs else (0.0, 0.0)
    base_w = max(axis_label_w(max(volume), _CHART_ROWS), axis_label_w(hi, _SNR_ROWS, lo=lo))
    slots, slice_minutes, label_w = _fit_rhythm(
        lambda minutes: _rhythm_slots(stamps, minutes), width, base_w
    )
    chars, buckets = _layout(label_w)
    volume = bucketize(stamps, start, now, buckets)

    out: list[RenderableType] = []
    out.append(body_heading("Volume", f"{len(observations)} receptions"))
    out.extend(
        axis_chart(
            timeline_rows(volume, rows=_CHART_ROWS),
            max(volume),
            chars,
            _time_axis(start, now),
            label_w=label_w,
        )
    )

    if snr_pairs:
        medians = bucket_medians(snr_pairs, start, now, buckets)
        lo, hi = chart_span(medians)
        rows = timeline_rows(medians, rows=_SNR_ROWS, style=_snr_cell_style)
        out.append(Text())
        out.append(body_heading("SNR", "median dB per slice · grey line = 0"))
        out.extend(
            axis_chart(
                rows,
                hi,
                chars,
                _time_axis(start, now),
                label_w=label_w,
                floor=lo,
            )
        )

    out.append(Text())
    out.append(_rhythm_heading("receptions by local time of day", slice_minutes, width))
    out.extend(
        axis_chart(
            timeline_rows(slots, rows=_CHART_ROWS),
            max(slots),
            len(slots) // 2,
            _quarter_axis,
            label_w=label_w,
        )
    )

    out.append(Text())
    out.append(body_heading("Record", "this window"))
    first, last = observations[0].observed_at, observations[-1].observed_at
    line = Text("heard    ", style="muted")
    line.append(f"first {_when_label(first)} · last {_when_label(last)}")
    line.append(f"  ({format_ago(age_seconds(last))})", style="muted")
    out.append(line)
    snrs = [p[1] for p in snr_pairs]
    if snrs:
        med = median(snrs)
        line = Text("snr      ", style="muted")
        line.append(f"{med:+.1f} dB median", style=snr_style(med))
        line.append(f"  ·  worst {min(snrs):+.1f} · best {max(snrs):+.1f}", style="muted")
        out.append(line)
    rssis = [o.rssi for o in observations if o.rssi is not None]
    if rssis:
        out.append(
            Text.assemble(
                ("rssi     ", "muted"),
                (f"{median(rssis):.0f} dBm median", ""),
                (f"  ·  weakest {min(rssis):.0f} · strongest {max(rssis):.0f}", "muted"),
            )
        )
    kinds: dict[str, int] = {}
    for o in observations:
        kinds[o.kind] = kinds.get(o.kind, 0) + 1
    parts = " · ".join(f"{kind} {count}" for kind, count in sorted(kinds.items()))
    out.append(Text.assemble(("kinds    ", "muted"), (parts, "")))
    return out


# --- the page of our node ----------------------------------------------------------------


def _self_sections(ctx: AppContext, window: timedelta | None, width: int) -> list[RenderableType]:
    """Build the page of our node: transmission volume, reach SNR band, rhythm, and ledger.

    Our node is the one subject that the reception history cannot describe, because our
    node never hears itself. Thus this page mirrors :func:`_node_sections` over the
    *outbound* record instead:

    * **Activity** is what our node puts on the air (each trace that it started and each
      message that it sent), instead of what it heard.
    * **Reach** charts the bottleneck SNR of our traces (how strongly our node gets out),
      where the node page charts the SNR of a heard node.
    * The **Rhythm** has the same shape: when does *our node* transmit?
    * The **Ledger** gives the tallies.

    The page shares the chart code of the node page and one common y-axis gutter. Thus
    the two pages look like the same instrument, pointed in opposite directions.
    """
    now = utcnow()
    since = now - window if window is not None else None
    stamps = ctx.repo.self_transmissions(since=since)
    reach = ctx.repo.self_trace_reach(since=since)
    ledger = ctx.repo.self_activity_ledger(since=since)
    if not stamps:
        return [
            Text(),
            Text("Nothing sent in this window.", style="muted"),
            Text("Press w to widen it.", style="muted"),
        ]
    start = since or stamps[0]
    snr_pairs = [(when, float(snr)) for when, ok, snr, _hops in reach if ok and snr is not None]

    # The rhythm puts each transmission into local time-of-day slices at the finest
    # width that the ladder fits (refer to :data:`_RHYTHM_SLICES`). A busy slice can be
    # more than one volume bucket, so its peak is also part of the size calculation of
    # the shared gutter (refer to _node_sections for the same steps: a provisional
    # width, then the real width).
    def _layout(label_w: int) -> tuple[int, int]:
        # A chart row uses axis_chrome(label_w) next to its cells: the two gutters, as
        # the platform draws them (the desktop mirrors its marks, the console does not).
        chars = max(20, width - axis_chrome(label_w))
        return chars, chars * 2

    chars, buckets = _layout(1)
    volume = bucketize(stamps, start, now, buckets)
    lo, hi = chart_span(bucket_medians(snr_pairs, start, now, buckets)) if snr_pairs else (0.0, 0.0)
    base_w = max(axis_label_w(max(volume), _CHART_ROWS), axis_label_w(hi, _SNR_ROWS, lo=lo))
    slots, slice_minutes, label_w = _fit_rhythm(
        lambda minutes: _rhythm_slots(stamps, minutes), width, base_w
    )
    chars, buckets = _layout(label_w)
    volume = bucketize(stamps, start, now, buckets)

    out: list[RenderableType] = []
    out.append(body_heading("Activity", f"{len(stamps)} sent · traces + messages"))
    out.extend(
        axis_chart(
            timeline_rows(volume, rows=_CHART_ROWS),
            max(volume),
            chars,
            _time_axis(start, now),
            label_w=label_w,
        )
    )

    if snr_pairs:
        medians = bucket_medians(snr_pairs, start, now, buckets)
        lo, hi = chart_span(medians)
        rows = timeline_rows(medians, rows=_SNR_ROWS, style=_snr_cell_style)
        out.append(Text())
        out.append(body_heading("Reach", "trace bottleneck dB per slice · grey line = 0"))
        out.extend(
            axis_chart(
                rows,
                hi,
                chars,
                _time_axis(start, now),
                label_w=label_w,
                floor=lo,
            )
        )

    out.append(Text())
    out.append(_rhythm_heading("transmissions by local time of day", slice_minutes, width))
    out.extend(
        axis_chart(
            timeline_rows(slots, rows=_CHART_ROWS),
            max(slots),
            len(slots) // 2,
            _quarter_axis,
            label_w=label_w,
        )
    )

    out.append(Text())
    out.append(body_heading("Ledger", "this window"))
    first, last = stamps[0], stamps[-1]
    line = Text("active   ", style="muted")
    line.append(f"first {_when_label(first)} · last {_when_label(last)}")
    line.append(f"  ({format_ago(age_seconds(last))})", style="muted")
    out.append(line)

    if ledger.trace_total:
        pct = round(100 * ledger.trace_ok / ledger.trace_total)
        hops = [h for _w, ok, _s, h in reach if ok and h is not None]
        line = Text("reach    ", style="muted")
        line.append(f"{ledger.trace_ok} of {ledger.trace_total} traces home")
        line.append(f" ({pct}%)", style="muted")
        if ledger.trace_targets:
            line.append(f"  ·  {ledger.trace_targets} targets", style="muted")
        if hops:
            line.append(f" · median {round(median(hops))} hops", style="muted")
        out.append(line)
        if snr_pairs:
            snrs = [s for _w, s in snr_pairs]
            med = median(snrs)
            line = Text("snr      ", style="muted")
            line.append(f"{med:+.1f} dB median", style=snr_style(med))
            line.append(f"  ·  worst {min(snrs):+.1f} · best {max(snrs):+.1f}", style="muted")
            out.append(line)

    sent = _self_sent_line(ledger)
    if sent is not None:
        out.append(sent)
    if ledger.tx_samples:
        out.append(
            Text.assemble(
                ("tx       ", "muted"),
                (f"{ledger.tx_samples} power samples", ""),
                ("  ·  reach vs. transmit power", "muted"),
            )
        )
    return out


def _self_sent_line(ledger) -> Text | None:  # noqa: ANN001 - SelfActivity, kept local
    """The Ledger line for sent messages: channel and direct counts, and the DM ack rate.

    The line is not shown (``None``) when nothing was sent in the time window. Thus a
    time window with only traces does not show an empty ``sent`` lane. Channel broadcasts
    are never acked, so the ack fraction is given only over the direct messages whose
    ack was tracked (:attr:`~meshterm.core.models.SelfActivity.dm_ackable`).
    """
    if not (ledger.msg_channel or ledger.msg_dm):
        return None
    line = Text("sent     ", style="muted")
    parts: list[str] = []
    if ledger.msg_channel:
        parts.append(f"{ledger.msg_channel} channel")
    if ledger.msg_dm:
        parts.append(f"{ledger.msg_dm} direct")
    line.append(" · ".join(parts))
    if ledger.dm_ackable:
        pct = round(100 * ledger.dm_acked / ledger.dm_ackable)
        line.append(f"  ·  {ledger.dm_acked}/{ledger.dm_ackable} acked ({pct}%)", style="muted")
    if ledger.dm_peers:
        line.append(f"  ·  {ledger.dm_peers} peers", style="muted")
    return line


# --- the mesh page -----------------------------------------------------------------------


def _day_spans(days: int, chars: int) -> list[tuple[int, int]]:
    """The lit bar ``(start, width)`` of each day, in the ``2 × chars`` dots of the chart.

    This is the one even split that the bars (:func:`_day_columns`) and their axis ticks
    (:func:`_day_centers`) both use. When each bar can keep at least two dots, each of
    the ``n − 1`` day *boundaries* gives one notch dot that no bar uses. The bars divide
    the rest with a floor-edge walk. Thus no two bars differ by more than one dot
    column, and the wider days are spread evenly among the narrower days, and do not
    collect at one end. The notches are only between days. The first bar starts flush
    at dot 0, because the axis border is enough separation. (A blank dot at the start
    doubles the left margin that the braille glyphs already have.) The last bar
    ends flush at the right border, so the two edges look the same. A denser history
    has no notches, and the bars fill all ``2 × chars`` dots.

    Args:
        days: How many day bars the chart draws.
        chars: The width of the chart in character cells.

    Returns:
        One ``(start dot, width in dots)`` for each lit bar, oldest first.
    """
    dots = chars * 2
    n = max(1, days)
    lit = dots - (n - 1)  # what the bars keep after each boundary gives its notch dot
    notch = n > 1 and lit // n >= 2
    if not notch:
        lit = dots
    edges = [i * lit // n for i in range(n + 1)]
    return [(edges[i] + (i if notch else 0), edges[i + 1] - edges[i]) for i in range(n)]


def _day_centers(days: int, chars: int) -> list[int]:
    """The chart cell at the centre of the bar of each day, the same as :func:`_day_columns`.

    This function reads the same :func:`_day_spans` split that ``_day_columns`` draws
    with. Thus a tick at ``centers[i]`` is below the bar of day ``i``, and not at a
    random fraction of the axis.

    Args:
        days: How many day bars the chart draws.
        chars: The width of the chart in character cells.

    Returns:
        One centre cell (``0 .. chars - 1``) for each day, oldest first.
    """
    return [min(chars - 1, (start + width // 2) // 2) for start, width in _day_spans(days, chars)]


def _even_picks(n: int, k: int) -> list[int]:
    """``k`` bar indices out of ``n``, spread evenly, with the two ends included.

    The endpoints are always index ``0`` and ``n − 1``. The interior indices are at the
    even fractions between them. When ``k`` is near ``n`` on a short axis, the rounding
    can put two indices on one bar. Thus the result has no duplicates, and can have fewer
    than ``k`` indices. The caller takes that to mean "``k`` does not fit", and tries a
    smaller ``k``.

    Args:
        n: How many bars the chart draws.
        k: How many ticks to spread across them (``1 .. n``).

    Returns:
        The selected bar indices, in ascending order.
    """
    if n <= 0:
        return []
    if k <= 1:
        return [0]
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})


def _ticks_fit(picks: list[int], labels: list[str], centers: list[int], chars: int) -> bool:
    """Whether the ``labels`` under the ``picks`` do not touch each other on the axis.

    This function does the exact placement of :func:`~meshterm.ui.braillechart._tick_axis`
    again. Each label is centred on the cell of its bar and clamped into the axis, and
    it must start :data:`~meshterm.ui.braillechart._TICK_GAP` cells after the end of the
    previous label. The function reports whether all the labels stay. The purpose is to
    measure the real labels (a bare ``"7"`` is a third of the width of ``"Jul 12"``). A
    fixed worst-case budget for each label removes ticks for which a variable-width
    axis has a lot of space.
    """
    last_end = -_TICK_GAP
    for i, label in zip(picks, labels, strict=True):
        cell = max(0, min(chars - 1, centers[i]))
        start = max(0, min(chars - len(label), cell - len(label) // 2))
        if start < last_end + _TICK_GAP:
            return False
        last_end = start + len(label)
    return True


def _fit_ticks(
    n: int, chars: int, centers: list[int], label_of: Callable[[list[int]], list[str]]
) -> list[tuple[int, str]]:
    """``(cell, label)`` ticks: as many bars as the *real* labels have space for.

    The function tries all the bars first. Then it decreases the tick count until an
    evenly spread subset (:func:`_even_picks`) passes the collision rule
    (:func:`_ticks_fit`) for the labels that it draws. The two ends keep a label at
    each count. The labels are built again for each candidate set (day numbers stay
    bare until a new month makes ``"Jul 1"`` necessary). Thus the fit uses their true
    widths, and does not reserve the width of the longest label for all of them.

    Args:
        n: How many bars the chart draws.
        chars: The width of the chart in character cells.
        centers: The centre cell of each bar (refer to :func:`_day_centers`).
        label_of: Builds the labels for a set of selected bar indices, in context (so
            that new months and the last ``today``/``now`` are at the correct place).

    Returns:
        The ticks to give to :func:`~meshterm.ui.braillechart.axis_chart`.
    """
    if n <= 0:
        return []
    for k in range(min(n, chars), 1, -1):
        picks = _even_picks(n, k)
        if len(picks) < k:
            continue  # the rounding joined two indices, so this count does not fit cleanly
        labels = label_of(picks)
        if _ticks_fit(picks, labels, centers, chars):
            return list(zip((centers[i] for i in picks), labels, strict=True))
    picks = [0] if n == 1 else [0, n - 1]
    return list(zip((centers[i] for i in picks), label_of(picks), strict=True))


def _day_ticks(shown: list, chars: int) -> list[tuple[int, str]]:
    """``(cell, label)`` axis ticks below the day bars: short dates, fewer to fit.

    There is one tick for each day when no labels touch. If not, there is an evenly
    spaced subset that keeps the two ends (refer to :func:`_fit_ticks`). Each label is
    below its own bar (refer to :func:`_day_centers`). The dates stay compact. The month
    shows only on the first tick and at each new month, so most ticks show a bare day
    number. The newest bar shows ``today`` when it is today, the same as the last
    ``now`` of the node page.

    Args:
        shown: The charted days, oldest first, each ``(iso_date, ...)``.
        chars: The width of the chart in character cells.

    Returns:
        The ticks to give to :func:`~meshterm.ui.braillechart.axis_chart`.
    """
    n = len(shown)
    centers = _day_centers(n, chars)
    # The day keys are local calendar days (refer to Repository.daily_activity), so
    # "today" is also the local date. datetime.now() is naive local time, which is
    # exactly what we compare.
    today = datetime.now().strftime("%Y-%m-%d")

    def label_of(picks: list[int]) -> list[str]:
        labels: list[str] = []
        prev_month: str | None = None
        for i in picks:
            iso = shown[i][0]
            try:
                day = datetime.strptime(iso, "%Y-%m-%d")
            except ValueError:
                labels.append(iso)
                continue
            month = day.strftime("%b")
            if i == n - 1 and iso == today:
                label = "today"
            elif month != prev_month:
                label = f"{month} {day.day}"
            else:
                label = str(day.day)
            prev_month = month
            labels.append(label)
        return labels

    return _fit_ticks(n, chars, centers, label_of)


def _hour_ticks(shown: list, chars: int) -> list[tuple[int, str]]:
    """``(cell, label)`` axis ticks below the hourly bars: local clock times, fewer to fit.

    This is the hour-resolution form of :func:`_day_ticks`, for the 24 h time window.
    There is one tick for each hour when the labels fit. If not, there is an evenly
    spaced subset that keeps the two ends (refer to :func:`_fit_ticks`). Each label is
    centred below its own bar (refer to :func:`_day_centers`). The hour keys are already
    local (refer to :meth:`~meshterm.persistence.repository.Repository.hourly_series`),
    so a label is the ``HH:00`` of its key, with no conversion. The newest bar shows
    ``now``, the same as the last ``now`` of the node page.

    Args:
        shown: The charted hours, oldest first, each ``(hour_iso, ...)`` a local
            ``YYYY-MM-DDTHH``.
        chars: The width of the chart in character cells.

    Returns:
        The ticks to give to :func:`~meshterm.ui.braillechart.axis_chart`.
    """
    n = len(shown)
    centers = _day_centers(n, chars)

    def label_of(picks: list[int]) -> list[str]:
        labels: list[str] = []
        for i in picks:
            if i == n - 1:
                labels.append("now")
                continue
            try:
                hour = datetime.strptime(shown[i][0], "%Y-%m-%dT%H")
            except ValueError:
                labels.append(shown[i][0])
                continue
            labels.append(f"{hour:%H:00}")
        return labels

    return _fit_ticks(n, chars, centers, label_of)


def _fill_days(
    active: list[tuple[str, int, int]], since: datetime | None, now: datetime
) -> list[tuple[str, int, int]]:
    """Fill the gaps of a day series, so that an empty day shows as an empty bar, not a skip.

    ``daily_activity`` returns only days with traffic. Thus a quiet day was once not
    shown, and its busy neighbours joined: two weeks with two empty days looked like
    twelve adjacent bars. This function walks each calendar day of the time window, and
    adds ``(iso, 0, 0)`` for the quiet days. Thus the x-axis of the chart is real
    calendar time. The range goes from the start of the time window through today. But
    it never starts before the first day ever recorded, because we do not invent empty
    days from before the monitoring started.

    Args:
        active: ``(iso_day, packets, nodes)`` for days with activity, oldest first.
        since: The start of the time window (``None`` = all of the history).
        now: The current time (the series ends on its calendar day).

    Returns:
        ``(iso_day, packets, nodes)`` for each calendar day in the range, oldest first.
    """
    if not active:
        return []
    by_iso = {iso: (packets, nodes) for iso, packets, nodes in active}
    # The keys are local calendar days, so the fill must also stop at local dates.
    # .date() on the raw UTC-aware ``since``/``now`` gives a day too early in western
    # zones.
    first = date.fromisoformat(active[0][0])
    floor = since.astimezone().date() if since is not None else first
    start = max(first, floor)
    end = max(start, now.astimezone().date())
    out: list[tuple[str, int, int]] = []
    day = start
    while day <= end:
        iso = day.isoformat()
        packets, nodes = by_iso.get(iso, (0, 0))
        out.append((iso, packets, nodes))
        day += timedelta(days=1)
    return out


def _fill_hours(
    active: list[tuple[str, int, int]], since: datetime, now: datetime
) -> list[tuple[str, int, int]]:
    """Fill the gaps of an hour series, so a quiet hour shows as an empty bar, not a skip.

    This is the hour-resolution form of :func:`_fill_days`, for the charts of the 24 h
    time window. :meth:`~meshterm.persistence.repository.Repository.hourly_series`
    returns only hours with traffic, so without a fill, a quiet hour joins its busy
    neighbours. This function walks each local clock hour of the time window, and adds
    ``(iso, 0, 0)`` for the quiet hours. Thus the x-axis is real clock time. The range
    goes from the start of the time window through the current hour. But it never starts
    before the first hour recorded, because we do not invent empty hours from before the
    monitoring started.

    The keys are local wall-clock ``YYYY-MM-DDTHH`` (the same as the SQL grouping), so
    the walk steps a *naive* local clock. When the walk adds an hour, the wall clock
    advances, and this is safe for DST. A spring-forward gap fills as an empty bar, and
    a fall-back repeat adds into one key, exactly as the grouping already did.

    Args:
        active: ``(hour_iso, packets, nodes)`` for hours with activity, oldest first,
            each ``hour_iso`` a local ``YYYY-MM-DDTHH``.
        since: The start of the time window.
        now: The current time (the series ends on its clock hour).

    Returns:
        ``(hour_iso, packets, nodes)`` for each local clock hour in the range, oldest
        first.
    """
    if not active:
        return []

    def floor_hour(when: datetime) -> datetime:
        return when.astimezone().replace(tzinfo=None, minute=0, second=0, microsecond=0)

    by_iso = {iso: (packets, nodes) for iso, packets, nodes in active}
    first = datetime.strptime(active[0][0], "%Y-%m-%dT%H")
    start = max(first, floor_hour(since))
    end = max(start, floor_hour(now))
    out: list[tuple[str, int, int]] = []
    hour = start
    while hour <= end:
        iso = hour.strftime("%Y-%m-%dT%H")
        packets, nodes = by_iso.get(iso, (0, 0))
        out.append((iso, packets, nodes))
        hour += timedelta(hours=1)
    return out


def _day_columns(values: list[int], chars: int) -> list:
    """Stretch the daily counts into day-wide bars that fill the chart width exactly.

    With one dot column for each day, a short history is a thin strip in a wide
    terminal. That is less than how all the other MeshTerm charts use their width. Thus
    each day repeats over its :func:`_day_spans` share of the dot columns of the chart
    instead. This is an even split: the widths never differ by more than one dot, and
    their total is always exactly ``2 × chars``. (A plain floor division removes the
    remainder. Then the bars stop before the axis border, and the caption has the size
    of the full width. The user then sees the full chart as if it moved to the left.)

    The dots that the split does not use (one for each day *boundary*, when the bars
    are wide enough, refer to :func:`_day_spans`) render as
    :data:`~meshterm.ui.braillechart.GAP` columns, blank down to the axis. Thus
    neighbours of the same height show as separate bars, and do not join into one solid
    block. A deeper history (bars of one dot) has no space for a boundary, and the days
    touch.

    Args:
        values: The count for each day, oldest first.
        chars: The width of the chart in character cells.

    Returns:
        Exactly ``2 × chars`` dot-column readings, oldest first (a
        :data:`~meshterm.ui.braillechart.GAP` marks a day-boundary notch).
    """
    out: list = [GAP] * (chars * 2)
    for value, (start, width) in zip(values, _day_spans(len(values), chars), strict=True):
        out[start : start + width] = [value] * width
    return out


def _mesh_sections(
    ctx: AppContext,
    window: timedelta | None,
    width: int,
    prefix_bytes: int = 0,
    resolve: NodeResolver = lambda label: label,
    resolve_key: NodeResolver = lambda node: node,
) -> list[RenderableType]:
    """Build the whole-mesh overview: days, rhythm, arrivals, and the all-time ledger.

    Args:
        ctx: The shared application context (only for repository reads).
        window: The history time window (``None`` = all that was ever recorded).
        width: The render width in cells.
        prefix_bytes: The hash width to light at the start of the key of each arrival
            (0 = none).
        resolve: Names a node hash from the contacts of the device, for arrivals whose
            observations never had a name.
        resolve_key: Expands a stored 12-hex node id to the full public key when the
            device has the node as a contact. Thus the key lane of the arrivals can fill
            the width that the terminal offers (refer to :func:`_contact_resolvers`).
    """
    now = utcnow()
    since = now - window if window is not None else None
    # The 24 h time window charts one hour for each column (its name is "24 h", not
    # "1 day"). Each wider time window keeps the calendar-day columns. Both use the same
    # bar and tick code. Only the bucket resolution and the axis labels are different.
    hourly = since is not None and window is not None and window <= timedelta(days=1)
    # The all-days series also gives the data for the ledger at the bottom. It is read
    # once here (it is one of the more costly scans), and only when necessary in the
    # hourly case.
    all_days: list[tuple[str, int, int]] | None = None
    if hourly:
        series = _fill_hours(ctx.repo.hourly_series(since), since, now)
    else:
        all_days = ctx.repo.daily_activity()
        series = _fill_days(all_days, since, now)
    if not series:
        return [
            Text(),
            Text("Nothing recorded in this window.", style="muted"),
            Text("Press w to widen it.", style="muted"),
        ]

    # The rhythm of the full mesh (charted below) puts the full time window into local
    # time-of-day slices at the finest width that the ladder fits (refer to
    # :data:`_RHYTHM_SLICES`). The slices are made again on the client side from one
    # minute-grid base scan. The tally of a busy slice can be more than the tally of any
    # one day, so its peak and the day peaks together set the size of one shared y-axis
    # gutter. The slices come back already in local time (rotated for each instant in
    # SQL), so no offset change is necessary here.
    minute_grid = ctx.repo.rhythm_activity(since=since)

    # The size of the y-axis gutter comes from the peaks of the full time window (not
    # only the visible slice), and all the charts share it. Thus all their gutters, and
    # their left edges, align. The rhythm keeps its own finer width. Only the gutter is
    # common.
    base_w = max(
        axis_label_w(max(d[1] for d in series), _CHART_ROWS),
        axis_label_w(max(d[2] for d in series), _CHART_ROWS),
    )
    slots, slice_minutes, label_w = _fit_rhythm(
        lambda minutes: _fold_slots(minute_grid, minutes), width, base_w
    )
    chars = max(20, width - axis_chrome(label_w))
    shown = series[-chars * 2 :]
    out: list[RenderableType] = []
    packets = [d[1] for d in shown]
    ticks = _hour_ticks(shown, chars) if hourly else _day_ticks(shown, chars)
    pkt_title, node_title = (
        ("Packets per hour", "Nodes per hour") if hourly else ("Packets per day", "Nodes per day")
    )
    out.append(body_heading(pkt_title, "local hours" if hourly else "local days"))
    out.extend(
        axis_chart(
            timeline_rows(_day_columns(packets, chars), rows=_CHART_ROWS),
            max(packets),
            chars,
            label_w=label_w,
            ticks=ticks,
        )
    )

    nodes = [d[2] for d in shown]
    out.append(Text())
    out.append(body_heading(node_title, "distinct nodes heard"))
    out.extend(
        axis_chart(
            timeline_rows(_day_columns(nodes, chars), rows=_CHART_ROWS),
            max(nodes),
            chars,
            label_w=label_w,
            ticks=ticks,
        )
    )

    # The rhythm chart of the node page, for the full mesh: when does this *mesh* talk?
    # The slices keep their own finer width, but they share the gutter of the day charts.
    # Thus the left edge of this chart aligns with the two charts above it.
    out.append(Text())
    out.append(
        body_heading("Rhythm", f"packets by local time of day · {_slice_note(slice_minutes)}")
    )
    out.extend(
        axis_chart(
            timeline_rows(slots, rows=_CHART_ROWS),
            max(slots),
            len(slots) // 2,
            _quarter_axis,
            label_w=label_w,
        )
    )

    arrivals = ctx.repo.first_seen(since=since)
    # One aggregation gives the data for the key lane of the arrivals and for the node
    # count of the ledger below (each heard node *is* a first-heard node, so the counts
    # agree).
    heard = ctx.repo.heard_nodes()
    out.append(Text())
    out.append(body_heading("Arrivals", "nodes heard for the first time ever"))
    if not arrivals:
        out.append(Text("none in this window", style="muted"))
    else:
        # Aligned lanes below column labels, the same presentation as the picker. The
        # name is in the hue of the node, which comes from its hash (the "unknown" of an
        # arrival with no name stays muted). The key is lit at the routing width, and
        # the age has the colour of its recency heat. The FIRST HEARD header has what
        # each row once repeated. For an arrival with no name, MeshTerm first asks the
        # resolver (the device may know the node as a contact, although its stored
        # observations never had a name). Each key expands to its fullest known form,
        # the same as in the lane of the picker: the key captured with an observation
        # (it shows when the device is offline), or else the contact list of the device,
        # or else the stored 12-hex prefix.
        stored_keys = {n.node: n.public_key for n in heard if n.node}
        listed = [
            (
                stored_keys.get(node) or resolve_key(node) or node,
                _known_name(resolve, node, name),
                first,
            )
            for node, name, first in arrivals[:12]
        ]
        name_w = min(
            _PICK_NAME_MAX,
            max([len("unknown"), *(len(n) for _node, n, _f in listed if n)]),
        )
        # The key lane uses the width that the name lane and the fixed FIRST HEARD tail
        # leave, with the old fixed lane as its minimum. It shows as many whole bytes as
        # fit, with an ellipsis after them on a byte boundary (the contract of
        # highlighted_hash).
        key_w = max(_PICK_HASH_W, width - name_w - _ARRIVAL_TAIL)
        out.append(
            Text(
                "  " + "NAME".ljust(name_w + 2) + "KEY".ljust(key_w + 2) + "FIRST HEARD",
                style="muted",
            )
        )
        for key, name, first in listed:
            secs = age_seconds(first)
            line = Text("  ", no_wrap=True, overflow="ellipsis")
            line.append(
                fit_cells(name or "unknown", name_w),
                style=name_style(name, key) if name else "muted",
            )
            line.append("  ")
            line.append_text(highlighted_hash(key, prefix_bytes, width=key_w, known=bool(name)))
            line.append("  ")
            line.append(_when_label(first))
            line.append(f"  ({format_ago(secs)})", style=_recency_style(secs))
            out.append(line)

    if all_days is None:
        all_days = ctx.repo.daily_activity()
    total_nodes = sum(1 for n in heard if n.node)
    busiest = max(all_days, key=lambda d: d[1])
    out.append(Text())
    out.append(body_heading("Ledger", "everything ever recorded"))
    out.append(
        Text.assemble(
            ("history  ", "muted"),
            (f"{ctx.repo.observation_count()} observations", ""),
            (f" across {len(all_days)} active days · {total_nodes} nodes", "muted"),
        )
    )
    out.append(
        Text.assemble(
            ("busiest  ", "muted"),
            (busiest[0], "brand"),
            (f"  {busiest[1]} packets", "muted"),
        )
    )
    return out


class _MeshFloods:
    """The stored floods of the mesh page for each time window, each with its scope view.

    To name the region of a scoped flood, MeshTerm calculates its transport code again
    under the key of each known region (refer to
    :meth:`~meshterm.core.region_store.RegionStore.scope_of`). Thus the floods of a time
    window are read and resolved once, and kept while the page is open. The ring asks
    which views a time window holds, and a narrowed page asks for the timestamps of one
    view.
    """

    def __init__(self, ctx: AppContext) -> None:
        """Bind to the history and the region names of the context."""
        self._ctx = ctx
        self._by_window: dict[timedelta | None, list[tuple[ScopeKey, datetime]]] = {}

    def _frames(self, window: timedelta | None) -> list[tuple[ScopeKey, datetime]]:
        if window not in self._by_window:
            store = getattr(self._ctx, "region_store", None)
            frames: list[tuple[ScopeKey, datetime]] = []
            if store is not None:
                since = utcnow() - window if window is not None else None
                for when, raw in self._ctx.repo.flood_frames(since=since):
                    key = scope_key(store.scope_of(raw))
                    if key is not None:
                        frames.append((key, when))
            self._by_window[window] = frames
        return self._by_window[window]

    def keys(self, window: timedelta | None) -> set[ScopeKey]:
        """The scope views in which ``window`` holds a flood."""
        return {key for key, _ in self._frames(window)}

    def stamps(self, window: timedelta | None, scope: ScopeKey) -> list[datetime]:
        """When each flood of the time window in ``scope`` was heard, oldest first."""
        return [when for key, when in self._frames(window) if key == scope]


def _floods(count: int) -> str:
    """``1 flood``, ``12 floods``."""
    return f"{count} flood{'' if count == 1 else 's'}"


def _mesh_scope_sections(
    stamps: list[datetime], window: timedelta | None, width: int
) -> list[RenderableType]:
    """The whole-mesh page narrowed to one scope: its floods per day, their rhythm, a ledger.

    These are the day (or hour) bars and the rhythm of the page that is not narrowed, over
    the floods of the scope instead of all the observations. The node counts and the
    arrivals have no narrowed form. A node is known by its adverts, and an advert gives
    no information about the scope in which other traffic was sent. Thus they are not
    shown, instead of showing the values of the whole mesh under the title of a scope.
    """
    now = utcnow()
    since = now - window if window is not None else None
    if not stamps:
        return [
            Text(),
            Text("Nothing recorded in this scope in this window.", style="muted"),
            Text("Press w to widen it, or s for another scope.", style="muted"),
        ]
    hourly = since is not None and window is not None and window <= timedelta(days=1)
    # The same local-time keys that the day and hour series of the repository group by.
    # Thus the fill and tick code below reads them exactly as it reads the keys of the
    # page that is not narrowed.
    per = Counter(
        when.astimezone().strftime("%Y-%m-%dT%H" if hourly else "%Y-%m-%d") for when in stamps
    )
    active = [(iso, count, 0) for iso, count in sorted(per.items())]
    series = _fill_hours(active, since, now) if hourly and since else _fill_days(active, since, now)

    base_w = axis_label_w(max(d[1] for d in series), _CHART_ROWS)
    slots, slice_minutes, label_w = _fit_rhythm(
        lambda minutes: _rhythm_slots(stamps, minutes), width, base_w
    )
    chars = max(20, width - axis_chrome(label_w))
    shown = series[-chars * 2 :]
    floods = [d[1] for d in shown]
    out: list[RenderableType] = []
    out.append(
        body_heading(
            "Floods per hour" if hourly else "Floods per day",
            "local hours" if hourly else "local days",
        )
    )
    out.extend(
        axis_chart(
            timeline_rows(_day_columns(floods, chars), rows=_CHART_ROWS),
            max(floods),
            chars,
            label_w=label_w,
            ticks=_hour_ticks(shown, chars) if hourly else _day_ticks(shown, chars),
        )
    )

    out.append(Text())
    out.append(
        body_heading("Rhythm", f"floods by local time of day · {_slice_note(slice_minutes)}")
    )
    out.extend(
        axis_chart(
            timeline_rows(slots, rows=_CHART_ROWS),
            max(slots),
            len(slots) // 2,
            _quarter_axis,
            label_w=label_w,
        )
    )

    days = Counter(when.astimezone().strftime("%Y-%m-%d") for when in stamps)
    busiest = max(days.items(), key=lambda day: day[1])
    out.append(Text())
    out.append(body_heading("Ledger", "this window"))
    out.append(
        Text.assemble(
            ("history  ", "muted"),
            (_floods(len(stamps)), ""),
            (f" across {len(days)} active day{'' if len(days) == 1 else 's'}", "muted"),
        )
    )
    out.append(
        Text.assemble(
            ("busiest  ", "muted"),
            (busiest[0], "brand"),
            (f"  {_floods(busiest[1])}", "muted"),
        )
    )
    return out


# --- the picker loop ---------------------------------------------------------------------

#: The maximum width of the name lane of the arrivals on the mesh page (longer names get
#: an ellipsis, so that the lanes stay in place). The picker sets the size of its own
#: name lane from its content instead (refer to
#: :meth:`~meshterm.ui.contactlist.ContactListScreen._lane_widths`).
_PICK_NAME_MAX = 18

#: The *minimum* width of the key lane of the arrivals on the mesh page, in cells (for a
#: very narrow terminal). If not at the minimum, the lane fills the width that the name
#: lane and the FIRST HEARD tail leave. Thus a resolved full key shows as many whole
#: bytes as fit (refer to the ``key_w`` calculation above).
_PICK_HASH_W = 16

#: All the content of an arrival row *other than* the name and key lanes, in cells: the
#: 2-cell indent, the two 2-cell lane gaps, the ``Jul 04 18:30`` stamp (12), and the
#: recency note in parentheses (``  (259w ago)`` at its widest, 12).
_ARRIVAL_TAIL = 2 + 2 + 2 + 12 + 12


def _known_name(resolve: NodeResolver, node: str | None, name: str | None) -> str | None:
    """The best display name for a heard node, or ``None`` when it is really unknown.

    This is the rule of the app to fill in the blanks: a node id without a stored name
    is not always unknown, because the contact list of the device may know it. The
    stored name has priority (it is what the node itself last put on the air). The
    resolver fills the blanks. Only a node that neither source can name stays
    ``unknown``.
    """
    if name:
        return name
    if node:
        named = resolve(node)
        if named and named != node:
            return named
    return None


async def _contact_resolvers(
    ctx: AppContext,
) -> tuple[NodeResolver, Callable[[str | None], int | None], NodeResolver]:
    """Three resolvers ``(name, type, key)`` over the contacts, best-effort like the prefix read.

    All three fill the blanks of the picker (and of the arrivals) from what the companion
    knows, but the stored observations never had:

    * The name resolver names a node that the device has as a contact, although its
      history had no name (a sensor that sends only telemetry, a repeater heard before
      it sent an advert).
    * The type resolver gives its node *type* the same way. Thus the glyph at the start
      of the row shows ``▲`` for a repeater, instead of the ``●`` fallback for a plain
      node.
    * The key resolver expands a stored 12-hex prefix to the full public key of the
      contact. Thus the hash lane shows more than the twelve stored digits when there is
      space.

    The contacts are read once, and all three resolvers are built from them. When no
    device is available, all three know nothing, and the stored names, types, and 12-hex
    prefixes are used alone.
    """
    from ..services.trace_runner import (
        make_key_resolver,
        make_node_resolver,
        make_node_type_resolver,
    )

    contacts = []
    try:
        if ctx.is_connected or ctx.settings.connect_on_start:
            contacts = await ctx.devstate.contacts()
    except Exception:  # noqa: BLE001 - an optional read. Without it, only stored names/types show.
        contacts = []
    return (
        make_node_resolver(contacts),
        make_node_type_resolver(contacts),
        make_key_resolver(contacts),
    )


async def _self_identity(ctx: AppContext) -> tuple[str | None, str | None]:
    """Our node's ``(name, public_key)`` for its picker lane, best-effort like the prefix read.

    The page of our node does not need a device, because it reads stored history. Thus
    this read only adds detail to the row. When the device is not available (or the
    firmware does not answer), the name stays a bare ``you`` and the hash stays a ``?``.
    """
    try:
        if not (ctx.is_connected or ctx.settings.connect_on_start):
            return None, None
        info = await ctx.devstate.self_info()
    except Exception:  # noqa: BLE001 - an optional read. Without it, the lane has no details.
        return None, None
    if not isinstance(info, dict):
        return None, None
    name, key = info.get("name"), info.get("public_key")
    return (str(name) if name else None), (str(key) if key else None)


class TimeMachinePickerScreen(ContactListScreen):
    """The subject picker of the Time Machine: the shared list below the whole-mesh row.

    The whole-mesh overview is first. Below it is the shared sortable list of the app
    (refer to :class:`~meshterm.ui.contactlist.ContactListScreen` for the lanes, the
    Ctrl+arrow sort, and type-to-filter): our node first, then each node ever heard. The
    hash lane of a heard node shows its fullest known key: the key captured with an
    observation (it shows when the device is offline), or else the contact list of the
    device, or else the stored 12-hex prefix.
    """

    def __init__(
        self,
        *,
        listed: list[tuple[HeardNode, str | None]],
        prefix_bytes: int,
        sort: ContactsSort,
        prompt: str,
        self_name: str | None = None,
        self_key: str | None = None,
        type_of: Callable[[str | None], int | None] = lambda _node: None,
        resolve_key: Callable[[str | None], str | None] = lambda node: node,
    ) -> None:
        """Build the picker over ``(node, name)`` pairs that are already resolved.

        Args:
            listed: Each heard node with a resolved display name (``None`` = unknown),
                in the most-recently-heard order of the repository. Sorted again by
                ``sort``.
            prefix_bytes: The path-hash width in bytes to light in each hash.
            sort: The sort state, which the Ctrl+arrows change in place. Give the same
                instance each time the picker opens again, so that the selected order
                stays. Its ring must span :data:`~meshterm.ui.contactlist.SORT_COLUMNS`,
                so that the user can reach the hash column.
            prompt: The instruction shown above the list.
            self_name: The advertised name of our node, for the lane of our node, which
                is always the first in the node list. ``None`` (no device available)
                gives a bare ``you``. The row is always there, because the page of our
                node reads stored history.
            self_key: The full public key of our node, lit as the hash in the lane of
                our node. ``None`` renders a muted ``?``.
            type_of: Resolves the type of a node from the contacts of the device. It
                fills the glyph at the start of the row for a node whose stored
                observations never had a type (refer to :func:`_contact_resolvers`).
                The default knows nothing, and leaves the glyph on the stored type.
            resolve_key: Expands the stored 12-hex key prefix of a heard node to its full
                public key when the device has the node as a contact. Thus the hash lane
                shows more than the stored digits (refer to :func:`_contact_resolvers`).
                The default returns the prefix unchanged, and the stored 12 hex digits
                stay.
        """
        rows = [ContactRow(value=SELF, name=self_name, key=self_key or "", you=True)]
        for node, name in listed:
            rows.append(
                ContactRow(
                    value=(node.node, name or node.node),
                    name=name,
                    # The full key, from the most durable source first: the key captured
                    # with the observation (it shows when the device is offline), or else
                    # the live contact list of the device, or else the stored prefix.
                    key=node.public_key or resolve_key(node.node) or node.node or "",
                    node_type=(
                        node.node_type if node.node_type is not None else type_of(node.node)
                    ),
                    last_seen=node.last_seen,
                    count=node.count,
                )
            )
        super().__init__(
            "Time machine",
            rows=rows,
            prefix_bytes=prefix_bytes,
            sort=sort,
            prompt=prompt,
            lead=[
                Choice(command_label("🌐 The whole mesh — days, arrivals, the ledger"), MESH),
                Separator(" "),
                section_heading("Nodes"),
            ],
        )


async def open_timemachine(ctx: AppContext) -> None:
    """Run the Time Machine: select a subject, explore its page, and do it again until Esc.

    Args:
        ctx: The shared application context (it must run the interactive TUI).

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - the caller is only in the menu
        raise RuntimeError("the time machine is only available in the menu")
    session = ctx.ui.session
    prefix_bytes = await ctx.devstate.routing_prefix_bytes()
    resolve, type_of, resolve_key = await _contact_resolvers(ctx)
    self_name, self_key = await _self_identity(ctx)
    # One sort for the full visit, so that the order that the user selects stays when the
    # user leaves a subject page and comes back. It opens with the most recently heard
    # first, as the list always did. Its ring spans the four columns of the shared list,
    # so that the Ctrl+arrows can reach the hash sort.
    sort = ContactsSort.from_name("heard", SORT_COLUMNS, SORT_OPENS_ASCENDING)

    while True:
        heard = ctx.repo.heard_nodes()
        if not heard:
            await session.message_dialog(
                Text(
                    "History is empty — the recorder fills it passively while "
                    "MeshTerm listens. Come back after the mesh has talked a while.",
                    style="muted",
                ),
                title="Time machine",
            )
            return
        # The stored names first, and the contact resolver fills the blanks (the rule of
        # the app: a node that we *can* name never shows as unknown).
        listed = [(node, _known_name(resolve, node.node, node.name)) for node in heard if node.node]
        picker = TimeMachinePickerScreen(
            listed=listed,
            prefix_bytes=prefix_bytes,
            sort=sort,
            prompt="Everything the recorder ever heard, explorable:",
            self_name=self_name,
            self_key=self_key,
            type_of=type_of,
            resolve_key=resolve_key,
        )
        # The picker stays pushed for the full visit. Thus a subject page nests above it,
        # and Esc goes back to the row from which the page opened, with the highlight, the
        # sort, and the filter unchanged. But the recorder continues to listen all the
        # time, so the subjects that it knows can increase while a page is open. Only
        # then is the list built again (and the place lost).
        subjects = {node.node for node, _ in listed}
        async with session.stay(picker) as visit:
            while True:
                result = await visit.result()
                picked = None if result is CANCEL else result
                if picked is None:
                    return
                if picked == SELF:
                    label = self_name or "you"
                    build = lambda window, width, _scope: _self_sections(  # noqa: E731
                        ctx, window, width
                    )
                    scopes = None
                elif picked == MESH:
                    label = "the whole mesh"
                    floods = _MeshFloods(ctx)

                    def build(window, width, scope, _pb=prefix_bytes, _floods=floods):  # noqa: ANN001, ANN202
                        if scope is None:
                            return _mesh_sections(ctx, window, width, _pb, resolve, resolve_key)
                        return _mesh_scope_sections(_floods.stamps(window, scope), window, width)

                    scopes = floods.keys
                else:
                    node_id, label = picked
                    build = (  # noqa: E731 - a small binding closure is better than a def here
                        lambda window, width, _scope, _id=node_id, _lb=label: _node_sections(
                            ctx, _id, _lb, window, width
                        )
                    )
                    scopes = None
                screen = TimeMachineScreen(session=session, label=label, build=build, scopes=scopes)
                await session.run_screen(screen)
                if {n.node for n in ctx.repo.heard_nodes() if n.node} != subjects:
                    break  # the mesh talked while the page was open. Make the list again.


async def open_timemachine_node(ctx: AppContext, node_id: str, label: str) -> None:
    """Open the Time Machine page of one node directly, without the subject picker.

    The *Time machine* link of the Node detail screen comes here. It is the same
    scrollable page for one node that :func:`open_timemachine` opens through its picker
    (volume, SNR band, hour-of-day rhythm, roll-up), but for a node that the user already
    selected in a different place. It reads only stored history, so no device is
    necessary and nothing transmits. It runs until the user leaves with Esc.

    Args:
        ctx: The shared application context (it must run the interactive TUI).
        node_id: The stored 12-hex key-prefix id of the node (the id in the observations).
        label: The display name of the node for the page heading.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - the caller is only in the menu
        raise RuntimeError("the time machine is only available in the menu")
    session = ctx.ui.session
    build = (  # noqa: E731 - a small binding closure is easier to read than a def here
        lambda window, width, _scope, _id=node_id, _lb=label: _node_sections(
            ctx, _id, _lb, window, width
        )
    )
    await session.run_screen(TimeMachineScreen(session=session, label=label, build=build))


async def open_timemachine_self(ctx: AppContext) -> None:
    """Open the Time Machine page of our node directly (the outbound-activity ledger).

    This is the *Time machine* link of the Node detail screen for our node. Our node
    never hears itself, so this page shows the traces that it started and the messages
    that it sent, not a reception history (refer to :func:`_self_sections`). It reads
    only stored history. It runs until the user leaves with Esc.

    Args:
        ctx: The shared application context (it must run the interactive TUI).

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - the caller is only in the menu
        raise RuntimeError("the time machine is only available in the menu")
    session = ctx.ui.session
    self_name, _ = await _self_identity(ctx)
    label = self_name or "you"
    build = lambda window, width, _scope: _self_sections(ctx, window, width)  # noqa: E731
    await session.run_screen(TimeMachineScreen(session=session, label=label, build=build))
