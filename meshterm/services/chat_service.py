# SPDX-License-Identifier: Apache-2.0
"""The chat service: message storage, unread counts, and the send path.

Like the passive monitor, this service does not listen by itself. The always-on
:class:`~meshterm.services.event_hub.EventHub` (``ctx.events``) does the listening. This
service is one of its subscribers. It writes each inbound
:class:`~meshterm.core.models.Message` to history as a
:class:`~meshterm.core.models.ChatMessage`, so that the transcript of a conversation stays
from one session to the next. It also owns the unread counter of each conversation, which
the menu header shows. Also, it owns the outbound send path, so that the CLI and the live
chat screen store what they send in the same way.

The service is state that lasts for the session, on the
:class:`~meshterm.context.AppContext` (``ctx.chat``). Unlike the monitor, it has no on/off
preference. Messages addressed to our node are communications, not overheard noise. Thus
MeshTerm always stores them while a device listens.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..core.channels import channel_identity
from ..core.connection import Unsubscribe
from ..core.events import EventKind, MeshEvent
from ..core.models import (
    ChatMessage,
    Contact,
    Delivery,
    Message,
    is_direct_messageable,
    utcnow,
)
from ..core.regions import WILDCARD, normalize

if TYPE_CHECKING:
    from ..context import AppContext


#: How many unacknowledged messages continue to wait for a late ack. The device itself
#: remembers the codes of only its last eight sends (MeshCore's ``EXPECTED_ACK_TABLE_SIZE``),
#: and it pushes no ack for an older send. Thus a few more than eight covers each ack that
#: is still possible.
_AWAITING_CAP = 16

#: Called with a message when its ack arrives after the send stopped its wait for the ack.
LateAck = Callable[[ChatMessage], None]


def _fallback_channel_id(idx: int | None) -> str:
    """The identity of a channel that MeshTerm cannot read.

    This is a slot that is not configured or not known. MeshTerm uses this identity only
    when the device has no channel to identify at ``idx``. This unusual case never occurs
    for a configured channel. The identity still comes from the slot (there is nothing
    intrinsic to use as a key). Thus it is the one place where the old link to the slot
    remains, and only for channels that have no real identity yet.
    """
    return f"slot:{idx}"


class ChatService:
    """Stores inbound messages in history, and keeps the unread count of each conversation.

    Use the async lifecycle methods (:meth:`start`, :meth:`stop`, :meth:`aclose`), the
    send helpers (:meth:`send_direct`, :meth:`send_channel`), and the unread accessors. The
    conversation that is open now is registered through :meth:`set_active`, so that its
    inbound messages do not increase the unread badge. :meth:`_notifies` decides which
    messages increase the badge at all. The badge counts only conversations that the Chat
    picker can list, but history stores all messages.
    """

    def __init__(self, ctx: AppContext) -> None:
        """Initialize the service, bound to an application context.

        Args:
            ctx: The shared application context (device, repository, event hub, logger).
        """
        self._ctx = ctx
        self._unsubscribe: Unsubscribe | None = None
        self._run_id: int | None = None
        self._unread: dict[str, int] = {}
        self._active: str | None = None
        self._session_count = 0
        # A single serialized worker stores the inbound messages (refer to
        # :meth:`_process_inbound`). An async device read resolves the identity of a channel
        # message, but the worker makes sure that MeshTerm still stores these messages in
        # the order in which they arrived. The queue moves each message from the
        # (synchronous) hub callback to that worker.
        self._queue: asyncio.Queue[Message] | None = None
        self._worker: asyncio.Task[None] | None = None
        # The last known channel slot index -> the intrinsic identity. The resolution of an
        # inbound message reads the slot again each time (refer to :meth:`channel_id_for`).
        # Thus this map is not the source of truth. It is only a warm map for the priming
        # and the backfill, and a fallback when a read fails.
        self._channel_ids: dict[int, str] = {}
        # Sent messages whose ack did not arrive before the send stopped its wait. The key
        # is the ack code that a late ack will carry (the code of each attempt, for a
        # message that was tried again). The oldest is first. Each message has the listener
        # that wants to know when its ack arrives (refer to :meth:`_settle_ack`).
        self._awaiting: OrderedDict[str, tuple[ChatMessage, LateAck | None]] = OrderedDict()
        self._ack_unsubscribe: Unsubscribe | None = None

    @property
    def active(self) -> bool:
        """Whether the service is subscribed to the hub and stores messages."""
        return self._unsubscribe is not None

    @property
    def session_count(self) -> int:
        """The number of inbound messages stored from the start of this process."""
        return self._session_count

    def unread(self, key: str) -> int:
        """Return the unread count for one conversation key."""
        return self._unread.get(key, 0)

    def unread_total(self) -> int:
        """Return the total unread count of all conversations."""
        return sum(self._unread.values())

    def set_active(self, key: str | None) -> None:
        """Mark ``key`` as the open conversation (or ``None`` when no conversation is open).

        The unread count of the open conversation is cleared immediately. The count does
        not increase while the conversation stays active, because the user looks at it.

        Args:
            key: The conversation key that is open now, or ``None`` on close.
        """
        self._active = key
        if key is not None:
            self._unread.pop(key, None)

    def clear_unread(self, key: str) -> None:
        """Remove the unread count of a conversation, but do not make it the active thread.

        MeshTerm uses this method when the user mutes a channel. The unread count of the
        channel becomes zero (as the mute UX specifies). The conversation that is open now,
        if there is one, does not change. This is different from :meth:`set_active`, which
        also changes the active thread.

        Args:
            key: The conversation key to clear.
        """
        self._unread.pop(key, None)

    async def refresh_channels(self) -> None:
        """Rebuild the map of slot index -> channel identity from the device's channel table.

        This map lets MeshTerm store an inbound message (which carries only a slot index)
        under the intrinsic identity of its channel. MeshTerm builds the full map again, so
        that the map correctly shows a slot that was reordered, renamed, given a new key, or
        cleared.

        The method reads through the session cache. That cache uses the same slot probe
        that the channel manager will make next in any case. Thus a channel edit costs one
        walk of the device, not this walk and also the walk of the manager. The old walk of
        this method also had no limit, but the shared probe has one. The old walk skipped
        empty slots and continued to the cap of 64 slots. Thus on firmware that never
        rejects an index, each edit paid 64 round trips before the screen could paint
        again. That was most of the cause of "applying a change takes a while".
        """
        slots = await self._ctx.devstate.channel_slots()
        self._channel_ids = {slot.idx: channel_identity(slot.name, slot.secret) for slot in slots}

    async def channel_id_for(self, idx: int) -> str:
        """Resolve a channel slot index to its intrinsic identity, with a new read of the slot.

        Each call reads the current occupant of the slot from the device. Thus the identity
        always shows the channel that is in that slot now. This is true also if the slots
        were reordered or got new keys after the last build of the cache (in this app,
        through the CLI or config tool, or on another client such as the phone app). A
        cache of slot→identity that lasted for all the session let a reorder store the
        messages of a channel under the wrong channel. The resolved identity updates the
        cache. If a read fails for a short time, the method uses the last identity that
        MeshTerm read for the slot (so that a short failure does not split the history of a
        channel). Only a slot that is really empty gives the identity that comes from the
        slot.

        Args:
            idx: The channel slot index that the device reported.

        Returns:
            The identity of the channel, to use as the key of its history.
        """
        try:
            device = await self._ctx.device()
            payload = await device.get_channel(idx)
        except Exception as exc:  # noqa: BLE001 - a fallback, not a misroute or a loss
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
        """Keep the new identity of a slot, and clear old screen caches when it changed.

        This resolution is the one place in the app that reads a channel slot for each
        message. Thus it is also the first to notice when the channel table of the device
        changes during a session: a slot that got a new key, was cleared, or was reordered on
        another client (the phone app). Nothing in this app invalidates such a change. The
        screens read their channel list from the session cache
        (:meth:`~meshterm.services.device_state.DeviceState.channel_slots`). That cache stays
        until a channel edit in the app removes it. If nothing removes it, the screens
        continue to list the channels that the device had at connect time, while the
        recorder stores new messages under the identity that the slot carries now. This
        difference is not visible, except as a symptom: the unread badge in the header
        counts a conversation for which the picker has no row. Thus a changed slot removes
        the cache here, and the next screen reads the real table of the device again.

        Args:
            idx: The channel slot that was just read.
            cid: The identity that is now in that slot, or ``None`` if the slot was empty.
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
        """Start to store inbound messages in history. Idempotent.

        Makes sure that the always-on event hub runs. (The hub opens the device connection,
        and it can raise an error if no device can be selected.) Then the method subscribes
        to the message stream of the hub, and opens a ``chat`` run to which the stored
        messages link.

        Raises:
            Exception: Any device or hub error goes to the caller. The caller can show it,
                and the service stays inactive.
        """
        if self.active:
            return
        await self._ctx.events.start()
        run_id = self._ctx.repo.start_run("chat", {"mode": "background"}, self._ctx.profile_name)
        self._queue = asyncio.Queue()
        self._worker = asyncio.ensure_future(self._process_inbound(run_id))

        def on_event(event: MeshEvent) -> None:
            # This runs on the event loop when messages arrive. Keep it fast, and do not let
            # it block. It only puts the message on the worker queue. The resolution and the
            # database write occur in the worker, so that a slow device read can never stop
            # the event pump.
            message = event.message
            queue = self._queue
            if message is not None and queue is not None:
                queue.put_nowait(message)

        self._unsubscribe = self._ctx.events.subscribe(on_event, EventKind.MESSAGE)
        self._ack_unsubscribe = self._ctx.events.subscribe(self._on_ack, EventKind.ACK)
        self._run_id = run_id
        self._ctx.log.info("chat recording (run %s)", run_id)
        await self._prime_channels()

    async def _prime_channels(self) -> None:
        """Warm the fallback map of channel identities, and backfill old history keyed by index.

        A read of the channels at the start fills the map that :meth:`channel_id_for` uses
        as a fallback when the read for a message fails. The method also backfills each
        message from before the identities, with the channels that are now in each slot.
        This is best-effort, because MeshTerm did not store the old map from slot to
        channel.
        """
        try:
            await self.refresh_channels()
            if self._channel_ids:
                self._ctx.repo.backfill_channel_ids(dict(self._channel_ids))
        except Exception as exc:  # noqa: BLE001 - the priming is best-effort, and never fatal
            self._ctx.log.debug("chat: channel priming failed: %s", exc)

    async def stop(self) -> None:
        """Stop the storage of messages, and close the run record. Idempotent.

        If the service does not store messages, this method does nothing. The event hub
        continues to listen. Only the subscription that this service uses to store
        messages, and its inbound worker, are removed.
        """
        if not self.active:
            return
        try:
            assert self._unsubscribe is not None
            self._unsubscribe()
            if self._ack_unsubscribe is not None:
                self._ack_unsubscribe()
        finally:
            self._unsubscribe = None
            self._ack_unsubscribe = None
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
        """Stop the storage of messages at the end of the session."""
        await self.stop()

    async def _process_inbound(self, run_id: int) -> None:
        """Resolve and store queued inbound messages one at a time, in the order of arrival.

        A single worker empties the queue. Thus channel messages are still stored in the
        order in which they arrived, although an async device read resolves their identity
        (refer to :meth:`channel_id_for`). History is ordered by insertion. Thus if MeshTerm
        stored messages concurrently, two messages could change places because of their
        different read latencies. The serial worker prevents this, and it still resolves
        each message against the live device.

        Args:
            run_id: The background ``chat`` run that owns the stored messages, and to which
                they link.
        """
        assert self._queue is not None
        while True:
            message = await self._queue.get()
            try:
                if message.is_post:
                    # The room still sends to our node. That fact decides whether the user
                    # must log in to open the room (refer to RoomService.login_due). This is
                    # true whether or not we already have this post.
                    rooms = getattr(self._ctx, "rooms", None)
                    if rooms is not None:
                        rooms.heard(message.sender)
                    if self._ctx.repo.has_room_post(ChatMessage.from_message(message)):
                        continue  # a post sent again: stored once, counted once
                channel_id = (
                    await self.channel_id_for(message.channel) if message.is_channel else None
                )
                notify = await self._notifies(message, channel_id)
                self._store_inbound(run_id, message, channel_id, notify=notify)
            except Exception as exc:  # noqa: BLE001 - one bad message must not stop the worker
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
        """Store an inbound message under a resolved identity, and increase its unread count.

        MeshTerm always stores the transcript. It does not increase the unread count for
        the open conversation (the user already has it on the screen). It also does not
        increase the count when ``notify`` is ``False``: a muted channel, or a conversation
        that the Chat picker cannot list (refer to :meth:`_notifies`). A message that does
        not notify is still written to history. Thus when the user opens its conversation
        later, the conversation shows all messages.
        """
        chat = ChatMessage.from_message(message, channel_id=channel_id)
        self._session_count += 1
        if notify and chat.key != self._active:
            self._unread[chat.key] = self._unread.get(chat.key, 0) + 1
        try:
            self._ctx.repo.record_chat_message(chat, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 - the storage must never break the subscription
            self._ctx.log.debug("chat: failed to record message: %s", exc)

    async def _notifies(self, message: Message, channel_id: str | None) -> bool:
        """Whether this message increases the unread badge in the header: the only badge rule.

        The badge is a pointer, not a count of all messages. Its only purpose is to send the
        user to a conversation that the user can open. Thus it counts only what the Chat
        picker can list: the configured channels of the device, and the contacts to which
        the user can send a direct message. MeshTerm stores all other messages in history,
        with no badge, because a count with no row to open is a badge that the user can
        never clear.

        In practice, these exclusions are machine traffic, not correspondence:

        * A direct message from a node that is not a recipient of DMs: a repeater or a
          sensor (refer to :func:`~meshterm.core.models.is_direct_messageable`), which the
          picker does not list. Each remote-CLI reply from a repeater that the user
          administers arrives this way.
        * All messages from a room server except its posts. A room is listed, because its
          board is a conversation. But the other things that it sends (its replies to the
          commands of an admin) are not on the board, so they have no row to point at.
        * A direct message from a sender that the contact table does not have at all.
        * A channel message for which the device has no configured slot. This includes a
          message whose slot could not be identified, which carries the fallback identity
          that comes from the slot.
        * A muted channel (refer to :class:`~meshterm.core.mute_store.MuteStore`). This is
          the one suppression that the user asks for, not one that the device implies.

        The method asks the device through the session cache. Thus this rule costs nothing
        for each message, more than the channel read that the identity already made.
        When the cache cannot answer, the message gets the benefit of the doubt and
        notifies. A missing device state must never be the reason that the user does not
        see a real message.

        Args:
            message: The inbound message.
            channel_id: Its resolved channel identity, for a channel message.

        Returns:
            ``True`` to increase the unread count of the conversation.
        """
        if message.is_channel:
            store = getattr(self._ctx, "mute_store", None)
            if store is not None and store.is_muted(channel_id):
                return False
            return await self._is_listed_channel(channel_id)
        return await self._is_listed_contact(message.sender, post=message.is_post)

    async def _is_listed_channel(self, channel_id: str | None) -> bool:
        """Whether a channel identity is one of the configured channel slots of the device.

        A message whose slot could not be identified carries the fallback identity that
        comes from the slot (refer to :func:`_fallback_channel_id`). That identity names no
        channel, and matches no row.
        """
        if not channel_id or channel_id.startswith("slot:"):
            return False
        devstate = getattr(self._ctx, "devstate", None)
        if devstate is None:
            return True  # no cache to ask: notify, and do not hide the message
        try:
            slots = await devstate.channel_slots()
        except Exception as exc:  # noqa: BLE001 - a read failure must not hide a message
            self._ctx.log.debug("chat: channel-slot lookup failed: %s", exc)
            return True
        return any(channel_identity(slot.name, slot.secret) == channel_id for slot in slots)

    async def _is_listed_contact(self, sender: str | None, *, post: bool = False) -> bool:
        """Whether a direct message's sender is a contact that the Chat picker lists for it.

        The match is the same as the match of an inbound sender to its thread in the live
        chat. Either prefix can be the shorter one, because the address on the air and the
        prefix in the contact table do not necessarily have the same width. A companion is
        listed for its messages. A room server is listed for its posts (``post``), and for
        nothing else.
        """
        if not sender:
            return False
        devstate = getattr(self._ctx, "devstate", None)
        if devstate is None:
            return True  # no cache to ask: notify, and do not hide the message
        try:
            contacts = await devstate.contacts()
        except Exception as exc:  # noqa: BLE001 - a read failure must not hide a message
            self._ctx.log.debug("chat: contact lookup failed: %s", exc)
            return True
        peer = sender.lower()
        for contact in contacts:
            for ident in (contact.key_prefix, contact.public_key[:12]):
                ident = (ident or "").lower()
                if ident and (ident.startswith(peer) or peer.startswith(ident)):
                    if contact.is_room:
                        # Chat lists a room after the user joins it, as it lists a channel
                        # after the channel is on a slot. A room that still sends to our
                        # node after we forgot it has no row for the badge to point at.
                        rooms = getattr(self._ctx, "rooms", None)
                        return post and (rooms is None or rooms.joined(contact))
                    return is_direct_messageable(contact.node_type)
        return False

    async def _deliver_direct(self, contact: Contact, text: str) -> tuple[bool, list[str]]:
        """Transmit a direct message, and try again softly until it is acknowledged.

        One logical send makes one first transmission, plus up to
        ``preferences.direct_message_soft_retries`` soft retries. The default is none (one
        transmission only), because a resend is a second transmission on a shared mesh. Two
        is the maximum, for three tries in total. MeshTerm sends the message again only
        when an attempt gets no acknowledgement. Each device call already blocks for a full
        delivery-ack window before it returns (refer to
        :meth:`~meshterm.core.connection.Device.send_direct_message`). Thus that window
        spaces the retries, and the retries do not overload the radio. The loop stops when
        an ack arrives. If no ack arrives, the last (``None``) result stays, and MeshTerm
        stores the message as unacknowledged. Then Ctrl-R in the chat screen is the retry
        that the user starts, in addition to these automatic retries.

        Args:
            contact: The recipient.
            text: The message body.

        Returns:
            Whether an attempt was acknowledged in its wait, and the ack code of each
            attempt. A late ack can still carry any of these codes (the device keeps them).

        Raises:
            Exception: A hard send failure (the companion rejects the send) goes to the
                caller. MeshTerm does not try such a rejection again, because it is not a
                timeout of a message that the mesh lost.
        """
        device = await self._ctx.device()
        attempts = max(0, self._ctx.preferences.direct_message_soft_retries) + 1
        codes: list[str] = []
        for attempt in range(1, attempts + 1):
            delivery: Delivery = await device.send_direct_message(contact, text)
            if delivery.code:
                codes.append(delivery.code)
            if delivery.acked:
                return True, codes
            if attempt < attempts:
                self._ctx.log.debug(
                    "chat: DM to %s unacked on try %d/%d; soft-retrying",
                    contact.name,
                    attempt,
                    attempts,
                )
        return False, codes

    async def send_direct(
        self, contact: Contact, text: str, *, on_late_ack: LateAck | None = None
    ) -> ChatMessage:
        """Send a direct message to a contact, and store it in history.

        MeshTerm tries the delivery with up to ``preferences.direct_message_soft_retries``
        automatic soft retries (refer to :meth:`_deliver_direct`). Then it stores the
        message as unacknowledged, which means not confirmed yet. An ack that arrives later
        still settles it (refer to :meth:`_settle_ack`), in history and on the screen that
        shows it.

        Args:
            contact: The recipient.
            text: The message body.
            on_late_ack: Called with the stored message if its ack arrives after the send
                stopped its wait. The Courier uses it, because otherwise the Courier sends
                the message again.

        Returns:
            The stored outbound :class:`ChatMessage`. Its ``acked`` shows whether a
            delivery acknowledgement arrived in the wait, until now.
        """
        acked, codes = await self._deliver_direct(contact, text)
        chat = ChatMessage(
            text=text,
            outbound=True,
            is_channel=False,
            peer=contact.key_prefix or contact.public_key[:12] or None,
            peer_name=contact.name,
            acked=acked,
            created_at=utcnow(),
        )
        chat.row_id = self._ctx.repo.record_chat_message(chat, run_id=self._run_id)
        if not acked:
            self._await_ack(chat, codes, on_late_ack)
        return chat

    async def resend_direct(self, contact: Contact, message: ChatMessage) -> ChatMessage:
        """Try again to deliver an unacknowledged direct message, and change it in place.

        MeshTerm uses this method to try again a message that was transmitted but never
        acknowledged (its ``acked`` is ``False``). The method uses the same stored row
        again. It changes the delivery state of the row, and does not make a duplicate
        entry in the transcript. Thus the message changes to delivered (or stays
        unacknowledged for one more retry). As with a first send, each manual retry makes
        up to ``preferences.direct_message_soft_retries`` soft retries of its own (refer to
        :meth:`_deliver_direct`).

        Args:
            contact: The recipient.
            message: The :class:`ChatMessage` that was sent before, to transmit again. The
                method changes it in place with the new delivery state.

        Returns:
            The same ``message``, with :attr:`~ChatMessage.acked` updated.
        """
        acked, codes = await self._deliver_direct(contact, message.text)
        if message.acked:
            return message  # a late ack for an earlier attempt settled it during this send
        message.acked = acked
        if message.row_id is not None:
            self._ctx.repo.update_chat_ack(message.row_id, message.acked)
        if not acked:
            self._await_ack(message, codes, None)
        return message

    async def send_post(self, room: Contact, text: str) -> ChatMessage:
        """Post to a room, and store the post in the history of the room.

        A post travels the same way as a direct message. It is addressed to the room
        server, which acknowledges it after it stores it. MeshTerm stores it under the key
        of the room in the same way, so the history of the board is one conversation. The
        acknowledgement means that the room kept the post, never that a person read it.
        The room discards the post of a read-only member, with no acknowledgement at all.

        Unlike a direct message, a post never gets soft retries. A retry is a new message
        to the room (it carries a new timestamp). If the room stored a post and the
        acknowledgement was lost on its way back, a retry stores the post again, and the
        room sends it to each member twice. All the room sees that duplicate, but only one
        person sees the duplicate of a direct message. ^R stays the user's own retry.

        Args:
            room: The room server.
            text: The post.

        Returns:
            The stored outbound :class:`ChatMessage`. Its ``acked`` tells whether the room
            stored the post.
        """
        device = await self._ctx.device()
        delivery = await device.send_direct_message(room, text)
        chat = ChatMessage(
            text=text,
            outbound=True,
            is_channel=False,
            peer=room.key_prefix or room.public_key[:12] or None,
            peer_name=room.name,
            acked=delivery.acked,
            created_at=utcnow(),
        )
        chat.row_id = self._ctx.repo.record_chat_message(chat, run_id=self._run_id)
        if not delivery.acked:
            self._await_ack(chat, [delivery.code] if delivery.code else [], None)
        return chat

    async def resend_post(self, room: Contact, message: ChatMessage) -> ChatMessage:
        """Post an unacknowledged post again, one time, and change its row in place (^R).

        Args:
            room: The room server.
            message: The post that got no acknowledgement. The method changes it with the
                new result.

        Returns:
            The same ``message``, with :attr:`~ChatMessage.acked` updated.
        """
        device = await self._ctx.device()
        delivery = await device.send_direct_message(room, message.text)
        if message.acked:
            return message  # a late ack for the first post settled it during this send
        message.acked = delivery.acked
        if message.row_id is not None:
            self._ctx.repo.update_chat_ack(message.row_id, message.acked)
        if not delivery.acked:
            self._await_ack(message, [delivery.code] if delivery.code else [], None)
        return message

    # --- late acks ------------------------------------------------------------------------

    def _await_ack(self, chat: ChatMessage, codes: list[str], on_late_ack: LateAck | None) -> None:
        """Continue to listen for the ack of a sent message after the send stopped its wait.

        The device pushes an ack when it arrives, however late, for any of its last eight
        sends. The ack of a flood comes back on the path that the message found, and it
        often arrives after the wait. Thus MeshTerm files the message under each code with
        which it was sent. The oldest entries are removed after :data:`_AWAITING_CAP`.
        """
        for code in codes:
            self._awaiting[code.lower()] = (chat, on_late_ack)
            self._awaiting.move_to_end(code.lower())
        while len(self._awaiting) > _AWAITING_CAP:
            self._awaiting.popitem(last=False)

    def _on_ack(self, event: MeshEvent) -> None:
        """Hub callback: an ack arrived. Settle the message that it belongs to, if one waits."""
        ack = event.ack
        if ack is None or not ack.code:
            return
        self._settle_ack(str(ack.code).lower())

    def _settle_ack(self, code: str) -> None:
        """Mark the message that waits on ``code`` as delivered, wherever it is shown or kept.

        The method changes the history row. The same :class:`ChatMessage` object that the
        chat screen holds changes to acknowledged, so its ✗ changes to ✓ at the next paint.
        The listener that asked for this news (the Courier) gets it. The method releases
        each other code under which the message was sent (the code of an earlier attempt):
        the message was delivered once, and that is the news.
        """
        entry = self._awaiting.pop(code, None)
        if entry is None:
            return
        chat, on_late_ack = entry
        for other in [c for c, (held, _) in self._awaiting.items() if held is chat]:
            del self._awaiting[other]
        if chat.acked:
            return
        chat.acked = True
        if chat.row_id is not None:
            try:
                self._ctx.repo.update_chat_ack(chat.row_id, True)
            except Exception as exc:  # noqa: BLE001 - the screen still shows it as delivered
                self._ctx.log.debug("chat: failed to record a late ack: %s", exc)
        self._ctx.log.info("chat: late ack from %s", chat.peer_name or chat.peer)
        if on_late_ack is not None:
            try:
                on_late_ack(chat)
            except Exception as exc:  # noqa: BLE001 - one listener must not break the hub
                self._ctx.log.debug("chat: late-ack listener failed: %s", exc)

    def channel_scope(self, channel_id: str | None) -> str | None:
        """The scope of a channel's messages, or ``None`` for the default scope of the device.

        MeshTerm reads it from :class:`~meshterm.core.region_store.RegionStore` by the
        intrinsic identity of the channel, so the scope follows the channel from slot to
        slot. A context with no store (the context of a test, or a surface that never built
        a store) has no channel scopes at all.
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
        """Broadcast a message on a channel, under the scope of the channel, and store it.

        MeshTerm resolves the identity of the channel before the send, because the identity
        names the scope for the message (:meth:`channel_scope`). The send itself is
        :meth:`~meshterm.core.connection.Device.send_channel_in_scope`: set the session
        scope, send, and restore, and let no other flood go out between these steps. If the
        device cannot take a scope, the message stops. It does not go out in a different
        way.

        Args:
            index: The channel slot on which to transmit.
            text: The message body.
            label: A display label for the channel (for example ``#general``), stored for
                the transcript.
            scope: Overrides the scope of the channel for this one message: a region name,
                or :data:`~meshterm.core.regions.WILDCARD` (``*``) to send it unscoped.
                This is the resend for a scoped message that nothing relayed. ``None`` uses
                the scope of the channel, or the device default when the channel has none.

        Returns:
            The stored outbound :class:`ChatMessage`. Its :attr:`~ChatMessage.scope` is
            the scope under which it went out.

        Raises:
            ~meshterm.core.connection.FloodScopeError: If the device cannot send under the
                scope. Nothing was sent, and nothing is stored.
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
        """The scope that a channel send asked for, as the transcript stores it.

        When the send asked for a scope, the message went out under that scope, because
        the send refuses and does not use a different scope. A plain send went out under
        the default scope of the device. MeshTerm reads that scope (one time in a session,
        through the devstate cache): its name when one is set, or ``*`` when none is set,
        because a plain flood with no default is unscoped. If MeshTerm cannot read the
        default (firmware before 1.15 has none to read, or the link fails for a short
        time), it stores ``None``. Unknown is the honest answer, and the transcript never
        claims a scope that it did not see.
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
