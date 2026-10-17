# SPDX-License-Identifier: Apache-2.0
"""The demo world: a synthetic MeshCore mesh over Montréal.

The ``--demo`` flag runs MeshTerm against this world. Two parts of the demo read it. The
simulated device (:mod:`meshterm.demo.device`) answers as the companion of our node. The
history (:mod:`meshterm.demo.history`) fills the database with what that companion heard
in the days before. Thus each screen shows one consistent mesh: a node on the map is a
node that the contacts, the chat, and the routes also know, and a route that a trace
walks is a route that the floods also use.

The world is fixed data and pure functions. Nothing in this module reads the clock, and
each random value comes from a generator with a fixed seed. Thus the world is the same at
each run, and the screenshots of the website show the same mesh each time that somebody
makes them again.

All the names are invented, and no node is a real node. The places are real: the
repeaters stand on the high points of the Montréal area, and the links between them come
from a simple model of the radio path (refer to :func:`link_snr`).
"""

from __future__ import annotations

import functools
import hashlib
import heapq
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from ..core.channels import DEFAULT_PUBLIC_SECRET, derive_secret
from ..core.geo import haversine_km
from ..core.models import (
    NODE_TYPE_CHAT,
    NODE_TYPE_REPEATER,
    NODE_TYPE_ROOM,
    NODE_TYPE_SENSOR,
)

# --------------------------------------------------------------------------- the nodes


@dataclass(frozen=True, slots=True, eq=False)
class Node:
    """One node of the demo world.

    Each node exists once, in :data:`NODES` or as :data:`SELF`. Thus two nodes are equal
    only when they are the same object, and the hash of a node is cheap: the link model
    keys its caches on nodes millions of times.

    Attributes:
        name: The name that the node advertises.
        hue: The first byte of its public key. This byte is the hash of the node in a
            path, and it selects the hue of its name on each screen. The world gives
            each node its own byte, so that no two nodes of the world have the same hash,
            and the hues of the repeaters stay far apart.
        node_type: The advert type (``NODE_TYPE_*``).
        lat: The latitude of the node. The link model always uses it.
        lon: The longitude of the node.
        site_db: The gain of the site, in dB, against a node at street level: a mountain
            top is worth more than a roof, and a roof more than a pocket.
        shares_location: Whether the adverts of the node carry its position. A node that
            does not share it still has one, because the link model needs it.
        added: Whether the node is in the contact list of our device. A node that is not
            added is a discovered node: MeshTerm only heard it.
        first_heard_days: How many days before now our node first heard this node.
        quiet_hours: How many hours before now the node transmitted for the last time.
            Zero is a node that is active now. ``None`` is a contact that our node never
            heard (a contact added from a shared card).
        talk: How often the node starts a transmission, relative to an ordinary node.
        public_key: The full public key (64 hex digits): the hue byte, then a fixed digest
            of the name.
    """

    name: str
    hue: int
    node_type: int
    lat: float
    lon: float
    site_db: float
    shares_location: bool = True
    added: bool = True
    first_heard_days: float = 120.0
    quiet_hours: float | None = 0.0
    talk: float = 1.0
    public_key: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Derive the public key from the hue byte and the name."""
        tail = hashlib.sha256(f"meshterm-demo/{self.name}".encode()).digest()[1:]
        object.__setattr__(self, "public_key", (bytes([self.hue]) + tail).hex())

    @property
    def node_id(self) -> str:
        """The canonical id of the node in the history: the first 12 hex digits of its key."""
        return self.public_key[:12]

    @property
    def hash(self) -> str:
        """The one-byte path hash of the node (2 hex digits)."""
        return self.public_key[:2]

    @property
    def is_repeater(self) -> bool:
        """Whether the node relays floods."""
        return self.node_type == NODE_TYPE_REPEATER


_R, _C, _ROOM, _S = NODE_TYPE_REPEATER, NODE_TYPE_CHAT, NODE_TYPE_ROOM, NODE_TYPE_SENSOR

#: Our node: the companion that the demo device is. It stands in La Petite-Patrie, near the
#: Jean-Talon market, with an antenna on the roof of a triplex. The buildings around it
#: hide the big repeaters (refer to :data:`_OBSTRUCTED`). It hears two small repeaters on
#: the roofs nearby, Villeray and Rosemont, as a companion in the city usually does.
SELF = Node("Alex Base", 0x91, _C, 45.5350, -73.6030, 2.0, first_heard_days=400.0)

# fmt: off
#: The other nodes of the world. The repeaters stand on the high points. The companions are
#: the people of the mesh. The order is the order of the contact list on the device.
NODES: tuple[Node, ...] = (
    # The repeaters, on the mountains, the towers, and the roofs.
    Node("Mont-Royal",      0x2A, _R, 45.5060, -73.5880, 13.0, first_heard_days=380.0),
    Node("Oratoire",        0x4F, _R, 45.4924, -73.6178,  8.0, first_heard_days=300.0),
    Node("Centre-Ville",    0xBC, _R, 45.5015, -73.5690,  8.0, first_heard_days=350.0),
    Node("Stade-Olympique", 0x98, _R, 45.5580, -73.5517, 10.0, first_heard_days=360.0),
    Node("Ahuntsic",        0x05, _R, 45.5560, -73.6600,  6.0, first_heard_days=210.0),
    Node("Laval-Chomedey",  0xCF, _R, 45.5640, -73.7450,  7.0, first_heard_days=190.0),
    Node("Verdun-Quai",     0xF3, _R, 45.4555, -73.5640,  5.0, first_heard_days=240.0),
    Node("Lachine",         0x85, _R, 45.4340, -73.6800,  7.0, first_heard_days=160.0),
    Node("Pointe-Claire",   0xAA, _R, 45.4485, -73.8165,  8.0, first_heard_days=130.0),
    Node("Île-Bizard",      0x3C, _R, 45.4950, -73.8900,  6.0, first_heard_days=95.0),
    Node("Longueuil",       0x73, _R, 45.5300, -73.5150,  6.0, first_heard_days=6.0),
    Node("Brossard",        0x18, _R, 45.4570, -73.4660,  6.0, first_heard_days=70.0),
    Node("Mont-St-Bruno",   0xE1, _R, 45.5530, -73.3260, 13.0, first_heard_days=280.0),
    Node("Mont-St-Hilaire", 0x61, _R, 45.5490, -73.1570, 15.0, first_heard_days=45.0),
    Node("Villeray",        0x70, _R, 45.5470, -73.6170,  4.0, first_heard_days=250.0),
    Node("Rosemont",        0xD8, _R, 45.5420, -73.5850,  4.0, first_heard_days=170.0),
    # The room servers.
    Node("MTL BBS",         0x3B, _ROOM, 45.5225, -73.5840, 4.0, first_heard_days=330.0),
    Node("Café Mesh",       0x64, _ROOM, 45.5250, -73.5990, 3.0, first_heard_days=60.0,
         quiet_hours=9 * 24.0),
    # The sensors.
    Node("Verdun Weather",  0x58, _S, 45.4585, -73.5660, 3.0, first_heard_days=200.0,
         talk=0.6),
    Node("Beaver Lake",     0x9E, _S, 45.4985, -73.5970, 4.0, first_heard_days=150.0,
         talk=0.6),
    Node("River Gauge",     0xCB, _S, 45.5110, -73.5470, 2.0, first_heard_days=110.0,
         talk=0.6, quiet_hours=30.0),
    # The companions: the people of the mesh.
    Node("Marie-Ève",       0xE6, _C, 45.5255, -73.5790, 1.0, first_heard_days=320.0,
         talk=1.6),
    Node("Jean-Seb",        0xAD, _C, 45.5590, -73.6650, 2.0, first_heard_days=300.0,
         talk=1.4),
    Node("Félix",           0x1A, _C, 45.5450, -73.5440, 2.0, first_heard_days=280.0,
         talk=1.2),
    Node("Dave-Lachine",    0x84, _C, 45.4385, -73.6710, 2.0, first_heard_days=150.0),
    Node("Kim-NDG",         0xBF, _C, 45.4735, -73.6145, 1.0, first_heard_days=260.0),
    Node("Nico R.",         0x94, _C, 45.5110, -73.5695, 0.0, shares_location=False,
         first_heard_days=90.0, talk=1.3),
    Node("Noor",            0x50, _C, 45.4960, -73.6300, 1.0, first_heard_days=75.0),
    Node("Chloé L.",        0xC9, _C, 45.5170, -73.6060, 1.0, first_heard_days=220.0),
    Node("Sam Verdun",      0x68, _C, 45.4610, -73.5690, 1.0, first_heard_days=240.0),
    Node("Steve K.",        0x22, _C, 45.5340, -73.5050, 1.0, first_heard_days=40.0),
    Node("Olivier.T",       0x33, _C, 45.5455, -73.5770, 1.0, first_heard_days=180.0,
         quiet_hours=50.0),
    Node("Raphaël",         0x0B, _C, 45.5810, -73.7480, 1.0, first_heard_days=130.0,
         quiet_hours=3.0),
    Node("Ana Brossard",    0x3F, _C, 45.4550, -73.4710, 1.0, first_heard_days=65.0,
         quiet_hours=14.0),
    Node("Léa",             0xDD, _C, 45.5450, -73.6240, 1.0, shares_location=False,
         first_heard_days=200.0, talk=0.5),
    Node("Rachid",          0x60, _C, 45.5240, -73.6000, 1.0, shares_location=False,
         first_heard_days=100.0, quiet_hours=4 * 24.0, talk=0.5),
    Node("Tomás",           0x77, _C, 45.4520, -73.8120, 1.0, first_heard_days=160.0,
         quiet_hours=62 * 24.0, talk=0.5),
    Node("Mathilde",        0xF7, _C, 45.4780, -73.5870, 0.0, first_heard_days=2 / 24),
    Node("Gabriel",         0x7F, _C, 45.5650, -73.2050, 1.0, first_heard_days=20.0),
    Node("Sylvie-Gaspésie", 0x6C, _C, 48.8300, -64.4800, 0.0, shares_location=False,
         first_heard_days=0.0, quiet_hours=None),
    # Discovered nodes: heard, but nobody added them.
    Node("T1000-E",         0x4C, _C, 45.5600, -73.7100, 0.0, added=False,
         first_heard_days=3.0, talk=0.4),
    Node("Wio Tracker",     0xA0, _C, 45.4650, -73.5480, 0.0, added=False,
         first_heard_days=12.0, quiet_hours=20.0, talk=0.4),
    Node("Mobile Node",     0xB2, _C, 45.5200, -73.5300, 0.0, shares_location=False,
         added=False, first_heard_days=1.5, talk=0.4),
)
# fmt: on

#: Each node of the world by name, our node included.
BY_NAME: dict[str, Node] = {node.name: node for node in (SELF, *NODES)}

#: The repeaters, in the order of :data:`NODES`.
REPEATERS: tuple[Node, ...] = tuple(node for node in NODES if node.is_repeater)


def node(name: str) -> Node:
    """The node of the world with this name. A name that the world does not have is a bug."""
    return BY_NAME[name]


# --------------------------------------------------------------------------- the radio

#: The weakest SNR (dB) at which the radio still decodes a packet. The demo device uses the
#: North American preset (SF7 at 62.5 kHz), whose demodulator stops near -7.5 dB.
FLOOR_DB = -7.5

#: The SNR (dB) of a link between two nodes at street level, one kilometre apart.
_ONE_KM_DB = 5.0

#: The loss (dB) for each decade of distance. Free space loses 20 dB for each decade. A
#: city loses more, because of the buildings.
_DECADE_DB = 25.0

#: Above this SNR (dB), a reading grows slowly: a receiver gives no SNR much above +12 dB,
#: whatever the strength of the signal.
_SOFT_CAP_DB = 10.0


#: The extra loss (dB) of a link from or to a companion of the people of the mesh. Such a
#: node is indoors, or in a pocket, at street level, so the buildings around it take more
#: than the distance explains. A link between two companions takes it two times. Thus a
#: companion reaches the repeaters near it, and another companion only when the two are
#: close, as on a real mesh. Our node has its antenna on a roof, and the room servers and
#: the sensors are outdoors, so they do not take it.
_GROUND_DB = 7.0

#: Links that the terrain blocks more than the model knows, by the names of their two
#: ends, in dB of extra loss. The buildings around our node hide the big repeaters from
#: it. Thus each flood reaches our node through the two small repeaters near it, usually
#: one copy from each. This is what a real companion in Montréal hears: two copies of most
#: messages, on paths that share their start and divide near the end.
_OBSTRUCTED: dict[frozenset[str], float] = {
    frozenset((SELF.name, name)): 14.0
    for name in ("Mont-Royal", "Oratoire", "Centre-Ville", "Stade-Olympique", "Ahuntsic")
}


def _ground_loss(a: Node, b: Node) -> float:
    """The loss that the street level adds to a link (refer to :data:`_GROUND_DB`)."""
    return _GROUND_DB * (_indoors(a) + _indoors(b))


def _indoors(node: Node) -> bool:
    """Whether ``node`` is a companion at street level: each one except our node."""
    return node.node_type == NODE_TYPE_CHAT and node is not SELF


def _skew(a: Node, b: Node) -> float:
    """A fixed offset (dB) for the link from ``a`` to ``b``: the terrain that no model knows.

    Two parts make the offset. The larger part is the same in each direction (a hill or a
    tower between the two sites). The smaller part depends on the direction, because the
    two radios are not the same. Thus a pair of nodes never has exactly the same SNR in the
    two directions, as on a real mesh.
    """
    pair = "|".join(sorted((a.name, b.name)))
    common = hashlib.sha256(f"terrain/{pair}".encode()).digest()
    oneway = hashlib.sha256(f"radio/{a.name}>{b.name}".encode()).digest()
    return (common[0] / 255 - 0.5) * 3.0 + (oneway[0] / 255 - 0.5) * 1.0


@functools.cache
def link_snr(a: Node, b: Node) -> float | None:
    """The mean SNR (dB) at ``b`` of a packet that ``a`` transmits.

    The model is a log-distance path loss: :data:`_ONE_KM_DB` at one kilometre, less
    :data:`_DECADE_DB` for each decade of distance, plus the gain of the two sites and the
    fixed terrain offset of the link (:func:`_skew`). It is simple, but it gives what a
    real mesh shows: the mountains hear each other over 30 km, and two pockets in the city
    hear each other over one or two kilometres.

    The world is fixed, so the function keeps each answer in a cache. The history asks for
    the same links almost two million times.

    Args:
        a: The node that transmits.
        b: The node that receives.

    Returns:
        The mean SNR in dB, or ``None`` when ``b`` cannot decode ``a`` (below
        :data:`FLOOR_DB`, or the same node).
    """
    if a.name == b.name:
        return None
    km = max(haversine_km(a.lat, a.lon, b.lat, b.lon), 0.05)
    snr = _ONE_KM_DB - _DECADE_DB * math.log10(km) + a.site_db + b.site_db + _skew(a, b)
    snr -= _ground_loss(a, b) + _OBSTRUCTED.get(frozenset((a.name, b.name)), 0.0)
    if snr > _SOFT_CAP_DB:
        snr = _SOFT_CAP_DB + (snr - _SOFT_CAP_DB) * 0.25
    return round(snr, 2) if snr >= FLOOR_DB else None


def loss_chance(snr: float) -> float:
    """The probability that one packet on a link with this mean SNR is lost.

    A link well above the floor loses nothing. A link near the floor loses most packets.
    """
    return max(0.0, min(0.9, (FLOOR_DB + 4.0 - snr) / 8.0))


def reading(snr: float, rng: random.Random) -> float:
    """One SNR reading of a link: the mean, plus noise, in the quarter-dB steps of the firmware."""
    return round((snr + rng.gauss(0.0, 1.0)) * 4) / 4


def hears(a: Node, b: Node) -> bool:
    """Whether ``b`` can decode ``a``."""
    return link_snr(a, b) is not None


# --------------------------------------------------------------------------- routes


@functools.cache
def best_route(origin: Node, target: Node) -> tuple[Node, ...] | None:
    """The repeaters on the best route from ``origin`` to ``target``, in order.

    The cost of each link is one hop, plus a penalty for a weak link. Thus the route has
    few hops, but it does not go over a link that loses half of its packets when a strong
    detour of the same length exists. A device learns a route of this type from the floods
    that it receives.

    Args:
        origin: The node at the start.
        target: The node at the end.

    Returns:
        The relays between the two ends (an empty tuple when they hear each other
        directly), or ``None`` when no route connects them.
    """
    direct = link_snr(origin, target)
    if direct is not None and direct > FLOOR_DB + 3.0:
        return ()
    costs: dict[str, float] = {}
    queue: list[tuple[float, int, str, tuple[Node, ...]]] = []
    order = 0
    for relay in REPEATERS:
        snr = link_snr(origin, relay)
        if snr is not None and relay.name != origin.name:
            order += 1
            heapq.heappush(queue, (_link_cost(snr), order, relay.name, (relay,)))
    best: tuple[float, tuple[Node, ...]] | None = None
    if direct is not None:
        best = (_link_cost(direct), ())
    while queue:
        cost, _, name, route = heapq.heappop(queue)
        if name in costs:
            continue
        costs[name] = cost
        last = route[-1]
        tail = link_snr(last, target)
        if tail is not None:
            total = cost + _link_cost(tail)
            if best is None or total < best[0]:
                best = (total, route)
        for relay in REPEATERS:
            if relay.name in costs or relay.name == origin.name:
                continue
            snr = link_snr(last, relay)
            if snr is not None:
                order += 1
                heapq.heappush(queue, (cost + _link_cost(snr), order, relay.name, (*route, relay)))
    return best[1] if best is not None else None


def _link_cost(snr: float) -> float:
    """The cost of one link for :func:`best_route`: one hop, more when the link is weak."""
    return 1.0 + 4.0 * loss_chance(snr)


#: The nodes whose floods do not use three-byte hashes, and the width that they use. Our
#: node uses three bytes, as most of the mesh does.
_NARROW_PATHS: dict[str, int] = {
    "Steve K.": 2,
    "Raphaël": 2,
    "Olivier.T": 1,
    "Tomás": 1,
    "T1000-E": 1,
    "Wio Tracker": 1,
    "Mobile Node": 1,
}


def path_width(origin: Node) -> int:
    """The bytes of each hash in the path of a flood from ``origin``.

    The node that starts a flood sets the width, and each relay adds its hash at that
    width. Most nodes of the mesh of Montréal use three bytes, a few use two, and some
    older ones still use one (:data:`_NARROW_PATHS`).
    """
    return _NARROW_PATHS.get(origin.name, 3)


@dataclass(frozen=True, slots=True)
class Copy:
    """One copy of a flood that our node hears.

    Attributes:
        relays: The repeaters that the copy went through, in order. Empty when our node
            heard the origin directly.
        snr: The SNR of the reception at our node (dB), from the last relay.
        delay_s: The time from the transmission at the origin to the reception (seconds).
    """

    relays: tuple[Node, ...]
    snr: float
    delay_s: float


def flood(origin: Node, rng: random.Random, *, listener: Node = SELF) -> list[Copy]:
    """The copies of one flood from ``origin`` that ``listener`` hears, earliest first.

    Each repeater relays the first copy that it receives, and only that copy. Thus the
    function runs the flood as a race: each link adds the airtime of the packet and the
    random delay of the relay, and each repeater keeps the route of its first copy. Our node
    hears the origin directly when it is in range, and each repeater in range that the
    flood reached. A weak link can lose a copy (:func:`loss_chance`). Thus two floods from
    the same node do not always arrive on the same paths.

    Args:
        origin: The node that transmits the flood.
        rng: The source of the delays, the losses, and the noise.
        listener: The node that hears the copies (our node by default).

    Returns:
        The copies that the listener receives.
    """
    arrivals: dict[Node, tuple[float, tuple[Node, ...]]] = {}
    queue: list[tuple[float, int, Node, tuple[Node, ...]]] = []
    skip = (origin, listener)
    order = 0
    for relay, snr in _hearers(origin):
        if relay in skip or rng.random() < loss_chance(snr):
            continue
        order += 1
        heapq.heappush(queue, (_airtime(rng), order, relay, (relay,)))
    while queue:
        when, _, relay, route = heapq.heappop(queue)
        if relay in arrivals:
            continue
        arrivals[relay] = (when, route)
        sent = when + rng.uniform(0.2, 1.4)  # the random transmit delay of the repeater
        for other, snr in _hearers(relay):
            if other in arrivals or other in skip or rng.random() < loss_chance(snr):
                continue
            order += 1
            heapq.heappush(queue, (sent + _airtime(rng), order, other, (*route, other)))
    copies: list[Copy] = []
    direct = link_snr(origin, listener)
    if direct is not None and rng.random() >= loss_chance(direct):
        copies.append(Copy((), reading(direct, rng), _airtime(rng)))
    for when, route in arrivals.values():
        relay = route[-1]
        snr = link_snr(relay, listener)
        if snr is None or rng.random() < loss_chance(snr):
            continue
        sent = when + rng.uniform(0.2, 1.4)
        copies.append(Copy(route, reading(snr, rng), sent + _airtime(rng)))
    copies.sort(key=lambda copy: copy.delay_s)
    return copies


@functools.cache
def _hearers(node: Node) -> tuple[tuple[Node, float], ...]:
    """The repeaters that hear ``node``, each with the mean SNR of the link."""
    return tuple((relay, snr) for relay in REPEATERS if (snr := link_snr(node, relay)) is not None)


def _airtime(rng: random.Random) -> float:
    """The time on the air of one short packet at SF7, with the time to decode it (seconds)."""
    return rng.uniform(0.15, 0.4)


@dataclass(frozen=True, slots=True)
class TraceWalk:
    """The result of one simulated trace along a forced path.

    Attributes:
        snrs: The SNR reading at the receiving end of each link, in order: the first relay,
            then each next relay, then our node for the last link. It is shorter than the
            walk when the trace died on the way.
        success: Whether the trace came back to our node.
        rtt_ms: The round trip of the trace (milliseconds), when it came back.
    """

    snrs: list[float] = field(default_factory=list)
    success: bool = False
    rtt_ms: float | None = None


def walk(relays: Sequence[Node], rng: random.Random) -> TraceWalk:
    """Simulate one trace from our node through ``relays`` and back to our node.

    A trace is a direct packet: each relay transmits it to the next node of the path, and
    measures the SNR of what it received. The last link comes back to our node. A link
    that does not exist, or a link that loses the packet, stops the trace.

    Args:
        relays: The forced path, as nodes.
        rng: The source of the noise and the losses.

    Returns:
        The readings, and whether the trace came back.
    """
    chain = [SELF, *relays, SELF]
    snrs: list[float] = []
    elapsed = 0.0
    for a, b in pairwise(chain):
        snr = link_snr(a, b)
        if snr is None or rng.random() < loss_chance(snr):
            return TraceWalk(snrs, False, None)
        snrs.append(reading(snr, rng))
        elapsed += _airtime(rng) + rng.uniform(0.05, 0.25)
    return TraceWalk(snrs, True, round(elapsed * 1000.0, 1))


# --------------------------------------------------------------------------- channels


@dataclass(frozen=True, slots=True)
class Channel:
    """One channel in a slot of the demo device.

    Attributes:
        slot: The slot index on the device.
        name: The name of the channel as the device stores it.
        secret: The 16-byte secret.
        scope: The region under which MeshTerm sends on this channel, or ``None`` for the
            default scope of the device.
    """

    slot: int
    name: str
    secret: bytes
    scope: str | None = None


#: The channels of the demo device. ``Family`` is a private channel, with a fixed secret
#: that is not derived from its name.
CHANNELS: tuple[Channel, ...] = (
    Channel(0, "Public", DEFAULT_PUBLIC_SECRET),
    Channel(1, "#montreal", derive_secret("#montreal"), scope="yul"),
    Channel(2, "#cycling", derive_secret("#cycling")),
    Channel(3, "Family", hashlib.sha256(b"meshterm-demo/Family").digest()[:16]),
)

#: Hashtag channels that other people of the mesh use and our device does not have. Our
#: node hears their packets, but it cannot read them.
FOREIGN_CHANNELS: tuple[bytes, ...] = tuple(
    derive_secret(name) for name in ("#quebec", "#radioamateur", "#test", "#plateau")
)

# --------------------------------------------------------------------------- regions

#: The region table of each repeater, as ``(name, parent, allows floods)`` rows, in the
#: form that :class:`~meshterm.core.region_sim.SimulatedRegionMap` takes. The regions are
#: the ones of the real mesh, each in the one before it: ``na`` (North America), ``can``
#: (Canada), ``onqc`` (Ontario and Québec), ``qc`` (Québec), and ``yul`` (Montréal).
#: Each repeater of Montréal, Laval, and the south shore carries all five.
#: Mont-St-Hilaire is outside the city: it has ``yul`` in its table, but it does not
#: relay its floods.
REGION_TABLES: dict[str, list[tuple[str, str, bool]]] = {}

_REGIONS = [
    ("na", "*", True),
    ("can", "na", True),
    ("onqc", "can", True),
    ("qc", "onqc", True),
    ("yul", "qc", True),
]
for _name in (
    "Oratoire",
    "Centre-Ville",
    "Stade-Olympique",
    "Verdun-Quai",
    "Ahuntsic",
    "Villeray",
    "Rosemont",
    "Mont-Royal",
    "Lachine",
    "Pointe-Claire",
    "Île-Bizard",
    "Laval-Chomedey",
    "Longueuil",
    "Brossard",
    "Mont-St-Bruno",
):
    REGION_TABLES[_name] = _REGIONS
REGION_TABLES["Mont-St-Hilaire"] = [*_REGIONS[:-1], ("yul", "qc", False)]

#: The contacts to which the device learned a route. A real device learns few routes: it
#: learns one only from a flood that it received from the node, and forgets it when a send
#: fails. Thus most contacts of the demo stay on flood, as on hardware.
ROUTED: tuple[str, ...] = (
    "Mont-Royal",
    "Stade-Olympique",
    "MTL BBS",
    "Marie-Ève",
    "Jean-Seb",
    "Dave-Lachine",
    "Nico R.",
    "Mathilde",
)


def device_route(target: Node) -> tuple[str, ...] | None:
    """The route that the demo device holds for ``target``, as path hashes.

    Returns:
        The hashes of the relays (empty for a direct neighbour), or ``None`` when the
        device holds no route (the contact is on flood).
    """
    if target.name not in ROUTED:
        return None
    route = best_route(SELF, target)
    return None if route is None else tuple(relay.hash for relay in route)


#: The repeaters for which our node remembers the admin password. The demo device accepts
#: this password on each repeater, as the simulator does.
ADMIN_PASSWORD = "admin"
ADMINISTERED: tuple[str, ...] = ("Mont-Royal", "Stade-Olympique")

#: The room that our node joined, and its room password (the stock password of a room
#: server, which the simulator accepts).
JOINED_ROOM = "MTL BBS"
ROOM_PASSWORD = "hello"
