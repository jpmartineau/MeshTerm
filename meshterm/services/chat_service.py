# SPDX-License-Identifier: Apache-2.0
"""The chat service: message persistence, unread tracking, and the send path.

Like the passive monitor, this service is not a listener in its own right — the always-on
:class:`~meshterm.services.event_hub.EventHub` (``ctx.events``) does the listening. This
service is one of its subscribers: the one that writes every inbound
:class:`~meshterm.core.models.Message` to history as a
:class:`~meshterm.core.models.ChatMessage`, so a conversation transcript survives across
sessions. It also owns the per-conversation *unread* counters shown in the menu header, and
the outbound send path (so both the CLI and the live chat screen record what they send the
same way).

The service is session-scoped state on the :class:`~meshterm.context.AppContext`
(``ctx.chat``). Unlike the monitor it has no on/off preference: messages addressed to us are
communications, not overheard noise, so they are always recorded while a device is listening.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..core.channels import channel_identity
from ..core.connection import Unsubscribe
from ..core.events import EventKind, MeshEvent
from ..core.models import (
    ChatMessage,
    Contact,
    Message,
    is_direct_messageable,
    utcnow,
)
from ..core.regions import WILDCARD, normalize

if TYPE_CHECKING:
    from ..context import AppContext


def _fallback_channel_id(idx: int | None) -> str:
    """Identity for a channel we can't read (an unconfigured/unknown slot).

    Only used when the device has no channel to identify at ``idx`` — a degenerate case a
    configured channel never hits. It is still slot-derived (there is nothing intrinsic to
    key on), so it is the one place the old slot coupling survives, and only for channels
    that have no real identity yet.
    """
    return f"slot:{idx}"


class ChatService:
    """Records inbound messages to history and tracks unread counts per conversation.

    Interact through the async lifecycle methods (:meth:`start`, :meth:`stop`,
    :meth:`aclose`), the send helpers (:meth:`send_direct`, :meth:`send_channel`), and the
    unread accessors. The currently-open conversation is registered via :meth:`set_active`
    so its inbound messages don't inflate the unread badge, and :meth:`_notifies` decides
    which messages raise it at all — the badge counts only conversations the Chat picker can
    list, while history records everything.
    """

    def __init__(self, ctx: AppContext) -> None:
        """Initialize the service bound to an application context.

        Args:
            ctx: The shared application context (device, repository, event hub, logger).
        """
        self._ctx = ctx
        self._unsubscribe: Unsubscribe | None = None
        self._run_id: int | None = None
        self._unread: dict[str, int] = {}
        self._active: str | None = None
        self._session_count = 0
        # Inbound messages are recorded off a single serialized worker (see
        # :meth:`_process_inbound`) so channel messages — whose identity is resolved with an
        # async device read — are still stored in the order they arrived. The queue is the
        # hand-off from the (synchronous) hub callback to that worker.
        self._queue: asyncio.Queue[Message] | None = None
        self._worker: asyncio.Task[None] | None = None
        # Last-known channel slot index -> intrinsic identity. Inbound resolution reads the
        # slot fresh every time (see :meth:`channel_id_for`), so this is not the source of
        # truth — only a warm map for priming/backfill and a fallback when a read fails.
        self._channel_ids: dict[int, str] = {}

    @property
    def active(self) -> bool:
        """Whether the service is subscribed to the hub and recording messages."""
        return self._unsubscribe is not None

    @property
    def session_count(self) -> int:
        """Number of inbound messages recorded since this process started."""
        return self._session_count

    def unread(self, key: str) -> int:
        """Return the unread count for one conversation key."""
        return self._unread.get(key, 0)

    def unread_total(self) -> int:
        """Return the total unread count across all conversations."""
        return sum(self._unread.values())

    def set_active(self, key: str | None) -> None:
        """Mark ``key`` as the open conversation (or ``None`` when none is open).

        The open conversation is immediately cleared of unread and won't accrue more while
        it stays active, since the user is looking at it.

        Args:
            key: The conversation key now open, or ``None`` on close.
        """
        self._active = key
        if key is not None:
            self._unread.pop(key, None)

    def clear_unread(self, key: str) -> None:
        """Drop a conversation's unread count without making it the active thread.

        Used when a channel is muted: the channel's outstanding unread is zeroed (per the
        mute UX) while the currently-open conversation, if any, is left untouched — unlike
        :meth:`set_active`, which also re-points the active thread.

        Args:
            key: The conversation key to clear.
        """
        self._unread.pop(key, None)

    async def refresh_channels(self) -> None:
        """Rebuild the slot-index -> channel-identity map from the device's channel table.

        The map is what lets an inbound message (which carries only a slot index) be recorded
        against its channel's intrinsic identity. It is rebuilt wholesale so a reordered,
        renamed, re-keyed, or cleared slot is reflected accurately.

        Read through the session cache, which is the same slot probe the channel manager is
        about to make anyway — so a channel edit costs *one* walk of the device rather than
        this one plus the manager's. Its own walk was also unbounded where the shared probe
        is not: it skipped empty slots and kept going to the 64-slot cap, so on firmware that
        never rejects an index every single edit paid 64 round-trips before the screen could
        redraw. That is most of what "applying a change takes a while" was.
        """
        slots = await self._ctx.devstate.channel_slots()
        self._channel_ids = {slot.idx: channel_identity(slot.name, slot.secret) for slot in slots}

    async def channel_id_for(self, idx: int) -> str:
        """Resolve a channel slot index to its intrinsic identity, reading the slot fresh.

        The slot's *current* occupant is read from the device every call, so the identity
        always reflects the channel that is actually in that slot right now — even if the
        slots were reordered or re-keyed (in this app, via the CLI/config tool, or on another
        client such as the phone app) since the cache was last built. Trusting a session-long
        slot→identity cache instead is exactly what let a reorder misfile a channel's
        messages. The resolved identity refreshes the cache; a transient read failure falls
        back to the last identity we saw for the slot (so a blip doesn't split a channel's
        history), and only a genuinely empty slot yields the slot-derived identity.

        Args:
            idx: The channel slot index the wire reported.

        Returns:
            The channel's identity, suitable for keying its history.
        """
        try:
            device = await self._ctx.device()
            payload = await device.get_channel(idx)
        except Exception as exc:  # noqa: BLE001 - fall back rather than misroute or drop
            self._ctx.log.debug("chat: channel read for slot %s failed: %s", idx, exc)
            return self._channel_ids.get(idx) or _fallback_channel_id(idx)
        if payload:
            name = str(payload.get("channel_name") or "")
            secret = bytes(payload.get("channel_secret") or b"\x00" * 16)
            cid = channel_identity(name, secret)
            self._note_slot(idx, cid)
            return cid
        self._note_slot(idx, None)
        return _fallback_channel_id(idx)

    def _note_slot(self, idx: int, cid: str | None) -> None:
        """Record a slot's freshly-read identity, dropping stale screen caches when it moved.

        This resolution is the one place in the app that reads a channel slot *per message*, so
        it is also the first to notice the device's channel table changing under a running
        session — a slot re-keyed, cleared, or reordered on another client (the phone app), which
        nothing in this app invalidates. The screens read their channel list from the session
        cache (:meth:`~meshterm.services.device_state.DeviceState.channel_slots`), which is held
        until an *in-app* channel edit drops it; left alone it would keep listing the channels the
        device had at connect time while the recorder files new messages under the identity the
        slot actually carries now. That divergence is invisible except as a symptom: the header's
        unread badge counts a conversation the picker has no row for. So a changed slot drops the
        cache here, and the next screen re-reads the device's real table.

        Args:
            idx: The channel slot just read.
            cid: The identity now in that slot, or ``None`` if the slot came back empty.
        """
        known = self._channel_ids.get(idx)
        if cid is None:
            self._channel_ids.pop(idx, None)
        else:
            self._channel_ids[idx] = cid
        if known is None or known == cid:
            return
        self._ctx.log.info(
            "chat: channel slot %s changed identity; re-reading the device's channels", idx
        )
        devstate = getattr(self._ctx, "devstate", None)
        if devstate is not None:
            devstate.invalidate_channels()

    async def start(self) -> None:
        """Begin recording inbound messages to history. Idempotent.

        Ensures the always-on event hub is running (which opens the device connection and
        may raise if no device can be selected), then subscribes to its message stream and
        opens a ``chat`` run for the recorded messages to link to.

        Raises:
            Exception: Propagates any device/hub error; the caller can surface it and the
                service simply stays inactive.
        """
        if self.active:
            return
        await self._ctx.events.start()
        run_id = self._ctx.repo.start_run("chat", {"mode": "background"}, self._ctx.profile_name)
        self._queue = asyncio.Queue()
        self._worker = asyncio.ensure_future(self._process_inbound(run_id))

        def on_event(event: MeshEvent) -> None:
            # Runs on the event loop as messages arrive; keep it cheap and non-blocking. It
            # only hands the message to the worker queue — resolution and the database write
            # happen off the worker so a slow device read can never stall the event pump.
            message = event.message
            queue = self._queue
            if message is not None and queue is not None:
                queue.put_nowait(message)

        self._unsubscribe = self._ctx.events.subscribe(on_event, EventKind.MESSAGE)
        self._run_id = run_id
        self._ctx.log.info("chat recording (run %s)", run_id)
        await self._prime_channels()

    async def _prime_channels(self) -> None:
        """Warm the fallback channel-identity map and backfill legacy (index-keyed) history.

        Reading the channels up front seeds the map that :meth:`channel_id_for` falls back to
        when a per-message read fails. It also backfills any pre-identity messages using the
        channels currently in each slot — best-effort, since the old slot-to-channel mapping
        wasn't recorded.
        """
        try:
            await self.refresh_channels()
            if self._channel_ids:
                self._ctx.repo.backfill_channel_ids(dict(self._channel_ids))
        except Exception as exc:  # noqa: BLE001 - priming is best-effort, never fatal
            self._ctx.log.debug("chat: channel priming failed: %s", exc)

    async def stop(self) -> None:
        """Stop recording and close the run record. Idempotent.

        A no-op if not recording. The event hub keeps listening; only this service's
        recording subscription and its inbound worker are removed.
        """
        if not self.active:
            return
        try:
            assert self._unsubscribe is not None
            self._unsubscribe()
        finally:
            self._unsubscribe = None
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        self._queue = None
        if self._run_id is not None:
            self._ctx.repo.finish_run(self._run_id, "ok", {"messages": self._session_count})
            self._run_id = None

    async def aclose(self) -> None:
        """Stop recording at session end."""
        await self.stop()

    async def _process_inbound(self, run_id: int) -> None:
        """Serially resolve and record queued inbound messages, preserving arrival order.

        A single worker drains the queue so that channel messages — whose identity is
        resolved with an async device read (see :meth:`channel_id_for`) — are still stored in
        the order they arrived. History is ordered by insertion, so recording concurrently
        could interleave two messages by their differing read latencies; the serial worker
        makes that impossible while still resolving each against the live device.

        Args:
            run_id: The owning background ``chat`` run to link recorded messages to.
        """
        assert self._queue is not None
        while True:
            message = await self._queue.get()
            try:
                channel_id = (
                    await self.channel_id_for(message.channel) if message.is_channel else None
                )
                notify = await self._notifies(message, channel_id)
                self._store_inbound(run_id, message, channel_id, notify=notify)
            except Exception as exc:  # noqa: BLE001 - one bad message must not kill the worker
                self._ctx.log.debug("chat: failed to record message: %s", exc)
            finally:
                self._queue.task_done()

    def _store_inbound(
        self,
        run_id: int,
        message: Message,
        channel_id: str | None,
        *,
        notify: bool = True,
    ) -> None:
        """Persist an inbound message under a resolved identity and bump its unread count.

        The transcript is always recorded; the unread bump is skipped for the open
        conversation (the user is already reading it) and whenever ``notify`` is ``False``
        (a muted channel, or a conversation the Chat picker can't list — see
        :meth:`_notifies`). A message that doesn't notify is still written to history, so
        opening its conversation later shows everything.
        """
        chat = ChatMessage.from_message(message, channel_id=channel_id)
        self._session_count += 1
        if notify and chat.key != self._active:
            self._unread[chat.key] = self._unread.get(chat.key, 0) + 1
        try:
            self._ctx.repo.record_chat_message(chat, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 - never let logging break the subscription
            self._ctx.log.debug("chat: failed to record message: %s", exc)

    async def _notifies(self, message: Message, channel_id: str | None) -> bool:
        """Whether this message should raise the header's unread badge — THE badge rule.

        The badge is a pointer, not a tally: its whole job is to send you to a conversation
        you can open. So it counts only what the Chat picker can actually list — the device's
        configured channels and the contacts you can direct-message. Everything else is
        recorded to history and left silent, because a count with no row to open is a badge
        that can never be cleared.

        What that excludes is, in practice, machine traffic rather than correspondence:

        * A direct message from a node that isn't a DM *recipient* — a repeater, room server,
          or sensor (see :func:`~meshterm.core.models.is_direct_messageable`), which the picker
          doesn't list. Every remote-CLI reply from a repeater you administer arrives this way.
        * A direct message from a sender the contact table doesn't hold at all.
        * A channel message the device has no configured slot for — including one whose slot
          couldn't be identified, which carries the slot-derived fallback identity.
        * A muted channel (see :class:`~meshterm.core.mute_store.MuteStore`), the one
          suppression the user asks for by hand rather than one the device implies.

        The device is consulted through the session cache, so this costs nothing per message
        beyond the channel read the identity already needed. Anything the cache can't answer
        gets the benefit of the doubt and notifies — a missing device state should never be
        the reason a real message goes unseen.

        Args:
            message: The inbound message.
            channel_id: Its resolved channel identity, for a channel message.

        Returns:
            ``True`` to bump the conversation's unread count.
        """
        if message.is_channel:
            store = getattr(self._ctx, "mute_store", None)
            if store is not None and store.is_muted(channel_id):
                return False
            return await self._is_listed_channel(channel_id)
        return await self._is_listed_contact(message.sender)

    async def _is_listed_channel(self, channel_id: str | None) -> bool:
        """Whether a channel identity is one of the device's configured channel slots.

        A message whose slot couldn't be identified carries the slot-derived fallback identity
        (see :func:`_fallback_channel_id`), which names no channel and matches no row.
        """
        if not channel_id or channel_id.startswith("slot:"):
            return False
        devstate = getattr(self._ctx, "devstate", None)
        if devstate is None:
            return True  # no cache to consult: notify rather than swallow
        try:
            slots = await devstate.channel_slots()
        except Exception as exc:  # noqa: BLE001 - a read failure must not silence a message
            self._ctx.log.debug("chat: channel-slot lookup failed: %s", exc)
            return True
        return any(channel_identity(slot.name, slot.secret) == channel_id for slot in slots)

    async def _is_listed_contact(self, sender: str | None) -> bool:
        """Whether a direct message's sender is a contact the Chat picker lists.

        Matched the way the live chat matches an inbound sender to its thread: either prefix
        may be the shorter one, since what the wire addresses and what the contact table stores
        need not be the same width.
        """
        if not sender:
            return False
        devstate = getattr(self._ctx, "devstate", None)
        if devstate is None:
            return True  # no cache to consult: notify rather than swallow
        try:
            contacts = await devstate.contacts()
        except Exception as exc:  # noqa: BLE001 - a read failure must not silence a message
            self._ctx.log.debug("chat: contact lookup failed: %s", exc)
            return True
        peer = sender.lower()
        for contact in contacts:
            for ident in (contact.key_prefix, contact.public_key[:12]):
                ident = (ident or "").lower()
                if ident and (ident.startswith(peer) or peer.startswith(ident)):
                    return is_direct_messageable(contact.node_type)
        return False

    async def _deliver_direct(self, contact: Contact, text: str):
        """Transmit a direct message, softly retrying until it is acknowledged.

        A single logical send makes one initial transmission plus up to
        ``preferences.direct_message_soft_retries`` soft retries (none by default — one shot,
        since a re-send is a second transmission on a shared mesh; two is the ceiling, for
        three tries in all): the message is re-sent only when an attempt goes unacknowledged,
        and each device
        call already blocks for a full delivery-ack window (see
        :meth:`~meshterm.core.connection.Device.send_direct_message`) before returning, so the
        retries are naturally spaced by that window rather than hammering the radio. The loop
        stops the moment an ack arrives; if none ever does, the last (``None``) result stands
        and the message is recorded unacknowledged — leaving Ctrl-R in the chat screen as the
        user-driven retry on top of these automatic ones.

        Args:
            contact: The recipient.
            text: The message body.

        Returns:
            The delivery :class:`~meshterm.core.models.Ack` from the first attempt that landed,
            or ``None`` if every attempt went unacknowledged.

        Raises:
            Exception: Propagates a hard send failure (the companion rejecting the send); such
                a rejection is not retried, since it is not a lost-in-the-mesh timeout.
        """
        device = await self._ctx.device()
        attempts = max(0, self._ctx.preferences.direct_message_soft_retries) + 1
        ack = None
        for attempt in range(1, attempts + 1):
            ack = await device.send_direct_message(contact, text)
            if ack is not None:
                return ack
            if attempt < attempts:
                self._ctx.log.debug(
                    "chat: DM to %s unacked on try %d/%d; soft-retrying",
                    contact.name,
                    attempt,
                    attempts,
                )
        return ack

    async def send_direct(self, contact: Contact, text: str) -> ChatMessage:
        """Send a direct message to a contact and record it in history.

        Delivery is attempted with up to ``preferences.direct_message_soft_retries`` automatic
        soft retries (see :meth:`_deliver_direct`) before the message is recorded as
        unacknowledged.

        Args:
            contact: The recipient.
            text: The message body.

        Returns:
            The recorded outbound :class:`ChatMessage` (its ``acked`` reflects whether a
            delivery acknowledgement arrived within the soft-retry budget).
        """
        ack = await self._deliver_direct(contact, text)
        chat = ChatMessage(
            text=text,
            outbound=True,
            is_channel=False,
            peer=contact.key_prefix or contact.public_key[:12] or None,
            peer_name=contact.name,
            acked=ack is not None,
            created_at=utcnow(),
        )
        chat.row_id = self._ctx.repo.record_chat_message(chat, run_id=self._run_id)
        return chat

    async def resend_direct(self, contact: Contact, message: ChatMessage) -> ChatMessage:
        """Re-attempt delivery of an unacknowledged direct message, updating it in place.

        Used to retry a message that was transmitted but never acknowledged (its ``acked``
        is ``False``). The same stored row is reused — its delivery state is updated rather
        than a duplicate transcript entry created — so the message simply flips to delivered
        (or stays unacknowledged for another retry). Like an initial send, each manual retry
        makes up to ``preferences.direct_message_soft_retries`` soft retries of its own (see
        :meth:`_deliver_direct`).

        Args:
            contact: The recipient.
            message: The previously-sent :class:`ChatMessage` to re-transmit; mutated in
                place with the new delivery state.

        Returns:
            The same ``message``, with :attr:`~ChatMessage.acked` refreshed.
        """
        ack = await self._deliver_direct(contact, message.text)
        message.acked = ack is not None
        if message.row_id is not None:
            self._ctx.repo.update_chat_ack(message.row_id, message.acked)
        return message

    def channel_scope(self, channel_id: str | None) -> str | None:
        """The region a channel's messages are sent under, or ``None`` for the device default.

        Read from :class:`~meshterm.core.region_store.RegionStore` by the channel's intrinsic
        identity, so the scope follows the channel across slots. A context with no store (a
        test's, or a surface that never built one) has no channel scopes at all.
        """
        store = getattr(self._ctx, "region_store", None)
        return store.channel_scope(channel_id) if store is not None else None

    async def send_channel(
        self,
        index: int,
        text: str,
        *,
        label: str | None = None,
        scope: str | None = None,
    ) -> ChatMessage:
        """Broadcast a message on a channel, under the channel's scope, and record it.

        The channel's identity is resolved *before* the send, because it is what names the
        scope the message goes out under (:meth:`channel_scope`). The send itself is
        :meth:`~meshterm.core.connection.Device.send_channel_in_scope` — set the session
        scope, send, restore, with no other flood let in between — and a scope the radio
        can't take stops the message rather than sending it some other way.

        Args:
            index: The channel slot to transmit on.
            text: The message body.
            label: A display label for the channel (e.g. ``#general``), stored for the
                transcript.
            scope: Overrides the channel's own scope for this one message: a region name,
                or :data:`~meshterm.core.regions.WILDCARD` (``*``) to send it unscoped —
                the resend for a scoped message nothing relayed. ``None`` uses the
                channel's scope, or the device default when the channel has none.

        Returns:
            The recorded outbound :class:`ChatMessage`, its :attr:`~ChatMessage.scope` the
            one it went out under.

        Raises:
            ~meshterm.core.connection.FloodScopeError: If the radio can't send under the
                scope; nothing was sent and nothing is recorded.
        """
        device = await self._ctx.device()
        channel_id = await self.channel_id_for(index)
        asked = scope if scope is not None else self.channel_scope(channel_id)
        await device.send_channel_in_scope(index, text, asked)
        chat = ChatMessage(
            text=text,
            outbound=True,
            is_channel=True,
            channel_id=channel_id,
            channel_idx=index,
            peer_name=label,
            created_at=utcnow(),
            scope=await self._sent_under(asked),
        )
        chat.row_id = self._ctx.repo.record_chat_message(chat, run_id=self._run_id)
        return chat

    async def _sent_under(self, asked: str | None) -> str | None:
        """What a channel send asked to go out under, as the transcript records it.

        A scope that was asked for is what it went under — the send refuses rather than
        substitute. A plain send went under the device's default scope, which is read (once
        a session, through the devstate cache): its name when one is set, ``*`` when none
        is — a plain flood with no default *is* unscoped. A default that can't be read
        (firmware before 1.15 has none to read; a link blip) records ``None``: unknown is
        the honest answer, and the transcript never claims a scope it didn't see.
        """
        bare = normalize(asked) if asked else ""
        if bare:
            return bare
        devstate = getattr(self._ctx, "devstate", None)
        if devstate is None:
            return None
        try:
            default = await devstate.default_scope()
        except Exception as exc:  # noqa: BLE001 - unknown, not a failed send
            self._ctx.log.debug("chat: default scope unreadable: %s", exc)
            return None
        return normalize(default) or WILDCARD
