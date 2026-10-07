# SPDX-License-Identifier: Apache-2.0
"""The live trace screens: compose a route from the observed topology, then see its replies.

This module is the interactive form of the two trace tools. (The scripted CLI keeps its
one-shot table output.) A trace is one walked path. The protocol has no destination
field, so "target" is only a UX concept. The two menu features divide on that line:

* **Trace target** (:func:`open_trace`) answers the question *can I reach this node?*
  The route is symmetric: the user composes (or explores) the outbound leg, and the
  return leg is those hops in mirror order around the pinned target.
* **Trace path** (:func:`open_trace_path`) answers the question *how far can a route
  that I build go?* It has no target. The user composes the full walk by hand, out and
  back by any way. The only condition is that the walk ends at a node that our node can
  hear.

Both features open the same screen, *armed but idle*. The screen shows the last known
route, the path is what the user makes it, and nothing transmits until the user tells
it to. An action list controls the screen: ↑/↓ select a row, and Enter runs it:

* **Compose path** opens the path composer (:mod:`~meshterm.ui.path_composer`). The user
  builds the route one hop at a time. For each step, the composer suggests hops from the
  links observed in *received* traffic: traces, contact routes that the firmware
  learned, and the packet paths in the RX log. The strongest link comes first. The user
  can also type a raw hex hash for a node that is not in the data. When the insertion
  slot is after a repeater for which the user has admin credentials, the composer can
  also *get the neighbour table of that repeater* over the mesh. A login is necessary,
  because the firmware ignores guests. The table is evidence from a second point of
  view. MeshTerm stores it and adds it immediately to the suggestions. When the user
  confirms the composer, the highlight of the action list moves to Trace: the user
  composes, then Enter walks the path. When the user leaves the composer with Esc, the
  highlight stays where it was.
* **Reverse path** (Trace path only) reverses the hop order of the walk:
  ``us → a → b → c`` becomes ``us → c → b → a``. A route in target mode is a symmetric
  boomerang, so a reversal changes nothing there. Radio links are rarely the same in the
  two directions, so this action measures the route that the user built in the opposite
  direction. It adopts the reversed spec the same as any change of path (the aggregates
  of the forward run clear). Trace stays idle, because nothing starts automatically.
  Thus the user reverses, then traces. Reverse again to go back. The stored history
  keeps the two directions.
* **Explore paths** (Trace target only, because candidates must have a destination)
  explores scenarios: ranked candidate routes to the target, directly from the topology
  evidence. These are the route that the device learned, the direct attempt, and the
  strongest observed alternatives. The user can adopt one directly. Or the user can
  probe all of them: one measured trace for each candidate, stored as
  ``path_candidates`` rows and ranked by reliability first. Then MeshTerm offers the
  winner for adoption. The evidence proposes, the measurement decides, and the user
  makes the choice.
* **Path width** opens a small dialog, where the user selects the path-hash width for
  each hop: 1, 2, or 4 bytes. A forced hop is addressed by this number of bytes from
  the start of its key. The first value is the routing width of the device. When the
  user changes the width, MeshTerm renders the current forced path again at the new
  width. The new width also applies to each spec that the composer and the explorer
  make after the change.
* **Sample count** opens a dialog, where the user selects how many traces one Trace
  action runs (1, 2, 3, 5, or 8). Runs of more than one trace are *paced*: a cooldown
  sleeps between the transmissions. This is because repeaters penalize (and can block)
  nodes that send traffic in bursts. The tracing dialog counts the traces as the replies
  arrive.
* **Trace** runs the selected number of traces. The highlight starts on this row, thus
  Enter alone traces. While the traces are in progress, a floating *tracing* dialog
  (spinner, progress, Abort) is over the screen. The replies go into the log behind
  the dialog. Esc in the dialog cancels the run, but the screen stays open, and MeshTerm
  keeps the traces that it already stored. The screen adds each trace of the session to
  its medians. A walk can cross a link two times in the same direction. Before such a
  walk, an amber Cancel/Trace dialog asks for confirmation, because such a walk is not
  a *trail*, and the trophy case ignores it (the no-cheat rule, refer to
  :mod:`~meshterm.services.records`). When a run places on the boards, a *New record*
  dialog opens after the run ends. Close (the default) stays on the screen. Trophy case
  opens the boards over the trace screen, so Esc from the boards goes back to the trace.

Esc leaves the screen. The screen has no Back row.

The layout, from top to bottom:

1. The walked route. This is the live route when a reply arrived. If not, it is the
   route that the next Trace will walk: composed by hand, or resolved automatically
   from the route that the device learned or from the stored history, with a label
   that shows that source. If not, it is the most recent stored trace.
2. The robust aggregates of the run.
3. The wire spec of that route.
4. The action list.
5. The results: the median SNR for each hop, with quality bars, and the individual
   traces, newest first.

All these parts are on the same page. ↑/↓ move the highlight of the action list. After
the last action, they edge-scroll into the results. PgUp/PgDn/Home/End scroll the full
screen by pages, and ←/→ slide the path of a hop where it goes past the edge.

The two path lanes draw through the only path widget (:mod:`~meshterm.ui.pathline`).
They break at hop boundaries, under their own value column. A route folds between nodes.
It never separates a name from the hash that addresses it, and it prefers the turn where
the mirrored return of a boomerang starts. The spec below the route folds after a comma,
never in the middle of a hash. The source of a route (the source of an automatic plan,
or the timestamp of a previous walk) is on the line below the route, not at its right
edge, because there it takes space from the route.

MeshTerm stores each trace the same as a scripted run: one ``runs`` row for each trace,
with the trace stored under it. Thus the stored history is the same, whichever front end
made it. Path walks are stored under :data:`~meshterm.core.models.PATH_TRACE_TARGET`.
Thus they are not in the history of the target picker, but they still give the
previous-route line of the path screen.
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

#: The width of the SNR quality bar for each hop, in characters. Each character holds
#: two fill steps (refer to :func:`snr_bar`). Thus the bar has a resolution of 16 steps
#: in half the cells of a block bar with one step for each cell.
_BAR_WIDTH = 8

#: The SNR range of the bars, in dB: -15 (almost not readable) to +10 (excellent). A
#: value outside the range clamps to an end, so the bar always shows *something* for a
#: heard hop.
_BAR_SNR_MIN = -15.0
_BAR_SNR_MAX = 10.0

#: The number of cells that one ←/→ press slides the paths of the hops. All the other
#: sliding path rows in the app use the same step (the ``hscroll`` of the select list,
#: the routes of the node page).
_HSCROLL_STEP = 8

#: The sample counts that the Sample count dialog offers: how many traces one Trace
#: action runs, with pacing between the transmissions.
SAMPLE_CHOICES = (1, 2, 3, 5, 8)

#: The value of the *Trophy case* button in the new-record dialog. This button opens the
#: trophy case over the trace screen, which stays pushed below it. Thus Esc from the
#: trophy case is one pop back to the trace that got the record.
OPEN_TROPHY_CASE = "records"


def _trophy_case_opener(ctx: AppContext) -> Callable[[], Awaitable[None]]:
    """A trophy case opener with no arguments, for the left button of the new-record dialog.

    The import is inside the closure, because the trophy case also imports this module
    (its records offer their own *Trace this path*). The two screens open each other. A
    strict stack permits this cycle, and ^W is the way out of it.
    """

    async def open_it() -> None:
        from .records_screen import open_records

        await open_records(ctx)

    return open_it


#: A runner for one trace: ``(path_spec, on_trace)`` → runs exactly one trace. When the
#: trace arrives, the runner gives the result to ``on_trace``, together with the
#: trophy case disciplines that the walk placed in, as short ``"Title — score"`` labels.
#: The session openers supply it. They close over the device, the repository, and the
#: preferences, so that the screen has no persistence or scoring code.
TraceOnce = Callable[[str, Callable[[TraceResult, Sequence[str]], None]], Awaitable[None]]

#: A path picker flow. It takes the current spec, runs its own dialogs over the screen,
#: and resolves to the new spec (``""`` = device-routed) or to ``None`` to keep the
#: current spec. The sample-count flow has the same shape, and it always resolves
#: ``None``.
PathFlow = Callable[[str], Awaitable[str | None]]

#: The label column of the route lane: the word and its padding. Its width is the
#: column under which the wrapped continuations of the route (and its source note) hang.
_ROUTE_LANE = "route  "

#: The label column of the path lane. It has the same label width as the aggregate
#: block, so the wire spec starts, and hangs, in the same column as the numbers above it.
_PATH_LANE = "path            "

#: All the marks at the start of the action rows, in row order. This list is necessary
#: to *measure* the icon column. ``⚡`` is an emoji of two cells, and the other marks
#: are one cell. Thus with a fixed ``"icon "`` prefix, the label of Explore starts one
#: column to the right of the labels of all the other actions. Refer to
#: :func:`_icon_lane`.
_ACTION_ICONS = ("✎", "⇄", "⚡", "⚙", "#", "▶")


def _icon_lane() -> int:
    """The icon column of the action rows, in cells: the marks of this screen, measured once."""
    return icon_lane(_ACTION_ICONS)


def _icon(text: Text, icon: str, style: str) -> None:
    """Append the mark of an action row in the icon column, with its trailing space."""
    text.append_text(icon_mark(icon, style, _icon_lane()))


def _lane_lines(label: str, value: list[Text], width: int) -> list[str]:
    """Render a labelled lane whose value hangs, already broken, under its own column.

    This function is the counterpart of :func:`~meshterm.ui.tui.render.render_hanging`
    for values that know their own line breaks.
    :meth:`~meshterm.ui.pathline.PathLine.wrapped` returns lines that wrap at hops and
    have their indent. A second wrap here only undoes that work. The label goes on the
    first line. The other lines are drawn as they are.

    Args:
        label: The label column of the lane, with its padding (the value hangs under it).
        value: The lines of the value. The first is bare, and the continuations have
            their own indent.
        width: The render width. Lines are cropped, never folded.

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
    """The part of ``line`` that a viewport of ``width`` cells shows, slid ``shift`` cells in.

    Each edge where the line continues has a :func:`~meshterm.ui.pathline.cut_mark`: a
    chip broken off in its own fill, or the ``…`` of an arrow line. The marks are chrome
    inside the viewport, so each mark uses one of its cells. The ``hscroll`` rows of the
    select list and the routes of the node page slide through the same viewport.
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
    """The trailing note of a lane (a source, a stamp, a count), on its own hanging line."""
    return Text(" " * indent + text, style=style)


def _spec_line(spec: str) -> PathLine:
    """The literal wire spec as a path line: joined with commas, each hop in its own hue.

    The line has exactly the characters that the radio gets (``3d63,7f21,3d63``). The
    purpose of the lane is to show the exact spec, so it stays plain-mode text and does
    not become chips. The only path widget adds two things. The first is the key colouring
    of the app: each addressed part of a key is lit in its own hue, which comes from the
    key, instead of one flat brand colour. The second is wrapping at hop boundaries, so
    a spec that is too long for its column breaks after a comma.

    Args:
        spec: The wire spec (hex hop hashes, separated by commas. Blanks are accepted).

    Returns:
        The :class:`~meshterm.ui.pathline.PathLine` of the spec, separated by commas.
    """
    tokens = [t.strip() for t in spec.split(",") if t.strip()]
    hops = [PathHop(t, key=t, lit_bytes=len(t) // 2) for t in tokens]
    return PathLine(hops, mode="plain", separator=",")


def snr_bar(snr: float | None, width: int = _BAR_WIDTH) -> Text:
    """Render an SNR reading as a horizontal quality bar in the shared SNR colours.

    This is the slim form, on a track, of the braille
    :func:`~meshterm.ui.braillechart.meter` of the app. The reading maps onto the range
    :data:`_BAR_SNR_MIN` → :data:`_BAR_SNR_MAX`. It fills a dark track of the same glyph
    at two steps for each cell.

    Args:
        snr: The reading in dB, or ``None`` (renders as a track with no fill).
        width: The width of the bar track in characters (each one has two fill steps).

    Returns:
        A :class:`Text` of filled braille cells over a dark track of the same glyph,
        coloured by :func:`~meshterm.ui.theme.snr_style`.
    """
    if snr is None:
        return meter(None, width, style="track", slim=True, track="track")
    span = _BAR_SNR_MAX - _BAR_SNR_MIN
    frac = min(1.0, max(0.0, (snr - _BAR_SNR_MIN) / span))
    return meter(frac, width, style=snr_style(snr), slim=True, track="track")


class TracingDialog(Screen):
    """The floating dialog for work in progress: a spinner chip, live progress, and Abort.

    The dialog is pushed over the trace screen while traces (or a scenario probe)
    transmit. Thus the user cannot miss the activity, or the way out, while the replies
    continue to arrive in the screen behind it. Enter, Esc, or Space aborts through the
    injected callback. The owner pops the dialog when the work ends, so the dialog never
    resolves a value of its own.
    """

    footer_hint = "Enter/Esc abort"

    @property
    def picocalc_lyra_lane(self):
        """No lane: the only verb of the dialog is abort, and Enter and Esc already do it."""
        from .tui.fkeys import EMPTY_LANE

        return EMPTY_LANE

    def __init__(self, title: str, *, spinner: Spinner, on_abort: Callable[[], None]) -> None:
        """Build the dialog.

        Args:
            title: The heading for the dialog border (for example ``Tracing — Lakeside``).
            spinner: The spinner to animate (shared with the ticker of the owner).
            on_abort: Called when the user asks to abort (it must be idempotent).
        """
        super().__init__()
        self.title = title
        self._spinner = spinner
        #: Called when the user aborts. It is public, so that an owner whose task starts
        #: only after the dialog (the probe sweep) can connect it again.
        self.on_abort = on_abort
        #: The progress on one line. The owner changes it as the replies arrive.
        self.status = "transmitting…"
        #: The most recent trace result, shown below the status line.
        self.last: TraceResult | None = None
        #: Whether to render the last-reply line. Trace and probe owners show their
        #: replies on it. Owners that send a request (a neighbour fetch) have no replies
        #: to show.
        self.show_last = True

    @property
    def dialog_width(self) -> int:
        """The natural outer width, around the widest line (the compositor can still cap it)."""
        widths = [
            cell_len(self.title),
            cell_len(self.footer_hint),
            cell_len(self.status) + 4,
            len("last  ✗ no reply — will retry"),
            len("  Abort  "),
        ]
        return max(widths) + 8

    def _last_line(self) -> Text:
        """The most recent reply, or a waiting note before the first reply arrives."""
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

        The button has the only button look of the app: a centred reverse-video chip in
        the shared ``selected`` fill. The ButtonDialog and the reconnect dialog draw
        their buttons the same way. Thus the committing control of each dialog looks
        the same.
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
        """A commit or dismiss key aborts the work in progress. All other keys do nothing."""
        if action in ("enter", "escape", "space"):
            self.on_abort()


class TraceScreen(Screen):
    """A full-screen live trace session: armed, but idle until the user starts a trace.

    One screen serves the two trace features, and ``mode`` selects the feature:

    * ``"target"`` traces a pinned destination over a symmetric (mirrored) route, and
      offers *Explore paths*.
    * ``"path"`` walks a route that the user composed by hand, with no target. It has no
      Explore (ranked candidates must have a destination) and no device routing. But it
      has a *Reverse path*, which turns the asymmetric walk end for end. Trace does
      nothing until a path is available to walk (composed here, or resolved
      automatically from the last stored walk).

    When the screen adopts a different path (composed, explored, reversed, or rendered
    again at a new width), the measurement starts again. The aggregates, the medians for
    each hop, and the trace log all described the old route. Thus they clear, as if the
    screen was new.

    ↑/↓ move the highlight over the action rows, and Enter runs the selected row. The
    highlight starts on Trace, so Enter alone traces. The results (the medians for each
    hop and the trace log) come after the actions on the page. ↓ on the last action
    edge-scrolls into the results, and PgUp/PgDn/Home/End move the full screen. Esc
    leaves the screen and cancels a trace in progress. MeshTerm keeps the traces that it
    already stored.
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
        """Create the screen (nothing transmits until the user runs Trace).

        Args:
            target: The label under which the traces are stored: the name of the
                destination in target mode, :data:`~meshterm.core.models.PATH_TRACE_TARGET`
                in path mode.
            mode: ``"target"`` (symmetric, can be explored) or ``"path"`` (a walk
                composed by hand, with no destination).
            device_label: The name of our node, which labels the endpoints of the route.
            device_hash: The public key of our node, so that the endpoints have a hash
                the same as each hop.
            resolve: Changes the raw hash of a hop to a friendly contact name, when it
                is known.
            session: The running TUI session (for paints and the floating dialog).
            trace: Runs exactly one trace and returns the result (refer to
                :data:`TraceOnce`). The screen runs it in a loop for runs with more
                than one sample.
            compose_path: Opens the path composer (one hop at a time) over this screen,
                with the current spec as its start. Resolves to the new spec, or to
                ``None`` if the user cancels.
            pick_width: Opens the path-hash width dialog. Resolves to the current spec,
                rendered again at the selected width, or to ``None`` when nothing
                changes.
            pick_samples: Opens the sample-count dialog. Always resolves ``None``
                (the owner keeps the count, and the screen reads it through
                ``sample_count``).
            width_bytes: Reads the width for each hop that is selected now, for the
                label of the action row. (The owner keeps the width, because the flows
                make specs at that width.)
            sample_count: Reads the sample count that is selected now, for the action
                rows and for the number of traces that one Trace action runs.
            explore: Opens the scenario browser and probe flow over this screen (target
                mode only). Resolves to an adopted spec, or to ``None`` to keep the
                current spec.
            pace_s: The cooldown that sleeps between the traces of a run with more than
                one sample. Thus a sample session is never a burst for the repeaters.
            previous: The most recent stored trace, if one exists. Its route is the
                first value of the route line, so that the screen opens with the last
                path in the history.
            auto_spec: Renders the spec that an *auto* trace (with no composed path)
                walks now. The session resolves it from the route that the device
                learned, or from the stored history, at the current width. (In path
                mode, it is the last successful stored walk, unchanged.) ``""`` means a
                trace with no path (a target that cannot be addressed, or a path walk
                with no history).
            auto_source: The short source of the automatic route (for example
                ``device route`` or ``last trace · Jul 09 14:32``), for the route line
                and the summary. Thus the screen never shows a route that the radio
                did not get.
            initial_spec: A forced path on which the screen opens armed, instead of
                idle on the automatic route. *Trace this path* in the trophy case uses
                it to open the exact route of a record again, ready to walk. Empty (the
                default) opens on the automatic route.
            open_trophy_case: Opens the trophy case over this screen. The left button
                of the new-record dialog does this. ``None`` leaves only *Close* in the
                dialog, which is what a screen that runs outside the menu gets.
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
        #: The trophy case disciplines that the current run placed in. The key is the
        #: title of the discipline, so that a run with more than one sample that
        #: improves keeps only its latest ``"Title — score"`` label for each board.
        #: Cleared when a run starts. Moved into the floating new-record dialog when the
        #: run ends.
        self._run_placed: dict[str, str] = {}
        #: The traces aggregated on the screen: the run of the current route. When the
        #: screen adopts a different path, this list clears (the old numbers describe
        #: the old route).
        self._traces: list[TraceResult] = []
        #: Increased at each change of ``_traces``. It is the key on which the caches
        #: for each frame below expire. (A bare ``len`` does not see a clear followed
        #: by a refill.)
        self._traces_rev = 0
        # The aggregate stats and the windowed results block. Each one is valid for one
        # traces revision (refer to render_body and _tail_lines). Thus a paint that
        # changed nothing reads them again, and does not aggregate each stored trace
        # again.
        self._stats_memo: tuple[int, TraceStats] | None = None
        self._tail_memo: tuple[tuple, list[str]] | None = None
        #: All the traces that this screen ran, across changes of path. It is the
        #: session count that the owner reports, and the clears for each route above
        #: do not change it.
        self._total_traces = 0
        self._running = False
        self._dialog_open = False
        self._status = ""
        self._progress: tuple[int, int] | None = None  # (current, total) during a run
        self._spinner = Spinner()
        #: The loop clock at the last transmission of this screen. :meth:`_pace_remaining`
        #: measures the cooldown from it, so the pacing stays after an abort and a retry.
        self._last_tx: float | None = None
        self._worker: asyncio.Task | None = None
        self._flight: TracingDialog | None = None
        # The action rows, in display order. The build-path group is first: Compose,
        # then Explore (target mode) or Reverse (path mode). Explore must have a
        # destination for which it ranks candidates, so path mode does not have it.
        # Only a path walk is asymmetric enough to turn end for end, so only path mode
        # offers Reverse. The highlight starts on Trace in the two modes.
        actions = ["compose", "explore"] if mode == "target" else ["compose", "reverse"]
        actions += ["width", "samples", "trace"]
        self._actions: tuple[str, ...] = tuple(actions)
        self._index = self._actions.index("trace")
        self._pin_cursor = False  # pin the viewport only while ↑/↓ are in use
        #: How far ←→ slid the paths of the hops (in cells), and how far they can slide:
        #: the tail of the widest path, as the last paint measured it. It is 0 while the
        #: full path of each row is visible, and this also keeps ``←→ scroll`` out of
        #: the footer.
        self._hshift = 0
        self._hop_pan = 0

    # --- state -----------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The footer keys, which change while traces are in progress."""
        if self._running:
            if self._progress is not None and self._progress[1] > 1:
                done, total = self._progress
                return f"tracing {done}/{total}… · PgUp/PgDn scroll · Esc back"
            return "tracing… · PgUp/PgDn scroll · Esc back"
        slide = " · ←→ scroll" if self._hop_pan else ""
        return f"↑↓ actions{slide} · Enter run · PgUp/PgDn scroll · Esc back"

    def start_trace(self) -> None:
        """Start a trace run in the background (does nothing while a run is in progress)."""
        if self._running:
            return
        self._running = True
        self._status = ""
        self._run_placed.clear()  # this run gets its own records
        self._spinner.reset()
        self._worker = asyncio.ensure_future(self._run_trace())
        self._session.invalidate()

    async def _run_trace(self) -> None:
        """Run one Trace action (the selected number of traces) under the dialog.

        Each transmission is paced, not only the transmissions in a run with more than
        one sample. :attr:`_pace_s` is measured from the *last* transmission of this
        screen. Thus if the user aborts a walk and runs it again immediately, the run
        waits for the same gap as a second sample. When the pacing was only between
        samples, one important gap stayed open. A user who sees a failure and pushes
        Enter again immediately makes exactly the burst that repeaters penalize. After
        a penalty, each later trace of that node comes back empty.

        The dialog counts the traces of the run as the replies arrive. It is pushed for
        the duration of the run, and it is popped however the run ends: completion,
        failure, or abort. Its Abort connects directly to :meth:`cancel`, so the
        cancellation path is the same when Esc goes to the dialog or to the screen. An
        aborted run keeps each trace that it already stored.
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
        except Exception as exc:  # noqa: BLE001 - show it inline, and keep the screen open
            self._status = f"trace failed: {exc}"
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a spinner problem must never stop a trace
                pass
            self._session.pop(dialog)
            self._flight = None
            self._running = False
            self._progress = None
            self._session.invalidate()
            if self._run_placed and not (self.future is not None and self.future.done()):
                # The run placed on the boards: open the new-record dialog after the
                # tracing dialog closes. This is a separate task, because an aborted run
                # comes here already cancelled, and it cannot await a dialog itself. A
                # run whose screen already resolves (Esc during the run) does not open
                # it, because a dialog must never follow the user out of the screen.
                asyncio.ensure_future(self._announce_records())

    def _pace_remaining(self) -> float:
        """The seconds to wait before the next transmission can go out (0 when none)."""
        if self._last_tx is None or self._pace_s <= 0:
            return 0.0
        elapsed = asyncio.get_running_loop().time() - self._last_tx
        return max(0.0, self._pace_s - elapsed)

    async def _animate(self) -> None:
        """Advance the spinner and paint again at a steady interval, until cancelled."""
        while True:
            await asyncio.sleep(spinner_interval())
            self._spinner.tick()
            self._session.invalidate()

    def _on_trace(self, result: TraceResult, placed: Sequence[str] = ()) -> None:
        """Append the trace that arrived, keep its records, show it, and paint again.

        Args:
            result: The trace that arrived.
            placed: The trophy case disciplines that this walk placed in, as short
                ``"Title — score"`` labels (empty when it set no record). They go into
                the tally of the run, which one dialog announces when the run ends
                (refer to :meth:`_announce_records`). A later improvement replaces the
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
        """Open the new-record dialog for all the records that the ended run placed.

        There is one dialog for each run, however many samples scored. Each placed
        discipline has its own ``★ Title — score`` line. The dialog has the platform
        shape: *Trophy case* on the left, *Close* on the right and the default. Thus
        Enter dismisses the dialog, and Esc closes it. Trophy case opens the trophy case
        *over* this screen, the same as any other sub-view. Esc from the trophy case is
        one pop back to the trace that got the record, and ^W leaves the full excursion
        at once. (Before, this button resolved the full screen instead: it unwound the
        trace first and took the user to the main menu. That was a hand-built way out
        of a stack that had no other way out.) The re-entrancy guard also stays held
        while the trophy case is open. Thus a record that arrives while it is open
        cannot put a second dialog behind it.
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
        """Cancel a trace run in progress (MeshTerm keeps the traces that it stored)."""
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()

    # --- input -------------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Move the highlight of the action list, run the selected action, scroll, or leave.

        ↑/↓ move the highlight of the action list. After the last action, edge scroll
        continues them down the results. PgUp/PgDn/Home/End move the full screen, and
        the highlight stays where it was until an arrow brings it back. ←→ slide the
        paths of the hops that go past the edge (refer to :meth:`_hop_lines`).
        """
        if action in ("left", "right"):
            # The paths are not the highlight. A slide of the paths must not give the
            # page back to the highlight of the action list. If it does, the page jumps
            # up to the actions when the user reads a hop that edge scroll made
            # visible. The shift is clamped to the tail of the paths at render.
            self._pin_cursor = False
            step = _HSCROLL_STEP if action == "right" else -_HSCROLL_STEP
            self._hshift = max(0, min(self._hop_pan, self._hshift + step))
        elif action == "enter":
            self._commit_action()
        elif action == "up":
            # The two ends clamp instead of wrap. This is the rule of the app for a
            # highlight: a highlight that jumps from one end to the other moves the page
            # with it.
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
        """Run the action row at the highlight (the flows guard against re-entry)."""
        key = self._actions[self._index]
        if key == "trace":
            # A path walk has nothing to transmit until a path is available (composed,
            # or the last stored walk). It has no target, so an empty spec cannot use
            # device routing instead.
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
            # Start the composer with the route that the screen shows: the composed
            # spec if one is set, or else the plan that was resolved automatically. Do
            # not use the bare stored spec (empty at the first open). Thus Compose
            # always continues from the visible route. When the user confirms the
            # composer, the highlight moves to Trace (refer to _open_flow): the user
            # composes, then Enter walks the path.
            self._open_flow(self._compose_path, seed=self._effective_spec()[0], focus_trace=True)
        elif key == "reverse":
            self._reverse_path()
        elif key == "explore" and self._explore is not None:
            self._open_flow(self._explore)

    def _open_flow(
        self, flow: PathFlow, seed: str | None = None, focus_trace: bool = False
    ) -> None:
        """Open a flow that selects a path over the screen (one at a time, not during a trace).

        The composer, the scenario explorer, and the width picker all resolve the same
        way. They resolve to a new spec to adopt (``""`` gives the routing back to the
        device), or to ``None`` to keep the current path unchanged. (The sample-count
        flow always resolves ``None``.)

        Args:
            flow: The dialog flow to run with the current spec.
            seed: The spec given to the flow, when it must be different from the stored
                spec. The composer starts from the *visible* route (the automatic plan
                when no spec is set), so it never opens empty over a route that the
                screen shows. The adopt test still compares with the stored spec. Thus
                when the user confirms an automatic plan without a change, the plan
                becomes pinned, exactly as if the user adopted it by hand.
            focus_trace: Whether a confirmed flow (any resolution that is not
                ``None``, also the unchanged spec) moves the highlight of the action
                list to Trace. The composer uses this when it returns, so that Enter
                walks the path that the user built. When the user leaves with Esc,
                the highlight stays where it was.
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
                    # A different spec is a different measurement. The aggregates, the
                    # medians for each hop, and the log all belong to the old route.
                    # Thus the session starts again, as clean as a new screen.
                    self._path_spec = spec.strip()
                    self._traces.clear()
                    self._traces_rev += 1
                    self._status = ""
            finally:
                self._dialog_open = False
                self._session.invalidate()

        self._session.run_detached(run())

    def _reverse_path(self) -> None:
        """Turn the walked path end for end, and arm the screen again in the reverse direction.

        A path walk is the only asymmetric trace. ``us → a → b → c → us`` and
        ``us → c → b → a → us`` cross the same links in opposite directions, and radio
        links are rarely the same in the two directions. Thus a reversal of the hop
        order lets the user measure one route out and back. This method adopts the
        reversed spec exactly as any other change of path. The aggregates and the log
        described the forward run, so they clear. Trace stays idle until the user runs
        it (nothing starts automatically). Reverse again to walk the path in the
        original direction. The stored history keeps the two directions, so that the
        user can compare them side by side.

        This method does nothing when there is nothing to reverse: no path, a single
        hop (which is its own mirror), or a trace or a dialog that is in progress.
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
        """Render the route, the aggregates, the actions, the medians for each hop, and the log.

        The action rows are rendered one line at a time (not through one Rich group), so
        that the body line of the highlighted row is known exactly. :meth:`cursor_line`
        pins that line while the user moves through the actions.
        """
        if self._stats_memo is not None and self._stats_memo[0] == self._traces_rev:
            stats = self._stats_memo[1]
        else:
            stats = TraceStats.from_traces(self._target, self._traces)
            self._stats_memo = (self._traces_rev, stats)
        current = next((t for t in reversed(self._traces) if t.success), None)
        # The two path lanes break at hop boundaries under their own value column (the
        # rule of the app for labelled rows). A long walk never folds back to column
        # zero. It never separates a name from its hash, and never breaks a wire spec
        # in the middle of a hash.
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
            # Put a gap before the next group: after the build-path group (Explore in
            # target mode, Reverse in path mode, whichever is last) and after the
            # trace-settings group.
            if key in ("explore", "reverse", "samples"):
                lines.append("")
        # The results are page, not a list window of their own. Nothing highlights them.
        # Thus a private list window hides its rows from the arrows, and gives the frame
        # a page exactly as tall as the screen. Then edge scroll has nothing to scroll
        # (refer to ListWindow). The frame scrolls the results, and ↓ on the last action
        # continues into them.
        lines.extend(self._tail_lines(stats, current, width))
        self._scroll_total = max(1, len(lines))
        return lines

    def _tail_lines(self, stats: TraceStats, current: TraceResult | None, width: int) -> list[str]:
        """The results block below the actions: the medians for each hop, then the trace log.

        While the screen is idle, the block is cached for each traces revision. The
        block renders each stored trace again, and between two completions nothing in
        it changes. A *running* trace does not use the cache at all, because its log row
        has the live spinner glyph.
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
            # The key is the shift at which the rows were drawn, which _hop_lines can
            # clamp. Thus the next paint finds it.
            self._tail_memo = ((self._traces_rev, self._status, width, self._hshift), lines)
        return lines

    def cursor_line(self) -> int | None:
        """The highlighted action row while ↑/↓ are in use, or else free scroll.

        It is important to return ``None`` between uses of the arrows. The frame always
        keeps the cursor line visible. If this method did not return ``None``, PgDn could
        never scroll the action list off the screen, and the user could not read a long
        trace log.
        """
        return getattr(self, "_cursor", None) if self._pin_cursor else None

    def _action_text(self, key: str, selected: bool) -> Text:
        """One action row: the pointer, the glyph, and the label (with the current value)."""
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
        else:  # trace: the row on which the highlight starts
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
        """The value lines of the route lane: the path, wrapped at hops, then its source.

        The route is the live route after a reply arrived. If not, it is the plan that
        the next Trace walks. If not, it is the last stored walk. If not, it is a muted
        note. In all cases, it breaks at hop boundaries under the column of the lane: a
        name never separates from its hash, and a boomerang folds at its turn. The
        source (where an automatic plan came from, or when a previous walk ran) goes on
        the line below. It is not at the right edge of the route, because there it takes
        space from the route.

        The two ends are bare (``bare_self``). Each walk on this screen leaves our node
        and comes back to our node. The name of our node at the two ends only pushes the
        hops that *are* new information out of the lane. Named hops also do not show
        their hash. This lane answers the question *which nodes*. The hex after each
        name is already in the ``path`` lane below, exactly, at the width that goes on
        the air.

        Args:
            current: The newest successful trace of this session, if one exists.
            width: The full body width. The lines fit it, with the indent.

        Returns:
            The lines of the value. The first is bare (the label goes on it), and the
            continuations already have the indent of the lane.
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
        """The wire spec that the next Trace walks, and whether automatic resolution gave it.

        A composed or adopted spec has priority, unchanged. When there is none, the
        automatic resolver of the session gives the path that it forces now: the
        route that the device learned or the stored history in target mode, or the last
        successful stored walk in path mode. ``trace_once`` in :func:`_open_session`
        makes the same call. Thus the route on the screen is the route on the air.
        """
        if self._path_spec:
            return self._path_spec, False
        spec = self._auto_spec()
        return spec, bool(spec)

    def _spec_tokens(self) -> list[str]:
        """The hop hashes of the effective spec, in walk order (empty = a trace with no path)."""
        spec, _ = self._effective_spec()
        return [t.strip() for t in spec.split(",") if t.strip()]

    def _confirm_ineligible(self, edge: tuple[str, str]) -> None:
        """Open the amber not-a-trail confirmation before a walk that cannot set records.

        The walk that is about to start crosses ``edge`` two times in the same
        direction. Thus it is not a trail, and the trophy case will ignore its scores
        (the no-cheat rule, refer to
        :func:`~meshterm.services.records.first_repeated_edge`). It is still correct to
        walk the path, so the dialog only makes the disqualification clear. Cancel
        leaves. Trace (on the right and the default, the same as each committing verb)
        transmits anyway.
        """
        if self._dialog_open or self._running:
            return
        self._dialog_open = True

        async def run() -> None:
            try:
                prompt = Text("This walk crosses ", style="warn")
                prompt.append_text(
                    # The hashes show at the width with which the screen addresses
                    # hops, the same as each other node that this session shows.
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
        """A preview of the route that the next Trace walks, or ``None`` if there is none.

        Renders the literal wire spec through the only path widget. It is the full walk,
        because the trace protocol has no separate field for the return path. In target
        mode, the spec is the symmetric boomerang (the outbound hops, the target, and
        those hops in mirror order). Its second half is dim, through the ``dim_from`` of
        the widget, and this means "you do not compose this part". When the line wraps,
        the dim half also gives the fold its natural seam (the turn). The spec of a path
        walk is the full route (composed by hand, or the last stored walk). Thus each
        hop renders in full colour, and only the automatic return to our node stays
        faint. The endpoints of our node are bare, as in :meth:`_route_value` (only the
        arrow). The hash of each hop is also not shown: the lane names nodes, and the
        spec below it is the exact hex.
        """
        spec, _ = self._effective_spec()
        tokens = [h.strip() for h in spec.split(",") if h.strip()]
        if not tokens:
            return None
        if self._mode == "target" and len(tokens) % 2 and tokens == tokens[::-1]:
            mid = len(tokens) // 2  # the outbound hops and the target are the first half
            outbound, return_leg = tokens[: mid + 1], tokens[mid + 1 :]
        else:
            outbound, return_leg = tokens, []
        return path_line(
            [None, *outbound, *return_leg, None],
            self._resolve,
            prefix_bytes=8,  # spec tokens are the addressed parts: light all of them
            self_name=self._device_label,
            show_hash=False,
            dim_from=1 + len(outbound),
            bare_self=True,
        )

    def _displayed_hop_count(self, current: TraceResult | None) -> int | None:
        """The number of nodes that the shown route goes through, without the endpoints.

        This method uses the same order of priority as the route line: the live route,
        or else the planned spec, or else the stored previous trace. It counts exactly
        the items that are drawn between the two ``us`` endpoints. (The last hop of a
        walked trace has no hash and *is* our node, so it does not count.)
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
        """The aggregate lanes of the session, with aligned labels (the path lane is separate)."""
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
        """The value lines of the ``path`` lane: the wire spec (or its note) and the hop count.

        A pinned spec is the exact string that the radio gets. It is drawn through the
        only path widget (:func:`_spec_line`), so it breaks after a comma, and does not
        divide a hash in two. The automatic and the "none" states stay prose. The hop
        count follows the last line. It goes to a line of its own only when it does not
        fit.

        Args:
            current: The newest successful trace of this session, if one exists. When
                a trace arrived, the count describes its walked route.
            width: The full body width. The lines fit it, with the indent.

        Returns:
            The lines of the value, with the same indent as the lines of
            :meth:`_route_value`.
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
        """The median SNR of each hop, with quality bars, in path order.

        Each hop shows ``n  origin → destination  +4.5 dB  meter``. The link is drawn
        through the only path widget, the same as the route lane above draws it. It
        shows names with no hash after them (a node with no name shows its hash, which
        is its only identity). Our end is the bare ``★``, and only the two ends of the
        route are square (refer to :meth:`_hop_path`).

        When each row fits fully, the hops are a table of four columns, with one line
        for each hop. When a row does not fit, each row uses two lines. All the rows are
        the same, so that the column of readings never jumps between lines while the
        user reads down it. The first line has the hop number and its path. The second
        line has the reading and its meter, flush right. A path that is still wider than
        its lane is cut on its own crack, and it slides under ←→ (:attr:`_hshift`, which
        all the rows share, and each row stops at its own tail). The hop number stays
        pinned.

        This method measures how far ←→ can slide (:attr:`_hop_pan`) and clamps the
        shift to that value. Thus the rows are drawn at the shift that the footer and the
        next key press agree on.
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
        # A slid line gives one cell to its left mark. Thus the tail of a path is
        # visible at ``cells - (lane - 1)``. This is the exact stop, the same as a
        # panning table uses.
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
        """The link of one hop, ``origin → destination``, as the route lane draws its nodes.

        A hop is one link of the walk. Thus only the two ends of the route are drawn
        square: the start of the first hop and the end of the last hop. Each other end
        is a node through which the route continues. It has the chevron (in the arrow
        form, a bare ``→``) that shows this.
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
        """The individual traces, newest first, with the spinner of a run in progress on top."""
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
        """One log line of a trace.

        The line has the number, the local time, the outcome, the hop count, the
        bottleneck SNR, and the RTT.
        """
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
    """Collapse a routing hash mode to the widest hop width that a trace can encode.

    Routing widths are ``mode + 1`` bytes, but the flags of a trace can encode only 1, 2,
    4, or 8 (refer to :func:`~meshterm.services.trace_runner.path_hash_flags`). The
    firmware matches by prefix, so a smaller width addresses the same nodes.

    Args:
        mode: The path-hash mode (``size - 1``). A negative value means unknown.

    Returns:
        The width for each hop in bytes (1, 2, 4, or 8).
    """
    size = max(mode + 1, 1)
    return max(s for s in (1, 2, 4, 8) if s <= size)


def _previous_outbound(previous: TraceResult | None, target_hash: str) -> tuple[str, ...] | None:
    """Extract the outbound repeaters from the last successful walk to a target.

    A trace in target mode walks the symmetric boomerang. Thus its stored hop hashes
    (the last hop, with no hash, is our node) are ``[out…, target, out reversed…]``:
    a palindrome of odd length whose middle entry is the target. When the stored walk
    has that shape, its first half is a route that the mesh already proved, ready to
    force again.

    Args:
        previous: The most recent successful stored trace, if one exists.
        target_hash: The full hex hash of the target, to make sure that the walk
            turned at this target (the hop hashes are prefixes of it).

    Returns:
        The outbound repeater hashes in order, from our node outward (empty = the
        target answered directly). ``None`` when there is no stored walk, or when it
        is not a boomerang that this function can recognize.
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
    """Extract the full walked route from the last successful stored path walk.

    A path walk has no destination to route to. But its stored spec is a route that
    the mesh already carried from end to end. Thus the previous walk, which the screen
    shows when it opens, is also a path that Trace can walk again immediately. The hop
    hashes are returned unchanged (the last hop, with no hash, is our node, and this
    function removes it). They were proved at the width at which they were
    transmitted, so they are not rendered again.

    Args:
        previous: The most recent stored path walk, if one exists.

    Returns:
        The walked hop hashes in transmit order. ``None`` when there is no stored
        walk, when it failed, or when it stored no hops that can be addressed.
    """
    if previous is None or not previous.success:
        return None
    tokens = tuple(h.node.lower() for h in previous.hops if h.node)
    return tokens or None


def _best_observed(topo: Any, target_hash: str) -> tuple[tuple[str, ...], str] | None:
    """The strongest outbound route to ``target_hash`` in the evidence, if the data has one.

    This is the observed-topology counterpart of the route that the firmware learned.
    The device often knows no route to a contact, because repeater contacts flood. In
    that case, the evidence of the recorder (traces, relay chains heard from other
    traffic, neighbour tables that MeshTerm got) may still give a route for which there
    is proof that it carried traffic. The ranking is done by
    :meth:`~meshterm.services.topology.MeshTopology.suggested`. This function returns
    the canonical outbound hops of the winner, with a short source line
    (``best observed · 3× · −7 dB``) for the route line and the summary. It returns
    ``None`` when no route is better than a direct attempt.

    Args:
        topo: The newly built :class:`~meshterm.services.topology.MeshTopology`.
        target_hash: The hex hash of the target (any width ≥ 1 byte).

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
    """The pathline of a scenario on one line: our node, the candidate hops, and the target.

    The line includes the full boomerang endpoints (not only the intermediate hops that
    a :class:`~meshterm.services.topology.PathScenario` stores). Thus the row shows the
    full route, and not a part that the user must complete in the mind. The line
    renders through the shared path widget, so each hop has its own key hue (a hop with
    no name has its hash, lit by prefix) instead of the old flat single colour. As in
    the route lane above it, our end is bare. Each candidate starts from our node, so
    its name is the same on each row, and the cells are for the candidates.

    A ``width`` budget *cuts* the line, and does not elide its middle
    (:func:`~meshterm.ui.pathline.cut_to`). The ``⋯`` elision exists to keep the two
    endpoints of a route. Here, the two endpoints are the same on each row by design:
    our ``★`` and the one target of the full screen. Thus an elision uses cells for what
    the user already knows, and takes them from the *front* of the candidates, which is
    the only part that is different. A cut also makes each row look the same as the
    highlighted row at shift zero. Thus when the highlight moves down the list, the row
    that it goes onto does not change (JP, 2026-08-10). ``None`` returns the full line, which is
    what the highlighted row slides under ``←→``.
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
    """The line that hangs below the pathline of a scenario: its length, source, and evidence.

    The hop count is first (:func:`~meshterm.ui.pathline.hops_atom`). The line above
    encodes this number but never states it, and it is the first thing on which the
    user compares one candidate route with another. The hop count also replaces the
    source tag of the *direct* attempt, which was the same word for the same reason (no
    repeaters). The device route keeps its own tag, because a route that the firmware
    learned is a claim about where the hops came from, not about how many there are.
    Observed candidates *are* their hop sequence, and have no tag. Then come the
    bottleneck SNR and the sample count that a trace can expect to measure, or
    ``unobserved`` when the evidence graph has no information about the route yet.
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
    """Open the live *Trace target* screen for ``target``, and run it until the user leaves.

    This is the symmetric feature: can I reach this node? Routes turn at the target and
    come back over the mirrored hops. *Explore paths* ranks candidate outbound legs from
    the observed evidence.

    Args:
        ctx: The shared application context (it must run the interactive TUI surface).
        target: The trace destination (a contact name or a key prefix).
        initial_spec: A forced path on which the screen opens armed (the full symmetric
            boomerang, hex hops separated by commas), instead of idle on the route that
            was resolved automatically. The Node detail screen uses it to give its
            suggested best path, so that the trace opens ready to walk that path. Empty
            (the default) opens on the automatic route (learned by the device, best
            observed, last trace, or direct, refer to :func:`_open_session`).

    Returns:
        The number of traces that ran while the screen was open.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    return await _open_session(ctx, target, initial_spec=initial_spec)


async def open_trace_path(ctx: AppContext, spec: str = "") -> int:
    """Open the live *Trace path* screen, and run it until the user leaves.

    This is the feature with a route by hand: how far can a route that I build go? It
    has no target. The user composes the full walk one hop at a time, and the walk must
    only end at a node that our node can hear. Thus there is no target picker, no device
    routing, and no scenario explorer. The traces are stored under
    :data:`~meshterm.core.models.PATH_TRACE_TARGET`.

    Args:
        ctx: The shared application context (it must run the interactive TUI surface).
        spec: A forced path on which the screen opens armed (hex hops separated by
            commas), instead of idle on the last stored walk. *Trace this path* in the
            trophy case uses it to open the route of a record again. Empty (the
            default) opens on the history.

    Returns:
        The number of traces that ran while the screen was open.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    return await _open_session(ctx, None, initial_spec=spec)


async def _open_session(ctx: AppContext, target: str | None, *, initial_spec: str = "") -> int:
    """Connect and run one live trace session (the two features share this code).

    This function connects the screen to the radio, the database, and the
    observed-topology services:

    * Each trace opens its own ``runs`` row and is stored under it (the same shape that
      a scripted ``meshterm trace`` writes).
    * The composer and scenario flows build a new
      :class:`~meshterm.services.topology.MeshTopology` from the stored evidence each
      time they open. Thus the suggestions always show the latest received traffic.

    Nothing transmits until the user asks. The screen opens idle, and the most recent
    stored trace is the first value of its route line.

    Args:
        ctx: The shared application context (it must run the interactive TUI surface).
        target: The trace destination (a contact name or a key prefix), or ``None`` for
            a path walk with no target.
        initial_spec: A forced path (hex hops separated by commas) on which the path
            screen opens armed, instead of idle on the history. *Trace this path* in the
            trophy case gives it. Ignored in target mode (only path walks arm on a bare
            spec).

    Returns:
        The number of traces that ran while the screen was open.

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

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - the caller is only in the menu
        raise RuntimeError("the live trace screen is only available in the menu")
    session = ctx.ui.session

    mode = "target" if target is not None else "path"
    record_target = target if target is not None else PATH_TRACE_TARGET

    device = await ctx.device()
    # The contacts and the self-info come from the session cache (refer to DeviceState).
    # The contacts table is a slow round trip on a busy node, and the user opens this
    # screen often. The screen still keeps the live ``device`` for the traces that it
    # runs.
    contacts = await ctx.devstate.contacts()
    resolve = trace_runner.make_node_resolver(contacts)
    self_info = await ctx.devstate.self_info()

    device_label = str(self_info.get("name") or LOCAL_DEVICE_LABEL)
    device_hash = str(self_info.get("public_key") or "") or None

    # The width for each hop at which composed and scenario specs are made: the routing
    # width of the region, collapsed to what a trace can encode. If it is unknown (old
    # firmware), it is 1 byte. That is the protocol default, and all stored evidence
    # uses it. The user can change it for the session with the *Path width* action of
    # the screen (refer to pick_width).
    try:
        width_bytes = collapse_trace_width(int(await ctx.devstate.path_hash_mode()))
    except Exception:  # noqa: BLE001 - an optional read. The 1-byte default always works.
        width_bytes = 1
    device_width = width_bytes  # kept, so that the width dialog can mark the default

    # How many traces one Trace action runs. It is session state, the same as the
    # width. The *Sample count* action of the screen changes it (refer to pick_samples).
    sample_count = 1

    # The target as a hash that can be addressed: the full key of a known contact, or
    # the typed hex prefix. An unknown target that is not hex can still be traced with
    # device routing. But to compose or explore, a destination hash is necessary on
    # which to pin the path. A path walk has no target: each target_* stays None, and
    # the composer runs with a route by hand.
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
        """Build the evidence graph from all the data that is stored now and on the device."""
        return build_topology(
            self_id=device_hash or "local",
            contacts=contacts,
            trace_paths=ctx.repo.trace_paths(),
            packet_paths=ctx.repo.packet_paths(),
            neighbour_links=ctx.repo.neighbour_links(),
        )

    previous = ctx.repo.latest_trace(record_target)

    # What an *auto* trace (with no composed path) forces on the air, and where that
    # route came from. A trace replies only when its destination is the last outbound
    # hop of the path and nothing reflects it back home. Thus auto must always give a
    # full boomerang. But the device rarely has one to give. The firmware learns the
    # ``out_path`` of a contact only from addressed traffic in the two directions
    # (checked on hardware: each repeater contact reported ``out_path_len`` -1,
    # flood). Thus a trace with device routing to a node farther than a direct
    # neighbour goes out with no repeaters, and fails. The order of priority: the route
    # that the device learned, when it really has one. If not, the outbound leg of the
    # last successful stored walk (the route that the screen shows). If not, the bare
    # destination: a direct attempt, which says clearly that it is one.
    device_route: tuple[str, ...] | None = None
    if target_contact is not None and target_contact.route_hops is not None:
        # To make hop hashes canonical, only the contact index is necessary. Thus an
        # *empty* graph does it. The full evidence build of fresh_topology() (stored
        # traces, packet paths, neighbour tables) is not necessary, because this
        # branch does not read it.
        ident = MeshTopology(device_hash or "local", contacts)
        device_route = tuple(ident.canonical(h) or h for h in target_contact.route_hops)
    auto_hops: tuple[str, ...] | None = None
    auto_source = ""
    if target_hash is not None:
        if device_route is not None:
            auto_hops, auto_source = device_route, "device route"
        elif (observed := _best_observed(fresh_topology(), target_hash)) is not None:
            # The firmware has no route, but the evidence of the recorder gives one: the
            # strongest observed path (the ranking uses sample counts and SNR). Because
            # of this, Trace target *suggests* a route from the data. It does not use a
            # flood with no path by default, which fails after the first neighbour.
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
        """The spec that auto forces at the current width of the session (``""`` = no path).

        The auto spec of a path walk is the stored hops, unchanged. They were proved at
        the width at which they were transmitted, so they are not rendered again.
        """
        if auto_hops is None:
            return ""
        if target_hash is None:
            return ",".join(auto_hops)
        return render_forced_spec(auto_hops, target_hash, width_bytes)

    async def unaddressable() -> None:
        """Explain why the path features of target mode must have a target that resolves."""
        await session.message_dialog(
            Text(
                f"{target!r} isn't a known contact or a hex key prefix, so a forced "
                "path can't end at it. Trace it device-routed, or pick a contact.",
            ),
            title="No destination hash",
        )

    async def fetch_neighbours_via(repeater: Contact, repeater_id: str) -> bool:
        """Log in to a repeater, get its neighbour table, and store the snapshot.

        The fetch action of the composer comes here. The password comes from the admin
        store, or from a prompt one time, which MeshTerm keeps when the login succeeds
        (the tx-optimize convention). Then the login and the fetch run under a floating
        spinner dialog with Abort. Each outcome closes its own ``runs`` row. A refused
        login also forgets the stored password, so the next attempt asks again.

        Args:
            repeater: The repeater contact to query (it has the public key).
            repeater_id: Its canonical id, the key under which the snapshot is stored.

        Returns:
            ``True`` when new neighbour links were stored (the caller must build the
            topology again). ``False`` on a cancel, a failure, or an empty table.
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
        dialog.show_last = False  # a fetch has no stream of replies to show
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
        except Exception as exc:  # noqa: BLE001 - show it in a dialog, and continue to compose
            error = exc
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a spinner problem must never stop a fetch
                pass
            session.pop(dialog)
        if aborted:
            ctx.repo.finish_run(run_id, "error", {"error": "aborted"})
            return False
        if error is not None:
            # The credential was already handled where the outcome was known (refer to
            # ``work`` and AdminStore.record). Thus an exception here is only a message
            # to show.
            ctx.repo.finish_run(run_id, "error", {"error": str(error) or type(error).__name__})
            await session.message_dialog(Text(str(error), style="err"), title="Fetch neighbours")
            return False
        ctx.repo.record_neighbours(run_id, repeater_id, entries)
        ctx.repo.finish_run(run_id, "ok", {"repeater": repeater.name, "neighbours": len(entries)})
        if not entries:
            # Checked on real firmware: an empty table is a normal answer (repeaters
            # forget neighbours when they reboot, and learn them again from adverts).
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
        """Open the composer, which starts with the hops of the current spec.

        This function runs the composer in a loop. A :class:`FetchNeighbours` resolution
        does the fetch, builds the topology again with the new evidence, and opens the
        composer again exactly where the user was (the same hops and insertion slot,
        with refreshed suggestions).
        """
        if mode == "target" and target_hash is None:
            await unaddressable()
            return None
        topo = fresh_topology()
        target_id = (
            (topo.canonical(target_hash) or target_hash[:12]) if target_hash is not None else None
        )
        # Start again from the current spec. A spec in target mode is the symmetric
        # boomerang (hops, target, mirror): start with only the outbound hops, and let
        # the composer make the rest again. A path walk is its spec, unchanged. Tokens
        # that match no contact are kept as they are, and not removed. An adopted route
        # must stay when the composer opens again, also where the evidence graph has no
        # data.
        tokens = [p.strip() for p in current.split(",") if p.strip()]
        ids = [topo.canonical(h) or h for h in tokens]
        if mode == "path":
            seed = ids
        elif ids and len(ids) % 2 == 1 and ids == ids[::-1]:
            seed = ids[: len(ids) // 2]
        elif target_id in ids:
            seed = ids[: ids.index(target_id)]  # an old walk by hand: keep the outbound
        else:
            seed = ids
        seed_cursor: int | None = None  # the first open puts the insertion slot at the end
        while True:
            # The nodes that can give their neighbour table: repeater contacts with a
            # public key for the login (our node has no new information for us).
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
                resolve=resolve,  # name a hop that the topology left ambiguous, as we do
            )
            result = await session.run_screen(screen)
            if result is CANCEL:
                return None
            if isinstance(result, FetchNeighbours):
                seed = screen.hops  # continue from the same point after the fetch
                seed_cursor = screen.cursor
                repeater = fetchable.get(result.node)
                if repeater is not None and await fetch_neighbours_via(repeater, result.node):
                    topo = fresh_topology()  # add the new reports to the suggestions
                continue
            return result

    async def pick_width(current: str) -> str | None:
        """Open the path-hash width picker, and render the current spec again to match.

        Each row shows a key with the addressed part lit at that width. Thus the choice
        means "this much of each key goes on the air". The selected width applies to
        each spec that the composer and the explorer make after it. A current forced
        path is rendered again immediately. The hops get their full width back through
        their canonical hashes where they are known, then they are collapsed to one
        width. (If only a narrower form of a hop is known, the full spec stays at the
        width that this hop can supply.)

        Returns:
            The spec, rendered again. ``None`` when the user cancels, when nothing
            changes, or when there is no forced path to render again (the width itself
            still stays).
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
        """Open the sample-count picker: how many traces one Trace action runs.

        Runs of more than one trace are paced by the cooldown preference between the
        transmissions. Thus a choice of 8 is a deliberate sample session, never a burst.
        Always resolves ``None``, because the count is session state that the screen
        reads, not a spec.
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
        """The fixed head of a probed candidate: rank, verdict, bottleneck SNR, round trip.

        This function is separate from :func:`outcome_title`, so that the row can
        *measure* the block that it pins out of the ←→ scroll
        (:attr:`~meshterm.ui.tui.select.Choice.hscroll_from`), and not guess it. These
        lanes are the only reason why the list is ranked. If they slide off to show the
        tail of a spec of ten hops, the row loses its identity and gets nothing, because
        the columns are the part that already fits.
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
        """Measure each candidate (one trace for each) under a dialog that can abort.

        One ``runs`` row covers the full sweep. Each trace and each candidate aggregate
        is stored under it. Thus an aborted probe still keeps all that it measured. Each
        candidate gets exactly one transmission (a probe compares, it does not take
        samples). Returns ``None`` when aborted.
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
        dialog.on_abort = sweep.cancel  # the task exists only now. Connect the button again.

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
        except Exception as exc:  # noqa: BLE001 - show it in a dialog, and keep the screen
            error = exc
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a spinner problem must never stop a probe
                pass
            session.pop(dialog)
        if aborted:
            # All that was measured before the abort is already stored. The run row
            # only stores the fact that the sweep did not finish.
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
        """The scenario flow: examine the ranked candidate routes, adopt one, or probe all."""
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
                    # The title knows the width (refer to Choice.title). A long
                    # candidate is cut to the terminal, with a crack on its own chip,
                    # exactly as the highlighted row is at shift zero. Then the
                    # highlighted row keeps its natural length and slides under ←→. The
                    # row is not drawn again in a different form when the highlight
                    # goes onto it.
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
                hscroll=True,  # a long candidate row slides under ←→, and is not truncated
            )
        )
        if picked is CANCEL or picked is None:  # Esc
            return None
        if picked[0] == "use":
            return picked[1].spec(target_hash, width_bytes)

        # Probe all: measure each different spec. (Scenarios can collapse to the same
        # spec after they are truncated to the trace width, and the radio must not walk
        # one spec two times.)
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
                # ←→ show the tail of a long spec. The ranking lanes before it stay
                # pinned (each row declares their width, refer to outcome_lanes).
                hscroll=True,
            )
        )
        if adopted is CANCEL or adopted is None:
            return None
        return adopted.candidate.spec

    # -- trophy case: score each walk that comes back home ------------------------------
    # A trace is a walk: it starts and ends at our node (a Trace path route directly,
    # and a Trace target boomerang the same). Thus each successful reply gets a score
    # in each trophy case discipline (``services.records.CATEGORIES``, seven now), and
    # MeshTerm offers it to their boards.

    def _as_float(value) -> float | None:  # noqa: ANN001
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    _lat, _lon = _as_float(self_info.get("adv_lat")), _as_float(self_info.get("adv_lon"))
    self_pos = (_lat, _lon) if _lat is not None and _lon is not None and (_lat or _lon) else None

    def build_positions(topo: MeshTopology) -> dict[str, tuple[float, float]]:
        """The node positions by canonical id.

        The advert history comes first, and the contacts are put over it.
        """
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
        """Score a walk that came home, offer it to each board, and return the placed boards.

        Returns the placed disciplines as short ``"Title — score"`` labels for the inline
        note of the screen. This function is defensive by design, because a scoring
        problem must never stop a trace. Thus a failure gives no records, and the error
        does not propagate.
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
        except Exception:  # noqa: BLE001 - the scoring must never stop a trace
            return []

    async def trace_once(
        path_spec: str, on_trace: Callable[[TraceResult, Sequence[str]], None]
    ) -> None:
        """Run one stored trace: one transmission, with its own ``runs`` row.

        The screen calls this function one time for each sample. The screen paces runs
        of more than one trace, so the repeaters never get a burst, whatever count the
        user selected. A walk that comes home gets a score in the trophy case. The
        records that it set go to the screen with the result.
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
            # A cancelled or failed trace still closes its run row, so that no
            # ``running`` orphan stays.
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
        # A spec from the caller arms the two modes: the stored route of a path walk, or
        # the suggested best path that the Node detail screen gives to a target trace. In
        # target mode, it arms only when the target can be addressed, because a forced
        # path must end at a real hash.
        initial_spec=initial_spec if (mode == "path" or target_hash is not None) else "",
        open_trophy_case=_trophy_case_opener(ctx),
    )
    try:
        await session.run_screen(screen)
    finally:
        screen.cancel()
    return screen._total_traces
