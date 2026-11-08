# SPDX-License-Identifier: Apache-2.0
"""Tests for device routes: the store's kept and saved routes, and the device layer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meshterm.core.connection import (
    ContactNotOnDeviceError,
    DeviceCommandError,
    MockDevice,
    route_at_width,
)
from meshterm.core.events import EventKind
from meshterm.core.models import Contact
from meshterm.core.outbound_store import (
    SOURCE_DEVICE,
    SOURCE_SITE,
    NoRouteOwnerError,
    OutboundStore,
    RouteOwner,
    clean_route,
)

DEVICE = "c0ffee" * 10 + "abcd"  # a public key of 32 bytes
ALICE = "f2c24f54551e" + "0" * 52
BOB = "a100008dc55e" + "1" * 52
HOME = [("3d63c6429436",)]
LAB = [("a100008dc55e",)]


@pytest.fixture
def store(tmp_path: Path) -> OutboundStore:
    """An empty store in a temporary directory."""
    return OutboundStore(tmp_path / "outbound.json")


# -- the store -----------------------------------------------------------------------


def test_clean_route_takes_no_hops_as_a_neighbour() -> None:
    """A device route can be empty (no relay), unlike an outbound path."""
    assert clean_route([]) == ()
    assert clean_route("") == ()
    assert clean_route("27D4,f2c2") == ("27d4", "f2c2")
    assert clean_route(["27d4", "27d4"]) is None
    assert clean_route(["xyz"]) is None


def test_keep_needs_a_site_or_a_fixed_device(store: OutboundStore) -> None:
    """With no active site and no fixed device, no record can keep a route."""
    assert store.route_owner(DEVICE) is None
    with pytest.raises(NoRouteOwnerError):
        store.keep_route(DEVICE, ALICE, ("3d63",))
    assert store.kept_routes(DEVICE) == {}


def test_kept_routes_belong_to_the_active_site(store: OutboundStore) -> None:
    """A route kept at Home is not the route at Foulab, and comes back with Home."""
    store.put_site("Home", HOME)
    store.put_site("Foulab", LAB)
    store.set_active("Home")
    assert store.keep_route(DEVICE, ALICE, ("3d63", "f2c2")) == RouteOwner(SOURCE_SITE, "Home")
    assert store.kept_route(DEVICE, ALICE) == ("3d63", "f2c2")
    store.set_active("Foulab")
    assert store.kept_route(DEVICE, ALICE) is None
    store.set_active("Home")
    assert store.kept_route(DEVICE, ALICE.upper()) == ("3d63", "f2c2")


def test_a_fixed_device_keeps_its_own_routes(store: OutboundStore) -> None:
    """The fixed device's record comes before the active site, for routes too."""
    store.put_site("Home", HOME)
    store.set_active("Home")
    store.keep_route(DEVICE, ALICE, ("3d63",))
    store.fix_device(DEVICE, "Roof", [])
    assert store.route_owner(DEVICE) == RouteOwner(SOURCE_DEVICE, "Roof")
    assert store.kept_route(DEVICE, ALICE) is None
    store.keep_route(DEVICE, ALICE, ())
    assert store.kept_route(DEVICE, ALICE) == ()
    store.fix_device(DEVICE, "Roof", [("27d4",)])  # new paths keep the routes
    assert store.kept_route(DEVICE, ALICE) == ()
    assert store.unkeep_route(DEVICE, ALICE)
    assert not store.unkeep_route(DEVICE, ALICE)


def test_a_rename_keeps_the_routes_of_the_site(store: OutboundStore) -> None:
    """New paths or a new name do not lose the kept and saved routes."""
    store.put_site("Home", HOME)
    store.set_active("Home")
    store.keep_route(DEVICE, ALICE, ("3d63",))
    store.save_routes("Home", {BOB: ("27",)})
    store.put_site("Maison", [("27d4",)], old_name="Home")
    assert store.kept_route(DEVICE, ALICE) == ("3d63",)
    assert store.saved_routes("Maison") == {BOB: ("27",)}


def test_saved_routes_are_taken_one_time(store: OutboundStore) -> None:
    """A take returns the saved routes and empties them. A newer save replaces an older."""
    store.put_site("Home", HOME)
    store.save_routes("Home", {ALICE: ("3d",), BOB: ()})
    store.save_routes("Home", {ALICE: ("27",)})
    assert store.take_saved("Home") == {ALICE: ("27",), BOB: ()}
    assert store.take_saved("Home") == {}
    with pytest.raises(KeyError):
        store.save_routes("Nowhere", {ALICE: ()})


def test_routes_are_written_from_the_records(tmp_path: Path) -> None:
    """A bad route or a bad contact key does not survive a write."""
    path = tmp_path / "outbound.json"
    path.write_text(
        json.dumps(
            {
                "active": "Home",
                "sites": [
                    {
                        "name": "Home",
                        "paths": [],
                        "kept": {ALICE: ["3d63"], "NOTHEX": ["3d"], BOB: ["zz"]},
                        "saved": {BOB: []},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = OutboundStore(path)
    store.set_active("Home")
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["sites"][0]["kept"] == {ALICE: ["3d63"]}
    assert written["sites"][0]["saved"] == {BOB: []}


# -- the device layer ------------------------------------------------------------------


def test_route_at_width_cuts_each_hop_and_refuses_what_the_radio_cannot_hold() -> None:
    """Each hop goes in at the radio's width. A short hop or a long route is refused."""
    assert route_at_width(["3D63C6", "27d4"], 2) == ("3d63", "27d4")
    assert route_at_width([], 1) == ()
    with pytest.raises(DeviceCommandError, match="shorter"):
        route_at_width(["3d"], 2)
    with pytest.raises(DeviceCommandError, match="not a hop"):
        route_at_width(["xyz"], 1)
    with pytest.raises(DeviceCommandError, match="at most 32"):
        route_at_width([f"{i:04x}" for i in range(33)], 2)


def _held(device: MockDevice, name: str) -> Contact:
    return next(c for c in device._contacts if c.name == name)


async def test_the_mock_sets_and_forgets_a_route() -> None:
    """Set writes the route at the radio's width. Reset makes the contact flood."""
    device = MockDevice()
    alice = _held(device, "Alice")
    assert await device.set_route(alice, ("b2c3d4e5", "a1b2c3d4")) == ("b2", "a1")
    assert _held(device, "Alice").route_hops == ("b2", "a1")
    assert await device.set_route(alice, ()) == ()
    await device.reset_route(alice)
    assert _held(device, "Alice").route_hops is None
    with pytest.raises(ContactNotOnDeviceError):
        await device.set_route(Contact(name="Stranger", public_key="ee" * 32), ())


async def test_the_mock_discovers_a_route_each_way() -> None:
    """A path discovery gives the out route and the in route, and changes nothing."""
    device = MockDevice()
    alice = _held(device, "Alice")
    before = alice.route_hops
    found = await device.discover_route(alice)
    assert found is not None
    assert found.out_hops == ("a1",)
    assert found.in_hops == ("a1",)
    assert _held(device, "Alice").route_hops == before
    device._unreachable.add("Alice")
    assert await device.discover_route(alice) is None


async def test_a_learned_route_is_pushed_as_a_route_change() -> None:
    """The mock pushes a route change when the answer to a flood teaches it a route."""
    device = MockDevice()
    events = []
    unsubscribe = await device.subscribe_events(events.append)
    try:
        held = _held(device, "Alice")
        device._set_route(device._mock_key(held), ("a1",), learned=True)
    finally:
        unsubscribe()
    changes = [e.route for e in events if e.kind is EventKind.ROUTE]
    assert [c.public_key for c in changes] == [held.public_key.lower()]


# -- the service -----------------------------------------------------------------------


class _DevState:
    """The parts of the device state cache that the service reads."""

    def __init__(self, device: MockDevice) -> None:
        self.device = device
        self.invalidated = 0

    async def contacts(self, *, force: bool = False) -> list[Contact]:
        return list(self.device._contacts)

    async def self_info(self) -> dict:
        return {"public_key": DEVICE, "name": "Homestead"}

    def invalidate_contacts(self) -> None:
        self.invalidated += 1


class _Repo:
    """A repository with no evidence: no first hop is silent."""

    def trace_paths(self) -> list:
        return []

    def packet_paths(self) -> list:
        return []

    def neighbour_links(self) -> list:
        return []


def _ctx(tmp_path: Path, device: MockDevice, *, auto_route: bool = False):  # noqa: ANN202
    from types import SimpleNamespace

    async def get_device() -> MockDevice:
        return device

    return SimpleNamespace(
        outbound_store=OutboundStore(tmp_path / "outbound.json"),
        devstate=_DevState(device),
        device=get_device,
        repo=_Repo(),
        preferences=SimpleNamespace(outbound_silent_hours=24, auto_route=auto_route),
        is_connected=True,
        _device=device,
    )


YAGI = "a1b2c3d4"  # the first hop of Home in these tests (Alice's route starts on it)
LOCAL = "b2c3d4e5"  # the first hop of Foulab (Observer-Bot's route starts on it)


def _sites(ctx) -> None:  # noqa: ANN001
    ctx.outbound_store.put_site("Home", [(YAGI,)])
    ctx.outbound_store.put_site("Foulab", [(LOCAL,)])


async def test_a_site_change_saves_clears_and_restores_learned_routes(tmp_path: Path) -> None:
    """At Foulab, Home's routes are saved and cleared. Back at Home, they come back."""
    from meshterm.services.device_routes import RouteKeeper, report_notice

    device = MockDevice()
    ctx = _ctx(tmp_path, device)
    _sites(ctx)
    keeper = RouteKeeper(ctx)
    report = await keeper.change_site("Foulab")
    assert report is not None
    assert report.saved == {"Home": 2}  # Alice and the room both start on Yagi
    assert _held(device, "Alice").route_hops is None  # cleared: it started on Yagi
    assert _held(device, "Lakeside BBS").route_hops is None
    assert _held(device, "Observer-Bot").route_hops == (LOCAL,)  # Foulab's own: it stays
    alice = _held(device, "Alice").public_key.lower()
    room = _held(device, "Lakeside BBS").public_key.lower()
    assert ctx.outbound_store.saved_routes("Home") == {alice: (YAGI,), room: (YAGI,)}
    assert report_notice(report) == "Saved 2 routes for Home and cleared them from the radio"

    report = await keeper.change_site("Home")
    assert report is not None
    assert report.saved == {"Foulab": 1}
    assert report.restored == 2
    assert _held(device, "Alice").route_hops == ("a1",)  # back, at the radio's width
    assert _held(device, "Observer-Bot").route_hops is None
    assert ctx.outbound_store.saved_routes("Home") == {}


async def test_a_route_learned_here_since_wins_over_a_saved_one(tmp_path: Path) -> None:
    """A saved route is not written over a route that the radio learned at this site."""
    from meshterm.services.device_routes import RouteKeeper

    device = MockDevice()
    ctx = _ctx(tmp_path, device)
    _sites(ctx)
    ctx.outbound_store.set_active("Home")
    alice = _held(device, "Alice")
    ctx.outbound_store.save_routes("Home", {alice.public_key.lower(): (LOCAL,)})
    report = await RouteKeeper(ctx).reconcile()
    assert report.restored == 0
    assert _held(device, "Alice").route_hops == (YAGI,)


async def test_kept_routes_are_written_and_a_silent_first_hop_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reconcile writes each kept route that the radio lacks, unless its hop is silent."""
    from datetime import datetime, timezone

    from meshterm.services import device_routes
    from meshterm.services.device_routes import ORIGIN_KEPT, RouteKeeper

    device = MockDevice()
    ctx = _ctx(tmp_path, device)
    _sites(ctx)
    ctx.outbound_store.set_active("Home")
    alice = _held(device, "Alice")
    bob = _held(device, "Observer-Bot")
    store = ctx.outbound_store
    store.keep_route(DEVICE, alice.public_key, (LOCAL, YAGI))
    store.keep_route(DEVICE, bob.public_key, ("c3d4e5f6",))
    long_ago = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def silent(topo, route, key, before):  # noqa: ANN001, ANN202
        return long_ago if route and route[0] == "c3d4e5f6" else None

    monkeypatch.setattr(device_routes, "silent_since", silent)
    keeper = RouteKeeper(ctx)
    report = await keeper.reconcile()
    assert report.kept_written == 1
    assert report.kept_skipped == ["Observer-Bot"]
    assert _held(device, "Alice").route_hops == ("b2", "a1")
    status = keeper.status(_held(device, "Alice"), device_key=DEVICE)
    assert status.origin == ORIGIN_KEPT
    assert status.kept_in_radio


async def test_the_actions_and_the_origin_of_a_route(tmp_path: Path) -> None:
    """Set marks the route as set by you. Keep needs a site. Forget makes it flood."""
    from meshterm.core.outbound_store import NoRouteOwnerError
    from meshterm.services.device_routes import (
        ORIGIN_FLOOD,
        ORIGIN_LEARNED,
        ORIGIN_SET,
        RouteKeeper,
    )

    device = MockDevice()
    ctx = _ctx(tmp_path, device)
    keeper = RouteKeeper(ctx)
    alice = _held(device, "Alice")
    assert keeper.status(alice, device_key=DEVICE).origin == ORIGIN_LEARNED
    await keeper.set(alice, (LOCAL,))
    assert keeper.status(_held(device, "Alice"), device_key=DEVICE).origin == ORIGIN_SET
    with pytest.raises(NoRouteOwnerError):
        await keeper.keep(alice, (LOCAL,))
    await keeper.forget(alice)
    assert keeper.status(_held(device, "Alice"), device_key=DEVICE).origin == ORIGIN_FLOOD
    assert ctx.devstate.invalidated >= 2


async def test_auto_route_sets_the_suggested_route_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the preference off nothing happens. On, a contact with no route gets one."""
    from types import SimpleNamespace

    from meshterm.services import device_routes
    from meshterm.services.device_routes import RouteKeeper

    device = MockDevice()
    alice = _held(device, "Alice")
    await device.reset_route(alice)
    ctx = _ctx(tmp_path, device)
    assert await RouteKeeper(ctx).prepare_message(alice) is None

    ctx = _ctx(tmp_path, device, auto_route=True)

    class _Topo:
        self_id = "local"

        def canonical(self, hop: str) -> str:
            return hop

        def suggested(self, target, *, outbound, silent_before):  # noqa: ANN001, ANN202
            return SimpleNamespace(hops=(LOCAL,))

    monkeypatch.setattr(device_routes, "session_topology", lambda *a: _Topo())
    assert await RouteKeeper(ctx).prepare_message(alice) == ("b2",)
    # A route that the radio holds, and that starts on no other site's hop, stays.
    assert await RouteKeeper(ctx).prepare_message(_held(device, "Alice")) is None


async def test_the_radio_gets_the_fresh_record_with_only_the_route_changed() -> None:
    """set_route reads the radio's record and changes its route only (flags and name stay)."""
    from types import SimpleNamespace

    from meshterm.core.connection import MeshCoreDevice

    key = "ab" * 32
    record = {
        "public_key": key,
        "type": 1,
        "flags": 1,
        "out_path_len": -1,
        "out_path_hash_mode": 0,
        "out_path": "",
        "adv_name": "Alice",
        "last_advert": 1_700_000_000,
        "adv_lat": 45.5,
        "adv_lon": -73.6,
    }
    calls: list[tuple[dict, str, int]] = []

    async def get_contact_by_key(pubkey: bytes):  # noqa: ANN202
        return SimpleNamespace(payload=dict(record), is_error=lambda: False)

    async def change_contact_path(contact: dict, path: str, path_hash_mode: int):  # noqa: ANN202
        calls.append((contact, path, path_hash_mode))
        return SimpleNamespace(payload={}, is_error=lambda: False)

    async def get_path_hash_mode() -> int:
        return 1

    device = MeshCoreDevice(port="mock")
    device._mc = SimpleNamespace(
        commands=SimpleNamespace(
            get_contact_by_key=get_contact_by_key,
            change_contact_path=change_contact_path,
            get_path_hash_mode=get_path_hash_mode,
        )
    )
    alice = Contact(name="Alice", public_key=key)
    assert await device.set_route(alice, ("3d63c6", "27d4aa")) == ("3d63", "27d4")
    ((sent, path, mode),) = calls
    assert sent == record  # the fresh record, unchanged: the library adds the route
    assert (path, mode) == ("3d6327d4", 1)
    with pytest.raises(DeviceCommandError, match="key prefix"):
        await device.set_route(Contact(name="Short", public_key="ab12"), ())
