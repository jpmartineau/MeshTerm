# SPDX-License-Identifier: Apache-2.0
"""The packets of the demo world: what our node hears from the mesh, packet by packet.

Two parts of the demo use this module. The history (:mod:`meshterm.demo.history`) asks it
for the packets of the past days, and stores them. The simulated device
(:mod:`meshterm.demo.device`) asks it for the packets of the next hour, and sends each one
as a live event at its time. Thus the live feed continues the stored history without a
seam.

Each transmission on the mesh goes through the link model of :mod:`meshterm.demo.world`.
A flood reaches our node on each path that the race of the relays gives
(:func:`~meshterm.demo.world.flood`), so one transmission is often heard several times,
on several paths, as on a real mesh. A direct packet follows the route of its sender, and
our node hears it only where a relay of that route is in its range. Each packet that our
node hears becomes one ``packet`` observation, with the fields that the meshcore library
gives for an overheard packet (refer to
:func:`~meshterm.core.connection.packet_observation_from_event`). An advert also becomes
one ``advert`` observation, as the companion reports it.

The rate of the traffic follows the hour of the virtual clock (:data:`RHYTHM`): the mesh is
quiet at night and busy in the evening.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..core.channels import channel_hash
from ..core.models import (
    NODE_TYPE_REPEATER,
    NODE_TYPE_ROOM,
    NODE_TYPE_SENSOR,
    Message,
    Observation,
)
from ..core.regions import region_key, scope_body, transport_code
from .script import NOW_LOCAL
from .world import (
    FOREIGN_CHANNELS,
    NODES,
    REGION_TABLES,
    REPEATERS,
    SELF,
    Copy,
    Node,
    best_route,
    flood,
    link_snr,
    loss_chance,
    path_width,
    reading,
)

#: The relative rate of the traffic for each local hour of the day, from midnight.
RHYTHM: tuple[float, ...] = (
    0.35, 0.2, 0.15, 0.12, 0.12, 0.18, 0.35, 0.6, 0.75, 0.7, 0.65, 0.7,
    0.8, 0.7, 0.65, 0.7, 0.8, 0.95, 1.0, 1.0, 1.0, 0.9, 0.7, 0.5,
)  # fmt: skip

#: The number of transmissions that start on the mesh in each minute at the busiest hour.
#: The adverts and the lines of the script come on top of this rate.
PEAK_PER_MIN = 1.1

#: The payload classes of the background traffic, with their relative weights. A flood
#: is heard several times. Thus the counts of the stored packets have other proportions:
#: the channel texts and the requests lead, as on the real mesh of Montréal.
_KINDS: tuple[tuple[str, float], ...] = (
    ("GRP_TXT", 18.0),
    ("REQ", 16.0),
    ("RESPONSE", 10.0),
    ("TEXT_MSG", 9.0),
    ("ACK", 5.0),
    ("ANON_REQ", 4.0),
    ("PATH", 3.0),
    ("TRACE", 2.0),
    ("GRP_DATA", 0.3),
    ("CONTROL", 0.3),
    ("MULTIPART", 0.05),
)

#: The 4-bit payload type of each class in the packet header (MeshCore's ``Packet.h``). The
#: transport code of a scoped flood comes from it.
_PAYLOAD_TYPE = {"GRP_TXT": 5, "ADVERT": 4}

#: How often each type of node sends a flood advert (hours), and how often a repeater
#: sends a local advert that only its neighbours hear (hours).
_ADVERT_EVERY_H = {"repeater": 12.0, "room": 12.0, "sensor": 6.0, "companion": 9.0}
_LOCAL_ADVERT_EVERY_H = 4.0


def rhythm(local_hour: float) -> float:
    """The relative rate of the traffic at a local hour of the virtual clock."""
    return RHYTHM[int(local_hour) % 24]


def local_hour(offset_s: float) -> float:
    """The local hour on the virtual clock, ``offset_s`` seconds after now (negative: before)."""
    return (NOW_LOCAL + offset_s / 3600.0) % 24.0


def _rssi(snr: float, rng: random.Random) -> float:
    """A plausible RSSI (dBm) for a reception with this SNR, over a noise floor near -110 dBm."""
    return float(round(-108.0 + 1.1 * snr + rng.gauss(0.0, 1.5)))


def active(node: Node, offset_s: float) -> bool:
    """Whether ``node`` transmits at this moment (``offset_s`` seconds after now).

    A node transmits after our node first heard it, and before it went quiet. A node that
    is active now stays active after now, so the live traffic continues the history. A
    contact that our node never heard never transmits.
    """
    if node.quiet_hours is None:
        return False
    first = -node.first_heard_days * 86400.0
    if node.quiet_hours <= 0:
        return first <= offset_s
    return first <= offset_s <= -node.quiet_hours * 3600.0


def _kind_of(node: Node) -> str:
    """The advert schedule that a node follows (refer to :data:`_ADVERT_EVERY_H`)."""
    return {
        NODE_TYPE_REPEATER: "repeater",
        NODE_TYPE_ROOM: "room",
        NODE_TYPE_SENSOR: "sensor",
    }.get(node.node_type, "companion")


def _phase(node: Node, salt: str) -> float:
    """A fixed fraction in ``[0, 1)`` for a node: where its periodic schedule starts."""
    return hashlib.sha256(f"{salt}/{node.name}".encode()).digest()[0] / 256.0


# --------------------------------------------------------------------------- packets


@dataclass(frozen=True, slots=True)
class Heard:
    """One event that our node hears: an observation or a received message.

    Attributes:
        offset_s: When it is heard, in seconds after now (negative: in the past).
        observation: The observation, or ``None``.
        message: The received message, or ``None``.
    """

    offset_s: float
    observation: Observation | None = None
    message: Message | None = None


def _mac(rng: random.Random) -> str:
    """A random 2-byte MAC (hex): the tag of an encrypted body that our node cannot read."""
    return f"{rng.getrandbits(16):04x}"


def grp_txt_body(secret: bytes, text: str, sent_at: datetime, attempt: int = 0) -> dict:
    """The fields of a GRP_TXT packet for ``text`` on the channel with ``secret``.

    The body is what the firmware sends: the timestamp of the sender and the attempt byte,
    then the text, padded to whole AES blocks and encrypted in ECB mode with the key of the
    channel. The MAC is the first two bytes of the HMAC-SHA256 of the ciphertext. Thus
    MeshTerm decrypts a stored copy exactly as it decrypts a real one (refer to
    :func:`~meshterm.core.channels.decrypt_channel_text`).

    Args:
        secret: The 16-byte key of the channel.
        text: The text of the message, with the ``Name: `` prefix of the sender.
        sent_at: The time on the clock of the sender.
        attempt: The resend counter of the sender.

    Returns:
        The ``payload_typename``, ``chan_hash``, ``cipher_mac``, and ``crypted`` fields.
    """
    from Crypto.Cipher import AES
    from Crypto.Hash import HMAC, SHA256

    plain = int(sent_at.timestamp()).to_bytes(4, "little") + bytes([attempt & 0x03])
    plain += text.encode("utf-8")
    plain += b"\x00" * (-len(plain) % 16)
    crypted = AES.new(secret, AES.MODE_ECB).encrypt(plain)
    mac = HMAC.new(secret, digestmod=SHA256)
    mac.update(crypted)
    return {
        "payload_typename": "GRP_TXT",
        "chan_hash": channel_hash(secret),
        "cipher_mac": mac.digest()[:2].hex(),
        "crypted": crypted.hex(),
    }


def scoped(body: dict, region: str) -> dict:
    """``body`` as a scoped flood: the route type, the transport code, and the HMAC input.

    Args:
        body: The fields of a GRP_TXT packet (refer to :func:`grp_txt_body`).
        region: The bare region name.

    Returns:
        A copy of ``body`` with ``route_typename``, ``transport_code``, and ``scope_body``.
    """
    payload = (
        bytes.fromhex(body["chan_hash"])
        + bytes.fromhex(body["cipher_mac"])
        + bytes.fromhex(body["crypted"])
    )
    hmac_input = scope_body(_PAYLOAD_TYPE["GRP_TXT"], payload)
    code = transport_code(region_key(region), hmac_input)
    return {
        **body,
        "route_typename": "TC_FLOOD",
        "transport_code": code.to_bytes(2, "little").hex() + "0000",
        "scope_body": hmac_input.hex(),
    }


def _relays_region(relay: Node, region: str | None) -> bool:
    """Whether ``relay`` relays a flood in ``region`` (``None``: an unscoped flood)."""
    if region is None:
        return True
    return any(name == region and allowed for name, _, allowed in REGION_TABLES.get(relay.name, ()))


def heard_flood(
    origin: Node,
    start_s: float,
    body: dict,
    rng: random.Random,
    *,
    region: str | None = None,
    advert: bool = False,
) -> list[Heard]:
    """The packets that our node hears from one flood.

    Args:
        origin: The node that transmits the flood.
        start_s: When it transmits (seconds after now).
        body: The payload fields of the packet.
        rng: The source of the delays, the losses, and the noise.
        region: The scope of the flood, or ``None`` for an unscoped flood. Only the
            repeaters that carry the region relay it.
        advert: Whether the packet is an advert. Then the companion also reports one
            ``advert`` observation, with the SNR of the first copy.

    Returns:
        One :class:`Heard` for each copy, and one more for the advert report.
    """
    copies = [
        copy
        for copy in flood(origin, rng)
        if all(_relays_region(relay, region) for relay in copy.relays)
    ]
    route = "TC_FLOOD" if region else "FLOOD"
    width = path_width(origin)
    out = [
        _packet(
            origin if advert else None,
            start_s,
            copy,
            {"route_typename": route, **body},
            rng,
            width=width,
        )
        for copy in copies
    ]
    if advert and copies:
        first = copies[0]
        out.append(Heard(start_s + first.delay_s, observation=_advert_report(origin, first, rng)))
    return out


def _packet(
    origin: Node | None,
    start_s: float,
    copy: Copy,
    body: dict,
    rng: random.Random,
    *,
    width: int = 1,
) -> Heard:
    """One ``packet`` observation for one copy of a transmission, with ``width``-byte hashes."""
    path = ",".join(relay.public_key[: 2 * width] for relay in copy.relays)
    raw = {**body, "path_len": len(copy.relays), "path_hash_size": width}
    share = origin is not None and origin.shares_location
    return Heard(
        start_s + copy.delay_s,
        observation=Observation(
            node=origin.node_id if origin is not None else None,
            public_key=origin.public_key if origin is not None else None,
            name=origin.name if origin is not None else None,
            kind="packet",
            node_type=origin.node_type if origin is not None else None,
            snr=copy.snr,
            rssi=_rssi(copy.snr, rng),
            lat=origin.lat if share else None,
            lon=origin.lon if share else None,
            path=path,
            raw=raw,
        ),
    )


def _advert_report(origin: Node, copy: Copy, rng: random.Random) -> Observation:
    """The ``advert`` observation that the companion reports for an advert that it heard."""
    return Observation(
        node=origin.node_id,
        public_key=origin.public_key,
        name=origin.name,
        kind="advert",
        node_type=origin.node_type,
        snr=copy.snr,
        rssi=_rssi(copy.snr, rng),
        lat=origin.lat if origin.shares_location else None,
        lon=origin.lon if origin.shares_location else None,
    )


def advert_body(origin: Node) -> dict:
    """The decoded fields of an advert packet from ``origin``."""
    body: dict = {
        "payload_typename": "ADVERT",
        "adv_key": origin.public_key,
        "adv_name": origin.name,
        "adv_type": origin.node_type,
    }
    if origin.shares_location:
        body.update(adv_lat=origin.lat, adv_lon=origin.lon)
    return body


def heard_direct(
    route: tuple[Node, ...], src: Node, start_s: float, body: dict, rng: random.Random
) -> list[Heard]:
    """The packets that our node hears from one direct packet along ``route``.

    The sender writes the route into the packet, and each relay removes itself before it
    transmits. Thus our node hears the sender with the full route, and each relay with
    the part of the route that is still in front of the packet.

    Args:
        route: The relays between the sender and the recipient.
        src: The sender.
        start_s: When the sender transmits (seconds after now).
        body: The payload fields of the packet.
        rng: The source of the delays, the losses, and the noise.

    Returns:
        One :class:`Heard` for each transmission that our node hears.
    """
    out: list[Heard] = []
    elapsed = 0.0
    for index, transmitter in enumerate((src, *route)):
        elapsed += rng.uniform(0.2, 0.9)
        if transmitter.name == SELF.name:
            continue
        snr = link_snr(transmitter, SELF)
        if snr is None or rng.random() < loss_chance(snr):
            continue
        remaining = route[index:]
        copy = Copy(remaining, reading(snr, rng), elapsed)
        direct = {"route_typename": "DIRECT", **body}
        out.append(_packet(None, start_s, copy, direct, rng, width=path_width(src)))
    return out


# --------------------------------------------------------------------------- traffic


def _pick(nodes: list[Node], rng: random.Random) -> Node:
    """A node, with the weight of its :attr:`~meshterm.demo.world.Node.talk`."""
    return rng.choices(nodes, weights=[node.talk for node in nodes])[0]


def adverts(start_s: float, end_s: float, rng: random.Random) -> Iterator[Heard]:
    """The adverts that our node hears between two moments (seconds after now).

    Each node sends a flood advert on a fixed period, with a fixed phase and some jitter.
    A repeater also sends local adverts, which only its neighbours hear. More than eight
    days before now, the history keeps only one advert of each node each day: enough for
    the dates of first and last reception, and not a full week of packets for each day of
    the year.
    """
    dense_from = -8 * 86400.0
    for origin in (*NODES, SELF):
        kind = _kind_of(origin)
        period = _ADVERT_EVERY_H[kind] * 3600.0
        moment = start_s - (start_s % period) + _phase(origin, "advert") * period
        while moment < end_s:
            sparse = moment < dense_from
            when = moment + rng.uniform(-0.1, 0.1) * period
            step = 86400.0 if sparse else period
            if start_s <= when < end_s and (origin is SELF or active(origin, when)):
                if origin is SELF:
                    # Our node hears its own advert only where the repeaters relay it. The
                    # history keeps these packets only for the recent days.
                    if not sparse:
                        yield from heard_flood(origin, when, advert_body(origin), rng)
                elif sparse:
                    copies = flood(origin, rng)
                    if copies:
                        yield Heard(
                            when + copies[0].delay_s,
                            observation=_advert_report(origin, copies[0], rng),
                        )
                else:
                    yield from heard_flood(origin, when, advert_body(origin), rng, advert=True)
            moment += step
        if kind != "repeater" or link_snr(origin, SELF) is None:
            continue
        period = _LOCAL_ADVERT_EVERY_H * 3600.0
        moment = max(start_s, dense_from)
        moment = moment - (moment % period) + _phase(origin, "local") * period
        while moment < end_s:
            if start_s <= moment and active(origin, moment):
                snr = link_snr(origin, SELF)
                if snr is not None and rng.random() >= loss_chance(snr):
                    copy = Copy((), reading(snr, rng), 0.3)
                    body = {"route_typename": "FLOOD", **advert_body(origin)}
                    yield _packet(origin, moment, copy, body, rng)
                    yield Heard(moment + 0.3, observation=_advert_report(origin, copy, rng))
            moment += period


def background(start_s: float, end_s: float, rng: random.Random) -> Iterator[Heard]:
    """The background traffic of the mesh between two moments (seconds after now).

    The transmissions arrive as a Poisson process, with the rate of the hour
    (:func:`rhythm`). Each one has a class (:data:`_KINDS`), a sender, and a recipient
    where its class has one. Then it becomes the packets that our node hears.
    """
    kinds = [kind for kind, _ in _KINDS]
    weights = [weight for _, weight in _KINDS]
    moment = start_s
    while True:
        rate_s = PEAK_PER_MIN * rhythm(local_hour(moment)) / 60.0
        moment += rng.expovariate(max(rate_s, 1e-6))
        if moment >= end_s:
            return
        awake = [node for node in NODES if active(node, moment)]
        people = [node for node in awake if node.node_type == SELF.node_type]
        servers = [node for node in awake if node.node_type != SELF.node_type]
        if not people or not servers:
            continue
        kind = rng.choices(kinds, weights=weights)[0]
        yield from _transmission(kind, moment, people, servers, rng)


def _transmission(
    kind: str, moment: float, people: list[Node], servers: list[Node], rng: random.Random
) -> Iterator[Heard]:
    """The packets that our node hears from one background transmission of class ``kind``."""
    sender = _pick(people, rng)
    if kind in ("GRP_TXT", "GRP_DATA"):
        secret = rng.choice(FOREIGN_CHANNELS)
        body = {
            "payload_typename": kind,
            "chan_hash": channel_hash(secret),
            "cipher_mac": _mac(rng),
            "crypted": rng.randbytes(16 * rng.randint(2, 6)).hex(),
        }
        yield from heard_flood(sender, moment, body, rng)
        return
    if kind == "CONTROL":
        # A discovery request: one hop, heard only near its sender.
        snr = link_snr(sender, SELF)
        if snr is not None:
            copy = Copy((), reading(snr, rng), 0.2)
            yield _packet(
                None, moment, copy, {"route_typename": "DIRECT", "payload_typename": kind}, rng
            )
        return
    if kind == "MULTIPART":
        yield from heard_flood(sender, moment, {"payload_typename": kind}, rng)
        return
    if kind == "TRACE":
        yield from _trace(moment, rng)
        return
    if kind in ("REQ", "ANON_REQ"):
        target = _pick(servers, rng)
    else:
        target = _pick([node for node in people if node is not sender] or people, rng)
    if kind == "RESPONSE":
        sender, target = _pick(servers, rng), sender
    body: dict = {"payload_typename": kind, "dest_hash": target.hash, "cipher_mac": _mac(rng)}
    if kind == "ANON_REQ":
        body["src_key"] = sender.public_key
    elif kind != "ACK":
        body["src_hash"] = sender.hash
    if kind == "ACK":
        body = {"payload_typename": kind, "ack_crc": f"{rng.getrandbits(32):08x}"}
    route = best_route(sender, target)
    if route is not None and kind in ("ACK", "RESPONSE", "TEXT_MSG", "REQ") and rng.random() < 0.55:
        yield from heard_direct(route, sender, moment, body, rng)
    else:
        yield from heard_flood(sender, moment, body, rng)


def _trace(moment: float, rng: random.Random) -> Iterator[Heard]:
    """A trace that another node runs, as our node hears it pass.

    The trace walks a few repeaters, out and back. Our node hears each relay of the walk
    that is in its range, with the SNR readings that the packet carries at that point.
    """
    start = rng.choice(REPEATERS)
    out = [start]
    for _ in range(rng.randint(1, 3)):
        options = [r for r in REPEATERS if r not in out and link_snr(out[-1], r) is not None]
        if not options:
            break
        out.append(rng.choice(options))
    walk = [*out, *reversed(out[:-1])]
    snrs: list[float] = []
    elapsed = 0.0
    tag = f"{rng.getrandbits(32):08x}"
    for a, b in zip(walk, walk[1:], strict=False):
        snr = link_snr(a, b)
        if snr is None or rng.random() < loss_chance(snr):
            return
        snrs.append(reading(snr, rng))
        elapsed += rng.uniform(0.2, 0.6)
        heard = link_snr(b, SELF)
        if heard is not None and rng.random() >= loss_chance(heard):
            body = {
                "route_typename": "DIRECT",
                "payload_typename": "TRACE",
                "trace_tag": tag,
                "trace_snrs": list(snrs),
            }
            yield _packet(None, moment, Copy((), reading(heard, rng), elapsed), body, rng)


def traffic(start_s: float, end_s: float, seed: str) -> list[Heard]:
    """All the traffic that our node hears between two moments, in time order.

    Args:
        start_s: The start, in seconds after now (negative: in the past).
        end_s: The end, in seconds after now.
        seed: The seed of the random source. The same seed gives the same traffic.

    Returns:
        The adverts and the background packets, sorted by time.
    """
    rng = random.Random(f"meshterm-demo/{seed}")
    heard = [*adverts(start_s, end_s, rng)]
    dense_from = max(start_s, -8 * 86400.0)
    if dense_from < end_s:
        heard.extend(background(dense_from, end_s, rng))
    heard = [item for item in heard if start_s <= item.offset_s < end_s]
    heard.sort(key=lambda item: item.offset_s)
    return heard


def stamp(heard: list[Heard], now: datetime) -> list[Heard]:
    """Give each observation in ``heard`` its time, from ``now`` and its offset."""
    for item in heard:
        if item.observation is not None:
            item.observation.observed_at = now + timedelta(seconds=item.offset_s)
    return heard
