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


# -- Trace target ----------------------------------------------------------------------


def _row(hops: tuple[str, ...] | None, opened: list | None = None):  # noqa: ANN202
    """A RouteRow over a route service that reports ``hops``, and records each open."""
    from meshterm.ui.device_route_screen import RouteRow

    routes = SimpleNamespace(
        status=lambda contact, device_key: RouteStatus(hops, ORIGIN_LEARNED),
    )

    class _Row(RouteRow):
        async def open(self, proven):  # noqa: ANN001, ANN201
            if opened is not None:
                opened.append(proven)

    return _Row(
        SimpleNamespace(routes=routes),
        Contact(name="Alice", public_key=ALICE_KEY),
        device_key="",
        build_env=lambda contact: _env(),
    )


def test_the_trace_row_lights_only_for_a_route_that_is_a_change() -> None:
    """A traced route that the radio lacks is offered. The same route says so, quietly."""
    from meshterm.ui.device_route_screen import ProvenRoute

    proven = ProvenRoute(hops=("27d4",), home=4, sent=5, worst_snr=2.5)
    assert proven.evidence() == "came home 4 of 5 · worst +2.5 dB"
    text, lit = _row(("3d",)).text(proven)
    assert lit
    assert text == "Use this route for DMs and admin — 4/5 came home…"
    text, lit = _row(("27",)).text(proven)
    assert not lit
    assert text == "Device route — 1 hop, learned · same as the trace…"
    text, lit = _row(None).text(None)
    assert (text, lit) == ("Device route — none, it floods…", False)
    failed = ProvenRoute(hops=("27d4",), home=0, sent=2)
    assert not _row(("3d",)).text(failed)[1]


def test_the_popup_offers_the_traced_route_first() -> None:
    """Use the traced route, then Use and keep it (with a site), then the usual rows."""
    from meshterm.ui.device_route_screen import ProvenRoute

    proven = ProvenRoute(hops=("27d4",), home=3, sent=3)
    learned = RouteStatus(("3d",), ORIGIN_LEARNED, owner=HOME)
    assert _values(_action_rows(learned, proven)) == [
        "traced",
        "traced+keep",
        "set",
        "keep",
        "forget",
        "discover",
    ]
    same = RouteStatus(("27",), ORIGIN_LEARNED, owner=HOME)
    assert _values(_action_rows(same, proven))[0] == "set"


async def test_trace_target_has_the_row_and_hands_it_the_traced_route() -> None:
    """The row follows Explore paths, turns green after a trace home, and opens with it."""
    import asyncio

    from meshterm.ui.device_route_screen import ProvenRoute
    from meshterm.ui.trace_screen import TraceScreen
    from tests.conftest import plain
    from tests.test_live_screens import _FakeSession, _trace

    opened: list = []
    session = _FakeSession()

    async def flow(current):  # noqa: ANN001, ANN202
        return current

    async def trace(spec, on_trace):  # noqa: ANN001, ANN202
        on_trace(_trace(5.0, 2.0))

    def prove(traces: list) -> ProvenRoute | None:
        home = [t for t in traces if t.success]
        return ProvenRoute(("27d4",), len(home), len(traces), 2.0) if home else None

    screen = TraceScreen(
        "Alice",
        device_label="Us",
        device_hash="aabb" + "00" * 30,
        resolve=lambda label: label,
        session=session,
        trace=trace,
        compose_path=flow,
        explore=flow,
        pick_width=flow,
        pick_samples=flow,
        width_bytes=lambda: 1,
        sample_count=lambda: 1,
        pace_s=0.0,
        route_row=_row(("3d",), opened),
        prove=prove,
    )
    screen.note_viewport(40)
    assert screen._actions == ("compose", "explore", "route", "width", "samples", "trace")
    before = "\n".join(plain(line) for line in screen.render_body(72))
    assert "Device route — 1 hop, learned…" in before
    screen._on_trace(_trace(5.0, 2.0))
    after = "\n".join(plain(line) for line in screen.render_body(72))
    assert "Use this route for DMs and admin — 1/1 came home…" in after
    screen._index = screen._actions.index("route")
    screen.handle("enter")
    await asyncio.sleep(0)
    assert opened == [ProvenRoute(("27d4",), 1, 1, 2.0)]
    assert len(screen._traces) == 1  # the popup changes no path of the screen
