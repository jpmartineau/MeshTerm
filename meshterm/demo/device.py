# SPDX-License-Identifier: Apache-2.0
"""The device of the demo: the companion of our node in the Montréal world.

:class:`DemoDevice` is the simulator (:class:`~meshterm.core.connection.MockDevice`) with
the demo world in place of its small test world. It holds the contacts, the channels, and
the settings of a companion that has lived on the mesh for months. It answers a trace
from the link model, a repeater from the region tables and the neighbours of the world,
and it plays the live traffic of the world while the demo runs: the adverts, the packets
of the background, and the lines of the script that arrive after the start
(:data:`~meshterm.demo.script.LIVE_DIRECT`, :data:`~meshterm.demo.script.LIVE_CHANNEL`).
When a channel message of our node mentions a node that has an answer in the script
(:data:`~meshterm.demo.script.REPLIES`), that node answers it.

Everything that the device sends comes from a fixed seed and a time after the start (or
after the message that it answers). Thus two runs play the same traffic at the same
moments.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from datetime import timedelta

from ..core import transmit_gate
from ..core.channels import MENTION
from ..core.connection import EventCallback, MockDevice, Unsubscribe
from ..core.events import MeshEvent
from ..core.models import Contact, Hop, Message, NeighbourInfo, RoomAccess, TraceResult, utcnow
from ..core.region_sim import SimulatedRegionMap
from . import script, traffic, world
from .history import TX_POWER

#: The length of one part of the live traffic (seconds). The device makes the traffic one
#: part at a time, each from its own seed, so a long session never waits for all of it.
_PART_S = 3600.0

#: Whether the device plays the background traffic of the world while the demo runs. The
#: screenshots turn it off (``scripts/screenshots.py``): a packet that arrives during a
#: capture changes the activity bars of the header, so two captures would not be the same.
#: The lines of the script still arrive.
PLAY_BACKGROUND = True

#: The TX power of each repeater of the world, as a read of its settings shows it.
REMOTE_TX = 22

#: The TX power at which a repeater of the world is best heard. It is lower than
#: :data:`REMOTE_TX`. Thus TX optimize finds a lower power for a repeater, with a better
#: signal at its target (refer to :meth:`DemoDevice._tuned`).
REMOTE_TX_BEST = 19


class DemoDevice(MockDevice):
    """The simulator, in the demo world (refer to the module docstring)."""

    def __init__(self) -> None:
        """Build the companion of our node, with the state that the demo starts from."""
        super().__init__(
            seed=hash_seed("device"),
            optimal_tx=TX_POWER,
            optimal_remote_tx=REMOTE_TX_BEST,
            admin_password=world.ADMIN_PASSWORD,
        )
        now = utcnow()
        self._tx_power = TX_POWER
        self._default_remote_tx = REMOTE_TX
        self._contacts = [_contact(node) for node in world.NODES if node.added]
        room = world.node(world.JOINED_ROOM)
        # The room knows our node as a member. Its posts until now are in the history
        # already, so a login brings only the posts that come after the start.
        self._room_board = []
        self._room_access = {room.public_key: RoomAccess.MEMBER}
        self._room_synced = {room.public_key: now}
        self._neighbour_tables = {}
        self._region_maps = {
            node.node_id: SimulatedRegionMap(world.REGION_TABLES[node.name])
            for node in world.REPEATERS
        }
        self._info.update(
            name=world.SELF.name,
            public_key=world.SELF.public_key,
            adv_lat=world.SELF.lat,
            adv_lon=world.SELF.lon,
            adv_loc_policy=1,
            max_tx_power=TX_POWER,
            # The preset of North America, which the mesh of Montréal uses.
            radio_freq=910.525,
            radio_bw=62.5,
            radio_sf=7,
            radio_cr=5,
        )
        self._channels = {
            channel.slot: {
                "channel_idx": channel.slot,
                "channel_name": channel.name,
                "channel_secret": channel.secret,
            }
            for channel in world.CHANNELS
        }
        self._clock_offset = 0
        # Our node uses the path width of most of the mesh. A trace collapses it to the
        # widest width that it can encode (three bytes become two).
        self._path_hash_mode = world.path_width(world.SELF) - 1
        self._answers = 0

    #: The route that a flood login teaches the device: one hop, through Mont-Royal.
    _LEARNED_ROUTE = (world.node("Mont-Royal").hash,)

    async def get_device_info(self) -> dict:  # noqa: D102 - inherited docstring
        return {
            "model": "MeshCore Simulator",
            "ver": "v1.15.0",
            "fw_build": "demo",
            "ble_pin": self._device_pin,
            "repeat": self._client_repeat,
        }

    async def get_battery(self) -> dict:  # noqa: D102 - inherited docstring
        return {"level": 3980, "used_kb": 412, "total_kb": 1024}

    async def get_stats(self) -> dict:  # noqa: D102 - inherited docstring
        return {
            "battery_mv": 3980,
            "uptime_secs": 273_640,  # 3 d 4 h
            "errors": 0,
            "queue_len": 0,
            "noise_floor": -112,
            "last_rssi": -96,
            "last_snr": 7.25,
            "tx_air_secs": 318,
            "rx_air_secs": 9_412,
            "recv": 14_803,
            "sent": 1_212,
            "flood_tx": 388,
            "direct_tx": 824,
            "flood_rx": 11_590,
            "direct_rx": 3_213,
            "recv_errors": 41,
        }

    # -- the remote nodes -----------------------------------------------------------

    def _remote_config(self, node: Contact) -> dict[str, str]:
        """The settings of a node of the world, as its CLI shows them."""
        cfg = super()._remote_config(node)
        known = _node_of(node)
        if known is not None and cfg.get("lat") == "0":
            cfg.update(
                lat=f"{known.lat:.4f}",
                lon=f"{known.lon:.4f}",
                freq="910.525",
                sf="7",
                **{"owner.info": "Mesh Montréal · #montreal", "advert.interval": "240"},
            )
        return cfg

    async def send_remote_command(  # noqa: D102 - inherited docstring
        self, node: Contact, command: str, *, timeout: float = 8.0
    ) -> str | None:
        verb = command.strip().split()[0].lower() if command.strip() else ""
        if verb == "neighbors" and self._mock_key(node) in self._admin_sessions:
            transmit_gate.mark()
            await asyncio.sleep(0)
            table = self._neighbours_of(node)
            return "\n".join(f"{n.node} {n.snr:+.1f}dB {_ago(n)}s" for n in table) or "none"
        return await super().send_remote_command(node, command, timeout=timeout)

    async def fetch_neighbours(self, node: Contact) -> list[NeighbourInfo]:  # noqa: D102
        await asyncio.sleep(0)
        if self._mock_key(node) not in self._admin_sessions:
            return await super().fetch_neighbours(node)  # the same refusal as the simulator
        return self._neighbours_of(node)

    def _neighbours_of(self, node: Contact) -> list[NeighbourInfo]:
        """The neighbour table of a repeater of the world: each node that it hears now."""
        repeater = _node_of(node)
        if repeater is None or not repeater.is_repeater:
            return []
        now = utcnow()
        table = []
        for other in (world.SELF, *world.NODES):
            snr = world.link_snr(other, repeater)
            if snr is None or not (other is world.SELF or traffic.active(other, 0.0)):
                continue
            heard_at = now - timedelta(seconds=self._rng.uniform(30.0, 3600.0))
            table.append(
                NeighbourInfo(other.public_key[:8], world.reading(snr, self._rng), heard_at)
            )
        return table

    # -- the answers -------------------------------------------------------------------

    async def send_channel_message(self, index: int, text: str) -> None:  # noqa: D102
        await super().send_channel_message(index, text)
        self._answer(index, text)

    def _answer(self, index: int, text: str) -> None:
        """Let a node that ``text`` mentions answer it, on the same channel.

        The first mentioned node that has an answer in :data:`~meshterm.demo.script.REPLIES`
        answers after :data:`~meshterm.demo.script.REPLY_DELAY_S`. The answer arrives as a
        live line of the script does: the copies of its flood, then the message.
        """
        channel = next((c for c in world.CHANNELS if c.slot == index), None)
        name = next((n for n in MENTION.findall(text) if n in script.REPLIES), None)
        if channel is None or name is None:
            return
        self._answers += 1
        rng = random.Random(f"meshterm-demo/answer/{self._answers}")
        line = script.Line(0.0, name, script.REPLIES[name])
        items = _live_channel(script.REPLY_DELAY_S, channel.name, line, rng)
        loop = asyncio.get_running_loop()
        start = loop.time()

        async def play() -> None:
            for item in items:
                await asyncio.sleep(max(0.0, item.offset_s - (loop.time() - start)))
                for listener in list(self._listeners):
                    _deliver(item, listener)

        task = asyncio.create_task(play())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # -- traces -----------------------------------------------------------------------

    async def run_trace(  # noqa: D102 - inherited docstring
        self,
        target: str,
        *,
        path: str | None = None,
        timeout: float | None = None,
    ) -> TraceResult:
        transmit_gate.mark()
        await asyncio.sleep(0.4)  # the time on the air, so that the progress is visible
        relays = _relays(target, path)
        width = len(path.split(",")[0].strip()) // 2 if path else 1
        if relays is None:
            return TraceResult(target=target, success=False, tx_power=self._tx_power)
        walked = world.walk(relays, self._rng)
        snrs = self._tuned(relays, walked.snrs) if walked.success else None
        if snrs is None:
            return TraceResult(
                target=target, success=False, tx_power=self._tx_power, path_hash_bytes=width
            )
        hops = [
            Hop(index, relay.public_key[: 2 * width], snr)
            for index, (relay, snr) in enumerate(zip(relays, snrs, strict=False))
        ]
        hops.append(Hop(len(relays), None, snrs[-1]))
        return TraceResult(
            target=target,
            success=True,
            hops=hops,
            round_trip_ms=walked.rtt_ms,
            tx_power=self._tx_power,
            path_hash_bytes=width,
        )

    def _tuned(self, relays: list[world.Node], snrs: list[float]) -> list[float] | None:
        """The readings of a trace, with the TX power that TX optimize set on its relays.

        The world gives each link its SNR at :data:`REMOTE_TX_BEST`. A relay that
        transmits at another power is heard less well on the link that it transmits into.
        The loss follows the curve of the simulator, which has its peak at the best power
        (:meth:`~meshterm.core.connection.MockDevice._expected_remote_snr`). Thus the sweep
        of TX optimize finds that peak, and a power far from it loses traces: a reading
        under :data:`~meshterm.demo.world.FLOOR_DB` always does. A relay that nobody tuned
        keeps the readings of the world.

        Args:
            relays: The forced path, as nodes.
            snrs: The readings of the world: one for each link, the last one back to us.

        Returns:
            The readings, or ``None`` if a weaker link lost the trace.
        """
        best = self._expected_remote_snr(self.optimal_remote_tx)
        readings = list(snrs)
        for index, relay in enumerate(relays):
            tx = self._remote_tx_for(relay.public_key)
            if tx is None:
                continue
            snr = readings[index + 1] + self._expected_remote_snr(tx) - best
            # Below the floor, the radio cannot decode the packet at all.
            if snr < world.FLOOR_DB or self._rng.random() < world.loss_chance(snr):
                return None
            readings[index + 1] = round(snr * 4) / 4
        return readings

    # -- the live traffic ---------------------------------------------------------------

    async def subscribe_events(  # noqa: D102 - inherited docstring
        self, on_event: EventCallback
    ) -> Unsubscribe:
        # Each subscriber gets the traffic from the moment that it subscribes, in parts of
        # one hour. A part comes from its own seed, so the same moment of each run has the
        # same packet.
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        start = loop.time()

        async def play() -> None:
            part = 0
            while not stop.is_set():
                for item in _live_part(part):
                    wait = item.offset_s - (loop.time() - start)
                    if wait > 0:
                        try:
                            await asyncio.wait_for(stop.wait(), wait)
                        except asyncio.TimeoutError:
                            pass  # the moment of the next item came
                    if stop.is_set():
                        return
                    _deliver(item, on_event)
                part += 1

        task = asyncio.create_task(play())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        self._listeners.append(on_event)

        def unsubscribe() -> None:
            stop.set()
            if on_event in self._listeners:
                self._listeners.remove(on_event)

        return unsubscribe


def hash_seed(name: str) -> int:
    """A fixed integer seed for one random source of the demo device."""
    return int.from_bytes(hashlib.sha256(f"meshterm-demo/{name}".encode()).digest()[:4], "big")


def _contact(node: world.Node) -> Contact:
    """The contact that the demo device holds for ``node``.

    The contact has no time of last reception. The app takes it from the history instead,
    where it is the time of the last advert that our node heard from the node (refer to
    :class:`~meshterm.services.device_state.DeviceState`). A real device knows no more.
    """
    return Contact(
        name=node.name,
        public_key=node.public_key,
        key_prefix=node.node_id,
        node_type=node.node_type,
        lat=node.lat if node.shares_location else None,
        lon=node.lon if node.shares_location else None,
        route_hops=world.device_route(node),
    )


def _node_of(contact: Contact) -> world.Node | None:
    """The node of the world that a contact names, by its key (or by its name)."""
    key = (contact.public_key or contact.key_prefix or "").lower()
    for node in (world.SELF, *world.NODES):
        if key and node.public_key.startswith(key[:12]):
            return node
    return world.BY_NAME.get(contact.name)


def _relays(target: str, path: str | None) -> list[world.Node] | None:
    """The relays of a trace: the forced path, or the route out to ``target`` and back.

    Returns:
        The relays, or ``None`` when a hash of the path names no repeater of the world
        (then no relay answers, and the trace times out).
    """
    if path:
        relays = []
        for token in (part.strip().lower() for part in path.split(",") if part.strip()):
            relay = next((r for r in world.REPEATERS if r.public_key.startswith(token)), None)
            if relay is None:
                return None
            relays.append(relay)
        return relays
    node = world.BY_NAME.get(target)
    if node is None or not node.is_repeater:
        return None
    route = world.best_route(world.SELF, node)
    if route is None:
        return None
    return [*route, node, *reversed(route)]


def _ago(entry: NeighbourInfo) -> int:
    """The seconds since a repeater last heard a neighbour, as its CLI writes them."""
    if entry.heard_at is None:
        return 0
    return max(0, int((utcnow() - entry.heard_at).total_seconds()))


def _live_part(part: int) -> list[traffic.Heard]:
    """The live traffic of one part: the background, then the lines of the script in it."""
    start, end = part * _PART_S, (part + 1) * _PART_S
    heard = traffic.traffic(start, end, f"live/{part}") if PLAY_BACKGROUND else []
    rng = random.Random(f"meshterm-demo/live-lines/{part}")
    for offset, line in script.LIVE_DIRECT:
        if start <= offset < end:
            heard.extend(_live_direct(offset, line, rng))
    for offset, name, line in script.LIVE_CHANNEL:
        if start <= offset < end:
            heard.extend(_live_channel(offset, name, line, rng))
    heard.sort(key=lambda item: item.offset_s)
    return heard


def _live_direct(offset: float, line: script.Line, rng: random.Random) -> list[traffic.Heard]:
    """A direct message that arrives during the demo, and the packets that carried it."""
    sender = world.node(line.sender)
    body = {
        "payload_typename": "TEXT_MSG",
        "dest_hash": world.SELF.hash,
        "src_hash": sender.hash,
        "cipher_mac": f"{rng.getrandbits(16):04x}",
    }
    copies = traffic.heard_flood(sender, offset, body, rng)
    first = copies[0].offset_s if copies else offset + 1.0
    snr = copies[0].observation.snr if copies and copies[0].observation else None
    message = Message(text=line.text, sender=sender.node_id, snr=snr)
    return [*copies, traffic.Heard(first + 0.05, message=message)]


def _live_channel(
    offset: float, name: str, line: script.Line, rng: random.Random
) -> list[traffic.Heard]:
    """A channel message that arrives during the demo, and the packets that carried it."""
    channel = next(c for c in world.CHANNELS if c.name == name)
    sender = world.node(line.sender)
    wire = f"{sender.name}: {line.text}"
    body = traffic.grp_txt_body(channel.secret, wire, utcnow() + timedelta(seconds=offset))
    if channel.scope:
        body = traffic.scoped(body, channel.scope)
    copies = traffic.heard_flood(sender, offset, body, rng, region=channel.scope)
    first = copies[0].offset_s if copies else offset + 1.0
    snr = copies[0].observation.snr if copies and copies[0].observation else None
    message = Message(text=wire, channel=channel.slot, is_channel=True, snr=snr)
    return [*copies, traffic.Heard(first + 0.05, message=message)]


def _deliver(item: traffic.Heard, on_event: EventCallback) -> None:
    """Send one live item to the subscriber, stamped with the time of now."""
    now = utcnow()
    if item.observation is not None:
        item.observation.observed_at = now
        on_event(MeshEvent.observation_event(item.observation))
    if item.message is not None:
        item.message.received_at = now
        item.message.sender_timestamp = now
        on_event(MeshEvent.message_event(item.message))
