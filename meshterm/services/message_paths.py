# SPDX-License-Identifier: Apache-2.0
"""Recover the paths a chat message rode in on, from the stored packet log.

The radio's RX log records every frame it decodes — including the relay path each one
traversed — and the recorder has been persisting those as ``packet`` observations all
along. A message heard more than once (a channel broadcast rebroadcast by several
repeaters, a flood that reached us two ways) therefore left one logged frame *per
arrival*, each with its own path and SNR. This module matches those frames back to a
chat message so the chat screen can show, on demand, every way the message reached us:

* **Channel messages** match by content: a ``GRP_TXT`` frame names its channel only by
  a one-byte hash, but we hold the channel's key, so
  :func:`~meshterm.core.channels.decrypt_channel_text` can confirm the channel by MAC
  and recover the plaintext. A frame whose text equals the message's (allowing for the
  ``Name: `` sender prefix convention on either side) is one arrival of that message —
  our own broadcasts included, since a repeater's rebroadcast of us is overheard and
  logged like anything else.
* **Direct messages** ride ECDH-encrypted ``TEXT_MSG`` frames that only the recipient can
  decrypt, so no *content* match is possible from the log. What such a frame carries in
  the clear is its **addressing and its MAC** (:mod:`~meshterm.core.frames`): the one-byte
  key hash of each end, then a two-byte tag over the encrypted body. That MAC is a
  fingerprint of the message itself — copies of one message share it and the next message's
  do not — so direct frames group as exactly as channel frames do, without ever reading
  them. Three filters, in order: the frame ran **between this conversation's two ends**,
  in the **direction this message travelled** (our own sends are ``us → peer``; a received
  message is ``peer → us``), and it carries **the same MAC** as the rest of its group.

  A conversation's messages are then told apart by which MAC group sits nearest the
  message's own time. That leaves one honest gap: history recorded before the MAC was kept
  has none, and those messages fall back to matching by address, direction and time —
  billed as such, because a frame between the right pair in the right window is only
  *probably* this one.

Nothing here transmits; it is a read-model over the repository.
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

#: How far around a channel message the log is searched. Wide, because an inbound
#: message is stamped with the *sender's* clock, which may drift from ours by minutes.
_CHANNEL_WINDOW = timedelta(minutes=15)

#: How far around a direct message the log is searched, before the conversation's own
#: messages narrow it further (see :func:`direct_window`). Generous on its own: a send is
#: retried for a while after it is composed, and an inbound message is stamped with the
#: sender's clock.
_DIRECT_WINDOW = timedelta(seconds=90)

#: The least room left either side of a direct message once its neighbours clamp the
#: window. Two messages composed a second apart still each need somewhere to look, and the
#: midpoint between them would otherwise leave one of them nothing at all.
_MIN_HALF_WINDOW = timedelta(seconds=2)

#: Route types whose ``path`` field is a routing *instruction* rather than a record of
#: where the packet has been — the sender writes the route and the relays consume it. The
#: library names the header's two route bits (see its ``ROUTE_TYPENAMES``); the flooded
#: ones accumulate instead, which is the reading every path here used to get.
_ROUTED_TYPES = frozenset({"DIRECT", "TC_DIRECT"})

#: Frame classes that carry a direct (addressed) text message — the payload class the
#: meshcore library names, spelled exactly as it reports it (see
#: :data:`~meshterm.core.frames.ADDRESSED_CLASSES`, the packet viewer's lane, the
#: dashboard's traffic keys). A near-miss here matches nothing at all and reads as a
#: mesh that never carries direct messages.
_DIRECT_TYPENAMES = frozenset({"TEXT_MSG"})

#: Hex digits of a frame's endpoint hash — the leading byte of a public key, as both
#: ends of a direct frame are addressed by.
_HASH_CHARS = 2 * ENDPOINT_HASH_BYTES


@dataclass(slots=True)
class Arrival:
    """One logged copy of a message reaching this radio.

    Attributes:
        when: When the frame was heard.
        hops: The relay path it rode, in propagation order (empty = arrived direct).
        snr: Reception SNR in dB — of the *last relay*, as with every packet row.
        resend: The sender's resend counter for this copy (0 = the original send),
            recovered from a decrypted channel frame; always 0 for direct frames.
        routed: Whether the frame was **direct-routed** rather than flooded, which decides
            what :attr:`hops` even means. A flooded packet accumulates its path — every
            relay appends itself — so the hops are the route it travelled to reach us. A
            direct-routed packet carries a route its sender wrote and its relays consume,
            so the hops are where it was *going*, and an empty one means the route was used
            up rather than that the packet arrived in a single leap. ``None`` for history
            recorded before the route type was kept.
        copies: How many logged frames this row stands for — always ``1`` as the matchers
            produce them, and more once :func:`collapse` folds a path heard several times
            into one row. A direct send is retried, and each retry is overheard off every
            repeater in earshot, so one message routinely leaves a dozen frames on two
            paths; listing them individually said "twelve arrivals" where the truth was
            "two paths, six times each".
        frame: The logged frame's raw payload — what :func:`message_scope` reads the
            message's scope from. Not part of an arrival's identity (two copies on one path
            are one row whatever else their frames carry), so it takes no part in equality.
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
        """Whether :attr:`hops` says anything about how this copy actually travelled.

        False for a direct-routed frame carrying no path: its route was consumed on the way
        and the field is empty because there is nothing left of it, not because the packet
        crossed no relays. Drawing that as a zero-hop arrival claimed a neighbour we do not
        have — the "impossible direct path" (JP, 2026-09-02).
        """
        return bool(self.hops) or not self.routed


def _frame_hops(observation: Observation) -> tuple[str, ...]:
    """An observation's path field as hop hashes (see :attr:`Arrival.routed` for what it means)."""
    return tuple(h for h in (observation.path or "").split(",") if h)


def _frame_routed(raw: dict) -> bool | None:
    """Whether a frame was direct-routed, or ``None`` where its route type wasn't kept."""
    name = str(raw.get("route_typename") or "").upper()
    return name in _ROUTED_TYPES if name and name != "UNK" else None


def _texts_match(wire: str, stored: str) -> bool:
    """Whether an on-air text and a stored chat text are the same message body.

    An inbound message is stored verbatim off the wire (sender prefix included), so an
    exact match covers it. Our own outbound messages are stored as typed while the
    radio prepends the ``Name: `` sender convention — so the wire text also matches
    when its prefix-stripped body equals the stored text. Comparison is
    whitespace-trimmed but otherwise exact: paths are evidence, and a fuzzy match
    would fabricate some.
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
    """Every logged arrival of a channel message, matched by decrypted content.

    Args:
        repo: The repository holding the packet log.
        message: The chat message whose arrivals to find (in- or outbound).
        channel_name: The conversation's channel name (for the decrypt attempt).
        secret: The channel's 16-byte key.

    Returns:
        The matching arrivals, oldest first (resends of the same message included,
        each carrying its ``resend`` counter).
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
    """A node's endpoint hash — the leading byte of its key, as a frame addresses it."""
    text = (key or "").strip().lower()
    return text[:_HASH_CHARS] if len(text) >= _HASH_CHARS else ""


def direct_window(repo: Repository, message: ChatMessage) -> tuple[datetime, datetime]:
    """The span of log to search for one direct message's frames.

    :data:`_DIRECT_WINDOW` either side, then clamped by the conversation's own messages —
    only those travelling the **same way**, because the two directions are stamped by two
    different clocks (see :meth:`~meshterm.persistence.repository.Repository
    .direct_message_bounds`). Without any clamp the flat window is wildly too wide at
    conversational pace: nine messages of one real exchange fell inside a single ±90 s
    window, so each of them claimed all nine messages' frames as its own.

    The two directions are clamped differently, because we know very different things about
    them:

    * **Our own sends** are stamped as they leave, by our own clock, so no frame of one can
      predate it — the window opens *at* the message (less a moment for jitter) and runs to
      the next send. Retries follow the send, so all the room goes forward.
    * **A received message** carries the sender's clock, which may sit either side of when
      we actually heard it. Its window stays centred, reaching halfway to the messages
      before and after it.

    Never narrower than :data:`_MIN_HALF_WINDOW` either side, so two messages a second apart
    still each have somewhere to look.

    Args:
        repo: The repository holding the messages.
        message: The message to bound a search around.

    Returns:
        ``(start, end)`` for :meth:`~meshterm.persistence.repository.Repository
        .packet_frames_between`.
    """
    at = message.created_at
    start, end = at - _DIRECT_WINDOW, at + _DIRECT_WINDOW
    previous, following = repo.direct_message_bounds(message.peer, at, outbound=message.outbound)
    if message.outbound:
        # Our clock, so nothing of this send predates it; the whole window goes forward,
        # up to the next send.
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
    """A frame's ``(source, destination)`` endpoint hashes, lowercased (empty when absent)."""
    return (
        str(raw.get("src_hash") or "").lower(),
        str(raw.get("dest_hash") or "").lower(),
    )


def _pick_group(groups: dict[str, list[Arrival]], message: ChatMessage) -> list[Arrival]:
    """The MAC group that is this message, out of the ones in the window.

    Each group is one message's frames — same MAC, so the same encrypted body. Which of
    them is *this* message is the one thing the log cannot say, so the clock decides, and it
    decides well: a send's frames start at the moment it was composed and run on through the
    retries, so the group to take is the earliest one that did not start before the message
    did. Falling back to the nearest group covers an inbound message, whose stamp is the
    sender's clock and may sit slightly after the frame we heard.
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
    """Every logged frame of one direct message, and whether they were matched exactly.

    Three filters (see the module docstring): the frame ran between this conversation's two
    ends, in the direction this message travelled, and — where the log kept it — it shares
    a MAC with the rest of its group, which is what makes one message's frames separable
    from the next message's without decrypting either.

    **Direction matters more than it looks.** Both ends' hashes appear on every frame of a
    conversation, whichever way it went, so matching on the pair alone put our own outgoing
    frames into the view of a message we *received* and vice versa. That is where the
    implausible one-hop rows came from: they were our own sends, overheard coming back off
    the two repeaters in earshot, correctly showing one hop because one hop is all they had
    travelled — but shown under an inbound message as if they were its route to us.

    Args:
        repo: The repository holding the packet log.
        message: The chat message whose frames to find.
        self_key: Our own node's key (or prefix) — one end of every frame here.
        peer_key: The conversation peer's key (or prefix) — the other end.

    Returns:
        ``(arrivals, exact)`` — the frames oldest first, and whether a MAC identified them
        as one message rather than merely correlating them by address and time.
    """
    ours, theirs = _endpoint_hash(self_key), _endpoint_hash(peer_key)
    # Which way this message travelled, so a send's own frames and a received message's
    # never appear under each other.
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
    # History recorded before the MAC was kept: address, direction and time are all there
    # is, and the caller says so rather than claiming more.
    return loose, False


def collapse(arrivals: Sequence[Arrival]) -> list[Arrival]:
    """Fold arrivals that rode the same path into one row apiece, counting the copies.

    A direct send is retried and each retry is overheard off every repeater in earshot, so
    one message leaves a dozen frames on two paths. The graph has always drawn the *distinct*
    paths; the row list showed every frame, which read as twelve arrivals where the truth was
    two paths heard six times each. Each surviving row keeps its **first** sighting's time
    and its **best** SNR — the earliest is when the path first worked, and the strongest is
    what the path is capable of.

    Args:
        arrivals: The matched frames, oldest first.

    Returns:
        One :class:`Arrival` per distinct path, in first-heard order, each carrying its
        :attr:`~Arrival.copies` count.
    """
    # Keyed on the route type as well as the hops: the same hashes mean two different
    # things under the two types, so folding them together would merge a route travelled
    # with a route intended.
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
    """The one scope a message was flooded under, read off its arrivals' frames.

    A transport code is a keyed hash of the payload alone — neither the header nor the
    path is in it (see :mod:`~meshterm.core.regions`) — so every relay's copy carries the
    sender's code unchanged, and one message has **one** scope however many ways it
    arrived. That is why the dialog states it once rather than per arrival row.

    Which frame answers still matters, because not every copy is a flood: a direct-routed
    copy has no scope at all. So the first scoped reading wins, a *named* one over one no
    known region reproduces (they are the same code, but a name is the better answer when
    any copy yields one); failing that, any plain-flood copy makes the message
    ``unscoped``; and a message heard only direct-routed has no scope to state.

    Args:
        arrivals: The message's arrivals, as the matchers (or :func:`collapse`) return
            them — each carrying its logged frame.
        scope_of: Reads a frame's scope (``ctx.region_store.scope_of``).

    Returns:
        The message's scope, or ``None`` where no copy was a flood.
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
    """How many distinct relay paths a set of arrivals covers."""
    return len({a.hops for a in arrivals})
