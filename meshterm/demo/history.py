# SPDX-License-Identifier: Apache-2.0
"""The history of the demo: what our node heard and did before the demo started.

At each start, ``--demo`` writes a new database with this module. The database holds what
the companion of our node would have stored after months on the mesh of Montréal:

* the adverts of each node since our node first heard it, and the traffic of the last
  eight days, packet by packet, each copy on its own path (:mod:`meshterm.demo.traffic`),
* the conversations of :mod:`meshterm.demo.script`: the stored messages, and the packets
  that carried them, encrypted as the firmware encrypts them, so that the paths of a
  message can be found again,
* the traces that our node ran (:data:`~meshterm.demo.script.TRACES`), each try in the
  link model, and the records that the walks set in the trophy case,
* the neighbour tables of the repeaters that our node administers.

Each time comes from ``now`` and a fixed age, and each random value comes from a fixed
seed. Thus two runs give the same database, with only the times moved to the new now.
"""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import datetime, timedelta

from .. import __version__
from ..core.channels import channel_identity
from ..core.models import (
    PATH_TRACE_TARGET,
    ChatMessage,
    Hop,
    NeighbourInfo,
    Observation,
    TraceResult,
)
from ..persistence.repository import Repository
from ..services.records import CATEGORY_BY_ID, walk_from_trace, walk_scores
from . import script, traffic, world

#: How many days of history the demo writes. The traffic of the last eight days is
#: complete. Each older day holds one advert from each node (refer to
#: :func:`~meshterm.demo.traffic.adverts`).
HISTORY_DAYS = 300

#: The transmit power of the demo device (dBm), stored with each trace.
TX_POWER = 22

#: The map viewport that the demo opens on: the island of Montréal, with the two shores.
MAP_VIEW = (45.5150, -73.5900, 10)


def seed(repo: Repository, now: datetime) -> None:
    """Write the full history of the demo into ``repo``.

    Args:
        repo: An empty database.
        now: The moment that the history ends at: the start of the demo.
    """
    rng = random.Random("meshterm-demo/history")
    heard = traffic.traffic(-HISTORY_DAYS * 86400.0, 0.0, "history")
    messages, carried = _conversations(now, rng)
    heard.extend(carried)
    heard.sort(key=lambda item: item.offset_s)
    traffic.stamp(heard, now)
    _store_observations(repo, now, [item.observation for item in heard if item.observation])
    # The transcripts are in the order of the row ids. Thus the oldest message goes first.
    for message in sorted(messages, key=lambda m: m.created_at):
        repo.record_chat_message(message)
    _store_traces(repo, now, rng)
    _store_neighbours(repo, now, rng)
    repo.set_map_view(*MAP_VIEW)


def _store_observations(repo: Repository, now: datetime, observations: list[Observation]) -> None:
    """Store the observations, in one ``monitor`` run for each day, as the app makes them.

    The app opens one run each time it starts to record. The demo writes one for each of
    the dense days, and one for all the older days.
    """
    by_day: dict[int, list[Observation]] = defaultdict(list)
    for obs in observations:
        age_days = (now - obs.observed_at).total_seconds() / 86400.0
        by_day[min(int(age_days), 8)].append(obs)
    for _, rows in sorted(by_day.items(), reverse=True):
        rows.sort(key=lambda obs: obs.observed_at)
        run_id = repo.start_run("monitor", {"mode": "background"}, at=rows[0].observed_at)
        count = repo.record_observations(run_id, rows)
        repo.finish_run(run_id, "ok", {"observations": count}, at=rows[-1].observed_at)


def _first_copy(origin: world.Node, offset: float, body: dict, rng: random.Random, **kwargs):
    """The copies of a flood that our node must have received, because the script says so.

    A flood can lose all its copies on weak links. A line of the script arrived by
    definition. Thus the function runs the flood again until a copy arrives.
    """
    for _ in range(12):
        copies = traffic.heard_flood(origin, offset, body, rng, **kwargs)
        if copies:
            return copies
    raise RuntimeError(f"no flood from {origin.name} reaches our node")


def _conversations(
    now: datetime, rng: random.Random
) -> tuple[list[ChatMessage], list[traffic.Heard]]:
    """The stored messages of the script, and the packets that carried them.

    Args:
        now: The end of the history.
        rng: The source of the delays, the losses, and the noise.

    Returns:
        The messages, and the packets that our node heard of them.
    """
    messages: list[ChatMessage] = []
    heard: list[traffic.Heard] = _greetings(rng)
    for channel in world.CHANNELS:
        identity = channel_identity(channel.name, channel.secret)
        for line in script.CHANNEL_LINES.get(channel.name, ()):
            offset = -line.age_min * 60.0
            sender = world.node(line.sender)
            wire = f"{sender.name}: {line.text}"
            when = now + timedelta(seconds=offset)
            body = traffic.grp_txt_body(channel.secret, wire, when)
            if channel.scope:
                body = traffic.scoped(body, channel.scope)
            if line.sender == script.ME:
                heard.extend(traffic.heard_flood(sender, offset, body, rng, region=channel.scope))
                messages.append(
                    ChatMessage(
                        text=line.text,
                        outbound=True,
                        is_channel=True,
                        channel_id=identity,
                        channel_idx=channel.slot,
                        peer_name=channel.name,
                        scope=channel.scope or "*",
                        created_at=when,
                    )
                )
                continue
            copies = _first_copy(sender, offset, body, rng, region=channel.scope)
            heard.extend(copies)
            messages.append(
                ChatMessage(
                    text=wire,
                    is_channel=True,
                    channel_id=identity,
                    channel_idx=channel.slot,
                    snr=copies[0].observation.snr if copies[0].observation else None,
                    created_at=when,
                )
            )
    for peer_name, lines in script.DIRECT_LINES.items():
        peer = world.node(peer_name)
        for line in lines:
            offset = -line.age_min * 60.0
            when = now + timedelta(seconds=offset)
            mac = f"{rng.getrandbits(16):04x}"
            if line.sender == script.ME:
                body = {
                    "payload_typename": "TEXT_MSG",
                    "dest_hash": peer.hash,
                    "src_hash": world.SELF.hash,
                    "cipher_mac": mac,
                }
                heard.extend(_send(world.SELF, peer, offset, body, rng))
                if line.acked:
                    ack = {"payload_typename": "ACK", "ack_crc": f"{rng.getrandbits(32):08x}"}
                    heard.extend(_send(peer, world.SELF, offset + 2.5, ack, rng))
                messages.append(
                    ChatMessage(
                        text=line.text,
                        outbound=True,
                        peer=peer.node_id,
                        peer_name=peer.name,
                        acked=line.acked,
                        created_at=when,
                    )
                )
                continue
            body = {
                "payload_typename": "TEXT_MSG",
                "dest_hash": world.SELF.hash,
                "src_hash": peer.hash,
                "cipher_mac": mac,
            }
            copies = _first_copy(peer, offset, body, rng)
            heard.extend(copies)
            snr = copies[0].observation.snr if copies[0].observation else None
            messages.append(
                ChatMessage(text=line.text, peer=peer.node_id, snr=snr, created_at=when)
            )
    for room_name, lines in script.ROOM_LINES.items():
        room = world.node(room_name)
        link = world.link_snr(room, world.SELF) or 0.0
        for line in lines:
            when = now - timedelta(minutes=line.age_min)
            if line.sender == script.ME:
                messages.append(
                    ChatMessage(
                        text=line.text,
                        outbound=True,
                        peer=room.node_id,
                        peer_name=room.name,
                        acked=line.acked,
                        created_at=when,
                    )
                )
                continue
            author = world.BY_NAME.get(line.sender)
            messages.append(
                ChatMessage(
                    text=line.text,
                    peer=room.node_id,
                    snr=world.reading(link, rng),
                    author=author.public_key[:8] if author is not None else line.sender,
                    created_at=when,
                )
            )
    return messages, heard


def _greetings(rng: random.Random) -> list[traffic.Heard]:
    """The adverts that the people of the script send when they come online to talk.

    A person who opens the app to write often sends an advert first. Thus a node that
    spoke a few minutes ago was also heard a few minutes ago, and the lists show it as
    recent. The function adds one advert before the first line of each sitting in the last
    two days (a sitting is a run of lines less than an hour apart).
    """
    lines = [
        line
        for group in (script.CHANNEL_LINES, script.DIRECT_LINES)
        for conversation in group.values()
        for line in conversation
        if line.sender != script.ME and line.age_min < 2 * 1440
    ]
    last_advert: dict[str, float] = {}
    heard: list[traffic.Heard] = []
    for line in sorted(lines, key=lambda line: -line.age_min):
        offset = -line.age_min * 60.0
        if offset - last_advert.get(line.sender, -1e12) < 3600.0:
            continue
        sender = world.node(line.sender)
        moment = offset - rng.uniform(60.0, 900.0)
        heard.extend(
            traffic.heard_flood(sender, moment, traffic.advert_body(sender), rng, advert=True)
        )
        last_advert[line.sender] = offset
    return heard


def _send(
    src: world.Node, dst: world.Node, offset: float, body: dict, rng: random.Random
) -> list[traffic.Heard]:
    """The packets that our node hears from a direct message (or its ack) from ``src`` to ``dst``.

    The message follows the route that the device holds when it holds one, else it floods.
    """
    holder, target = (dst, src) if dst is world.SELF else (src, dst)
    route = world.device_route(target) if holder is world.SELF else None
    if route is None:
        return traffic.heard_flood(src, offset, body, rng)
    relays = tuple(world.BY_NAME[_name_of(h)] for h in route)
    if src is not world.SELF:
        relays = tuple(reversed(relays))
    return traffic.heard_direct(relays, src, offset, body, rng)


def _name_of(hash_hex: str) -> str:
    """The name of the repeater with this path hash."""
    return next(node.name for node in world.REPEATERS if node.hash == hash_hex)


def _store_traces(repo: Repository, now: datetime, rng: random.Random) -> None:
    """Run each trace of the script in the link model, store each try, and score the walks.

    A trace that does not come back is tried again a minute later, as a person does, up to
    five times. Each try is a run of its own, as in the trace screen. A walk that comes
    back goes to the trophy case, through the same scoring as a live trace.
    """
    canonical = {node.hash: node.node_id for node in world.NODES}
    positions = {node.node_id: (node.lat, node.lon) for node in world.NODES if node.shares_location}
    self_pos = (world.SELF.lat, world.SELF.lon)
    for age_min, target, names in script.TRACES:
        relays = [world.node(name) for name in names]
        spec = ",".join(relay.hash for relay in relays)
        label = target or PATH_TRACE_TARGET
        for attempt in range(5):
            when = now - timedelta(minutes=age_min) + timedelta(seconds=75 * attempt)
            walked = world.walk(relays, rng)
            # The last reading of a walk that came back is the link to our node: the hop
            # without a hash that the firmware adds at the end.
            hops = [
                Hop(i, relay.hash, snr)
                for i, (relay, snr) in enumerate(zip(relays, walked.snrs, strict=False))
            ]
            if walked.success:
                hops.append(Hop(len(relays), None, walked.snrs[-1]))
            result = TraceResult(
                target=label,
                success=walked.success,
                hops=hops if walked.success else [],
                round_trip_ms=walked.rtt_ms,
                tx_power=TX_POWER,
                path_hash_bytes=1,
                timestamp=when,
            )
            params = {"target": label, "path": spec}
            run_id = repo.start_run("trace", params, at=when - timedelta(seconds=2))
            repo.record_trace(run_id, result)
            placed: list[str] = []
            derived = walk_from_trace(
                result, canonical=canonical.get, positions=positions, self_pos=self_pos
            )
            if derived is not None:
                walk_spec, route, stats = derived
                for category_id, score in walk_scores(stats).items():
                    category = CATEGORY_BY_ID[category_id]
                    row = repo.record_discovery(
                        category_id,
                        1,
                        walk_spec,
                        route,
                        score=score,
                        stats=stats.as_dict(),
                        app_version=__version__,
                        ascending=category.ascending,
                        at=when,
                    )
                    if row is not None:
                        placed.append(f"{category.title} — {category.format_score(score)}")
            summary = {
                "target": label,
                "success": result.success,
                "min_snr": result.min_snr,
                "rtt_ms": result.round_trip_ms,
                "records": placed or None,
            }
            repo.finish_run(run_id, "ok", summary, at=when)
            if walked.success:
                break


def _store_neighbours(repo: Repository, now: datetime, rng: random.Random) -> None:
    """Store two fetches of the neighbour table of each repeater that our node administers.

    A repeater lists each node that it hears directly, with the SNR that it measured, the
    same as the simulated device answers a live fetch.
    """
    for name in world.ADMINISTERED:
        repeater = world.node(name)
        for age_h in (30.0, 2.0):
            fetched = now - timedelta(hours=age_h)
            offset = -age_h * 3600.0
            entries = []
            for other in (world.SELF, *world.NODES):
                snr = world.link_snr(other, repeater)
                if snr is None or not (other is world.SELF or traffic.active(other, offset)):
                    continue
                heard_at = fetched - timedelta(seconds=rng.uniform(60.0, 4 * 3600.0))
                entries.append(
                    NeighbourInfo(other.public_key[:8], world.reading(snr, rng), heard_at)
                )
            params = {"mode": "neighbours", "repeater": repeater.name}
            run_id = repo.start_run("trace", params, at=fetched - timedelta(seconds=3))
            repo.record_neighbours(run_id, repeater.node_id, entries, at=fetched)
            summary = {"repeater": repeater.name, "neighbours": len(entries)}
            repo.finish_run(run_id, "ok", summary, at=fetched)
