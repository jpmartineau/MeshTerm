# SPDX-License-Identifier: Apache-2.0
"""The ``chat`` tool: channel and direct messaging over the mesh.

Interactively it opens a conversation picker and then a live, full-screen chat (see
:mod:`meshterm.ui.chat`) where sent and received messages stream together. On the CLI it
exposes ``send``, ``history``, and ``list`` subcommands for scripted use. Channels are only
*listed* here for picking; creating and editing channel slots lives in the ``channels`` tool.

Every inbound message is recorded to history by the always-on
:class:`~meshterm.services.chat_service.ChatService`, and outbound messages are recorded on
send, so ``history`` reflects the full transcript regardless of which path produced it.
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
    ChatMessage,
    Contact,
    Conversation,
    is_direct_messageable,
)
from ..services.trace_runner import NameKeyResolver, make_name_key_resolver
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

#: How many recent messages ``chat history`` prints by default.
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
        """Nothing to gather here — the conversation picker lives inside :meth:`run`.

        The picker has to *stay pushed* while a chat runs, so backing out of a thread lands
        on the very list it was opened from — same cursor, same typed filter. A prompt
        gathered here would resolve, and pop, before the tool ran.

        Args:
            ctx: Shared application context.

        Returns:
            ``{"live": True}`` — the menu's marker for the interactive path.
        """
        return {"live": True}

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Open a live chat (menu) or perform a scripted messaging action (CLI).

        Args:
            ctx: Shared application context.
            params: Either ``conversation`` (menu) or a ``cli_action`` with its arguments.

        Returns:
            A :class:`ToolResult` summarizing what happened.
        """
        action = params.get("cli_action")
        if action is not None:
            return await self._run_cli(ctx, action, params)
        return await self._run_live(ctx)

    # -- interactive picker -----------------------------------------------------

    async def _run_live(self, ctx: AppContext) -> ToolResult:
        """Keep the conversation picker pushed and open chats above it until it is left.

        One screen for the whole visit, so backing out of a thread lands on the row it was
        opened from with the typed filter still narrowing the list. The picker used to be
        rebuilt from scratch each round and the cursor put back by a ``default=`` restore,
        which recovers the cursor alone — and only while the row it names still exists.

        The rows *are* data: an exchange moves its thread up the recency order, and deleting
        a history hollows its dot and demotes the row to the alphabetical tail. So they are
        re-read and swapped in place after each chat and each delete, which follows the
        highlighted thread wherever it moved to.

        Args:
            ctx: Shared application context.

        Returns:
            A :class:`ToolResult` counting the conversations opened and the messages the
            last one showed.
        """
        from ..ui.chat import open_chat
        from ..ui.tui import SelectScreen
        from ..ui.tui.screen import CANCEL

        picker = SelectScreen(
            "Chat — pick a conversation",
            await self._picker_items(ctx),
            # Enter pushes the chat screen, so the committing verb is `open`. Neither the
            # scroll nor the erase atom is written into the base: the select list splices
            # each in exactly when its key would act — ←→ only while the highlighted row
            # overflows, Del only on a thread that has history to lose — and the three of
            # them together are what has to stay inside 72 cells (the archive preview's list
            # is the same arithmetic). `Del erase` is that budget's doing: the row already
            # names the conversation, so the atom spends its cells on the verb, and `erase`
            # is the word the app uses elsewhere for taking data away.
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
                elif choice is CANCEL or choice is None:  # Esc — out to the menu
                    return ToolResult(summary={"conversations": opened, "messages": shown})
                else:
                    shown = await open_chat(ctx, choice)
                    opened += 1
                picker.replace_items(await self._picker_items(ctx))

    async def _picker_items(self, ctx: AppContext) -> list:
        """Build the picker's rows: the pinned lane names, the Channels group, then Direct.

        Read fresh every time the list is built or swapped, so a thread that just gained
        messages sits where its recency puts it. Cheap enough to redo after each chat: the
        two device reads go through the session cache and the rest is stored history.

        Args:
            ctx: Shared application context.

        Returns:
            The :class:`~meshterm.ui.tui.select.Choice` / ``Separator`` rows, in display
            order.
        """
        # Through the session cache: this picker runs on every Chat open, and its two
        # reads — the channel-slot probe and the contacts table — are the two slowest
        # round-trips on a companion. Reading them from the device each time is what made
        # opening Chat stall for seconds (the cached chat screen behind it never got the
        # chance to help). The cache holds channels until the channel editor writes a
        # slot and refreshes contacts in the background (see
        # :class:`~meshterm.services.device_state.DeviceState`).
        channels = _channels_from_slots(await ctx.devstate.channel_slots())
        contacts = await ctx.devstate.contacts()
        # Only companion nodes are listed — we don't DM repeaters, rooms, or sensors;
        # a contact whose type was never advertised gets the benefit of the doubt (the
        # app-wide DM rule, see is_direct_messageable).
        companions = [c for c in contacts if is_direct_messageable(c.node_type)]
        # A stable snapshot orders the rows (so the list doesn't reshuffle under the
        # cursor), while a self-refreshing view feeds each row's live preview
        # (see _LiveLasts).
        lasts = ctx.repo.last_chat_messages()
        live = _LiveLasts(ctx, seed=lasts)
        # Names in previews/mentions resolve back to keys for their hue (the app-wide
        # colour rule); a name no contact or stored advert carries stays muted.
        key_of = make_name_key_resolver(contacts, ctx.repo.node_names())

        # List contacts by recency — those with messages first, newest exchange at the top —
        # then the never-contacted ones alphabetically (see _recency_key).
        direct = [Conversation(label=c.name, is_channel=False, contact=c) for c in companions]
        direct.sort(key=lambda conv: _recency_key(conv, lasts))
        # One measurement for the whole list, headings and rows alike, taken before either is
        # built: the name lane is only as wide as the names actually in it, and every cell it
        # gives back goes to the message preview (see _lanes).
        lanes = _lanes(channels + direct)

        # The lane names pin for the whole picker (they mean the same in both groups),
        # so scrolling into Direct keeps them overhead with that group's heading under
        # them, instead of the header vanishing one row in — see Screen.sticky_rows.
        items: list = [
            Separator(lambda w: _picker_header(lanes, w), pinned=True),
            section_heading("📡 Channels"),
        ]
        for conversation in channels:
            items.append(
                Choice(
                    title=_row_title(ctx, conversation, live, key_of, lanes),
                    value=conversation,
                    # ←→ slide the message alone; the lanes in front of it hold (_Lanes.head).
                    hscroll_from=lanes.head,
                )
            )

        items.append(section_heading("👤 Direct"))
        if companions:
            for conversation in direct:
                items.append(
                    Choice(
                        title=_row_title(ctx, conversation, live, key_of, lanes),
                        value=conversation,
                        # Del offers to delete this thread's stored history —
                        # only where there is history to delete.
                        deletable=lasts.get(conversation.key) is not None,
                        hscroll_from=lanes.head,
                    )
                )
        else:
            items.append(Separator("  no contacts yet — receive an advert first"))

        return items

    @staticmethod
    async def _delete_history(ctx: AppContext, conversation: Conversation) -> None:
        """Confirm and delete one direct conversation's stored history.

        Deleting history is irreversible data loss, so the confirm wears the reserved
        red (``destructive``): Cancel on the left, the committing Delete on the right.
        On confirm the peer's messages are removed from the database and the thread's
        unread count is cleared; the contact itself (a device-side record) is untouched.
        """
        if not await ctx.ui.dialog(
            f"Delete the chat history with {conversation.label}? Every stored "
            "message in this conversation is removed.",
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
            ctx: Shared application context.
            action: One of ``send``, ``history``, ``list``.
            params: The action's arguments.

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
        """Send a channel or direct message and report whether it was acknowledged.

        A channel broadcast has nothing to report — there is no acknowledgement on a
        channel — so it prints nothing and lets the exit status say it went out. A direct
        message prints its ``acked`` state, which is the one thing the send does not
        already tell the caller: the radio accepted it either way, and whether the peer
        answered is a separate fact.

        A channel message goes out under the channel's send scope, or under ``scope`` for
        this one message (a region name, or ``*`` for unscoped); the document says which it
        went under. A radio that can't send under the scope refuses before anything is
        transmitted, and that is a failure (exit 1), never a quiet unscoped send.
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
        message = await ctx.chat.send_direct(contact, text)
        return ToolResult(
            summary={"to": contact.name, "acked": bool(message.acked)},
            report=(_sent(contact=contact, acked=bool(message.acked)),),
        )

    async def _cli_history(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Print a conversation's stored transcript."""
        limit = int(params.get("limit") or _HISTORY_LIMIT)
        channel = params.get("channel")
        to = params.get("to")
        if channel is not None:
            channel_id = await ctx.chat.channel_id_for(int(channel))
            messages = ctx.repo.recent_chat_messages(
                is_channel=True, channel_id=channel_id, limit=limit
            )
            label = f"#{channel}"
        else:
            device = await ctx.device()
            contact = _resolve_contact(await device.get_contacts(), str(to))
            messages = ctx.repo.recent_chat_messages(
                is_channel=False,
                peer=contact.key_prefix or contact.public_key[:12],
                limit=limit,
            )
            label = contact.name

        return ToolResult(
            summary={"conversation": label, "messages": len(messages)},
            report=(_transcript(messages),),
            exit_code=exitcodes.OK if messages else exitcodes.NO_RESULT,
        )

    async def _cli_listen(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Tail inbound messages live in the console (and record them to history).

        A plain-console receiver, outside the full-screen menu: it starts the always-on
        listener and prints each message as it arrives. Doubles as a diagnostic — with the
        pump's debug logging enabled (``--debug`` on the CLI) it shows whether messages are
        being pulled from the companion at all.

        Args:
            ctx: Shared application context.
            params: ``seconds`` — how long to listen (``0``/``None`` = until interrupted).

        Returns:
            A :class:`ToolResult` with how many messages were seen.
        """
        from ..ui import script

        seconds = params.get("seconds") or 0
        await ctx.device()
        await ctx.chat.start()  # begins recording inbound to history too
        # About the run, not part of its answer: stderr, so a redirected stdout holds
        # nothing but messages.
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
                    await asyncio.Event().wait()  # until Ctrl-C / cancellation
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
        """List channels, contacts, and their most recent message."""
        device = await ctx.device()
        channels = await _read_channels(device)
        contacts = await device.get_contacts()
        lasts = ctx.repo.last_chat_messages()

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

        @chat_app.command("send", help="Send a message to a contact or channel")
        def _send_cmd(
            text: str = typer.Argument(..., help="The message body"),
            to: str | None = typer.Option(None, "--to", help="Contact name or key prefix"),
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
            to: str | None = typer.Option(None, "--to", help="Contact name or key prefix"),
            channel: int | None = typer.Option(None, "--channel", help="Channel slot index"),
            limit: int = typer.Option(_HISTORY_LIMIT, "--limit", help="Max messages to show"),
        ) -> None:
            if (to is None) == (channel is None):
                raise typer.BadParameter("Pass exactly one of --to / --channel.")
            run_tool_command(
                self,
                {"cli_action": "history", "to": to, "channel": channel, "limit": limit},
            )

        @chat_app.command("list", help="List channels, contacts, and recent messages")
        def _list_cmd() -> None:
            run_tool_command(self, {"cli_action": "list"})

        @chat_app.command("listen", help="Tail inbound messages live in the console")
        def _listen_cmd(
            seconds: int = typer.Option(
                0, "--seconds", "-s", help="How long to listen (0 = until Ctrl-C)"
            ),
            debug: bool = typer.Option(
                False, "--debug", help="Log the message-pull activity (diagnostic)"
            ),
        ) -> None:
            if debug:
                _enable_receive_debug()
            run_tool_command(self, {"cli_action": "listen", "seconds": seconds})

        app.add_typer(chat_app, name=self.name)


# -- helpers ------------------------------------------------------------------


def _cli_scope(scope: str, channel: int | None) -> str:
    """Check ``--scope`` before anything is sent: a region the firmware could hold, or ``*``.

    A usage error (exit 2), not a device failure: nothing has been transmitted yet. A
    direct message has no scope to give it here — the firmware floods a DM under the
    session scope like anything else, but only a channel send sets one for itself.

    Raises:
        typer.BadParameter: For a direct send, or a name the firmware would refuse.
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
    """Route the message-pull and library debug logs to the console for diagnosis.

    Raises the ``meshcore`` and connection-layer loggers to DEBUG and attaches a stderr
    handler, so ``chat listen --debug`` shows whether inbound messages are being pulled
    from the companion (the receive path) at all.
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
    """Turn cached channel slots into channel conversations, always offering Public (slot 0).

    The picker reads its channels through the session cache
    (:meth:`~meshterm.services.device_state.DeviceState.channel_slots`) so it reuses the one
    slow slot probe instead of re-walking every slot on each Chat open; this maps that cached
    :class:`~meshterm.core.channel_probe.ChannelSlot` list onto the picker's
    :class:`~meshterm.core.models.Conversation` rows. Channel 0 (the default public channel) is
    synthesised when the firmware reports no slot for it, so there is always somewhere to chat —
    the same guarantee :func:`_read_channels` (the CLI path) makes.

    Args:
        slots: The configured channel slots, from the session cache.

    Returns:
        One :class:`~meshterm.core.models.Conversation` per slot, with Public prepended when
        slot 0 is absent.
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
    """Probe channel slots and return them as channel conversations.

    Channel 0 (the default public channel) is always offered even when the firmware reports
    no configured slots, so there is always somewhere to chat.

    Args:
        device: The connected device to probe.

    Returns:
        One :class:`~meshterm.core.models.Conversation` per configured channel (at least
        channel 0).
    """
    conversations: list[Conversation] = []
    for idx in range(CHANNEL_SLOT_PROBE_CAP):
        try:
            channel = await device.get_channel(idx)
        except Exception:  # noqa: BLE001 - firmware may not support channel reads
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
    """Find a contact by exact name (case-insensitive) or key-prefix match.

    Args:
        contacts: The known contacts.
        needle: A contact name or public-key prefix.

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
    """Sort key ordering conversations by recency, then name.

    Conversations that have been chatted with sort first, most-recent exchange at the top;
    those never chatted with sort after them, alphabetically by label. The leading ``0``/``1``
    keeps the two groups apart so their differently-typed tie-breakers never compare.

    Args:
        conversation: The conversation to rank.
        lasts: Map of conversation key to its most recent message.

    Returns:
        A tuple usable as a ``sorted`` key.
    """
    last: ChatMessage | None = lasts.get(conversation.key)
    if last is not None:
        return (0, -last.created_at.timestamp())
    return (1, conversation.label.casefold())


class _LiveLasts:
    """A self-refreshing view of each conversation's most recent message.

    The picker snapshots :meth:`~meshterm.persistence.repository.Repository.last_chat_messages`
    once to *order* the rows (so the list never reshuffles under the cursor), but the row
    previews read through this so a message arriving while the list is open updates the
    sender/text preview on the next repaint. It re-queries the repository at most a few times a
    second (bounded by ``ttl``) rather than once per row per repaint, so a wide list stays cheap.
    """

    def __init__(self, ctx: AppContext, *, seed: dict, ttl: float = 0.5) -> None:
        """Bind to a context, seeding the cache with the snapshot already loaded at open."""
        self._ctx = ctx
        self._ttl = ttl
        self._cache = seed
        self._at = time.monotonic()

    def get(self, key: str) -> ChatMessage | None:
        """Return the latest message for ``key``, refreshing the cache once its TTL lapses."""
        now = time.monotonic()
        if now - self._at >= self._ttl:
            try:
                self._cache = self._ctx.repo.last_chat_messages()
            except Exception:  # noqa: BLE001 - keep the last good snapshot on a read error
                pass
            self._at = now
        return self._cache.get(key)


#: Width of the unread-badge lane between the label and the age (fits ``● 999``).
_BADGE_WIDTH = 5
#: Width of the relative-age lane between the badge and the preview (right-aligned; fits ``now``
#: and two-digit spans like ``59m`` / ``23h``), so every row's message text starts in one column.
_AGE_WIDTH = 3
#: Floor for the conversation lane. The header word is ``CONVERSATION`` (12 cells) and the
#: lane carries a two-cell gap after it, so anything narrower than this runs the heading
#: straight into ``UNREAD`` on a mesh whose every name is ``Bob``.
_LABEL_MIN = 12
#: Ceiling for it. The lane is sized to the longest name it actually holds (see
#: :func:`_lanes`), and this is where that stops paying: one very long name would otherwise
#: buy padding in front of every *short* one. It holds the long-but-ordinary shape of a
#: repeater-style name (``YUL-Cartierville``), a name past it ellipsizes — the row is a way
#: in to the conversation, whose own screen is titled with the whole name — and every cell
#: the ceiling saves goes to the preview.
_LABEL_MAX = 16


@dataclass(frozen=True)
class _Lanes:
    """The picker's fixed lane widths, measured once per list build.

    Both the column header and every row are laid out from one of these, so the headings
    cannot drift off the columns they name. Only the first two vary: the marker is the
    platform's own glyph width (a channel glyph is double-cell on regular and single on the
    PicoCalc console) and the label lane is sized to the longest name the list actually
    holds, within :data:`_LABEL_MIN` / :data:`_LABEL_MAX`.

    Attributes:
        marker: The leading glyph lane — a channel glyph or contact dot plus its space.
        label: Cells the conversation name is padded/ellipsized to.
    """

    marker: int
    label: int

    @property
    def head(self) -> int:
        """Cells before the preview: everything ←→ leave pinned (:attr:`Choice.hscroll_from`).

        Marker, name, unread badge and age, with the two-cell gap that follows each — the
        columns that say *which conversation this row is*. The preview is the only run that
        slides, which is the point: the lanes are the reader's place in the list and they
        already fit.
        """
        return self.marker + self.label + 2 + _BADGE_WIDTH + 2 + _AGE_WIDTH + 2


def _lanes(conversations: list[Conversation]) -> _Lanes:
    """Measure the picker's lanes against the conversations it is about to draw.

    The name lane used to be a flat 22 cells, which is a wide claim on a 72-column terminal
    and a very wide one on the PicoCalc's 53: every cell it holds past the longest name in
    the list is padding taken straight out of the last-message preview. Sized to the content
    instead, a mesh of ``Alice`` and ``Bob`` keeps its names inside the header word's own
    lane and gives everything else to the messages.

    Args:
        conversations: Every row the list will hold, channels and direct alike.

    Returns:
        The lane widths for both the header and the rows.
    """
    longest = max((cell_len(c.label) for c in conversations), default=0)
    return _Lanes(
        # The marker is "glyph + space", and the glyph is the platform's: double-cell on
        # regular, single on the console font. Read from the widget rather than assumed, so
        # the two groups' lanes line up on both platforms — a contact's dot is padded to
        # whatever a channel's glyph measures here (see _append_marker).
        marker=cell_len(channel_glyph("Public", None)) + 1,
        label=max(_LABEL_MIN, min(_LABEL_MAX, longest)),
    )


def _picker_header(lanes: _Lanes, width: int) -> str:
    """Column headers over the picker's fixed lanes (see :func:`_title` for the layout).

    The indent covers the select screen's pointer column (2 cells, drawn on choice rows but
    not separators) plus the marker lane, so each header lands exactly over its column.
    UNREAD borrows its lane's trailing gap — the badge lane itself is one cell too narrow
    for the word — which still leaves a space before the age column.

    Resolved against the render width (the header row is pinned, so it must stay one row):
    on a terminal too narrow for the whole line, ``LAST MESSAGE`` gives its cells back a
    word at a time rather than the line wrapping or losing the label (see
    :func:`~meshterm.ui.menus.column_header`).
    """
    return column_header(
        [
            Lane("CONVERSATION", lanes.label + 2),
            Lane("UNREAD", _BADGE_WIDTH + 2),
            Lane("TIME", _AGE_WIDTH + 2),
            Lane(("LAST MESSAGE", "LAST MSG", "LAST")),
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
) -> Callable[[], str | Text]:
    """Return a picker-row title *callable* the select screen re-renders on each repaint.

    Both the unread badge and the last-message preview are read live, so a message arriving
    while the picker sits open updates that row's ``●`` count *and* its sender/text preview on
    the next repaint (the session already repaints ~1×/s for the header).

    Args:
        ctx: Shared application context (for the live unread count).
        conversation: The conversation the row represents.
        lasts: The self-refreshing latest-message view feeding the preview.
        key_of: Maps a sender name back to its node's key, for the preview hues.
        lanes: The list's measured lane widths.

    Returns:
        A zero-argument callable producing the current row title.
    """
    return lambda: _title(ctx, conversation, lasts, key_of, lanes)


def _title(
    ctx: AppContext,
    conversation: Conversation,
    lasts: _LiveLasts,
    key_of: NameKeyResolver,
    lanes: _Lanes,
) -> str | Text:
    """Build a picker row as fixed-width, colour-coded lanes.

    Alignment carries the readability — marker, label, unread badge, relative age, and preview
    each sit in their own lane, so every row's message text starts in the same column. Colour is
    purposeful: a direct contact's name takes its key-derived palette hue (a channel label stays
    base), the leading dot is the standard companion pink with its shape marking history, the
    unread ``●`` badge is red, the age is muted, and the preview mutes its body while lighting
    sender names and ``@mentions`` in their key-derived hue — the same colours the live
    transcript uses. The row is always a Rich :class:`~rich.text.Text` so those spans survive
    under the select screen's row highlight.

    Args:
        ctx: Shared application context (for the live unread count).
        conversation: The conversation the row represents.
        lasts: The self-refreshing latest-message view.
        key_of: Maps a preview sender/mention name back to its node's key.
        lanes: The list's measured lane widths.

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
    # Unread badge lane (_BADGE_WIDTH cells): a red ● with the count in warn, or blank filler so
    # the following lanes still line up on rows with nothing unread.
    if unread:
        text.append("●", style="err")
        text.append(f" {unread}".ljust(_BADGE_WIDTH - 1), style="warn")
    else:
        text.append(" " * _BADGE_WIDTH)
    # Relative-age lane (right-aligned) sits between the badge and the message text, so the ages
    # stack in one tidy column and the previews all start at the same place.
    age = _ago(last.created_at) if last is not None else ""
    text.append("  ")
    text.append(f"{age:>{_AGE_WIDTH}}", style="muted")
    text.append("  ")
    if last is not None:
        text.append_text(_preview_text(last, key_of))
    return text


#: The standard companion pink — the shared plain-node ``●`` colour — the contact dot's
#: hue: the shape (filled/hollow) marks history, the colour marks "a companion", and the
#: name beside it carries the person's own key-derived hue.
_COMPANION_DOT_STYLE = NODE_GLYPHS[NODE_TYPE_CHAT][1]


def _append_marker(
    text: Text, conversation: Conversation, last: ChatMessage | None, lanes: _Lanes
) -> None:
    """Prepend the row's leading marker — a channel glyph or a contact dot — in its own lane.

    A channel keeps its openness marker (＃ / 🌐 / 🔒). A contact gets a small circle in the
    standard companion pink — filled (``●``) once we've exchanged messages, a hollow ring
    (``○``) before any — so the hollow-vs-filled shape marks whether there's history while the
    name itself carries the person's key-derived hue.

    The dot is padded out to :attr:`_Lanes.marker`, which is measured from the channel glyph
    itself rather than assumed: that glyph is double-cell on regular and single-cell on the
    PicoCalc console font, so a hard-coded pad lined the two sections up on one platform and
    left every channel row a cell to the left of every direct row on the other.
    """
    if conversation.is_channel:
        glyph = channel_glyph(conversation.label, conversation.secret)
        text.append(glyph + " " * max(1, lanes.marker - cell_len(glyph)))
    else:
        dot = "●" if last is not None else "○"
        text.append(dot, style=_COMPANION_DOT_STYLE)
        text.append(" " * max(1, lanes.marker - cell_len(dot)))


def _preview_text(last: ChatMessage, key_of: NameKeyResolver) -> Text:
    """A muted last-message preview with sender names and ``@mentions`` lit in their hue.

    Mirrors the live transcript: our own messages get a ``you:`` prefix, an inbound channel
    message's inline ``Name:`` sender is coloured in its key-derived hue (muted when no known
    node carries the name), and every ``@[Name]`` mention reads as a bare ``@Name`` the same
    way — so the list and the chat speak the same colour language.

    The preview is built *whole*, however long the message ran. It used to be clipped to a
    fixed 40 cells, which is a cut nothing could undo: the row is what ←→ scroll now, and a
    preview pre-truncated to roughly the visible lane would have had nothing left to reveal.
    The list cuts it at the right edge like any other row; the scroll walks past that.
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
    else:
        _append_body(text, body_raw, key_of)
    return text


def _append_body(text: Text, body: str, key_of: NameKeyResolver) -> None:
    """Append ``body`` to ``text``, muted, lighting up the names it mentions.

    Each ``@[Name]`` is drawn in that node's key-derived hue, or left muted when the
    name resolves to no node we know.
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
    """The column age for a message time, through THE grammar (``format_age``).

    One deliberate difference from the widget: a missing or naive timestamp (a stray one
    from the wire) reads as a *blank* lane rather than ``never`` — the picker wants an
    empty cell there, not a word.
    """
    if getattr(when, "tzinfo", None) is None:
        return ""
    return format_age(age_seconds(when))


def _channel_ref(idx: int | None, label: str, secret: bytes | None) -> ChannelRef:
    """One channel as the shared shape, from whatever the surface knows about it.

    A transcript row and a live message know a slot and a label but not the key, so
    ``public`` is read off the name where the secret is not in hand — which is what the
    leading ``#`` means and how a public channel is written everywhere else in the app.
    """
    return ChannelRef(
        slot=int(idx or 0),
        name=label,
        public=is_public_channel(label, secret) if secret else label.startswith("#"),
        hash=channel_hash(secret) if secret else None,
    )


def _message_columns() -> tuple:
    """One transcript row's columns, shared by ``history`` and the ``listen`` stream.

    ``DIR`` carries what the menu's transcript writes into its sender lane as the word
    "you": which way a message went is a fact of its own, and spending the name lane on it
    made our own name a value that lane could not otherwise hold. ``PEER`` is therefore
    always the *other* party.

    ``TEXT`` is last and escaped: it is the rest of the line, and "the rest of the line"
    stops being true the moment a body holds a newline — after which the remainder reads
    as a second record with an empty time. The document carries the body raw, a JSON string
    holding a newline natively.
    """
    from ..ui import fields

    return (
        fields.when("created_at", "TIME"),
        fields.word("direction", "DIR"),
        # One column for whichever end the conversation had; the document keeps the two
        # apart, because a parser cannot tell a channel from a contact by its label.
        fields.plain_only(fields.name("conversation", "PEER")),
        fields.node("node", lanes=()),
        fields.channel("channel", lanes=()),
        fields.snr("snr_db", "SNR_DB"),
        fields.free("text", "TEXT"),
        fields.hidden("acked"),
    )


def _transcript(messages: list[ChatMessage]) -> Listing:
    """A conversation's stored messages, oldest first, one record each."""
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
                "snr_db": message.snr,
                "text": message.text,
                "acked": message.acked,
            }
        )
    return Listing(
        key="messages",
        columns=_message_columns(),
        rows=rows,
        order=("TIME", "DIR", "PEER", "SNR_DB", "TEXT"),
    )


def _live_messages() -> Listing:
    """The shape ``chat listen`` streams: what arrived, as it arrived.

    ``TIME`` is absolute here where the transcript's is an age, and for the reason the
    whole time rule turns on: every row of a live tail would read ``now``, which is no
    information at all. There is no ``DIR`` lane — everything a tail hears came in — and
    the body goes last, so the one field with no width can never push a lane.
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
            pin(fields.snr("snr_db", "SNR_DB"), 6),
            fields.free("text", "TEXT"),
            fields.hidden("direction"),
            fields.hidden("acked"),
        ),
        order=("TIME", "PEER", "SNR_DB", "TEXT"),
    )


def _conversations(ctx: AppContext, rows: list, lasts: dict) -> Listing:
    """Every channel and contact, with the last thing said in each.

    ``LAST`` is an age rather than an instant: a conversation list is scanned for what is
    warm, and ``never`` is a real answer — the conversation exists and nothing has been
    said in it.
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
        records.append(
            {
                "kind": "channel" if conversation.is_channel else "direct",
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
                # Not truncated to 40 cells the way the picker's preview is: nothing here
                # wraps, so there is no reason to cut a message short — but a body that
                # holds a newline would still end the record, so it is folded flat.
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

    A direct message prints its ``acked`` state, which is the one thing the send does not
    already tell the caller: the radio accepted it either way, and whether the peer
    answered is a separate fact. A channel broadcast prints nothing, because there *is* no
    acknowledgement on a channel — and the document says that in the one way the plain
    face never could, with ``null`` rather than ``false``.

    The document also carries the ``scope`` a channel message went out under, in the same
    shape a received frame's scope takes (:func:`~meshterm.ui.fields.scope`): the region,
    ``unscoped``, or ``null`` where it isn't known (a direct message, or a default scope
    that couldn't be read).
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
            # `acked` is the only lane: the caller named the recipient and the radio
            # accepting the message is what a zero exit already says, so the one fact
            # left to report is whether the peer answered.
            fields.hidden("kind"),
            fields.node("node", lanes=()),
            fields.channel("channel", lanes=()),
            fields.hidden("sent"),
            fields.flag("acked", "acked"),
            # Document only: a direct message has no scope of its own to print, and a
            # channel send prints nothing at all.
            replace(fields.scope("scope", "scope"), lanes=()),
        ),
        values={
            "kind": "channel" if channel is not None else "direct",
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
