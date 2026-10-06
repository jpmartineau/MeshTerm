# SPDX-License-Identifier: Apache-2.0
"""Find the paths on which a chat message arrived, from the stored packet log.

The RX log of the device records each packet that the device decodes, with the relay path
that the packet went through. The recorder always stored these packets as ``packet``
observations. Thus a message that was heard more than once left one logged packet for each
arrival, each with its own path and SNR. For example, several repeaters relayed a channel
broadcast, or a flood reached our node on two paths. This module matches these packets to
a chat message. Thus the chat screen can show, when the user asks, each path on which the
message reached our node:

* **Channel messages** match by content. A ``GRP_TXT`` packet identifies its channel only
  by a one-byte hash. But MeshTerm has the key of the channel, so
  :func:`~meshterm.core.channels.decrypt_channel_text` can confirm the channel by MAC and
  recover the plaintext. A packet whose text is equal to the text of the message is one
  arrival of that message. The comparison accepts the ``Name: `` sender prefix on either
  side. Our own broadcasts are included, because the device overhears the relay of our
  broadcast by a repeater and logs it like all other packets.
* **Direct messages** go in ECDH-encrypted ``TEXT_MSG`` packets that only the recipient
  can decrypt. Thus a match by content is not possible from the log. But such a packet
  carries its **addressing and its MAC** in clear text (:mod:`~meshterm.core.frames`): the
  one-byte key hash of each end, then a two-byte tag over the encrypted body. That MAC
  identifies the message itself: the copies of one message have the same MAC, and the
  next message has a different MAC. Thus direct packets make groups as exactly as channel
  packets do, and MeshTerm never decrypts them. Three filters apply, in this order: the
  packet went **between the two ends of this conversation**, in the **direction in which
  this message travelled** (our own sends are ``us → peer``, and a received message is
  ``peer → us``), and it carries **the same MAC** as the rest of its group.

  Then the time tells apart the messages of a conversation: the MAC group nearest to the
  time of a message is that message. One real gap remains. History that was stored before
  MeshTerm kept the MAC has no MAC. For these messages, MeshTerm matches by address,
  direction, and time, and it shows that it used this type of match. The reason is that
  a packet between the correct pair in the correct window is probably this message, but
  not certainly.

Nothing in this module transmits. It is a read-model over the repository.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ..core.channels import decrypt_channel_text, split_channel_sender
from ..core.frames import ENDPOINT_HASH_BYTES
from ..core.models import ChatMessage, Observation

if TYPE_CHECKING:
    from ..core.regions import Scope
    from ..persistence.repository import Repository

#: The span of the log that MeshTerm searches around a channel message. It is wide,
#: because an inbound message has the time of the sender's clock, and that clock can be
#: minutes different from our clock.
_CHANNEL_WINDOW = timedelta(minutes=15)

#: The span of the log that MeshTerm searches around a direct message, before the other
#: messages of the conversation make it narrower (refer to :func:`direct_window`). This
#: span alone is wide: a send is tried again for some time after it is composed, and an
#: inbound message has the time of the sender's clock.
_DIRECT_WINDOW = timedelta(seconds=90)

#: The minimum span on each side of a direct message, after its neighbours limit the
#: window. Two messages composed one second apart must each still have a span to search.
#: Without this minimum, the midpoint between them can leave one of them no span at all.
_MIN_HALF_WINDOW = timedelta(seconds=2)

#: Route types whose ``path`` field is an instruction for the route, not a record of where
#: the packet went. The sender writes the route, and the relays consume it. The library
#: gives names to the two route bits of the header (refer to its ``ROUTE_TYPENAMES``). The
#: flood types add to the path instead. Before, MeshTerm read each path here as a flood
#: path.
_ROUTED_TYPES = frozenset({"DIRECT", "TC_DIRECT"})

#: Packet classes that carry a direct (addressed) text message. This is the payload class
#: that the meshcore library names, spelled exactly as the library reports it (refer to
#: :data:`~meshterm.core.frames.ADDRESSED_CLASSES`, the lane of the packet viewer, and the
#: traffic keys of the dashboard). A name that is almost correct matches nothing here.
#: Then the mesh looks as if it never carries direct messages.
_DIRECT_TYPENAMES = frozenset({"TEXT_MSG"})

#: The number of hex digits in the endpoint hash of a packet. This hash is the first byte
#: of a public key. Both ends of a direct packet are addressed by this byte.
_HASH_CHARS = 2 * ENDPOINT_HASH_BYTES


@dataclass(slots=True)
class Arrival:
    """One logged copy of a message that reached the device.

    Attributes:
        when: The time when the packet was heard.
        hops: The relay path of the packet, in the order of propagation (empty means that
            the packet arrived direct).
        snr: The SNR of the reception in dB. It is the SNR from the last relay only, the
            same as for each packet row.
        resend: The resend counter of the sender for this copy (0 is the original send).
            It comes from a decrypted channel packet. It is always 0 for direct packets.
        routed: Whether the packet was **direct-routed** or flooded. This value decides
            what :attr:`hops` means. A flooded packet adds to its path (each relay adds
            itself), so the hops are the route on which the packet reached our node. A
            direct-routed packet carries a route that its sender wrote and its relays
            consume. Thus the hops are the part of the route that the packet still had to
            go through. An empty path means that the packet used all of its route, not
            that the packet arrived in a single hop. ``None`` for history that was stored
            before MeshTerm kept the route type.
        copies: The number of logged packets that this row represents. The matchers
            always make it ``1``. It becomes more after :func:`collapse` combines a path
            heard several times into one row. A direct send is tried again, and the device
            overhears each retry from each repeater in range. Thus one message often
            leaves a dozen packets on two paths. A list of each packet said "twelve
            arrivals", but the truth was "two paths, six times each".
        frame: The raw payload of the logged packet. :func:`message_scope` reads the scope
            of the message from it. It is not part of the identity of an arrival (two
            copies on one path are one row, whatever else their packets carry). Thus
            equality does not use it.
    """

    when: datetime
    hops: tuple[str, ...]
    snr: float | None
    resend: int = 0
    routed: bool | None = None
    copies: int = 1
    frame: dict | None = field(default=None, compare=False, repr=False)

    @property
    def route_known(self) -> bool:
        """Whether :attr:`hops` tells how this copy travelled.

        False for a direct-routed packet that carries no path. The packet consumed its
        route on the way. The field is empty because nothing of the route remains, not
        because the packet went through no relays. MeshTerm once drew such a packet as a
        zero-hop arrival, and thus claimed a neighbour that we do not have: the
        "impossible direct path" (JP, 2026-09-02).
        """
        return bool(self.hops) or not self.routed


def _frame_hops(observation: Observation) -> tuple[str, ...]:
    """The path field of an observation, as hop hashes.

    Refer to :attr:`Arrival.routed` for what the hops mean.
    """
    return tuple(h for h in (observation.path or "").split(",") if h)


def _frame_routed(raw: dict) -> bool | None:
    """Whether a packet was direct-routed, or ``None`` when its route type was not kept."""
    name = str(raw.get("route_typename") or "").upper()
    return name in _ROUTED_TYPES if name and name != "UNK" else None


def _texts_match(wire: str, stored: str) -> bool:
    """Whether a text from the air and a stored chat text are the same message body.

    MeshTerm stores an inbound message exactly as it came from the air (with the sender
    prefix), so an exact match covers it. MeshTerm stores our own outbound messages as
    the user typed them, but the device adds the ``Name: `` sender prefix. Thus the text
    from the air also matches when its body without the prefix is equal to the stored
    text. The comparison ignores whitespace at the two ends, but it is otherwise exact.
    Paths are evidence, and a fuzzy match can invent false evidence.
    """
    wire, stored = wire.strip(), stored.strip()
    if wire == stored:
        return True
    _sender, body = split_channel_sender(wire)
    body = body.strip()
    return bool(body) and body == stored


def channel_arrivals(
    repo: Repository,
    message: ChatMessage,
    *,
    channel_name: str,
    secret: bytes,
) -> list[Arrival]:
    """All the logged arrivals of a channel message, matched by decrypted content.

    Args:
        repo: The repository that holds the packet log.
        message: The chat message for which to find the arrivals (inbound or outbound).
        channel_name: The channel name of the conversation (for the decryption).
        secret: The 16-byte key of the channel.

    Returns:
        The matching arrivals, oldest first. Resends of the same message are included,
        and each carries its ``resend`` counter.
    """
    frames = repo.packet_frames_between(
        message.created_at - _CHANNEL_WINDOW, message.created_at + _CHANNEL_WINDOW
    )
    arrivals: list[Arrival] = []
    for frame in frames:
        raw = frame.raw if isinstance(frame.raw, dict) else {}
        if raw.get("payload_typename") != "GRP_TXT":
            continue
        chan_hash = raw.get("chan_hash")
        cipher_mac = raw.get("cipher_mac")
        crypted = raw.get("crypted")
        if not (chan_hash and cipher_mac and crypted):
            continue
        decrypted = decrypt_channel_text(chan_hash, cipher_mac, crypted, [(channel_name, secret)])
        if decrypted is None or not _texts_match(decrypted.text, message.text):
            continue
        arrivals.append(
            Arrival(
                when=frame.observed_at,
                hops=_frame_hops(frame),
                snr=frame.snr,
                resend=decrypted.attempt,
                routed=_frame_routed(raw),
                frame=raw,
            )
        )
    return arrivals


def _endpoint_hash(key: str | None) -> str:
    """The endpoint hash of a node: the first byte of its key, as a packet addresses it."""
    text = (key or "").strip().lower()
    return text[:_HASH_CHARS] if len(text) >= _HASH_CHARS else ""


def direct_window(repo: Repository, message: ChatMessage) -> tuple[datetime, datetime]:
    """The span of the log to search for the packets of one direct message.

    The span is :data:`_DIRECT_WINDOW` on each side. Then the other messages of the
    conversation limit it. Only the messages that travelled the **same way** limit it,
    because two different clocks stamp the two directions (refer to
    :meth:`~meshterm.persistence.repository.Repository
    .direct_message_bounds`). Without a limit, the flat window is much too wide at the speed
    of a conversation. Nine messages of one real exchange were all in a single ±90 s
    window, so each of them claimed the packets of all nine messages.

    The two directions get different limits, because we know very different things about
    them:

    * **Our own sends** get the time of our own clock when they leave. Thus no packet of a
      send can come before it. The window opens at the message (minus a short time for
      jitter) and goes to the next send. Retries come after the send, so all the span
      goes forward.
    * **A received message** has the time of the sender's clock, which can be before or
      after the time when we heard it. Its window stays centred, and it goes halfway to
      the messages before and after it.

    The window is never narrower than :data:`_MIN_HALF_WINDOW` on each side. Thus two
    messages one second apart still each have a span to search.

    Args:
        repo: The repository that holds the messages.
        message: The message around which to limit a search.

    Returns:
        ``(start, end)`` for :meth:`~meshterm.persistence.repository.Repository
        .packet_frames_between`.
    """
    at = message.created_at
    start, end = at - _DIRECT_WINDOW, at + _DIRECT_WINDOW
    previous, following = repo.direct_message_bounds(message.peer, at, outbound=message.outbound)
    if message.outbound:
        # Our own clock stamps it, so no packet of this send comes before it. All the
        # window goes forward, up to the next send.
        start = at - _MIN_HALF_WINDOW
        if following is not None:
            end = min(end, max(at + _MIN_HALF_WINDOW, following))
    else:
        if previous is not None:
            start = max(start, min(at - _MIN_HALF_WINDOW, previous + (at - previous) / 2))
        if following is not None:
            end = min(end, max(at + _MIN_HALF_WINDOW, at + (following - at) / 2))
    return start, end


def _addressed(raw: dict) -> tuple[str, str]:
    """A packet's ``(source, destination)`` endpoint hashes, in lower case (empty if absent)."""
    return (
        str(raw.get("src_hash") or "").lower(),
        str(raw.get("dest_hash") or "").lower(),
    )


def _pick_group(groups: dict[str, list[Arrival]], message: ChatMessage) -> list[Arrival]:
    """The MAC group that is this message, from the groups in the window.

    Each group holds the packets of one message: the same MAC, thus the same encrypted
    body. The log cannot tell which group is this message. Thus the clock decides, and it
    decides well. The packets of a send start at the time when it was composed, and they
    continue through the retries. Thus the correct group is the earliest one that did not
    start before the message. The fallback to the nearest group covers an inbound message,
    whose time comes from the sender's clock and can be a little after the packet that we
    heard.
    """
    if not groups:
        return []
    at = message.created_at
    started = {mac: min(a.when for a in arrivals) for mac, arrivals in groups.items()}
    after = [mac for mac, first in started.items() if first >= at]
    if after:
        return groups[min(after, key=lambda mac: started[mac])]
    return groups[min(started, key=lambda mac: abs(started[mac] - at))]


def direct_arrivals(
    repo: Repository,
    message: ChatMessage,
    *,
    self_key: str | None = None,
    peer_key: str | None = None,
) -> tuple[list[Arrival], bool]:
    """All the logged packets of one direct message, and whether the match is exact.

    Three filters apply (refer to the module docstring). The packet went between the two
    ends of this conversation, in the direction in which this message travelled. Also,
    where the log kept the MAC, the packet has the same MAC as the rest of its group. The
    MAC lets MeshTerm separate the packets of one message from the packets of the next
    message, without decryption of either.

    **Direction is more important than it seems.** The hashes of both ends are on each
    packet of a conversation, in both directions. Thus a match on the pair alone put our
    own outgoing packets under a message that we received (not sent), and the opposite.
    That caused the improbable one-hop rows. They were our own sends, overheard when the
    two repeaters in range relayed them back. They correctly showed one hop, because they
    had travelled only one hop. But they showed under an inbound message as if they were
    its route to our node.

    Args:
        repo: The repository that holds the packet log.
        message: The chat message for which to find the packets.
        self_key: The key (or key prefix) of our node. It is one end of each packet here.
        peer_key: The key (or key prefix) of the conversation peer: the other end.

    Returns:
        ``(arrivals, exact)``: the packets oldest first, and whether a MAC identified them
        as one message. If not, MeshTerm only correlated them by address and time.
    """
    ours, theirs = _endpoint_hash(self_key), _endpoint_hash(peer_key)
    # The direction of this message. Thus the packets of a send never show under a
    # received message, and the packets of a received message never show under a send.
    if message.outbound:
        want_src, want_dest = ours, theirs
    else:
        want_src, want_dest = theirs, ours

    start, end = direct_window(repo, message)
    groups: dict[str, list[Arrival]] = {}
    loose: list[Arrival] = []
    for frame in repo.packet_frames_between(start, end):
        raw = frame.raw if isinstance(frame.raw, dict) else {}
        if raw.get("payload_typename") not in _DIRECT_TYPENAMES:
            continue
        src, dest = _addressed(raw)
        if (want_src and src != want_src) or (want_dest and dest != want_dest):
            continue
        arrival = Arrival(
            when=frame.observed_at,
            hops=_frame_hops(frame),
            snr=frame.snr,
            routed=_frame_routed(raw),
            frame=raw,
        )
        mac = str(raw.get("cipher_mac") or "").lower()
        if mac:
            groups.setdefault(mac, []).append(arrival)
        else:
            loose.append(arrival)
    if groups:
        return _pick_group(groups, message), True
    # History stored before MeshTerm kept the MAC: only the address, direction, and time
    # are available. The caller says so, and does not claim more.
    return loose, False


def collapse(arrivals: Sequence[Arrival]) -> list[Arrival]:
    """Combine the arrivals on the same path into one row for each path, and count the copies.

    A direct send is tried again, and the device overhears each retry from each repeater in
    range. Thus one message leaves a dozen packets on two paths. The graph always drew the
    distinct paths. But the row list showed each packet, and that read as twelve arrivals,
    when the truth was two paths heard six times each. Each row that remains keeps the time
    of its **first** packet and its **best** SNR. The earliest time is when the path first
    worked, and the strongest SNR is what the path can do.

    Args:
        arrivals: The matched packets, oldest first.

    Returns:
        One :class:`Arrival` for each distinct path, in the order first heard, each with
        its :attr:`~Arrival.copies` count.
    """
    # The key has the route type and the hops. The same hashes have two different meanings
    # under the two types. If the two are combined, a route that the packet travelled
    # merges with a route that the sender intended.
    folded: dict[tuple, Arrival] = {}
    for arrival in arrivals:
        key = (arrival.hops, arrival.routed)
        seen = folded.get(key)
        if seen is None:
            folded[key] = replace(arrival, copies=1)
            continue
        best = (
            seen.snr
            if arrival.snr is None
            else (arrival.snr if seen.snr is None else max(seen.snr, arrival.snr))
        )
        folded[key] = replace(seen, snr=best, copies=seen.copies + 1)
    return list(folded.values())


def message_scope(
    arrivals: Sequence[Arrival], scope_of: Callable[[dict | None], Scope | None]
) -> Scope | None:
    """The one scope under which a message was flooded, read from the packets of its arrivals.

    A transport code is a keyed hash of the payload only. Neither the header nor the path
    is in it (refer to :mod:`~meshterm.core.regions`). Thus the copy from each relay
    carries the code of the sender without change. One message has **one** scope, however
    many paths it arrived on. For this reason, the dialog shows the scope once, not for
    each arrival row.

    But the packet that gives the answer is still important, because not each copy is a
    flood: a direct-routed copy has no scope at all. Thus the first scoped reading wins,
    and a named one wins over one that no known region reproduces. (They are the same
    code, but a name is the better answer when a copy gives one.) If there is no scoped
    reading, any plain flood copy makes the message ``unscoped``. A message heard only as
    direct-routed has no scope to show.

    Args:
        arrivals: The arrivals of the message, as the matchers (or :func:`collapse`)
            return them. Each carries its logged packet.
        scope_of: Reads the scope of a packet (``ctx.region_store.scope_of``).

    Returns:
        The scope of the message, or ``None`` when no copy was a flood.
    """
    unscoped: Scope | None = None
    unnamed: Scope | None = None
    for arrival in arrivals:
        scope = scope_of(arrival.frame)
        if scope is None:
            continue
        if not scope.scoped:
            unscoped = unscoped or scope
        elif scope.region:
            return scope
        else:
            unnamed = unnamed or scope
    return unnamed or unscoped


def distinct_paths(arrivals: list[Arrival]) -> int:
    """The number of distinct relay paths that a set of arrivals covers."""
    return len({a.hops for a in arrivals})
