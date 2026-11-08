# SPDX-License-Identifier: Apache-2.0
"""Tests for the Device route popup of the node page: its row, its pages, and its actions."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

from meshterm.core.models import Contact, DiscoveredRoute, utcnow
from meshterm.core.outbound_store import SOURCE_DEVICE, SOURCE_SITE, RouteOwner
from meshterm.services.device_routes import (
    ORIGIN_FLOOD,
    ORIGIN_KEPT,
    ORIGIN_LEARNED,
    ORIGIN_SET,
    RouteStatus,
)
from meshterm.services.topology import build_topology
from meshterm.ui.device_route_screen import (
    RouteEnv,
    _action_rows,
    _apply,
    _discover,
    _picker_rows,
    row_label,
)
from meshterm.ui.tui.select import Choice

HOME = RouteOwner(SOURCE_SITE, "Home")
HUB = "3d63c6429436"
ALICE_KEY = "f2c24f54551e" + "0" * 52


def _env() -> RouteEnv:
    hub = Contact(name="Hilltop", public_key=HUB + "0" * 52, key_prefix=HUB)
    alice = Contact(name="Alice", public_key=ALICE_KEY, key_prefix=ALICE_KEY[:12])
    topo = build_topology(
        self_id="aaaaaaaaaaaa",
        contacts=[hub, alice],
        trace_paths=[],
        packet_paths=[],
        neighbour_links=[],
    )
    return RouteEnv(
        contact=alice,
        label="Alice",
        target="f2c24f54551e",
        topology=topo,
        self_key="aa" * 32,
        width_bytes=1,
        resolve=lambda hop: None,
        scenarios=[],
        suggested=SimpleNamespace(hops=(HUB,), source="outbound", rank=1, samples=0),
    )


def _values(rows: list) -> list:
    return [row.value for row in rows if isinstance(row, Choice)]


def test_the_row_says_what_the_radio_holds() -> None:
    """Flood, learned, set by you, and kept read differently, and a skipped kept route shows."""
    assert row_label(RouteStatus(None, ORIGIN_FLOOD)) == "Device route · none, it floods…"
    assert row_label(RouteStatus((), ORIGIN_LEARNED)) == "Device route · no relays, learned…"
    assert row_label(RouteStatus(("3d",), ORIGIN_SET)) == "Device route · 1 hop, set by you…"
    kept = RouteStatus(("3d", "27"), ORIGIN_KEPT, kept=("3d", "27"), owner=HOME)
    assert row_label(kept) == "Device route · 2 hops, kept at Home…"
    skipped = RouteStatus(("27",), ORIGIN_LEARNED, kept=("3d",), owner=HOME, skipped_since=utcnow())
    assert row_label(skipped).endswith("learned · kept route skipped…")


def test_the_actions_follow_the_state_of_the_route() -> None:
    """Keep needs a site, a kept route offers Stop keeping, and a flood has nothing to forget."""
    assert _values(_action_rows(RouteStatus(None, ORIGIN_FLOOD))) == ["set", "discover"]
    learned = RouteStatus(("3d",), ORIGIN_LEARNED, owner=HOME)
    assert _values(_action_rows(learned)) == ["set", "keep", "forget", "discover"]
    no_site = RouteStatus(("3d",), ORIGIN_LEARNED)
    assert _values(_action_rows(no_site)) == ["set", "site", "forget", "discover"]
    kept = RouteStatus(("3d",), ORIGIN_KEPT, kept=("3d",), owner=HOME)
    assert _values(_action_rows(kept)) == ["set", "unkeep", "forget", "discover"]
    fixed = RouteStatus(("3d",), ORIGIN_LEARNED, owner=RouteOwner(SOURCE_DEVICE, "Roof"))
    labels = [row.title for row in _action_rows(fixed) if isinstance(row, Choice)]
    assert "this radio (Roof)" in str(labels[1])


def test_the_picker_puts_the_suggested_route_first_and_compose_last() -> None:
    """The suggested route, no relays, then Compose. A route never repeats."""
    env = _env()
    env.scenarios = [SimpleNamespace(hops=(HUB,), source="observed", rank=0, samples=4)]
    values = _values(_picker_rows(env, RouteStatus(None, ORIGIN_FLOOD)))
    assert values == [("hops", (HUB,)), ("hops", ()), "compose"]


class _Routes:
    """The route service, as the popup calls it."""

    def __init__(self, found: DiscoveredRoute | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.found = found

    async def set(self, contact: Contact, hops: tuple) -> tuple:
        self.calls.append(("set", tuple(hops)))
        return tuple(hops)

    async def keep(self, contact: Contact, hops: tuple) -> RouteOwner:
        self.calls.append(("keep", tuple(hops)))
        return HOME

    async def discover(self, contact: Contact) -> DiscoveredRoute | None:
        self.calls.append(("discover", ()))
        return self.found


def _ctx(routes: _Routes, answer: object = None):  # noqa: ANN202
    prompts: list = []

    async def dialog(prompt, buttons, **kwargs):  # noqa: ANN001, ANN003, ANN202
        prompts.append(prompt)
        return answer

    @asynccontextmanager
    async def busy_dialog(message: str = "", *, title: str = ""):  # noqa: ANN202
        yield None

    ui = SimpleNamespace(dialog=dialog, busy_dialog=busy_dialog, session=None)
    return SimpleNamespace(routes=routes, ui=ui), prompts


async def test_a_picked_route_replaces_a_kept_route() -> None:
    """With a kept route, Set keeps the new route. Without one, it only sets it."""
    routes = _Routes()
    ctx, _ = _ctx(routes)
    kept = RouteStatus(("3d",), ORIGIN_KEPT, kept=("3d",), owner=HOME)
    assert await _apply(ctx, _env(), kept, (HUB, "27d4"))
    assert await _apply(ctx, _env(), RouteStatus(None, ORIGIN_FLOOD), ())
    assert routes.calls == [("keep", (HUB, "27d4")), ("set", ())]


async def test_discover_shows_both_routes_and_uses_the_route_out() -> None:
    """One discovery, a dialog with the route each way, and Use sets the route out."""
    routes = _Routes(DiscoveredRoute(out_hops=("3d",), in_hops=("27", "3d")))
    ctx, prompts = _ctx(routes, answer="use")
    assert await _discover(ctx, _env(), RouteStatus(None, ORIGIN_FLOOD))
    assert routes.calls == [("discover", ()), ("set", (HUB,))]
    text = prompts[0].plain
    assert "Out, to Alice (1 hop)" in text
    assert "Back, to you (2 hops)" in text


async def test_no_answer_to_a_discovery_changes_nothing() -> None:
    """With no answer, the popup says so and sets nothing."""
    routes = _Routes(None)
    ctx, prompts = _ctx(routes)
    assert not await _discover(ctx, _env(), RouteStatus(("3d",), ORIGIN_LEARNED))
    assert routes.calls == [("discover", ())]
    assert "No answer from Alice" in prompts[0]


def test_the_route_row_follows_the_routes_on_the_node_page() -> None:
    """The row comes after the routes, and a refresh keeps the highlight on it."""
    from rich.text import Text

    from meshterm.ui.node_detail_screen import NodeDetailScreen, _Action, _RoutesView, _Tab

    row = _Action("device_route", "🧭", "", "Device route · none, it floods…")
    trace = _Action("trace", "🎯", "", "Trace — auto route …")
    view = _RoutesView(routes=[], note="no route observed yet")
    screen = NodeDetailScreen(
        title="Node — Alice",
        header=Text("Alice"),
        info_rows=[],
        tabs=[_Tab(name="Routes", kind="routes")],
        routes=view,
        trace_action=trace,
        route_action=row,
    )
    focus = [payload for _kind, payload in screen._focusables()]
    assert focus == [trace, row]
    newer = _Action("device_route", "🧭", "", "Device route · 1 hop, set by you…")
    screen.replace_routes(view, route_action=newer, trace_action=trace)
    assert screen._focusables()[screen._row_index][1] is newer
