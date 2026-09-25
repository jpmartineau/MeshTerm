# SPDX-License-Identifier: Apache-2.0
"""Domain models shared across services, tools, persistence, and visualizations.

These are plain dataclasses with no I/O dependencies so they can be constructed by the
real device, the simulator, or rehydrated from the database identically.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

#: Maps a hop's raw key-prefix hash to a display label (a contact name when known, or the
#: hash itself when not). ``None`` passes through, being our own device. THE alias for the
#: name side of a hop, shared by everything that builds one
#: (:func:`~meshterm.services.trace_runner.make_node_resolver`) and everything that takes
#: one (every screen that draws a route).
NodeResolver = Callable[[str | None], str | None]

#: Maps a display name back to the node's key — as full as we hold one: a public key, a key
#: prefix, or a stored node id — or ``None`` for a name no known node carries. The colour
#: side of :data:`NodeResolver`: where that turns hex into names, this turns a bare name back
#: into the key its palette hue derives from
#: (:func:`~meshterm.services.trace_runner.make_name_key_resolver`).
NameKeyResolver = Callable[[str], str | None]

#: Label used for our own (local) device when framing a trace path's endpoints.
LOCAL_DEVICE_LABEL = "us"

#: Sentinel ``target`` recorded on traces run by the *Trace path* feature, which walks a
#: hand-composed route with no destination node at all (a trace is one walked path; the
#: notion of a target is pure UX). The parentheses keep it from colliding with contact
#: names. History queries key on it: ``Repository.latest_trace`` seeds the path screen
#: from it, while ``Repository.traced_targets`` excludes it so composed walks never
#: surface in the target picker's "Recently traced" list.
PATH_TRACE_TARGET = "(path)"

# MeshCore advert types — the low nibble of an advert's flag byte — classify the kind of
# node an advert announces. Repeaters are fixed infrastructure and carry the mesh, so
# location-aware features (e.g. the map) prioritise them over ordinary leaf nodes.
NODE_TYPE_CHAT = 1
NODE_TYPE_REPEATER = 2
NODE_TYPE_ROOM = 3
NODE_TYPE_SENSOR = 4

#: Human-readable label for each advert type, for legends and summaries.
#: The one name for each advert type, everywhere MeshTerm prints one and in every
#: `--json` answer that carries a role or a type. A type-1 node is a companion: the radio
#: someone talks through, the same kind as the one in your hand.
NODE_TYPE_LABELS = {
    NODE_TYPE_CHAT: "companion",
    NODE_TYPE_REPEATER: "repeater",
    NODE_TYPE_ROOM: "room server",
    NODE_TYPE_SENSOR: "sensor",
}


def node_type_label(node_type: int | None) -> str | None:
    """The name for an advert type, ``type N`` for one MeshTerm does not know, None for none."""
    if node_type is None:
        return None
    return NODE_TYPE_LABELS.get(int(node_type), f"type {node_type}")


class LoginResult(Enum):
    """How an admin login ended — and, crucially, whether the node was there to end it.

    "Rejected" and "never answered" used to be the same ``False``, and every caller read
    that ``False`` as *wrong password* and dropped the remembered credential. Administering
    a repeater while it happened to be down therefore erased its password: the node had said
    nothing at all, and we took the silence as a denial.

    They are not the same claim. A refusal is the node telling us the password is wrong, and
    only that is grounds for forgetting it. Silence says nothing about the password —
    the node may be asleep, out of range, or the reply lost on the way home — so the
    credential must survive it. Truthy exactly when the session opened, so the long-standing
    ``if not await device.admin_login(...)`` still reads "we are not logged in"; the two
    failures are told apart by identity, which is what makes each call site say which one it
    is handling.
    """

    #: The node accepted the password; an admin session is open.
    ACCEPTED = "accepted"
    #: The node answered and said no. The stored password is wrong — forget it.
    REFUSED = "refused"
    #: Nothing came back. Out of reach, asleep, or the reply was lost. Keep the password.
    NO_REPLY = "no_reply"

    def __bool__(self) -> bool:
        """Truthy only when logged in, so the plain ``if not …`` idiom keeps its meaning."""
        return self is LoginResult.ACCEPTED


def is_direct_messageable(node_type: int | None) -> bool:
    """Whether a node of this advert type is a direct-message recipient — THE DM rule.

    We only send direct messages to *companion* (chat) nodes: a repeater, room server, or
    sensor is infrastructure, not someone to message. A contact whose type was never
    advertised (``None``) gets the benefit of the doubt, so a real companion is never
    hidden by a missing type. Every DM recipient picker (the chat conversation list, the
    courier outbox) filters through this one predicate, so "DMs go to companions only" is
    enforced in a single place.

    Args:
        node_type: The node's advert type (see the ``NODE_TYPE_*`` constants), or ``None``
            when it was never advertised.

    Returns:
        ``True`` for a companion node or an untyped contact; ``False`` for a repeater,
        room, or sensor.
    """
    return node_type in (NODE_TYPE_CHAT, None)


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp.

    Returns:
        The current time in UTC.
    """
    return datetime.now(timezone.utc)


#: How far into the future an advert timestamp may sit (seconds) before it is refused as a
#: lie. An advert's timestamp is stamped by the *sender's* clock, and a mesh node with no
#: time source can be minutes — or months — off; a couple of minutes of skew is ordinary and
#: harmless (the age clamps to "now" until real time catches up), but anything beyond it
#: would pin the contact at the top of every heard-sorted list, reading "now" for as long as
#: the bogus timestamp stays ahead of the wall clock.
ADVERT_FUTURE_SKEW_S = 300


def advert_time(last_advert: object) -> datetime | None:
    """Convert an advert's Unix timestamp into a *plausible* UTC datetime, or ``None``.

    THE converter for every ``last_advert`` epoch entering the app — the device's live
    contact table and the cross-session contact store both pass through here — so the
    plausibility rule lives in one place. The value is written by the advertising node's
    own clock, which makes it hearsay: a zero/absent/garbage value means "never heard",
    and a timestamp more than :data:`ADVERT_FUTURE_SKEW_S` ahead of our clock is a mis-set
    sender clock and is refused the same way. Callers with their own reception evidence
    (recorded observations) fill the resulting ``None`` from that instead — an honest
    "when *we* heard it" beats a fictional "heard in the future".

    Args:
        last_advert: The raw ``last_advert`` field from a contact payload or stored entry.

    Returns:
        A timezone-aware UTC :class:`datetime`, or ``None`` when unknown or implausible.
    """
    try:
        seconds = int(last_advert)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    when = datetime.fromtimestamp(seconds, tz=timezone.utc)
    if when > utcnow() + timedelta(seconds=ADVERT_FUTURE_SKEW_S):
        return None
    return when


@dataclass(slots=True)
class Contact:
    """A node known to the connected companion device.

    Attributes:
        name: Human-friendly name advertised by the node.
        public_key: Full public key hex string, if known.
        key_prefix: Short key prefix used to address the node.
        last_seen: When the node was last heard, if known.
        node_type: Advert type of the node (see the ``NODE_TYPE_*`` constants), if known.
        lat: Latitude the node last advertised (decimal degrees), if it shared one.
        lon: Longitude the node last advertised (decimal degrees), if it shared one.
        route_hops: The device-learned outbound route to this contact, one hex path-hash
            per repeater in order from us outward — knowledge the firmware distilled from
            the paths of *received* flood packets. An empty tuple means the firmware
            considers the contact a direct neighbour (a zero-hop route); ``None`` means
            no route has been learned (or the transport didn't report one).
    """

    name: str
    public_key: str = ""
    key_prefix: str = ""
    last_seen: datetime | None = None
    node_type: int | None = None
    lat: float | None = None
    lon: float | None = None
    route_hops: tuple[str, ...] | None = None

    @property
    def has_location(self) -> bool:
        """Whether this contact advertised a usable latitude/longitude."""
        return self.lat is not None and self.lon is not None

    @property
    def is_repeater(self) -> bool:
        """Whether this contact advertises as a repeater (fixed infrastructure)."""
        return self.node_type == NODE_TYPE_REPEATER


@dataclass(slots=True)
class MapMarker:
    """One mesh node to overlay on the map.

    Data, not drawing: a located node with the little the map needs to place and label it.
    It lives here rather than beside the renderer because it is assembled well below the UI
    — :func:`~meshterm.services.markers.gather_markers` builds the list from contacts and
    observations, and three separate surfaces draw it.

    Attributes:
        label: Node name shown beside the marker.
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.
        is_repeater: Whether the node is a repeater (prioritised marker).
        is_self: Whether this is our own node (highlighted).
        detail: Extra text for the CLI legend (e.g. ``"18 pkts · +6.0 dB"``).
        key: The node's key hex (as full as the caller holds), seeding the label's
            key-derived hue; ``None`` leaves the label the muted no-key grey.
    """

    label: str
    lat: float
    lon: float
    is_repeater: bool = False
    is_self: bool = False
    detail: str = ""
    key: str | None = None

    def _rank(self) -> int:
        """Draw order: self on top of repeaters on top of leaf nodes."""
        return 2 if self.is_self else (1 if self.is_repeater else 0)


@dataclass(slots=True)
class NeighbourInfo:
    """One entry of a remote repeater's neighbour table, fetched over the mesh.

    Repeater firmware keeps a table of the nodes it has *heard directly* (zero-hop),
    each with the SNR measured at the repeater — a vantage point ours can't reproduce.
    :meth:`~meshterm.core.connection.Device.fetch_neighbours` returns these; the values
    describe the link ``repeater ↔ node`` as observed *at the repeater*.

    Attributes:
        node: The neighbour's hex public-key prefix, lowercased (the width the firmware
            replied at — typically 8 hex).
        snr: SNR (dB) the repeater measured receiving this neighbour, if reported.
        heard_at: When the repeater last heard the neighbour (derived from the reply's
            seconds-ago field), or ``None`` when it didn't say.
    """

    node: str
    snr: float | None = None
    heard_at: datetime | None = None


@dataclass(slots=True)
class Observation:
    """A single over-the-air reception heard while passively monitoring the mesh.

    Adverts, telemetry frames, and other packets the companion overhears are captured as
    observations and logged longitudinally, so the mesh's behavior can be reviewed over
    time rather than only at the instant a command runs.

    Attributes:
        node: Key prefix / hash identifying the transmitting node, if known — the stored
            12-hex canonical id everything groups and joins on. Kept short on purpose so it
            matches the key prefixes traces, messages, and the topology graph carry.
        public_key: The node's *full* public key, when the advert carried one (it arrives
            in the payload and is truncated to :attr:`node` for the id). Display-only —
            lets a hash lane show more than the twelve stored digits — and ``None`` for a
            packet class or firmware that named only a short hash.
        name: Friendly name advertised by the node, if carried.
        kind: Packet class, e.g. ``advert`` or ``telemetry``.
        node_type: Advert type of the transmitting node (see the ``NODE_TYPE_*`` constants),
            e.g. :data:`NODE_TYPE_REPEATER`; ``None`` when the packet did not carry it.
        snr: Signal-to-noise ratio (dB) the companion measured, if reported.
        rssi: Received signal strength (dBm), if reported.
        lat: Advertised latitude (decimal degrees), when the node shares location.
        lon: Advertised longitude (decimal degrees), when the node shares location.
        path: For ``packet`` observations (the companion's RX packet log), the relay
            path the packet traversed before reaching us: comma-separated per-hop hex
            hashes in propagation order, nearest the *originator* first and the repeater
            we actually heard last. An empty string means the packet arrived direct
            (zero hops); ``None`` means the packet class carries no path. For packets,
            :attr:`snr`/:attr:`rssi` describe our reception from the *last relay* in
            this path — not the originating :attr:`node` — which is why packet rows are
            kept out of per-node reception statistics and feed topology instead.
        observed_at: When the packet was heard.
        raw: Optional raw event payload for debugging/replay.
    """

    node: str | None
    public_key: str | None = None
    name: str | None = None
    kind: str = "advert"
    node_type: int | None = None
    snr: float | None = None
    rssi: float | None = None
    lat: float | None = None
    lon: float | None = None
    path: str | None = None
    observed_at: datetime = field(default_factory=utcnow)
    raw: dict | None = None


@dataclass(slots=True)
class Message:
    """An inbound text message the companion received (direct or on a channel).

    Direct messages carry a :attr:`sender` key prefix; channel messages carry a
    :attr:`channel` index instead. Both are surfaced by the always-on event hub as
    :attr:`~meshterm.core.events.EventKind.MESSAGE` events, so client features (an inbox,
    notifications) can react to them without polling.

    Attributes:
        text: The decoded message body.
        sender: Key prefix of the sending contact (direct messages), if known.
        channel: Channel index the message arrived on (channel messages), if applicable.
        is_channel: Whether this is a channel message rather than a direct one.
        sender_timestamp: The sender's own timestamp for the message, if carried.
        snr: Signal-to-noise ratio (dB) of the reception, if reported.
        received_at: When the companion delivered the message to us.
        raw: Optional raw event payload for debugging/replay.
    """

    text: str
    sender: str | None = None
    channel: int | None = None
    is_channel: bool = False
    sender_timestamp: datetime | None = None
    snr: float | None = None
    received_at: datetime = field(default_factory=utcnow)
    raw: dict | None = None


@dataclass(slots=True)
class Ack:
    """A delivery acknowledgement for a message the companion sent.

    Attributes:
        code: The ACK correlation code (hex), matching it to the sent message, if present.
        received_at: When the acknowledgement arrived.
        raw: Optional raw event payload for debugging/replay.
    """

    code: str | None = None
    received_at: datetime = field(default_factory=utcnow)
    raw: dict | None = None


def conversation_key(is_channel: bool, channel_id: str | None, peer: str | None) -> str:
    """Return a stable key identifying a chat conversation.

    A conversation is either a channel or a direct exchange with one contact; this key
    is how history, unread counts, and the live screen all agree on which thread a message
    belongs to. It is deliberately keyed on something *intrinsic* to the conversation — a
    channel's identity (derived from its secret; see
    :func:`~meshterm.core.channels.channel_identity`) and a contact's key prefix — never on
    a slot index or list position, so reordering channels never re-points history.

    Args:
        is_channel: Whether the conversation is a channel.
        channel_id: The channel's slot-independent identity (for channel conversations).
        peer: The contact's key prefix (for direct conversations).

    Returns:
        ``"chan:<identity>"`` for a channel or ``"dm:<peer>"`` (lowercased) for a direct
        exchange.
    """
    if is_channel:
        return f"chan:{channel_id}"
    return f"dm:{(peer or '').lower()}"


@dataclass(slots=True)
class ChatMessage:
    """A chat message, sent or received, on a channel or with a contact.

    This is the persisted, display-oriented view of a message: it unifies the outbound
    messages we send with the inbound :class:`Message` events the hub delivers, so a
    conversation transcript is a single ordered list of these. Each belongs to exactly one
    conversation — a channel (:attr:`is_channel` with :attr:`channel_idx`) or a direct
    exchange with a contact (:attr:`peer` holding the contact's key prefix).

    Attributes:
        text: The message body.
        outbound: ``True`` if we sent it, ``False`` if we received it.
        is_channel: Whether it belongs to a channel rather than a direct exchange.
        channel_id: The channel's slot-independent identity, for channel messages. This is
            what the message is keyed to, so its history follows the channel across slot
            reorders (see :func:`~meshterm.core.channels.channel_identity`).
        channel_idx: The channel slot the message went out on / arrived on. Retained only
            for reference and legacy backfill — the conversation key never uses it.
        peer: The other party's key prefix, for direct messages.
        peer_name: A friendly name for the peer/channel, snapshotted for display.
        snr: Signal-to-noise ratio (dB) of an inbound reception, if reported.
        acked: Delivery state of an outbound direct message: ``True`` acknowledged, ``False``
            sent but not acknowledged (retryable), ``None`` either still awaiting the ack
            or not applicable (a channel broadcast or an inbound message).
        created_at: When the message was sent or received.
        row_id: The ``messages`` table primary key once persisted, used to update an
            outbound message's delivery state in place on retry; ``None`` until stored.
        scope: What an outbound channel message was flooded under: the bare region name,
            :data:`~meshterm.core.regions.WILDCARD` (``*``) for unscoped, or ``None`` where
            it isn't known — every inbound or direct message, and anything sent before the
            scope was recorded.
    """

    text: str
    outbound: bool = False
    is_channel: bool = False
    channel_id: str | None = None
    channel_idx: int | None = None
    peer: str | None = None
    peer_name: str | None = None
    snr: float | None = None
    acked: bool | None = None
    created_at: datetime = field(default_factory=utcnow)
    row_id: int | None = None
    scope: str | None = None

    @property
    def key(self) -> str:
        """The key of the conversation this message belongs to."""
        return conversation_key(self.is_channel, self.channel_id, self.peer)

    @classmethod
    def from_message(
        cls,
        message: Message,
        *,
        peer_name: str | None = None,
        channel_id: str | None = None,
    ) -> ChatMessage:
        """Build an inbound :class:`ChatMessage` from a received :class:`Message`.

        Args:
            message: The inbound message delivered by the event hub.
            peer_name: A friendly name for the sender, resolved from contacts if known.
            channel_id: The channel's resolved identity (channel messages only). The wire
                carries only a slot index, so the caller resolves it to the channel's
                intrinsic identity before recording.

        Returns:
            The equivalent inbound :class:`ChatMessage`.
        """
        return cls(
            text=message.text,
            outbound=False,
            is_channel=message.is_channel,
            channel_id=channel_id if message.is_channel else None,
            channel_idx=message.channel,
            peer=None if message.is_channel else (message.sender or None),
            peer_name=peer_name,
            snr=message.snr,
            # Prefer the sender's own timestamp (the actual moment the message was
            # composed) over ``received_at`` (when we happened to pull it off the radio),
            # so the transcript reflects message time rather than retrieval time. Falls
            # back to the receive time when the sender carried no timestamp.
            created_at=message.sender_timestamp or message.received_at,
        )


@dataclass(slots=True)
class Conversation:
    """A selectable chat thread: a channel or a direct exchange with a contact.

    Attributes:
        label: Display name (e.g. ``#general`` or ``Alice``).
        is_channel: Whether this is a channel rather than a direct conversation.
        channel_idx: The channel slot to address for sending / live matching, for channels.
        channel_id: The channel's slot-independent identity, used to key its history (see
            :func:`~meshterm.core.channels.channel_identity`).
        secret: The channel's 16-byte secret, for channels — used only to show its
            public/private openness marker; ``None`` when unknown.
        contact: The contact, for direct conversations.
    """

    label: str
    is_channel: bool
    channel_idx: int | None = None
    channel_id: str | None = None
    secret: bytes | None = None
    contact: Contact | None = None

    @property
    def peer(self) -> str | None:
        """The peer key prefix for a direct conversation, else ``None``."""
        if self.is_channel or self.contact is None:
            return None
        return self.contact.key_prefix or self.contact.public_key[:12] or None

    @property
    def key(self) -> str:
        """The conversation's stable key (see :func:`conversation_key`)."""
        return conversation_key(self.is_channel, self.channel_id, self.peer)


@dataclass(slots=True)
class HeardNode:
    """Aggregated reception statistics for one node across many observations.

    Attributes:
        node: Key prefix / hash of the node (``None`` only if never identified).
        name: Most recent friendly name seen for the node, if any.
        count: Number of observations aggregated.
        median_snr: Median SNR (dB) across observations that reported one.
        best_snr: Strongest SNR (dB) seen, if any.
        last_rssi: Most recent RSSI (dBm), if any.
        last_seen: Timestamp of the most recent observation.
        lat: Most recent advertised latitude, if the node shared one.
        lon: Most recent advertised longitude, if the node shared one.
        node_type: Most recent advert type seen for the node (see the ``NODE_TYPE_*``
            constants), if any observation carried it.
        public_key: The node's full public key, when any observation captured one (see
            :attr:`Observation.public_key`); ``None`` leaves only the short :attr:`node` id.
    """

    node: str | None
    name: str | None
    count: int
    median_snr: float | None
    best_snr: float | None
    last_rssi: float | None
    last_seen: datetime
    lat: float | None = None
    lon: float | None = None
    node_type: int | None = None
    public_key: str | None = None

    @property
    def has_location(self) -> bool:
        """Whether this node reported a usable latitude/longitude."""
        return self.lat is not None and self.lon is not None

    @property
    def is_repeater(self) -> bool:
        """Whether this node advertised itself as a repeater."""
        return self.node_type == NODE_TYPE_REPEATER

    @classmethod
    def from_observations(cls, node: str | None, observations: list[Observation]) -> HeardNode:
        """Aggregate one node's observations into reception statistics.

        Args:
            node: The node identifier these observations belong to.
            observations: The observations for ``node`` (must be non-empty).

        Returns:
            A :class:`HeardNode` summarizing them. The most recent observation supplies
            the name, RSSI, and location; SNR is summarized robustly (median + best).
        """
        ordered = sorted(observations, key=lambda o: o.observed_at)
        latest = ordered[-1]
        snrs = [o.snr for o in ordered if o.snr is not None]
        located = next(
            (o for o in reversed(ordered) if o.lat is not None and o.lon is not None), None
        )
        name = next((o.name for o in reversed(ordered) if o.name), None)
        node_type = next((o.node_type for o in reversed(ordered) if o.node_type is not None), None)
        public_key = next((o.public_key for o in reversed(ordered) if o.public_key), None)
        return cls(
            node=node,
            name=name,
            count=len(ordered),
            median_snr=statistics.median(snrs) if snrs else None,
            best_snr=max(snrs) if snrs else None,
            last_rssi=latest.rssi,
            last_seen=latest.observed_at,
            lat=located.lat if located else None,
            lon=located.lon if located else None,
            node_type=node_type,
            public_key=public_key,
        )


@dataclass(slots=True)
class SelfActivity:
    """The Time Machine's own-node ledger: what this station *did*, not what it heard.

    Our own node is the one subject the reception history can't describe — we never
    overhear ourselves — so its Time Machine page is built from the outbound record
    instead: the traces we launched and the messages we sent. This carries the roll-up
    tallies that page prints, over one history window (see
    :meth:`~meshterm.persistence.repository.Repository.self_activity_ledger`).

    Attributes:
        trace_total: Traces launched in the window (timed-out attempts included).
        trace_ok: How many of those came home (``success = 1``).
        trace_targets: Distinct destinations aimed at, hand-composed path walks
            (filed under :data:`PATH_TRACE_TARGET`) excluded — they name no target.
        msg_channel: Channel messages sent (``outbound = 1``, ``is_channel = 1``).
        msg_dm: Direct messages sent (``outbound = 1``, ``is_channel = 0``).
        dm_acked: Direct messages sent that were acknowledged (``acked = 1``).
        dm_ackable: Direct messages sent whose ack was tracked at all (``acked``
            non-null) — the denominator the ack rate is honest over, since a channel
            broadcast is never acked and an in-flight DM has no verdict yet.
        dm_peers: Distinct contacts we sent a direct message to.
        tx_samples: TX-power optimization samples recorded (each a robust reach probe).
    """

    trace_total: int = 0
    trace_ok: int = 0
    trace_targets: int = 0
    msg_channel: int = 0
    msg_dm: int = 0
    dm_acked: int = 0
    dm_ackable: int = 0
    dm_peers: int = 0
    tx_samples: int = 0


@dataclass(slots=True)
class Hop:
    """A single hop in a path trace.

    Attributes:
        index: Zero-based position of the hop along the path.
        node: Identifier of the relaying node (name or key prefix), if resolved.
        snr: Signal-to-noise ratio in dB recorded at this hop.
    """

    index: int
    node: str | None
    snr: float


@dataclass(slots=True)
class TraceResult:
    """The aggregated outcome of a single path trace to a target.

    Attributes:
        target: Name or key prefix of the trace destination.
        success: Whether a trace reply was received before timeout.
        hops: Per-hop SNR readings, ordered from source to destination.
        round_trip_ms: Round-trip time of the trace in milliseconds, if measured.
        tx_power: TX power level in effect when the trace ran, if known.
        path_hash_bytes: Per-hop path-hash width (bytes) used by the trace command, so
            node hashes can be displayed at the same width the command addressed them.
        timestamp: When the trace completed.
        raw: Optional raw event payload for debugging/replay.
    """

    target: str
    success: bool
    hops: list[Hop] = field(default_factory=list)
    round_trip_ms: float | None = None
    tx_power: int | None = None
    path_hash_bytes: int | None = None
    timestamp: datetime = field(default_factory=utcnow)
    raw: dict | None = None

    @property
    def hop_count(self) -> int:
        """Number of hops recorded in the trace."""
        return len(self.hops)

    @property
    def min_snr(self) -> float | None:
        """The bottleneck (weakest) SNR along the path, or ``None`` if no hops."""
        if not self.hops:
            return None
        return min(h.snr for h in self.hops)

    def edges(self, device_label: str = LOCAL_DEVICE_LABEL) -> list[HopEdge]:
        """Frame the per-hop SNR as directed ``origin -> destination`` edges.

        Each hop's SNR is the signal measured arriving at that node, so an edge runs
        from the previous node to this one. The first edge therefore originates at our
        own device, and (because firmware records the reply returning to us as a final
        hash-less hop) the last edge's destination is our device too.

        Args:
            device_label: Name to show for our own device at the path's endpoints.

        Returns:
            One :class:`HopEdge` per hop, in path order.
        """
        edges: list[HopEdge] = []
        origin = device_label
        for hop in self.hops:
            destination = hop.node or device_label
            edges.append(
                HopEdge(index=hop.index, origin=origin, destination=destination, snr=hop.snr)
            )
            origin = destination
        return edges


@dataclass(slots=True)
class HopEdge:
    """A directed link in a trace path: a hop framed as ``origin -> destination``.

    Attributes:
        index: Zero-based position of the hop along the path.
        origin: Identifier of the transmitting node (our device for the first edge).
        destination: Identifier of the receiving node (our device for the last edge).
        snr: Signal-to-noise ratio in dB measured at ``destination``.
    """

    index: int
    origin: str
    destination: str
    snr: float


@dataclass(slots=True)
class HopAggregate:
    """Median SNR for one hop position aggregated across several traces.

    ``origin`` and ``destination`` hold raw node identifiers; ``None`` means our own
    device, so the display label can be applied at render time.

    Attributes:
        index: Zero-based hop position along the path.
        origin: Representative transmitting node at this position (``None`` = us).
        destination: Representative receiving node at this position (``None`` = us).
        median_snr: Median SNR in dB measured at ``destination`` across the samples.
        samples: Number of traces that reported this hop.
    """

    index: int
    origin: str | None
    destination: str | None
    median_snr: float
    samples: int


@dataclass(slots=True)
class TraceStats:
    """Robust statistics aggregated over several traces to the same target.

    Robust (median-based) metrics are used throughout because mesh SNR readings are
    noisy and prone to outliers.

    Attributes:
        target: The trace destination these statistics describe.
        samples: Number of traces attempted.
        successes: Number of traces that returned a reply.
        median_min_snr: Median of each trace's bottleneck SNR, the headline metric.
        median_rtt_ms: Median round-trip time, if measured.
        tx_power: TX power level in effect for these samples, if fixed.
        hop_snrs: Per-hop median SNR across the successful traces, in path order.
    """

    target: str
    samples: int
    successes: int
    median_min_snr: float | None
    median_rtt_ms: float | None
    tx_power: int | None = None
    hop_snrs: list[HopAggregate] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        """Fraction of traces that returned a reply, in the range ``[0, 1]``."""
        return self.successes / self.samples if self.samples else 0.0

    @classmethod
    def from_traces(cls, target: str, traces: list[TraceResult]) -> TraceStats:
        """Aggregate a list of traces into robust statistics.

        Args:
            target: The trace destination.
            traces: The individual trace results to aggregate.

        Returns:
            A :class:`TraceStats` summarizing the supplied traces.
        """
        successes = [t for t in traces if t.success]
        min_snrs = [t.min_snr for t in successes if t.min_snr is not None]
        rtts = [t.round_trip_ms for t in successes if t.round_trip_ms is not None]
        tx_powers = {t.tx_power for t in traces if t.tx_power is not None}
        return cls(
            target=target,
            samples=len(traces),
            successes=len(successes),
            median_min_snr=statistics.median(min_snrs) if min_snrs else None,
            median_rtt_ms=statistics.median(rtts) if rtts else None,
            tx_power=next(iter(tx_powers)) if len(tx_powers) == 1 else None,
            hop_snrs=cls._aggregate_hops(successes),
        )

    @staticmethod
    def _aggregate_hops(successes: list[TraceResult]) -> list[HopAggregate]:
        """Compute the median SNR per hop position across successful traces.

        Hops are grouped by their path position; for each position the SNR median is
        taken and the most common origin/destination nodes are used so the aggregate
        path reads like a single representative trace. Node identities are kept raw
        (``None`` = our device) so a display label can be applied later.

        Args:
            successes: The successful traces to aggregate.

        Returns:
            One :class:`HopAggregate` per hop position, ordered along the path.
        """
        snrs_by_index: dict[int, list[float]] = {}
        origins_by_index: dict[int, list[str | None]] = {}
        dests_by_index: dict[int, list[str | None]] = {}
        for trace in successes:
            origin: str | None = None  # the first hop originates at our device
            for hop in trace.hops:
                snrs_by_index.setdefault(hop.index, []).append(hop.snr)
                origins_by_index.setdefault(hop.index, []).append(origin)
                dests_by_index.setdefault(hop.index, []).append(hop.node)
                origin = hop.node

        aggregates: list[HopAggregate] = []
        for index in sorted(snrs_by_index):
            snrs = snrs_by_index[index]
            origin = Counter(origins_by_index[index]).most_common(1)[0][0]
            destination = Counter(dests_by_index[index]).most_common(1)[0][0]
            aggregates.append(
                HopAggregate(
                    index=index,
                    origin=origin,
                    destination=destination,
                    median_snr=statistics.median(snrs),
                    samples=len(snrs),
                )
            )
        return aggregates


@dataclass(slots=True)
class TxLevelResult:
    """Robust measurement of one TX power level during an optimization sweep.

    The headline metric is ``target_snr`` — the SNR the *target* node reports receiving
    from the admin-tuned node just ahead of it — since that single link is what the
    remote-admin optimizer is tuning. ``success_rate`` is the primary objective
    (reliability first), with ``target_snr`` the tie-breaker.

    Attributes:
        tx_power: The transmit power level tested on the admin node.
        samples: Number of traces run at this level.
        successes: Number of traces that returned a reply.
        target_snr: Median SNR (dB) received at the target across successful traces,
            or ``None`` if every trace at this level failed.
        score: Scalar objective for display/plotting (the median target SNR, or
            negative infinity when nothing got through).
        stats: Aggregated trace statistics at this level (full path, for display).
    """

    tx_power: int
    samples: int
    successes: int
    target_snr: float | None
    score: float
    stats: TraceStats

    @property
    def success_rate(self) -> float:
        """Fraction of traces that returned a reply, in the range ``[0, 1]``."""
        return self.successes / self.samples if self.samples else 0.0


@dataclass(slots=True)
class TxOptResult:
    """The outcome of a remote-admin TX-power optimization run.

    Attributes:
        target: Node the SNR was measured at.
        admin_node: Label of the node whose TX power was tuned (the hop before target).
        path: The forced path the traces walked (comma-separated hashes).
        original_tx: The admin node's TX power before the sweep, if it could be read.
        best_tx: The chosen optimal TX power.
        best_snr: Median target SNR (dB) at ``best_tx``.
        best_success_rate: Trace success rate at ``best_tx``.
        applied: Whether ``best_tx`` was written to the admin node.
        levels: Every level measured, in the order tested.
    """

    target: str
    admin_node: str
    path: str
    original_tx: int | None
    best_tx: int
    best_snr: float | None
    best_success_rate: float
    applied: bool
    levels: list[TxLevelResult] = field(default_factory=list)

    @property
    def best_level(self) -> TxLevelResult | None:
        """The :class:`TxLevelResult` for ``best_tx``, if present."""
        return next((lv for lv in self.levels if lv.tx_power == self.best_tx), None)

    def sorted_by_tx(self) -> list[TxLevelResult]:
        """Return measured levels sorted ascending by TX power.

        Returns:
            The levels ordered by TX power, suitable for plotting.
        """
        return sorted(self.levels, key=lambda lv: lv.tx_power)
