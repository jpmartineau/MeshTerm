# SPDX-License-Identifier: Apache-2.0
"""Smoke tests for the skeleton: models, mock device, service, and persistence.

These tests run without hardware. They use the :class:`MockDevice` simulator.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meshterm.core.connection import DeviceCommandError, MockDevice, clamp_tx_power
from meshterm.core.models import Contact, TraceResult, TraceStats
from meshterm.persistence.repository import Repository
from meshterm.services import trace_runner


def test_clamp_tx_power() -> None:
    """The function clamps TX power to the supported range."""
    assert clamp_tx_power(-5) == 1
    assert clamp_tx_power(99) == 22
    assert clamp_tx_power(14) == 14


def test_advert_time_refuses_a_future_sender_clock() -> None:
    """The stamp of an advert is from the clock of the sender. A future stamp is refused.

    A plausible stamp converts. A node with a wrong clock advertises timestamps from the
    future. If the code accepts such a timestamp, the contact shows "heard now" and is at
    the top of each list that is sorted by heard time, until the wall clock catches up.
    Normal skew (less than the
    tolerance) stays: its age clamps to "now" for a short time and corrects itself. The
    usual never-heard forms (zero, absent, or garbage) still become None.
    """
    from meshterm.core.models import ADVERT_FUTURE_SKEW_S, advert_time, utcnow

    past = int(utcnow().timestamp()) - 3600
    when = advert_time(past)
    assert when is not None and int(when.timestamp()) == past
    assert advert_time(int(utcnow().timestamp()) + 60) is not None  # normal skew
    assert advert_time(int(utcnow().timestamp()) + ADVERT_FUTURE_SKEW_S + 60) is None
    assert advert_time(0) is None
    assert advert_time(None) is None
    assert advert_time("garbage") is None


def test_trace_stats_aggregation() -> None:
    """Robust statistics show the successes and the bottleneck SNR."""
    from meshterm.core.models import Hop

    traces = [
        TraceResult(target="x", success=True, hops=[Hop(0, "a", 5.0), Hop(1, "b", -2.0)]),
        TraceResult(target="x", success=False),
        TraceResult(target="x", success=True, hops=[Hop(0, "a", 7.0), Hop(1, "b", 0.0)]),
    ]
    stats = TraceStats.from_traces("x", traces)
    assert stats.samples == 3
    assert stats.successes == 2
    assert stats.success_rate == pytest.approx(2 / 3)
    assert stats.median_min_snr == pytest.approx(-1.0)  # median of [-2.0, 0.0]


def test_parse_trace_path_mixes_names_and_hex() -> None:
    """Names and hex can be mixed. The hex width stays, and the names are cut to match."""
    contacts = [Contact(name="Alice", key_prefix="d4e5f6a7")]
    # 1-byte hops: hex, a contact name (the case does not matter), hex again
    assert trace_runner.parse_trace_path("3d, alice ,f2", contacts) == "3d,d4,f2"
    # the width comes from the hex tokens that the user types, with no cut to 1 byte
    assert trace_runner.parse_trace_path("a1b2c3", contacts) == "a1b2c3"


def test_parse_trace_path_preserves_three_byte_width() -> None:
    """A width of 3 bytes (6 hex digits) stays, and the names are cut to that width."""
    contacts = [Contact(name="Alice", key_prefix="d4e5f6a7b8c9")]
    assert trace_runner.parse_trace_path("3d5f7a,Alice,f2a1b3", contacts) == "3d5f7a,d4e5f6,f2a1b3"


def test_parse_trace_path_rejects_mixed_widths() -> None:
    """All the hex hops must have the same number of bytes."""
    with pytest.raises(ValueError):
        trace_runner.parse_trace_path("3d,a1b2c3", contacts=[])


def test_last_traced_by_name_folds_hex_to_contacts_and_keeps_latest() -> None:
    """The TRACED lane of the Trace picker joins each stored target to its contact.

    A node can be traced as "alice" on one day and by its key prefix on another day. The
    lane shows one last-traced time for it: the later of the two times. A name that looks
    like hex stays a name (the code matches it by name, not by address). A prefix that
    names no contact is removed.
    """
    from datetime import datetime, timezone

    from meshterm.tools.trace import _last_traced_by_name

    contacts = [
        Contact(name="Alice", public_key="d4e5f6a7" + "00" * 28, key_prefix="d4e5f6a7"),
        Contact(name="cafe", public_key="12ab34cd" + "00" * 28, key_prefix="12ab34cd"),
    ]

    def when(day: int) -> datetime:
        return datetime(2026, 7, day, tzinfo=timezone.utc)

    traced = {
        "d4e5f6": when(1),  # Alice, by an early trace with the key prefix
        "Alice": when(9),  # Alice by name, at a later time: the latest time must win
        "cafe": when(5),  # a name that looks like hex, matched as a name
        "beefbeef": when(3),  # a prefix that names no contact: removed
    }
    assert _last_traced_by_name(contacts, traced) == {"Alice": when(9), "cafe": when(5)}


def test_node_type_resolver_matches_hop_hash_to_contact_type() -> None:
    """The hash of a relay resolves to the node type of its contact (a prefix in either)."""
    from meshterm.core.models import NODE_TYPE_REPEATER

    type_of = trace_runner.make_node_type_resolver(
        [
            Contact(
                name="Repeater",
                public_key="3d63c6" + "00" * 26,
                key_prefix="3d63c6429436",
                node_type=NODE_TYPE_REPEATER,
            ),
            Contact(name="Typeless", public_key="a1b2c3" + "00" * 26, key_prefix="a1b2c3d4"),
        ]
    )
    assert type_of("3d63") == NODE_TYPE_REPEATER  # a short hash is the prefix of the full key
    assert type_of("a1b2") is None  # a known contact, but it advertised no type
    assert type_of("ffff") is None  # an unknown node
    assert type_of(None) is None  # our device, passed through


def test_key_resolver_expands_a_stored_prefix_to_the_full_key() -> None:
    """The stored 12-hex prefix of a heard node resolves to the whole public key of the contact."""
    full = "3d63c6429436" + "ab" * 26  # 64 hex
    resolve = trace_runner.make_key_resolver(
        [
            Contact(name="Repeater", public_key=full, key_prefix="3d63c6429436"),
            Contact(name="Keyless", public_key="", key_prefix="a1b2c3d4"),
        ]
    )
    assert resolve("3d63c6429436") == full  # the stored prefix starts the key
    assert resolve("3d63") == full  # each shorter slice of it, too
    assert resolve("ffffffffffff") == "ffffffffffff"  # no contact: the prefix stays
    assert resolve("a1b2c3d4") == "a1b2c3d4"  # the contact has no full key to expand to
    assert resolve(None) is None


def test_path_hash_flags_power_of_two_only() -> None:
    """The trace flags encode the hash width as 1 << s, so only 1, 2, 4, and 8 bytes map."""
    assert trace_runner.path_hash_flags(1) == 0
    assert trace_runner.path_hash_flags(2) == 1
    assert trace_runner.path_hash_flags(4) == 2
    assert trace_runner.path_hash_flags(8) == 3
    assert trace_runner.path_hash_flags(3) is None


def test_parse_trace_hops_reads_path_snr() -> None:
    """The SNR of each hop is in the payload['path'] dicts. The last node has no hash. It counts."""
    from meshterm.core.connection import parse_trace_hops

    payload = {
        "path": [
            {"hash": "3d", "snr": 4.5},
            {"hash": "f2", "snr": -2.0},
            {"snr": 1.25},  # last node: our device, no hash
        ]
    }
    hops = parse_trace_hops(payload)
    assert [h.node for h in hops] == ["3d", "f2", None]
    assert [h.snr for h in hops] == [4.5, -2.0, 1.25]
    assert [h.index for h in hops] == [0, 1, 2]


def test_parse_trace_hops_empty_without_path() -> None:
    """A reply with no parsed path gives no hops (the SNR shows as n/a)."""
    from meshterm.core.connection import parse_trace_hops

    assert parse_trace_hops({}) == []


def test_route_text_annotates_nodes_with_command_width_hash() -> None:
    """The route shows the hash of each node at the path-hash width of the command."""
    from meshterm.core.models import Hop
    from meshterm.ui.widgets import _route_text

    resolve = trace_runner.make_node_resolver(
        [Contact(name="Alice", public_key="3d63c6" + "00" * 26, key_prefix="3d63c6429436")]
    )
    # A known node (resolves to Alice), then the reply that returns to our device (node=None).
    result = TraceResult(
        target="Alice",
        success=True,
        hops=[Hop(0, "3d63c6", 12.0), Hop(1, None, 12.0)],
        path_hash_bytes=2,  # the command used 2-byte hashes
    )
    plain = _route_text(result, "Me", resolve).plain
    assert "Alice" in plain
    assert "(3d63)" in plain  # cut to 2 bytes, not the full 3d63c6
    assert "3d63c6" not in plain
    assert plain.count("Me") == 2  # our device at both ends, with no hash
    assert "Me (" not in plain.replace(" ", " ")  # the device has no annotation


def test_route_text_annotates_our_device_with_hash() -> None:
    """If the function has our key, both endpoints (us) have our hash at the command width."""
    from meshterm.core.models import Hop
    from meshterm.ui.widgets import _route_text

    result = TraceResult(
        target="x",
        success=True,
        hops=[Hop(0, "3d63", 12.0), Hop(1, None, 12.0)],
        path_hash_bytes=2,  # the command used 2-byte hashes
    )
    plain = _route_text(result, "Me", device_hash="a1b2c3" + "00" * 29).plain.replace("\xa0", " ")
    assert plain.count("Me (a1b2)") == 2  # our device at both ends, with our hash
    assert "a1b2c3" not in plain  # cut to the 2-byte width of the command


def test_traces_table_annotates_links_with_hashes() -> None:
    """The From→To column of each trace shows ``name (hash)``, also for our device."""
    from meshterm.core.models import Hop
    from meshterm.ui.widgets import traces_table

    resolve = trace_runner.make_node_resolver(
        [Contact(name="Alice", public_key="3d63c6" + "00" * 26, key_prefix="3d63c6429436")]
    )
    traces = [
        TraceResult(
            target="Alice",
            success=True,
            hops=[Hop(0, "3d63c6", 12.0), Hop(1, None, 9.0)],
            path_hash_bytes=2,  # the command used 2-byte hashes
        )
    ]
    table = traces_table(traces, "Me", resolve, device_hash="a1b2c3" + "00" * 29)
    links = [cell.plain.replace("\xa0", " ") for cell in table.columns[1].cells]
    assert any("Me (a1b2)" in link for link in links)  # our device has its hash
    assert any("Alice (3d63)" in link for link in links)  # cut to the 2-byte width
    assert not any("3d63c6" in link for link in links)


def test_route_text_unknown_node_shows_hash_only() -> None:
    """An unresolved node shows only its hash, cut to the command width."""
    from meshterm.core.models import Hop
    from meshterm.ui.widgets import _route_text

    result = TraceResult(
        target="x",
        success=True,
        hops=[Hop(0, "aa11bb", 12.0), Hop(1, None, 12.0)],
        path_hash_bytes=1,  # a width of 1 byte
    )
    plain = _route_text(result, "Me").plain.replace(" ", " ")
    assert "→ aa →" in plain  # a hash of 1 byte, no name, no parentheses
    assert "aa11bb" not in plain


def test_route_text_contains_no_non_breaking_spaces() -> None:
    """prompt_toolkit draws U+00A0 as an underscore, so route text must never have one."""
    from meshterm.core.models import Hop
    from meshterm.ui.widgets import _route_text

    result = TraceResult(
        target="x",
        success=True,
        hops=[Hop(0, "3d63", 12.0), Hop(1, None, 12.0)],
        path_hash_bytes=2,
    )
    plain = _route_text(result, "Me", device_hash="a1b2" + "00" * 30).plain
    assert "\xa0" not in plain
    assert plain == "Me (a1b2) → 3d63 → Me (a1b2)"


def test_parse_trace_path_rejects_unknown_token() -> None:
    """A token that is not a known contact and is not valid hex is an error."""
    with pytest.raises(ValueError):
        trace_runner.parse_trace_path("nope", contacts=[])
    with pytest.raises(ValueError):
        trace_runner.parse_trace_path("  ", contacts=[])


async def test_adopt_device_is_reused_without_reopening(tmp_path: Path) -> None:
    """``device()`` returns a device that the startup probe adopted, without a change."""
    from rich.console import Console

    from meshterm.context import AppContext
    from meshterm.core.admin_store import AdminStore
    from meshterm.core.config import Settings
    from meshterm.core.device_store import DeviceStore

    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "adopt.db")
    ctx = AppContext(
        console=Console(file=__import__("io").StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
    )
    try:
        adopted = MockDevice()
        await adopted.connect()
        ctx.adopt_device(adopted)
        # ``device()`` must return the same connection that we adopted. It must not open
        # another connection.
        assert await ctx.device() is adopted
    finally:
        ctx.repo.close()


async def test_mock_device_honors_forced_path() -> None:
    """A forced path sets the repeater hops, and the device adds a hop that returns to us."""
    device = MockDevice()
    await device.connect()
    result = await device.run_trace("Alice", path="3d,f2,3d")
    # Three forced repeater hops, and the hop with no hash for the reply that returns to us.
    assert result.hop_count == 4
    assert [h.node for h in result.hops] == ["3d", "f2", "3d", None]


async def test_mock_device_remove_contact_drops_it_from_the_table() -> None:
    """If the code removes a contact, the device deletes it from its table (by public key)."""
    from meshterm.core.connection import ContactNotOnDeviceError

    device = MockDevice()
    await device.connect()
    before = await device.get_contacts()
    alice = next(c for c in before if c.name == "Alice")
    await device.remove_contact(alice)
    after = await device.get_contacts()
    assert "Alice" not in {c.name for c in after}
    assert len(after) == len(before) - 1
    # If the code removes a contact that the device does not hold, the device refuses, as the
    # firmware refuses. Thus the path "wasn't on the device, removed here anyway" of the
    # screen can run on --mock.
    with pytest.raises(ContactNotOnDeviceError):
        await device.remove_contact(alice)
    assert len(await device.get_contacts()) == len(after)


async def test_mock_device_blank_path_traces() -> None:
    """A blank or automatic path still gives a successful trace that has hops."""
    device = MockDevice()
    await device.connect()
    # Try several times, because the simulator sometimes drops very weak links.
    results = [await device.run_trace("Alice") for _ in range(10)]
    assert any(r.success and r.hops for r in results)


def test_trace_timeout_scales_with_hops() -> None:
    """The reply wait grows by one allowance for each hop, within its documented limits."""
    from meshterm.services.trace_runner import (
        TRACE_TIMEOUT_BASE_S,
        TRACE_TIMEOUT_CEILING_S,
        TRACE_TIMEOUT_FLOOD_S,
        TRACE_TIMEOUT_PER_HOP_S,
        trace_timeout,
    )

    # The code cannot set a size for an unknown hop count (a flood with no path). Thus it
    # uses the flat budget that it always used.
    assert trace_timeout(0) == TRACE_TIMEOUT_FLOOD_S
    assert trace_timeout(-3) == TRACE_TIMEOUT_FLOOD_S

    # Each hop adds exactly one allowance for a hop to the fixed base.
    assert trace_timeout(1) == TRACE_TIMEOUT_BASE_S + TRACE_TIMEOUT_PER_HOP_S
    assert trace_timeout(5) == TRACE_TIMEOUT_BASE_S + 5 * TRACE_TIMEOUT_PER_HOP_S
    # Thus a longer walk always has more time to come home than a shorter walk.
    assert trace_timeout(8) > trace_timeout(3)
    # The ceiling is the limit for a route that otherwise grows with no end.
    assert trace_timeout(10_000) == TRACE_TIMEOUT_CEILING_S


async def test_run_trace_sizes_wait_to_the_forced_route(monkeypatch) -> None:  # noqa: ANN001
    """The reply wait of a forced path has a size that depends on its hop count, not a flat default.

    The real device must give the reply the budget that grows with the route. Thus a long
    walk is not cut off before it can travel out and back.
    """
    import asyncio as _asyncio

    from meshterm.core import connection as connection_module
    from meshterm.core.connection import MeshCoreDevice
    from meshterm.services.trace_runner import trace_timeout

    seen: dict[str, float] = {}
    real_wait_for = _asyncio.wait_for

    async def spy_wait_for(awaitable, timeout):  # noqa: ANN001, ANN202
        seen["timeout"] = timeout
        return await real_wait_for(awaitable, 0)  # a miss, and the budget is not used

    monkeypatch.setattr(connection_module.asyncio, "wait_for", spy_wait_for)

    class _Commands:
        async def send_trace(self, *, auth_code, tag, flags, path):  # noqa: ANN001, ANN201
            return None

    class _MC:
        commands = _Commands()

        def subscribe(self, event_type, callback, attribute_filters=None):  # noqa: ANN001, ANN201
            return _Subscription()

    class _Subscription:
        def unsubscribe(self) -> None:
            return None

    device = MeshCoreDevice(port="COM-test")
    device._mc = _MC()

    # Five hops of one byte give a walk of five hops (the spec is already the mirrored
    # round trip).
    result = await device.run_trace("Alice", path="3d,f2,3d,f2,3d")
    assert result.success is False
    assert seen["timeout"] == trace_timeout(5)

    # An explicit timeout still has priority over the automatic size.
    await device.run_trace("Alice", path="3d,f2,3d,f2,3d", timeout=2.0)
    assert seen["timeout"] == 2.0


async def test_run_trace_catches_a_reply_that_beats_the_send_acknowledgement() -> None:
    """A reply that arrives while ``send_trace`` is still blocked is still the reply to our trace.

    The send acknowledgement of the companion has only its event type as a correlation. Thus
    a command at the same time can consume our acknowledgement, and the send waits for its
    own default time. In the past, the code listened for the tag only after the send
    returned. It dropped each reply that arrived in that window, and the trace showed "no
    reply" although the mesh answered it.
    """
    import asyncio as _asyncio

    from meshterm.core.connection import MeshCoreDevice

    class _Event:
        payload = {"tag": 1, "path": [{"hash": "3d", "snr": 4.0}, {"snr": 2.0}]}

    listeners: list = []

    class _Commands:
        async def send_trace(self, *, auth_code, tag, flags, path):  # noqa: ANN001, ANN201
            # The reply arrives before the acknowledgement: deliver it, then keep waiting.
            for callback in listeners:
                callback(_Event())
            await _asyncio.sleep(0)
            return None

        async def send_appstart(self):  # noqa: ANN201 - the read of the tx power after a success
            return _SelfInfo()

    class _SelfInfo:
        payload = {"tx_power": 20}

    class _MC:
        commands = _Commands()

        def subscribe(self, event_type, callback, attribute_filters=None):  # noqa: ANN001, ANN201
            listeners.append(callback)
            return _Subscription()

    class _Subscription:
        def unsubscribe(self) -> None:
            listeners.clear()

    device = MeshCoreDevice(port="COM-test")
    device._mc = _MC()
    result = await device.run_trace("Alice", path="3d,f2,3d")
    assert result.success is True
    assert [h.node for h in result.hops] == ["3d", None]


def test_trace_edges_endpoints_are_our_device() -> None:
    """The first edge starts at us and the last edge returns to us (#3 and #4)."""
    from meshterm.core.models import Hop

    result = TraceResult(
        target="Alice",
        success=True,
        hops=[Hop(0, "3d", 5.0), Hop(1, "f2", 1.0), Hop(2, None, -1.0)],
    )
    edges = result.edges(device_label="us")
    assert [(e.origin, e.destination) for e in edges] == [
        ("us", "3d"),
        ("3d", "f2"),
        ("f2", "us"),
    ]
    assert [e.snr for e in edges] == [5.0, 1.0, -1.0]


def test_trace_stats_reports_per_hop_medians() -> None:
    """The aggregation gives a median SNR for each hop position (#5)."""
    from meshterm.core.models import Hop

    traces = [
        TraceResult(target="x", success=True, hops=[Hop(0, "a", 4.0), Hop(1, None, 0.0)]),
        TraceResult(target="x", success=True, hops=[Hop(0, "a", 8.0), Hop(1, None, 2.0)]),
    ]
    stats = TraceStats.from_traces("x", traces)
    # The node identities are raw (None = our device). The code adds a label at render time.
    assert [(h.origin, h.destination, h.median_snr) for h in stats.hop_snrs] == [
        (None, "a", 6.0),
        ("a", None, 1.0),
    ]


async def test_mock_device_trace_is_unimodal_in_tx() -> None:
    """The simulator has its peak near its optimal TX power (the average over the noise)."""
    device = MockDevice(optimal_tx=14)
    await device.connect()

    async def avg_snr(tx: int) -> float:
        await device.set_tx_power(tx)
        results = await trace_runner.run_traces(device, "Alice", samples=15, cooldown_s=0)
        stats = TraceStats.from_traces("Alice", results)
        return stats.median_min_snr or -99.0

    low, peak, high = await avg_snr(2), await avg_snr(14), await avg_snr(22)
    assert peak > low
    assert peak > high


async def _setup_link(optimal_remote_tx: int = 20):
    """Return a connected mock device, the (admin, target) contacts, and the forced path.

    The path goes admin → target. Thus the optimizer tunes the admin node and reads the
    SNR that the target receives from it.
    """
    device = MockDevice(optimal_remote_tx=optimal_remote_tx)
    await device.connect()
    contacts = await device.get_contacts()
    admin = next(c for c in contacts if c.name == "Yagi-Repeater")
    target = next(c for c in contacts if c.name == "Alice")
    path = trace_runner.parse_trace_path(f"{admin.name},{target.name}", contacts)
    assert await device.admin_login(admin, "admin")
    return device, admin, target, path


async def test_admin_login_rejects_wrong_password() -> None:
    """The device refuses a wrong admin password, and the node stays closed to tuning."""
    from meshterm.core.connection import LoginResult

    device = MockDevice(admin_password="secret")
    await device.connect()
    admin = (await device.get_contacts())[0]

    refused = await device.admin_login(admin, "nope")
    assert refused is LoginResult.REFUSED and not refused
    with pytest.raises(DeviceCommandError):
        await device.set_remote_tx_power(admin, 18)  # not logged in
    accepted = await device.admin_login(admin, "secret")
    assert accepted is LoginResult.ACCEPTED and accepted
    await device.set_remote_tx_power(admin, 18)  # allowed now
    assert await device.get_remote_tx_power(admin) == 18


async def test_tx_optimizer_finds_simulator_peak(tmp_path: Path) -> None:
    """The optimizer converges near the simulated remote optimum, and it applies the value."""
    from meshterm.services import tx_optimizer

    device, admin, target, path = await _setup_link(optimal_remote_tx=20)

    result = await tx_optimizer.optimize_tx_power(
        device,
        target.name,
        admin,
        path,
        samples_per_level=8,
        coarse_step=3,
        cooldown_s=0,
    )
    assert abs(result.best_tx - 20) <= 3  # near the true remote peak
    assert result.best_success_rate == 1.0  # reliability first: the winner never drops
    assert result.applied
    assert await device.get_remote_tx_power(admin) == result.best_tx  # the node stays tuned
    assert result.admin_node == "Yagi-Repeater"


async def test_tx_optimizer_no_apply_restores_original(tmp_path: Path) -> None:
    """If apply is off, the node stays at the power that it had at the start."""
    from meshterm.services import tx_optimizer

    device, admin, target, path = await _setup_link()
    await device.set_remote_tx_power(admin, 15)  # a power at the start that is known

    result = await tx_optimizer.optimize_tx_power(
        device,
        target.name,
        admin,
        path,
        samples_per_level=4,
        coarse_step=4,
        refine=False,
        verify=False,
        apply=False,
        cooldown_s=0,
    )
    assert result.original_tx == 15
    assert not result.applied
    assert await device.get_remote_tx_power(admin) == 15  # restored, not the winner


async def test_tx_optimizer_reports_phases_in_order() -> None:
    """on_phase announces each enabled search stage, in the order coarse → refine → verify."""
    from meshterm.services import tx_optimizer

    device, admin, target, path = await _setup_link()
    phases: list[str] = []

    await tx_optimizer.optimize_tx_power(
        device,
        target.name,
        admin,
        path,
        samples_per_level=2,
        coarse_step=6,
        apply=False,
        cooldown_s=0,
        on_phase=phases.append,
    )
    assert phases == list(tx_optimizer.PHASES)

    phases.clear()
    await tx_optimizer.optimize_tx_power(
        device,
        target.name,
        admin,
        path,
        samples_per_level=2,
        coarse_step=6,
        refine=False,
        verify=False,
        apply=False,
        cooldown_s=0,
        on_phase=phases.append,
    )
    assert phases == ["coarse"]  # the optimizer does not announce a stage that is off


def test_select_best_prefers_reliability_then_lowest_power() -> None:
    """A level with 100% reliability wins over a less reliable level with a higher SNR.

    If the levels are equal, the lower TX wins.
    """
    from meshterm.core.models import TraceStats, TxLevelResult
    from meshterm.services.tx_optimizer import select_best

    def level(tx: int, snr: float, successes: int, samples: int = 5) -> TxLevelResult:
        return TxLevelResult(
            tx_power=tx,
            samples=samples,
            successes=successes,
            target_snr=snr,
            score=snr,
            stats=TraceStats(
                target="t",
                samples=samples,
                successes=successes,
                median_min_snr=snr,
                median_rtt_ms=None,
            ),
        )

    # A level that is not reliable but has a great SNR must not win over a level that is
    # fully reliable.
    flaky = level(26, snr=12.0, successes=3)
    solid = level(20, snr=7.0, successes=5)
    assert select_best([flaky, solid]).tx_power == 20

    # Among reliable levels with an SNR in the tolerance, the lowest TX wins.
    a = level(18, snr=7.4, successes=5)
    b = level(22, snr=8.0, successes=5)
    assert select_best([a, b], snr_tolerance=1.0).tx_power == 18


def test_round_trip_path_mirrors_back_to_a_reachable_node() -> None:
    """The trace path goes out to the target and back, so a node that is near can answer."""
    from meshterm.services.tx_optimizer import _round_trip_path

    assert _round_trip_path(["3f", "f2"]) == "3f,f2,3f"
    assert _round_trip_path(["3d", "3f", "f2"]) == "3d,3f,f2,3f,3d"
    assert _round_trip_path(["f2"]) == "f2"  # one hop: nothing to mirror


def test_trace_target_snr_matches_target_hop_not_position() -> None:
    """The code reads the SNR of the target from its hash, also when it is the turn-around hop."""
    from meshterm.core.models import Hop
    from meshterm.services.tx_optimizer import trace_target_snr

    # Round trip 3f -> f2 -> 3f -> us: we want the f2 hop (index 1). We do not want the
    # later 3f hop or the return hop that has no hash.
    trace = TraceResult(
        target="f2",
        success=True,
        hops=[Hop(0, "3f", 4.0), Hop(1, "f2", 7.5), Hop(2, "3f", 3.0), Hop(3, None, 4.0)],
    )
    assert trace_target_snr(trace, "f2") == 7.5
    assert trace_target_snr(TraceResult(target="f2", success=False), "f2") is None


class _Event:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def is_error(self) -> bool:
        return False


def _mc_with(contacts: dict, path_hash_mode: int = 2):
    """Build a minimal mock ``MeshCore`` client that has the trace commands that the tests use.

    The contacts arrive in the same way as the radio sends them: one record frame for each
    contact, then the closing frame with the whole table (refer to :class:`_FakeMeshCore`).
    """
    from meshcore import EventType

    mc = _FakeMeshCore(
        [(EventType.NEXT_CONTACT, info) for info in contacts.values()]
        + [(EventType.CONTACTS, contacts)]
    )

    async def get_path_hash_mode() -> int:
        return path_hash_mode

    mc.commands.get_path_hash_mode = get_path_hash_mode
    return mc


async def test_contact_with_future_advert_stamp_reads_as_never_heard() -> None:
    """A contact with an advert that a wrong (future) clock stamped arrives with no heard time.

    The firmware stores the timestamp in the advert without a change, and it only
    increases it (its replay filter). Thus a wrong future stamp stays until real time
    passes it. The conversion refuses the stamp here, so the contact does not show as
    "heard in the future".
    """
    from meshterm.core.connection import MeshCoreDevice
    from meshterm.core.models import utcnow

    now = int(utcnow().timestamp())
    mc = _mc_with(
        {
            "Bogus-Clock": {
                "adv_name": "Bogus-Clock",
                "public_key": "aa" * 32,
                "last_advert": now + 7 * 86400,
            },
            "Honest": {"adv_name": "Honest", "public_key": "bb" * 32, "last_advert": now - 300},
        }
    )
    device = MeshCoreDevice(port="COM-test")
    device._mc = mc
    by_name = {c.name: c for c in await device.get_contacts()}
    assert by_name["Bogus-Clock"].last_seen is None  # refused, so not "heard in the future"
    assert by_name["Honest"].last_seen is not None


async def test_three_byte_route_appends_destination_hash() -> None:
    """A learned route with many hops goes through the repeaters, the target, then back (#1).

    Two things are important for a region that is not a power of two (hashes of 3 bytes).
    First, each routing hash of 3 bytes becomes its first 2 bytes (the widest trace width
    that the protocol can represent). Second, the destination's own hash is added as
    the last outbound hop, because a trace replies only when its destination is the last
    outbound hop. The trace protocol has no separate return-path field, so the code
    mirrors the same repeaters back afterwards. If it does not, no node relays the reply
    home.
    """
    from meshterm.core.connection import MeshCoreDevice

    mc = _mc_with(
        {
            "Repeater": {
                "adv_name": "Repeater",
                "public_key": "aabbcc" + "00" * 29,
                # Two hops, and each hop is a routing hash of 3 bytes (mode 2 => size 3).
                "out_path": "112233445566",
                "out_path_len": 2,
                "out_path_hash_mode": 2,
            }
        }
    )

    device = MeshCoreDevice(port="COM-test")
    resolved = await device._trace_path_to_contact(mc, "Repeater")
    assert resolved is not None
    path_bytes, flags = resolved
    # Two repeater hops cut to 2 bytes, the target's own hash (2 bytes), then the same
    # two repeaters mirrored back to us.
    assert path_bytes == bytes.fromhex("11224455aabb44551122")
    assert flags == trace_runner.path_hash_flags(2)  # a width of 2 bytes


async def test_direct_neighbor_resolves_to_destination_hash() -> None:
    """A direct neighbor (no learned route) resolves to one destination hop.

    This was checked on hardware. These contacts report ``out_path_len == -1`` (no stored
    route), but they answer a trace with one hop that is addressed to their own hash. If
    the mode of the contact is unknown (-1), the path-hash mode of the region (3 bytes)
    sets the width. The code cuts it to the 2 bytes that the protocol can represent.
    """
    from meshterm.core.connection import MeshCoreDevice

    mc = _mc_with(
        {
            "Neighbor": {
                "adv_name": "Neighbor",
                "public_key": "3d63c6" + "00" * 29,
                # A direct neighbor, as the firmware reports it: no learned route.
                "out_path": "",
                "out_path_len": -1,
                "out_path_hash_mode": -1,
            }
        },
        path_hash_mode=2,
    )

    device = MeshCoreDevice(port="COM-test")
    resolved = await device._trace_path_to_contact(mc, "Neighbor")
    assert resolved is not None
    path_bytes, flags = resolved
    # Only the target's own hash. The region width of 3 is cut to a trace hop of 2 bytes.
    assert path_bytes == bytes.fromhex("3d63")
    assert flags == trace_runner.path_hash_flags(2)


async def test_unknown_contact_resolves_to_none() -> None:
    """A target that is not known gives ``None``, so the trace can use no path."""
    from meshterm.core.connection import MeshCoreDevice

    mc = _mc_with(
        {
            "Somebody": {
                "adv_name": "Somebody",
                "public_key": "abcdef" + "00" * 29,
                "out_path": "",
                "out_path_len": -1,
                "out_path_hash_mode": -1,
            }
        }
    )

    device = MeshCoreDevice(port="COM-test")
    resolved = await device._trace_path_to_contact(mc, "Nobody")
    assert resolved is None


class _FakeSubscription:
    """One :meth:`_FakeMeshCore.subscribe` registration. ``unsubscribe`` cancels it."""

    def __init__(self, bus: dict, event_type, callback) -> None:  # noqa: ANN001
        """Register ``callback`` for ``event_type`` on ``bus``."""
        self._bus = bus
        self._key = event_type
        self._callback = callback
        bus.setdefault(event_type, []).append(callback)

    def unsubscribe(self) -> None:
        """Remove the registration, as the subscription of the library does."""
        self._bus.get(self._key, []).remove(self._callback)


class _FakeMeshCore:
    """A companion that answers a contacts request with a script of frames.

    Each script entry is one ``(EventType, payload)`` frame. The class sends it to each
    listener that subscribed to it. Thus a test can stream records, close the dump, refuse
    the dump, or say nothing.
    """

    def __init__(self, *scripts) -> None:
        """Take one frame script for each contacts request. Answer them in order."""
        from types import SimpleNamespace

        self._bus: dict = {}
        self._scripts = list(scripts)
        self.requests = 0
        self.commands = SimpleNamespace(get_contacts_async=self._request)

    def subscribe(self, event_type, callback, attribute_filters=None):  # noqa: ANN001, ANN201
        """Register a listener. Return an object that can unsubscribe it."""
        return _FakeSubscription(self._bus, event_type, callback)

    async def _request(self, lastmod: int = 0) -> None:
        """Answer with the next script of frames."""
        from meshcore.events import Event

        self.requests += 1
        script = self._scripts.pop(0) if self._scripts else []
        for event_type, payload in script:
            for callback in list(self._bus.get(event_type, [])):
                callback(Event(event_type, payload))


async def test_contacts_payload_retries_a_refused_read() -> None:
    """If a companion refuses the dump, the code tries again. It does not show the error."""
    from meshcore import EventType

    from meshterm.core.connection import MeshCoreDevice

    record = {"adv_name": "Repeater", "public_key": "aabbcc" + "00" * 29}
    mc = _FakeMeshCore(
        [(EventType.ERROR, {"reason": "ERR_CODE_BAD_STATE"})],
        [(EventType.ERROR, {"reason": "ERR_CODE_BAD_STATE"})],
        [(EventType.NEXT_CONTACT, record), (EventType.CONTACTS, {"Repeater": record})],
    )

    device = MeshCoreDevice(port="COM-test")
    payload = await device._contacts_payload(mc, retries=3, delay=0, idle=0.05)
    assert "Repeater" in payload
    assert mc.requests == 3  # refused two times, served on the third try


async def test_contacts_payload_waits_out_a_slow_dump() -> None:
    """A table that streams for longer than a fixed deadline still completes.

    The idle clock starts again at each record. Thus the read ends only when the radio is
    quiet. The time of the whole dump does not end it.
    """
    from meshcore import EventType
    from meshcore.events import Event

    from meshterm.core.connection import MeshCoreDevice

    class _SlowMeshCore(_FakeMeshCore):
        async def _request(self, lastmod: int = 0) -> None:
            self.requests += 1
            table = {}
            for index in range(8):
                await asyncio.sleep(0.04)  # each gap is in the idle window
                record = {"adv_name": f"Node{index}", "public_key": f"{index:02x}" + "00" * 31}
                table[record["adv_name"]] = record
                for callback in list(self._bus.get(EventType.NEXT_CONTACT, [])):
                    callback(Event(EventType.NEXT_CONTACT, record))
            for callback in list(self._bus.get(EventType.CONTACTS, [])):
                callback(Event(EventType.CONTACTS, table))

    mc = _SlowMeshCore()
    device = MeshCoreDevice(port="COM-test")
    payload = await device._contacts_payload(mc, retries=0, delay=0, idle=0.1)
    assert len(payload) == 8  # 0.32 s of streaming, with an idle window of 0.1 s


async def test_contacts_payload_ignores_an_error_meant_for_another_command() -> None:
    """An ERROR with no correlation in the dump is for another command. It does not end the read."""
    from meshcore import EventType

    from meshterm.core.connection import MeshCoreDevice

    record = {"adv_name": "Repeater", "public_key": "aabbcc" + "00" * 29}
    mc = _FakeMeshCore(
        [
            (EventType.NEXT_CONTACT, record),
            (EventType.ERROR, {"reason": "ERR_CODE_BAD_STATE"}),  # for a battery poll, not for us
            (EventType.CONTACTS, {"Repeater": record}),
        ]
    )

    device = MeshCoreDevice(port="COM-test")
    payload = await device._contacts_payload(mc, retries=0, delay=0, idle=0.05)
    assert "Repeater" in payload
    assert mc.requests == 1


async def test_contacts_payload_raises_clean_error_after_retries() -> None:
    """A companion that says nothing causes a clear error that the user can act on."""
    from meshterm.core.connection import DeviceCommandError, MeshCoreDevice

    mc = _FakeMeshCore()  # the companion answers each request with silence

    device = MeshCoreDevice(port="COM-test")
    with pytest.raises(DeviceCommandError) as exc:
        await device._contacts_payload(mc, retries=2, delay=0, idle=0.05)
    assert "no event received" in str(exc.value)
    assert mc.requests == 3


async def test_latest_trace_returns_previous_run(tmp_path: Path) -> None:
    """latest_trace rebuilds the most recent stored trace, and it skips a given run."""
    from meshterm.core.models import Hop

    repo = Repository(tmp_path / "prev.db")
    old_run = repo.start_run("trace", {"target": "Alice"})
    repo.record_trace(
        old_run,
        TraceResult(target="Alice", success=True, hops=[Hop(0, "3d", 4.0), Hop(1, None, 1.0)]),
    )
    new_run = repo.start_run("trace", {"target": "Alice"})

    prev = repo.latest_trace("Alice", exclude_run_id=new_run)
    assert prev is not None
    assert [h.node for h in prev.hops] == ["3d", None]
    assert prev.edges("us")[-1].destination == "us"
    assert repo.latest_trace("Nobody") is None
    repo.close()


async def test_repository_round_trip(tmp_path: Path) -> None:
    """The code stores a run and its traces, and it can read them back."""
    repo = Repository(tmp_path / "test.db")
    run_id = repo.start_run("trace", {"target": "Alice", "samples": 2})

    device = MockDevice()
    await device.connect()
    results = await trace_runner.run_traces(
        device, "Alice", samples=2, cooldown_s=0, persist=lambda t: repo.record_trace(run_id, t)
    )
    repo.finish_run(run_id, "ok", {"count": len(results)})

    runs = repo.list_runs()
    assert runs[0].tool == "trace"
    assert runs[0].status == "ok"
    repo.close()
