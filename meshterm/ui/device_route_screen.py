# SPDX-License-Identifier: Apache-2.0
"""The Device route popup: see, set, keep, forget, and discover the radio's route to a contact.

The radio holds one *device route* for each contact: the relays that it sends to the
contact through (refer to :mod:`meshterm.services.device_routes`). The Routes tab of the
node page has one row for it, ``Device route · …``, which says what the radio holds now.
Enter on the row opens this popup. Its first page shows the route and how the radio got it
(learned, set by you, or kept), and lists the actions:

* **Set the route…** turns the page to a picker. The suggested route comes first (an
  outbound path of the user, then the best way on from it), then the other routes that
  MeshTerm heard, then *no relays* (a neighbour), then ``Compose…``, which opens the path
  composer. When the route is kept, a new route replaces the kept route too.
* **Keep this route** remembers the route for the active site, or for the fixed device,
  and MeshTerm writes it back each time that the radio replaces it. With no site, the row
  says that Keep needs a site, and opens Outbound paths.
* **Stop keeping it** forgets the choice. The radio keeps what it holds.
* **Forget the route** clears the route in the radio. The next message floods, and the
  radio learns a new route from the answer.
* **Discover routes…** sends one path discovery (one flood) and shows the route in each
  direction, with ``Use this route`` to set the route out.

The popup asks and then closes (a popup is not a place). The node page then shows the new
state on its row.

Trace target has the same row (:class:`RouteRow`). A trace that came home walked its route
out and back, which is the best proof that MeshTerm can have for a route. Thus, after such
a trace, the row offers the route that the trace walked (a :class:`ProvenRoute`), and the
popup gets two rows before the others: **Use the traced route** and **Use and keep it**.
A contact that does not relay a trace and that our node hears directly has no relay at
which a walk can turn. There, the row offers no relays at once, with the packets heard as
its proof, and the first row of the popup is **Use no relays**.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rich.text import Text

from ..core.models import Contact
from ..core.outbound_store import SOURCE_DEVICE, NoRouteOwnerError
from ..services.device_routes import (
    ORIGIN_KEPT,
    ORIGIN_SET,
    RouteStatus,
    same_route,
    silent_before_now,
)
from ..services.topology import MeshTopology, PathScenario
from .cooldown import wait_for_cooldown
from .menus import WizardPage, menu_rows, run_wizard
from .pathline import cut_to, path_line
from .tui.select import Choice, Separator
from .widgets import age_seconds, format_ago

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import AppContext

#: The action key of the row on the node page.
ACTION = "device_route"
#: The icon of the row (the path class of the payload icons).
ICON = "🧭"

_SET = "set"
_KEEP = "keep"
_UNKEEP = "unkeep"
_FORGET = "forget"
_DISCOVER = "discover"
_NEED_SITE = "site"
_COMPOSE = "compose"
_USE_TRACED = "traced"
_KEEP_TRACED = "traced+keep"
#: How many heard routes the picker lists after the suggested route.
_PICKER_ROUTES = 6


@dataclass(slots=True)
class RouteEnv:
    """What the popup needs from the node page, which already read it.

    Attributes:
        contact: The contact, as the radio reported it (with its route).
        label: The name of the contact on the page.
        target: The canonical id of the contact in the graph.
        topology: The evidence graph around our node.
        self_key: The public key of the connected companion, or ``""``.
        width_bytes: The path-hash width at which a hop with no name shows its hash.
        resolve: Names a hop that the graph cannot name.
        scenarios: The suggested routes of the page, best first.
        suggested: The one best route, or ``None``.
        outbound: The outbound paths that apply now, as canonical ids.
    """

    contact: Contact
    label: str
    target: str
    topology: MeshTopology
    self_key: str
    width_bytes: int
    resolve: Callable[[str], str | None]
    scenarios: list[PathScenario]
    suggested: PathScenario | None
    outbound: tuple[tuple[str, ...], ...] = ()

    def name(self, hop: str | None) -> str | None:
        """The name of a hop: the target's label, the name in the graph, or the resolver's."""
        if hop is None:
            return None
        if hop == self.target:
            return self.label
        return self.topology.display_name(hop) or self.resolve(hop) or hop

    def line(self, hops: tuple[str, ...]) -> Text:
        """A route from our ``★`` through ``hops`` to the contact, as a path line."""
        return path_line(
            [None, *hops, self.target],
            self.name,
            prefix_bytes=self.width_bytes,
            hash_bytes=self.width_bytes,
            bare_self=True,
            dim_self=False,
        ).text()

    def back_line(self, hops: tuple[str, ...]) -> Text:
        """A route from the contact through ``hops`` back to our ``★``, as a path line."""
        return path_line(
            [self.target, *hops, None],
            self.name,
            prefix_bytes=self.width_bytes,
            hash_bytes=self.width_bytes,
            bare_self=True,
            dim_self=False,
        ).text()

    def canonical(self, hops: tuple[str, ...]) -> tuple[str, ...]:
        """The hops as the graph names them, where it can (a longer key prefix)."""
        return tuple(self.topology.canonical(hop) or hop for hop in hops)


@dataclass(frozen=True, slots=True)
class ProvenRoute:
    """A route out to a contact with proof: traces that came home on it, or a neighbour heard.

    The usual proof is a trace. A contact that does not relay a trace and that MeshTerm
    hears directly has no relay at which a walk can turn. Its proof is the packets that our
    node heard from it with no relay, and its route is no relays.

    Attributes:
        hops: The relays from our node to the contact, as canonical ids. Empty means no
            relay.
        home: How many traces came home on the route.
        sent: How many traces went out on it.
        worst_snr: The median bottleneck SNR (dB) of the traces that came home, if known.
        heard: How many times our node heard the contact directly, for a route with no
            relays and no trace. It is 0 for a route that traces proved.
    """

    hops: tuple[str, ...]
    home: int
    sent: int
    worst_snr: float | None = None
    heard: int = 0

    @property
    def proved(self) -> bool:
        """Whether there is proof to offer the route on: a trace came home, or a neighbour heard."""
        return self.home > 0 or (not self.hops and self.heard > 0)

    def evidence(self) -> str:
        """The proof in words: ``came home 5 of 5 · worst +3.5 dB``, or ``heard directly 4×``."""
        if not self.home:
            return f"heard directly {self.heard}×"
        text = f"came home {self.home} of {self.sent}"
        if self.worst_snr is not None:
            text += f" · worst {self.worst_snr:+.1f} dB"
        return text


def _hops_text(hops: tuple[str, ...]) -> str:
    count = len(hops)
    if count == 0:
        return "no relays"
    return "1 hop" if count == 1 else f"{count} hops"


def row_label(status: RouteStatus, *, sep: str = " · ") -> str:
    """The label of the row: the route that the radio holds, in short.

    Args:
        status: The device route now.
        sep: The text between the name of the row and its value. The node page uses a
            middle dot. The action list of Trace target uses a dash, because each of its
            rows has that shape.
    """
    if status.hops is None:
        now = "none, it floods"
    elif status.origin == ORIGIN_KEPT and status.owner is not None:
        now = f"{_hops_text(status.hops)}, kept at {status.owner.name}"
    elif status.origin == ORIGIN_SET:
        now = f"{_hops_text(status.hops)}, set by you"
    else:
        now = f"{_hops_text(status.hops)}, learned"
    if status.skipped_since is not None:
        now += " · kept route skipped"
    return f"Device route{sep}{now}…"


def _where(status: RouteStatus) -> str:
    """Where a kept route lives, in words."""
    owner = status.owner
    if owner is None:
        return ""
    return f"this radio ({owner.name})" if owner.source == SOURCE_DEVICE else owner.name


#: The cells before the text of a status line: the pointer column of the rows below it.
#: Thus the route and the action labels start in the same cell.
_INDENT = "  "


def _note(text: str, style: str = "muted") -> Separator:
    """One status line, in the column of the row labels."""
    return Separator(Text(_INDENT + text, style))


def _route_note(line: Text) -> Separator:
    """One route as a status line, in the column of the row labels, cut to the width."""

    def title(width: int) -> Text:
        text = Text(_INDENT)
        text.append_text(cut_to(line, max(1, width - len(_INDENT))))
        return text

    return Separator(title)


def _status_rows(
    env: RouteEnv, status: RouteStatus, proven: ProvenRoute | None = None
) -> list[Any]:
    """The lines above the actions: the route now and its origin, then any other route.

    The other routes are the kept route (when the radio holds another one) and the route
    that the traces walked (when it is a change).
    """
    rows: list[Any] = []
    if status.hops is None:
        rows.append(_note("none — the radio floods, then learns a route"))
    else:
        rows.append(_route_note(env.line(env.canonical(status.hops))))
        if status.origin == ORIGIN_KEPT:
            how = f"kept at {_where(status)}"
        elif status.origin == ORIGIN_SET:
            how = "set by you"
        else:
            how = "learned from a path return"
        if status.changed_at is not None:
            how += f" · {format_ago(age_seconds(status.changed_at))}"
        rows.append(_note(how))
    if status.kept is not None and not status.kept_in_radio:
        rows.append(_note(f"you keep another route at {_where(status)}:"))
        rows.append(_route_note(env.line(status.kept)))
        if status.skipped_since is not None:
            rows.append(
                _note(
                    "not in the radio: its first hop was last heard "
                    f"{format_ago(age_seconds(status.skipped_since))}",
                    "warn",
                )
            )
    if _offers_traced(status, proven):
        assert proven is not None
        if proven.home:
            rows.append(_note(f"the trace {proven.evidence()} on:"))
        else:
            rows.append(_note(f"{env.label} can't relay a trace, and it was {proven.evidence()}:"))
        rows.append(_route_note(env.line(proven.hops)))
    return rows


def _offers_traced(status: RouteStatus, proven: ProvenRoute | None) -> bool:
    """Whether the proven route is a change: it has its proof, and the radio holds another."""
    return proven is not None and proven.proved and not same_route(status.hops, proven.hops)


def _action_rows(status: RouteStatus, proven: ProvenRoute | None = None) -> list[Any]:
    """The actions of the first page, for the state of the route and a proven route."""
    rows: list[tuple[str, str, str]] = []
    if _offers_traced(status, proven):
        assert proven is not None
        rows.append(
            (
                "Use the traced route" if proven.home else "Use no relays",
                "the kept route changes too" if status.kept is not None else "set it now",
                _USE_TRACED,
            )
        )
        if status.kept is None and status.owner is not None:
            rows.append(
                ("Use and keep it", f"write it back here at {_where(status)}", _KEEP_TRACED)
            )
    rows.append(
        (
            "Set the route…",
            "the kept route changes too" if status.kept is not None else "choose its relays",
            _SET,
        )
    )
    if status.kept is not None:
        rows.append(("Stop keeping it", "the radio keeps what it holds", _UNKEEP))
    elif status.hops is not None:
        if status.owner is None:
            rows.append(("Keep this route…", "Keep needs a site: open Outbound paths", _NEED_SITE))
        else:
            rows.append(("Keep this route", f"write it back here at {_where(status)}", _KEEP))
    if status.hops is not None:
        rows.append(("Forget the route", "the next message floods", _FORGET))
    rows.append(("Discover routes…", "one flood: the route each way", _DISCOVER))
    return menu_rows(rows)


def _scenario_tag(scenario: PathScenario) -> str:
    """Where a suggested route came from, in words."""
    if scenario.source == "outbound":
        return f"outbound path {scenario.rank}"
    if scenario.source == "device":
        return "the radio's route"
    if scenario.samples:
        return f"heard {scenario.samples}×"
    return "heard"


def _picker_rows(env: RouteEnv, status: RouteStatus) -> list[Any]:
    """The routes to set: the suggested route, the routes heard, no relays, and Compose."""
    rows: list[Any] = []
    seen: list[tuple[str, ...]] = []

    def add(hops: tuple[str, ...], tag: str) -> None:
        if hops in seen or env.target in hops:
            return
        seen.append(hops)
        line = env.line(hops)
        now = " · now in the radio" if same_route(status.hops, hops) else ""

        def title(width: int, line: Text = line) -> Text:
            return cut_to(line, width)

        rows.append(Choice(title=title, value=("hops", hops), detail=Text(f"{tag}{now}", "muted")))

    if env.suggested is not None:
        add(tuple(env.suggested.hops), f"★ suggested · {_scenario_tag(env.suggested)}")
    for scenario in env.scenarios:
        if len(seen) > _PICKER_ROUTES:
            break
        if scenario.source != "direct":
            add(tuple(scenario.hops), _scenario_tag(scenario))
    add((), "no relays: the contact is a neighbour")
    rows.extend(menu_rows([("Compose…", "choose each hop yourself", _COMPOSE)]))
    return rows


async def open_device_route(
    ctx: AppContext, env: RouteEnv, *, proven: ProvenRoute | None = None
) -> bool:
    """Run the Device route popup for one contact.

    Args:
        ctx: The shared application context.
        env: What the popup needs from the page that opened it.
        proven: The route that traces walked and came home on, from Trace target. When
            it is not the radio's route, the popup offers it first.

    Returns:
        Whether the route or the kept route changed, so that the page shows it again.
    """
    status = ctx.routes.status(
        env.contact,
        device_key=env.self_key,
        topo=env.topology,
        silent_before=silent_before_now(ctx),
    )
    title = f"Device route — {env.label}"

    def first(_values: list) -> WizardPage:
        items = [
            *_status_rows(env, status, proven),
            Separator(" "),
            *_action_rows(status, proven),
        ]
        default = _USE_TRACED if _offers_traced(status, proven) else _SET
        return WizardPage(title=title, items=items, default=default)

    def second(values: list):  # type: ignore[no-untyped-def] - a page, or an awaitable
        if values[0] != _SET:
            return _answer(values[0])
        return WizardPage(
            title=f"Set the route — {env.label}",
            items=_picker_rows(env, status),
            prompt="The radio sends to the contact on this route:",
        )

    answers = await run_wizard(ctx.ui.session, [first, second])
    if answers is None:
        return False
    action, picked = answers[0], answers[1]
    if action in (_USE_TRACED, _KEEP_TRACED) and proven is not None:
        if action == _KEEP_TRACED:
            return await _guard(ctx, env, ctx.routes.keep(env.contact, proven.hops))
        return await _apply(ctx, env, status, proven.hops)
    if action == _SET:
        if picked == _COMPOSE:
            hops = await _compose(ctx, env, status)
            if hops is None:
                return False
        else:
            hops = picked[1]
        return await _apply(ctx, env, status, hops)
    if action == _KEEP and status.hops is not None:
        return await _guard(ctx, env, ctx.routes.keep(env.contact, env.canonical(status.hops)))
    if action == _UNKEEP:
        await ctx.routes.unkeep(env.contact)
        return True
    if action == _FORGET:
        return await _guard(ctx, env, ctx.routes.forget(env.contact))
    if action == _NEED_SITE:
        from .outbound_screen import manage_outbound

        await manage_outbound(ctx)
        return True
    if action == _DISCOVER:
        return await _discover(ctx, env, status)
    return False


async def _answer(value: Any) -> Any:
    """A step of the wizard that asks nothing: it gives the first answer again at once."""
    return value


async def _guard(ctx: AppContext, env: RouteEnv, action: Any) -> bool:
    """Run one route action, and say in a dialog why the radio refused it."""
    try:
        await action
    except NoRouteOwnerError:
        await ctx.ui.dialog(
            "Keep needs a site. Choose one on Outbound paths first.",
            [("OK", None)],
            title="Device route",
            default=0,
        )
        return False
    except Exception as exc:  # noqa: BLE001 - the radio refused. The page stays as it was
        await ctx.ui.dialog(
            f"The radio refused the change to the route to {env.label}: {exc}",
            [("OK", None)],
            title="Device route",
            default=0,
            danger=True,
        )
        return False
    return True


async def _apply(ctx: AppContext, env: RouteEnv, status: RouteStatus, hops: tuple) -> bool:
    """Write a picked route. A kept route is replaced, so that the radio keeps the new one."""
    hops = tuple(hops)
    if status.kept is not None:
        return await _guard(ctx, env, ctx.routes.keep(env.contact, hops))
    return await _guard(ctx, env, ctx.routes.set(env.contact, hops))


async def _compose(ctx: AppContext, env: RouteEnv, status: RouteStatus) -> tuple | None:
    """Compose a route hop by hop in the path composer. ``None`` when the user backs out.

    The composer composes a trace to the contact, so its Auto row means "let the device
    route". Here, that is to forget the route.
    """
    from .path_composer import AUTO_SPEC, PathComposerScreen
    from .tui.screen import CANCEL

    seed = list(env.canonical(status.hops)) if status.hops else None
    screen = PathComposerScreen(
        device_label="you",
        device_hash=env.self_key or None,
        topology=env.topology,
        width_bytes=env.width_bytes,
        target_id=env.target,
        target_hash=(env.contact.public_key or env.target).lower(),
        target_label=env.label,
        hops=seed,
        resolve=env.resolve,
        outbound_paths=env.outbound,
    )
    result = await ctx.ui.session.run_screen(screen)
    if result is CANCEL or result is None:
        return None
    if result == AUTO_SPEC:
        await _guard(ctx, env, ctx.routes.forget(env.contact))
        return None
    return tuple(screen.hops)


async def _discover(ctx: AppContext, env: RouteEnv, status: RouteStatus) -> bool:
    """Send one path discovery, show the two routes, and set the route out on request."""
    if not await wait_for_cooldown(ctx, action="Path discovery"):
        return False
    try:
        async with ctx.ui.busy_dialog(
            f"asking {env.label} for its routes…", title="Discover routes"
        ):
            found = await ctx.routes.discover(env.contact)
    except Exception as exc:  # noqa: BLE001 - say why, and keep the page
        await ctx.ui.dialog(
            f"The radio couldn't send the path discovery: {exc}",
            [("OK", None)],
            title="Discover routes",
            default=0,
            danger=True,
        )
        return False
    if found is None:
        await ctx.ui.dialog(
            f"No answer from {env.label}. It may be out of reach, or asleep.",
            [("OK", None)],
            title="Discover routes",
            default=0,
        )
        return False
    out = env.canonical(found.out_hops)
    back = env.canonical(found.in_hops)
    prompt = Text()
    prompt.append(f"Out, to {env.label} ({_hops_text(out)}):\n", "muted")
    prompt.append_text(env.line(out))
    prompt.append(f"\n\nBack, to you ({_hops_text(back)}):\n", "muted")
    prompt.append_text(env.back_line(back))
    if same_route(status.hops, out):
        prompt.append("\n\nThe radio already holds the route out.", "muted")
    answer = await ctx.ui.dialog(
        prompt,
        [("Close", None), ("Use this route", "use")],
        title=f"Discover routes — {env.label}",
        default=1,
    )
    if answer != "use":
        return False
    return await _apply(ctx, env, status, out)


class RouteRow:
    """The Device route row of Trace target, and the popup that it opens.

    The trace screen reads :meth:`text` at each paint, so the row says what the radio
    holds now. After a trace comes home on another route, the row offers that route
    instead, and Enter opens the popup with it first.

    Args:
        ctx: The shared application context.
        contact: The target, as the radio reported it.
        device_key: The public key of the connected companion, or ``""``.
        build_env: Builds what the popup needs, for the contact as it is now. It runs
            when the popup opens, because it reads all the evidence.
    """

    def __init__(
        self,
        ctx: AppContext,
        contact: Contact,
        *,
        device_key: str,
        build_env: Callable[[Contact], RouteEnv],
    ) -> None:
        """Read the route that the radio holds now."""
        self._ctx = ctx
        self._contact = contact
        self._device_key = device_key
        self._build_env = build_env
        self._status = self._read()

    def _read(self) -> RouteStatus:
        return self._ctx.routes.status(self._contact, device_key=self._device_key)

    @property
    def contact(self) -> Contact:
        """The target, as the radio reported it the last time."""
        return self._contact

    def text(self, proven: ProvenRoute | None) -> tuple[str, bool]:
        """The label of the row, and whether it is lit: a traced route to offer.

        Args:
            proven: The route that the traces of the screen came home on, or ``None``.
        """
        if _offers_traced(self._status, proven):
            assert proven is not None
            if not proven.home:
                return f"Use no relays for DMs and admin — {proven.evidence()}…", True
            came = f"{proven.home}/{proven.sent} came home"
            return f"Use this route for DMs and admin — {came}…", True
        label = row_label(self._status, sep=" — ")
        if proven is not None and proven.home > 0:
            label = label.removesuffix("…") + " · same as the trace…"
        return label, False

    async def open(self, proven: ProvenRoute | None) -> None:
        """Open the popup, then read the contact and its route again after a change."""
        env = self._build_env(self._contact)
        if not await open_device_route(self._ctx, env, proven=proven):
            return
        try:
            contacts = await self._ctx.devstate.contacts()
        except Exception:  # noqa: BLE001 - keep the old snapshot, the row still opens
            contacts = []
        wanted = self._contact.public_key.lower()
        self._contact = next((c for c in contacts if c.public_key.lower() == wanted), self._contact)
        self._status = self._read()
