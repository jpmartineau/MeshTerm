# SPDX-License-Identifier: Apache-2.0
"""Tests for the passive mesh monitor: listening, aggregation, and storage.

All the tests run against the :class:`MockDevice` simulator and an SQLite database on disk.
They do not need hardware.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

from meshterm.core.connection import (
    _MOCK_MONITOR_INTERVAL_S as _MOCK_INTERVAL,
)
from meshterm.core.connection import (
    MockDevice,
    ack_from_event,
    message_from_event,
    observation_from_event,
)
from meshterm.core.models import ChatMessage, HeardNode, Observation, utcnow
from meshterm.persistence.repository import Repository
from meshterm.services.monitor_service import MonitorService


class _StubContext:
    """A minimal substitute for :class:`~meshterm.context.AppContext` in the service tests."""

    def __init__(self, repo: Repository, device: MockDevice) -> None:
        self.repo = repo
        self._device = device
        self._events = None
        self.profile_name = None
        self.log = logging.getLogger("test.monitor")

    async def device(self) -> MockDevice:
        await self._device.connect()
        return self._device

    @property
    def events(self):
        """Build a real event hub for this stub context at the first use, as AppContext does."""
        from meshterm.services.event_hub import EventHub

        if self._events is None:
            self._events = EventHub(self)
        return self._events


async def _collect_observations(device: MockDevice, duration_s: float) -> list[Observation]:
    """Subscribe, collect observations for a period, then unsubscribe. This is a test helper."""
    seen: list[Observation] = []

    def on_event(event) -> None:
        if event.observation is not None:
            seen.append(event.observation)

    unsubscribe = await device.subscribe_events(on_event)
    try:
        await asyncio.sleep(duration_s)
    finally:
        unsubscribe()
    return seen


async def test_subscribe_events_streams_observations_then_stops() -> None:
    """The event stream sends a burst at once, continues to send, and stops at unsubscribe."""
    device = MockDevice()
    await device.connect()

    seen: list[Observation] = []

    def on_event(event) -> None:
        if event.observation is not None:
            seen.append(event.observation)

    unsubscribe = await device.subscribe_events(on_event)
    try:
        # The stream sends the first burst synchronously, before any await.
        first_burst = len(seen)
        assert first_burst >= 1
        assert any(o.lat is not None for o in seen)  # the burst has a repeater with a location

        await asyncio.sleep(_MOCK_INTERVAL * 3)
        assert len(seen) > first_burst  # more packets arrived while the test did nothing
    finally:
        unsubscribe()

    frozen = len(seen)
    await asyncio.sleep(_MOCK_INTERVAL * 3)
    assert len(seen) == frozen  # no more packets after the unsubscribe


async def test_disconnect_stops_background_emitter() -> None:
    """A disconnect cancels each live subscription task. The task does not stay running."""
    device = MockDevice()
    await device.connect()
    await device.subscribe_events(lambda _event: None)
    assert device._bg_tasks  # a background sender is running
    await device.disconnect()
    await asyncio.sleep(0)  # let the cancellation finish
    assert not device._bg_tasks


def test_heard_node_aggregation() -> None:
    """``from_observations`` gives the count, a robust SNR, the latest RSSI, and the location."""
    now = utcnow()
    obs = [
        Observation(node="a1", name="Yagi", snr=4.0, rssi=-100.0, observed_at=now),
        Observation(
            node="a1",
            name="Yagi",
            snr=8.0,
            rssi=-90.0,
            lat=45.5,
            lon=-73.5,
            observed_at=now + timedelta(seconds=5),
        ),
        Observation(
            node="a1", name="Yagi", snr=6.0, rssi=-95.0, observed_at=now + timedelta(seconds=2)
        ),
    ]
    node = HeardNode.from_observations("a1", obs)
    assert node.count == 3
    assert node.median_snr == 6.0  # median of [4, 8, 6]
    assert node.best_snr == 8.0
    assert node.last_seen == now + timedelta(seconds=5)  # the newest observation
    assert node.last_rssi == -90.0  # the RSSI of that newest observation
    assert node.has_location  # the location of one observation stays


def test_heard_node_latest_wins_for_rssi_and_location() -> None:
    """The newest observation gives the RSSI.

    The newest observation with a location gives the coordinates.
    """
    now = utcnow()
    obs = [
        Observation(node="b2", snr=1.0, rssi=-80.0, lat=10.0, lon=20.0, observed_at=now),
        Observation(node="b2", snr=2.0, rssi=-70.0, observed_at=now + timedelta(seconds=10)),
    ]
    node = HeardNode.from_observations("b2", obs)
    assert node.last_rssi == -70.0  # the latest reading
    assert (node.lat, node.lon) == (10.0, 20.0)  # the latest observation that had a fix


def test_observation_from_event_parses_advert() -> None:
    """The event parser gets the node id, name, SNR, RSSI, and location from a payload."""

    class _Event:
        payload = {
            "public_key": "AABBCC0011",
            "adv_name": "Repeater",
            "snr": "7.5",
            "rssi": -92,
            "adv_lat": 45.5,
            "adv_lon": -73.6,
        }

    obs = observation_from_event(_Event(), "advert")
    assert obs is not None
    assert obs.node == "aabbcc0011"
    assert obs.name == "Repeater"
    assert obs.snr == 7.5
    assert obs.rssi == -92.0
    assert (obs.lat, obs.lon) == (45.5, -73.6)


def test_observation_from_event_keeps_full_public_key() -> None:
    """An advert with the whole key keeps it in ``public_key``, and ``node`` stays the 12-hex id."""
    full = "aabbccddee11223344556677" + "00" * 20  # 64 hex

    class _Event:
        payload = {"public_key": full.upper(), "adv_name": "Repeater", "snr": 7.5}

    obs = observation_from_event(_Event(), "advert")
    assert obs is not None
    assert obs.node == full[:12]  # the stored 12-hex canonical id that all joins use
    assert obs.public_key == full  # the whole key, kept for a longer hash display


def test_observation_from_event_short_hash_leaves_no_full_key() -> None:
    """An advert with only a short hash stores only the id. The code does not make a full key."""

    class _Event:
        payload = {"hash": "aabbcc0011", "adv_name": "Rep"}

    obs = observation_from_event(_Event(), "advert")
    assert obs is not None and obs.node == "aabbcc0011" and obs.public_key is None


def test_observation_from_event_without_node_is_skipped() -> None:
    """A payload with no node identifier gives no observation."""

    class _Event:
        payload = {"snr": 3.0}

    assert observation_from_event(_Event(), "advert") is None


def test_message_from_event_parses_direct_message() -> None:
    """A CONTACT_MSG_RECV payload becomes a direct message with a sender and a timestamp."""
    from datetime import datetime, timezone

    class _Event:
        payload = {
            "type": "PRIV",
            "pubkey_prefix": "aabbccddeeff",
            "text": "hello there",
            "sender_timestamp": 1_700_000_000,
            "txt_type": 0,
        }

    msg = message_from_event(_Event())
    assert msg is not None
    assert msg.text == "hello there"
    assert msg.sender == "aabbccddeeff"
    assert msg.is_channel is False
    assert msg.channel is None
    assert msg.sender_timestamp == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)


def test_message_from_event_parses_channel_message() -> None:
    """A CHANNEL_MSG_RECV payload becomes a channel message with no sender contact."""

    class _Event:
        payload = {"type": "CHAN", "channel_idx": 2, "text": "net tonight", "SNR": 5.0}

    msg = message_from_event(_Event())
    assert msg is not None
    assert msg.is_channel is True
    assert msg.channel == 2
    assert msg.sender is None
    assert msg.snr == 5.0


def test_message_from_event_without_text_is_skipped() -> None:
    """A payload with no text body gives no message."""

    class _Event:
        payload = {"pubkey_prefix": "aabb"}

    assert message_from_event(_Event()) is None


def test_ack_from_event_extracts_code() -> None:
    """An ACK payload becomes an Ack with the correlation code."""

    class _Event:
        payload = {"code": "deadbeef"}

    assert ack_from_event(_Event()).code == "deadbeef"


async def test_observations_round_trip_and_aggregate(tmp_path: Path) -> None:
    """The store keeps observations, and ``heard_nodes`` combines them again across runs."""
    repo = Repository(tmp_path / "obs.db")
    run_id = repo.start_run("monitor", {"duration": 1})

    device = MockDevice()
    await device.connect()
    observations = await _collect_observations(device, 0.2)
    for obs in observations:
        repo.record_observation(run_id, obs)
    assert observations

    nodes = repo.heard_nodes()
    assert nodes
    # The aggregate must count each reception, except the packet-log rows. The SNR of such
    # a row belongs to the last relay and not to the node, so these rows go to the topology.
    receptions = [o for o in observations if o.kind != "packet"]
    assert sum(n.count for n in nodes) == len(receptions)
    # The simulator also overhears relayed packets. The store keeps them with their path.
    packets = repo.packet_paths()
    assert packets and all(p.hops for p in packets)
    # The nodes are in order, with the most recently heard node first.
    assert nodes == sorted(nodes, key=lambda n: n.last_seen, reverse=True)
    repo.close()


def test_observation_count_totals_every_run(tmp_path: Path) -> None:
    """``observation_count`` adds the observations of all runs.

    It also works for an empty database.
    """
    repo = Repository(tmp_path / "count.db")
    assert repo.observation_count() == 0
    run_id = repo.start_run("monitor", {})
    repo.record_observation(run_id, Observation(node="a1", snr=1.0))
    repo.record_observation(run_id, Observation(node="b2", snr=2.0))
    assert repo.observation_count() == 2
    repo.close()


def test_observation_full_key_round_trips_into_heard_nodes(tmp_path: Path) -> None:
    """The store keeps a captured full public key, and it shows on the combined HeardNode."""
    full = "3d63c6429436" + "ab" * 26  # 64 hex digits. The node is the first 12.
    repo = Repository(tmp_path / "keys.db")
    run_id = repo.start_run("monitor", {})
    # One advert has the whole key. A later advert for the same node has only the prefix.
    repo.record_observation(run_id, Observation(node=full[:12], public_key=full, snr=5.0))
    repo.record_observation(run_id, Observation(node=full[:12], snr=6.0))
    (node,) = repo.heard_nodes()
    assert node.node == full[:12]  # the id stays the 12-hex prefix
    assert node.public_key == full  # the full key comes from the row that had it
    assert node.count == 2
    repo.close()


def test_last_heard_by_node_is_the_latest_non_packet_reception(tmp_path: Path) -> None:
    """The last heard time of a node is its latest reception that is not a packet.

    This is the evidence of the heard time, with one stamp for each node. It does not use
    ``packet`` rows. The contacts merge uses it, and compares it with the advert time that
    the sender stamped on the device. The query excludes ``packet`` rows in the same way as
    ``heard_nodes``, because when a node hears a relayed packet, it hears the last relay and
    not the originator.
    """
    repo = Repository(tmp_path / "heard.db")
    run_id = repo.start_run("monitor", {})
    early = utcnow() - timedelta(days=4)
    late = utcnow() - timedelta(minutes=5)
    repo.record_observation(run_id, Observation(node="aa" * 6, observed_at=early))
    repo.record_observation(run_id, Observation(node="aa" * 6, observed_at=late))
    repo.record_observation(
        run_id, Observation(node="bb" * 6, kind="packet", path="c1", observed_at=late)
    )
    heard = repo.last_heard_by_node()
    assert heard == {"aa" * 6: late}  # the latest wins, and the relayed packet credits no node
    repo.close()


def test_last_message_by_peer_credits_only_inbound_direct_messages(tmp_path: Path) -> None:
    """When MeshTerm receives a DM, it hears the sender.

    A sent DM and channel traffic do not count.
    """
    repo = Repository(tmp_path / "msgs.db")
    early = utcnow() - timedelta(days=4)
    late = utcnow() - timedelta(minutes=5)
    repo.record_chat_message(ChatMessage(text="hi", peer="AbAbAb", created_at=early))
    repo.record_chat_message(ChatMessage(text="again", peer="ababab", created_at=late))
    repo.record_chat_message(
        ChatMessage(text="mine", peer="cdcdcd", outbound=True, created_at=late)
    )
    repo.record_chat_message(
        ChatMessage(text="all", is_channel=True, channel_id="pub", created_at=late)
    )
    assert repo.last_message_by_peer() == {"ababab": late}  # the store keeps peers in lower case
    repo.close()


async def test_monitor_service_records_while_hub_pumps(tmp_path: Path) -> None:
    """``start()`` subscribes before the hub opens. It logs observations when the hub pumps."""
    repo = Repository(tmp_path / "svc.db")
    ctx = _StubContext(repo, MockDevice())
    service = MonitorService(ctx)

    assert not service.active
    await service.start()  # no device is necessary: no connection is open yet
    assert service.active
    await ctx.events.start()  # the hub opens, and the recording catches the first burst

    await asyncio.sleep(_MOCK_INTERVAL * 3)  # let packets stream in during "other work"
    assert service.session_count > 0
    # The database was empty at the start, so the total is what this session captured.
    assert service.total_count() == service.session_count

    await service.stop()
    assert not service.active
    assert repo.observation_count() == service.session_count  # all the observations were logged

    frozen = service.session_count
    await asyncio.sleep(_MOCK_INTERVAL * 3)
    assert service.session_count == frozen  # the recording did stop

    # The hub is always on, and it continues to listen after the recording stops. Shut it
    # down cleanly, so that the background sender task of the simulator does not continue
    # after the test.
    await ctx.events.stop()
    await ctx._device.disconnect()
    repo.close()


async def test_monitor_service_counts_every_packet_kind(tmp_path: Path) -> None:
    """The activity histogram counts observations, messages, and acks in the same way.

    The newest bucket is first.
    """
    from meshterm.core.events import MeshEvent
    from meshterm.core.models import Ack, Message
    from meshterm.services.monitor_service import ACTIVITY_BUCKETS

    repo = Repository(tmp_path / "act.db")
    ctx = _StubContext(repo, MockDevice())
    service = MonitorService(ctx)
    await service.start()

    assert service.activity_histogram() == (0,) * ACTIVITY_BUCKETS
    hub = ctx.events
    hub.publish(MeshEvent.observation_event(Observation(node="n1", name="n1")))
    hub.publish(MeshEvent.message_event(Message(text="hi", sender="n1")))
    hub.publish(MeshEvent.ack_event(Ack(code="01")))

    histogram = service.activity_histogram()
    assert len(histogram) == ACTIVITY_BUCKETS
    # All three are in the newest bucket. If a slot rolls over, they are in the newest two.
    assert sum(histogram[:2]) == 3 and sum(histogram) == 3

    await service.stop()
    hub.publish(MeshEvent.ack_event(Ack(code=2)))
    assert sum(service.activity_histogram()) == 3  # the counter stopped when the service stopped
    repo.close()


async def test_monitor_service_start_without_device_is_safe(tmp_path: Path) -> None:
    """The recording can start before a device exists. A silent session leaves no run."""

    class _NoDevice(_StubContext):
        async def device(self) -> MockDevice:
            raise ValueError("no companion device selected")

    repo = Repository(tmp_path / "nodev.db")
    service = MonitorService(_NoDevice(repo, MockDevice()))

    await service.start()  # the call does not use the device, so it cannot fail
    assert service.active
    assert service.session_count == 0

    await service.stop()
    assert not service.active
    assert repo.observation_count() == 0
    assert repo.list_runs() == []  # the service opens the run row only if needed, so none
    repo.close()
