# SPDX-License-Identifier: Apache-2.0
"""The live mesh dashboard: all the activity around this node, on one screen.

This is the interactive face of the ``dashboard`` tool. One full-screen page, which
paints again continuously, shows three summaries of the mesh, the most general first:

* **Activity**: a tall braille bar chart of *each* packet that the hub hears (adverts,
  telemetry, RX-logged packets, messages, acks). It has one dot column for each minute,
  which is the full horizontal resolution of braille: two minutes for each character.
  The chart fills the full width of the terminal, with the newest minute at the right
  (the timeline direction of the whole app), and with the count scale mirrored on both
  edges. It is a larger form of the indicator in the header, drawn from the same
  :meth:`~meshterm.services.monitor_service.MonitorService` buckets. A pulse line under
  it shows the rate, the nodes that MeshTerm heard, and the busiest node of the time
  window.
* **Traffic**: the packets heard on the air, counted by class (chan text, trace, path,
  …), with the names and icons that the live feed gives them, and each with a
  proportional bar. Each packet counts one time. What the device *reported* about a
  packet (its advert event, a decoded message) does not count a second time next to it.
* **RF health**: the reception quality of the trailing time window. It shows the median
  SNR (on the quality bar of the trace tool) and the median RSSI from stored
  observations. It also shows the live numbers of the radio itself (noise floor, last
  RSSI/SNR, airtime, battery), which MeshTerm polls from the device in the same way as
  Device info.

**Scope** limits the full screen to the floods of one region. ``s`` (the F3 chip on the
PicoCalc) goes through each scope view in turn: all, unscoped, each region heard, and
unknown scopes. The ``w`` key of the Time Machine goes through its time windows in the
same way. A narrowed view reads the trailing time window of the screen itself (stored +
live, 2 h), because a cryptographic check names the region of each packet, and the
database cannot do that check. The numbers of the radio itself stay complete, because
they are the numbers of the device.

Before, a stream of each packet was at the end of this screen. Now it is a tool of its
own: the Live feed (refer to :mod:`meshterm.ui.livefeed_screen`), directly under the
dashboard in the menu. Thus this screen is only an overview. It scrolls as one body with
the arrow keys and the page keys.

The screen has no subscriptions of its own. The opener (:func:`open_dashboard`) connects
the hub subscription, the poll of the device statistics, and the paint each second. It
removes all of them when the screen resolves. The Esc key leaves the screen directly.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter, deque
from collections.abc import Callable
from statistics import median
from typing import TYPE_CHECKING, Any, NamedTuple

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from ..core.events import MeshEvent
from ..core.models import NODE_TYPE_REPEATER, Observation, utcnow
from ..core.regions import Scope
from ..persistence.repository import OBSERVATION_WINDOW
from ..platforms import Platform, on_platform
from ..services.monitor_service import ACTIVITY_BUCKET_S, ACTIVITY_BUCKETS
from .braillechart import axis_chart, axis_chrome, axis_label_w, meter, timeline_rows
from .packet_viewer import KIND_STYLES, payload_marks
from .scopering import ScopeKey, ring, scope_atom, scope_chip, scope_key, step
from .theme import snr_style
from .trace_screen import snr_bar
from .tui.render import render_lines
from .tui.screen import Screen

if TYPE_CHECKING:
    from ..context import AppContext

#: The seconds between full paints while the dashboard is open, for the ages and the rates
#: (the screen has no spinner).
_REFRESH_S = 1.0

#: The seconds between polls of the statistics of the device (noise floor, airtime, battery).
_STATS_POLL_S = 10.0

#: The height of the activity chart in braille rows (each row is four dot rows).
_CHART_ROWS = 3

#: The order of the classes in the traffic section. Heard classes that are not in this list
#: sort last, in alphabetical order. Chat packets come before protocol packets, and the
#: ``packet`` bucket without a class ends the list.
_TRAFFIC_ORDER = (
    "packet:GRP_TXT",
    "packet:GRP_DATA",
    "packet:TEXT_MSG",
    "packet:REQ",
    "packet:RESPONSE",
    "packet:ANON_REQ",
    "packet:PATH",
    "packet:TRACE",
    "packet:ADVERT",
    "packet:ACK",
    "packet:MULTIPART",
    "packet:CONTROL",
    "packet",
)


def _is_frame_bucket(bucket: str) -> bool:
    """Whether a count bucket counts packets heard on the air (``packet[:<TYPENAME>]``).

    The monitor also counts what the *device* reported (``advert``, ``telemetry``,
    ``message``, ``ack``). But each of those is a packet that is already counted under its
    own class (the advert event *and* its RX-logged ``ADVERT`` packet). Thus the traffic
    section counts only the packets on the air: each packet one time, under the name that
    the live feed gives it.
    """
    return bucket == "packet" or bucket.startswith("packet:")


def _traffic_chrome(bucket: str) -> tuple[str, str]:
    """The ``(icon, label)`` of one traffic bucket, from the same function as the live feed.

    On PICOCALC_LYRA, :func:`~meshterm.ui.theme.glyph` maps the icons to single glyphs.
    """
    typename = bucket.split(":", 1)[1] if ":" in bucket else None
    return payload_marks(typename)


#: The number of cells that the meter of a traffic lane uses (48 half-step levels).
_TRAFFIC_METER_CELLS = 24

#: The label column that each section grid with a hanging indent keeps free (the
#: ``snr      `` / ``radio    `` lane). Thus wrapped values align with their own block,
#: never with column 0.
_GRID_LABEL_W = 9


#: The title of the screen, before a scope atom.
_TITLE = "Dashboard — mesh overview"

#: What the activity chart of the whole mesh counts, as the aside of the heading says it.
#: The desktop adds the unit of the chart, ``one minute per dot column``. A font that draws
#: braille solid (the font of the PicoCalc or of the Cardputer,
#: :attr:`~meshterm.platforms.Platform.solid_braille`) has no dots to count. Also, on 53
#: columns, these words make the heading too long for its line (JP, 2026-10-04). The axis
#: under the chart still marks the minutes.
_ACTIVITY_ASIDE = "every packet heard · one minute per dot column"


@on_platform
def _bind_activity_aside(platform: Platform) -> None:
    """Bind the activity aside to the braille of the platform (runs now and at each switch)."""
    global _ACTIVITY_ASIDE
    _ACTIVITY_ASIDE = (
        "every packet heard"
        if platform.solid_braille
        else "every packet heard · one minute per dot column"
    )


class _ScopeDigest(NamedTuple):
    """The numbers of one scope view in the time window, in the shapes of the sections.

    Attributes:
        histogram: Packets for each minute, the newest first, in the bucket shape of the
            monitor.
        counts: Packets by traffic bucket (``packet:<TYPENAME>``).
        snrs: All the SNR values of those packets.
        rssis: All the RSSI values of those packets.
    """

    histogram: tuple[int, ...]
    counts: dict[str, int]
    snrs: list[float]
    rssis: list[float]


def _span_label(minutes: int) -> str:
    """A compact duration: ``45 min`` under two hours, or else ``2.5 h`` / ``3 h``."""
    if minutes < 120:
        return f"{minutes} min"
    text = f"{minutes / 60:.1f}"
    return (text[:-2] if text.endswith(".0") else text) + " h"


class DashboardScreen(Screen):
    """The live mesh overview. It renders the state and scrolls. The opener gives it data."""

    floating = False

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The footer keys. The hint shows the scroll keys only when the overview overflows.

        The dashboard is one body that scrolls, but on a tall terminal the full body often
        fits. Then a static ``↑↓ PgUp/PgDn scroll`` names keys that do nothing. Thus show
        that atom only while the content is taller than the viewport (refer to
        :attr:`~meshterm.ui.tui.screen.Screen.content_overflows`). In other cases, Esc is
        the only key that acts, with ``s`` when there is more than one scope view.
        """
        atoms = ["↑↓ PgUp/PgDn scroll"] if self.content_overflows else []
        if len(self._ring()) > 1:
            atoms.append("s scope")
        return " · ".join([*atoms, "Esc back"])

    @property
    def picocalc_lyra_lane(self):
        """The shared F-key lane, dimmed like the hint above, plus the scope cycle.

        The PicoCalc draws no hint line at all, because the lane *is* the footer. Thus the
        rule of the hint must apply there too: when the full overview fits, Top/End and the
        paging pair have nothing to move. The scope cycle uses F3, as the time-window cycle
        of the Time Machine does. Its chip names the view that a key press goes *to* (the
        title already says where you are). When no flood was heard that the screen can
        narrow to, the slot stays empty.
        """
        from .tui.fkeys import FPair, default_lane

        lane = list(default_lane(nav=self.content_overflows))
        views = self._ring()
        if len(views) > 1:
            lane[2] = FPair(scope_chip(step(views, self._scope)), "scope")
        return lane

    def __init__(
        self,
        *,
        session: Any,
        resolve: Any,
        window: list[Observation],
        activity: Any,
        activity_flags: Any,
        kind_counts: Any,
        scope_of: Callable[[dict | None], Scope | None] | None = None,
    ) -> None:
        """Create the dashboard over its data feeds.

        Args:
            session: The active TUI session (for paints).
            resolve: Changes a node hash into a friendly contact name, when the name is
                known.
            window: The stored observations that start the trailing time window (oldest
                first).
            activity: A callable with no arguments that returns the histogram of all
                packets from the monitor.
            activity_flags: A callable with no arguments that returns a flag for each
                bucket of the histogram: whether the bucket is from this session (stored
                history draws grey, live traffic draws green).
            kind_counts: A callable with no arguments that returns the counts by kind
                from the monitor.
            scope_of: Names the scope of a packet from its raw payload
                (:meth:`~meshterm.core.region_store.RegionStore.scope_of`). ``None`` gives
                no scope views.
        """
        super().__init__()
        self._scope_of = scope_of
        #: The scope view on the screen. ``None`` is all the packets heard.
        self._scope: ScopeKey | None = None
        self._set_title()
        self._session = session
        self._resolve = resolve
        self._activity = activity
        self._activity_flags = activity_flags
        self._kind_counts = kind_counts
        #: The trailing time window of observations (stored seed + live), oldest first.
        self._window: deque[Observation] = deque(window, maxlen=4000)
        #: The numbers of the device itself, which the poll of the opener refreshes (the
        #: dynamic rows of Device info): ``stats`` from get_stats, ``battery`` from
        #: get_battery.
        self.stats: dict = {}
        self.battery: dict = {}
        # The aggregates of the time window, made in one pass (refer to _digest_window).
        # They are refreshed each time that the window itself changes. ``_window_rev``
        # counts these changes, and ``_digest_rev`` remembers the last one. The screen
        # paints once a second to keep its clocks correct. Without this cache, on a quiet
        # mesh each of those paints goes through the same 4000 observations again and gets
        # the same numbers.
        self._window_rev = 0
        self._digest_rev = -1
        self._win_nodes: set[str] = set()
        self._win_repeaters: set[str] = set()
        self._win_counts: Counter = Counter()
        self._win_snrs: list[float] = []
        self._win_rssis: list[float] = []
        #: The packets of the time window that have a scope, each with the view that it
        #: counts in.
        self._win_scoped: list[tuple[ScopeKey, Observation]] = []
        #: The numbers of the narrowed view, and the inputs for which they were calculated
        #: (refer to :meth:`_scoped`).
        self._scoped_memo: tuple[tuple, _ScopeDigest] | None = None
        #: The last activity chart, and the inputs from which it was drawn (refer to
        #: :meth:`_activity_section`).
        self._chart_memo: tuple[tuple, list[RenderableType]] | None = None

    # --- live time window ------------------------------------------------------------

    def on_event(self, event: MeshEvent) -> None:
        """Add one hub observation to the trailing time window, and paint again."""
        obs = event.observation
        if obs is not None:
            self._window.append(obs)
            self._window_rev += 1
            self._prune()
            self._session.invalidate()

    def _prune(self) -> None:
        """Remove the observations that are older than the trailing time window."""
        cutoff = utcnow() - OBSERVATION_WINDOW
        while self._window and self._window[0].observed_at < cutoff:
            self._window.popleft()
            self._window_rev += 1

    # --- input -----------------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Scroll the overview body, change the scope view, or close the screen."""
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
        elif action == "scope" or (action == "text" and data.lower() == "s"):
            self._cycle_scope()
        elif action == "escape":
            self.resolve(None)

    # --- scope -----------------------------------------------------------------------

    def _ring(self) -> list[ScopeKey | None]:
        """All the scope views on offer: all packets, then each scope heard in the window."""
        self._digest_window()
        return ring((key for key, _ in self._win_scoped), self._scope)

    def _cycle_scope(self) -> None:
        """Go to the next scope view (refer to :func:`~meshterm.ui.scopering.step`)."""
        views = self._ring()
        if len(views) < 2:
            return
        self._scope = step(views, self._scope)
        self._set_title()
        self.scroll_to_top()
        self._session.invalidate()

    def _set_title(self) -> None:
        """Set the title for the scope view: a narrowed view names itself, shorter if narrow."""
        if self._scope is None:
            self.title, self.short_title = _TITLE, ""
            return
        atom = scope_atom(self._scope)
        self.title = f"{_TITLE} · {atom}"
        self.short_title = f"Dashboard · {atom}"

    def _scoped(self) -> _ScopeDigest:
        """The narrowed view's numbers, recalculated only for a new window, view, or minute."""
        minute = int(time.time() // ACTIVITY_BUCKET_S)
        memo_key = (self._window_rev, self._scope, minute)
        if self._scoped_memo is not None and self._scoped_memo[0] == memo_key:
            return self._scoped_memo[1]
        histogram = [0] * ACTIVITY_BUCKETS
        counts: Counter = Counter()
        snrs: list[float] = []
        rssis: list[float] = []
        for key, o in self._win_scoped:
            if key != self._scope:
                continue
            age = minute - int(o.observed_at.timestamp() // ACTIVITY_BUCKET_S)
            if 0 <= age < ACTIVITY_BUCKETS:
                histogram[age] += 1
            typename = (o.raw or {}).get("payload_typename")
            counts[f"packet:{typename}" if typename else "packet"] += 1
            if o.snr is not None:
                snrs.append(o.snr)
            if o.rssi is not None:
                rssis.append(o.rssi)
        digest = _ScopeDigest(tuple(histogram), dict(counts), snrs, rssis)
        self._scoped_memo = (memo_key, digest)
        return digest

    # --- rendering ---------------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render the overview sections as one body that scrolls."""
        self._prune()
        self._digest_window()
        body: list[RenderableType] = [
            *self._activity_section(width),
            Text(),
            *self._traffic_section(),
            Text(),
            *self._rf_section(),
        ]
        lines = render_lines(Group(*body), width)
        self._scroll_total = max(1, len(lines))
        return lines

    def _digest_window(self) -> None:
        """Fold the observation window into the aggregates that each paint uses, in one pass.

        The pulse line, the row of the busiest transmitter, and the RF section each use a
        different slice of the same time window. Before, each consumer went through the
        deque (up to 4000 entries) one time. That made up to five full passes on each paint
        of a busy mesh. Now one pass here fills all of them, and the section builders read
        the results.

        Also, that pass runs only when the time window has changed. The screen paints each
        second, so that its rates, ages, and clock stay correct. But the aggregates depend
        only on the deque. Before, on a mesh that was quiet for a moment, each of those
        paints went through four thousand observations again to get the numbers that it
        already had. The time window changes in exactly two places (an observation that
        arrives, an observation that expires), and both increase the revision that this
        method compares against.
        """
        if self._digest_rev == self._window_rev:
            return
        self._digest_rev = self._window_rev
        nodes: set[str] = set()
        repeaters: set[str] = set()
        counts: Counter = Counter()
        snrs: list[float] = []
        rssis: list[float] = []
        scoped: list[tuple[ScopeKey, Observation]] = []
        for o in self._window:
            if o.node and o.node_type == NODE_TYPE_REPEATER:
                repeaters.add(o.node)
            if o.kind == "packet":
                # The region store caches by packet, so each packet is checked one time in
                # each visit.
                key = scope_key(self._scope_of(o.raw)) if self._scope_of else None
                if key is not None:
                    scoped.append((key, o))
                continue
            if o.node:
                nodes.add(o.node)
                counts[o.node] += 1
            if o.snr is not None:
                snrs.append(o.snr)
            if o.rssi is not None:
                rssis.append(o.rssi)
        self._win_nodes = nodes
        self._win_repeaters = repeaters
        self._win_counts = counts
        self._win_snrs = snrs
        self._win_rssis = rssis
        self._win_scoped = scoped

    # -- activity --

    def _activity_section(self, width: int) -> list[RenderableType]:
        """The chart of all packets (the newest minute at the right), and the pulse line.

        It has one dot column for each minute, two for each cell. The chart fills all the
        cells of the terminal between the scale gutters. The gutters are drawn as the
        platform draws them: the desktop mirrors its marks on the right, and the console
        keeps only the tick (refer to :func:`~meshterm.ui.braillechart.axis_chrome`). A
        wider terminal shows more history. Time goes from oldest to now, left to right,
        which is the direction of each MeshTerm timeline.
        """
        # The newest first, one count for each minute.
        histogram = list(self._scoped().histogram if self._scope else self._activity())
        # Size the label lane from the peak of the full histogram (not only of the visible
        # slice), so that the gutters never move when a burst scrolls out of the chart.
        label_w = axis_label_w(max(histogram, default=0), _CHART_ROWS)
        chars = max(10, width - axis_chrome(label_w))
        minutes = chars * 2
        shown = (histogram + [0] * minutes)[:minutes]
        peak = max(shown)

        heading = Text("Activity", style="accent")
        # A narrowed view uses the aside to tell what it counts, because the user must know
        # it. The minute for each column is on the axis below, in both cases.
        what = "floods in this scope · last 2 h" if self._scope else _ACTIVITY_ASIDE
        heading.append(f"  ·  {what}", style="muted")
        # Buckets that come from the stored history of a previous session are grey. Only
        # what this session heard itself shows green.
        flags = (tuple(self._activity_flags()) + (True,) * minutes)[:minutes]
        styles = ["ok" if live else "muted" for live in reversed(flags)]

        # The chart depends only on these numbers, and on the idle tick each second they
        # are the same numbers. The histogram changes only when a packet arrives or a new
        # minute starts. To rasterize the braille rows and their axis chrome is the most
        # expensive part of the paint of this screen. Thus it occurs one time for each
        # different picture, not one time for each paint. The cache has one slot, because
        # the chart that changed last is the one to keep.
        key = (chars, label_w, peak, minutes, tuple(shown), tuple(styles))
        if self._chart_memo is not None and self._chart_memo[0] == key:
            chart = self._chart_memo[1]
        else:

            def caption_at(frac: float) -> str:
                if frac >= 1.0:
                    return "now"
                return "−" + _span_label(round(minutes * (1 - frac)))

            chart_rows = timeline_rows(
                list(reversed(shown)), rows=_CHART_ROWS, column_styles=styles
            )
            chart = list(axis_chart(chart_rows, peak, chars, caption_at, label_w=label_w))
            self._chart_memo = (key, chart)

        out: list[RenderableType] = [heading, *chart]
        out.append(Text())
        out.append(self._pulse_grid(shown, width))
        return out

    def _grid(self, rows: list[tuple[str, Text]]) -> Table:
        """Labelled rows with a hanging indent: the values wrap in their own block.

        This is the alignment rule of the whole app: the continuation lines of a wrapped
        item align with the item, never with the start of the line. The method does it as
        a grid of two columns without borders. The label lane has a fixed width of
        :data:`_GRID_LABEL_W` cells. The value column uses the width that remains, and folds
        its text in itself.
        """
        grid = Table(
            box=None,
            show_header=False,
            show_edge=False,
            pad_edge=False,
            padding=(0, 0),
            expand=False,
        )
        grid.add_column(width=_GRID_LABEL_W, no_wrap=True)
        grid.add_column(overflow="fold")
        for label, value in rows:
            grid.add_row(Text(label, style="muted"), value)
        return grid

    def _pulse_grid(self, shown: list[int], width: int) -> Table:
        """The rates, the heard nodes, and the busiest transmitter of the time window.

        ``shown`` is the visible slice of the chart (the newest first, one bucket for each
        minute). Thus the rates describe exactly what the chart draws. The word "heard"
        after the node count is not necessary. The method keeps it only when the line fits
        in ``width`` and does not wrap.
        """
        recent = sum(shown[:15]) / max(1, min(15, len(shown)))
        overall = sum(shown) / max(1, len(shown))
        span = _span_label(len(shown))
        nodes = self._win_nodes  # the window digest of one pass (refer to _digest_window)
        repeaters = self._win_repeaters

        def compose(heard: bool) -> Text:
            line = Text()
            line.append(f"{recent:.1f} pkt/min", style="brand")
            line.append(f" (15 m) · {overall:.1f} ({span})", style="muted")
            if self._scope:
                return line  # the heard nodes come from adverts, which have no scope
            line.append("  ·  ", style="muted")
            line.append(str(len(nodes)))
            suffix = f" node{'s' if len(nodes) != 1 else ''}"
            line.append(suffix + (" heard" if heard else ""), style="muted")
            if repeaters:
                line.append(
                    f" ({len(repeaters)} repeater{'s' if len(repeaters) != 1 else ''})",
                    style="muted",
                )
            return line

        line = compose(heard=True)
        if len(line.plain) + _GRID_LABEL_W > width:
            line = compose(heard=False)
        rows = [("pulse", line)]
        busiest = None if self._scope else self._busiest()
        if busiest is not None:
            name, count = busiest
            value = Text(name, style="brand")
            value.append(
                f"  {count} packet{'s' if count != 1 else ''} in the window",
                style="muted",
            )
            rows.append(("busiest", value))
        return self._grid(rows)

    def _busiest(self) -> tuple[str, int] | None:
        """The most-heard node of the time window, or ``None`` when it is quiet.

        Only the observations that name a node count.
        """
        counts = self._win_counts  # the window digest of one pass (refer to _digest_window)
        if not counts:
            return None
        node, count = counts.most_common(1)[0]
        return self._name(node), count

    # -- traffic --

    def _traffic_section(self) -> list[RenderableType]:
        """The packets heard on the air, by class, each with an icon and a proportional meter.

        Each class shows after MeshTerm hears it, with the name that the live feed gives
        it. Classes that were never heard stay hidden. Each bar has the one colour in which
        the feed draws the class of a packet (the colour of ``packet``). The icon and the
        label tell the class of a row, so the colour has nothing more to say here.
        """
        heading = Text("Traffic", style="accent")
        if self._scope:
            heading.append("  ·  by packet class · floods in the last 2 h", style="muted")
            counts = self._scoped().counts
            empty = "nothing heard in this scope in the last 2 h"
        else:
            heading.append("  ·  by packet class · stored history + live", style="muted")
            counts = {k: n for k, n in self._kind_counts().items() if _is_frame_bucket(k)}
            empty = "nothing heard yet"
        if not counts:
            return [heading, Text(empty, style="muted")]
        order = {k: i for i, k in enumerate(_TRAFFIC_ORDER)}
        peak = max(counts.values())
        chrome = {bucket: _traffic_chrome(bucket) for bucket in counts}
        label_w = max(len(label) for _, label in chrome.values())
        count_w = len(str(peak))
        style = KIND_STYLES["packet"]
        rows: list[RenderableType] = [heading]
        for bucket in sorted(counts, key=lambda k: (order.get(k, len(order)), k)):
            count = counts[bucket]
            icon, label = chrome[bucket]
            row = Text(f"{icon} ")
            row.append(f"{label.ljust(label_w)}  ", style="muted")
            row.append(f"{count:>{count_w}}  ")
            # The shared braille meter, at full height and with no track. The lane shows
            # 48 levels in its 24 cells (two half-steps for each cell).
            row.append_text(meter(count / peak, _TRAFFIC_METER_CELLS, style=style))
            rows.append(row)
        return rows

    # -- rf health --

    def _rf_section(self) -> list[RenderableType]:
        """The reception quality of the time window, and the live numbers of the radio itself.

        It is rendered as a labelled grid. Thus an item that wraps on a narrow terminal
        aligns its continuation with its own block, not with the start of the line.
        """
        heading = Text("RF health", style="accent")
        heading.append("  ·  reception over 2 h · radio live", style="muted")
        rows: list[tuple[str, Text]] = []

        # The window digest of one pass (refer to _digest_window), or the packets of the
        # narrowed view.
        if self._scope:
            snrs, rssis = self._scoped().snrs, self._scoped().rssis
        else:
            snrs, rssis = self._win_snrs, self._win_rssis
        if snrs:
            med = median(snrs)
            line = Text(f"{med:+.1f} dB median  ", style=snr_style(med))
            line.append_text(snr_bar(med))
            lo, hi = min(snrs), max(snrs)
            line.append(f"  worst {lo:+.1f} · best {hi:+.1f}", style="muted")
            rows.append(("snr", line))
        if rssis:
            line = Text(f"{median(rssis):.0f} dBm median")
            line.append(f"  weakest {min(rssis):.0f} · strongest {max(rssis):.0f}", style="muted")
            rows.append(("rssi", line))

        stats = self.stats
        if stats.get("noise_floor") is not None:
            line = Text(f"{stats['noise_floor']} dBm noise floor")
            if stats.get("last_rssi") is not None:
                line.append(f" · last RSSI {stats['last_rssi']} dBm", style="muted")
            if stats.get("last_snr") is not None:
                line.append(f" · last SNR {stats['last_snr']:+.1f} dB", style="muted")
            rows.append(("radio", line))
        extras = Text()
        label = "airtime"
        if stats.get("tx_air_secs") is not None:
            extras.append(f"TX {stats['tx_air_secs']} s · RX {stats.get('rx_air_secs', 0)} s")
        level = self.battery.get("level")
        if level:
            if extras.plain:
                extras.append("  ·  ", style="muted")
            else:
                label = "battery"
            extras.append(f"{int(level) / 1000:.2f} V")
        if extras.plain:
            rows.append((label, extras))

        if not rows:
            return [heading, Text("no receptions in the window yet", style="muted")]
        return [heading, self._grid(rows)]

    def _name(self, node: str | None) -> str:
        """The friendly name of a node if known, or else its raw hash (never ``None``)."""
        if not node:
            return "?"
        named = self._resolve(node)
        return named if named else node


async def open_dashboard(ctx: AppContext) -> None:
    """Open the live dashboard, and run it until the user closes it.

    The function connects the screen to its data feeds. The stored trailing time window
    gives the first data. A hub subscription sends each new packet in. The function polls
    the statistics of the device itself at a slow rate (and skips the poll without a
    message while nothing is connected). A ticker paints each second, so that the rates
    and ages stay correct. When the screen closes, the function stops and removes all of
    these.

    Args:
        ctx: The shared application context (it must run the interactive TUI surface).

    Raises:
        RuntimeError: If the caller is not in the interactive menu (there is no
            full-screen session).
    """
    from ..services import trace_runner
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - the only caller is the menu
        raise RuntimeError("the dashboard is only available in the menu")
    session = ctx.ui.session

    contacts = []
    try:
        if ctx.is_connected or ctx.settings.connect_on_start:
            # Through the session cache. The contacts are one of the slowest reads when a
            # screen opens. Thus MeshTerm reads them one time (not at each open of the
            # dashboard), and this keeps navigation fast over Bluetooth.
            contacts = await ctx.devstate.contacts()
    except Exception:  # noqa: BLE001 - the dashboard renders correctly without contact names
        contacts = []
    # The contacts first, and all the names that the recorder ever heard as the fallback.
    # This is the rule of the whole app: a node that we can name never renders as a bare
    # hash.
    resolve = trace_runner.make_node_resolver(contacts, ctx.repo.node_names())

    window = ctx.repo.recent_observations(since=utcnow() - OBSERVATION_WINDOW)
    screen = DashboardScreen(
        session=session,
        resolve=resolve,
        window=window,
        activity=ctx.monitor.activity_histogram,
        activity_flags=ctx.monitor.activity_session_flags,
        kind_counts=ctx.monitor.kind_counts,
        scope_of=ctx.region_store.scope_of if ctx.region_store is not None else None,
    )

    unsubscribe = ctx.events.subscribe(screen.on_event)

    async def poll_stats() -> None:
        """Refresh the numbers of the radio itself at a slow rate, while it is connected."""
        while True:
            if ctx.is_connected:
                try:
                    device = await ctx.device()
                    screen.stats = dict(await device.get_stats() or {})
                    screen.battery = dict(await device.get_battery() or {})
                except Exception:  # noqa: BLE001 - optional reads. Keep the last good values.
                    pass
                session.invalidate()
            await asyncio.sleep(_STATS_POLL_S)

    async def tick() -> None:
        """Paint each second, so that the rates, the ages, and the chart clock stay correct."""
        while True:
            await asyncio.sleep(_REFRESH_S)
            session.invalidate()

    poller = asyncio.ensure_future(poll_stats())
    ticker = asyncio.ensure_future(tick())
    try:
        await session.run_screen(screen)
    finally:
        unsubscribe()
        for task in (poller, ticker):
            task.cancel()
        for task in (poller, ticker):
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - the teardown must never show a poll failure
                pass
