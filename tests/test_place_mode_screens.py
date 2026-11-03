# SPDX-License-Identifier: Apache-2.0
"""Place mode (^N) on the path screens: Node detail, the packet viewer, the live feed, and Trace.

Each screen opens with node names. ^N changes its path lines to the places of the nodes
(refer to :mod:`meshterm.ui.placemode`), and the hint and the lane chip say what the key
does now. The place service of the session is replaced by a small fake in each test.
"""

from __future__ import annotations

import pytest
from rich.text import Text

from meshterm.core.events import MeshEvent
from meshterm.core.models import Hop, Observation, TraceResult, utcnow
from meshterm.core.places import Place
from meshterm.ui import placemode
from meshterm.ui.livefeed_screen import LiveFeedScreen
from meshterm.ui.node_detail_screen import (
    NodeDetailScreen,
    _Route,
    _RoutesView,
    _Tab,
)
from meshterm.ui.packet_viewer import PacketEntry, PacketViewer
from meshterm.ui.pathline import PathHop, PathLine
from meshterm.ui.trace_screen import TraceScreen
from tests.conftest import plain as _plain

#: The node that has a place in these tests (12 hex digits, the id of a node).
_HUB = "3d63c6429436"


class _Places:
    """A fake place service: the hub is in Villeray, and no other node has a place."""

    def get(self, node: str | None) -> Place | None:
        return Place("Villeray", None, "tiles") if node == _HUB else None


@pytest.fixture
def places(monkeypatch: pytest.MonkeyPatch) -> _Places:
    """Make the fake service the place service of the session."""
    service = _Places()
    monkeypatch.setattr("meshterm.services.places._CURRENT", service)
    return service


def _lane_places(screen) -> list:  # noqa: ANN001
    """The slots of the lane that ^N drives."""
    return [pair for pair in screen.picocalc_lyra_lane if pair and pair.action == placemode.ACTION]


# -- Node detail -------------------------------------------------------------------


def _detail(with_line: bool = True) -> NodeDetailScreen:
    line = PathLine([PathHop("Hub", key=_HUB), PathHop("★", you=True)])
    route = _Route(
        draw=(_HUB,),
        spec="3d,f2",
        path=line.text(),
        context=Text("1 hop"),
        line=line if with_line else None,
    )
    routes = _RoutesView(
        routes=[route],
        glyph_of=lambda n: ("●", "#ffffff"),
        label_of=lambda n: n[:2],
        label_rgb_of=lambda n: (200, 200, 200),
    )
    screen = NodeDetailScreen(
        title="Node — Hub",
        header=Text("Hub"),
        info_rows=[("heard", Text("5m ago"))],
        tabs=[_Tab("Info", "info"), _Tab("Routes", "routes")],
        routes=routes,
    )
    screen.note_viewport(30)
    return screen


def test_node_detail_places_act_only_on_the_routes_tab(places: _Places) -> None:
    """^N shows on the Routes tab only, and it changes the route row (not the header)."""
    screen = _detail()
    assert "^N" not in screen.footer_hint  # the Info tab has no path line
    assert not _lane_places(screen)
    screen.handle(placemode.ACTION)
    assert not screen._places.on  # the key does nothing there

    screen.handle("tab")
    assert "^N places" in screen.footer_hint
    assert [p.label for p in _lane_places(screen)] == ["Places"]
    assert "❯ Hub" in _plain(screen.render_body(72))

    screen.handle(placemode.ACTION)
    body = _plain(screen.render_body(72))
    assert "❯ Villeray" in body and "❯ Hub" not in body
    assert "^N names" in screen.footer_hint
    assert [p.label for p in _lane_places(screen)] == ["Names"]

    screen.handle(placemode.ACTION)
    assert "❯ Hub" in _plain(screen.render_body(72))


def test_node_detail_without_a_path_line_hides_the_key() -> None:
    """A route with no path line to change gives no hint and no chip."""
    screen = _detail(with_line=False)
    screen.handle("tab")
    assert "^N" not in screen.footer_hint and not _lane_places(screen)


# -- the packet viewer -----------------------------------------------------------------


def _resolver(names: dict[str, str]):  # noqa: ANN202
    """A node resolver that also tells the node of a hash, as ``make_node_resolver`` does."""

    def resolve(hop: str) -> str:
        return names.get(hop, "")

    resolve.node_of = lambda hop: _HUB if hop in names else None  # type: ignore[attr-defined]
    return resolve


def _row(body: str, label: str) -> str:
    """The line of a card that starts with ``label``."""
    return next(line for line in body.splitlines() if line.startswith(label))


def test_packet_viewer_places_change_the_via_chain_and_the_sender(places: _Places) -> None:
    """The ``from`` chip and the ``via`` line change. The route graph keeps its labels."""
    entry = PacketEntry(when=utcnow(), kind="packet", node=_HUB, path=_HUB, raw={})
    viewer = PacketViewer([entry], 0, resolve=_resolver({_HUB: "Hub"}))
    assert "^N places" in viewer.footer_hint
    assert viewer.footer_hint.endswith("Esc close")
    assert [p.label for p in _lane_places(viewer)] == ["Places"]
    body = _plain(viewer.render_body(80))
    assert "Hub" in _row(body, "from") and "Hub" in _row(body, "via")

    viewer.handle(placemode.ACTION)
    body = _plain(viewer.render_body(80))
    assert "Villeray" in _row(body, "from") and "Villeray" in _row(body, "via")
    assert "Hub" not in _row(body, "from") + _row(body, "via")
    assert "^N names" in viewer.footer_hint
    assert [p.label for p in _lane_places(viewer)] == ["Names"]


def test_packet_viewer_hides_the_key_when_no_hop_is_drawn() -> None:
    """An ack has no path line and no hop chip, so ^N has no hint, no chip, and no effect."""
    entry = PacketEntry(when=utcnow(), kind="ack", where="abcd")
    viewer = PacketViewer([entry], 0, resolve=lambda h: "")
    assert "^N" not in viewer.footer_hint and not _lane_places(viewer)
    viewer.handle(placemode.ACTION)
    assert not viewer._places.on


# -- the live feed -----------------------------------------------------------------------


class _FeedSession:
    def invalidate(self) -> None:
        pass


def test_livefeed_places_change_the_route_chips(places: _Places) -> None:
    """The two endpoints of an addressed packet change with ^N. An empty feed has no key."""
    resolve = _resolver({_HUB: "Hub", "a1": "Alice"})
    screen = LiveFeedScreen(session=_FeedSession(), resolve=resolve, seed=[])
    screen.note_viewport(48)
    assert "^N" not in screen.footer_hint and not _lane_places(screen)  # no row yet

    raw = {"payload_typename": "TEXT_MSG", "dest_hash": _HUB, "src_hash": "a1"}
    obs = Observation(
        node="", name="", kind="packet", snr=5.0, rssi=-90.0, observed_at=utcnow(), raw=raw
    )
    screen.on_event(MeshEvent.observation_event(obs))
    assert "^N places" in screen.footer_hint
    assert [p.label for p in _lane_places(screen)] == ["Places"]
    body = _plain(screen.render_body(100))
    assert "Hub" in body

    screen.handle(placemode.ACTION)
    body = _plain(screen.render_body(100))
    assert "Villeray" in body and "Hub" not in body
    assert "^N names" in screen.footer_hint
    assert [p.label for p in _lane_places(screen)] == ["Names"]


# -- Trace -------------------------------------------------------------------------------


class _TraceSession:
    def invalidate(self) -> None:
        pass


def _trace_screen(auto_spec: str) -> TraceScreen:
    async def flow(current):  # noqa: ANN001, ANN202
        return current

    async def trace(path_spec, on_trace):  # noqa: ANN001, ANN202
        on_trace(TraceResult(target="Hub", success=True, hops=[Hop(index=0, node=_HUB, snr=5.0)]))

    screen = TraceScreen(
        "Hub",
        device_label="Us",
        device_hash="aabb" + "00" * 30,
        resolve=_resolver({_HUB: "Hub"}),
        session=_TraceSession(),
        trace=trace,
        compose_path=flow,
        explore=flow,
        pick_width=flow,
        pick_samples=flow,
        width_bytes=lambda: 6,
        sample_count=lambda: 1,
        pace_s=0.0,
        auto_spec=lambda: auto_spec,
    )
    screen.note_viewport(40)
    return screen


def test_trace_places_change_the_route_lane(places: _Places) -> None:
    """The planned route changes with ^N, and the hint and the chip say what the key does."""
    screen = _trace_screen(_HUB)
    assert "^N places" in screen.footer_hint
    assert [p.label for p in _lane_places(screen)] == ["Places"]
    assert "Hub" in _plain(screen.render_body(100))

    screen.handle(placemode.ACTION)
    body = _plain(screen.render_body(100))
    assert "Villeray" in body
    assert "^N names" in screen.footer_hint
    assert [p.label for p in _lane_places(screen)] == ["Names"]


def test_trace_hides_the_key_when_there_is_no_route() -> None:
    """With no route on the page, ^N has no hint, no chip, and no effect."""
    screen = _trace_screen("")
    assert "^N" not in screen.footer_hint and not _lane_places(screen)
    screen.handle(placemode.ACTION)
    assert not screen._places.on
