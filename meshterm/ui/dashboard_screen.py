# SPDX-License-Identifier: Apache-2.0
"""The live mesh dashboard: everything going on around this node, on one screen.

The interactive face of the ``dashboard`` tool. One full-screen, always-repainting view
stacks three reads of the mesh, coarsest first:

* **Activity** — a tall braille bar chart of *every* packet the hub hears (adverts,
  telemetry, RX-logged packets, messages, acks), one dot column per minute — braille's
  full horizontal resolution, two minutes per character — stretched across whatever
  width the terminal offers, newest at the right (the app-wide timeline direction)
  with the count scale mirrored on both edges. The header indicator's big sibling,
  drawn from the same :meth:`~meshterm.services.monitor_service.MonitorService`
  buckets. A pulse line beneath it reads the rate, who's been heard, and the busiest
  node of the window.
* **Traffic** — the frames heard on the air, tallied by class (chan text, trace,
  path, …) under the names and icons the live feed gives them, each with a
  proportional bar. Every frame counts once: what the device *reported* about a frame
  (its advert event, a decoded message) is not counted a second time beside it.
* **RF health** — the trailing window's reception quality: median SNR (on the trace
  tool's quality bar) and RSSI from stored observations, plus the radio's own live
  numbers — noise floor, last RSSI/SNR, airtime, battery — polled from the device the
  way Device info reads them.

The per-packet stream that used to close this screen is now its own tool — the Live
feed (see :mod:`meshterm.ui.livefeed_screen`), right under the dashboard in the menu —
so this screen is pure overview: it scrolls as one body under the arrow/page keys.

The screen holds no subscriptions of its own — the opener (:func:`open_dashboard`)
wires the hub subscription, the device-stats poll, and the once-a-second repaint, and
tears them all down when the screen resolves. Esc backs out directly.
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from statistics import median
from typing import TYPE_CHECKING, Any

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from ..core.events import MeshEvent
from ..core.models import NODE_TYPE_REPEATER, Observation, utcnow
from ..persistence.repository import OBSERVATION_WINDOW
from .braillechart import axis_chart, axis_chrome, axis_label_w, meter, timeline_rows
from .packet_viewer import KIND_STYLES, payload_marks
from .theme import snr_style
from .trace_screen import snr_bar
from .tui.render import render_lines
from .tui.screen import Screen

if TYPE_CHECKING:
    from ..context import AppContext

#: Seconds between full repaints while the dashboard is open (ages, rates, spinner-less).
_REFRESH_S = 1.0

#: Seconds between polls of the device's own statistics (noise floor, airtime, battery).
_STATS_POLL_S = 10.0

#: How many braille rows tall the activity chart draws (each row is four dot rows).
_CHART_ROWS = 3

#: The order the traffic panel lists its classes in (heard ones not listed sort last,
#: alphabetically): chat-ish frames before protocol-ish ones, the class-less ``packet``
#: closing the list.
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
    """Whether a tally bucket counts frames heard on the air (``packet[:<TYPENAME>]``).

    The monitor also tallies what the *device* reported — ``advert``, ``telemetry``,
    ``message``, ``ack`` — but each of those is a frame already counted under its own
    class (the advert event *and* its RX-logged ``ADVERT`` frame), so the traffic panel
    counts frames only: every packet once, under the name the live feed gives it.
    """
    return bucket == "packet" or bucket.startswith("packet:")


def _traffic_chrome(bucket: str) -> tuple[str, str]:
    """One traffic bucket's ``(icon, label)`` — the live feed's, from the same function.

    Icons are mapped to single glyphs on PICOCALC via :func:`~meshterm.ui.theme.glyph`.
    """
    typename = bucket.split(":", 1)[1] if ":" in bucket else None
    return payload_marks(typename)


#: How many character cells a traffic lane's meter spans (48 half-step levels).
_TRAFFIC_METER_CELLS = 24

#: The label column every hanging-indent section grid reserves (the ``snr      `` /
#: ``radio    `` lane), so wrapped values align with their own block, never column 0.
_GRID_LABEL_W = 9


def _span_label(minutes: int) -> str:
    """A compact duration — ``45 min`` under two hours, else ``2.5 h`` / ``3 h``."""
    if minutes < 120:
        return f"{minutes} min"
    text = f"{minutes / 60:.1f}"
    return (text[:-2] if text.endswith(".0") else text) + " h"


class DashboardScreen(Screen):
    """The live mesh overview. Renders state and scrolls; the opener feeds it."""

    floating = False

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The footer keys — advertising scroll only when the overview actually overflows.

        The dashboard is one scrolling body, but on a tall terminal it often fits whole; a
        static ``↑↓ PgUp/PgDn scroll`` then names keys that do nothing. Show that atom only
        while the content is taller than the viewport (see
        :attr:`~meshterm.ui.tui.screen.Screen.content_overflows`); otherwise Esc is the only
        key that acts.
        """
        if self.content_overflows:
            return "↑↓ PgUp/PgDn scroll · Esc back"
        return "Esc back"

    @property
    def fkey_lane(self):
        """The shared lane, dimmed on the same gate the hint above uses.

        The PicoCalc draws no hint line at all — the lane *is* the footer — so the rule
        the hint follows has to hold there too: an overview that fits whole has nothing
        for Top/End or the paging pair to move.
        """
        from .tui.fkeys import default_lane

        return default_lane(nav=self.content_overflows)

    def __init__(
        self,
        *,
        session: Any,
        resolve: Any,
        window: list[Observation],
        activity: Any,
        activity_flags: Any,
        kind_counts: Any,
    ) -> None:
        """Create the dashboard over its data feeds.

        Args:
            session: The running TUI session (for repaints).
            resolve: Maps a node hash to a friendly contact name when known.
            window: The stored observations seeding the trailing window (oldest first).
            activity: Zero-arg callable returning the monitor's all-packet histogram.
            activity_flags: Zero-arg callable returning the histogram's per-bucket
                this-session flags (seeded history draws grey, live traffic green).
            kind_counts: Zero-arg callable returning the monitor's kind tallies.
        """
        super().__init__()
        self.title = "Dashboard — mesh overview"
        self._session = session
        self._resolve = resolve
        self._activity = activity
        self._activity_flags = activity_flags
        self._kind_counts = kind_counts
        #: The trailing window of observations (stored seed + live), oldest first.
        self._window: deque[Observation] = deque(window, maxlen=4000)
        #: The device's own numbers, refreshed by the opener's poll (Device info's
        #: dynamic rows): ``stats`` from get_stats, ``battery`` from get_battery.
        self.stats: dict = {}
        self.battery: dict = {}
        # The one-pass window aggregates (see _digest_window), refreshed whenever the
        # window itself moves — which is what ``_window_rev`` counts and ``_digest_rev``
        # remembers. The screen repaints once a second to keep its clocks honest, and on a
        # quiet mesh every one of those frames would otherwise re-walk the same 4000
        # observations to reach the identical numbers.
        self._window_rev = 0
        self._digest_rev = -1
        self._win_nodes: set[str] = set()
        self._win_repeaters: set[str] = set()
        self._win_counts: Counter = Counter()
        self._win_snrs: list[float] = []
        self._win_rssis: list[float] = []
        #: The last activity chart and the inputs it was drawn from — see
        #: :meth:`_activity_section`.
        self._chart_memo: tuple[tuple, list[RenderableType]] | None = None

    # --- live window -----------------------------------------------------------------

    def on_event(self, event: MeshEvent) -> None:
        """Fold one hub observation into the trailing window, and repaint."""
        obs = event.observation
        if obs is not None:
            self._window.append(obs)
            self._window_rev += 1
            self._prune()
            self._session.invalidate()

    def _prune(self) -> None:
        """Drop window observations that aged past the trailing window."""
        cutoff = utcnow() - OBSERVATION_WINDOW
        while self._window and self._window[0].observed_at < cutoff:
            self._window.popleft()
            self._window_rev += 1

    # --- input -----------------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Scroll the overview body, or dismiss."""
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
        elif action == "escape":
            self.resolve(None)

    # --- rendering ---------------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render the overview sections as one scrolling body."""
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
        """Fold the observation window into the per-frame aggregates, in one pass.

        The pulse line, the busiest-transmitter row, and the RF section each need a
        different slice of the same window; scanning the (up to 4000-entry) deque once
        per consumer added up to five full passes on every repaint of a busy mesh.
        One pass here fills them all; the section builders read the results.

        And that pass runs only when the window has actually moved. The screen repaints
        every second so its rates, ages and clock stay honest, but the aggregates are a
        pure function of the deque — on a mesh quiet for a beat, every one of those frames
        was re-walking four thousand observations to arrive at the numbers it already had.
        The window changes in exactly two places (an arriving observation, an expiring one),
        both of which bump the revision this compares against.
        """
        if self._digest_rev == self._window_rev:
            return
        self._digest_rev = self._window_rev
        nodes: set[str] = set()
        repeaters: set[str] = set()
        counts: Counter = Counter()
        snrs: list[float] = []
        rssis: list[float] = []
        for o in self._window:
            if o.node and o.node_type == NODE_TYPE_REPEATER:
                repeaters.add(o.node)
            if o.kind == "packet":
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

    # -- activity --

    def _activity_section(self, width: int) -> list[RenderableType]:
        """The all-packet chart — newest minute at the right — plus the pulse line.

        One dot column per minute, two per character cell, stretched across every
        cell the terminal offers between the scale gutters (drawn as the platform
        draws them — the desktop mirrors its marks on the right, the console keeps
        the tick alone; see :func:`~meshterm.ui.braillechart.axis_chrome`); a wider
        terminal simply shows more history. Time runs oldest→now left to right,
        every MeshTerm timeline's direction.
        """
        histogram = list(self._activity())  # newest first, one count per minute
        # Size the label lane from the whole histogram's peak (not just the visible
        # slice) so the gutters never shift as a burst scrolls out of view.
        label_w = axis_label_w(max(histogram, default=0), _CHART_ROWS)
        chars = max(10, width - axis_chrome(label_w))
        minutes = chars * 2
        shown = (histogram + [0] * minutes)[:minutes]
        peak = max(shown)

        heading = Text("Activity", style="accent")
        heading.append("  ·  every packet heard · one minute per dot column", style="muted")
        # Buckets seeded from a previous session's stored history draw grey; only
        # what this session heard itself pulses green.
        flags = (tuple(self._activity_flags()) + (True,) * minutes)[:minutes]
        styles = ["ok" if live else "muted" for live in reversed(flags)]

        # The chart is a pure function of these numbers, and on the idle second-tick they
        # are the same numbers: the histogram only moves when a packet lands or the minute
        # rolls over. Rasterizing the braille rows and their axis chrome is the priciest
        # part of this screen's paint, so it is done once per distinct picture rather than
        # once per repaint. One slot — the chart that just changed is the one to keep.
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
        """Labelled rows with a hanging indent: values wrap within their own block.

        The app-wide alignment rule — a wrapped item's continuation lines align with
        the item, never with the line start — done as a two-column frameless grid:
        the label lane is fixed at :data:`_GRID_LABEL_W` cells, the value column
        soaks up the rest and folds inside itself.
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
        """Rates, who's been heard, and the window's busiest transmitter.

        ``shown`` is the chart's visible slice (newest first, one bucket per minute),
        so the quoted rates describe exactly what the chart draws. The word "heard"
        after the node count is a luxury: it is kept only when the line fits ``width``
        without wrapping.
        """
        recent = sum(shown[:15]) / max(1, min(15, len(shown)))
        overall = sum(shown) / max(1, len(shown))
        span = _span_label(len(shown))
        nodes = self._win_nodes  # the one-pass window digest (see _digest_window)
        repeaters = self._win_repeaters

        def compose(heard: bool) -> Text:
            line = Text()
            line.append(f"{recent:.1f} pkt/min", style="brand")
            line.append(f" (15 m) · {overall:.1f} ({span})", style="muted")
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
        busiest = self._busiest()
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
        """The window's most-heard attributable node, or ``None`` in silence."""
        counts = self._win_counts  # the one-pass window digest (see _digest_window)
        if not counts:
            return None
        node, count = counts.most_common(1)[0]
        return self._name(node), count

    # -- traffic --

    def _traffic_section(self) -> list[RenderableType]:
        """Frames heard on the air, by class, each with an icon and a proportional meter.

        Every class shows once heard, named as the live feed names it; classes never
        heard stay hidden. Every bar is the one colour the feed draws a frame's class in
        (``packet``'s): the icon and the label say which class a row is, so colour has
        nothing left to say here.
        """
        heading = Text("Traffic", style="accent")
        heading.append("  ·  by packet class · stored history + live", style="muted")
        counts = {k: n for k, n in self._kind_counts().items() if _is_frame_bucket(k)}
        if not counts:
            return [heading, Text("nothing heard yet", style="muted")]
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
            # The shared braille meter, full-height with no track: the lane resolves
            # 48 levels across its 24 cells (two half-steps per cell).
            row.append_text(meter(count / peak, _TRAFFIC_METER_CELLS, style=style))
            rows.append(row)
        return rows

    # -- rf health --

    def _rf_section(self) -> list[RenderableType]:
        """The window's reception quality plus the radio's own live numbers.

        Rendered as a labelled grid so an item that wraps on a narrow terminal
        aligns its continuation with its own block, not the line start.
        """
        heading = Text("RF health", style="accent")
        heading.append("  ·  reception over 2 h · radio live", style="muted")
        rows: list[tuple[str, Text]] = []

        snrs = self._win_snrs  # the one-pass window digest (see _digest_window)
        rssis = self._win_rssis
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
        """A node's friendly name when known, else its raw hash (never ``None``)."""
        if not node:
            return "?"
        named = self._resolve(node)
        return named if named else node


async def open_dashboard(ctx: AppContext) -> None:
    """Open the live dashboard and run it until dismissed.

    Wires the screen to its feeds: the stored trailing window seeds it, a hub
    subscription streams every new packet in, the device's own statistics are polled
    on a slow cadence (skipped quietly while nothing is connected), and a once-a-second
    ticker keeps rates and ages honest. Everything is torn down when the screen closes.

    Args:
        ctx: The shared application context (must be running the interactive TUI surface).

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from ..services import trace_runner
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the dashboard is only available in the menu")
    session = ctx.ui.session

    contacts = []
    try:
        if ctx.is_connected or ctx.settings.connect_on_start:
            # Through the session cache: contacts are one of the slowest reads on a
            # screen open, so reading them once (not per dashboard open) is what keeps
            # navigation snappy over Bluetooth.
            contacts = await ctx.devstate.contacts()
    except Exception:  # noqa: BLE001 - the dashboard renders fine without contact names
        contacts = []
    # Contacts first, every name the recorder ever overheard as the fallback — the
    # app-wide rule that a node we can name never renders as a bare hash.
    resolve = trace_runner.make_node_resolver(contacts, ctx.repo.node_names())

    window = ctx.repo.recent_observations(since=utcnow() - OBSERVATION_WINDOW)
    screen = DashboardScreen(
        session=session,
        resolve=resolve,
        window=window,
        activity=ctx.monitor.activity_histogram,
        activity_flags=ctx.monitor.activity_session_flags,
        kind_counts=ctx.monitor.kind_counts,
    )

    unsubscribe = ctx.events.subscribe(screen.on_event)

    async def poll_stats() -> None:
        """Refresh the radio's own numbers on a slow cadence, while connected."""
        while True:
            if ctx.is_connected:
                try:
                    device = await ctx.device()
                    screen.stats = dict(await device.get_stats() or {})
                    screen.battery = dict(await device.get_battery() or {})
                except Exception:  # noqa: BLE001 - optional reads; keep the last good ones
                    pass
                session.invalidate()
            await asyncio.sleep(_STATS_POLL_S)

    async def tick() -> None:
        """Repaint once a second so rates, ages, and the chart's clock stay honest."""
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
            except Exception:  # noqa: BLE001 - teardown must never surface a poll hiccup
                pass
