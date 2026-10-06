# SPDX-License-Identifier: Apache-2.0
"""Domain models that the services, tools, persistence, and visualizations share.

These are plain dataclasses with no I/O dependencies. Thus the real device, the
simulator, and the code that reads them back from the database all construct them in the
same way.
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

#: Maps the raw hash of a hop to a display label: a contact name when it is known, or the
#: hash itself when it is not. ``None`` passes through unchanged, because it is our node.
#: This is the only alias for the name side of a hop. All the code that builds one
#: (:func:`~meshterm.services.trace_runner.make_node_resolver`) and all the code that takes
#: one (each screen that draws a route) use it.
NodeResolver = Callable[[str | None], str | None]

#: Maps a display name back to the key of the node, in the longest form that we have (a
#: public key, a key prefix, or a stored node id). It gives ``None`` for a name that no
#: known node has. This is the colour side of :data:`NodeResolver`. That alias changes hex
#: into names. This alias changes a bare name back into the key from which its palette hue
#: comes
#: (:func:`~meshterm.services.trace_runner.make_name_key_resolver`).
NameKeyResolver = Callable[[str], str | None]

#: The label for our node (the local device) at the endpoints of a trace path.
LOCAL_DEVICE_LABEL = "us"

#: The sentinel ``target`` that is stored on the traces of the Trace path feature. That
#: feature walks a route that the user composed, with no destination node (a trace is one
#: walked path, and a target is only a UX concept). The parentheses prevent a collision
#: with a contact name. History queries use it as a key. ``Repository.latest_trace`` uses
#: it to give the path screen its start values. ``Repository.traced_targets`` excludes it,
#: so that composed walks never appear in the "Recently traced" list of the target picker.
PATH_TRACE_TARGET = "(path)"

# The MeshCore advert types (the low nibble of the flag byte of an advert) classify the
# node that an advert announces. Repeaters are fixed infrastructure and carry the mesh.
# Thus the features that use a location (for example, the map) give them priority over
# usual leaf nodes.
NODE_TYPE_CHAT = 1
NODE_TYPE_REPEATER = 2
NODE_TYPE_ROOM = 3
NODE_TYPE_SENSOR = 4

#: A label that a person can read for each advert type, for legends and summaries.
#: This is the only name for each advert type. MeshTerm uses it each time that it prints
#: one, and in each `--json` answer that has a role or a type. A type-1 node is a
#: companion: the node that a person talks through, the same type as the one in your hand.
NODE_TYPE_LABELS = {
    NODE_TYPE_CHAT: "companion",
    NODE_TYPE_REPEATER: "repeater",
    NODE_TYPE_ROOM: "room server",
    NODE_TYPE_SENSOR: "sensor",
}


def node_type_label(node_type: int | None) -> str | None:
    """The name of an advert type: ``type N`` for an unknown type, and None for no type."""
    if node_type is None:
        return None
    return NODE_TYPE_LABELS.get(int(node_type), f"type {node_type}")


class LoginResult(Enum):
    """How an admin login ended, and also whether the node was there to end it.

    "Rejected" and "never answered" were once the same ``False``. Each caller read that
    ``False`` as a wrong password and removed the stored credential. Thus, when the user
    administered a repeater while it was down, MeshTerm erased its password. The node said
    nothing at all, and we read the silence as a refusal.

    These two results do not mean the same thing. A refusal is the node that tells us that
    the password is wrong, and only a refusal is a reason to forget the password. Silence
    tells nothing about the password: the node may be asleep or out of range, or the reply
    may be lost on its way back. Thus the credential must stay after a silence.

    The value is truthy only when the session opened. Thus the old idiom
    ``if not await device.admin_login(...)`` still means "we are not logged in". The code
    tells the two failures apart by identity. Thus each call site must say which failure
    it handles.
    """

    #: The node accepted the password. An admin session is open.
    ACCEPTED = "accepted"
    #: The node answered and said no. The stored password is wrong. Forget it.
    REFUSED = "refused"
    #: No reply came back. The node is out of range or asleep, or the reply was lost. Keep
    #: the password.
    NO_REPLY = "no_reply"

    def __bool__(self) -> bool:
        """Truthy only when logged in, so that the plain ``if not …`` idiom keeps its meaning."""
        return self is LoginResult.ACCEPTED


class RoomAccess(Enum):
    """What a room server let us do after our login, in the words of the room screen.

    A room answers a login with the role that it gave us. The role is the low two bits of
    the permissions byte that its access list keeps for each client (MeshCore's
    ``PERM_ACL_*``). The three members of this enum are what those roles do in a room,
    because that is what the user must know. The firmware names for the roles do not tell
    exactly that. A room keeps the posts of all the roles but one: role 0, its "guest". A
    wrong password gets that role when the owner set ``allow.read.only`` on. The firmware
    calls role 1 read-only. But only a person gives role 1, by hand (``setperm``), never a
    login, and the room still keeps its posts. Thus role 1 is a member here, because that
    is what it is.
    """

    #: Read, post, and run the command line of the room. The room keeps its admins after a
    #: restart.
    ADMIN = "admin"
    #: Read and post. This is the level of the room password.
    MEMBER = "member"
    #: Read only. The room does not keep the posts of this member, and sends no
    #: acknowledgement.
    READ_ONLY = "read-only"

    @property
    def can_post(self) -> bool:
        """Whether the room keeps what this member posts."""
        return self is not RoomAccess.READ_ONLY

    @classmethod
    def from_login(cls, payload: dict) -> RoomAccess:
        """Read the access that a room gave, from its login reply.

        When the firmware sends the access-list permissions byte (companion 1.10 and
        later), this method reads that byte. An older reply has only the legacy flag in its
        first byte. A room sets that flag to ``1`` for an admin and to ``2`` for a guest.

        Args:
            payload: The payload of the ``LOGIN_SUCCESS`` event, as the meshcore library
                parses it (``acl_permissions`` and ``permissions``).

        Returns:
            The access that the room gave.
        """
        acl = payload.get("acl_permissions")
        if isinstance(acl, int):
            role = acl & 0x03
            if role == 0x03:
                return cls.ADMIN
            return cls.READ_ONLY if role == 0 else cls.MEMBER
        legacy = payload.get("permissions")
        if legacy == 1:
            return cls.ADMIN
        return cls.READ_ONLY if legacy == 2 else cls.MEMBER


@dataclass(frozen=True, slots=True)
class RoomLogin:
    """How a login to a room server ended, and what it let us do.

    The :class:`LoginResult` keeps its meaning and its policy about what to remember. A
    room adds one fact that the login to a repeater never needed: which of its roles the
    room gave us.

    Attributes:
        result: Accepted, refused, or never answered. A room never refuses. A wrong
            password gets no reply at all, so :attr:`~LoginResult.NO_REPLY` is also the
            result of a password with a typing error.
        access: The access that the room gave, when it accepted the login.
        flood: How the device sent the request, as the device confirmed it: ``True`` to
            the whole mesh, ``False`` along the route that it learned for the room.
            ``None`` when the device never confirmed that it sent the request. This field
            shows one of the two causes of silence after a login: a learned route becomes
            stale when the mesh changes, and a flood goes around that problem.
        radio_error: The reason that the companion gave for not sending the request, when
            it refused to send it (most often, because the room is not in its contacts).
            ``None`` otherwise.
    """

    result: LoginResult
    access: RoomAccess | None = None
    flood: bool | None = None
    radio_error: str | None = None

    def __bool__(self) -> bool:
        """Truthy only when logged in, the same as the :class:`LoginResult` that it holds."""
        return bool(self.result)


def is_room(node_type: int | None) -> bool:
    """Whether a node of this advert type is a room server (you join it, you do not DM it).

    This is the room equivalent of :func:`is_direct_messageable`. Together, the two
    functions put each contact in the Chat picker list into its correct section. A contact
    whose type was never advertised is not a room. The reason: to join a room starts with a
    password prompt, and MeshTerm must not show a password prompt for a companion.

    Args:
        node_type: The advert type of the node, or ``None`` when the node never
            advertised it.

    Returns:
        ``True`` only for :data:`NODE_TYPE_ROOM`.
    """
    return node_type == NODE_TYPE_ROOM


def is_direct_messageable(node_type: int | None) -> bool:
    """Whether a node of this advert type is a direct-message recipient: the only DM rule.

    We send direct messages only to companion (chat) nodes. A repeater or a sensor is
    infrastructure, not a person to send a message to. A room server is a place where you
    post (refer to :func:`is_room`). The transmission is the same, but the action of the
    user is different, and the room screen draws a different thing. A contact whose type
    was never advertised (``None``) gets the benefit of the doubt. Thus a missing type never
    hides a real companion. Each DM recipient picker (the chat conversation list, the
    courier outbox) uses this one predicate as its filter. Thus MeshTerm applies the rule
    "DMs go to companions only" in one place.

    Args:
        node_type: The advert type of the node (refer to the ``NODE_TYPE_*`` constants),
            or ``None`` when the node never advertised it.

    Returns:
        ``True`` for a companion node or a contact with no type. ``False`` for a
        repeater, a room, or a sensor.
    """
    return node_type in (NODE_TYPE_CHAT, None)


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp.

    Returns:
        The current time in UTC.
    """
    return datetime.now(timezone.utc)


#: The maximum time (in seconds) that an advert timestamp can be in the future. After this
#: limit, MeshTerm refuses the timestamp as false. The clock of the sender writes the
#: timestamp of an advert, and a mesh node with no time source can be wrong by minutes, or
#: by months. A skew of a few minutes is usual and causes no problem (the age clamps to
#: "now" until the real time catches up). But if MeshTerm accepts a larger skew, that skew
#: pins the contact at the top of each list sorted by heard time. The contact then shows
#: "now" for as long as the false timestamp stays ahead of the wall clock.
ADVERT_FUTURE_SKEW_S = 300


def advert_time(last_advert: object) -> datetime | None:
    """Change the Unix timestamp of an advert into a plausible UTC datetime, or ``None``.

    This is the only converter for each ``last_advert`` epoch that comes into the app. The
    live contact table of the device and the contact store (which lasts across sessions)
    both go through it. Thus the plausibility rule is in one place. The clock of the node
    that advertised writes the value, so the value is hearsay. A value that is zero, absent,
    or garbage means "never heard". A timestamp that is more than
    :data:`ADVERT_FUTURE_SKEW_S` ahead of our clock comes from a sender clock that is set
    incorrectly, and the function refuses it in the same way. Callers that have their own
    evidence of reception (stored observations) use that evidence to fill the ``None``
    result. A true "when we heard it" is better than a false "heard in the future".

    Args:
        last_advert: The raw ``last_advert`` field from a contact payload or a stored
            entry.

    Returns:
        A timezone-aware UTC :class:`datetime`, or ``None`` when the time is unknown or
        not plausible.
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
    """A node that the connected companion knows.

    Attributes:
        name: The name that the node advertised, for people to read.
        public_key: The full public key as a hex string, if it is known.
        key_prefix: The short key prefix that MeshTerm uses to address the node.
        last_seen: When the node was last heard, if it is known.
        node_type: The advert type of the node (refer to the ``NODE_TYPE_*`` constants),
            if it is known.
        lat: The latitude that the node last advertised (decimal degrees), if it shared
            one.
        lon: The longitude that the node last advertised (decimal degrees), if it shared
            one.
        route_hops: The outbound route to this contact that the device learned: one hex
            path hash for each repeater, in order from us outward. The firmware found this
            route from the paths of the flood packets that it received. An empty tuple
            means that the firmware thinks that the contact is a direct neighbour (a
            zero-hop route). ``None`` means that the device learned no route (or that the
            transport did not report one).
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
        """Whether this contact advertised a usable latitude and longitude."""
        return self.lat is not None and self.lon is not None

    @property
    def is_repeater(self) -> bool:
        """Whether this contact advertises as a repeater (fixed infrastructure)."""
        return self.node_type == NODE_TYPE_REPEATER

    @property
    def is_room(self) -> bool:
        """Whether this contact advertises as a room server (refer to :func:`is_room`)."""
        return is_room(self.node_type)


@dataclass(slots=True)
class MapMarker:
    """One mesh node to put on the map.

    This class is data, not a drawing. It is a node with a location, and the small amount
    of data that the map must have to put the node on the map and label it. It is in this
    module instead of near the renderer, because code far below the UI makes it:
    :func:`~meshterm.services.markers.gather_markers` builds the list from contacts and
    observations, and three different surfaces draw it.

    Attributes:
        label: The node name that shows next to the marker.
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.
        is_repeater: Whether the node is a repeater (a marker with priority).
        is_self: Whether this is our node (drawn with emphasis).
        detail: More text for the CLI legend (for example ``"18 pkts · +6.0 dB"``).
        key: The key of the node in hex (the longest form that the caller has). The hue
            of the label comes from this key. ``None`` leaves the label in the muted grey
            for a node with no key.
    """

    label: str
    lat: float
    lon: float
    is_repeater: bool = False
    is_self: bool = False
    detail: str = ""
    key: str | None = None

    def _rank(self) -> int:
        """Draw order: our node above the repeaters, and the repeaters above leaf nodes."""
        return 2 if self.is_self else (1 if self.is_repeater else 0)


@dataclass(slots=True)
class NeighbourInfo:
    """One entry of the neighbour table of a remote repeater, read over the mesh.

    Repeater firmware keeps a table of the nodes that it heard directly (zero-hop). Each
    entry has the SNR that the repeater measured. Our node cannot measure from that
    position. :meth:`~meshterm.core.connection.Device.fetch_neighbours` returns these
    entries. The values describe the link ``repeater ↔ node`` as the repeater observes it.

    Attributes:
        node: The key prefix of the neighbour in lowercase hex, at the width of the
            firmware reply (typically 8 hex digits).
        snr: The SNR (dB) that the repeater measured when it received this neighbour, if
            reported.
        heard_at: When the repeater last heard the neighbour (calculated from the
            seconds-ago field of the reply), or ``None`` when the reply did not tell.
    """

    node: str
    snr: float | None = None
    heard_at: datetime | None = None


@dataclass(slots=True)
class Observation:
    """One reception over the air, heard while MeshTerm monitors the mesh passively.

    MeshTerm captures the adverts, telemetry packets, and other packets that the companion
    overhears as observations, and logs them over time. Thus the user can examine the
    behaviour of the mesh over time, instead of only at the moment when a command runs.

    Attributes:
        node: The key prefix or the hash that identifies the node that transmitted, if
            known. This is the stored canonical id of 12 hex digits, and all the code
            groups and joins on it. It is short on purpose, so that it agrees with the key
            prefixes in traces, messages, and the topology graph.
        public_key: The full public key of the node, when the advert had one (it comes in
            the payload, and MeshTerm truncates it to :attr:`node` for the id). MeshTerm
            only shows it: it lets a hash lane show more than the twelve stored digits.
            ``None`` for a packet class or a firmware that gave only a short hash.
        name: The name that the node advertised, if the packet had one.
        kind: The packet class, for example ``advert`` or ``telemetry``.
        node_type: The advert type of the node that transmitted (refer to the
            ``NODE_TYPE_*`` constants), for example :data:`NODE_TYPE_REPEATER`. ``None``
            when the packet did not have it.
        snr: The signal-to-noise ratio (dB) that the companion measured, if reported.
        rssi: The received signal strength (dBm), if reported.
        lat: The advertised latitude (decimal degrees), when the node shares its location.
        lon: The advertised longitude (decimal degrees), when the node shares its
            location.
        path: For ``packet`` observations (the RX packet log of the companion): the relay
            path of the packet before it got to us. It is a list of hex hashes, one for
            each hop, separated by commas, in the order of propagation. The hop nearest
            the originator is first, and the repeater that we heard is last. An empty
            string means that the packet came directly (zero hops). ``None`` means that the
            packet class has no path. For packets, :attr:`snr` and :attr:`rssi` describe
            our reception from the last relay in this path, not from the originating
            :attr:`node`. For this reason, packet rows are not in the reception statistics
            of each node, and they go into the topology instead.
        observed_at: When the packet was heard.
        raw: An optional raw event payload, for debugging and replay.
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
    """An inbound text message that the companion received (direct or on a channel).

    A direct message has a :attr:`sender` key prefix. A channel message has a
    :attr:`channel` index instead. The event hub, which always runs, gives both types as
    :attr:`~meshterm.core.events.EventKind.MESSAGE` events. Thus client features (an inbox,
    notifications) can react to them without polling.

    Attributes:
        text: The decoded message body.
        sender: The key prefix of the contact that sent the message (direct messages),
            if known.
        channel: The index of the channel that the message came on (channel messages),
            if applicable.
        is_channel: Whether this is a channel message instead of a direct one.
        sender_timestamp: The timestamp that the sender gave the message, if the message
            has one. A room post has the time when it was posted, by the clock of the
            room. A post from the backlog comes long after that time.
        snr: The signal-to-noise ratio (dB) of the reception, if reported.
        received_at: When the companion delivered the message to us.
        author: For a room post: the key prefix of the author, in lowercase hex. A room
            post is a direct message from a room server, signed with the first four bytes
            of the key of its writer. The room is the :attr:`sender` (it is the node that
            transmitted), and the author is the member that the room vouches for. ``None``
            for all other messages, also for the command-line replies of the room.
        raw: An optional raw event payload, for debugging and replay.
    """

    text: str
    sender: str | None = None
    channel: int | None = None
    is_channel: bool = False
    sender_timestamp: datetime | None = None
    snr: float | None = None
    received_at: datetime = field(default_factory=utcnow)
    author: str | None = None
    raw: dict | None = None

    @property
    def is_post(self) -> bool:
        """Whether this is a room post (a message that a room relayed for a member)."""
        return self.author is not None


@dataclass(slots=True)
class Ack:
    """A delivery acknowledgement for a message that the companion sent.

    Attributes:
        code: The ACK correlation code (hex) that matches it to the sent message, if
            present.
        received_at: When the acknowledgement came.
        raw: An optional raw event payload, for debugging and replay.
    """

    code: str | None = None
    received_at: datetime = field(default_factory=utcnow)
    raw: dict | None = None


@dataclass(slots=True)
class Delivery:
    """A direct message that the device accepted, its ack, and whether the ack came in time.

    The ack is the proof from the recipient that it received the message (for a room, that
    it stored the post). The key of the ack is a four-byte code. The device calculates that
    code from the message, and returns it at the moment when it accepts the send. The ack
    has the same code, at any time that it comes. Because MeshTerm keeps the code, an ack
    that comes after the wait still counts. The device pushes the ack in all cases. The ack
    of a flood comes back on the path that the message found, and it often takes longer
    than a person will wait.

    Attributes:
        code: The ack code that the device expects back, in lowercase hex. ``None`` when
            the device gave no code (then there is nothing to wait for, and nothing that
            a late ack can match).
        ack: The acknowledgement, when it came during the wait. ``None`` otherwise. This
            ``None`` does not mean "not delivered". It means only "not confirmed yet".
        flood: How the device sent it: ``True`` to the whole mesh, ``False`` along the
            route that it learned. ``None`` when the device did not tell.
    """

    code: str | None
    ack: Ack | None = None
    flood: bool | None = None

    @property
    def acked(self) -> bool:
        """Whether the acknowledgement of the recipient came during the wait."""
        return self.ack is not None


def conversation_key(is_channel: bool, channel_id: str | None, peer: str | None) -> str:
    """Return a stable key that identifies a chat conversation.

    A conversation is a channel, or a direct exchange with one contact. With this key, the
    history, the unread counts, and the live screen all agree about the thread of a
    message. On purpose, the key comes from a property of the conversation itself: the
    identity of a channel (which comes from its secret, refer to
    :func:`~meshterm.core.channels.channel_identity`), or the key prefix of a contact. It
    never comes from a slot index or a list position. Thus, when the user reorders the
    channels, the history stays with its channel.

    Args:
        is_channel: Whether the conversation is a channel.
        channel_id: The identity of the channel, which does not depend on the slot (for
            channel conversations).
        peer: The key prefix of the contact (for direct conversations).

    Returns:
        ``"chan:<identity>"`` for a channel, or ``"dm:<peer>"`` (in lowercase) for a
        direct exchange.
    """
    if is_channel:
        return f"chan:{channel_id}"
    return f"dm:{(peer or '').lower()}"


@dataclass(slots=True)
class ChatMessage:
    """A chat message, sent or received, on a channel or with a contact.

    This is the stored form of a message, made for the screen. It puts the outbound
    messages that we send and the inbound :class:`Message` events from the hub into one
    type. Thus a conversation transcript is one ordered list of these records. Each message
    is part of exactly one conversation: a channel (:attr:`is_channel` with
    :attr:`channel_idx`), or a direct exchange with a contact (:attr:`peer` holds the key
    prefix of the contact).

    Attributes:
        text: The message body.
        outbound: ``True`` if we sent it, ``False`` if we received it.
        is_channel: Whether it is part of a channel instead of a direct exchange.
        channel_id: The identity of the channel, which does not depend on the slot, for
            channel messages. This identity is the key of the message. Thus its history
            follows the channel when the slots are reordered (refer to
            :func:`~meshterm.core.channels.channel_identity`).
        channel_idx: The channel slot on which the message went out or came in. MeshTerm
            keeps it only for reference and for the legacy backfill. The conversation key
            never uses it.
        peer: The key prefix of the other party, for direct messages.
        peer_name: The name of the peer or the channel at that time, kept for the screen.
        snr: The signal-to-noise ratio (dB) of an inbound reception, if reported.
        acked: The delivery state of an outbound direct message. ``True``: acknowledged.
            ``False``: sent but not acknowledged (the user can try again). ``None``: the
            ack is still expected, or the state does not apply (a channel broadcast or an
            inbound message).
        created_at: When the message was sent or received.
        row_id: The primary key in the ``messages`` table, after MeshTerm stores the
            message. MeshTerm uses it to update the delivery state of an outbound message
            in place, when the user tries again. ``None`` until the message is stored.
        scope: The scope under which an outbound channel message was flooded: the region
            name alone, :data:`~meshterm.core.regions.WILDCARD` (``*``) for unscoped, or
            ``None`` when the scope is not known. The scope is not known for each inbound
            or direct message, and for each message sent before MeshTerm stored the scope.
        author: For an inbound room post: the key prefix of the member who wrote it (refer to
            :attr:`Message.author`). :attr:`peer` is then the room. ``None`` for all other
            messages. This includes our own posts, which a room never sends back. It also
            includes the command-line replies of a room, which have its conversation key
            but are not posts (refer to :attr:`is_post`).
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
    author: str | None = None

    @property
    def is_post(self) -> bool:
        """Whether this is an inbound room post, by the member that :attr:`author` names."""
        return self.author is not None

    @property
    def key(self) -> str:
        """The key of the conversation that this message is part of."""
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
            message: The inbound message that the event hub delivered.
            peer_name: A name for the sender, resolved from the contacts if it is known.
            channel_id: The resolved identity of the channel (channel messages only). The
                wire has only a slot index. Thus the caller resolves the index to the
                identity of the channel before the message is stored.

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
            # Use the timestamp of the sender (the moment when the message was composed)
            # instead of ``received_at`` (when MeshTerm read it from the device). Thus the
            # transcript shows the time of the message, not the time of the read. If the
            # sender gave no timestamp, use the receive time.
            created_at=message.sender_timestamp or message.received_at,
            author=None if message.is_channel else message.author,
        )


@dataclass(slots=True)
class Conversation:
    """A chat thread to select: a channel, a room, or a direct exchange with a contact.

    MeshTerm addresses a room the same as a direct exchange: one node, and its key prefix is
    the key of the history. Thus a room is a conversation with a :attr:`contact` whose type is
    room server (:attr:`is_room`). The difference is the content of the transcript: posts
    from many members, each under its :attr:`ChatMessage.author`, instead of the messages
    of one peer.

    Attributes:
        label: The name that the screen shows (for example ``#general`` or ``Alice``).
        is_channel: Whether this is a channel instead of a direct conversation.
        channel_idx: For channels: the channel slot to address when MeshTerm sends, and
            to match live messages.
        channel_id: The identity of the channel, which does not depend on the slot. It is
            the key of its history (refer to
            :func:`~meshterm.core.channels.channel_identity`).
        secret: For channels: the 16-byte secret of the channel. MeshTerm uses it only to
            show the public or private mark of the channel. ``None`` when it is not known.
        contact: The contact, for direct conversations and rooms.
    """

    label: str
    is_channel: bool
    channel_idx: int | None = None
    channel_id: str | None = None
    secret: bytes | None = None
    contact: Contact | None = None

    @property
    def peer(self) -> str | None:
        """The key prefix of the peer for a direct conversation or a room, else ``None``."""
        if self.is_channel or self.contact is None:
            return None
        return self.contact.key_prefix or self.contact.public_key[:12] or None

    @property
    def is_room(self) -> bool:
        """Whether this conversation is the board of a room server."""
        return not self.is_channel and self.contact is not None and self.contact.is_room

    @property
    def key(self) -> str:
        """The stable key of the conversation (refer to :func:`conversation_key`)."""
        return conversation_key(self.is_channel, self.channel_id, self.peer)


@dataclass(slots=True)
class HeardNode:
    """Reception statistics for one node, aggregated from many observations.

    Attributes:
        node: The key prefix or the hash of the node (``None`` only if the node was never
            identified).
        name: The most recent name heard for the node, if any.
        count: The number of aggregated observations.
        median_snr: The median SNR (dB) of the observations that reported one.
        best_snr: The strongest SNR (dB) heard, if any.
        last_rssi: The most recent RSSI (dBm), if any.
        last_seen: The timestamp of the most recent observation.
        lat: The most recent advertised latitude, if the node shared one.
        lon: The most recent advertised longitude, if the node shared one.
        node_type: The most recent advert type heard for the node (refer to the
            ``NODE_TYPE_*`` constants), if an observation had it.
        public_key: The full public key of the node, when an observation captured one
            (refer to :attr:`Observation.public_key`). ``None`` leaves only the short
            :attr:`node` id.
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
        """Whether this node reported a usable latitude and longitude."""
        return self.lat is not None and self.lon is not None

    @property
    def is_repeater(self) -> bool:
        """Whether this node advertised itself as a repeater."""
        return self.node_type == NODE_TYPE_REPEATER

    @classmethod
    def from_observations(cls, node: str | None, observations: list[Observation]) -> HeardNode:
        """Aggregate the observations of one node into reception statistics.

        Args:
            node: The identifier of the node of these observations.
            observations: The observations for ``node`` (the list must not be empty).

        Returns:
            A :class:`HeardNode` that summarizes them. The most recent observation gives
            the name, the RSSI, and the location. The SNR summary is robust (the median
            and the best value).
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
    """The ledger of our node in the Time Machine: what our node did, not what it heard.

    Our node is the only subject that the reception history cannot describe, because we
    never overhear ourselves. Thus its Time Machine page comes from the outbound records
    instead: the traces that we started and the messages that we sent. This class holds
    the totals that that page shows, for one time window of the history (refer to
    :meth:`~meshterm.persistence.repository.Repository.self_activity_ledger`).

    Attributes:
        trace_total: The traces started in the time window (also the tries that timed
            out).
        trace_ok: How many of those traces came back (``success = 1``).
        trace_targets: The number of different destinations. This excludes the path
            walks that the user composed (stored under :data:`PATH_TRACE_TARGET`),
            because they have no target.
        msg_channel: The channel messages sent (``outbound = 1``, ``is_channel = 1``).
        msg_dm: The direct messages sent (``outbound = 1``, ``is_channel = 0``).
        dm_acked: The direct messages sent that were acknowledged (``acked = 1``).
        dm_ackable: The direct messages sent for which MeshTerm tracked the ack
            (``acked`` is not null). This is the correct denominator for the ack rate,
            because a channel broadcast never gets an ack, and a DM in transit has no
            result yet.
        dm_peers: The number of different contacts that we sent a direct message to.
        tx_samples: The stored samples of the TX power optimization (each sample is a
            robust probe of the reach).
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
    """One hop in a path trace.

    Attributes:
        index: The zero-based position of the hop along the path.
        node: The identifier of the node that relays (a name or a key prefix), if
            resolved.
        snr: The signal-to-noise ratio in dB, measured at this hop.
    """

    index: int
    node: str | None
    snr: float


@dataclass(slots=True)
class TraceResult:
    """The aggregated result of one path trace to a target.

    Attributes:
        target: The name or the key prefix of the trace destination.
        success: Whether a trace reply came before the timeout.
        hops: The SNR value of each hop, in order from the source to the destination.
        round_trip_ms: The round-trip time of the trace in milliseconds, if measured.
        tx_power: The TX power level in use when the trace ran, if known.
        path_hash_bytes: The path-hash width (bytes) for each hop that the trace command
            used. Thus the screen can show the node hashes at the same width that the
            command used to address them.
        timestamp: When the trace completed.
        raw: An optional raw event payload, for debugging and replay.
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
        """The number of hops in the trace."""
        return len(self.hops)

    @property
    def min_snr(self) -> float | None:
        """The bottleneck (weakest) SNR along the path, or ``None`` if there are no hops."""
        if not self.hops:
            return None
        return min(h.snr for h in self.hops)

    def edges(self, device_label: str = LOCAL_DEVICE_LABEL) -> list[HopEdge]:
        """Change the SNR of each hop into directed ``origin -> destination`` edges.

        The SNR of a hop is the strength of the signal that arrives at that node. Thus an
        edge goes from the previous node to this node, and the first edge starts at our
        node. The firmware stores the reply that comes back to us as a last hop with no
        hash. Thus the destination of the last edge is also our node.

        Args:
            device_label: The name to show for our node at the endpoints of the path.

        Returns:
            One :class:`HopEdge` for each hop, in path order.
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
    """A directed link in a trace path: a hop in the form ``origin -> destination``.

    Attributes:
        index: The zero-based position of the hop along the path.
        origin: The identifier of the node that transmits (our node for the first edge).
        destination: The identifier of the node that receives (our node for the last
            edge).
        snr: The signal-to-noise ratio in dB, measured at ``destination``.
    """

    index: int
    origin: str
    destination: str
    snr: float


@dataclass(slots=True)
class HopAggregate:
    """The median SNR for one hop position, aggregated from several traces.

    ``origin`` and ``destination`` hold raw node identifiers. ``None`` means our node.
    Thus the code can apply the label for the screen at render time.

    Attributes:
        index: The zero-based hop position along the path.
        origin: The representative node that transmits at this position (``None`` = us).
        destination: The representative node that receives at this position (``None`` =
            us).
        median_snr: The median SNR in dB measured at ``destination``, over all the
            samples.
        samples: The number of traces that reported this hop.
    """

    index: int
    origin: str | None
    destination: str | None
    median_snr: float
    samples: int


@dataclass(slots=True)
class TraceStats:
    """Robust statistics, aggregated from several traces to the same target.

    All the metrics are robust (they use the median), because the SNR values of a mesh are
    noisy and can have outliers.

    Attributes:
        target: The trace destination that these statistics describe.
        samples: The number of traces that MeshTerm tried.
        successes: The number of traces that returned a reply.
        median_min_snr: The median of the bottleneck SNR of each trace. This is the main
            metric.
        median_rtt_ms: The median round-trip time, if measured.
        tx_power: The TX power level in use for these samples, if it was fixed.
        hop_snrs: The median SNR of each hop over the successful traces, in path order.
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
        """The fraction of traces that returned a reply, in the range ``[0, 1]``."""
        return self.successes / self.samples if self.samples else 0.0

    @classmethod
    def from_traces(cls, target: str, traces: list[TraceResult]) -> TraceStats:
        """Aggregate a list of traces into robust statistics.

        Args:
            target: The trace destination.
            traces: The trace results to aggregate, one for each trace.

        Returns:
            A :class:`TraceStats` that summarizes the traces.
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
        """Calculate the median SNR for each hop position, over the successful traces.

        The hops are grouped by their position in the path. For each position, the
        function takes the median SNR and uses the most common origin and destination
        nodes. Thus the aggregate path looks like one representative trace. The node
        identities stay raw (``None`` = our node), so that a label for the screen can be
        applied later.

        Args:
            successes: The successful traces to aggregate.

        Returns:
            One :class:`HopAggregate` for each hop position, in order along the path.
        """
        snrs_by_index: dict[int, list[float]] = {}
        origins_by_index: dict[int, list[str | None]] = {}
        dests_by_index: dict[int, list[str | None]] = {}
        for trace in successes:
            origin: str | None = None  # the first hop starts at our node
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
    """A robust measurement of one TX power level during an optimization sweep.

    The main metric is ``target_snr``: the SNR that the target node reports for the signal
    from the admin-tuned node immediately before it. The reason is that the remote-admin
    optimizer tunes only that one link. ``success_rate`` is the primary objective
    (reliability first), and ``target_snr`` is the tie-breaker.

    Attributes:
        tx_power: The transmit power level that was tested on the admin node.
        samples: The number of traces that ran at this level.
        successes: The number of traces that returned a reply.
        target_snr: The median SNR (dB) received at the target over the successful
            traces, or ``None`` if all the traces at this level failed.
        score: A scalar objective to show and plot (the median target SNR, or negative
            infinity when no trace got through).
        stats: The aggregated trace statistics at this level (the full path, to show).
    """

    tx_power: int
    samples: int
    successes: int
    target_snr: float | None
    score: float
    stats: TraceStats

    @property
    def success_rate(self) -> float:
        """The fraction of traces that returned a reply, in the range ``[0, 1]``."""
        return self.successes / self.samples if self.samples else 0.0


@dataclass(slots=True)
class TxOptResult:
    """The result of one run of the remote-admin TX power optimization.

    Attributes:
        target: The node at which the SNR was measured.
        admin_node: The label of the node whose TX power was tuned (the hop before the
            target).
        path: The forced path that the traces walked (hashes separated by commas).
        original_tx: The TX power of the admin node before the sweep, if MeshTerm could
            read it.
        best_tx: The chosen optimal TX power.
        best_snr: The median target SNR (dB) at ``best_tx``.
        best_success_rate: The trace success rate at ``best_tx``.
        applied: Whether ``best_tx`` was written to the admin node.
        levels: All the measured levels, in the order of the test.
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
        """Return the measured levels, sorted by TX power in ascending order.

        Returns:
            The levels in order of TX power, ready to plot.
        """
        return sorted(self.levels, key=lambda lv: lv.tx_power)
