# SPDX-License-Identifier: Apache-2.0
"""The Time Machine: the mesh's recorded history, explorable node by node.

The interactive face of the ``timemachine`` tool. The database has been recording every
overheard packet since the first session, but nothing surfaced that history beyond the
dashboard's two-hour window — this screen is the archaeology dig. A picker offers the
whole mesh or any node ever heard; each subject renders as a scrollable page of braille
charts and stats over a switchable window (``w`` cycles 24 h → 7 d → 30 d → all time —
the PicoCalc's ring stops at 30 d, its F3 chip cycling the same three spans):

* a **node** shows its reception volume over the window, its median-SNR band (coloured
  by quality), its hour-of-day rhythm (when does this node talk?), and the roll-up
  stats — first/last heard, medians, extremes;
* the **whole mesh** shows packets per day and nodes per day across the history, the
  mesh-wide hour-of-day rhythm, the arrivals of the window (nodes heard for the first
  time ever, in aligned name/hash/first-heard lanes), and the all-time totals.

The whole-mesh page also cycles its **scope** on ``s`` (F2 on the PicoCalc): all, unscoped,
each region heard in the window A–Z, unknown scope — the dashboard's ring over the recorded
history. A narrowed page charts that scope's floods per day (or hour) and their rhythm, and
its own ledger; the node counts and arrivals leave, being read off adverts, which carry no
scope. Floods stored before MeshTerm kept their route count only under all. The node and
own-node pages have no scope to narrow: a node's page is its adverts and telemetry, and
what we send is traces, which go direct.

Charts read chronologically — oldest at the left, now at the right, the app-wide
timeline direction — and draw through :mod:`~meshterm.ui.braillechart`, so the grey
baseline always marks zero: the SNR band's readings hang below it or rise above it
by their actual sign. Everything is stored data — no device is needed and nothing
transmits.
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

#: Every history window the screen knows (``None`` = everything ever recorded). What a
#: session actually offers is the platform-bound :data:`_WINDOWS` below.
_ALL_WINDOWS: tuple[tuple[str, timedelta | None], ...] = (
    ("24 h", timedelta(days=1)),
    ("7 d", timedelta(days=7)),
    ("30 d", timedelta(days=30)),
    ("all time", None),
)

#: The offered windows. The PicoCalc's ring stops at 30 d (JP, 2026-08-08): all time —
#: the one span whose scan grows with the whole history — stays a desktop affordance,
#: and the F3 chip there cycles the three spans that remain. Bound at platform-switch
#: time.
_WINDOWS: tuple[tuple[str, timedelta | None], ...] = _ALL_WINDOWS


@on_platform
def _bind_windows(platform: Platform) -> None:
    """Bind the offered window ring to the platform (runs now and on every switch)."""
    global _WINDOWS
    _WINDOWS = _ALL_WINDOWS[:3] if platform.footer_fkeys else _ALL_WINDOWS


#: Picker sentinel for the whole-mesh overview page.
MESH = ("mesh",)

#: Picker sentinel for our own node's page (the outbound ledger; see :func:`_self_sections`).
SELF = ("self",)

#: How many braille rows tall the volume/rhythm charts draw.
_CHART_ROWS = 2

#: How many braille rows tall the SNR band draws — one more than the volume charts,
#: because zero-anchoring (readings hang below the grey zero line by their actual
#: depth) spends some dots on honesty and the extra row buys the swing back.
_SNR_ROWS = 3


def bucketize(stamps: list[datetime], start: datetime, end: datetime, buckets: int) -> list[int]:
    """Fold timestamps into ``buckets`` equal slices of ``[start, end]``, oldest first."""
    counts = [0] * max(1, buckets)
    span = max(1.0, (end - start).total_seconds())
    for stamp in stamps:
        index = int((stamp - start).total_seconds() / span * buckets)
        counts[min(buckets - 1, max(0, index))] += 1
    return counts


#: The Rhythm chart's candidate slice widths in minutes, finest first: the chart takes
#: the finest whose full day fits the width (JP, 2026-08-09). A fixed 15-minute slicing
#: used to outrun the PicoCalc's 53 columns; now the standard desktop keeps its 15-minute
#: sweep (48 cells + both gutters), the console steps down only as far as its width
#: forces, and a terminal wide enough earns the finer sweeps. If even the hour chart is
#: too wide, it draws anyway — tough shit, per the spec.
_RHYTHM_SLICES: tuple[int, ...] = (1, 5, 10, 15, 20, 30, 60)


def _slice_note(minutes: int) -> str:
    """The heading atom naming the rhythm's slice width: ``20-min slices``, ``1 h slices``."""
    return "1 h slices" if minutes >= 60 else f"{minutes}-min slices"


def _rhythm_slots(stamps: list[datetime], minutes: int) -> list[int]:
    """Fold timestamps into their local time-of-day slice at ``minutes`` per slot."""
    slots = [0] * (24 * 60 // minutes)
    for stamp in stamps:
        local = stamp.astimezone()
        slots[(local.hour * 60 + local.minute) // minutes] += 1
    return slots


def _fold_slots(minute_grid: list[int], minutes: int) -> list[int]:
    """Fold the repository's minute-of-day base grid into ``minutes``-wide slices.

    A minute divides every rung of :data:`_RHYTHM_SLICES`, so the mesh page re-slices the
    one :meth:`~meshterm.persistence.repository.Repository.rhythm_activity` scan
    client-side instead of re-querying per candidate width.
    """
    per = max(1, minutes)
    return [sum(minute_grid[i : i + per]) for i in range(0, len(minute_grid), per)]


def _fit_rhythm(
    slots_at: Callable[[int], list[int]], width: int, base_w: int
) -> tuple[list[int], int, int]:
    """Pick the finest rhythm slice whose chart fits ``width``: ``(slots, minutes, label_w)``.

    ``base_w`` is the gutter width the section's other charts already need; the rhythm's
    own scale joins it (a slice's tally can top a single volume bucket's), and the fit is
    judged against the row a chart actually draws — both gutters as the platform draws
    them (:func:`~meshterm.ui.braillechart.axis_chrome`) plus the cells. The coarsest
    slice comes back even when it doesn't fit: the ladder has nowhere further to step,
    and a clipped hour chart beats no rhythm at all.
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
    """Per-bucket medians of timestamped readings (``None`` for empty buckets)."""
    grouped: list[list[float]] = [[] for _ in range(max(1, buckets))]
    span = max(1.0, (end - start).total_seconds())
    for stamp, value in pairs:
        index = int((stamp - start).total_seconds() / span * buckets)
        grouped[min(buckets - 1, max(0, index))].append(value)
    return [median(values) if values else None for values in grouped]


def _snr_cell_style(values: list[float]) -> str:
    """Colour one SNR-band cell by its readings' quality (the shared SNR palette)."""
    return snr_style(sum(values) / len(values))


def _time_axis(start: datetime, end: datetime) -> Callable[[float], str]:
    """A compact axis labeller over a real time span, closing on ``now``.

    A window wider than a couple of days reads its marks as bare dates (``Jul 4``); a
    tighter one, where the calendar day barely changes, reads them as times (``18:30``)
    — the same "shorten what you can" the day charts' dates use, so the volume and SNR
    axes stay legible instead of repeating a full ``Jul 04 18:30`` stamp at every tick.
    """
    wide = (end - start).total_seconds() > 2 * 86400

    def label_at(frac: float) -> str:
        if frac >= 1.0:
            return "now"
        when = (start + (end - start) * frac).astimezone()
        return f"{when:%b} {when.day}" if wide else f"{when:%H:%M}"

    return label_at


def _quarter_axis(frac: float) -> str:
    """The rhythm charts' labeller: the local hour at ``frac`` of a full-day sweep.

    Whatever slice width the ladder settled on, the slices span midnight to midnight, so
    the fraction maps onto the whole ``0 → 24 h`` day (the right edge closing on
    ``24 h``), landing the intermediate marks on clean six-hour boundaries.
    """
    return f"{round(frac * 24)} h"


class TimeMachineScreen(Screen):
    """One subject's history page: scrollable sections, ``w`` cycles the window.

    A page whose history carries scope (the whole mesh's) also cycles it on ``s``.
    """

    floating = False

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The page's keys; ``s scope`` only where there is a scope to narrow to."""
        scope = " · s scope" if len(self._ring()) > 1 else ""
        return f"↑↓ PgUp/PgDn scroll · w window{scope} · Esc back"

    @property
    def fkey_lane(self):
        """The shared pager, plus the window cycle on F3.

        ``w`` is the whole point of this screen — the same history at several spans — and
        it is exactly the kind of affordance that vanishes on a platform with no hint line
        to read it off. The chip is the only place the PicoCalc can learn the key exists,
        so it earns the free F3 slot even though the letter itself is easy to press. (One
        chip per span on F1–F3 was tried and reverted — JP, 2026-08-09: the cycle was
        better.)

        The chip names the span it would *take you to*, not the one on screen: the title
        already says where you are, so a chip repeating it would be the same claim twice
        and would never tell you what pressing it does. ``all time`` shortens to ``all``
        to stay inside the 6-cell chip — the only span whose name doesn't already fit,
        and one this platform's ring may not even offer (see :func:`_bind_windows`).
        """
        from .tui.fkeys import FPair, default_lane

        lane = list(default_lane(nav=self.content_overflows))
        nxt, _delta = _WINDOWS[(self._window_index + 1) % len(_WINDOWS)]
        lane[2] = FPair(f"▸ {'all' if nxt == 'all time' else nxt}", "window")
        # The scope cycle beside it, on the same terms: the chip names where a press goes.
        views = self._ring()
        if len(views) > 1:
            lane[1] = FPair(scope_chip(step(views, self._scope)), "scope")
        return lane

    def __init__(
        self,
        *,
        session,  # noqa: ANN001 - TuiSession, imported lazily to avoid a cycle
        label: str,
        build: Callable[[timedelta | None, int, ScopeKey | None], list[RenderableType]],
        scopes: Callable[[timedelta | None], set[ScopeKey]] | None = None,
    ) -> None:
        """Create the page over its section builder.

        Args:
            session: The running TUI session (for repaints on window switch).
            label: The subject's display name (titles the screen).
            build: Renders the sections for ``(window, width, scope)``; called once per
                combination and cached — the data is stored history, so nothing needs
                re-querying per repaint. ``scope`` is ``None`` for every packet.
            scopes: The scope views a window's history holds, for a page that can narrow
                to one; ``None`` offers no scope cycle.
        """
        super().__init__()
        self._session = session
        self._label = label
        self._build = build
        self._scopes = scopes
        self._window_index = 1  # open on 7 d: enough depth to see shape, still fast
        #: The scope view on screen; ``None`` is every packet.
        self._scope: ScopeKey | None = None
        self._cache: dict[tuple[int, int, ScopeKey | None], list[str]] = {}
        self._set_title()

    def _ring(self) -> list[ScopeKey | None]:
        """The scope views on offer in the current window (just ``[None]`` if none)."""
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
        """Scroll, cycle the window, or dismiss.

        The window cycles on either the ``w`` key or the ``window`` action the F-key lane
        dispatches — one behaviour, two ways in, so the chip is not a second implementation
        of the letter. The ring it cycles is the platform's (see :func:`_bind_windows`).
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
        """Render (or reuse) the current window's sections."""
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
    """A compact local timestamp for chart captions: ``Jul 04 18:30``."""
    return when.astimezone().strftime("%b %d %H:%M")


def _node_sections(
    ctx: AppContext, node_id: str, label: str, window: timedelta | None, width: int
) -> list[RenderableType]:
    """Build one node's history page: volume, SNR band, rhythm, and the roll-up."""
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

    # Volume, SNR, and the rhythm share one y-axis gutter width (like the mesh page's
    # charts) so their left edges line up. A provisional width finds the peaks that size
    # the gutter, then the real width re-buckets Volume/SNR flush with it. The rhythm
    # folds every reception into local time-of-day slices at the finest width the ladder
    # fits (see :data:`_RHYTHM_SLICES`) — a slice's tally can top a single volume
    # bucket's, so its peak joins the sizing too.
    def _layout(label_w: int) -> tuple[int, int]:
        # A chart row spends axis_chrome(label_w) beside its cells -- both gutters, as
        # the platform draws them (the desktop mirrors its marks, the console doesn't).
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
    out.append(
        body_heading("Rhythm", f"receptions by local time of day · {_slice_note(slice_minutes)}")
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


# --- the own-node page -------------------------------------------------------------------


def _self_sections(ctx: AppContext, window: timedelta | None, width: int) -> list[RenderableType]:
    """Build the own-node page: transmission volume, reach SNR band, rhythm, and the ledger.

    Our own node is the one subject the reception history can't describe — we never
    overhear ourselves — so this page mirrors :func:`_node_sections` over the *outbound*
    record instead: **Activity** is what we put on the air (every trace we launched and
    message we sent) rather than what we heard; **Reach** charts our traces' bottleneck
    SNR (how strongly we get out) where the node page charts a heard node's SNR; the
    **Rhythm** is unchanged in shape — when do *we* transmit — and the **Ledger** rolls up
    the tallies. It shares the node page's chart machinery and one common y-axis gutter, so
    the two pages read as the same instrument pointed opposite ways.
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

    # The rhythm folds every transmission into local time-of-day slices at the finest
    # width the ladder fits (see :data:`_RHYTHM_SLICES`); a busy slice can top a single
    # volume bucket, so its peak joins the shared-gutter sizing (see _node_sections for
    # the same provisional-then-real width dance).
    def _layout(label_w: int) -> tuple[int, int]:
        # A chart row spends axis_chrome(label_w) beside its cells -- both gutters, as
        # the platform draws them (the desktop mirrors its marks, the console doesn't).
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
    out.append(
        body_heading("Rhythm", f"transmissions by local time of day · {_slice_note(slice_minutes)}")
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
    """The Ledger's outbound-messages line: channel/direct counts and the DM ack rate.

    Omitted entirely when nothing was sent in the window (``None``), so a trace-only
    window doesn't print an empty ``sent`` lane. Channel broadcasts are never acked, so
    the ack fraction is stated only over the direct messages whose ack was actually
    tracked (:attr:`~meshterm.core.models.SelfActivity.dm_ackable`).
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
    """Each day's lit-bar ``(start, width)`` within the chart's ``2 × chars`` dots.

    The one even split behind both the bars (:func:`_day_columns`) and their axis
    ticks (:func:`_day_centers`). When every bar can keep at least two dots, each
    of the ``n − 1`` day *boundaries* pays one unclaimed notch dot and the bars
    split the rest with a floor-edge walk, so no two differ by more than one dot
    column and the wider days interleave evenly among the narrower ones instead
    of pooling at either end. The notches sit strictly between days: the first
    bar starts flush at dot 0 (the axis border is separation enough — a leading
    blank dot would double the left margin the braille glyphs already carry)
    and the last ends flush at the right border, so both edges read alike. A
    denser history drops the notches and the bars simply tile all ``2 × chars``
    dots.

    Args:
        days: How many day bars the chart draws.
        chars: The chart's width in character cells.

    Returns:
        One ``(start dot, width in dots)`` per lit bar, oldest first.
    """
    dots = chars * 2
    n = max(1, days)
    lit = dots - (n - 1)  # what the bars keep once each boundary pays its notch dot
    notch = n > 1 and lit // n >= 2
    if not notch:
        lit = dots
    edges = [i * lit // n for i in range(n + 1)]
    return [(edges[i] + (i if notch else 0), edges[i + 1] - edges[i]) for i in range(n)]


def _day_centers(days: int, chars: int) -> list[int]:
    """The chart cell each day's bar is centred on, mirroring :func:`_day_columns`.

    Reads the same :func:`_day_spans` split ``_day_columns`` draws with, so a tick
    placed at ``centers[i]`` lands under day ``i``'s bar rather than at an
    arbitrary fraction of the axis.

    Args:
        days: How many day bars the chart draws.
        chars: The chart's width in character cells.

    Returns:
        One centre cell (``0 .. chars - 1``) per day, oldest first.
    """
    return [min(chars - 1, (start + width // 2) // 2) for start, width in _day_spans(days, chars)]


def _even_picks(n: int, k: int) -> list[int]:
    """``k`` bar indices out of ``n``, evenly spread with both ends included.

    The endpoints are always index ``0`` and ``n − 1``; the interior lands on the
    even fractions between. Rounding can collapse two picks onto one bar when ``k``
    approaches ``n`` on a short axis, so the result is de-duplicated and may hold
    fewer than ``k`` — the caller treats that as "``k`` does not fit" and steps down.

    Args:
        n: How many bars the chart draws.
        k: How many ticks to spread across them (``1 .. n``).

    Returns:
        The chosen bar indices, ascending.
    """
    if n <= 0:
        return []
    if k <= 1:
        return [0]
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})


def _ticks_fit(picks: list[int], labels: list[str], centers: list[int], chars: int) -> bool:
    """Whether ``labels`` placed under ``picks`` clear each other on the axis.

    Replays the exact placement :func:`~meshterm.ui.braillechart._tick_axis` runs —
    each label centred on its bar's cell, clamped into the axis, needing
    :data:`~meshterm.ui.braillechart._TICK_GAP` cells past the previous label's end —
    and reports whether every one survives. Measuring the real labels (a bare ``"7"``
    is a third the width of ``"Jul 12"``) is the point: a fixed worst-case budget per
    label would drop ticks a variable-width axis has ample room for.
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
    """``(cell, label)`` ticks: as many bars as the *actual* labels leave room for.

    Tries every bar first, then steps the tick count down until an evenly spread
    subset (:func:`_even_picks`) clears the collision rule (:func:`_ticks_fit`) for
    the labels it would actually draw. Both ends stay annotated at every count.
    Because the labels are rebuilt for each candidate set — day numbers stay bare
    until a month rollover forces ``"Jul 1"`` — the fit tracks their true widths
    rather than reserving the longest label's width for all of them.

    Args:
        n: How many bars the chart draws.
        chars: The chart's width in character cells.
        centers: Each bar's centre cell (see :func:`_day_centers`).
        label_of: Builds the labels for a set of picked bar indices, in context
            (so month rollovers and the closing ``today``/``now`` land correctly).

    Returns:
        The ticks to pass to :func:`~meshterm.ui.braillechart.axis_chart`.
    """
    if n <= 0:
        return []
    for k in range(min(n, chars), 1, -1):
        picks = _even_picks(n, k)
        if len(picks) < k:
            continue  # rounding fused two picks — this many will not fit cleanly
        labels = label_of(picks)
        if _ticks_fit(picks, labels, centers, chars):
            return list(zip((centers[i] for i in picks), labels, strict=True))
    picks = [0] if n == 1 else [0, n - 1]
    return list(zip((centers[i] for i in picks), label_of(picks), strict=True))


def _day_ticks(shown: list, chars: int) -> list[tuple[int, str]]:
    """``(cell, label)`` axis ticks under the day bars: short dates, thinned to fit.

    One tick per day where the labels all clear each other, else an evenly spaced
    subset keeping both ends (see :func:`_fit_ticks`); each label sits under its own
    bar (see :func:`_day_centers`). Dates are kept compact — the month is shown only
    on the first tick and whenever it rolls over, so most ticks read as a bare day
    number — and the newest bar reads ``today`` when it is, mirroring the node page's
    closing ``now``.

    Args:
        shown: The charted days, oldest first, each ``(iso_date, ...)``.
        chars: The chart's width in character cells.

    Returns:
        The ticks to pass to :func:`~meshterm.ui.braillechart.axis_chart`.
    """
    n = len(shown)
    centers = _day_centers(n, chars)
    # The day keys are local calendar days (see Repository.daily_activity), so "today"
    # is the local date too — datetime.now() is naive local, exactly what we compare.
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
    """``(cell, label)`` axis ticks under the hourly bars: local clock times, thinned to fit.

    The hour-resolution sibling of :func:`_day_ticks` for the 24 h window: one tick
    per hour where the labels fit, else an evenly spaced subset keeping both ends
    (see :func:`_fit_ticks`), each label centred under its own bar (see
    :func:`_day_centers`). The hour keys are already local (see
    :meth:`~meshterm.persistence.repository.Repository.hourly_series`), so a label is
    just its key's ``HH:00``, no conversion; the newest bar reads ``now``, mirroring
    the node page's closing ``now``.

    Args:
        shown: The charted hours, oldest first, each ``(hour_iso, ...)`` a local
            ``YYYY-MM-DDTHH``.
        chars: The chart's width in character cells.

    Returns:
        The ticks to pass to :func:`~meshterm.ui.braillechart.axis_chart`.
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
    """Fill a day series' gaps so an empty day shows as an empty bar, not a skip.

    ``daily_activity`` returns only days with traffic, so a quiet day used to vanish and
    its busy neighbours fused — a fortnight with two dead days reading as twelve adjacent
    bars. This walks every calendar day of the window and emits ``(iso, 0, 0)`` for the
    silent ones, so the chart's x-axis is real calendar time. The range runs from the
    window's floor (but never earlier than the first day ever recorded — we don't invent
    emptiness from before monitoring began) through today.

    Args:
        active: ``(iso_day, packets, nodes)`` for days with activity, oldest first.
        since: The window's start (``None`` = all of history).
        now: The current time (the series ends on its calendar day).

    Returns:
        ``(iso_day, packets, nodes)`` for every calendar day in range, oldest first.
    """
    if not active:
        return []
    by_iso = {iso: (packets, nodes) for iso, packets, nodes in active}
    # The keys are local calendar days, so bound the fill by local dates too — .date()
    # on the raw UTC-aware since/now would floor a day early in western zones.
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
    """Fill an hour series' gaps so a quiet hour shows as an empty bar, not a skip.

    The hour-resolution sibling of :func:`_fill_days`, feeding the 24 h window's
    charts: :meth:`~meshterm.persistence.repository.Repository.hourly_series` returns
    only hours with traffic, so a silent hour would fuse its busy neighbours. This
    walks every local clock hour of the window and emits ``(iso, 0, 0)`` for the quiet
    ones, so the x-axis is real clock time. The range runs from the window's floor
    (but never earlier than the first hour recorded — we don't invent emptiness from
    before monitoring began) through the current hour.

    The keys are local wall-clock ``YYYY-MM-DDTHH`` (matching the SQL grouping), so the
    walk steps a *naive* local clock: adding an hour advances the wall clock, which is
    DST-robust — a spring-forward gap fills as an empty bar and a fall-back repeat sums
    into one key, exactly as the grouping already did.

    Args:
        active: ``(hour_iso, packets, nodes)`` for hours with activity, oldest first,
            each ``hour_iso`` a local ``YYYY-MM-DDTHH``.
        since: The window's start.
        now: The current time (the series ends on its clock hour).

    Returns:
        ``(hour_iso, packets, nodes)`` for every local clock hour in range, oldest
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
    """Stretch per-day counts into day-wide bars that fill the chart's width exactly.

    One dot column per day leaves a short history as a sliver in a wide terminal —
    beneath how every other MeshTerm chart spends its width — so each day repeats
    over its :func:`_day_spans` share of the chart's dot columns instead: an even
    split whose widths never differ by more than one dot and whose total always
    lands on exactly ``2 × chars`` (a plain floor division drops the remainder,
    leaving the bars short of the axis border and caption sized for the full
    width — misreading as the whole chart sitting shifted left).

    The dots the split leaves unclaimed — one per day *boundary*, when the bars
    are wide enough to afford them (see :func:`_day_spans`) — render as
    :data:`~meshterm.ui.braillechart.GAP` columns, blank clean down to the axis,
    so same-height neighbours read as separate bars instead of fusing into one
    solid block. A deeper history (bars of one dot) has no boundary to spare and
    the days simply abut.

    Args:
        values: Per-day counts, oldest first.
        chars: The chart's width in character cells.

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
        ctx: The shared application context (repository reads only).
        window: The history window (``None`` = everything ever recorded).
        width: Render width in columns.
        prefix_bytes: The hash width to light at the head of each arrival's key (0 = none).
        resolve: Names a node hash from the device's contacts, for arrivals whose
            observations never carried a name.
        resolve_key: Expands a stored 12-hex node id to the full public key when the
            device holds it as a contact, so the arrivals' key lane can fill whatever
            width the terminal offers (see :func:`_contact_resolvers`).
    """
    now = utcnow()
    since = now - window if window is not None else None
    # The 24 h window charts an hour per column (its name is "24 h", not "1 day");
    # every wider window keeps the calendar-day columns. Both feed the same bar and
    # tick machinery — only the bucket resolution and the axis labels differ.
    hourly = since is not None and window is not None and window <= timedelta(days=1)
    # The all-days series feeds the ledger at the bottom too — fetched once here (it is
    # one of the costlier scans), and only on demand in the hourly case.
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

    # The mesh-wide rhythm (charted below) folds the whole window into local time-of-day
    # slices at the finest width the ladder fits (see :data:`_RHYTHM_SLICES`), re-sliced
    # client-side from one minute-grid base scan. A busy slice's tally can top any single
    # day's, so its peak joins the day peaks in sizing one shared y-axis gutter. The
    # slices come back already in local time (rotated per-instant in SQL), no offset
    # shuffle here.
    minute_grid = ctx.repo.rhythm_activity(since=since)

    # The y-axis gutter is sized from the whole window's peaks (not just the visible
    # slice) and shared by every chart, so all their gutters — and thus their left edges —
    # line up. The rhythm keeps its own finer width; only the gutter is common.
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

    # The node page's rhythm chart, mesh-wide: when does this *mesh* talk? The slices
    # keep their own finer width but share the day charts' gutter, so this chart's left
    # edge lines up with the two above it.
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
    # One aggregation serves both the arrivals' key lane and the ledger's node count
    # below (every heard node *is* a first-seen node, so the counts agree).
    heard = ctx.repo.heard_nodes()
    out.append(Text())
    out.append(body_heading("Arrivals", "nodes heard for the first time ever"))
    if not arrivals:
        out.append(Text("none in this window", style="muted"))
    else:
        # Aligned lanes under column labels, the picker's presentation: the name in
        # the node's hash-derived hue (a nameless arrival's "unknown" stays muted),
        # the key lit at the routing width, the age glowing with recency heat.
        # The FIRST HEARD header carries what used to be repeated on every row.
        # A nameless arrival first asks the resolver (the device may know the node
        # as a contact even though its stored observations never carried a name),
        # and each key expands to its fullest known form the way the picker's lane
        # does: the key captured with an observation (shows offline), else the
        # device's contact list, else the stored 12-hex prefix.
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
        # The key lane absorbs whatever width the name lane and the fixed FIRST
        # HEARD tail leave, floored at the old fixed lane — as many whole bytes as
        # fit, ellipsized past that on a byte boundary (highlighted_hash's contract).
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
    """The mesh page's stored floods per window, each with the scope view it counts in.

    Naming a scoped flood's region recomputes its transport code under each known region's
    key (see :meth:`~meshterm.core.region_store.RegionStore.scope_of`), so a window's
    floods are read and resolved once and kept for the page's life: the ring asks which
    views a window holds, and a narrowed page asks for one view's timestamps.
    """

    def __init__(self, ctx: AppContext) -> None:
        """Bind to the context's history and region names."""
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
        """The scope views ``window`` holds any flood in."""
        return {key for key, _ in self._frames(window)}

    def stamps(self, window: timedelta | None, scope: ScopeKey) -> list[datetime]:
        """When each of the window's floods in ``scope`` was heard, oldest first."""
        return [when for key, when in self._frames(window) if key == scope]


def _floods(count: int) -> str:
    """``1 flood``, ``12 floods``."""
    return f"{count} flood{'' if count == 1 else 's'}"


def _mesh_scope_sections(
    stamps: list[datetime], window: timedelta | None, width: int
) -> list[RenderableType]:
    """The whole-mesh page narrowed to one scope: its floods per day, their rhythm, a ledger.

    The unnarrowed page's day (or hour) bars and rhythm, over the scope's floods instead of
    every observation. Its node counts and arrivals have no narrowed form — a node is known
    by its adverts, and an advert says nothing about the scope other traffic was sent in —
    so they leave rather than show the whole mesh's under a scope's title.
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
    # The same local-time keys the repository's day and hour series group by, so the fill
    # and tick machinery below reads them exactly as it reads the unnarrowed page's.
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

#: Widest the mesh page's arrivals name lane grows (longer names ellipsize so the lanes
#: stay put). The picker sizes its own name lane to content instead (see
#: :meth:`~meshterm.ui.contactlist.ContactListScreen._lane_widths`).
_PICK_NAME_MAX = 18

#: The mesh page's arrivals key lane's *floor* in cells (a very narrow terminal). The lane
#: otherwise flexes to fill the width the name lane and the FIRST HEARD tail leave, so a
#: resolved full key shows as many whole bytes as fit (see the ``key_w`` math above).
_PICK_HASH_W = 16

#: Everything in an arrival row *besides* the name and key lanes, in cells: the 2-cell
#: indent, the two 2-cell lane gaps, the ``Jul 04 18:30`` stamp (12), and the recency
#: parenthetical (``  (259w ago)`` at its widest, 12).
_ARRIVAL_TAIL = 2 + 2 + 2 + 12 + 12


def _known_name(resolve: NodeResolver, node: str | None, name: str | None) -> str | None:
    """The best display name for a heard node, or ``None`` when truly unknown.

    The app-wide fill-in-the-blanks rule: a node id without a stored name is not
    necessarily a mystery — the device's contact list may know it. The stored name
    wins (it's what the node itself last put on the air); the resolver fills the
    blanks; only a node neither source can name stays ``unknown``.
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
    """A ``(name, type, key)`` resolver trio over the contacts, best-effort like the prefix read.

    All three fill the picker's (and arrivals') blanks from what the companion knows but the
    stored observations never carried: the name resolver names a node the device holds as a
    contact even though its history was nameless (a telemetry-only sensor, a repeater heard
    before it advertised); the type resolver supplies its node *type* the same way, so the
    row's leading glyph reads ``▲`` for a repeater rather than the plain-node ``●`` fallback;
    the key resolver expands a stored 12-hex prefix to the contact's full public key, so the
    hash lane shows more than the twelve stored digits when there's room. Contacts are read
    once and all three built from them. With no device reachable all three simply know
    nothing, and the stored names, types, and 12-hex prefixes stand alone.
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
    except Exception:  # noqa: BLE001 - optional read; absence just leaves names/types stored-only
        contacts = []
    return (
        make_node_resolver(contacts),
        make_node_type_resolver(contacts),
        make_key_resolver(contacts),
    )


async def _self_identity(ctx: AppContext) -> tuple[str | None, str | None]:
    """Our own node's ``(name, public_key)`` for its picker lane, best-effort like the prefix read.

    The own-node page needs no device — it reads stored history — so this fetch only dresses
    the row: an unreachable device (or firmware that won't answer) simply leaves the name a
    bare ``you`` and the hash a ``?``.
    """
    try:
        if not (ctx.is_connected or ctx.settings.connect_on_start):
            return None, None
        info = await ctx.devstate.self_info()
    except Exception:  # noqa: BLE001 - optional read; absence just leaves the lane undressed
        return None, None
    if not isinstance(info, dict):
        return None, None
    name, key = info.get("name"), info.get("public_key")
    return (str(name) if name else None), (str(key) if key else None)


class TimeMachinePickerScreen(ContactListScreen):
    """The Time Machine's subject picker: the shared list under the whole-mesh row.

    The whole-mesh overview leads; under it runs the app's shared sortable list
    (see :class:`~meshterm.ui.contactlist.ContactListScreen` for the lanes, the Ctrl+arrow
    sort, and type-to-filter) — our own node first, then every node ever heard. A heard
    node's hash lane shows its fullest known key: the one captured with an observation
    (shows offline), else the device's contact list, else the stored 12-hex prefix.
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
        """Build the picker over already-resolved ``(node, name)`` pairs.

        Args:
            listed: Every heard node with a resolved display name (``None`` = unknown),
                in the repository's most-recently-heard order; re-sorted per ``sort``.
            prefix_bytes: Path-hash width in bytes to light in each hash.
            sort: The sort state, mutated in place by the Ctrl+arrows — pass the same
                instance across re-opens so the chosen order persists. Its ring should span
                :data:`~meshterm.ui.contactlist.SORT_COLUMNS` for the hash column to be
                reachable.
            prompt: The instruction shown above the list.
            self_name: Our own node's advertised name, for the own-node lane that always
                leads the node list; ``None`` (no reachable device) falls back to a bare
                ``you``. The row is always present — the own-node page reads stored history.
            self_key: Our own node's full public key, lit as the own-node lane's hash;
                ``None`` renders a muted ``?``.
            type_of: Resolves a node's type from the device's contacts, filling the leading
                glyph for a node whose stored observations never carried one (see
                :func:`_contact_resolvers`); the default knows nothing, leaving the glyph on
                the stored type alone.
            resolve_key: Expands a heard node's stored 12-hex key prefix to its full public
                key when the device holds it as a contact, so the hash lane shows more than
                the stored digits (see :func:`_contact_resolvers`); the default returns the
                prefix unchanged, leaving the stored 12 hex to stand.
        """
        rows = [ContactRow(value=SELF, name=self_name, key=self_key or "", you=True)]
        for node, name in listed:
            rows.append(
                ContactRow(
                    value=(node.node, name or node.node),
                    name=name,
                    # The full key, most-durable source first: the one captured with the
                    # observation (shows offline), else the device's live contact list,
                    # else the stored prefix.
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
    """Run the Time Machine: pick a subject, explore its page, repeat until Esc.

    Args:
        ctx: The shared application context (must be running the interactive TUI).

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the time machine is only available in the menu")
    session = ctx.ui.session
    prefix_bytes = await ctx.devstate.routing_prefix_bytes()
    resolve, type_of, resolve_key = await _contact_resolvers(ctx)
    self_name, self_key = await _self_identity(ctx)
    # One sort for the whole visit, so the order the user picks survives leaving a subject
    # page and coming back. Opens most-recently-heard first, as the list always has; its ring
    # spans the shared list's four columns so the Ctrl+arrows can reach the hash sort.
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
        # Stored names first, the contact resolver filling the blanks (the app-wide
        # rule: a node we *can* name never shows as unknown).
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
        # The picker stays pushed for the whole visit, so a subject page nests above it and
        # Esc lands back on the row it was opened from — cursor, sort and filter intact. The
        # recorder keeps listening the whole time, though, so the subjects it knows can grow
        # while a page is open; the list is rebuilt (and the place lost) only when it has.
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
                    build = (  # noqa: E731 - a tiny binding closure beats a def here
                        lambda window, width, _scope, _id=node_id, _lb=label: _node_sections(
                            ctx, _id, _lb, window, width
                        )
                    )
                    scopes = None
                screen = TimeMachineScreen(session=session, label=label, build=build, scopes=scopes)
                await session.run_screen(screen)
                if {n.node for n in ctx.repo.heard_nodes() if n.node} != subjects:
                    break  # the mesh spoke while the page was open; relist


async def open_timemachine_node(ctx: AppContext, node_id: str, label: str) -> None:
    """Open one node's Time Machine page directly, skipping the subject picker.

    The Node detail screen's *Time machine* link lands here: the same scrollable per-node
    page :func:`open_timemachine` reaches through its picker (volume, SNR band, hour-of-day
    rhythm, roll-up), but for a node already chosen elsewhere. Reads stored history only, so
    no device is needed and nothing transmits; runs until dismissed with Esc.

    Args:
        ctx: The shared application context (must be running the interactive TUI).
        node_id: The node's stored 12-hex key-prefix id (what observations carry).
        label: The node's display name for the page heading.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the time machine is only available in the menu")
    session = ctx.ui.session
    build = (  # noqa: E731 - a tiny binding closure reads better than a def here
        lambda window, width, _scope, _id=node_id, _lb=label: _node_sections(
            ctx, _id, _lb, window, width
        )
    )
    await session.run_screen(TimeMachineScreen(session=session, label=label, build=build))


async def open_timemachine_self(ctx: AppContext) -> None:
    """Open our own node's Time Machine page directly (the outbound-activity ledger).

    The Node detail screen's *Time machine* link for our own node: we never overhear
    ourselves, so this page is the traces we launched and the messages we sent, not a
    reception history (see :func:`_self_sections`). Reads stored history only; runs until
    dismissed with Esc.

    Args:
        ctx: The shared application context (must be running the interactive TUI).

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the time machine is only available in the menu")
    session = ctx.ui.session
    self_name, _ = await _self_identity(ctx)
    label = self_name or "you"
    build = lambda window, width, _scope: _self_sections(ctx, window, width)  # noqa: E731
    await session.run_screen(TimeMachineScreen(session=session, label=label, build=build))
