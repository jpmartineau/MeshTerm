# SPDX-License-Identifier: Apache-2.0
"""The ``chat`` tool: channel messages and direct messages over the mesh.

In the menu, it opens a conversation picker, and then a live chat on the full screen
(refer to :mod:`meshterm.ui.chat`). There, the sent messages and the received messages
stream together. For a room server, it opens the board of the room instead
(:mod:`meshterm.ui.room`). On the CLI, it has the ``send``, ``history``, ``list``, and
``listen`` subcommands for scripts. You address a room with ``--to``, the same as a
contact. This tool only *lists* channels and rooms, so that you can select one. The
``channels`` tool configures the channel slots, and the ``rooms`` tool joins a room.

The :class:`~meshterm.services.chat_service.ChatService` always runs and stores each
received message in the history. Each sent message is stored when it is sent. Thus
``history`` shows the full transcript, whichever path made each message.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import typer
from rich.cells import cell_len
from rich.text import Text

from ..context import AppContext
from ..core import exitcodes
from ..core.channels import (
    CHANNEL_SLOT_PROBE_CAP,
    DEFAULT_PUBLIC_SECRET,
    MENTION,
    channel_hash,
    channel_identity,
    is_public_channel,
    split_channel_sender,
)
from ..core.connection import Device
from ..core.events import EventKind, MeshEvent
from ..core.models import (
    NODE_TYPE_CHAT,
    NODE_TYPE_LABELS,
    NODE_TYPE_ROOM,
    ChatMessage,
    Contact,
    Conversation,
    NodeResolver,
    is_direct_messageable,
    is_room,
)
from ..services.trace_runner import (
    NameKeyResolver,
    make_name_key_resolver,
    make_node_resolver,
)
from ..ui import renderers
from ..ui.fields import ChannelRef, NodeRef
from ..ui.menus import Lane, column_header, fit_cells, section_heading
from ..ui.theme import name_style
from ..ui.tui import Choice, DeleteRequest, Separator
from ..ui.widgets import NODE_GLYPHS, age_seconds, channel_glyph, format_age
from .base import Tool, ToolResult, register

if TYPE_CHECKING:
    from ..core.channel_probe import ChannelSlot
    from ..ui.report import Facts, Listing

#: The number of recent messages that ``chat history`` prints by default.
_HISTORY_LIMIT = 50


@register
class ChatTool(Tool):
    """Send and receive channel and direct messages, with a live interactive chat."""

    name = "chat"
    title = "Chat"
    icon = "💬"
    help = "Channel and direct messaging, with history"
    category = "Message"
    order = 10

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Nothing to collect here. The conversation picker is in :meth:`run`.

        The picker must *stay pushed* while a chat runs. Thus, when the user leaves a
        thread, the user goes back to the same list that opened it, with the same
        highlight and the same typed filter. A prompt here resolves, and pops, before the
        tool runs.

        Args:
            ctx: The shared application context.

        Returns:
            ``{"live": True}``: the marker of the menu for the interactive path.
        """
        return {"live": True}

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Open a live chat (menu), or do a scripted message action (CLI).

        Args:
            ctx: The shared application context.
            params: ``conversation`` (menu), or a ``cli_action`` with its arguments.

        Returns:
            A :class:`ToolResult` with a summary of what occurred.
        """
        action = params.get("cli_action")
        if action is not None:
            return await self._run_cli(ctx, action, params)
        return await self._run_live(ctx)

    # -- interactive picker -----------------------------------------------------

    async def _run_live(self, ctx: AppContext) -> ToolResult:
        """Keep the conversation picker pushed, and open chats above it until the user leaves.

        One screen stays for the full visit. Thus, when the user leaves a thread, the user
        goes back to the row that opened it, and the typed filter still narrows the list.
        Before, each round made the picker again from nothing, and a ``default=`` restore
        put the highlight back. That restored only the highlight, and only while its row
        was still there.

        The rows *are* data. An exchange moves its thread up in the order of recency. A
        delete of a history makes its dot hollow and moves the row down to the alphabetical
        end of the list. Thus, after each chat and each delete, the rows are read again and
        replaced in place. The highlight follows its thread to its new position.

        Args:
            ctx: The shared application context.

        Returns:
            A :class:`ToolResult` that counts the opened conversations and the messages that
            the last one showed.
        """
        from ..ui.chat import open_chat
        from ..ui.tui import SelectScreen
        from ..ui.tui.screen import CANCEL

        picker = SelectScreen(
            "Chat — pick a conversation",
            await self._picker_items(ctx),
            # Enter pushes the chat screen, so the committing verb is `open`. The base hint
            # does not contain the scroll atom or the erase atom. The select list adds each
            # one only when its key does something: ←→ only while the highlighted row is
            # too long, and Del only on a thread that has history to lose. The three
            # together must fit in 72 cells (the list of the archive preview has the same
            # calculation). That limit is the reason for `Del erase`. The row already names
            # the conversation, so the atom uses its cells for the verb. Also, `erase` is
            # the word that the app uses in other places when it removes data.
            footer_hint="↑↓ move · type to filter · Enter open · Esc back",
            delete_hint="Del erase",
        )
        opened = 0
        shown = 0
        async with ctx.ui.session.stay(picker) as visit:
            while True:
                choice = await visit.result()
                if isinstance(choice, DeleteRequest):
                    await self._delete_history(ctx, choice.value)
                elif choice is CANCEL or choice is None:  # Esc: back to the menu
                    return ToolResult(summary={"conversations": opened, "messages": shown})
                else:
                    shown = await open_chat(ctx, choice)
                    opened += 1
                picker.replace_items(await self._picker_items(ctx))

    async def _picker_items(self, ctx: AppContext) -> list:
        """Make the rows of the picker: the pinned lane names, then Channels, Rooms, and Direct.

        The rows are read again each time that the list is made or replaced. Thus a thread
        that got new messages is at the position that its recency gives it. It costs
        little to do this after each chat: the two device reads go through the session
        cache, and the rest is stored history.

        Args:
            ctx: The shared application context.

        Returns:
            The :class:`~meshterm.ui.tui.select.Choice` and ``Separator`` rows, in the
            order on the screen.
        """
        # Read through the session cache. This picker runs each time that Chat opens. Its
        # two reads (the channel slot probe and the contacts table) are the two slowest
        # round trips on a companion. When MeshTerm read them from the device each time,
        # Chat stopped for seconds when it opened (the cached chat screen behind it had no
        # chance to help). The cache keeps the channels until the channel editor writes a
        # slot, and reads the contacts again in the background (refer to
        # :class:`~meshterm.services.device_state.DeviceState`).
        channels = _channels_from_slots(await ctx.devstate.channel_slots())
        contacts = await ctx.devstate.contacts()
        # Direct lists only companion nodes. We do not send direct messages to repeaters,
        # rooms, or sensors. A contact that never advertised its type is accepted (the
        # app-wide DM rule, refer to is_direct_messageable). Room servers have their
        # own section: a room is a place that you join and post to, not a person to whom
        # you send a message.
        companions = [c for c in contacts if is_direct_messageable(c.node_type)]
        # Only the rooms that we joined, in the same way as only the channels on a slot are
        # listed. The Rooms page joins rooms (refer to meshterm.ui.rooms).
        rooms = [
            Conversation(label=c.name, is_channel=False, contact=c)
            for c in contacts
            if is_room(c.node_type) and ctx.rooms.joined(c)
        ]
        room_peers = [conv.peer for conv in rooms if conv.peer]
        # A stable snapshot sets the order of the rows (so that the list does not change its
        # order under the highlight). A lookup that reads itself again gives the live preview
        # of each row (refer to _LiveLasts). The latest message of a room is its latest
        # *post*.
        lasts = ctx.repo.last_chat_messages(rooms=room_peers)
        live = _LiveLasts(ctx, seed=lasts, rooms=room_peers)
        # The names in previews and mentions resolve back to keys for their hue (the
        # app-wide colour rule). A name that no contact and no stored advert has stays
        # muted. A room post names its author by key, which resolves in the other
        # direction.
        stored_names = ctx.repo.node_names()
        key_of = make_name_key_resolver(contacts, stored_names)
        name_of = make_node_resolver(contacts, stored_names)

        # List the contacts by recency: first the contacts with messages, with the newest
        # exchange at the top. Then the contacts with no messages, in alphabetical order
        # (refer to _recency_key). The same for rooms.
        direct = [Conversation(label=c.name, is_channel=False, contact=c) for c in companions]
        direct.sort(key=lambda conv: _recency_key(conv, lasts))
        rooms.sort(key=lambda conv: _recency_key(conv, lasts))
        # One measurement for the full list, the headings and the rows, done before they are
        # made. The name lane is only as wide as the names in it, and each cell that it does
        # not use goes to the message preview (refer to _lanes).
        lanes = _lanes(channels + rooms + direct)

        # The lane names pin for the full picker (they have the same meaning in the two
        # groups). Thus, when the list scrolls into Direct, they stay at the top, with the
        # heading of that group under them. Otherwise the header goes away after one row.
        # Refer to Screen.sticky_rows.
        items: list = [
            Separator(lambda w: _picker_header(lanes, w), pinned=True),
            section_heading("📡 Channels"),
        ]
        for conversation in channels:
            items.append(
                Choice(
                    title=_row_title(ctx, conversation, live, key_of, lanes),
                    value=conversation,
                    # ←→ move only the message. The lanes before it stay (_Lanes.head).
                    hscroll_from=lanes.head,
                )
            )

        items.append(section_heading("📌 Rooms"))
        for conversation in rooms:
            items.append(
                Choice(
                    title=_row_title(ctx, conversation, live, key_of, lanes, name_of=name_of),
                    value=conversation,
                    deletable=lasts.get(conversation.key) is not None,
                    hscroll_from=lanes.head,
                )
            )
        if not rooms:
            # Show this, do not omit it. Users come here with the question "how do I get on
            # a room?", and an empty section tells them where to go.
            items.append(Separator("  no rooms joined — join one on the Rooms page"))

        items.append(section_heading("👤 Direct"))
        if companions:
            for conversation in direct:
                items.append(
                    Choice(
                        title=_row_title(ctx, conversation, live, key_of, lanes),
                        value=conversation,
                        # Del offers to delete the stored history of this thread, only
                        # where there is history to delete.
                        deletable=lasts.get(conversation.key) is not None,
                        hscroll_from=lanes.head,
                    )
                )
        else:
            items.append(Separator("  no contacts yet — receive an advert first"))

        return items

    @staticmethod
    async def _delete_history(ctx: AppContext, conversation: Conversation) -> None:
        """Confirm, then delete the stored history of one direct conversation or one room.

        A delete of history is a data loss that cannot be undone. Thus the confirm has the
        reserved red (``destructive``): Cancel on the left, and the committing Delete on
        the right. When the user confirms, MeshTerm deletes the messages of the peer from
        the database and clears the unread count of the thread. The contact itself (a
        record on the device) does not change. The posts of a room go only from this
        machine. The room keeps its own posts, and does not send them again, because it
        got the acknowledgements for them.
        """
        prompt = (
            f"Delete the posts stored from {conversation.label}? They are removed from "
            "this machine, and the room won't send them again."
            if conversation.is_room
            else f"Delete the chat history with {conversation.label}? Every stored "
            "message in this conversation is removed."
        )
        if not await ctx.ui.dialog(
            prompt,
            [("Cancel", False), ("Delete", True)],
            title="Delete history",
            default=1,
            destructive=True,
        ):
            return
        ctx.repo.delete_chat_history(conversation.peer)
        ctx.chat.clear_unread(conversation.key)

    # -- CLI --------------------------------------------------------------------

    async def _run_cli(self, ctx: AppContext, action: str, params: dict[str, Any]) -> ToolResult:
        """Dispatch a scripted CLI action.

        Args:
            ctx: The shared application context.
            action: One of ``send``, ``history``, ``listen``, ``list``.
            params: The arguments of the action.

        Returns:
            A :class:`ToolResult` for the action.
        """
        if action == "send":
            return await self._cli_send(ctx, params)
        if action == "history":
            return await self._cli_history(ctx, params)
        if action == "listen":
            return await self._cli_listen(ctx, params)
        return await self._cli_list(ctx)

    async def _cli_send(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Send a channel message or a direct message, and report whether it was acknowledged.

        A channel broadcast has nothing to report, because a channel has no
        acknowledgement. Thus it prints nothing, and the exit status says that the message
        went out. A direct message prints its ``acked`` state. That is the one thing that
        the send does not already tell the caller: the radio accepted the message in the
        two cases, and the answer of the peer is a separate fact.

        A channel message goes out under the send scope of the channel, or under ``scope``
        for this one message (a region name, or ``*`` for unscoped). The document says
        which scope it went out under. If the radio cannot send under the scope, it
        refuses before it transmits anything. That is a failure (exit 1), never a silent
        unscoped send.

        ``--to`` a room server posts to the room. The room acknowledges a post after it
        stores it, so ``acked`` has the same meaning. A room that no longer knows us (it
        restarted, or we never joined) discards the post with no acknowledgement. Use
        ``rooms join`` first.
        """
        device = await ctx.device()
        text = str(params["text"])
        channel = params.get("channel")
        if channel is not None:
            message = await ctx.chat.send_channel(
                int(channel), text, label=f"#{channel}", scope=params.get("scope") or None
            )
            return ToolResult(
                summary={"channel": channel, "sent": True, "scope": message.scope},
                report=(_sent(channel=int(channel), scope=message.scope),),
            )

        contact = _resolve_contact(await device.get_contacts(), str(params["to"]))
        if contact.is_room:
            message = await ctx.chat.send_post(contact, text)
        else:
            message = await ctx.chat.send_direct(contact, text)
        return ToolResult(
            summary={"to": contact.name, "acked": bool(message.acked)},
            report=(_sent(contact=contact, acked=bool(message.acked)),),
        )

    async def _cli_history(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Print the stored transcript of a conversation. For a room, print its board by author.

        The transcript of a room is its posts. Its command replies are not in it, the same
        as on its board in the menu. Where a direct transcript names the peer, the
        transcript of a room names the author of each post. The reason: the room is the
        conversation that the caller already named, and the posts in it have different
        writers. Our own posts name our node, as each of our hops does on this face.
        """
        limit = int(params.get("limit") or _HISTORY_LIMIT)
        channel = params.get("channel")
        to = params.get("to")
        room = False
        name_of: NodeResolver | None = None
        me: NodeRef | None = None
        if channel is not None:
            channel_id = await ctx.chat.channel_id_for(int(channel))
            messages = ctx.repo.recent_chat_messages(
                is_channel=True, channel_id=channel_id, limit=limit
            )
            label = f"#{channel}"
        else:
            device = await ctx.device()
            contacts = await device.get_contacts()
            contact = _resolve_contact(contacts, str(to))
            room = contact.is_room
            messages = ctx.repo.recent_chat_messages(
                is_channel=False,
                peer=contact.key_prefix or contact.public_key[:12],
                limit=limit,
                posts_only=room,
            )
            label = contact.name
            if room:
                name_of = make_node_resolver(contacts, ctx.repo.node_names())
                me = _self_ref(await device.get_self_info())

        return ToolResult(
            summary={"conversation": label, "messages": len(messages)},
            report=(_transcript(messages, room=room, name_of=name_of, me=me),),
            exit_code=exitcodes.OK if messages else exitcodes.NO_RESULT,
        )

    async def _cli_listen(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Show the received messages live in the console (and store them in the history).

        A receiver on the plain console, outside the full-screen menu. It starts the
        listener that always runs, and prints each message when it arrives. It is also a
        diagnostic: with the debug logging of the pump on (``--debug`` on the CLI), it
        shows whether MeshTerm gets messages from the companion.

        Args:
            ctx: The shared application context.
            params: ``seconds``: how long to listen (``0`` or ``None`` means until an
                interrupt).

        Returns:
            A :class:`ToolResult` with the number of heard messages.
        """
        from ..ui import script

        seconds = params.get("seconds") or 0
        await ctx.device()
        await ctx.chat.start()  # also starts to store the received messages in the history
        # This line is about the run, and is not part of its answer. Thus it goes to stderr,
        # so that a redirected stdout holds only messages.
        script.stderr_console().print(
            "listening for messages"
            + (f" for {seconds}s" if seconds else " — press Ctrl-C to stop"),
            style="muted",
            highlight=False,
        )
        seen = 0

        def on_message(event: MeshEvent) -> None:
            nonlocal seen
            message = event.message
            if message is None:
                return
            seen += 1
            channel = (
                _channel_ref(message.channel, f"#{message.channel}", None)
                if message.is_channel
                else None
            )
            emit(
                {
                    "received_at": message.received_at,
                    "conversation": channel.name if channel else (message.sender or None),
                    "direction": "in",
                    "node": None if message.is_channel else NodeRef(hash=message.sender),
                    "channel": channel,
                    "author": NodeRef(hash=message.author) if message.author else None,
                    "snr_db": message.snr,
                    "text": message.text,
                    "acked": None,
                }
            )

        with renderers.stream(ctx, _live_messages()) as emit:
            unsubscribe = ctx.events.subscribe(on_message, EventKind.MESSAGE)
            try:
                if seconds:
                    await asyncio.sleep(seconds)
                else:
                    await asyncio.Event().wait()  # until Ctrl-C or a cancellation
            except (KeyboardInterrupt, asyncio.CancelledError):  # pragma: no cover - interactive
                pass
            finally:
                unsubscribe()
        return ToolResult(
            summary={"messages": seen},
            message=f"[muted]stopped — heard {seen} message{'' if seen == 1 else 's'}[/muted]",
            exit_code=exitcodes.OK if seen else exitcodes.NO_RESULT,
        )

    async def _cli_list(self, ctx: AppContext) -> ToolResult:
        """List the channels, the contacts, and the most recent message of each."""
        device = await ctx.device()
        channels = await _read_channels(device)
        contacts = await device.get_contacts()
        rooms = [(c.key_prefix or c.public_key[:12]).lower() for c in contacts if c.is_room]
        lasts = ctx.repo.last_chat_messages(rooms=rooms)

        rows = [*channels] + [
            Conversation(label=c.name, is_channel=False, contact=c) for c in contacts
        ]
        return ToolResult(
            summary={"conversations": len(rows)},
            report=(_conversations(ctx, rows, lasts),),
            exit_code=exitcodes.OK if rows else exitcodes.NO_RESULT,
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``chat`` subcommand group.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        chat_app = typer.Typer(help=self.help, no_args_is_help=True, rich_markup_mode=None)

        @chat_app.command("send", help="Send a message to a contact or channel, or post to a room")
        def _send_cmd(
            text: str = typer.Argument(..., help="The message body"),
            to: str | None = typer.Option(None, "--to", help="Contact or room name, or key prefix"),
            channel: int | None = typer.Option(None, "--channel", help="Channel slot index"),
            scope: str | None = typer.Option(
                None,
                "--scope",
                help="Send under this region instead of the channel's scope (* = unscoped)",
            ),
        ) -> None:
            if (to is None) == (channel is None):
                raise typer.BadParameter("Pass exactly one of --to / --channel.")
            if scope is not None:
                scope = _cli_scope(scope, channel)
            run_tool_command(
                self,
                {"cli_action": "send", "to": to, "channel": channel, "text": text, "scope": scope},
            )

        @chat_app.command("history", help="Show a conversation's stored history")
        def _history_cmd(
            to: str | None = typer.Option(None, "--to", help="Contact or room name, or key prefix"),
            channel: int | None = typer.Option(None, "--channel", help="Channel slot index"),
            limit: int = typer.Option(_HISTORY_LIMIT, "--limit", help="Max messages to show"),
        ) -> None:
            if (to is None) == (channel is None):
                raise typer.BadParameter("Pass exactly one of --to / --channel.")
            run_tool_command(
                self,
                {"cli_action": "history", "to": to, "channel": channel, "limit": limit},
            )

        @chat_app.command("list", help="List channels, rooms, contacts, and recent messages")
        def _list_cmd() -> None:
            run_tool_command(self, {"cli_action": "list"})

        app.add_typer(chat_app, name=self.name)


# -- helpers ------------------------------------------------------------------


def _cli_scope(scope: str, channel: int | None) -> str:
    """Check ``--scope`` before anything is sent: a region that the firmware can hold, or ``*``.

    A bad value is a usage error (exit 2), not a device failure, because nothing was
    transmitted yet. Here, a direct message has no scope to get. The firmware floods a
    DM under the session scope, the same as all other packets, but only a channel send
    sets a scope for itself.

    Raises:
        typer.BadParameter: For a direct send, or for a name that the firmware refuses.
    """
    from ..core.regions import WILDCARD, RegionNameError, normalize, validate

    if channel is None:
        raise typer.BadParameter("--scope applies to a channel send (--channel).")
    if normalize(scope) == WILDCARD:
        return WILDCARD
    try:
        return validate(scope)
    except RegionNameError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _enable_receive_debug() -> None:
    """Send the debug logs of the message pull and of the library to the console.

    This function sets the ``meshcore`` logger and the logger of the connection layer to
    DEBUG, and attaches a stderr handler. Thus ``chat listen --debug`` shows whether
    MeshTerm pulls received messages from the companion (the receive path) at all.
    """
    import logging
    import sys

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    for name in ("meshcore", "meshterm.core.connection"):
        log = logging.getLogger(name)
        log.setLevel(logging.DEBUG)
        log.addHandler(handler)


def _channels_from_slots(slots: list[ChannelSlot]) -> list[Conversation]:
    """Change cached channel slots into channel conversations, and always offer Public (slot 0).

    The picker reads its channels through the session cache
    (:meth:`~meshterm.services.device_state.DeviceState.channel_slots`). Thus it uses the one
    slow slot probe again, and does not read each slot again each time that Chat opens.
    This function maps that cached :class:`~meshterm.core.channel_probe.ChannelSlot` list
    onto the :class:`~meshterm.core.models.Conversation` rows of the picker. When the
    firmware reports no slot for channel 0 (the default public channel), this function
    makes one. Thus there is always a place to chat. :func:`_read_channels` (the CLI path)
    gives the same guarantee.

    Args:
        slots: The configured channel slots, from the session cache.

    Returns:
        One :class:`~meshterm.core.models.Conversation` for each slot, with Public at the
        start when slot 0 is absent.
    """
    conversations = [
        Conversation(
            label=slot.name,
            is_channel=True,
            channel_idx=slot.idx,
            channel_id=channel_identity(slot.name, slot.secret),
            secret=slot.secret,
        )
        for slot in slots
    ]
    if not any(c.channel_idx == 0 for c in conversations):
        conversations.insert(
            0,
            Conversation(
                label="Public",
                is_channel=True,
                channel_idx=0,
                channel_id="slot:0",
                secret=DEFAULT_PUBLIC_SECRET,
            ),
        )
    return conversations


async def _read_channels(device: Device) -> list[Conversation]:
    """Probe the channel slots, and return them as channel conversations.

    This function always offers channel 0 (the default public channel), also when the
    firmware reports no configured slots. Thus there is always a place to chat.

    Args:
        device: The connected device to probe.

    Returns:
        One :class:`~meshterm.core.models.Conversation` for each configured channel (at
        least channel 0).
    """
    conversations: list[Conversation] = []
    for idx in range(CHANNEL_SLOT_PROBE_CAP):
        try:
            channel = await device.get_channel(idx)
        except Exception:  # noqa: BLE001 - the firmware may not support channel reads
            break
        if channel:
            name = str(channel.get("channel_name") or idx)
            secret = bytes(channel.get("channel_secret") or b"\x00" * 16)
            conversations.append(
                Conversation(
                    label=name,
                    is_channel=True,
                    channel_idx=idx,
                    channel_id=channel_identity(name, secret),
                    secret=secret,
                )
            )
    if not any(c.channel_idx == 0 for c in conversations):
        conversations.insert(
            0,
            Conversation(
                label="Public",
                is_channel=True,
                channel_idx=0,
                channel_id="slot:0",
                secret=DEFAULT_PUBLIC_SECRET,
            ),
        )
    return conversations


def _resolve_contact(contacts: list[Contact], needle: str) -> Contact:
    """Find a contact by its exact name (case-insensitive), or by a key prefix that matches.

    Args:
        contacts: The known contacts.
        needle: A contact name, or a prefix of a public key.

    Returns:
        The matching contact.

    Raises:
        typer.BadParameter: If no contact matches ``needle``.
    """
    folded = needle.casefold()
    for contact in contacts:
        pub = (contact.public_key or "").lower().removeprefix("0x")
        if contact.name.casefold() == folded or pub.startswith(folded):
            return contact
    raise typer.BadParameter(f"no contact matches {needle!r}")


def _recency_key(conversation: Conversation, lasts: dict) -> tuple:
    """A sort key that puts the conversations in order of recency, then of name.

    The conversations that have messages come first, with the most recent exchange at the
    top. The conversations with no messages come after them, in alphabetical order of
    label. The ``0`` or ``1`` at the start keeps the two groups apart. Thus their
    tie-breakers, which have different types, are never compared.

    Args:
        conversation: The conversation to rank.
        lasts: A map from the conversation key to its most recent message.

    Returns:
        A tuple that ``sorted`` can use as a key.
    """
    last: ChatMessage | None = lasts.get(conversation.key)
    if last is not None:
        return (0, -last.created_at.timestamp())
    return (1, conversation.label.casefold())


class _LiveLasts:
    """A lookup of the most recent message of each conversation, which reads itself again.

    The picker takes one snapshot of
    :meth:`~meshterm.persistence.repository.Repository.last_chat_messages` to set the *order*
    of the rows (so that the list never changes its order under the highlight). But the row
    previews read through this lookup. Thus, when a message arrives while the list is open,
    the next paint shows the new sender and text in the preview. The lookup queries the
    repository again a few times each second at most (``ttl`` sets the limit), not one time
    for each row on each paint. Thus a wide list stays fast.
    """

    def __init__(
        self, ctx: AppContext, *, seed: dict, ttl: float = 0.5, rooms: list[str] | None = None
    ) -> None:
        """Bind to a context, and start the cache with the snapshot that was read at open.

        ``rooms`` are the peers of the room servers in the conversations. Thus a new read
        gets the latest *post* of a room, the same as the first snapshot did (refer to
        ``Repository.last_chat_messages``).
        """
        self._ctx = ctx
        self._ttl = ttl
        self._cache = seed
        self._rooms = list(rooms or ())
        self._at = time.monotonic()

    def get(self, key: str) -> ChatMessage | None:
        """Return the latest message for ``key``. Read the cache again when its TTL ends."""
        now = time.monotonic()
        if now - self._at >= self._ttl:
            try:
                self._cache = self._ctx.repo.last_chat_messages(rooms=self._rooms)
            except Exception:  # noqa: BLE001 - if a read error occurs, keep the last snapshot
                pass
            self._at = now
        return self._cache.get(key)


#: The width of the unread badge lane, between the label and the age (``● 999`` fits).
_BADGE_WIDTH = 5
#: The width of the relative age lane, between the badge and the preview. It is right-aligned.
#: ``now`` and two-digit spans such as ``59m`` and ``23h`` fit. Thus the message text of each
#: row starts in the same column.
_AGE_WIDTH = 3
#: The minimum width of the conversation lane. The header word is ``CONVERSATION`` (12 cells)
#: and the lane has a gap of two cells after it. If the lane is narrower, the heading touches
#: ``NEW`` on a mesh where each name is ``Bob``.
_LABEL_MIN = 12
#: The maximum width of the lane. The lane width is the length of the longest name in it
#: (refer to :func:`_lanes`), and at this width that rule is no longer useful. Without this
#: limit, one very long name puts padding before each *short* name. This width holds the
#: long but usual shape of a repeater name (``YUL-Cartierville``). A longer name ends with
#: an ellipsis. The row only opens the conversation, and the title of its screen has the
#: full name. Each cell that this limit saves goes to the preview.
_LABEL_MAX = 16


@dataclass(frozen=True)
class _Lanes:
    """The fixed lane widths of the picker, measured one time each time the list is made.

    The column header and each row get their layout from one of these objects. Thus the
    headings cannot move away from the columns that they name. Only the first two widths
    change. The marker has the glyph width of the platform (a channel glyph is two cells
    on regular, and one cell on the PicoCalc console). The label lane has the width of the
    longest name in the list, between :data:`_LABEL_MIN` and :data:`_LABEL_MAX`.

    Attributes:
        marker: The first glyph lane: a channel glyph or a contact dot, and its space.
        label: The number of cells to which the conversation name is padded or cut with an
            ellipsis.
    """

    marker: int
    label: int

    @property
    def head(self) -> int:
        """The cells before the preview: all that ←→ do not move (:attr:`Choice.hscroll_from`).

        The marker, the name, the unread badge, and the age, each with the gap of two cells
        after it. These columns say *which conversation this row is*. The preview is the
        only part that moves, and that is the purpose. The lanes show the user where the
        user is in the list, and they already fit.
        """
        return self.marker + self.label + 2 + _BADGE_WIDTH + 2 + _AGE_WIDTH + 2


def _lanes(conversations: list[Conversation]) -> _Lanes:
    """Measure the lanes of the picker for the conversations that it will draw.

    Before, the name lane was always 22 cells. That is wide on a terminal of 72 cells, and
    very wide on the 53 cells of the PicoCalc. Each cell after the longest name in the list
    is padding that the preview of the last message loses. When the width comes from the
    content, a mesh of ``Alice`` and ``Bob`` keeps its names in the lane of the header word,
    and gives all the other cells to the messages.

    Args:
        conversations: All the rows that the list will hold, channels and direct.

    Returns:
        The lane widths for the header and for the rows.
    """
    longest = max((cell_len(c.label) for c in conversations), default=0)
    return _Lanes(
        # The marker is "glyph + space", and the glyph comes from the platform: two cells on
        # regular, one cell on the console font. The width is read from the widget, not
        # assumed. Thus the lanes of the two groups align on the two platforms: the dot of
        # a contact is padded to the width of a channel glyph here (refer to
        # _append_marker).
        marker=cell_len(channel_glyph("Public", None)) + 1,
        label=max(_LABEL_MIN, min(_LABEL_MAX, longest)),
    )


def _picker_header(lanes: _Lanes, width: int) -> str:
    """Column headers above the fixed lanes of the picker (the layout is in :func:`_title`).

    The indent covers the pointer column of the select screen (2 cells, drawn on choice
    rows but not on separators) and the marker lane. Thus each header is exactly above its
    column. ``NEW`` counts the unread messages, ``LAST`` is the age of the last message,
    and ``MESSAGE`` is that message. The words are short, because the lanes under the
    first two are a badge and an age, and a heading that is longer than its lane touches
    the next heading. ``LAST`` also uses the gap after its lane (the age lane is three
    cells), and that leaves a space before the preview.

    The header is resolved for the render width (the header row is pinned, so it must
    stay one row). On a terminal that is too narrow for the full line, ``MESSAGE`` becomes
    ``MSG`` and gives back its cells. Thus the line does not wrap and does not lose the
    label (refer to :func:`~meshterm.ui.menus.column_header`).
    """
    return column_header(
        [
            Lane("CONVERSATION", lanes.label + 2),
            Lane("NEW", _BADGE_WIDTH + 2),
            Lane("LAST", _AGE_WIDTH + 2),
            Lane(("MESSAGE", "MSG")),
        ],
        width,
        indent=2 + lanes.marker,
    )


def _row_title(
    ctx: AppContext,
    conversation: Conversation,
    lasts: _LiveLasts,
    key_of: NameKeyResolver,
    lanes: _Lanes,
    *,
    name_of: NodeResolver | None = None,
) -> Callable[[], str | Text]:
    """Return the row title as a *callable* that the select screen renders on each paint.

    The unread badge and the preview of the last message are both read live. Thus, when a
    message arrives while the picker is open, the next paint changes the ``●`` count of
    that row *and* its preview of the sender and text (the session already paints
    approximately one time each second, for the header).

    Args:
        ctx: The shared application context (for the live unread count).
        conversation: The conversation of the row.
        lasts: The lookup of the latest messages, which reads itself again and gives the
            preview.
        key_of: Maps a sender name back to the key of its node, for the hues of the preview.
        lanes: The measured lane widths of the list.
        name_of: Maps the author key of a room post to a name (only for rooms).

    Returns:
        A callable with no arguments, which makes the current row title.
    """
    return lambda: _title(ctx, conversation, lasts, key_of, lanes, name_of=name_of)


def _title(
    ctx: AppContext,
    conversation: Conversation,
    lasts: _LiveLasts,
    key_of: NameKeyResolver,
    lanes: _Lanes,
    *,
    name_of: NodeResolver | None = None,
) -> str | Text:
    """Make a picker row from lanes of fixed width, with colour codes.

    The alignment makes the row easy to read. The marker, the label, the unread badge, the
    relative age, and the preview each have their own lane. Thus the message text of each
    row starts in the same column. Each colour has a purpose:

    - The name of a direct contact gets the palette hue that comes from its key (a channel
      label keeps the base style).
    - The first dot is the standard companion pink, and its shape shows the history.
    - The unread ``●`` badge is red, and the age is muted.
    - The preview mutes its body, and lights the sender names and the ``@mentions`` in the
      hues that come from their keys. These are the same colours as in the live transcript.

    The row is always a Rich :class:`~rich.text.Text`, so that those spans stay under the
    row highlight of the select screen.

    Args:
        ctx: The shared application context (for the live unread count).
        conversation: The conversation of the row.
        lasts: The lookup of the latest messages, which reads itself again.
        key_of: Maps a sender name or a mention name in the preview back to the key of its
            node.
        lanes: The measured lane widths of the list.
        name_of: Maps the author key of a room post to a name (only for rooms).

    Returns:
        The row title as a styled :class:`~rich.text.Text`.
    """
    unread = ctx.chat.unread(conversation.key)
    last = lasts.get(conversation.key)
    text = Text(no_wrap=True, overflow="ellipsis")
    _append_marker(text, conversation, last, lanes)
    label_style = ""
    if not conversation.is_channel and conversation.contact is not None:
        contact = conversation.contact
        label_style = name_style(conversation.label, contact.public_key or contact.key_prefix)
    text.append(fit_cells(conversation.label, lanes.label), style=label_style or None)
    text.append("  ")
    # The unread badge lane (_BADGE_WIDTH cells): a red ● with the count in warn, or blank
    # spaces. Thus the lanes after it still align on rows with nothing unread.
    if unread:
        text.append("●", style="err")
        text.append(f" {unread}".ljust(_BADGE_WIDTH - 1), style="warn")
    else:
        text.append(" " * _BADGE_WIDTH)
    # The relative age lane (right-aligned) is between the badge and the message text. Thus
    # the ages are in one neat column, and all the previews start at the same position.
    age = _ago(last.created_at) if last is not None else ""
    text.append("  ")
    text.append(f"{age:>{_AGE_WIDTH}}", style="muted")
    text.append("  ")
    if last is not None:
        text.append_text(_preview_text(last, key_of, name_of))
    return text


#: The standard companion pink (the shared colour of the plain node ``●``) is the hue of the
#: contact dot. The shape (filled or hollow) shows the history, the colour shows "a
#: companion", and the name next to it has the hue that comes from the key of the person.
_COMPANION_DOT_STYLE = NODE_GLYPHS[NODE_TYPE_CHAT][1]

#: The marker of a room: the ``■`` of the room server, in the colour of its type. The map
#: and the contact list give a room the same mark. The console font has no hollow square,
#: so the marker shows no history state. The preview lane says whether there is something
#: to read, or a room to join.
_ROOM_MARK = NODE_GLYPHS[NODE_TYPE_ROOM]


def _append_marker(
    text: Text, conversation: Conversation, last: ChatMessage | None, lanes: _Lanes
) -> None:
    """Add the first marker of the row (a channel glyph or a contact dot) in its own lane.

    A channel keeps its marker for open or private (＃ / 🌐 / 🔒). A contact gets a small
    circle in the standard companion pink. It is filled (``●``) after we exchanged
    messages, and a hollow ring (``○``) before that. Thus the shape (hollow or filled)
    shows whether there is history, and the name itself has the hue that comes from the
    key of the person.

    The dot is padded to :attr:`_Lanes.marker`, which is measured from the channel glyph
    itself, not assumed. That glyph is two cells on regular and one cell on the PicoCalc
    console font. A fixed pad aligned the two sections on one platform. On the other
    platform, it put each channel row one cell to the left of each direct row.
    """
    if conversation.is_channel:
        glyph = channel_glyph(conversation.label, conversation.secret)
        text.append(glyph + " " * max(1, lanes.marker - cell_len(glyph)))
    elif conversation.is_room:
        mark, style = _ROOM_MARK
        text.append(mark, style=style)
        text.append(" " * max(1, lanes.marker - cell_len(mark)))
    else:
        dot = "●" if last is not None else "○"
        text.append(dot, style=_COMPANION_DOT_STYLE)
        text.append(" " * max(1, lanes.marker - cell_len(dot)))


def _preview_text(
    last: ChatMessage, key_of: NameKeyResolver, name_of: NodeResolver | None = None
) -> Text:
    """A muted preview of the last message, with sender names and ``@mentions`` lit in their hue.

    It is the same as the live transcript:

    - Our own messages get a ``you:`` prefix.
    - In a received channel message, the inline ``Name:`` sender gets the hue that comes from
      its key (muted when no known node has the name).
    - A room post starts with its author, named by key in the same way as the room screen
      names authors (:func:`~meshterm.ui.room.author_label`).
    - Each ``@[Name]`` mention shows as a bare ``@Name``, in the same way.

    Thus the list and the chat use the same colour language.

    The preview is made *in full*, however long the message is. Before, it was cut to a
    fixed 40 cells, and nothing could undo that cut. Now ←→ scroll the row. A preview that
    was cut before to approximately the visible lane has nothing more to show. The list
    cuts it at the right edge, the same as any other row, and the scroll goes past that.
    """
    body_raw = last.text.replace("\n", " ")
    text = Text()
    if last.outbound:
        text.append("you: ", style="accent")
        _append_body(text, body_raw, key_of)
    elif last.is_channel:
        name, body = split_channel_sender(body_raw)
        if name is not None:
            text.append(name, style=name_style(name, key_of(name)))
            text.append(": ", style="muted")
            _append_body(text, body, key_of)
        else:
            _append_body(text, body_raw, key_of)
    elif last.author and name_of is not None:
        from ..ui.room import author_label

        name, key = author_label(last.author, name_of)
        text.append(name, style=name_style(name, key))
        text.append(": ", style="muted")
        _append_body(text, body_raw, key_of)
    else:
        _append_body(text, body_raw, key_of)
    return text


def _append_body(text: Text, body: str, key_of: NameKeyResolver) -> None:
    """Append ``body`` to ``text``, muted, and light the names that it mentions.

    Each ``@[Name]`` is drawn in the hue that comes from the key of that node. It stays
    muted when the name resolves to no node that we know.
    """
    pos = 0
    for match in MENTION.finditer(body):
        if match.start() > pos:
            text.append(body[pos : match.start()], style="muted")
        name = match.group(1)
        text.append(f"@{name}", style=name_style(name, key_of(name)))
        pos = match.end()
    if pos < len(body):
        text.append(body[pos:], style="muted")


def _ago(when: Any) -> str:
    """The column age for a message time, through the only grammar (``format_age``).

    There is one difference from the widget, on purpose. A missing or naive timestamp (a
    bad value from the wire) shows as a *blank* lane, not as ``never``. The picker must
    have an empty cell there, not a word.
    """
    if getattr(when, "tzinfo", None) is None:
        return ""
    return format_age(age_seconds(when))


def _channel_ref(idx: int | None, label: str, secret: bytes | None) -> ChannelRef:
    """One channel in the shared shape, from what the surface knows about it.

    A transcript row and a live message know a slot and a label, but not the key. Thus,
    when the secret is not available, ``public`` comes from the name. A ``#`` at the start
    of the name means a public channel, and the app writes a public channel that way in
    all other places.
    """
    return ChannelRef(
        slot=int(idx or 0),
        name=label,
        public=is_public_channel(label, secret) if secret else label.startswith("#"),
        hash=channel_hash(secret) if secret else None,
    )


def _message_columns(*, author_lane: bool = False) -> tuple:
    """The columns of one transcript row, shared by ``history`` and the ``listen`` stream.

    ``DIR`` holds what the transcript of the menu writes into its sender lane as the word
    "you". The direction of a message is a separate fact. When the name lane held it, our
    own name became a value that the lane could not hold in other cases. Thus ``PEER`` is
    always the *other* party.

    ``TEXT`` is last and escaped. It is the rest of the line, and "the rest of the line"
    is not true when a body holds a newline. After the newline, the rest looks like a
    second record with an empty time. The document holds the raw body, because a JSON
    string can hold a newline.

    ``author`` is the writer of a room post. It is in each document (``null`` when the
    conversation is not a room), so that one command answers in one shape. For the
    transcript of a room, ``author_lane`` also draws it as ``AUTHOR``, because there it is
    the column that the user wants to see.
    """
    from dataclasses import replace

    from ..ui import fields

    peer = fields.plain_only(fields.name("conversation", "PEER"))
    return (
        fields.when("created_at", "TIME"),
        fields.word("direction", "DIR"),
        # One column for the other end of the conversation, whichever type it is. The
        # document keeps the two types apart, because a parser cannot tell a channel from a
        # contact by its label. The transcript of a room names the room on each row, so
        # there AUTHOR is in its place.
        replace(peer, lanes=()) if author_lane else peer,
        fields.node("node", lanes=()),
        fields.channel("channel", lanes=()),
        _author_column(lane=author_lane),
        fields.snr("snr_db", "SNR_DB"),
        fields.free("text", "TEXT"),
        fields.hidden("acked"),
    )


def _author_column(*, lane: bool) -> Any:
    """The author of a room post: the shared node object, drawn as one hop (``Name (hash)``).

    It uses the grammar of a hop (:func:`~meshterm.ui.script.route`), because that is the
    one way in which the plain face names a node by name *and* key. When nothing names an
    author, the author is only its hash, never an empty ``()``.
    """
    from dataclasses import replace

    from ..ui import fields, script
    from ..ui.report import Lane

    column = fields.node("author", lanes=())
    if not lane:
        return column

    def hop(ref: NodeRef | None) -> str:
        return script.route([(ref.name, ref.hash)]) if ref is not None else script.NONE

    return replace(column, lanes=(Lane(header="AUTHOR", render=hop),))


def _self_ref(info: dict) -> NodeRef:
    """Our node, from the self info of the companion: how one of our posts names its author."""
    key = str(info.get("public_key") or "").lower()
    return NodeRef(
        name=str(info.get("name") or "") or None,
        key=key or None,
        hash=key[:8] or None,
        type=NODE_TYPE_LABELS.get(info.get("adv_type")),
        is_self=True,
    )


def _author_ref(author: str, name_of: NodeResolver | None) -> NodeRef:
    """The author of a post as a node: always its hash, and its name if something names it."""
    from ..ui.room import author_label

    label, key = author_label(author, name_of) if name_of is not None else (author, None)
    return NodeRef(name=label if key else None, hash=author)


def _transcript(
    messages: list[ChatMessage],
    *,
    room: bool = False,
    name_of: NodeResolver | None = None,
    me: NodeRef | None = None,
) -> Listing:
    """The stored messages of a conversation, oldest first, one record for each.

    Args:
        messages: The transcript.
        room: Whether it is the board of a room. Then each row names its author, in an
            ``AUTHOR`` lane in the place of ``PEER`` (because the peer is the room on
            each row).
        name_of: Names the key of an author, for a room.
        me: Our node, the author of our own posts in a room.
    """
    from ..ui.report import Listing

    rows = []
    for message in messages:
        channel = (
            _channel_ref(message.channel_idx, message.peer_name or f"#{message.channel_idx}", None)
            if message.is_channel
            else None
        )
        rows.append(
            {
                "created_at": message.created_at,
                "direction": "out" if message.outbound else "in",
                "conversation": message.peer_name or message.peer or None,
                "node": None
                if message.is_channel
                else NodeRef(name=message.peer_name, hash=message.peer),
                "channel": channel,
                "author": (
                    _author_ref(message.author, name_of)
                    if message.author
                    else (me if room and message.outbound else None)
                ),
                "snr_db": message.snr,
                "text": message.text,
                "acked": message.acked,
            }
        )
    return Listing(
        key="messages",
        columns=_message_columns(author_lane=room),
        rows=rows,
        order=("TIME", "DIR", "AUTHOR" if room else "PEER", "SNR_DB", "TEXT"),
    )


def _live_messages() -> Listing:
    """The shape that ``chat listen`` streams: what arrived, when it arrived.

    Here ``TIME`` is absolute, but in the transcript it is an age. The reason is the base
    of the full time rule: with ages, each row of a live tail shows ``now``, which gives no
    information. There is no ``DIR`` lane, because all that a tail hears came in. The
    body goes last, so that the one field with no width can never push a lane.
    """
    from dataclasses import replace

    from ..ui import fields
    from ..ui.report import Listing

    def pin(column, width: int):  # noqa: ANN001, ANN202 - one column in, one column out
        return replace(column, lanes=tuple(replace(lane, width=width) for lane in column.lanes))

    return Listing(
        key="messages",
        columns=(
            pin(fields.instant("received_at", "TIME"), 25),
            pin(fields.plain_only(fields.name("conversation", "PEER")), 16),
            fields.node("node", lanes=()),
            fields.channel("channel", lanes=()),
            # The writer of a room post is in the document. The tail itself names the room,
            # which is the node that sent the post.
            _author_column(lane=False),
            pin(fields.snr("snr_db", "SNR_DB"), 6),
            fields.free("text", "TEXT"),
            fields.hidden("direction"),
            fields.hidden("acked"),
        ),
        order=("TIME", "PEER", "SNR_DB", "TEXT"),
    )


def _conversations(ctx: AppContext, rows: list, lasts: dict) -> Listing:
    """Each channel and contact, with the last message in each.

    ``LAST`` is an age, not an instant. A user looks through a conversation list for the
    conversations that are active. ``never`` is a real answer: the conversation exists,
    and it has no messages.
    """
    from ..ui import fields
    from ..ui.report import Listing

    records = []
    for conversation in rows:
        last = lasts.get(conversation.key)
        channel = (
            _channel_ref(conversation.channel_idx, conversation.label, conversation.secret)
            if conversation.is_channel
            else None
        )
        contact = getattr(conversation, "contact", None)
        if conversation.is_channel:
            kind = "channel"
        elif conversation.is_room:
            kind = "room"
        else:
            kind = "direct"
        records.append(
            {
                "kind": kind,
                "conversation": conversation.label,
                "channel": channel,
                "node": None
                if conversation.is_channel
                else NodeRef(
                    name=conversation.label,
                    key=(getattr(contact, "public_key", "") or "").lower() or None,
                    hash=(getattr(contact, "key_prefix", "") or "").lower() or None,
                    type=NODE_TYPE_LABELS.get(getattr(contact, "node_type", None)),
                ),
                "unread": ctx.chat.unread(conversation.key),
                "last_message_at": last.created_at if last is not None else None,
                # Not cut to 40 cells, as the preview of the picker is. Nothing here wraps,
                # so there is no reason to make a message shorter. But a body that holds a
                # newline still ends the record, so it is folded flat.
                "last_text": last.text if last is not None else None,
            }
        )
    return Listing(
        key="conversations",
        columns=(
            fields.word("kind", "KIND"),
            fields.plain_only(fields.name("conversation", "CONVERSATION")),
            fields.channel("channel", lanes=()),
            fields.node("node", lanes=()),
            fields.integer("unread", "UNREAD"),
            fields.when("last_message_at", "LAST", absent="never"),
            fields.free("last_text", "LAST_TEXT"),
        ),
        rows=records,
        order=("CONVERSATION", "KIND", "UNREAD", "LAST", "LAST_TEXT"),
    )


def _sent(
    *,
    contact: object | None = None,
    channel: int | None = None,
    acked: bool | None = None,
    scope: str | None = None,
) -> Facts:
    """What one ``chat send`` did.

    A direct message prints its ``acked`` state. That is the one thing that the send does
    not already tell the caller: the radio accepted the message in the two cases, and the
    answer of the peer is a separate fact. A channel broadcast prints nothing, because a
    channel *has* no acknowledgement. The document says that in the one way that the plain
    face cannot: with ``null``, not ``false``.

    The document also holds the ``scope`` under which a channel message went out, in the
    same shape as the scope of a received packet (:func:`~meshterm.ui.fields.scope`): the
    region, ``unscoped``, or ``null`` when it is not known (a direct message, or a default
    scope that MeshTerm could not read).
    """
    from dataclasses import replace

    from ..core.regions import UNSCOPED, WILDCARD, Scope
    from ..ui import fields
    from ..ui.report import PAIRS, SILENT, Facts

    sent_scope = None
    if scope == WILDCARD:
        sent_scope = UNSCOPED
    elif scope:
        sent_scope = Scope("scoped", scope)

    node = (
        NodeRef(
            name=contact.name,
            key=(getattr(contact, "public_key", "") or "").lower() or None,
            hash=(getattr(contact, "key_prefix", "") or "").lower() or None,
            type=NODE_TYPE_LABELS.get(getattr(contact, "node_type", None)),
        )
        if contact is not None
        else None
    )
    return Facts(
        key="sent",
        fields=(
            # `acked` is the only lane. The caller named the recipient, and a zero exit
            # already says that the radio accepted the message. Thus the one fact that is
            # left to report is whether the peer answered.
            fields.hidden("kind"),
            fields.node("node", lanes=()),
            fields.channel("channel", lanes=()),
            fields.hidden("sent"),
            fields.flag("acked", "acked"),
            # Only in the document. A direct message has no scope of its own to print, and a
            # channel send prints nothing.
            replace(fields.scope("scope", "scope"), lanes=()),
        ),
        values={
            "kind": (
                "channel"
                if channel is not None
                else ("room" if getattr(contact, "is_room", False) else "direct")
            ),
            "node": node,
            "channel": (
                _channel_ref(channel, f"#{channel}", None) if channel is not None else None
            ),
            "sent": True,
            "acked": acked,
            "scope": sent_scope,
        },
        shape=SILENT if channel is not None else PAIRS,
    )
