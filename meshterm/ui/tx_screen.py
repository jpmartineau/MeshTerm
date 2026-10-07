# SPDX-License-Identifier: Apache-2.0
"""The live TX-optimization screen: select the link, arm the sweep, and watch the levels arrive.

This is the interactive face of the ``tx-optimize`` tool. The scripted CLI keeps its
one-shot table and HTML chart. The screen is built again on the armed-but-idle pattern of
the trace screen. One dialog with steps in the tool selects the *link*. First it asks for
the admin node whose transmit power the sweep tunes, then for the target whose reception
the sweep optimizes. The screen then opens idle over the main menu. The route, the sweep
parameters, and the login state are across the top. A list of actions controls
everything, and nothing transmits until the user commits Sweep.

* **Route** opens the path composer of the trace screen, which goes hop by hop and is
  pinned at the tuned node. The user composes how the measurement reaches that node, and
  each step is suggested from the links that MeshTerm observed. The code adds the target
  hop automatically. The default is the direct shot.
* **Range / Step / Samples** float small dialogs that adjust the sweep: the TX range, the
  spacing of the coarse grid, and the traces that are measured for each level. The Sweep
  row always shows the resulting worst-case number of transmissions. Thus the cost of a
  commit is on the screen before it occurs (each run is paced, like the sampling of the
  trace tool).
* **Sweep** first logs in, if it must. The password prompt floats only if no password is
  remembered, and MeshTerm remembers a password that works for the next time. Then it
  runs the optimizer under a floating in-flight dialog that has Abort. The phases are
  coarse, refine, and verify. Each measured level goes into the bar chart behind the
  dialog, and the current best level has a star. If the user aborts in the middle of the
  sweep, the code restores the original power of the node.
* **Apply winner** is added to the actions when a sweep has found a winner (the highlight
  moves to it). It is the same offer as the dialog after the sweep, for the case that the
  user first declined it.

Each sweep opens one ``runs`` row and stores its levels and traces in the same way as a
scripted run records them. A finished sweep leaves the screen armed. The user can adjust
the range and sweep again. A new sweep is a new measurement, so the chart clears when it
starts.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from ..core.connection import REMOTE_TX_MAX, REMOTE_TX_MIN
from ..core.models import Contact, LoginResult, TraceResult, TxLevelResult, TxOptResult
from ..services import trace_runner, tx_optimizer
from ..services.topology import build_topology, render_custom_spec
from .menus import icon_lane, icon_mark
from .pathline import SELF_GLYPH, PathHop, PathLine, cut_to, path_line
from .theme import name_style, snr_style
from .trace_screen import TracingDialog, collapse_trace_width, snr_bar
from .tui.render import render_lines, render_to_ansi
from .tui.screen import Screen
from .tui.spinner import Spinner, spinner_interval
from .widgets import NodeResolver

if TYPE_CHECKING:
    from ..context import AppContext

#: The words for the user for each optimizer phase that is reported through ``on_phase``.
_PHASE_LABELS = {
    "coarse": "coarse sweep",
    "refine": "refining around the leader",
    "verify": "verifying the leader",
}

#: The coarse grid spacings that the Step dialog offers. 1 measures each level, so no refine
#: pass is necessary. A wider grid needs fewer transmissions, but it needs a refine pass
#: later.
STEP_CHOICES = (1, 2, 3, 4, 5)

#: The numbers of samples for each level that the Samples dialog offers (the ladder of the
#: trace tool).
SAMPLE_CHOICES = (1, 2, 3, 5, 8)


#: Each mark that the action rows of the sweep start with. The list is there to *measure*
#: the icon column, so that each label starts in the same place. Also, a platform that
#: draws no icon lane (refer to :func:`~meshterm.ui.menus.command_icon`) makes the column
#: zero cells wide.
_ACTION_ICONS = ("✎", "⚙", "#", "▶", "★")


def _icon_lane() -> int:
    """The icon column of the action rows, in cells (the marks of this screen, measured once)."""
    return icon_lane(_ACTION_ICONS)


def _icon(text: Text, icon: str, style: str) -> None:
    """Append the mark of an action row in the icon column, with its trailing space."""
    text.append_text(icon_mark(icon, style, _icon_lane()))


class TxSweepScreen(Screen):
    """A full-screen session of a TX-power sweep. It is armed, but idle until the user starts it.

    ↑/↓ move the highlight over the action rows, and Enter commits the selected row. The
    highlight opens on Sweep, so a plain Enter sweeps. PgUp/PgDn/Home/End scroll the body.
    Esc leaves the screen and cancels any sweep that is in flight (the unwind of the
    optimizer restores the original power of the node).

    The screen renders the state and sends the keys to the correct code. The owning
    session (refer to :func:`open_tx_optimize`) gives it the flows: the route composer, the
    parameter dialogs, and the sweep runner. The session sends the measurements back
    through :meth:`on_phase`, :meth:`on_level`, :meth:`complete`, and :meth:`fail`.
    """

    floating = False

    def __init__(
        self,
        *,
        admin_label: str,
        target_label: str,
        device_label: str,
        device_hash: str | None,
        resolve: NodeResolver,
        session: Any,
        tx_min: int,
        tx_max: int,
        admin_key: str | None = None,
        target_key: str | None = None,
        step: int = 3,
        samples: int = 3,
        run_sweep: Callable[[], None] = lambda: None,
        apply_winner: Callable[[], None] = lambda: None,
        compose_route: Callable[[], None] = lambda: None,
        pick_range: Callable[[], None] = lambda: None,
        pick_step: Callable[[], None] = lambda: None,
        pick_samples: Callable[[], None] = lambda: None,
    ) -> None:
        """Create the sweep screen. Nothing transmits until the user commits Sweep.

        Args:
            admin_label: The display name of the node that the sweep tunes.
            target_label: The display name of the node at which the SNR is measured.
            device_label: The name of our node, which starts the route line.
            device_hash: The public key of our node, so that the ends of the route have a
                hash.
            resolve: Maps the raw hash of a hop to the name of a contact, when it is known.
            admin_key: The key or hash of the tuned node, so that its name has its own hue.
            target_key: The key or hash of the target, for the same reason.
            session: The running TUI session (for paints).
            tx_min: The initial low end of the sweep range.
            tx_max: The initial high end of the sweep range.
            step: The initial spacing of the coarse grid.
            samples: The initial number of traces that are measured for each level.
            run_sweep: Starts one sweep. The owner guards it, and it does nothing while a
                sweep is in flight.
            apply_winner: Offers the apply dialog again for the winner of a completed
                sweep.
            compose_route: Opens the route composer flow over this screen.
            pick_range: Floats the TX-range dialog.
            pick_step: Floats the grid-spacing dialog.
            pick_samples: Floats the dialog for the number of samples for each level.
        """
        super().__init__()
        self.title = f"TX optimize — {admin_label} → {target_label}"
        self._admin_label = admin_label
        self._target_label = target_label
        self._admin_key = admin_key
        self._target_key = target_key
        self._device_label = device_label
        self._device_hash = device_hash
        self._resolve = resolve
        self._session = session
        self._run_sweep = run_sweep
        self._apply_winner = apply_winner
        self._compose_route = compose_route
        self._pick_range = pick_range
        self._pick_step = pick_step
        self._pick_samples = pick_samples

        #: The sweep parameters. The dialogs of the owner change them between sweeps.
        self.tx_min = tx_min
        self.tx_max = tx_max
        self.step = step
        self.samples = samples
        #: The hops that the measurement crosses *before* the tuned node (output of the
        #: composer, at the spec width of the session). Empty means the direct shot.
        self.route_hops: list[str] = []
        #: One line that describes the login state (remembered, will ask, or logged in). The
        #: owner maintains it, and the header shows it.
        self.login_text = ""

        self.running = False
        self._dialog_open = False
        self._phase: str | None = None
        self._done = 0
        self._total = 0
        self._levels: dict[int, TxLevelResult] = {}
        self._best_tx: int | None = None
        self._result: TxOptResult | None = None
        self._applied = False
        self._error: str | None = None
        self._worker: asyncio.Task | None = None
        self._index = self._actions.index("sweep")
        self._pin_cursor = False  # pin the view only while the user uses ↑/↓

    # --- state -------------------------------------------------------------------

    @property
    def _actions(self) -> tuple[str, ...]:
        """The action rows in display order. Apply is added when there is a winner."""
        rows = ["route", "range", "step", "samples", "sweep"]
        if self._can_apply():
            rows.append("apply")
        return tuple(rows)

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The footer keys. They change when a sweep is in flight."""
        if self.running:
            return "sweeping… · PgUp/PgDn scroll · Esc back"
        return "↑↓ actions · Enter run · PgUp/PgDn scroll · Esc back"

    def _can_apply(self) -> bool:
        """Whether Apply is offered: the sweep finished, it has a winner, and it is not applied."""
        return self._result is not None and self._result.best_snr is not None and not self._applied

    def estimated_traces(self) -> int:
        """The worst-case number of transmissions that one commit of Sweep can run.

        The number is the levels of the coarse grid, plus up to ``2 · (step − 1)`` integer
        fill-ins of the refine pass around the winner, plus the verify measurement again.
        Each is measured with the chosen number of samples for each level. The real number
        is usually lower, because the refine pass stops at the ends of the range and skips
        levels that are already measured.
        """
        coarse = len(tx_optimizer.coarse_levels(self.tx_min, self.tx_max, self.step))
        refine = max(0, 2 * (self.step - 1))
        return (coarse + refine + 1) * self.samples

    # --- owner feed ----------------------------------------------------------------

    def sweep_started(self, worker: asyncio.Task) -> None:
        """Take a sweep that just started. It is a new measurement, so clear the old evidence."""
        self.running = True
        self._worker = worker
        self._phase = None
        self._done = self._total = 0
        self._levels.clear()
        self._best_tx = None
        self._result = None
        self._applied = False
        self._error = None
        self._session.invalidate()

    def sweep_finished(self) -> None:
        """Mark that the sweep is not in flight any more, however it ended."""
        self.running = False
        self._worker = None
        self._session.invalidate()

    def on_phase(self, phase: str) -> None:
        """Record that the optimizer enters a search phase (refer to ``tx_optimizer.PHASES``)."""
        self._phase = phase
        self._session.invalidate()

    def on_level(self, done: int, total: int, level: TxLevelResult) -> None:
        """Record one measured level. It replaces an earlier pass at the same power."""
        self._done, self._total = done, total
        self._levels[level.tx_power] = level
        self._best_tx = tx_optimizer.select_best(list(self._levels.values())).tx_power
        self._session.invalidate()

    def complete(self, result: TxOptResult) -> None:
        """Take the selection of the finished sweep and move the highlight to Apply."""
        self._result = result
        self._best_tx = result.best_tx
        if self._can_apply():
            self._index = self._actions.index("apply")
        self._session.invalidate()

    def fail(self, error: str) -> None:
        """Mark the sweep as failed. The levels that already arrived stay on the screen."""
        self._error = error
        self._session.invalidate()

    def mark_applied(self) -> None:
        """Record that the code wrote the winner to the admin node."""
        self._applied = True
        self._index = min(self._index, len(self._actions) - 1)
        self._session.invalidate()

    def phase_label(self) -> str:
        """The status line of the in-flight dialog: the phase and the progress of the levels."""
        label = _PHASE_LABELS.get(self._phase or "", "starting…")
        if self._total:
            return f"{label} · level {self._done}/{self._total}"
        return label

    def cancel(self) -> None:
        """Cancel any sweep that is in flight. The unwind of the optimizer restores the power."""
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()

    # --- input -------------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Move the highlight of the actions, commit the selected action, scroll, or leave."""
        if action == "enter":
            self._commit_action()
        elif action == "up":
            # Both ends clamp and do not wrap. This is the rule of the whole app for the
            # highlight of a row. A highlight that jumps from one end to the other takes
            # the page of results with it.
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
        """Run the action row under the highlight. The flows guard against a second entry."""
        actions = self._actions
        key = actions[min(self._index, len(actions) - 1)]
        if key == "sweep":
            if not self.running:
                self._run_sweep()
        elif key == "apply":
            if self._can_apply():
                self._apply_winner()
        elif key == "route":
            self._open_flow(self._compose_route)
        elif key == "range":
            self._open_flow(self._pick_range)
        elif key == "step":
            self._open_flow(self._pick_step)
        elif key == "samples":
            self._open_flow(self._pick_samples)

    def _open_flow(self, flow: Callable[[], None]) -> None:
        """Float a parameter flow over the screen. Only one at a time, and never during a sweep."""
        if self._dialog_open or self.running:
            return
        self._dialog_open = True

        async def run() -> None:
            try:
                await flow()  # type: ignore[misc]  # the flows of the owner are async closures
            finally:
                self._dialog_open = False
                self._session.invalidate()

        asyncio.ensure_future(run())

    # --- rendering -----------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render the header, the list of actions, the chart of levels, and the outcome."""
        lines = self._header_lines(width)
        lines.append("")
        self._cursor: int | None = None
        actions = self._actions
        self._index = min(self._index, len(actions) - 1)
        for i, key in enumerate(actions):
            selected = i == self._index
            text = self._action_text(key, selected)
            text.no_wrap = True
            # The route row has a path line, so the code cuts it and does not truncate it.
            # A chip that goes past the end of the row cracks. Each other row keeps the
            # ellipsis.
            text = cut_to(text, width)
            text.no_wrap = True
            if selected:
                self._cursor = len(lines)
            lines.append(render_to_ansi(text, width))
            if key == "samples":
                lines.append("")  # separate the sweep group from the parameters
        tail: list[RenderableType] = []
        if self.running or self._levels:
            tail += [Text(), Text("Levels", style="accent"), self._levels_table()]
        outcome = self._outcome()
        if outcome is not None:
            tail += [Text(), outcome]
        lines.extend(render_lines(Group(*tail), width))
        self._scroll_total = max(1, len(lines))
        return lines

    def cursor_line(self) -> int | None:
        """The line of the highlighted action row while ↑/↓ are in use. Else the page scrolls."""
        return getattr(self, "_cursor", None) if self._pin_cursor else None

    def _action_text(self, key: str, selected: bool) -> Text:
        """One action row: the pointer, the glyph, and the label with the current value."""
        text = Text("❯ " if selected else "  ", style="cursor" if selected else "")
        if key == "route":
            _icon(text, "✎", "brand")
            if self.route_hops:
                # Only the relays that the user composed. The sweep always starts from us and
                # always arrives at the repeater that it tunes, and the user did not select
                # either of them here as a hop. Thus both ends are drawn open. The chevrons
                # say that the route continues past them. This is exactly what ``via`` says
                # in the word before them.
                text.append("Route — via ")
                text.append_text(
                    path_line(
                        list(self.route_hops),
                        self._resolve,
                        from_origin=False,
                        to_destination=False,
                    ).text()
                )
            else:
                text.append(f"Route — direct to {self._admin_label}")
        elif key == "range":
            _icon(text, "⚙", "accent")
            text.append(f"Range — TX {self.tx_min}–{self.tx_max}")
        elif key == "step":
            _icon(text, "⚙", "accent")
            text.append(
                f"Step — every {_nth(self.step)} level, then refine"
                if self.step > 1
                else "Step — every level (no refine needed)"
            )
        elif key == "samples":
            _icon(text, "#", "accent")
            text.append(
                f"Samples — {self.samples} trace{'s' if self.samples != 1 else ''} per level"
            )
        elif key == "sweep":
            _icon(text, "▶", "ok")
            text.append(f"Sweep — up to {self.estimated_traces()} paced transmissions")
        else:  # apply: it is there only when there is a winner to set
            _icon(text, "★", "ok")
            best = self._result.best_tx if self._result is not None else "?"
            text.append(f"Apply winner — set TX {best} on {self._admin_label}")
        if selected:
            text.style = "cursor"
        return text

    def _header_lines(self, width: int) -> list[str]:
        """The header lanes: tuned link, measured route, sweep range, and login state.

        The route renders through the only path widget. It wraps at the boundaries of hops,
        under its own value column (the hanging-indent rule), and does not fold back to
        column zero. The tuned link above it has the same key hues as the route.
        """
        tuning = Text.assemble(
            ("tuning   ", "muted"),
            (self._admin_label, name_style(self._admin_label, self._admin_key)),
            (" → ", "muted"),
            (self._target_label, name_style(self._target_label, self._target_key)),
        )
        lines = [render_to_ansi(tuning, width, no_wrap=True)]
        route = self._route_line().wrapped(width, indent=9)
        first = Text("route    ", style="muted")
        first.append_text(route[0])
        lines.append(render_to_ansi(first, width, no_wrap=True))
        lines.extend(render_to_ansi(cont, width, no_wrap=True) for cont in route[1:])
        rest = Text("sweep    ", style="muted")
        rest.append(
            f"TX {self.tx_min}–{self.tx_max} · step {self.step} · "
            f"{self.samples} trace{'s' if self.samples != 1 else ''}/level"
        )
        if self.login_text:
            rest.append("\n")
            rest.append("login    ", style="muted")
            rest.append(self.login_text)
        if self._result is not None and self._result.original_tx is not None:
            rest.append("\n")
            rest.append("was      ", style="muted")
            rest.append(f"TX {self._result.original_tx}")
        lines.extend(render_lines(rest, width))
        return lines

    def _route_line(self) -> PathLine:
        """The walk of one measurement: out through the tuned link, and back as a mirror image.

        The outbound leg is us, the composed hops, the tuned node, and the target. The code
        draws it in full colour (each name in its own key hue). The return is the outbound
        leg mirrored back. It is the trace boomerang that the optimizer really sends. The
        code dims it, and it means "the user does not compose this".

        Both our ends use the ``★`` of the whole app and not our name. Each measurement
        that this screen makes leaves us and comes back to us. If the line spelled out our
        name two times, the lane would lose cells that the hops being tuned need. The trace
        route lane and the trophy card make the same choice.
        """
        hops: list[PathHop] = [PathHop(SELF_GLYPH, you=True)]
        for hop in self.route_hops:
            hops.append(PathHop(self._hop_name(hop), key=hop))
        hops.append(PathHop(self._admin_label, key=self._admin_key))
        hops.append(PathHop(self._target_label, key=self._target_key))
        hops.append(PathHop(self._admin_label, key=self._admin_key, dim=True))
        for hop in reversed(self.route_hops):
            hops.append(PathHop(self._hop_name(hop), key=hop, dim=True))
        hops.append(PathHop(SELF_GLYPH, you=True, dim=True))
        return PathLine(hops)

    def _hop_name(self, hop: str) -> str:
        """The name of a route hop when it is known, otherwise its raw hash."""
        named = self._resolve(hop)
        return named if named else hop

    def _levels_table(self) -> Table:
        """Each measured level as a bar chart, in ascending order of TX. The best has a star."""
        table = Table(box=None, padding=(0, 1, 0, 0), expand=False, header_style="muted")
        table.add_column("TX", justify="right", min_width=4)
        table.add_column("SNR AT TARGET")
        table.add_column("", justify="right")  # numeric SNR
        table.add_column("RELIABILITY", justify="right")
        table.add_column("TRACES", justify="right", style="muted")
        for level in sorted(self._levels.values(), key=lambda lv: lv.tx_power):
            is_best = level.tx_power == self._best_tx
            tx_cell = Text()
            tx_cell.append("★ " if is_best else "  ", style="ok")
            tx_cell.append(str(level.tx_power), style="brand" if is_best else "")
            snr = level.target_snr
            if snr is None:
                bar_cell: Text = Text("✗ no reply", style="err")
                snr_cell = Text("—", style="muted")
            else:
                bar_cell = snr_bar(snr)
                snr_cell = Text(f"{snr:+.1f} dB", style=snr_style(snr))
            rate = level.success_rate
            rate_style = "ok" if rate >= 1.0 else ("warn" if rate > 0 else "err")
            table.add_row(
                tx_cell,
                bar_cell,
                snr_cell,
                Text(f"{rate:.0%}", style=rate_style),
                f"{level.successes}/{level.samples}",
            )
        return table

    def _outcome(self) -> Text | None:
        """The verdict and the apply status of the completed sweep, or the error of the sweep."""
        if self._error is not None:
            return Text(f"✗ sweep failed: {self._error}", style="err")
        result = self._result
        if result is None:
            return None
        if result.best_snr is None:
            return Text.assemble(
                ("! ", "warn"),
                ("no traces reached ", ""),
                (self._target_label, "brand"),
                (" at any level — check the route, then sweep again.", ""),
            )
        outcome = Text.assemble(
            ("best     ", "muted"),
            (f"TX {result.best_tx}", "brand"),
            ("  ·  ", "muted"),
        )
        outcome.append(f"{result.best_snr:+.1f} dB", style=snr_style(result.best_snr))
        outcome.append(f" at {self._target_label}  ·  ", style="muted")
        outcome.append(f"{result.best_success_rate:.0%} reliable")
        outcome.append("\n")
        outcome.append("status   ", style="muted")
        if self._applied:
            outcome.append(f"✓ TX {result.best_tx} set on {self._admin_label}", style="ok")
        else:
            restored = (
                f" — the node keeps TX {result.original_tx}"
                if result.original_tx is not None
                else ""
            )
            outcome.append(f"not applied{restored}", style="muted")
        return outcome


def _nth(n: int) -> str:
    """Give the ordinal for the Step action row: ``2 → "2nd"``, ``3 → "3rd"``, and so on."""
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n if n < 20 else n % 10, "th")
    return f"{n}{suffix}"


def parse_tx_range(text: str) -> tuple[int, int] | None:
    """Parse a typed TX range, such as ``"12-28"`` or ``"12 28"``, into ``(low, high)``.

    The function limits the values to the range that the remote firmware can represent.
    It returns ``None`` if the text does not have exactly two numbers in order.
    """
    numbers = re.findall(r"\d+", text)
    if len(numbers) != 2:
        return None
    lo, hi = int(numbers[0]), int(numbers[1])
    if lo > hi:
        return None
    return max(REMOTE_TX_MIN, lo), min(REMOTE_TX_MAX, hi)


async def open_tx_optimize(
    ctx: AppContext,
    *,
    admin_node: Contact,
    target_label: str,
    target_hash: str,
) -> dict[str, Any]:
    """Run the live TX-optimization session for one tuned link.

    The dialog of the tool has selected the link and is already gone. This function
    connects the armed-idle screen to the radio, to the topology evidence, and to the
    database. It runs the screen until the user leaves it, and Esc goes to the main menu,
    because the sweep is the whole visit. The login occurs in the first commit of Sweep.
    If a password is remembered, the login is silent. Otherwise, one floating prompt asks
    for the password. MeshTerm remembers the password if the login works, and forgets it
    if the node rejects it (the convention of the trace composer). Each sweep opens its own
    ``runs`` row and records each level and trace under it, in the same way as a scripted
    run does.

    Args:
        ctx: The shared application context. It must run the interactive menu.
        admin_node: The contact that the sweep tunes (the hop before the target).
        target_label: The display name of the node at which the SNR is measured.
        target_hash: The hex hash of the target (the full key, or the prefix that the user
            typed).

    Returns:
        The recorded summary of the last sweep (empty if the user left before a sweep ran).

    Raises:
        RuntimeError: If the caller calls it outside the interactive menu (there is no
            full-screen session).
    """
    from .path_composer import AUTO_SPEC, PathComposerScreen
    from .surface import TuiUi
    from .tui import CANCEL, Choice, SelectScreen

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - the caller is only in the menu
        raise RuntimeError("the live TX sweep is only available in the menu")
    session = ctx.ui.session
    device = await ctx.device()
    # The contacts, the self-info, and the routing width come from the session cache (refer
    # to DeviceState). The code still keeps the live ``device`` for the own transmissions of
    # the sweep below.
    contacts = await ctx.devstate.contacts()
    resolve = trace_runner.make_node_resolver(contacts)
    self_info = await ctx.devstate.self_info()
    device_label = str(self_info.get("name") or "this node")
    device_hash = str(self_info.get("public_key") or "") or None

    try:
        width_bytes = collapse_trace_width(int(await ctx.devstate.path_hash_mode()))
    except Exception:  # noqa: BLE001 - an optional read. The default of 1 byte always works
        width_bytes = 1

    admin_hash = (admin_node.public_key or admin_node.key_prefix).lower().removeprefix("0x")
    target_hex = target_hash.lower().removeprefix("0x")

    logged_in = False
    summary: dict[str, Any] = {}

    def login_text() -> str:
        if logged_in:
            return f"✓ logged in to {admin_node.name}"
        if ctx.admin_store.get(admin_node) is not None:
            return "password remembered — logs in at the first sweep"
        return "no saved password — asks at the first sweep"

    screen = TxSweepScreen(
        admin_label=admin_node.name,
        target_label=target_label,
        device_label=device_label,
        device_hash=device_hash,
        resolve=resolve,
        session=session,
        tx_min=ctx.preferences.tx_opt_min,
        tx_max=ctx.preferences.tx_opt_max,
        admin_key=admin_hash,
        target_key=target_hex,
    )
    screen.login_text = login_text()

    def spec() -> str:
        """The one-way wire spec of the next sweep: composed hops, tuned node, and target."""
        return render_custom_spec((*screen.route_hops, admin_hash, target_hex), width_bytes)

    # --- parameter flows (floated over the screen) ---------------------------------

    async def compose_route() -> None:
        """Compose how the measurement reaches the tuned node, which is pinned as the target."""
        topo = build_topology(
            self_id=device_hash or "local",
            contacts=contacts,
            trace_paths=ctx.repo.trace_paths(),
            packet_paths=ctx.repo.packet_paths(),
            neighbour_links=ctx.repo.neighbour_links(),
        )
        admin_id = topo.canonical(admin_hash) or admin_hash[:12]
        composer = PathComposerScreen(
            device_label=device_label,
            device_hash=device_hash,
            topology=topo,
            width_bytes=width_bytes,
            target_id=admin_id,
            target_hash=admin_hash,
            target_label=admin_node.name,
            hops=[topo.canonical(h) or h for h in screen.route_hops],
            resolve=resolve,  # name a hop that the topology left ambiguous, as we do
        )
        result = await session.run_screen(composer)
        if result is CANCEL or not isinstance(result, str):
            return
        if result == AUTO_SPEC:
            screen.route_hops = []  # routing by the device has no meaning here: direct shot
            return
        # The composer gives the symmetric boomerang around the tuned node. Keep only the
        # outbound hops before it. The code adds the target leg.
        tokens = [t for t in result.split(",") if t]
        if tokens and len(tokens) % 2 == 1 and tokens == tokens[::-1]:
            screen.route_hops = tokens[: len(tokens) // 2]
        else:
            screen.route_hops = tokens[:-1] if tokens else []

    async def pick_range() -> None:
        """Select the TX range of the sweep: the default, the full range, or a typed range."""
        preferred_lo, preferred_hi = ctx.preferences.tx_opt_min, ctx.preferences.tx_opt_max
        items = [
            Choice(
                title=f"TX {preferred_lo}–{preferred_hi}  —  your configured default",
                value=(preferred_lo, preferred_hi),
            ),
            Choice(
                title=f"TX {REMOTE_TX_MIN}–{REMOTE_TX_MAX}  —  the full remote range",
                value=(REMOTE_TX_MIN, REMOTE_TX_MAX),
            ),
            Choice(title="Custom…  —  type a window", value="custom"),
        ]
        picked = await session.run_screen(
            SelectScreen(
                "Sweep range",
                items,
                prompt="The TX window the sweep explores:",
                default=(screen.tx_min, screen.tx_max),
                footer_hint="↑↓ move · Enter set · Esc keep",
                filterable=False,
            )
        )
        if picked is CANCEL or picked is None:
            return
        if picked == "custom":
            typed = await session.text(
                "Sweep range",
                prompt=f"Low–high, within {REMOTE_TX_MIN}–{REMOTE_TX_MAX}:",
                default=f"{screen.tx_min}-{screen.tx_max}",
                validate=lambda t: (
                    parse_tx_range(t) is not None
                    or "Enter two numbers, low then high (e.g. 14-24)."
                ),
            )
            if not typed:
                return
            parsed = parse_tx_range(typed)
            if parsed is None:
                return
            picked = parsed
        screen.tx_min, screen.tx_max = picked

    async def pick_step() -> None:
        """Select the coarse grid spacing. 1 measures each level, so there is nothing to refine."""
        items = [
            Choice(
                title=f"{n}"
                + ("  —  every level up front" if n == 1 else f"  —  every {_nth(n)} level"),
                value=n,
            )
            for n in STEP_CHOICES
        ]
        picked = await session.run_screen(
            SelectScreen(
                "Coarse step",
                items,
                prompt="The coarse sweep measures the window at this spacing, then refines.",
                default=screen.step,
                footer_hint="↑↓ move · Enter set · Esc keep",
                filterable=False,
            )
        )
        if picked is not CANCEL and picked is not None:
            screen.step = int(picked)

    async def pick_samples() -> None:
        """Select the number of traces for each level. They are paced, like the trace tool."""
        pace = ctx.preferences.trace_cooldown_s
        items = [
            Choice(
                title=f"{n} trace{'s' if n > 1 else ' '}"
                + ("  —  fast but noisy" if n == 1 else ""),
                value=n,
            )
            for n in SAMPLE_CHOICES
        ]
        picked = await session.run_screen(
            SelectScreen(
                "Samples per level",
                items,
                prompt=f"Each TX level is measured with this many traces, {pace:g} s apart.",
                default=screen.samples,
                footer_hint="↑↓ move · Enter set · Esc keep",
                filterable=False,
            )
        )
        if picked is not CANCEL and picked is not None:
            screen.samples = int(picked)

    # --- login, sweep, apply --------------------------------------------------------

    async def ensure_login() -> bool:
        """Log in to the tuned node one time for each session. Ask for a password only if necessary.

        This runs *before* the in-flight dialog opens. Thus the password prompt floats over
        the idle screen, and the login command itself is behind the skeleton card. This is
        the same pattern as each other device call before a transmission.
        """
        nonlocal logged_in
        if logged_in:
            return True
        password = ctx.admin_store.get(admin_node)
        if password is None:
            password = await session.text(
                f"Admin password for {admin_node.name}",
                prompt="The repeater ignores tuning commands without an admin login.",
                password=True,
            )
            if not password:
                return False
        async with ctx.ui.busy_overlay():
            outcome = await device.admin_login(admin_node, password)
        ctx.admin_store.record(admin_node, password, outcome)
        if outcome is LoginResult.REFUSED:
            screen.login_text = login_text()
            await session.message_dialog(
                Text(
                    f"{admin_node.name!r} rejected the admin login (wrong password?). "
                    "The saved password was cleared; sweep again to enter a new one.",
                    style="err",
                ),
                title="Admin login",
            )
            return False
        if not outcome:
            # No reply is not a denial. The password stays for the next attempt.
            screen.login_text = login_text()
            await session.message_dialog(
                Text(
                    f"No reply from {admin_node.name} — it may be out of reach, asleep, "
                    "or busy. The saved password was kept; sweep again when it answers.",
                    style="warn",
                ),
                title="Admin login",
            )
            return False
        logged_in = True
        screen.login_text = login_text()
        return True

    async def apply_dialog() -> None:
        """Offer the winner in a floating dialog. If the user selects Apply, write it."""
        result = screen._result
        if result is None or result.best_snr is None or screen._applied:
            return
        was = f" (currently {result.original_tx})" if result.original_tx is not None else ""
        prompt = Text.assemble(
            ("Set TX ", ""),
            (str(result.best_tx), "brand"),
            (f" on {admin_node.name}?{was}", ""),
        )
        # The convention of platform dialogs: the safe way out is on the left, and the
        # committing action is on the right and is the default. Thus Enter applies, and Esc
        # backs out.
        choice = await session.button_dialog(
            prompt,
            [("Cancel", "cancel"), ("Apply", "apply")],
            title="Apply winner",
            default=1,
            # The default hint of the dialog: Enter commits the button that is highlighted,
            # and ←→ moves the highlight (refer to the quit confirm in ui/menu.py).
        )
        if choice != "apply":
            return
        async with ctx.ui.busy_overlay():
            await device.set_remote_tx_power(admin_node, result.best_tx)
        ctx.log.info("set TX power %s on %s", result.best_tx, admin_node.name)
        screen.mark_applied()
        summary["applied"] = True

    async def sweep() -> None:
        """One commit of Sweep: log in if it is necessary, then run the optimizer under a dialog."""
        run_id: int | None = None
        try:
            if not await ensure_login():
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the screen shows the error. It is not a crash
            screen.fail(str(exc))
            return
        finally:
            if not logged_in:
                screen.sweep_finished()

        spinner = Spinner()
        flight = TracingDialog(
            f"Sweeping — {admin_node.name} → {target_label}",
            spinner=spinner,
            on_abort=screen.cancel,
        )
        session.push(flight)
        ticker = asyncio.ensure_future(_animate(session, spinner, flight, screen))
        try:
            path = spec()
            run_id = ctx.repo.start_run(
                "tx-optimize",
                {
                    "path": path,
                    "samples": screen.samples,
                    "step": screen.step,
                    "tx_min": screen.tx_min,
                    "tx_max": screen.tx_max,
                },
                ctx.profile_name,
            )

            def on_trace(trace: TraceResult) -> None:
                flight.last = trace
                assert run_id is not None
                ctx.repo.record_trace(run_id, trace)

            result = await tx_optimizer.optimize_tx_power(
                device,
                target_label,
                admin_node,
                path,
                tx_min=screen.tx_min,
                tx_max=screen.tx_max,
                coarse_step=screen.step,
                samples_per_level=screen.samples,
                apply=False,  # the dialog makes the decision, after the evidence is complete
                cooldown_s=ctx.preferences.trace_cooldown_s,
                snr_tolerance=ctx.preferences.tx_snr_tolerance_db,
                on_level=screen.on_level,
                on_phase=screen.on_phase,
                persist_level=lambda lv: ctx.repo.record_tx_sample(run_id, lv),
                persist_trace=on_trace,
            )
        except asyncio.CancelledError:
            if run_id is not None:
                ctx.repo.finish_run(run_id, "error", {"error": "cancelled"})
            raise
        except Exception as exc:  # noqa: BLE001 - the screen shows the error. It is not a crash
            screen.fail(str(exc))
            if run_id is not None:
                ctx.repo.finish_run(run_id, "error", {"error": str(exc)})
            return
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a short fault of the spinner must never break the sweep
                pass
            session.pop(flight)
            screen.sweep_finished()
        summary.clear()
        summary.update(
            {
                "target": result.target,
                "admin_node": result.admin_node,
                "path": result.path,
                "best_tx": result.best_tx,
                "best_snr": result.best_snr,
                "best_success_rate": round(result.best_success_rate, 3),
                "original_tx": result.original_tx,
                "applied": False,
                "levels_measured": len(result.levels),
            }
        )
        ctx.repo.finish_run(run_id, "ok", summary)
        screen.complete(result)
        await apply_dialog()

    def run_sweep() -> None:
        if screen.running:
            return
        screen.sweep_started(asyncio.ensure_future(sweep()))

    def apply_winner() -> None:
        asyncio.ensure_future(apply_dialog())

    screen._run_sweep = run_sweep
    screen._apply_winner = apply_winner
    screen._compose_route = compose_route
    screen._pick_range = pick_range
    screen._pick_step = pick_step
    screen._pick_samples = pick_samples

    try:
        await session.run_screen(screen)
    finally:
        worker = screen._worker
        if worker is not None and not worker.done():
            # Esc in the middle of a sweep: stop the search and let the unwind of the
            # optimizer restore the original power of the node. The skeleton card covers
            # that last device command.
            worker.cancel()
            async with ctx.ui.busy_overlay():
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 - the sweep is over, so there is nothing to show
                    pass
    return summary


async def _animate(
    session: Any, spinner: Spinner, flight: TracingDialog, screen: TxSweepScreen
) -> None:
    """Advance the spinner and the status of the in-flight dialog at a steady rate."""
    while True:
        await asyncio.sleep(spinner_interval())
        spinner.tick()
        flight.status = screen.phase_label()
        session.invalidate()
