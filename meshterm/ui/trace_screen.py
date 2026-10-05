# SPDX-License-Identifier: Apache-2.0
"""The live trace screens: compose a route from observed topology, then watch it answer.

This is the interactive face of the two trace tools (the scripted CLI keeps its one-shot
table output). A trace is one walked path — the protocol has no destination field, so
"target" is purely a UX notion — and the two menu features split along exactly that line:

* **Trace target** (:func:`open_trace`) answers *can I reach this node?* The route is
  symmetric: compose (or explore) the outbound leg, and the return is those hops
  mirrored around the pinned target.
* **Trace path** (:func:`open_trace_path`) answers *how far can a route I build carry?*
  There is no target at all: the whole walk is composed by hand, out and back by
  whatever way you choose, and it only has to end within our own earshot.

Both open the same screen, *armed but idle*: the last-known route shows, the path is
whatever you make it, and nothing transmits until you say so. An action list drives it —
↑/↓ select a row, Enter commits it:

* **Compose path** opens the path composer (:mod:`~meshterm.ui.path_composer`): build
  the route hop by hop, each step suggested from the links observed in *received*
  traffic — traces, firmware-learned contact routes, and RX-logged packet paths —
  strongest first, with raw hex entry for nodes the data has never seen. Standing on a
  repeater you hold admin credentials for, the composer can also *fetch that repeater's
  neighbour table* over the mesh (login required; firmware ignores guests):
  second-vantage evidence, persisted and folded straight back into the suggestions.
  Committing the composer parks the action cursor on Trace — compose, then plain
  Enter walks it — while backing out with Esc leaves the cursor where it was.
* **Reverse path** (Trace path only — a target-mode route is a symmetric boomerang,
  so flipping it changes nothing) reverses the walk's hop order: ``us → a → b → c``
  becomes ``us → c → b → a``. Radio links rarely read the same both ways, so this
  measures the route you built in the opposite direction. It adopts the reversed spec
  like any path change (the forward run's aggregates clear) and leaves Trace idle —
  nothing auto-fires — so you flip, then trace; reverse again to go back. The stored
  history keeps both directions.
* **Explore paths** (Trace target only — candidates need a destination) explores
  scenarios: ranked candidate routes to the target straight from the topology evidence
  (the device's own learned route, the direct shot, and the strongest observed
  alternatives). Adopt one directly — or probe them all, one measured trace per
  candidate, persisted as ``path_candidates`` rows and ranked reliability-first, with
  the winner offered for adoption. Evidence proposes, measurement decides, you dispose.
* **Path width** floats a small dialog picking the per-hop path-hash width (1, 2, or
  4 bytes — the leading key slice forced hops are addressed by). It seeds from the
  device's routing width; changing it re-renders a standing forced path at the new
  width and shapes every spec the composer and explorer emit after it.
* **Sample count** floats a dialog picking how many traces one Trace action runs
  (1, 2, 3, 5, or 8). Multi-trace runs are *paced* — a cooldown sleeps between
  transmissions, because repeaters penalize (and can blacklist) nodes that burst
  traffic — and the in-flight dialog counts them off as replies stream in.
* **Trace** (the cursor opens here, so plain Enter still just traces) runs the chosen
  number of traces. While they fly, a floating *tracing* dialog (spinner, progress,
  Abort) sits over the screen — replies stream into the log behind it, and Esc in the
  dialog cancels without leaving the screen; traces already recorded are kept. The
  screen keeps aggregating every trace of the session into its medians. A walk that
  crosses some link twice in the same direction first confirms on an amber
  Cancel/Trace dialog — such a walk isn't a *trail*, so the trophy case ignores it
  (the no-cheat rule; see :mod:`~meshterm.services.records`) — and a run that places
  on the boards floats a *New record* dialog once it settles: Close (the default)
  stays on the screen, Trophy case unwinds the whole session and opens the boards,
  so leaving them lands on the main menu.
* **Back** leaves the screen, exactly like Esc.

Layout, top to bottom: the walked route (live when a reply has landed, else the route
the next Trace will walk — composed by hand, or auto-resolved from the device's learned
route or the stored history and labelled with that provenance — else the most recent
stored trace), the run's robust aggregates, the wire spec that route amounts to, the
action list, then the results — per-hop median SNR with quality bars and the
individual traces newest-first — on the same page: ↑/↓ belong to the action cursor and
edge-scroll on into the results past the last action, PgUp/PgDn/Home/End page the
whole screen, and ←/→ slide a hop's path where it runs past the edge.

Both path lanes draw through THE path widget (:mod:`~meshterm.ui.pathline`) and break
at hop boundaries under their own value column: a route folds between nodes — never
parting a name from the hash it is addressed by, and preferring the turn where a
boomerang's mirrored return begins — while the spec below it folds after a comma,
never mid-hash. Whatever a route's provenance is (an auto plan's source, a previous
walk's timestamp) hangs on the line under it rather than crowding its right edge.

Every trace is persisted exactly like a scripted run: one ``runs`` row per trace,
recorded under it, so the stored history reads the same no matter which front end
produced it. Path walks record under :data:`~meshterm.core.models.PATH_TRACE_TARGET`,
keeping them out of the target picker's history while still seeding the path screen's
previous-route line.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

from rich.cells import cell_len
from rich.console import Group, RenderableType
from rich.text import Text

from ..core.models import PATH_TRACE_TARGET, HopAggregate, TraceResult, TraceStats
from ..services import trace_runner
from ..services.records import first_repeated_edge
from ..services.topology import render_forced_spec
from .braillechart import meter
from .menus import icon_lane, icon_mark, marked_label, section_heading
from .pathline import (
    ELIDE_HEAD,
    ELIDE_TAIL,
    PathHop,
    PathLine,
    cut_mark,
    cut_to,
    hops_atom,
    path_line,
)
from .theme import snr_style
from .tui.render import crop_cells, render_lines, render_to_ansi
from .tui.screen import Screen
from .tui.spinner import Spinner, spinner_interval
from .widgets import NodeResolver, highlighted_hash, link_text, route_path

if TYPE_CHECKING:
    from ..context import AppContext

#: How wide the per-hop SNR quality bars draw, in characters. Each character packs
#: two fill steps (see :func:`snr_bar`), so the bar reads at 16-step resolution in
#: half the columns a one-step-per-cell block bar would need.
_BAR_WIDTH = 8

#: The SNR range the bars span, in dB: -15 (barely readable) to +10 (excellent). Values
#: outside clamp to the ends, so the bar always shows *something* for a heard hop.
_BAR_SNR_MIN = -15.0
_BAR_SNR_MAX = 10.0

#: Cells one ←/→ press slides the per-hop paths by — the step every other sliding path
#: row in the app takes (the select list's ``hscroll``, the node page's routes).
_HSCROLL_STEP = 8

#: The sample counts the Sample count dialog offers: how many traces one Trace action
#: runs, paced between transmissions.
SAMPLE_CHOICES = (1, 2, 3, 5, 8)

#: The new-record dialog's *Trophy case* button value. Choosing it opens the trophy case
#: over the trace screen, which stays pushed underneath — Esc from the trophy case is one
#: pop back onto the trace that earned the record.
OPEN_TROPHY_CASE = "records"


def _trophy_case_opener(ctx: AppContext) -> Callable[[], Awaitable[None]]:
    """A zero-argument opener for the trophy case, for the new-record dialog's left button.

    Imported inside the closure because the trophy case imports *this* module back (its
    records offer a *Trace this path* of their own) — the two screens open each other, which
    is exactly the cycle a strict stack allows and ^W exists to climb out of.
    """

    async def open_it() -> None:
        from .records_screen import open_records

        await open_records(ctx)

    return open_it


#: A single-trace runner: ``(path_spec, on_trace)`` → runs exactly one trace, handing
#: the result — plus any trophy-case disciplines the walk just placed in, as short
#: ``"Title — score"`` labels — to ``on_trace`` when it lands. Provided by the session
#: openers, which close over the device, repository, and settings so the screen stays
#: free of persistence and scoring concerns.
TraceOnce = Callable[[str, Callable[[TraceResult, Sequence[str]], None]], Awaitable[None]]

#: A path picker flow: takes the current spec, runs its own dialogs over the screen, and
#: resolves to the new spec (``""`` = device-routed) or ``None`` to keep the current one.
#: The sample-count flow shares the shape and simply always resolves ``None``.
PathFlow = Callable[[str], Awaitable[str | None]]

#: The route lane's label column: the word plus its padding. Its width is the column
#: the route's wrapped continuations (and its provenance note) hang under.
_ROUTE_LANE = "route  "

#: The path lane's label column — the aggregate block's own label width, so the wire
#: spec starts, and hangs, in the same column as the numbers above it.
_PATH_LANE = "path            "

#: Every mark the action rows lead with, in row order. The list exists to *measure*
#: the icon column: ``⚡`` is an emoji, two cells where the rest are one, so a hard
#: ``"icon "`` prefix would start Explore's label a column right of every other
#: action's. See :func:`_icon_lane`.
_ACTION_ICONS = ("✎", "⇄", "⚡", "⚙", "#", "▶")


def _icon_lane() -> int:
    """The action rows' icon column, in cells — this screen's own marks, measured once."""
    return icon_lane(_ACTION_ICONS)


def _icon(text: Text, icon: str, style: str) -> None:
    """Append an action row's mark in the icon column, its trailing space included."""
    text.append_text(icon_mark(icon, style, _icon_lane()))


def _lane_lines(label: str, value: list[Text], width: int) -> list[str]:
    """Render a labelled lane whose value hangs, already broken, under its own column.

    The counterpart to :func:`~meshterm.ui.tui.render.render_hanging` for values that
    know their own line breaks: :meth:`~meshterm.ui.pathline.PathLine.wrapped` hands
    back lines that hop-wrap and carry their indent, so re-wrapping them here would
    only undo that. The label rides the first line; the rest are drawn as given.

    Args:
        label: The lane's label column, padding included (the value hangs under it).
        value: The value's lines — first bare, continuations self-indented.
        width: The render width; lines are cropped, never folded.

    Returns:
        The rendered ANSI lines.
    """
    lines: list[str] = []
    for i, line in enumerate(value):
        row = Text(label, style="muted") if i == 0 else Text()
        row.append_text(line)
        lines.append(render_to_ansi(row, width, no_wrap=True))
    return lines


def _slid(line: Text, shift: int, width: int) -> Text:
    """``line`` seen through a ``width``-cell window slid ``shift`` cells in.

    Each edge the line continues past wears :func:`~meshterm.ui.pathline.cut_mark` — a
    chip broken off in its own fill, an arrow line's ``…`` — and the marks are chrome
    inside the window, so each costs it a cell. The same window the select list's
    ``hscroll`` rows and the node page's routes slide through.
    """
    left = 1 if shift else 0
    inner = max(1, width - left)
    right = 1 if shift + inner < line.cell_len else 0
    window = max(1, inner - right)
    out = Text(no_wrap=True)
    if left:
        out.append_text(cut_mark(line, shift, ELIDE_HEAD))
    out.append_text(crop_cells(line, shift, window))
    if right:
        out.append_text(cut_mark(line, shift + window - 1, ELIDE_TAIL))
    return out


def _note(text: str, indent: int, *, style: str = "faint") -> Text:
    """A lane's trailing note — provenance, a stamp, a count — on its own hanging line."""
    return Text(" " * indent + text, style=style)


def _spec_line(spec: str) -> PathLine:
    """The literal wire spec as a path line: comma-joined, each hop in its own hue.

    Exactly the characters the radio is handed (``3d63,7f21,3d63``) — the lane's whole
    point is being the verbatim spec, so it stays plain-mode text rather than becoming
    chips. What THE path widget adds is the app-wide key colouring (each addressed
    slice lit in its own key-derived hue instead of one flat brand smear) and hop-
    boundary wrapping, so a spec too long for its column breaks after a comma.

    Args:
        spec: The wire spec (comma-separated hex hop hashes; blanks tolerated).

    Returns:
        The spec's :class:`~meshterm.ui.pathline.PathLine`, comma-separated.
    """
    tokens = [t.strip() for t in spec.split(",") if t.strip()]
    hops = [PathHop(t, key=t, lit_bytes=len(t) // 2) for t in tokens]
    return PathLine(hops, mode="plain", separator=",")


def snr_bar(snr: float | None, width: int = _BAR_WIDTH) -> Text:
    """Render an SNR reading as a horizontal quality bar in the shared SNR colours.

    The slim-on-a-track flavour of the app's braille :func:`~meshterm.ui.braillechart.meter`:
    the reading maps onto :data:`_BAR_SNR_MIN` → :data:`_BAR_SNR_MAX` and fills a dark
    same-glyph track at two steps per cell.

    Args:
        snr: The reading in dB, or ``None`` (renders as an entirely unlit track).
        width: Bar track width in characters (each worth two fill steps).

    Returns:
        A :class:`Text` of filled braille cells over a dark, same-glyph track,
        coloured by :func:`~meshterm.ui.theme.snr_style`.
    """
    if snr is None:
        return meter(None, width, style="track", slim=True, track="track")
    span = _BAR_SNR_MAX - _BAR_SNR_MIN
    frac = min(1.0, max(0.0, (snr - _BAR_SNR_MIN) / span))
    return meter(frac, width, style=snr_style(snr), slim=True, track="track")


class TracingDialog(Screen):
    """The floating in-flight dialog: a spinner chip, live progress, and Abort.

    Pushed over the trace screen while traces (or a scenario probe) transmit, so the
    activity — and the way out — is unmissable while replies keep streaming into the
    screen behind it. Enter, Esc, or Space aborts via the injected callback; the owner
    pops the dialog when the work finishes, so it never resolves a value of its own.
    """

    footer_hint = "Enter/Esc abort"

    @property
    def picocalc_lyra_lane(self):
        """No lane: the dialog's only verb is abort, and it already answers to Enter and Esc."""
        from .tui.fkeys import EMPTY_LANE

        return EMPTY_LANE

    def __init__(self, title: str, *, spinner: Spinner, on_abort: Callable[[], None]) -> None:
        """Build the dialog.

        Args:
            title: Heading for the dialog border (e.g. ``Tracing — Lakeside``).
            spinner: The spinner to animate (shared with the owner's ticker).
            on_abort: Invoked when the user asks to abort (idempotent expected).
        """
        super().__init__()
        self.title = title
        self._spinner = spinner
        #: Invoked when the user aborts. Public so an owner whose task only exists
        #: after the dialog does (the probe sweep) can rewire it.
        self.on_abort = on_abort
        #: One-line progress, updated by the owner as replies land.
        self.status = "transmitting…"
        #: The most recent trace result, echoed beneath the status line.
        self.last: TraceResult | None = None
        #: Whether to render the last-reply line. Trace/probe owners stream replies
        #: through it; request-shaped owners (a neighbour fetch) have none to show.
        self.show_last = True

    @property
    def dialog_width(self) -> int:
        """Natural outer width hugging the widest line (compositor still caps it)."""
        widths = [
            cell_len(self.title),
            cell_len(self.footer_hint),
            cell_len(self.status) + 4,
            len("last  ✗ no reply — will retry"),
            len("  Abort  "),
        ]
        return max(widths) + 8

    def _last_line(self) -> Text:
        """The most recent reply, or a waiting note before the first one lands."""
        line = Text("last  ", style="muted")
        if self.last is None:
            line.append("waiting for the first reply…", style="muted")
        elif not self.last.success:
            line.append("✗ no reply", style="err")
        else:
            line.append("✓ ", style="ok")
            line.append(f"{self.last.hop_count} hop{'s' if self.last.hop_count != 1 else ''}")
            if self.last.min_snr is not None:
                line.append("  min ", style="muted")
                line.append(f"{self.last.min_snr:+.1f} dB", style=snr_style(self.last.min_snr))
            if self.last.round_trip_ms is not None:
                line.append(f"  {self.last.round_trip_ms:.0f} ms", style="muted")
        return line

    def render_body(self, width: int) -> list[str]:
        """Render the spinner chip, the last reply, and the Abort button.

        The button is the app's one button look — a centered reverse-video chip in the
        shared ``selected`` fill, exactly as the ButtonDialog and reconnect dialog draw
        theirs — so every popup's committing control reads the same.
        """
        from .tui.prompt import _center

        chip = self._spinner.text()
        chip.append(f"  {self.status}", style="")
        lines = render_lines(chip, width)
        if self.show_last:
            lines.extend(render_lines(self._last_line(), width))
        lines.append("")
        button = Text("  Abort  ", style="selected")
        lines.append(render_to_ansi(_center(button, width), width))
        return lines

    def handle(self, action: str, data: str = "") -> None:
        """Any commit/dismiss key aborts the in-flight work; everything else is inert."""
        if action in ("enter", "escape", "space"):
            self.on_abort()


class TraceScreen(Screen):
    """A full-screen live trace session — armed, but idle until told.

    One screen serves both trace features; ``mode`` decides which. ``"target"`` traces
    a pinned destination over a symmetric (mirrored) route and offers *Explore paths*;
    ``"path"`` walks a hand-composed route with no target at all — no Explore (ranked
    candidates need a destination), no device routing, but a *Reverse path* that flips
    the asymmetric walk end-for-end, and Trace stays inert until a path exists to walk
    (composed here, or auto-resolved from the last stored walk).

    Adopting a different path — composed, explored, reversed, or re-rendered at a new
    width — restarts the measurement: the aggregates, per-hop medians, and trace log
    all described the old route, so they clear as if the screen had just opened.

    ↑/↓ move the cursor over the action rows and Enter commits the selected one — the
    cursor opens on Trace, so plain Enter still just traces. The results (per-hop
    medians and the trace log) follow the actions on the page: ↓ off the last action
    edge-scrolls on into them, PgUp/PgDn/Home/End page the whole screen, and Esc (or
    the Back row) backs out, cancelling any in-flight trace; already-recorded traces
    are kept.
    """

    floating = False

    def __init__(
        self,
        target: str,
        *,
        mode: str = "target",
        device_label: str,
        device_hash: str | None,
        resolve: NodeResolver,
        session: Any,
        trace: TraceOnce,
        compose_path: PathFlow,
        pick_width: PathFlow,
        pick_samples: PathFlow,
        width_bytes: Callable[[], int],
        sample_count: Callable[[], int],
        explore: PathFlow | None = None,
        pace_s: float = 1.0,
        previous: TraceResult | None = None,
        auto_spec: Callable[[], str] = lambda: "",
        auto_source: str = "",
        initial_spec: str = "",
        open_trophy_case: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Create the screen (nothing transmits until the user commits Trace).

        Args:
            target: The label traces persist under — the destination's name in target
                mode, :data:`~meshterm.core.models.PATH_TRACE_TARGET` in path mode.
            mode: ``"target"`` (symmetric, explorable) or ``"path"`` (hand-composed
                walk, no destination).
            device_label: Our own node's name, labelling the route's endpoints.
            device_hash: Our own public key, so the endpoints carry a hash like every hop.
            resolve: Maps a hop's raw hash to a friendly contact name when known.
            session: The running TUI session (for repaints and the floating dialog).
            trace: Runs exactly one trace, handing back the result (see
                :data:`TraceOnce`); the screen loops it for multi-sample runs.
            compose_path: Opens the hop-by-hop path composer over this screen, seeded
                with the current spec; resolves to the new spec or ``None`` if cancelled.
            pick_width: Floats the path-hash width dialog; resolves to the standing
                spec re-rendered at the chosen width, or ``None`` when nothing changes.
            pick_samples: Floats the sample-count dialog; always resolves ``None``
                (the count lives with the owner, read back via ``sample_count``).
            width_bytes: Reads the currently chosen per-hop width, for the action row's
                label (the owner holds the width, since the flows emit specs at it).
            sample_count: Reads the currently chosen sample count, for the action rows
                and for how many traces one Trace commit runs.
            explore: Opens the scenario browser/probe flow over this screen (target
                mode only); resolves to an adopted spec or ``None`` to keep the current.
            pace_s: Cooldown slept between the traces of a multi-sample run, so a
                sampling session never reads as a burst to the repeaters.
            previous: The most recent stored trace, if any — its route seeds the route
                line so the screen opens knowing the path history last saw.
            auto_spec: Renders the spec an *auto* trace (no composed path) actually
                walks right now — the session resolves it from the device's learned
                route or the stored history at the current width (in path mode, the
                last successful stored walk verbatim). ``""`` means a path-less
                trace (unaddressable target, or a path walk with no history).
            auto_source: Short provenance of the auto route (e.g. ``device route``,
                ``last trace · Jul 09 14:32``) for the route line and summary, so
                the screen never claims a route the radio wasn't given.
            initial_spec: A forced path to open armed on, instead of idle on the auto
                route — how *Trace this path* from the trophy case reopens a record's
                exact route, ready to walk again. Empty (the default) opens on auto.
            open_trophy_case: Opens the trophy case over this screen — what the new-record
                dialog's left button does. ``None`` leaves the dialog with nothing but
                *Close* to do, which is what a screen run outside the menu gets.
        """
        super().__init__()
        self.title = f"Trace — {target}" if mode == "target" else "Trace path"
        self._target = target
        self._mode = mode
        self._flight_label = target if mode == "target" else "path"
        self._device_label = device_label
        self._device_hash = device_hash
        self._resolve = resolve
        self._session = session
        self._trace_once = trace
        self._compose_path = compose_path
        self._explore = explore
        self._pick_width = pick_width
        self._pick_samples = pick_samples
        self._width_bytes = width_bytes
        self._sample_count = sample_count
        self._pace_s = pace_s
        self._previous = previous
        self._auto_spec = auto_spec
        self._auto_source = auto_source
        self._path_spec = initial_spec.strip()
        self._open_trophy_case = open_trophy_case
        #: The trophy-case disciplines the current run has placed in, keyed by
        #: discipline title so an improving multi-sample run keeps only its latest
        #: ``"Title — score"`` label per board. Cleared when a run starts; drained
        #: into the floating new-record dialog once the run settles.
        self._run_placed: dict[str, str] = {}
        #: Traces aggregated on screen — the current route's run. Adopting a
        #: different path clears it (old numbers describe the old route).
        self._traces: list[TraceResult] = []
        #: Bumped on every mutation of ``_traces`` — the key the per-frame memos
        #: below expire on (a bare ``len`` would miss a clear-then-refill).
        self._traces_rev = 0
        # The aggregate stats and the windowed results block, each valid for one
        # traces revision (see render_body / _tail_lines): a repaint that changed
        # nothing re-reads them instead of re-aggregating every stored trace.
        self._stats_memo: tuple[int, TraceStats] | None = None
        self._tail_memo: tuple[tuple, list[str]] | None = None
        #: Every trace this screen ever ran, across path changes — the session
        #: count the owner reports, immune to the per-route clears above.
        self._total_traces = 0
        self._running = False
        self._dialog_open = False
        self._status = ""
        self._progress: tuple[int, int] | None = None  # (current, total) mid-run
        self._spinner = Spinner()
        #: Loop clock at this screen's last transmission — what :meth:`_pace_remaining`
        #: measures the cooldown from, so pacing survives an abort-and-retry.
        self._last_tx: float | None = None
        self._worker: asyncio.Task | None = None
        self._flight: TracingDialog | None = None
        # The action rows, in display order. The build-path group leads: Compose,
        # then Explore (target mode) or Reverse (path mode). Explore needs a
        # destination to rank candidates for, so path mode drops it; and only a path
        # walk is asymmetric enough to flip end-for-end, so only it offers Reverse.
        # The cursor opens on Trace either way.
        actions = ["compose", "explore"] if mode == "target" else ["compose", "reverse"]
        actions += ["width", "samples", "trace"]
        self._actions: tuple[str, ...] = tuple(actions)
        self._index = self._actions.index("trace")
        self._pin_cursor = False  # only pin the view while ↑/↓ are actually in use
        #: How far ←→ have slid the per-hop paths (cells), and how far they may: the
        #: widest path's tail, as the last paint measured it — 0 while every row's whole
        #: path is in view, which is also what keeps ``←→ scroll`` out of the footer.
        self._hshift = 0
        self._hop_pan = 0

    # --- state -----------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The footer keys, tracking whether traces are in flight."""
        if self._running:
            if self._progress is not None and self._progress[1] > 1:
                done, total = self._progress
                return f"tracing {done}/{total}… · PgUp/PgDn scroll · Esc back"
            return "tracing… · PgUp/PgDn scroll · Esc back"
        slide = " · ←→ scroll" if self._hop_pan else ""
        return f"↑↓ actions{slide} · Enter run · PgUp/PgDn scroll · Esc back"

    def start_trace(self) -> None:
        """Kick off a trace run in the background (no-op while one is already flying)."""
        if self._running:
            return
        self._running = True
        self._status = ""
        self._run_placed.clear()  # this run earns its own records
        self._spinner.reset()
        self._worker = asyncio.ensure_future(self._run_trace())
        self._session.invalidate()

    async def _run_trace(self) -> None:
        """Drive one Trace commit — the chosen number of traces — under the dialog.

        Every transmission is paced, not just the ones inside a multi-sample run:
        :attr:`_pace_s` is measured from the *last* transmission this screen made, so
        aborting a walk and immediately re-running it waits out the same gap a second
        sample would. Pacing only between samples left the one hole that matters — a
        user watching a failure and hitting Enter again straight away is exactly the
        burst repeaters penalize, and a penalized node's every later trace comes home
        empty. The dialog counts the run off as replies land; it is pushed for the
        duration and popped however the run ends — completion, failure, or abort — and
        its Abort wires straight to :meth:`cancel`, so the cancellation path is the
        same whether Esc lands on the dialog or the screen. An aborted run keeps every
        trace already recorded.
        """
        total = max(1, int(self._sample_count()))
        dialog = TracingDialog(
            f"Tracing — {self._flight_label}", spinner=self._spinner, on_abort=self.cancel
        )
        self._flight = dialog
        self._session.push(dialog)
        ticker = asyncio.ensure_future(self._animate())
        try:
            for done in range(total):
                self._progress = (done + 1, total)
                wait = self._pace_remaining()
                if wait > 0:
                    dialog.status = (
                        f"trace {done + 1}/{total} · pacing…" if total > 1 else "pacing…"
                    )
                    self._session.invalidate()
                    await asyncio.sleep(wait)
                if total > 1:
                    dialog.status = f"trace {done + 1}/{total} · transmitting…"
                    self._session.invalidate()
                self._last_tx = asyncio.get_running_loop().time()
                await self._trace_once(self._path_spec, self._on_trace)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - report inline, keep the screen alive
            self._status = f"trace failed: {exc}"
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a spinner hiccup must never break a trace
                pass
            self._session.pop(dialog)
            self._flight = None
            self._running = False
            self._progress = None
            self._session.invalidate()
            if self._run_placed and not (self.future is not None and self.future.done()):
                # The run placed on the boards: float the new-record dialog once the
                # tracing dialog is down. A separate task, because an aborted run
                # reaches here already cancelled and couldn't await a dialog itself;
                # a run whose screen is already resolving (Esc mid-run) skips it —
                # a popup must never chase the user out of the screen.
                asyncio.ensure_future(self._announce_records())

    def _pace_remaining(self) -> float:
        """Seconds still owed before the next transmission may go out (0 when clear)."""
        if self._last_tx is None or self._pace_s <= 0:
            return 0.0
        elapsed = asyncio.get_running_loop().time() - self._last_tx
        return max(0.0, self._pace_s - elapsed)

    async def _animate(self) -> None:
        """Advance the in-flight spinner and repaint on a steady cadence, until cancelled."""
        while True:
            await asyncio.sleep(spinner_interval())
            self._spinner.tick()
            self._session.invalidate()

    def _on_trace(self, result: TraceResult, placed: Sequence[str] = ()) -> None:
        """Append the landed trace, bank any records it set, echo it, and repaint.

        Args:
            result: The trace that just landed.
            placed: The trophy-case disciplines this walk placed in, as short
                ``"Title — score"`` labels (empty when it set nothing). They bank
                into the run's tally — announced in one dialog when the run settles
                (see :meth:`_announce_records`), a later improvement replacing the
                earlier label on the same board.
        """
        self._traces.append(result)
        self._traces_rev += 1
        self._total_traces += 1
        for label in placed:
            self._run_placed[label.split(" — ")[0]] = label
        if self._flight is not None:
            self._flight.last = result
        self._session.invalidate()

    async def _announce_records(self) -> None:
        """Float the new-record dialog for everything the settled run placed.

        One dialog per run, however many samples scored: each placed discipline reads
        as its own ``★ Title — score`` line. Platform-dialog shape — *Trophy case* on
        the left, *Close* on the right and default — so Enter simply dismisses and Esc
        closes. Choosing Trophy case opens it *over* this screen, like any other
        sub-view: Esc from the trophy case is one pop back onto the trace that earned
        the record, and ^W is what leaves the whole excursion at once. (It used to
        resolve the whole screen instead, unwinding the trace away first and landing the
        reader on the main menu — a hand-built escape from a stack that had no other way
        out.) The re-entrancy guard is held across the trophy case too, so a record
        landing while it is open cannot stack a second dialog behind it.
        """
        if self._dialog_open:
            return
        self._dialog_open = True
        try:
            labels = list(self._run_placed.values())
            self._run_placed.clear()
            prompt = Text()
            for i, label in enumerate(labels):
                if i:
                    prompt.append("\n")
                prompt.append("★ ", style="accent")
                prompt.append(label, style="accent")
            choice = await self._session.button_dialog(
                prompt,
                [("Trophy case", OPEN_TROPHY_CASE), ("Close", None)],
                title="New record" if len(labels) == 1 else f"{len(labels)} new records",
                default=1,
                footer_hint="←→ choose · Enter select · Esc close",
            )
            if choice == OPEN_TROPHY_CASE and self._open_trophy_case is not None:
                await self._open_trophy_case()
        finally:
            self._dialog_open = False
            self._session.invalidate()

    def cancel(self) -> None:
        """Cancel any in-flight trace run (already-recorded traces are kept)."""
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()

    # --- input -------------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Move the action cursor, commit the selected action, scroll, or dismiss.

        ↑/↓ belong to the action cursor, and past the last action edge scroll carries
        them on down the results; PgUp/PgDn/Home/End page the whole screen, the
        cursor left where it was until an arrow brings it back. ←→ slide the per-hop
        paths that run past the edge (see :meth:`_hop_lines`).
        """
        if action in ("left", "right"):
            # The paths are not the highlight: sliding them must not hand the page back
            # to the action cursor, or reading a hop edge-scrolled into view would yank
            # the page up to the actions under it. Clamped to the paths' tail at render.
            self._pin_cursor = False
            step = _HSCROLL_STEP if action == "right" else -_HSCROLL_STEP
            self._hshift = max(0, min(self._hop_pan, self._hshift + step))
        elif action == "enter":
            self._commit_action()
        elif action == "up":
            # Both ends clamp rather than wrap — the app-wide rule for a row cursor:
            # a highlight that leaps end to end takes the page with it.
            self._index = max(0, self._index - 1)
            self._pin_cursor = True
        elif action == "down":
            self._index = min(len(self._actions) - 1, self._index + 1)
            self._pin_cursor = True
        elif action == "pageup":
            self._pin_cursor = False
            self.scroll_pages(-1)
        elif action in ("pagedown", "space"):
            self._pin_cursor = False
            self.scroll_pages(1)
        elif action in ("home", "ctrl_home"):
            self._pin_cursor = False
            self.scroll_to_top()
        elif action in ("end", "ctrl_end"):
            self._pin_cursor = False
            self.scroll_to_bottom()
        elif action == "escape":
            self.cancel()
            self.resolve(None)

    def _commit_action(self) -> None:
        """Run the action row under the cursor (the flows guard against re-entry)."""
        key = self._actions[self._index]
        if key == "trace":
            # A path walk has nothing to transmit until a path exists (composed,
            # or the last stored walk) — with no target, an empty spec can't fall
            # back to device routing.
            if self._mode == "path" and not self._effective_spec()[0]:
                return
            edge = first_repeated_edge(self._spec_tokens())
            if edge is not None:
                self._confirm_ineligible(edge)
            else:
                self.start_trace()
        elif key == "width":
            self._open_flow(self._pick_width)
        elif key == "samples":
            self._open_flow(self._pick_samples)
        elif key == "compose":
            # Seed the composer with the route the screen is showing — the composed
            # spec if one stands, else the auto-resolved plan — not the bare
            # (empty on first open) stored spec, so opening Compose always resumes
            # from the visible route. Committing the composer parks the cursor on
            # Trace (see _open_flow): compose, then plain Enter walks it.
            self._open_flow(self._compose_path, seed=self._effective_spec()[0], focus_trace=True)
        elif key == "reverse":
            self._reverse_path()
        elif key == "explore" and self._explore is not None:
            self._open_flow(self._explore)

    def _open_flow(
        self, flow: PathFlow, seed: str | None = None, focus_trace: bool = False
    ) -> None:
        """Float a path-picking flow over the screen (one at a time, not mid-trace).

        The composer, the scenario explorer, and the width picker all resolve the
        same way: a new spec to adopt (``""`` returns routing to the device), or
        ``None`` to leave the current path untouched (which is all the sample-count
        flow ever resolves).

        Args:
            flow: The dialog flow to run with the current spec.
            seed: The spec handed to the flow, when it should differ from the stored
                one — the composer seeds from the *visible* route (the auto plan when
                no spec stands), so it never opens blank over a shown route. The
                adopt test still compares against the stored spec, so re-confirming an
                auto plan verbatim simply pins it, exactly like adopting it by hand.
            focus_trace: Whether committing the flow (any non-``None`` resolution,
                even the unchanged spec) should park the action cursor on Trace —
                the composer's hand-back, so plain Enter walks what was just built.
                Backing out with Esc leaves the cursor where it was.
        """
        if self._dialog_open or self._running:
            return
        self._dialog_open = True

        async def run() -> None:
            try:
                spec = await flow(self._path_spec if seed is None else seed)
                if spec is not None and focus_trace:
                    self._index = self._actions.index("trace")
                if spec is not None and spec.strip() != self._path_spec:
                    # A different spec is a different measurement: the aggregates,
                    # per-hop medians, and log all belong to the old route, so the
                    # session restarts as clean as a fresh screen.
                    self._path_spec = spec.strip()
                    self._traces.clear()
                    self._traces_rev += 1
                    self._status = ""
            finally:
                self._dialog_open = False
                self._session.invalidate()

        self._session.run_detached(run())

    def _reverse_path(self) -> None:
        """Flip the walked path end-for-end and re-arm on the reversed direction.

        A path walk is the one asymmetric trace: ``us → a → b → c → us`` and
        ``us → c → b → a → us`` cross the same links the opposite way, and radio
        links rarely read the same in both — so reversing the hop order lets one
        route be measured out and back. It adopts the reversed spec exactly like any
        other path change: the aggregates and log described the forward run, so they
        clear, and Trace stays idle until the user commits it (nothing auto-fires).
        Reverse again to walk it the original way; the stored history keeps both
        directions for side-by-side comparison.

        A no-op when there is nothing to flip: no path, or a single hop (its own
        mirror), or already mid-trace/mid-dialog.
        """
        if self._dialog_open or self._running:
            return
        spec, _ = self._effective_spec()
        tokens = [h.strip() for h in spec.split(",") if h.strip()]
        if len(tokens) < 2:
            return
        reversed_spec = ",".join(reversed(tokens))
        if reversed_spec == self._path_spec:
            return
        self._path_spec = reversed_spec
        self._traces.clear()
        self._traces_rev += 1
        self._status = ""
        self._session.invalidate()

    # --- rendering -----------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render the route, aggregates, action list, per-hop medians, and the log.

        The action rows are rendered line by line (not through one Rich group) so the
        highlighted row's body line is known exactly — that is what :meth:`cursor_line`
        pins while the user is navigating.
        """
        if self._stats_memo is not None and self._stats_memo[0] == self._traces_rev:
            stats = self._stats_memo[1]
        else:
            stats = TraceStats.from_traces(self._target, self._traces)
            self._stats_memo = (self._traces_rev, stats)
        current = next((t for t in reversed(self._traces) if t.success), None)
        # Both path lanes break at hop boundaries under their own value column (the
        # app-wide labelled-row rule): a long walk never folds back to column zero,
        # and never splits a name from its hash or a wire spec mid-hash.
        lines = _lane_lines(_ROUTE_LANE, self._route_value(current, width), width)
        lines.extend(render_lines(Group(Text(), self._summary(stats)), width))
        lines.extend(_lane_lines(_PATH_LANE, self._path_value(current, width), width))
        lines.append("")
        self._cursor: int | None = None
        for i, key in enumerate(self._actions):
            selected = i == self._index
            text = self._action_text(key, selected)
            text.no_wrap = True
            text.truncate(width, overflow="ellipsis")
            if selected:
                self._cursor = len(lines)
            lines.append(render_to_ansi(text, width))
            # Set the next group apart: after the build-path group (Explore in target
            # mode, Reverse in path mode — whichever closes it) and after the
            # trace-settings group.
            if key in ("explore", "reverse", "samples"):
                lines.append("")
        # The results are page, not a window of their own: nothing highlights them, so a
        # private window would hide its rows from the arrows and leave the frame a page
        # exactly as tall as the screen — edge scroll would have nothing to scroll (see
        # ListWindow). The frame scrolls them, ↓ off the last action carries on into them.
        lines.extend(self._tail_lines(stats, current, width))
        self._scroll_total = max(1, len(lines))
        return lines

    def _tail_lines(self, stats: TraceStats, current: TraceResult | None, width: int) -> list[str]:
        """The results block under the actions: per-hop medians, then the trace log.

        Memoized per traces revision while idle — the block re-renders every stored
        trace, and between completions nothing in it moves. A *running* trace skips
        the memo outright: its log row carries the live spinner glyph.
        """
        key = (self._traces_rev, self._status, width, self._hshift)
        if not self._running and self._tail_memo is not None and self._tail_memo[0] == key:
            return self._tail_memo[1]
        lines: list[str] = []
        if stats.hop_snrs:
            hash_bytes = current.path_hash_bytes if current is not None else None
            lines += render_lines(Group(Text(), Text("Per-hop medians", style="accent")), width)
            lines += self._hop_lines(stats, hash_bytes, width)
        else:
            self._hop_pan = self._hshift = 0
        if self._running or self._status or self._traces:
            lines += render_lines(
                Group(Text(), Text("Traces", style="accent"), self._trace_log()), width
            )
        if not self._running:
            # Keyed on the shift the rows were actually drawn at, which _hop_lines may
            # have clamped in — so the next paint finds it.
            self._tail_memo = ((self._traces_rev, self._status, width, self._hshift), lines)
        return lines

    def cursor_line(self) -> int | None:
        """The highlighted action row while ↑/↓ are in use; free scrolling otherwise.

        Returning ``None`` between navigations matters: the frame force-keeps a
        cursor line visible, which would otherwise stop PgDn ever scrolling the
        action list off screen to read a long trace log.
        """
        return getattr(self, "_cursor", None) if self._pin_cursor else None

    def _action_text(self, key: str, selected: bool) -> Text:
        """One action row: pointer, glyph, and label (current value inlined)."""
        text = Text("❯ " if selected else "  ", style="cursor" if selected else "")
        if key == "compose":
            _icon(text, "✎", "brand")
            text.append("Compose path")
        elif key == "reverse":
            _icon(text, "⇄", "accent")
            if self._effective_spec()[0]:
                text.append("Reverse path — trace it the other way")
            else:
                text.append("Reverse path — compose a path first", style="muted")
        elif key == "explore":
            _icon(text, "⚡", "warn")
            text.append("Explore paths")
        elif key == "width":
            w = self._width_bytes()
            _icon(text, "⚙", "accent")
            text.append(f"Path width — {w} byte{'s' if w != 1 else ''} per hop")
        elif key == "samples":
            n = self._sample_count()
            _icon(text, "#", "accent")
            text.append(f"Sample count — {n} trace{'s' if n != 1 else ''}")
        else:  # trace — the row the cursor opens on
            _icon(text, "▶", "ok")
            if self._mode == "path" and not self._effective_spec()[0]:
                text.append("Trace — compose a path first", style="muted")
            else:
                n = self._sample_count()
                if n > 1:
                    text.append(f"Trace — {n} paced transmissions")
                else:
                    text.append("Trace — one transmission")
        if selected:
            text.style = "cursor"
        return text

    def _route_value(self, current: TraceResult | None, width: int) -> list[Text]:
        """The route lane's value lines: the path, hop-wrapped, then its provenance.

        The route itself is live once a reply has landed, else the plan the next Trace
        walks, else the last stored walk, else a muted note. Whichever it is, it breaks
        at hop boundaries under the lane's own column — a name never parts from its
        hash, and a boomerang folds at its turn — while the provenance (where an auto
        plan came from, when a previous walk ran) takes the line below rather than
        crowding the route's right edge.

        Both ends go bare (``bare_self``): every walk on this screen leaves us and comes
        back to us, so naming ourselves twice would only push the hops that *are* news
        out of the lane. Named hops go bare of their hash too: this lane answers *which
        nodes*, and the hex it would repeat after every name is already spelled out
        verbatim, at the width it goes on the air, in the ``path`` lane below.

        Args:
            current: The newest successful trace of this session, if any.
            width: The full body width; the lines fit it, indent included.

        Returns:
            The value's lines — the first bare (the label rides it), continuations
            already carrying the lane's indent.
        """
        indent = len(_ROUTE_LANE)
        if current is not None:
            route = route_path(
                current,
                self._device_label,
                self._resolve,
                self._device_hash,
                bare_self=True,
                show_hash=False,
            )
            return route.wrapped(width, indent=indent)
        planned = self._planned_route()
        if planned is not None:
            lines = planned.wrapped(width, indent=indent)
            if self._effective_spec()[1] and self._auto_source:
                lines.append(_note(f"(auto · {self._auto_source})", indent))
            return lines
        if self._previous is not None:
            route = route_path(
                self._previous,
                self._device_label,
                self._resolve,
                self._device_hash,
                bare_self=True,
                show_hash=False,
            )
            lines = route.wrapped(width, indent=indent)
            stamp = self._previous.timestamp.astimezone().strftime("%b %d %H:%M")
            lines.append(_note(f"(previous · {stamp})", indent))
            return lines
        if self._mode == "path":
            return [Text("none — compose a path to walk", style="muted")]
        return [Text("unknown — press Enter to trace", style="muted")]

    def _effective_spec(self) -> tuple[str, bool]:
        """The wire spec the next Trace walks, and whether auto resolution supplied it.

        A composed/adopted spec wins verbatim; with none, the session's auto
        resolver says what it would force right now (the device's learned route or
        the stored history in target mode, the last successful stored walk in path
        mode) — the same call :func:`_open_session`'s ``trace_once`` makes, so the
        route on screen is the route on the air.
        """
        if self._path_spec:
            return self._path_spec, False
        spec = self._auto_spec()
        return spec, bool(spec)

    def _spec_tokens(self) -> list[str]:
        """The effective spec's hop hashes in walk order (empty = path-less trace)."""
        spec, _ = self._effective_spec()
        return [t.strip() for t in spec.split(",") if t.strip()]

    def _confirm_ineligible(self, edge: tuple[str, str]) -> None:
        """Float the amber not-a-trail confirm before walking a record-ineligible path.

        The walk about to fly crosses ``edge`` twice in the same direction, so it
        isn't a trail and the trophy case will ignore whatever it scores (the
        no-cheat rule — see :func:`~meshterm.services.records.first_repeated_edge`).
        Walking it is still perfectly fine, so the dialog only makes the
        disqualification explicit: Cancel backs out, Trace — on the right and the
        default, like every committing verb — transmits anyway.
        """
        if self._dialog_open or self._running:
            return
        self._dialog_open = True

        async def run() -> None:
            try:
                prompt = Text("This walk crosses ", style="warn")
                prompt.append_text(
                    # The hashes read at the width the screen addresses hops by, like
                    # every other node this session shows.
                    link_text(
                        edge[0],
                        edge[1],
                        self._device_label,
                        self._resolve,
                        self._width_bytes(),
                        self._device_hash,
                    )
                )
                prompt.append(" twice in the same direction, ", style="warn")
                prompt.append(
                    "so it isn't a trail — records will ignore it. Trace anyway?",
                    style="warn",
                )
                choice = await self._session.button_dialog(
                    prompt,
                    [("Cancel", None), ("Trace", "trace")],
                    title="Not eligible for records",
                    default=1,
                    footer_hint="←→ choose · Enter select · Esc cancel",
                    border_style="warn",
                )
            finally:
                self._dialog_open = False
                self._session.invalidate()
            if choice == "trace":
                self.start_trace()

        asyncio.ensure_future(run())

    def _planned_route(self) -> PathLine | None:
        """The route the next Trace walks as a preview, or ``None`` without one.

        Renders the literal wire spec through THE path widget — the whole walk, since
        the trace protocol has no separate return-path field. In target mode the spec
        is the symmetric boomerang (outbound hops, the target, those hops mirrored):
        its second half is dimmed via the widget's ``dim_from``, reading as "this part
        isn't yours to compose" — and, when the line wraps, giving the fold its natural
        seam (the turn). A path walk's spec is the whole route (hand-composed, or the
        last stored walk), so every hop renders in full colour and only the automatic
        landing back on us stays faint. Our own endpoints go bare, as in
        :meth:`_route_value` — the arrow alone — and so does every hop's hash: the lane
        names nodes, and the spec below it is the hex, verbatim.
        """
        spec, _ = self._effective_spec()
        tokens = [h.strip() for h in spec.split(",") if h.strip()]
        if not tokens:
            return None
        if self._mode == "target" and len(tokens) % 2 and tokens == tokens[::-1]:
            mid = len(tokens) // 2  # outbound hops + target = first half, inclusive
            outbound, return_leg = tokens[: mid + 1], tokens[mid + 1 :]
        else:
            outbound, return_leg = tokens, []
        return path_line(
            [None, *outbound, *return_leg, None],
            self._resolve,
            prefix_bytes=8,  # spec tokens are the addressed slices: light them whole
            self_name=self._device_label,
            show_hash=False,
            dim_from=1 + len(outbound),
            bare_self=True,
        )

    def _displayed_hop_count(self, current: TraceResult | None) -> int | None:
        """How many nodes the displayed route passes through, endpoints excluded.

        Follows the route line's precedence — the live route, else the planned spec,
        else the stored previous trace — counting exactly the items drawn between the
        two ``us`` endpoints (a walked trace's final hash-less hop *is* us, so it
        doesn't count).
        """
        if current is not None:
            return sum(1 for h in current.hops if h.node)
        spec, _ = self._effective_spec()
        tokens = [h for h in spec.split(",") if h.strip()]
        if tokens:
            return len(tokens)
        if self._previous is not None and self._previous.hops:
            return sum(1 for h in self._previous.hops if h.node)
        return None

    def _summary(self, stats: TraceStats) -> Text:
        """The session's aggregate lanes, label-aligned (the path lane hangs apart)."""
        snr = stats.median_min_snr
        snr_text = Text(f"{snr:+.1f} dB", style=snr_style(snr)) if snr is not None else Text("—")
        rtt = f"{stats.median_rtt_ms:.0f} ms" if stats.median_rtt_ms is not None else "—"
        rate = f"{stats.success_rate:.0%} ({stats.successes}/{stats.samples})"
        return Text.assemble(
            ("success rate    ", "muted"),
            (rate if stats.samples else "—", ""),
            ("\n", ""),
            ("median min SNR  ", "muted"),
            snr_text,
            ("\n", ""),
            ("median RTT      ", "muted"),
            (rtt, ""),
        )

    def _path_value(self, current: TraceResult | None, width: int) -> list[Text]:
        """The ``path`` lane's value lines: the wire spec (or its note) + hop count.

        A pinned spec is the verbatim string the radio is handed, drawn through THE
        path widget (:func:`_spec_line`) so it breaks after a comma instead of
        splitting a hash in half; the auto/none states stay prose. The hop count
        trails the last line, dropping to a line of its own only when it doesn't fit.

        Args:
            current: The newest successful trace of this session, if any — its walked
                route is what the count describes once one has landed.
            width: The full body width; the lines fit it, indent included.

        Returns:
            The value's lines, indented like :meth:`_route_value`'s.
        """
        indent = len(_PATH_LANE)
        if self._path_spec:
            lines = _spec_line(self._path_spec).wrapped(width, indent=indent)
        elif self._effective_spec()[1] and self._auto_source:
            lines = [Text(f"auto · {self._auto_source}", style="muted")]
        elif self._mode == "path":
            lines = [Text("none — compose a path first", style="muted")]
        else:
            lines = [Text("auto — path-less (unknown target)", style="muted")]
        hops = self._displayed_hop_count(current)
        if hops is not None:
            count = f"{hops} hop{'s' if hops != 1 else ''}"
            used = lines[-1].cell_len + (indent if len(lines) == 1 else 0)
            if used + len(count) + 4 <= width:
                lines[-1].append(f"  · {count}", style="muted")
            else:
                lines.append(_note(count, indent, style="muted"))
        return lines

    def _hop_lines(self, stats: TraceStats, hash_bytes: int | None, width: int) -> list[str]:
        """The per-hop median SNRs with quality bars, in path order.

        Each hop reads ``n  origin → destination  +4.5 dB  meter``, the link drawn through
        THE path widget as the route lane above draws it: names, no hash after them (an
        unnamed node shows its hash, the only identity it has), our own end the bare
        ``★``, and only the route's own two ends square (see :meth:`_hop_path`). Where
        every row fits whole the hops are a four-column table, one line each.
        Where any doesn't, every row takes two — all alike, so the column of readings
        never jumps between lines as the eye runs down it: the hop number and its path on
        the first, the reading and its meter flush right on the second. A path still
        wider than its lane is cut on its own crack and slides under ←→ (:attr:`_hshift`,
        shared by every row, each stopping at its own tail), the hop number pinned.

        Measures how far ←→ may slide (:attr:`_hop_pan`) and clamps the shift to it, so
        the rows are drawn at the shift the footer and the next press agree on.
        """
        numbers = [Text(str(agg.index), style="muted") for agg in stats.hop_snrs]
        last = len(stats.hop_snrs) - 1
        paths = [
            self._hop_path(agg, hash_bytes, from_origin=i == 0, to_destination=i == last)
            for i, agg in enumerate(stats.hop_snrs)
        ]
        readings = [
            Text(f"{agg.median_snr:+.1f} dB", style=snr_style(agg.median_snr))
            for agg in stats.hop_snrs
        ]
        number_w = max(n.cell_len for n in numbers)
        path_w = max(p.cell_len for p in paths)
        reading_w = max(r.cell_len for r in readings)
        lines: list[str] = []
        if number_w + 1 + path_w + 1 + reading_w + 1 + _BAR_WIDTH <= width:
            self._hop_pan = self._hshift = 0
            for number, path, reading, agg in zip(
                numbers, paths, readings, stats.hop_snrs, strict=True
            ):
                row = Text(" " * (number_w - number.cell_len))
                row.append_text(number)
                row.append(" ")
                row.append_text(path)
                row.append(" " * (path_w - path.cell_len + 1 + reading_w - reading.cell_len))
                row.append_text(reading)
                row.append(" ")
                row.append_text(snr_bar(agg.median_snr))
                lines.append(render_to_ansi(row, width, no_wrap=True))
            return lines
        lane = max(1, width - number_w - 1)
        # A slid line gives a cell up to its left mark, so a path's tail is in view at
        # ``cells - (lane - 1)`` — the exact stop, as a panning table takes it.
        tails = [p.cell_len - (lane - 1) if p.cell_len > lane else 0 for p in paths]
        self._hop_pan = max(tails)
        self._hshift = min(self._hshift, self._hop_pan)
        for number, path, reading, tail, agg in zip(
            numbers, paths, readings, tails, stats.hop_snrs, strict=True
        ):
            first = Text(" " * (number_w - number.cell_len))
            first.append_text(number)
            first.append(" ")
            first.append_text(_slid(path, min(self._hshift, tail), lane))
            lines.append(render_to_ansi(first, width, no_wrap=True))
            second = Text(" " * max(0, width - reading.cell_len - 1 - _BAR_WIDTH))
            second.append_text(reading)
            second.append(" ")
            second.append_text(snr_bar(agg.median_snr))
            lines.append(render_to_ansi(second, width, no_wrap=True))
        return lines

    def _hop_path(
        self,
        agg: HopAggregate,
        hash_bytes: int | None,
        *,
        from_origin: bool,
        to_destination: bool,
    ) -> Text:
        """One hop's link, ``origin → destination``, as the route lane draws its nodes.

        A hop is one link of the walk, so only the route's own two ends are drawn square —
        the first hop's start, the last hop's finish. Every other end is a node the route
        runs on through, and wears the chevron (the arrow form, a bare ``→``) that says so.
        """
        ends = [
            None if not node or node == self._device_label else node
            for node in (agg.origin, agg.destination)
        ]
        return path_line(
            ends,
            self._resolve,
            prefix_bytes=hash_bytes or 8,
            self_name=self._device_label,
            hash_bytes=hash_bytes,
            bare_self=True,
            from_origin=from_origin,
            to_destination=to_destination,
        ).text()

    def _trace_log(self) -> RenderableType:
        """The individual traces, newest first, with the in-flight spinner on top."""
        rows: list[RenderableType] = []
        if self._running:
            spin = self._spinner.text()
            if self._progress is not None and self._progress[1] > 1:
                spin.append(f"  tracing {self._progress[0]}/{self._progress[1]}…", style="muted")
            else:
                spin.append("  tracing…", style="muted")
            rows.append(spin)
        elif self._status:
            rows.append(Text(self._status, style="err"))
        numbering = range(len(self._traces), 0, -1)
        for number, trace in zip(numbering, reversed(self._traces), strict=True):
            rows.append(self._trace_row(number, trace))
        return Group(*rows)

    def _trace_row(self, number: int, trace: TraceResult) -> Text:
        """One log line: number, local time, outcome, hop count, bottleneck SNR, RTT."""
        stamp = trace.timestamp.astimezone().strftime("%H:%M:%S")
        row = Text.assemble((f"#{number:<3}", "muted"), (f"{stamp}  ", "muted"))
        if not trace.success:
            row.append("✗ no reply", style="err")
            return row
        row.append("✓ ", style="ok")
        row.append(f"{trace.hop_count} hop{'s' if trace.hop_count != 1 else ''}", style="")
        if trace.min_snr is not None:
            row.append("  min ", style="muted")
            row.append(f"{trace.min_snr:+.1f} dB", style=snr_style(trace.min_snr))
        if trace.round_trip_ms is not None:
            row.append(f"  {trace.round_trip_ms:.0f} ms", style="muted")
        return row


def collapse_trace_width(mode: int) -> int:
    """Collapse a routing hash mode to the widest trace-representable hop width.

    Routing widths are ``mode + 1`` bytes, but a trace's flags can only encode 1, 2, 4,
    or 8 (see :func:`~meshterm.services.trace_runner.path_hash_flags`); the firmware
    matches by prefix, so collapsing down addresses the same nodes.

    Args:
        mode: The path-hash mode (``size - 1``); negative means unknown.

    Returns:
        The per-hop width in bytes (1, 2, 4, or 8).
    """
    size = max(mode + 1, 1)
    return max(s for s in (1, 2, 4, 8) if s <= size)


def _previous_outbound(previous: TraceResult | None, target_hash: str) -> tuple[str, ...] | None:
    """Extract the outbound repeaters from the last successful walk to a target.

    A target-mode trace walks the symmetric boomerang, so its stored hop hashes
    (the final hash-less hop is us) read ``[out…, target, out reversed…]`` — an
    odd-length palindrome whose middle entry is the target. When the stored walk
    has that shape, its first half is a route the mesh has already proven, ready
    to force again.

    Args:
        previous: The most recent successful stored trace, if any.
        target_hash: The target's full hex hash, to confirm the walk really
            turned at this target (hop hashes are prefixes of it).

    Returns:
        The outbound repeater hashes in order from us outward (empty = the
        target answered directly), or ``None`` when there is no stored walk or
        it isn't a recognizable boomerang.
    """
    if previous is None or not previous.success:
        return None
    tokens = [h.node.lower() for h in previous.hops if h.node]
    if not tokens or len(tokens) % 2 == 0 or tokens != tokens[::-1]:
        return None
    mid = len(tokens) // 2
    if not target_hash.lower().startswith(tokens[mid]):
        return None
    return tuple(tokens[:mid])


def _previous_walk(previous: TraceResult | None) -> tuple[str, ...] | None:
    """Extract the whole walked route from the last successful stored path walk.

    A path walk has no destination to route to, but its stored spec is a route the
    mesh has already carried end to end — so the previous walk the screen opens
    showing is also a path Trace can immediately walk again. The hop hashes come
    back verbatim (the final hash-less hop is us and drops out): they were proven
    at the width they were transmitted, so no re-rendering is applied.

    Args:
        previous: The most recent stored path walk, if any.

    Returns:
        The walked hop hashes in transmit order, or ``None`` when there is no
        stored walk, it failed, or it recorded no addressable hops.
    """
    if previous is None or not previous.success:
        return None
    tokens = tuple(h.node.lower() for h in previous.hops if h.node)
    return tokens or None


def _best_observed(topo: Any, target_hash: str) -> tuple[tuple[str, ...], str] | None:
    """The strongest evidence-backed outbound route to ``target_hash``, if the data has one.

    The observed-topology counterpart to the firmware's learned route: when the device
    knows no route to a contact — the common case, since repeater contacts flood — the
    recorder's own evidence (traces, overheard relay chains, fetched neighbour tables)
    may still spell out a route that has demonstrably carried traffic. Delegates the
    ranking to :meth:`~meshterm.services.topology.MeshTopology.suggested` and returns the
    winner's canonical outbound hops with a short provenance line
    (``best observed · 3× · −7 dB``) for the route line and summary, or ``None`` when
    nothing beats a direct shot.

    Args:
        topo: The freshly built :class:`~meshterm.services.topology.MeshTopology`.
        target_hash: The target's hex hash (any width ≥ 1 byte).

    Returns:
        ``(outbound_hops, provenance)`` for the suggested route, or ``None``.
    """
    scenario = topo.suggested(topo.canonical(target_hash) or target_hash[:12])
    if scenario is None:
        return None
    source = f"best observed · {scenario.samples}×"
    if scenario.weakest_snr is not None:
        source += f" · {scenario.weakest_snr:+.0f} dB"
    return scenario.hops, source


def _scenario_path(
    scenario: Any,
    topo: Any,
    target_id: str,
    *,
    device_label: str,
    width_bytes: int,
    width: int | None = None,
) -> Text:
    """A scenario's pathline: us, the candidate hops, and the target — one line.

    Full boomerang endpoints included (not just the intermediate hops a
    :class:`~meshterm.services.topology.PathScenario` stores), so the row reads as
    the whole route rather than a fragment the reader has to mentally close.
    Renders through the shared path widget, so every hop wears its own key hue (an
    unnamed hop its prefix-lit hash) instead of the old single-colour smear — and, as
    in the route lane above it, our own end goes bare: every candidate starts from us,
    so the word would be the same on every row and the cells are the candidates'.

    A ``width`` budget *cuts* the line rather than middle-eliding it
    (:func:`~meshterm.ui.pathline.cut_to`): the ``⋯`` rescue exists to save a route's two
    endpoints, and here both endpoints are the same two on every row by construction — our
    own ``★`` and the one target the whole screen is about — so it would spend cells on
    what the reader already knows and take them from the candidates' *front*, the only part
    that differs. Cutting also makes every row read the way the highlighted one does at
    shift zero, so walking the cursor down the list no longer rewrites the row under it
    (JP, 2026-08-10). ``None`` returns the full line, which is what the highlighted row
    slides under ``←→``.
    """
    route = path_line(
        [None, *scenario.hops, target_id],
        topo.display_name,
        prefix_bytes=width_bytes,
        self_name=device_label,
        bare_self=True,
    )
    return route.text() if width is None else cut_to(route.text(), width)


def _scenario_detail(scenario: Any) -> Text:
    """The line hanging under a scenario's pathline: its length, provenance and evidence.

    The hop count leads (:func:`~meshterm.ui.pathline.hops_atom`) — the figure the line
    above encodes but never states, and the first thing one candidate route is weighed
    against another on. It also absorbs the *direct* shot's provenance tag, which was
    that same word for the same reason (no repeaters at all); the device route keeps its
    own, since a firmware-learned route is a claim about where the hops came from rather
    than about how many there are. Observed candidates *are* their hop sequence and carry
    no tag. Then the bottleneck SNR and sample count a trace would expect to measure, or
    ``unobserved`` when the evidence graph has nothing to say about it yet.
    """
    atoms: list[Text] = [hops_atom(len(scenario.hops))]
    if scenario.source == "device":
        atoms.append(Text(scenario.label, style="accent"))
    if scenario.weakest_snr is not None:
        snr = Text("weakest ", style="muted")
        snr.append(f"{scenario.weakest_snr:+.1f} dB", style=snr_style(scenario.weakest_snr))
        atoms.append(snr)
    if scenario.samples:
        atoms.append(Text(f"{scenario.samples}×", style="muted"))
    elif scenario.score == 0:
        atoms.append(Text("unobserved", style="faint"))
    detail = Text()
    for i, atom in enumerate(atoms):
        if i:
            detail.append("  ·  ", style="muted")
        detail.append_text(atom)
    return detail


async def open_trace(ctx: AppContext, target: str, *, initial_spec: str = "") -> int:
    """Open the live *Trace target* screen for ``target`` and run it until dismissed.

    The symmetric feature: can I reach this node? Routes turn at the target and come
    home over the mirrored hops; *Explore paths* ranks candidate outbound legs from
    the observed evidence.

    Args:
        ctx: The shared application context (must be running the interactive TUI surface).
        target: The trace destination (contact name or key prefix).
        initial_spec: A forced path to open armed on (the full symmetric boomerang,
            comma-separated hex hops), instead of idle on the auto-resolved route — how
            the Node detail screen hands over its suggested best path so the trace opens
            ready to walk it. Empty (the default) opens on the auto route (device-learned,
            best-observed, last trace, or direct — see :func:`_open_session`).

    Returns:
        The number of traces run while the screen was open.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    return await _open_session(ctx, target, initial_spec=initial_spec)


async def open_trace_path(ctx: AppContext, spec: str = "") -> int:
    """Open the live *Trace path* screen and run it until dismissed.

    The hand-routed feature: how far can a route I build carry? There is no target —
    the whole walk is composed hop by hop and only has to end within our earshot — so
    there is no target picker, no device routing, and no scenario explorer. Traces
    record under :data:`~meshterm.core.models.PATH_TRACE_TARGET`.

    Args:
        ctx: The shared application context (must be running the interactive TUI surface).
        spec: A forced path to open armed on (comma-separated hex hops), instead of idle
            on the last stored walk — how the trophy case's *Trace this path* reopens a
            record's route. Empty (the default) opens on history.

    Returns:
        The number of traces run while the screen was open.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    return await _open_session(ctx, None, initial_spec=spec)


async def _open_session(ctx: AppContext, target: str | None, *, initial_spec: str = "") -> int:
    """Wire and run one live trace session (both features share this plumbing).

    Wires the screen to the radio, the database, and the observed-topology services:
    each trace opens its own ``runs`` row and is recorded under it (same shape a
    scripted ``meshterm trace`` writes); the composer and scenario flows build a fresh
    :class:`~meshterm.services.topology.MeshTopology` from stored evidence on each open,
    so suggestions always reflect the latest received traffic. Nothing transmits until
    the user asks — the screen opens idle, its route line seeded from the most recent
    stored trace.

    Args:
        ctx: The shared application context (must be running the interactive TUI surface).
        target: The trace destination (contact name or key prefix), or ``None`` for a
            target-less path walk.
        initial_spec: A forced path (comma-separated hex hops) to open the path screen
            armed on, instead of idle on history — the trophy case's *Trace this path*
            hand-off. Ignored in target mode (only path walks arm on a bare spec).

    Returns:
        The number of traces run while the screen was open.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from ..core.connection import DeviceAuthenticationError, DeviceCommandError
    from ..core.models import (
        LOCAL_DEVICE_LABEL,
        Contact,
        LoginResult,
        NeighbourInfo,
    )
    from ..services.path_probe import ProbeCandidate, ProbeOutcome, probe_paths
    from ..services.topology import MeshTopology, build_topology, collapse_width, is_path_hash
    from .path_composer import FetchNeighbours, PathComposerScreen
    from .surface import TuiUi
    from .tui import CANCEL, Choice, SelectScreen, Separator

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the live trace screen is only available in the menu")
    session = ctx.ui.session

    mode = "target" if target is not None else "path"
    record_target = target if target is not None else PATH_TRACE_TARGET

    device = await ctx.device()
    # Contacts and self-info come from the session cache (see DeviceState): the contacts
    # table is a slow round-trip on a busy node, and this screen is reached often. The live
    # ``device`` is still held for the traces this screen actually runs.
    contacts = await ctx.devstate.contacts()
    resolve = trace_runner.make_node_resolver(contacts)
    self_info = await ctx.devstate.self_info()

    device_label = str(self_info.get("name") or LOCAL_DEVICE_LABEL)
    device_hash = str(self_info.get("public_key") or "") or None

    # The per-hop width composed/scenario specs are emitted at: the region's routing
    # width, collapsed to what a trace can encode. Unknown (old firmware) → 1 byte, the
    # protocol default and what all stored evidence uses anyway. The user can override
    # it for the session through the screen's *Path width* action (see pick_width).
    try:
        width_bytes = collapse_trace_width(int(await ctx.devstate.path_hash_mode()))
    except Exception:  # noqa: BLE001 - optional read; the 1-byte default always works
        width_bytes = 1
    device_width = width_bytes  # remembered so the width dialog can mark the default

    # How many traces one Trace commit runs. Session state like the width; the
    # screen's *Sample count* action changes it (see pick_samples).
    sample_count = 1

    # The target as an addressable hash: a known contact's full key, or the typed hex
    # prefix itself. A non-hex unknown target can still be traced device-routed, but
    # composing/exploring needs a destination hash to pin the path on. A path walk has
    # no target at all — every target_* stays None and the composer runs hand-routed.
    target_contact: Contact | None = None
    target_hash: str | None = None
    target_label: str | None = None
    if target is not None:
        needle = target.casefold()
        target_contact = next(
            (
                c
                for c in contacts
                if c.name.casefold() == needle
                or ((c.public_key or "").lower().startswith(target.lower()))
            ),
            None,
        )
        raw_hash = (
            ((target_contact.public_key or target_contact.key_prefix) if target_contact else target)
            .lower()
            .removeprefix("0x")
        )
        target_hash = raw_hash if is_path_hash(raw_hash) else None
        target_label = target_contact.name if target_contact else target

    def fresh_topology() -> MeshTopology:
        """Build the evidence graph from everything currently stored + on the device."""
        return build_topology(
            self_id=device_hash or "local",
            contacts=contacts,
            trace_paths=ctx.repo.trace_paths(),
            packet_paths=ctx.repo.packet_paths(),
            neighbour_links=ctx.repo.neighbour_links(),
        )

    previous = ctx.repo.latest_trace(record_target)

    # What an *auto* trace (no composed path) forces on the air, and where that
    # route came from. A trace only replies when its destination is the path's
    # final outbound hop and nothing reflects it home, so auto must always spell
    # out a full boomerang — but the device rarely has one to offer: firmware
    # only learns a contact's ``out_path`` from two-way addressed traffic
    # (verified on hardware: every repeater contact reported ``out_path_len``
    # -1, flood), so a device-routed trace to anything further than a direct
    # neighbour would go out with no repeaters and die. Precedence: the device's
    # learned route when it genuinely has one, else the outbound leg of the last
    # successful stored walk (the route the screen shows), else the bare
    # destination — a direct attempt, honest about being one.
    device_route: tuple[str, ...] | None = None
    if target_contact is not None and target_contact.route_hops is not None:
        # Canonicalizing hop hashes needs only the contact index, so an *empty*
        # graph does it — not the full evidence build (stored traces, packet paths,
        # neighbour tables) fresh_topology() runs, which this branch would then
        # discard unread.
        ident = MeshTopology(device_hash or "local", contacts)
        device_route = tuple(ident.canonical(h) or h for h in target_contact.route_hops)
    auto_hops: tuple[str, ...] | None = None
    auto_source = ""
    if target_hash is not None:
        if device_route is not None:
            auto_hops, auto_source = device_route, "device route"
        elif (observed := _best_observed(fresh_topology(), target_hash)) is not None:
            # No firmware route, but the recorder's own evidence spells one out — the
            # strongest observed path (sample counts, SNR behind the ranking). This is
            # what makes Trace target *suggest* a route from the data rather than
            # defaulting to a doomed path-less flood past the first neighbour.
            auto_hops, auto_source = observed
        else:
            auto_hops = _previous_outbound(previous, target_hash)
            if auto_hops is not None and previous is not None:
                stamp = previous.timestamp.astimezone().strftime("%b %d %H:%M")
                auto_source = f"last trace · {stamp}"
            else:
                auto_hops, auto_source = (), "direct — no known route"
    elif mode == "path":
        auto_hops = _previous_walk(previous)
        if auto_hops is not None and previous is not None:
            stamp = previous.timestamp.astimezone().strftime("%b %d %H:%M")
            auto_source = f"last walk · {stamp}"

    def auto_spec() -> str:
        """The spec auto forces at the session's current width (``""`` = path-less).

        A path walk's auto spec is the stored hops verbatim — they were proven at
        the width they were transmitted, so no re-rendering is applied to them.
        """
        if auto_hops is None:
            return ""
        if target_hash is None:
            return ",".join(auto_hops)
        return render_forced_spec(auto_hops, target_hash, width_bytes)

    async def unaddressable() -> None:
        """Explain why target-mode path features need a resolvable target."""
        await session.message_dialog(
            Text(
                f"{target!r} isn't a known contact or a hex key prefix, so a forced "
                "path can't end at it. Trace it device-routed, or pick a contact.",
            ),
            title="No destination hash",
        )

    async def fetch_neighbours_via(repeater: Contact, repeater_id: str) -> bool:
        """Log in to a repeater, fetch its neighbour table, and persist the snapshot.

        The composer's fetch action lands here: password from the admin store (or a
        one-time prompt, remembered on success — the tx-optimize convention), then the
        login + fetch run under a floating spinner dialog with Abort. Every outcome
        closes its own ``runs`` row; a rejected login also forgets the stored password
        so the next attempt asks fresh.

        Args:
            repeater: The repeater contact to query (carries the public key).
            repeater_id: Its canonical id, the key the snapshot is stored under.

        Returns:
            ``True`` when new neighbour links were recorded (the caller should rebuild
            the topology); ``False`` on cancel, failure, or an empty table.
        """
        password = ctx.admin_store.get(repeater)
        if password is None:
            password = await session.text(
                f"Admin password for {repeater.name}",
                prompt="The repeater ignores neighbour requests without an admin login.",
                password=True,
            )
            if not password:
                return False
        run_id = ctx.repo.start_run(
            "trace",
            {"mode": "neighbours", "repeater": repeater.name},
            ctx.profile_name,
        )
        spinner = Spinner()
        dialog = TracingDialog(
            f"Fetch neighbours — {repeater.name}", spinner=spinner, on_abort=lambda: None
        )
        dialog.show_last = False  # a fetch has no streaming replies to echo
        dialog.status = f"logging in to {repeater.name}…"

        async def work() -> list[NeighbourInfo]:
            outcome = await device.admin_login(repeater, password)
            ctx.admin_store.record(repeater, password, outcome)
            if outcome is LoginResult.REFUSED:
                raise DeviceAuthenticationError(
                    f"{repeater.name!r} rejected the admin login (wrong password?). "
                    "The saved password was cleared; retry to enter a new one."
                )
            if not outcome:
                raise DeviceCommandError(
                    f"No reply from {repeater.name} — it may be out of reach, asleep, or "
                    "busy. The saved password was kept; retry when it answers."
                )
            dialog.status = "fetching the neighbour table…"
            session.invalidate()
            return await device.fetch_neighbours(repeater)

        task = asyncio.ensure_future(work())
        dialog.on_abort = task.cancel

        async def animate() -> None:
            while True:
                await asyncio.sleep(spinner_interval())
                spinner.tick()
                session.invalidate()

        ticker = asyncio.ensure_future(animate())
        session.push(dialog)
        error: BaseException | None = None
        aborted = False
        entries: list[NeighbourInfo] = []
        try:
            entries = await task
        except asyncio.CancelledError:
            aborted = True
        except Exception as exc:  # noqa: BLE001 - surface in a dialog, keep composing
            error = exc
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a spinner hiccup must never break a fetch
                pass
            session.pop(dialog)
        if aborted:
            ctx.repo.finish_run(run_id, "error", {"error": "aborted"})
            return False
        if error is not None:
            # The credential was already settled where the outcome was known (see ``work``
            # and AdminStore.record); an exception here is only ever a message to show.
            ctx.repo.finish_run(run_id, "error", {"error": str(error) or type(error).__name__})
            await session.message_dialog(Text(str(error), style="err"), title="Fetch neighbours")
            return False
        ctx.repo.record_neighbours(run_id, repeater_id, entries)
        ctx.repo.finish_run(run_id, "ok", {"repeater": repeater.name, "neighbours": len(entries)})
        if not entries:
            # Verified on real firmware: an empty table is a normal answer (repeaters
            # forget neighbours across reboots and relearn them from adverts).
            await session.message_dialog(
                Text(
                    f"{repeater.name} answered, but its neighbour table is empty — "
                    "it relearns neighbours from received adverts, so ask again later.",
                    style="muted",
                ),
                title="Fetch neighbours",
            )
            return False
        return True

    async def compose(current: str) -> str | None:
        """Open the hop-by-hop composer seeded with the current spec's hops.

        Runs the composer in a loop: a :class:`FetchNeighbours` resolution performs the
        fetch, rebuilds the topology with the new evidence, and reopens the composer
        exactly where the user stood (same hops and insertion cursor, refreshed
        suggestions).
        """
        if mode == "target" and target_hash is None:
            await unaddressable()
            return None
        topo = fresh_topology()
        target_id = (
            (topo.canonical(target_hash) or target_hash[:12]) if target_hash is not None else None
        )
        # Re-seed from the current spec. A target-mode spec is the symmetric boomerang
        # (hops, target, mirror): seed just the outbound hops and let the composer
        # regenerate the rest. A path walk is its spec verbatim. Tokens no contact
        # matches are kept as they are rather than dropped — an adopted route must
        # survive a reopen even where the evidence graph is blind.
        tokens = [p.strip() for p in current.split(",") if p.strip()]
        ids = [topo.canonical(h) or h for h in tokens]
        if mode == "path":
            seed = ids
        elif ids and len(ids) % 2 == 1 and ids == ids[::-1]:
            seed = ids[: len(ids) // 2]
        elif target_id in ids:
            seed = ids[: ids.index(target_id)]  # a stale hand walk: keep the outbound
        else:
            seed = ids
        seed_cursor: int | None = None  # first open parks the cursor at the end
        while True:
            # Nodes whose neighbour table can be asked for: repeater contacts with a
            # public key to log in against (our own node has nothing new to tell us).
            fetchable: dict[str, Contact] = {}
            for c in contacts:
                if not c.is_repeater or not (c.public_key or "").strip():
                    continue
                cid = topo.canonical(c.public_key)
                if cid is not None and cid != topo.self_id:
                    fetchable[cid] = c
            screen = PathComposerScreen(
                device_label=device_label,
                device_hash=device_hash,
                topology=topo,
                width_bytes=width_bytes,
                target_id=target_id,
                target_hash=target_hash,
                target_label=target_label,
                hops=seed,
                cursor=seed_cursor,
                fetch_nodes=frozenset(fetchable),
                resolve=resolve,  # name a hop the topology left ambiguous, as we do
            )
            result = await session.run_screen(screen)
            if result is CANCEL:
                return None
            if isinstance(result, FetchNeighbours):
                seed = screen.hops  # resume mid-thought after the fetch
                seed_cursor = screen.cursor
                repeater = fetchable.get(result.node)
                if repeater is not None and await fetch_neighbours_via(repeater, result.node):
                    topo = fresh_topology()  # fold the new reports into suggestions
                continue
            return result

    async def pick_width(current: str) -> str | None:
        """Float the path-hash width picker and re-render the standing spec to match.

        Each row previews a key with the addressed slice lit at that width, so the
        choice reads as "this much of every key goes on the air". The chosen width
        shapes every spec the composer/explorer emit afterwards; a standing forced
        path is re-rendered immediately — hops are widened back through their
        canonical hashes where known, then collapsed uniformly (a hop only ever known
        narrower keeps the whole spec at what it can honour).

        Returns:
            The re-rendered spec, or ``None`` when cancelled, unchanged, or there is
            no forced path to re-render (the width itself still sticks).
        """
        nonlocal width_bytes
        sample = (target_hash or device_hash or "").lower().removeprefix("0x")
        items: list = []
        for w in (1, 2, 4):
            title = Text(f"{w} byte{'s' if w > 1 else ' '}")
            if sample:
                title.append("  ")
                title.append_text(highlighted_hash(sample, w, width=16))
            if w == device_width:
                title.append("  · device default", style="muted")
            items.append(Choice(title=title, value=w))
        picked = await session.run_screen(
            SelectScreen(
                "Path width",
                items,
                prompt="Forced hops are addressed by this many leading key bytes.",
                default=width_bytes,
                footer_hint="↑↓ move · Enter set · Esc keep",
                filterable=False,
            )
        )
        if picked is CANCEL or picked is None or int(picked) == width_bytes:
            return None
        width_bytes = int(picked)
        tokens = [p.strip() for p in current.split(",") if p.strip()]
        if not tokens:
            return None
        topo = fresh_topology()
        full = [topo.canonical(t) or t for t in tokens]
        width = collapse_width(*full, ceiling=width_bytes)
        return ",".join(f[: width * 2] for f in full)

    async def pick_samples(current: str) -> str | None:
        """Float the sample-count picker: how many traces one Trace action runs.

        Multi-trace runs are paced by the configured cooldown between transmissions,
        so choosing 8 is a deliberate sampling session, never a burst. Always resolves
        ``None`` — the count is session state the screen reads back, not a spec.
        """
        nonlocal sample_count
        pace = ctx.preferences.trace_cooldown_s
        items: list = []
        for n in SAMPLE_CHOICES:
            title = Text(f"{n} trace{'s' if n > 1 else ' '}")
            if n == 1:
                title.append("  · a single transmission", style="muted")
            items.append(Choice(title=title, value=n))
        picked = await session.run_screen(
            SelectScreen(
                "Sample count",
                items,
                prompt=f"One Trace action runs this many traces, {pace:g} s apart.",
                default=sample_count,
                footer_hint="↑↓ move · Enter set · Esc keep",
                filterable=False,
            )
        )
        if picked is not CANCEL and picked is not None:
            sample_count = int(picked)
        return None

    def outcome_lanes(rank: int, outcome: ProbeOutcome) -> Text:
        """A probed candidate's fixed head: rank, verdict, bottleneck SNR, round trip.

        Split out from :func:`outcome_title` so the row can *measure* the block it pins
        out of the ←→ scroll (:attr:`~meshterm.ui.tui.select.Choice.hscroll_from`) rather
        than guess at it. These lanes are the whole reason the list is ranked — sliding
        them off to read the tail of a ten-hop spec would cost the row its identity and
        gain nothing, since the columns are the part that already fits.
        """
        stats = outcome.stats
        text = Text(f"#{rank}  ", style="muted")
        if stats.successes:
            text.append("✓ replied", style="ok")
        else:
            text.append("✗ no reply", style="err")
        snr = stats.median_min_snr
        if snr is not None:
            text.append("  min ", style="muted")
            text.append(f"{snr:+.1f} dB", style=snr_style(snr))
        if stats.median_rtt_ms is not None:
            text.append(f"  {stats.median_rtt_ms:.0f} ms", style="muted")
        text.append("  via ", style="muted")
        return text

    def outcome_title(lanes: Text, outcome: ProbeOutcome) -> Text:
        """One probed candidate as a ranked select row: one measured trace, not a guess."""
        text = lanes.copy()
        text.append(outcome.candidate.spec, style="brand")
        text.append(f"  ({outcome.candidate.label})", style="faint")
        return text

    async def run_probe(
        candidates: list[ProbeCandidate],
    ) -> list[ProbeOutcome] | None:
        """Measure every candidate — one trace each — under an abortable dialog.

        One ``runs`` row spans the sweep; each trace and each candidate aggregate is
        recorded under it, so an aborted probe still keeps everything it measured.
        Each candidate gets exactly one transmission (probing is comparison, not
        sampling); ``None`` when aborted.
        """
        run_id = ctx.repo.start_run(
            "trace",
            {
                "target": target,
                "mode": "probe",
                "paths": [c.spec for c in candidates],
            },
            ctx.profile_name,
        )
        spinner = Spinner()
        dialog = TracingDialog(f"Probing — {target_label}", spinner=spinner, on_abort=lambda: None)
        dialog.status = f"path 1/{len(candidates)} · one trace each"

        def on_result(index: int, done: int, result: TraceResult) -> None:
            dialog.status = f"path {index + 1}/{len(candidates)} · one trace each"
            dialog.last = result
            session.invalidate()

        sweep = asyncio.ensure_future(
            probe_paths(
                device,
                target,
                candidates,
                cooldown_s=ctx.preferences.trace_cooldown_s,
                on_result=on_result,
                persist_trace=lambda t: ctx.repo.record_trace(run_id, t),
                persist_candidate=lambda o: ctx.repo.record_path_candidate(
                    run_id,
                    target,
                    o.candidate.spec,
                    bottleneck_snr=o.stats.median_min_snr,
                    success_rate=o.stats.success_rate,
                    median_rtt_ms=o.stats.median_rtt_ms,
                ),
            )
        )
        dialog.on_abort = sweep.cancel  # the task exists only now; rewire the button

        async def animate() -> None:
            while True:
                await asyncio.sleep(spinner_interval())
                spinner.tick()
                session.invalidate()

        ticker = asyncio.ensure_future(animate())
        session.push(dialog)
        error: BaseException | None = None
        aborted = False
        outcomes: list[ProbeOutcome] = []
        try:
            outcomes = await sweep
        except asyncio.CancelledError:
            aborted = True
        except Exception as exc:  # noqa: BLE001 - surface in a dialog, keep the screen
            error = exc
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a spinner hiccup must never break a probe
                pass
            session.pop(dialog)
        if aborted:
            # Everything measured before the abort is already persisted; the run row
            # just records that the sweep didn't finish.
            ctx.repo.finish_run(run_id, "error", {"error": "aborted"})
            return None
        if error is not None:
            ctx.repo.finish_run(run_id, "error", {"error": str(error) or type(error).__name__})
            await session.message_dialog(
                Text(f"probe failed: {error}", style="err"), title="Path probe"
            )
            return None
        best = outcomes[0] if outcomes else None
        ctx.repo.finish_run(
            run_id,
            "ok",
            {
                "target": target,
                "paths": len(candidates),
                "best_path": best.candidate.spec if best else None,
                "best_success_rate": round(best.stats.success_rate, 3) if best else None,
                "best_median_min_snr": best.stats.median_min_snr if best else None,
            },
        )
        return outcomes

    async def explore(current: str) -> str | None:
        """The scenario flow: browse ranked candidate routes, adopt one, or probe all."""
        if target_hash is None:
            await unaddressable()
            return None
        topo = fresh_topology()
        target_id = topo.canonical(target_hash) or target_hash[:12]
        scenarios = topo.scenarios(target_id, device_route=device_route)
        if not scenarios:
            await session.message_dialog(
                Text(
                    "no observed evidence involving this target yet — run a trace or "
                    "let monitoring accumulate paths first.",
                    style="muted",
                ),
                title="Explore paths",
            )
            return None

        items: list = [section_heading("Candidate paths · from received evidence")]
        for scenario in scenarios:
            items.append(
                Choice(
                    # Width-aware (see Choice.title): a long candidate is cut to the
                    # terminal, cracking on its own chip exactly as the highlighted row
                    # does at shift zero — the highlight then keeps its natural length
                    # and slides under ←→ instead of the row being redrawn as it lands.
                    title=(
                        lambda width, scenario=scenario: _scenario_path(
                            scenario,
                            topo,
                            target_id,
                            device_label=device_label,
                            width_bytes=width_bytes,
                            width=width,
                        )
                    ),
                    detail=_scenario_detail(scenario),
                    value=("use", scenario),
                )
            )
        items.append(Separator(" "))
        items.append(
            Choice(
                title=marked_label("⚡", "Probe all — trace each path once and rank", "warn"),
                value=("probe", None),
            )
        )
        picked = await session.run_screen(
            SelectScreen(
                f"Explore paths — {target_label}",
                items,
                prompt="Choose the outbound leg — the return mirrors it.",
                footer_hint="↑↓ move · Enter adopt/probe · Esc back",
                hscroll=True,  # a long candidate row slides under ←→ instead of truncating
            )
        )
        if picked is CANCEL or picked is None:  # Esc
            return None
        if picked[0] == "use":
            return picked[1].spec(target_hash, width_bytes)

        # Probe all: measure each distinct spec (scenarios can collapse to the same
        # spec once truncated to the trace width — the radio must not walk one twice).
        candidates: list[ProbeCandidate] = []
        for scenario in scenarios:
            spec = scenario.spec(target_hash, width_bytes)
            if all(c.spec != spec for c in candidates):
                candidates.append(ProbeCandidate(label=scenario.label, spec=spec))
        outcomes = await run_probe(candidates)
        if not outcomes:
            return None
        result_items: list = [section_heading("Ranked · reliability, then bottleneck SNR")]
        for rank, outcome in enumerate(outcomes, start=1):
            lanes = outcome_lanes(rank, outcome)
            result_items.append(
                Choice(
                    title=outcome_title(lanes, outcome),
                    value=outcome,
                    hscroll_from=lanes.cell_len,
                )
            )
        result_items.append(Separator(" "))
        result_items.append(Choice(title="Keep current path", value=None))  # the exit: no adoption
        adopted = await session.run_screen(
            SelectScreen(
                f"Probe results — {target_label}",
                result_items,
                footer_hint="↑↓ move · Enter adopt path · Esc keep",
                # ←→ read the tail of a long spec; the ranking lanes in front of it stay
                # pinned (each row declares their width — see outcome_lanes).
                hscroll=True,
            )
        )
        if adopted is CANCEL or adopted is None:
            return None
        return adopted.candidate.spec

    # -- trophy case: score every walk that comes home ---------------------------------
    # A trace is a walk (it starts and ends at us — a Trace path route directly, a
    # Trace target boomerang just the same), so every successful reply is scored against
    # the six trophy-case disciplines and offered to their boards. See services/records.

    def _as_float(value) -> float | None:  # noqa: ANN001
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    _lat, _lon = _as_float(self_info.get("adv_lat")), _as_float(self_info.get("adv_lon"))
    self_pos = (_lat, _lon) if _lat is not None and _lon is not None and (_lat or _lon) else None

    def build_positions(topo: MeshTopology) -> dict[str, tuple[float, float]]:
        """Node positions by canonical id: advert history first, contacts over it."""
        positions: dict[str, tuple[float, float]] = {}
        for heard in ctx.repo.heard_nodes():
            if heard.node and heard.lat is not None and heard.lon is not None:
                positions[heard.node] = (heard.lat, heard.lon)
        for contact in contacts:
            cid = topo.canonical(contact.public_key or contact.key_prefix)
            if (
                cid is not None
                and contact.lat is not None
                and contact.lon is not None
                and (contact.lat or contact.lon)
            ):
                positions[cid] = (contact.lat, contact.lon)
        return positions

    def offer_records(result: TraceResult) -> list[str]:
        """Score a walk home and offer it to every board; return the ones it placed.

        Returns the placed disciplines as short ``"Title — score"`` labels for the
        screen's inline note. Defensive by design — a scoring hiccup must never break
        a trace — so any failure yields no records rather than propagating.
        """
        from .. import __version__
        from ..services.records import CATEGORY_BY_ID, walk_from_trace, walk_scores

        try:
            topo = fresh_topology()
            derived = walk_from_trace(
                result,
                canonical=topo.canonical,
                positions=build_positions(topo),
                self_pos=self_pos,
            )
            if derived is None:
                return []
            spec, route, stats = derived
            placed: list[str] = []
            for cid, score in walk_scores(stats).items():
                cat = CATEGORY_BY_ID[cid]
                row_id = ctx.repo.record_discovery(
                    cid,
                    width_bytes,
                    spec,
                    route,
                    score=score,
                    stats=stats.as_dict(),
                    app_version=__version__,
                    ascending=cat.ascending,
                )
                if row_id is not None:
                    placed.append(f"{cat.title} — {cat.format_score(score)}")
            return placed
        except Exception:  # noqa: BLE001 - scoring must never break a trace
            return []

    async def trace_once(
        path_spec: str, on_trace: Callable[[TraceResult, Sequence[str]], None]
    ) -> None:
        """Run one persisted trace: one transmission, its own ``runs`` row.

        The screen calls this once per sample; multi-trace runs are paced there, so
        the repeaters never see a burst regardless of the chosen count. A walk that
        comes home is scored into the trophy case, and any records it set are handed
        to the screen with the result.
        """
        spec = path_spec.strip() or auto_spec()
        path = trace_runner.parse_trace_path(spec, contacts) if spec else None
        params: dict[str, Any] = {"target": record_target}
        if path:
            params["path"] = path
        run_id = ctx.repo.start_run("trace", params, ctx.profile_name)
        try:
            result = await device.run_trace(record_target, path=path)
        except BaseException as exc:
            # A cancelled or failed trace still closes its run row, so no ``running``
            # orphan is left behind.
            ctx.repo.finish_run(run_id, "error", {"error": str(exc) or type(exc).__name__})
            raise
        ctx.repo.record_trace(run_id, result)
        placed = offer_records(result) if result.success else []
        on_trace(result, placed)
        ctx.repo.finish_run(
            run_id,
            "ok",
            {
                "target": record_target,
                "success": result.success,
                "min_snr": result.min_snr,
                "rtt_ms": result.round_trip_ms,
                "records": placed or None,
            },
        )

    screen = TraceScreen(
        record_target,
        mode=mode,
        device_label=device_label,
        device_hash=device_hash,
        resolve=resolve,
        session=session,
        trace=trace_once,
        compose_path=compose,
        explore=explore if mode == "target" else None,
        pick_width=pick_width,
        pick_samples=pick_samples,
        width_bytes=lambda: width_bytes,
        sample_count=lambda: sample_count,
        pace_s=ctx.preferences.trace_cooldown_s,
        previous=previous,
        auto_spec=auto_spec,
        auto_source=auto_source,
        # A caller-supplied spec arms either mode: a path walk's stored route, or the Node
        # detail screen's suggested best path handed to a target trace. In target mode it
        # only arms when the target is addressable — a forced path must end at a real hash.
        initial_spec=initial_spec if (mode == "path" or target_hash is not None) else "",
        open_trophy_case=_trophy_case_opener(ctx),
    )
    try:
        await session.run_screen(screen)
    finally:
        screen.cancel()
    return screen._total_traces
