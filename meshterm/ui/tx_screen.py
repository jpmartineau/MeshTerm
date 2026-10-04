# SPDX-License-Identifier: Apache-2.0
"""The live TX-optimization screen: pick the link, arm the sweep, watch levels land.

The interactive face of the ``tx-optimize`` tool (the scripted CLI keeps its one-shot
table and HTML chart), rebuilt on the trace screen's armed-but-idle pattern. One stepped
dialog in the tool chooses the *link* — the admin node whose transmit power is tuned,
then the target whose reception is optimized — and the screen opens idle over the main
menu: the route, the sweep parameters, and the login state read across the top, an
action list drives everything, and nothing transmits until Sweep is committed.

* **Route** opens the trace screen's hop-by-hop path composer, pinned at the tuned
  node: compose how the measurement reaches it, each step suggested from observed
  links; the target hop is appended automatically. The default is the direct shot.
* **Range / Step / Samples** float small dialogs adjusting the sweep: the TX window,
  the coarse grid spacing, and the traces measured per level. The Sweep row always
  shows the resulting worst-case transmission count, so the cost of a commit is on
  screen before it happens (every run is paced, like the trace tool's sampling).
* **Sweep** logs in first when it must — the password prompt floats only when nothing
  is remembered, and a working password is remembered for next time — then runs the
  optimizer under a floating in-flight dialog with Abort: coarse → refine → verify,
  every measured level landing in the bar chart behind it, the current best starred.
  Aborting mid-sweep restores the node's original power.
* **Apply winner** joins the actions once a sweep found one (the cursor lands on it),
  the same offer as the after-sweep dialog for whenever that was first declined.

Every sweep opens one ``runs`` row and persists its levels and traces exactly as a
scripted run records them. A finished sweep leaves the screen armed: adjust the range
and sweep again — a new sweep is a new measurement, so the chart clears as it starts.
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

#: Human phrasing for each optimizer phase reported through ``on_phase``.
_PHASE_LABELS = {
    "coarse": "coarse sweep",
    "refine": "refining around the leader",
    "verify": "verifying the leader",
}

#: The coarse grid spacings the Step dialog offers. 1 measures every level (no refine
#: pass left to do); wider grids trade fewer transmissions for a refine pass later.
STEP_CHOICES = (1, 2, 3, 4, 5)

#: The per-level sample counts the Samples dialog offers (the trace tool's ladder).
SAMPLE_CHOICES = (1, 2, 3, 5, 8)


#: Every mark the sweep's action rows lead with. The list exists to *measure* the icon
#: column so each label starts in the same place, and so a platform that draws no icon
#: lane at all (see :func:`~meshterm.ui.menus.command_icon`) collapses it to nothing.
_ACTION_ICONS = ("✎", "⚙", "#", "▶", "★")


def _icon_lane() -> int:
    """The action rows' icon column, in cells — this screen's own marks, measured once."""
    return icon_lane(_ACTION_ICONS)


def _icon(text: Text, icon: str, style: str) -> None:
    """Append an action row's mark in the icon column, its trailing space included."""
    text.append_text(icon_mark(icon, style, _icon_lane()))


class TxSweepScreen(Screen):
    """A full-screen TX-power sweep session — armed, but idle until told.

    ↑/↓ move the cursor over the action rows and Enter commits the selected one — the
    cursor opens on Sweep, so plain Enter still just sweeps. PgUp/PgDn/Home/End scroll
    the body, and Esc (or the Back row) backs out, cancelling any in-flight sweep (the
    optimizer's unwind restores the node's original power).

    The screen renders state and routes keys; the owning session (see
    :func:`open_tx_optimize`) injects the flows — the route composer, the parameter
    dialogs, and the sweep runner — and feeds measurements back through
    :meth:`on_phase` / :meth:`on_level` / :meth:`complete` / :meth:`fail`.
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
        """Create the sweep screen (nothing transmits until the user commits Sweep).

        Args:
            admin_label: Display name of the node being tuned.
            target_label: Display name of the node the SNR is measured at.
            device_label: Our own node's name, opening the route line.
            device_hash: Our own public key, so the route endpoints carry a hash.
            resolve: Maps a hop's raw hash to a friendly contact name when known.
            admin_key: The tuned node's key/hash, so its name wears its own hue.
            target_key: The target's key/hash, likewise.
            session: The running TUI session (for repaints).
            tx_min: Initial low end of the sweep window.
            tx_max: Initial high end of the sweep window.
            step: Initial coarse grid spacing.
            samples: Initial traces measured per level.
            run_sweep: Starts one sweep (owner-guarded; no-op while one is flying).
            apply_winner: Re-offers the apply dialog for a completed sweep's winner.
            compose_route: Opens the route composer flow over this screen.
            pick_range: Floats the TX-window dialog.
            pick_step: Floats the grid-spacing dialog.
            pick_samples: Floats the per-level sample-count dialog.
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

        #: Sweep parameters, mutated by the owner's dialogs between sweeps.
        self.tx_min = tx_min
        self.tx_max = tx_max
        self.step = step
        self.samples = samples
        #: Hops the measurement crosses *before* the tuned node (composer output; at
        #: the session's spec width). Empty = the direct shot.
        self.route_hops: list[str] = []
        #: One line describing the login state, owner-maintained (remembered /
        #: will-ask / logged in), shown in the header.
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
        self._pin_cursor = False  # only pin the view while ↑/↓ are actually in use

    # --- state -------------------------------------------------------------------

    @property
    def _actions(self) -> tuple[str, ...]:
        """The action rows in display order; Apply appears once there is a winner."""
        rows = ["route", "range", "step", "samples", "sweep"]
        if self._can_apply():
            rows.append("apply")
        return tuple(rows)

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The footer keys, tracking whether a sweep is in flight."""
        if self.running:
            return "sweeping… · PgUp/PgDn scroll · Esc back"
        return "↑↓ actions · Enter run · PgUp/PgDn scroll · Esc back"

    def _can_apply(self) -> bool:
        """Whether Apply should be offered (finished, got a winner, not yet set)."""
        return self._result is not None and self._result.best_snr is not None and not self._applied

    def estimated_traces(self) -> int:
        """The worst-case transmission count one Sweep commit can run.

        Coarse grid levels, plus the refine pass's up to ``2 · (step − 1)`` integer
        fill-ins around the winner, plus the verify re-measure — each measured with
        the chosen per-level samples. The real count is usually lower (refine clamps
        at the window's ends and skips levels already measured).
        """
        coarse = len(tx_optimizer.coarse_levels(self.tx_min, self.tx_max, self.step))
        refine = max(0, 2 * (self.step - 1))
        return (coarse + refine + 1) * self.samples

    # --- owner feed ----------------------------------------------------------------

    def sweep_started(self, worker: asyncio.Task) -> None:
        """Adopt a just-started sweep: a new measurement, so the old evidence clears."""
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
        """Mark the sweep no longer in flight (however it ended)."""
        self.running = False
        self._worker = None
        self._session.invalidate()

    def on_phase(self, phase: str) -> None:
        """Record the optimizer entering a search phase (see ``tx_optimizer.PHASES``)."""
        self._phase = phase
        self._session.invalidate()

    def on_level(self, done: int, total: int, level: TxLevelResult) -> None:
        """Record one measured level (replacing any earlier pass at the same power)."""
        self._done, self._total = done, total
        self._levels[level.tx_power] = level
        self._best_tx = tx_optimizer.select_best(list(self._levels.values())).tx_power
        self._session.invalidate()

    def complete(self, result: TxOptResult) -> None:
        """Adopt the finished sweep's selection and park the cursor on Apply."""
        self._result = result
        self._best_tx = result.best_tx
        if self._can_apply():
            self._index = self._actions.index("apply")
        self._session.invalidate()

    def fail(self, error: str) -> None:
        """Mark the sweep failed, keeping whatever levels already landed on screen."""
        self._error = error
        self._session.invalidate()

    def mark_applied(self) -> None:
        """Record that the winner was written to the admin node."""
        self._applied = True
        self._index = min(self._index, len(self._actions) - 1)
        self._session.invalidate()

    def phase_label(self) -> str:
        """The in-flight dialog's status line: phase and level progress."""
        label = _PHASE_LABELS.get(self._phase or "", "starting…")
        if self._total:
            return f"{label} · level {self._done}/{self._total}"
        return label

    def cancel(self) -> None:
        """Cancel any in-flight sweep (the optimizer's unwind restores the power)."""
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()

    # --- input -------------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Move the action cursor, commit the selected action, scroll, or dismiss."""
        if action == "enter":
            self._commit_action()
        elif action == "up":
            # Both ends clamp rather than wrap — the app-wide rule for a row cursor:
            # a highlight that leaps end to end takes the results window with it.
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
        """Float a parameter flow over the screen (one at a time, never mid-sweep)."""
        if self._dialog_open or self.running:
            return
        self._dialog_open = True

        async def run() -> None:
            try:
                await flow()  # type: ignore[misc]  # owner flows are async closures
            finally:
                self._dialog_open = False
                self._session.invalidate()

        asyncio.ensure_future(run())

    # --- rendering -----------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render the header, the action list, the level chart, and the outcome."""
        lines = self._header_lines(width)
        lines.append("")
        self._cursor: int | None = None
        actions = self._actions
        self._index = min(self._index, len(actions) - 1)
        for i, key in enumerate(actions):
            selected = i == self._index
            text = self._action_text(key, selected)
            text.no_wrap = True
            # The route row carries a path line, so it is cut rather than truncated: a
            # chip that runs off the row cracks, every other row keeps the ellipsis.
            text = cut_to(text, width)
            text.no_wrap = True
            if selected:
                self._cursor = len(lines)
            lines.append(render_to_ansi(text, width))
            if key == "samples":
                lines.append("")  # set the sweep group apart from the parameters
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
        """The highlighted action row while ↑/↓ are in use; free scrolling otherwise."""
        return getattr(self, "_cursor", None) if self._pin_cursor else None

    def _action_text(self, key: str, selected: bool) -> Text:
        """One action row: pointer, glyph, and label (current value inlined)."""
        text = Text("❯ " if selected else "  ", style="cursor" if selected else "")
        if key == "route":
            _icon(text, "✎", "brand")
            if self.route_hops:
                # The composed relays alone — the sweep always sets out from us and always
                # lands on the repeater being tuned, and neither is a hop anyone picked
                # here. So both ends are drawn open: the chevrons say the route runs on
                # past them, which is exactly what ``via`` claims in the word before it.
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
        else:  # apply — present only once there is a winner to set
            _icon(text, "★", "ok")
            best = self._result.best_tx if self._result is not None else "?"
            text.append(f"Apply winner — set TX {best} on {self._admin_label}")
        if selected:
            text.style = "cursor"
        return text

    def _header_lines(self, width: int) -> list[str]:
        """The header lanes: tuned link, measured route, sweep window, login state.

        The route renders through THE path widget and wraps at hop boundaries under
        its own value column (the hanging-indent rule) instead of folding back to
        column zero; the tuned link above it wears the same key hues as the route.
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
        """The walk one measurement makes: out through the tuned link, mirrored home.

        The outbound leg — us, any composed hops, the tuned node, the target — draws
        in full colour (each name in its own key hue); the return (the outbound
        mirrored back, the trace boomerang the optimizer actually flies) is dimmed,
        reading as "not yours to compose".

        Both our ends stand on the app-wide ``★`` rather than our name: every
        measurement this screen makes leaves us and comes home to us, so spelling
        ourselves out twice per line would cost the lane the very hops the sweep is
        tuning — the same trade the trace route lane and the trophy card make.
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
        """A route hop's friendly name when known, else its raw hash."""
        named = self._resolve(hop)
        return named if named else hop

    def _levels_table(self) -> Table:
        """Every measured level as a bar chart, ascending by TX, the best starred."""
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
        """The completed sweep's verdict and apply status, or the in-flight error."""
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
    """``2 → "2nd"``, ``3 → "3rd"``, … for the Step action row."""
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n if n < 20 else n % 10, "th")
    return f"{n}{suffix}"


def parse_tx_range(text: str) -> tuple[int, int] | None:
    """Parse a typed TX window like ``"12-28"`` / ``"12 28"`` into ``(low, high)``.

    Clamped to the remote firmware's representable window; ``None`` when the text
    doesn't contain exactly two numbers in order.
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

    The tool's dialog has chosen the link and is already gone; this wires the armed-idle
    screen to the radio, the topology evidence, and the database, and runs it until
    dismissed — and Esc lands on the main menu, the sweep being the whole visit. Login
    happens inside the first Sweep commit — a remembered password silently, otherwise
    one floating prompt (remembered on success, forgotten on rejection: the trace
    composer's convention). Each sweep opens its own ``runs`` row and records every
    level and trace under it, exactly as a scripted run does.

    Args:
        ctx: The shared application context (must be running the interactive TUI surface).
        admin_node: The contact being tuned (the hop before the target).
        target_label: Display name of the node the SNR is measured at.
        target_hash: The target's hex hash (full key, or the typed prefix).

    Returns:
        The last sweep's recorded summary (empty if dismissed before any sweep ran).

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .path_composer import AUTO_SPEC, PathComposerScreen
    from .surface import TuiUi
    from .tui import CANCEL, Choice, SelectScreen

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the live TX sweep is only available in the menu")
    session = ctx.ui.session
    device = await ctx.device()
    # Contacts/self-info/routing width from the session cache (see DeviceState); the live
    # ``device`` is still held for the sweep's own transmissions below.
    contacts = await ctx.devstate.contacts()
    resolve = trace_runner.make_node_resolver(contacts)
    self_info = await ctx.devstate.self_info()
    device_label = str(self_info.get("name") or "this node")
    device_hash = str(self_info.get("public_key") or "") or None

    try:
        width_bytes = collapse_trace_width(int(await ctx.devstate.path_hash_mode()))
    except Exception:  # noqa: BLE001 - optional read; the 1-byte default always works
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
        """The one-way wire spec of the next sweep: composed hops, tuned node, target."""
        return render_custom_spec((*screen.route_hops, admin_hash, target_hex), width_bytes)

    # --- parameter flows (floated over the screen) ---------------------------------

    async def compose_route() -> None:
        """Compose how the measurement reaches the tuned node, pinned as the target."""
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
            resolve=resolve,  # name a hop the topology left ambiguous, as we do
        )
        result = await session.run_screen(composer)
        if result is CANCEL or not isinstance(result, str):
            return
        if result == AUTO_SPEC:
            screen.route_hops = []  # device routing is meaningless here: direct shot
            return
        # The composer emits the symmetric boomerang around the tuned node; keep just
        # the outbound hops before it (the target leg is ours to append).
        tokens = [t for t in result.split(",") if t]
        if tokens and len(tokens) % 2 == 1 and tokens == tokens[::-1]:
            screen.route_hops = tokens[: len(tokens) // 2]
        else:
            screen.route_hops = tokens[:-1] if tokens else []

    async def pick_range() -> None:
        """Pick the sweep's TX window: the configured default, the full range, or typed."""
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
        """Pick the coarse grid spacing (1 = measure every level, nothing to refine)."""
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
        """Pick how many traces each level measures (paced, like the trace tool's)."""
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
        """Log in to the tuned node once per session, prompting only when needed.

        Runs *before* the in-flight dialog goes up, so the password prompt floats
        over the idle screen and the login command itself hides behind the skeleton
        card — the same shape as every other pre-transmission device call.
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
            # Silence is not a denial: the password stays put for the next attempt.
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
        """Offer the winner in a floating dialog; write it to the node on Apply."""
        result = screen._result
        if result is None or result.best_snr is None or screen._applied:
            return
        was = f" (currently {result.original_tx})" if result.original_tx is not None else ""
        prompt = Text.assemble(
            ("Set TX ", ""),
            (str(result.best_tx), "brand"),
            (f" on {admin_node.name}?{was}", ""),
        )
        # Platform-dialog convention: the safe way out left, the committing action right
        # and default, so Enter applies and Esc backs out.
        choice = await session.button_dialog(
            prompt,
            [("Cancel", "cancel"), ("Apply", "apply")],
            title="Apply winner",
            default=1,
            # The dialog default hint: Enter commits whichever button is highlighted,
            # and ←→ is what moves it (see the quit confirm in ui/menu.py).
        )
        if choice != "apply":
            return
        async with ctx.ui.busy_overlay():
            await device.set_remote_tx_power(admin_node, result.best_tx)
        ctx.log.info("set TX power %s on %s", result.best_tx, admin_node.name)
        screen.mark_applied()
        summary["applied"] = True

    async def sweep() -> None:
        """One Sweep commit: log in if needed, then drive the optimizer under a dialog."""
        run_id: int | None = None
        try:
            if not await ensure_login():
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - shown on the screen, not crashed through
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
                apply=False,  # the decision moves to the dialog, after the evidence is in
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
        except Exception as exc:  # noqa: BLE001 - shown on the screen, not crashed through
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
            except Exception:  # noqa: BLE001 - a spinner hiccup must never break the sweep
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
            # Esc mid-sweep: stop the search and let the optimizer's unwind restore the
            # node's original power. The skeleton card covers that last device command.
            worker.cancel()
            async with ctx.ui.busy_overlay():
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 - the sweep is over; nothing to surface
                    pass
    return summary


async def _animate(
    session: Any, spinner: Spinner, flight: TracingDialog, screen: TxSweepScreen
) -> None:
    """Advance the in-flight dialog's spinner and status on a steady cadence."""
    while True:
        await asyncio.sleep(spinner_interval())
        spinner.tick()
        flight.status = screen.phase_label()
        session.invalidate()
