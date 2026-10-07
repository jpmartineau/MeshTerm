# SPDX-License-Identifier: Apache-2.0
"""The live chat screen and the function that opens it.

This is the interactive, full-screen chat: a transcript that scrolls, with an input line
pinned at the bottom. The messages that you send and the messages that come in over the
mesh show together in real time. This module is in the UI layer, the same as the device
picker and the config editor, but it can depend on the context and the services. It
connects the send path of :class:`~meshterm.services.chat_service.ChatService` and the
event hub, which always runs, to a :class:`~meshterm.ui.tui.screen.Screen`.

While the screen is open, it subscribes to the hub. Thus the inbound messages for the
current conversation are appended live. Separately,
:class:`~meshterm.services.chat_service.ChatService` stores each inbound message. Thus
the history is complete, also when the screen is not open.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from rich.console import Group, RenderableType
from rich.text import Text

from ..core import links
from ..core.channels import MENTION, split_channel_sender
from ..core.connection import ContactNotOnDeviceError
from ..core.events import EventKind, MeshEvent
from ..core.models import ChatMessage, Contact, Conversation, Message, utcnow
from ..core.regions import WILDCARD
from ..platforms import Platform, on_platform
from .qr import QrScreen, qr_strip
from .theme import name_style, snr_style
from .tui.prompt import (
    CHANNEL_BYTE_LIMIT,
    DM_BYTE_LIMIT,
    LineEditor,
    byte_counter,
)
from .tui.render import render_hanging, render_lines, right_aligned_tail
from .tui.screen import CANCEL, Screen
from .tui.spinner import Spinner, spinner_interval
from .widgets import name_chip

if TYPE_CHECKING:
    from ..context import AppContext


#: The delivery marks for a *resolved* outbound direct message, at the end of its line:
#: acknowledged, or transmitted but not acknowledged (^R can retry it). These are the
#: ``✓``/``✗`` status marks of the app in their ok/err styles, not the ✅/❌ emoji, which
#: are for the lane of packet-class icons. While the ack is still pending, the line shows an
#: animated spinner instead (refer to :meth:`ChatScreen._delivery_glyph`).
_DELIVERED = ("✓", "ok")
_FAILED = ("✗", "err")

#: A word between whitespace. The wrap moves a word only as a whole. Thus the run that a
#: wrap must not cut a URL in is the word that has the URL, with a bracket or a full stop
#: next to it.
_WORD = re.compile(r"\S+")

#: The column where a URL starts when it is too long to hang under its body: the column of
#: the timestamp, after the ``❯`` that is in front of the first line of the selected message.
_URL_GUTTER = 2

#: Whether the URLs of a message are drawn as QR codes under it (``Platform.url_codes``,
#: only on the PicoCalc). Bound for each platform, never asked at each frame.
_URL_CODES = False


@on_platform
def _bind(platform: Platform) -> None:
    """Hang codes under URLs where the platform draws them (now and at each switch)."""
    global _URL_CODES
    _URL_CODES = platform.url_codes


class ChatScreen(Screen):
    """A live conversation: a transcript that scrolls, above a pinned input line.

    The transcript automatically stays on the newest message (and goes back to the bottom
    each time you type or send). ↑ selects a message. The selection moves with
    ↑↓/PgUp/PgDn, and the viewport moves with it. ^End (or Esc) puts the focus on the
    compose line again. Enter sends the current line, or does an action on a selected
    message: in a channel, it starts a reply with an ``@mention``, and in a direct chat, it
    opens the delivery paths of the message. In the two types of chat, ^P opens the paths
    of the selected message. A path belongs to one message, so when no message is
    selected, there is nothing to show. ^U shows the links of the selected message as QR
    codes, on the share screen. When no message is selected, Esc leaves the chat.

    A channel with a send scope shows its region in the title (``#ops · yul``). There, ^R
    sends a message again *unscoped*: the selected message, or the newest when no message is
    selected. This is the solution for a scoped message that no repeater in range relays.
    It comes after an amber confirm, because an unscoped flood gets to each repeater that
    the scope kept it from.
    """

    floating = False
    #: Home and End move the caret of the compose line here. Thus edge scroll does not use
    #: them.
    home_end_jumps = False

    @property
    def picocalc_lyra_lane(self):
        """The PicoCalc lane in the words of the transcript: Latest/Oldest, Paths, Retry.

        The Shift bank of the shared lane sends the plain ``end``/``home`` actions. But
        here the cursor of the compose line already uses them (refer to :meth:`handle`).
        Thus, without a change, the two keyboard keys go to the same fallback ("clear the
        selection, stay at the tail"), and each one seems to scroll to the bottom. Instead,
        the two Shift slots send ``ctrl_end``/``ctrl_home``, and their labels say what
        these actions do to a conversation: go back to the *latest* message and the
        compose line, or go back to the *oldest*. This is the shared pair with new labels
        in the same slots. Thus it keeps the shared sides: each jump is still behind the
        page key that goes in the same direction (refer to
        :data:`~meshterm.ui.tui.fkeys.DEFAULT_LANE`). F4/F5 keep the shared paging pair.
        From the outside, the transcript moves by one screen, also when it is the selection
        that moves.

        The F3 pair follows the two rules of the lane. *Retry* is not there in a channel,
        because a channel message is never acknowledged, so there is nothing to retry. In
        a direct chat with nothing outstanding, it is only dim. The Shift slot of a channel
        is *Resend* instead: the selected message (or the newest) again, unscoped (refer
        to :meth:`_retry_target`). It is there when a resend is connected, and it is dim
        until that message is one of ours that went out under a region. *Paths* must have
        a selected message to show the paths of, and the two navigation slots must have a
        transcript to move through.

        F1/F2 have the **day** jump: the section step of the transcript (its dividers are
        days). A grouped select list does the same with ``Sect ↑``/``Sect ↓``, on the same
        keyboard keys, which send the same ``ctrl_pageup``/``ctrl_pagedown``. The label
        names what the sections *are* here, because of the lane rule that a chip names an
        action. A left-hand pair rises toward F1. Thus ``Day ↑`` (back through the
        transcript) is on the outer side of ``Day ↓``, the same as the pager on the right
        and the list that it copies. These chips are lit only when there is more than one
        day to step between. A conversation in one afternoon has no sections, the same as
        a list with one section.

        ``QR`` (^U) is on the Shift half of F2, next to ``Retry`` on the Shift half of F3.
        The two are actions *on* the selected message: the codes of its links on a share
        screen, larger than the codes under it in the transcript. ``QR`` is lit while the
        selection has a URL.
        """
        from .tui.fkeys import FPair, default_lane

        lane = list(default_lane(nav=bool(self._messages)))
        live = bool(self._messages)
        # The messages are in time order. Thus there are two days exactly when the two ends
        # have different dates. This costs O(1). A walk through the dividers dates the full
        # transcript again at each paint.
        days = live and (
            self._messages[0].created_at.astimezone().date()
            != self._messages[-1].created_at.astimezone().date()
        )
        lane[0] = FPair("Day ↑", "ctrl_pageup", enabled=days)
        lane[1] = FPair(
            "Day ↓",
            "ctrl_pagedown",
            "QR",
            "url_code",
            enabled=days,
            opp_enabled=bool(self._picked_urls()),
        )
        lane[3] = FPair("Page ↓", "pagedown", "Latest", "ctrl_end", enabled=live, opp_enabled=live)
        lane[4] = FPair("Page ↑", "pageup", "Oldest", "ctrl_home", enabled=live, opp_enabled=live)
        if not self._is_channel:
            retry = ("Retry", "retry")
        elif self._resend_unscoped is not None:
            retry = ("Resend", "retry")
        else:
            retry = ("", "")
        lane[2] = FPair(
            "Paths",
            "paths",
            *retry,
            enabled=self._selected is not None and self._paths is not None,
            opp_enabled=self._retry_target() is not None,
        )
        return lane

    def __init__(
        self,
        conversation: Conversation,
        messages: list[ChatMessage],
        *,
        send: Callable[[str], Awaitable[ChatMessage | None]],
        names: dict[str, str],
        session,  # noqa: ANN001 - a TuiSession, imported late to prevent an import cycle
        resend: Callable[[ChatMessage], Awaitable[ChatMessage]] | None = None,
        paths: Callable[[ChatMessage], Awaitable[None]] | None = None,
        key_of: Callable[[str], str | None] | None = None,
        scope: str | None = None,
        resend_unscoped: Callable[[ChatMessage], Awaitable[ChatMessage | None]] | None = None,
    ) -> None:
        """Build the chat screen.

        Args:
            conversation: The thread that the screen shows (its label is the title of the
                screen).
            messages: The initial transcript (history), the oldest first.
            send: An async callable that sends a line and returns the stored outbound
                message (or ``None`` if nothing was sent).
            names: A map from contact key prefix to friendly name, for the labels of
                inbound direct messages.
            session: The running :class:`~meshterm.ui.tui.session.TuiSession`. The screen
                uses it to ask for a paint when messages arrive or a send completes.
            resend: An async callable that tries again to deliver a direct message that
                is not acknowledged, and changes the message in place (direct chats only,
                ``None`` for channels).
            paths: An async callable that shows the delivery paths of the selected
                message (the ^P view). ``None`` makes this action do nothing, with no
                message.
            key_of: Finds the key of the node of a sender from its display name (refer to
                :func:`~meshterm.services.trace_runner.make_name_key_resolver`). The key
                is the source of the hue of the sender. With ``None`` (or for a name that
                it cannot find), the sender gets the grey of an unknown node
                (``node.unknown``), because colour is only for identities that have a key.
            scope: The send scope of the channel, shown in the title as a bare ``·`` atom
                (``#ops · yul``: the title of a channel has no other atom that the user
                can think is a region name). ``None`` for a channel that sends under the
                default scope of the device, and always ``None`` for a direct chat.
            resend_unscoped: An async callable that sends the text of a channel message
                again, unscoped, and returns the stored message (channels only. With
                ``None``, ^R does nothing there).
        """
        super().__init__()
        self._scope = scope if conversation.is_channel else None
        self.title = f"{conversation.label} · {self._scope}" if self._scope else conversation.label
        self._is_channel = conversation.is_channel
        # Many voices or one. The user reads a channel and a room in the same way: each
        # message under its author, and Enter on a message starts a reply with an
        # @mention. A direct chat has two persons, and Enter on a message opens its paths.
        self._multiparty = conversation.is_channel or conversation.is_room
        # Whether typing goes to the compose line. It is off where no typed text can be
        # sent (a room that keeps nothing from us). Thus the keyboard keys never fill a
        # line that the user cannot see.
        self._composing = True
        self._key_of: Callable[[str], str | None] = key_of or (lambda name: None)
        # The one remote sender of a direct thread is the peer. Its key gives the colour of
        # the header, also when the resolver cannot find the display name.
        contact = conversation.contact
        self._peer_key = (
            ""
            if conversation.is_channel or contact is None
            else (contact.public_key or contact.key_prefix or "")
        )
        self._messages = list(messages)
        self._send = send
        self._resend = resend
        self._resend_unscoped = resend_unscoped if conversation.is_channel else None
        self._paths = paths
        self._names = names
        self._session = session
        self._editor = LineEditor()
        self._sending = False
        # It turns while a direct message is in flight, so that the glyph at the end of the
        # message spins (instead of a static hourglass) until the ack resolves. All messages
        # share it: only one send or retry is ever in flight at a time (``_sending``
        # controls the two).
        self._spinner = Spinner()
        self._status = ""
        self._stick = True  # keep the newest message visible until the user scrolls up
        self._paths_open = False  # one paths dialog at a time
        self._codes_open = False  # one share screen of links at a time
        self._paste_open = False  # one paste-confirm dialog at a time
        self._resend_open = False  # one resend-unscoped confirm at a time
        # The selection: the index of the highlighted message (or None when the compose
        # line has the focus), and the body line where it rendered, so that the frame keeps
        # it visible.
        self._selected: int | None = None
        self._selected_line: int | None = None
        # One line after the last rendered row of the selected message, so that its full
        # block stays visible, not only its head (refer to render_body).
        self._selected_end: int | None = None
        # The cache of the output of _render_grouped for each message. Thus a paint that
        # something outside the transcript causes (a key press, the 1 s idle tick, an
        # animation step of a spinner) renders again only the rows that changed. Refer to
        # _render_grouped.
        self._render_cache: dict | None = None
        # Strong references to the sends, resends, and spinner tickers that a key handler
        # starts. The event loop holds only a *weak* reference. Thus the garbage collector
        # can remove a task that nothing else names while it runs: the result is a message
        # that is only half transmitted, or a spinner that stops. Each task removes itself
        # from the set when it finishes.
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> asyncio.Task:  # noqa: ANN001 - any coroutine of this screen
        """Start ``coro`` on the loop and keep a reference to it until it finishes."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @property
    def footer_hint(self) -> str:
        """The key hint. It shows whether a message is selected and what Enter does to it.

        Only the keys that do something show. ``^P paths`` must have a selected message to
        show the paths *of*. ``^U QR`` must have a selected message with a URL in it
        (refer to :meth:`_picked_urls`). ``^R retry failed`` must have something to retry
        (refer to :meth:`_retry_target`). The F-key lane uses the same rules to dim its
        slots.
        """
        if self._selected is not None:
            code = " · ^U QR" if self._picked_urls() else ""
            if self._is_channel and self._retry_target() is not None:
                # A selected scoped message of ours can be sent again unscoped, and the hint
                # must name that key. The "(@mention)" of the reply and ^End are removed to
                # keep the line inside 72 cells (Esc still cancels the selection, and Enter
                # still says what it does). ↑↓ is also removed when the hint must also name
                # the code of a link.
                pick = "" if code else " · ↑↓ pick"
                return f"Enter reply · ^P paths{code} · ^R resend unscoped{pick} · Esc cancel"
            if self._is_channel:
                return f"Enter reply (@mention) · ^P paths{code} · ↑↓ pick · ^End/Esc cancel"
            return f"Enter paths{code} · ↑↓ pick · ^End/Esc cancel"
        if self._retry_target() is not None:
            if self._is_channel:
                return "Enter send · ↑ pick a message · ^R resend unscoped · Esc back"
            return "Enter send · ↑ pick a message · ^R retry failed · Esc back"
        return "Enter send · ↑ pick a message · Esc back"

    # --- live updates --------------------------------------------------------

    def append(self, message: ChatMessage) -> None:
        """Append an inbound message to the transcript and paint."""
        self._messages.append(message)
        # Do not pull the viewport to the tail while the user selects a message to reply to.
        if self._selected is None:
            self._stick = True
        self._session.invalidate()

    # --- rendering -----------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render the transcript, a divider, the input line, and the status, if there is one."""
        self._selected_line = None
        self._selected_end = None
        if not self._messages:
            lines = render_lines(Text(self._empty_text, style="muted"), width)
        else:
            # Direct threads and channel threads use the same grouped transcript, in the
            # style of Discord or Slack: consecutive messages from one sender share a
            # coloured header, with day dividers between them. Because the render is for
            # each message, we can also store where the selected message is (refer to
            # _selected_line / cursor_line). The user can select in the two types of thread.
            # The only difference is what Enter then does with the selection: a reply in a
            # channel, or the delivery paths in a direct thread.
            if self._selected is not None:
                self._selected = max(0, min(self._selected, len(self._messages) - 1))
            lines = self._render_grouped(width)

        compose = self._compose_line(width)
        footer_parts: list[RenderableType] = [
            Text("─" * width, style="muted"),
            compose,
        ]
        if self._selected is not None:
            footer_parts.append(self._pick_banner())
        if self._status:
            footer_parts.append(Text(self._status, style="muted"))
        lines += render_lines(Group(*footer_parts), width)

        # Stay at the bottom: give the frame an offset that is too large, so that it clamps
        # the viewport to the last lines (input and latest messages). A scroll up clears
        # the stick.
        if self._stick:
            self.scroll = len(lines)
        elif self._selected_end is not None:
            # The selection keeps its *full* message visible, with the codes under its text.
            # The frame keeps only the one line that cursor_line names, the head of the
            # message. With only that line, a selection that moves down stops the head on
            # the bottom row, and the codes are below the viewport. Scroll far enough to
            # show the last row of the message (one extra line for the day divider pinned
            # at the top). The frame then moves the viewport up again only if that pushes
            # the head off the top.
            self.scroll = max(self.scroll, self._selected_end - self._scroll_viewport + 1)
        return lines

    def cursor_line(self) -> int | None:
        """Keep the selected reply target visible.

        When nothing is selected, the scroll is free (the stick controls it).
        """
        return self._selected_line

    #: The text of an empty transcript.
    _empty_text = "No messages yet — say hello!"

    @property
    def _replies(self) -> bool:
        """Whether Enter on a selected message replies to it (or else opens its paths).

        This is true where there are many voices and an @mention says which one gets the
        answer, and only while there is a compose line for the reply.
        """
        return self._multiparty and self._composing

    def _compose_line(self, width: int) -> RenderableType:
        """The compose line under the transcript: the input, with its byte limit.

        The byte counter is pinned to the right edge of the *last* line of the input. Thus,
        when the text wraps onto a second line, the counter stays in the bottom-right
        corner and does not follow the cursor down the wrap. Only when the last line is
        already full to the edge does the counter go onto its own right-aligned line.
        """
        limit = self._byte_limit()
        input_line = self._editor.render(overflow_at=self._overflow_at(limit))
        return right_aligned_tail(input_line, self._byte_counter(limit), width)

    # --- outgoing byte limit -------------------------------------------------

    def _byte_limit(self) -> int:
        """The UTF-8 byte limit for a message in this conversation (channel or direct)."""
        return CHANNEL_BYTE_LIMIT if self._is_channel else DM_BYTE_LIMIT

    def _used_bytes(self) -> int:
        """The UTF-8 byte length of the current compose buffer: what counts against the limit."""
        return len(self._editor.text.encode("utf-8"))

    def _overflow_at(self, limit: int) -> int | None:
        """The index of the first compose character with bytes past ``limit``, or ``None``.

        The count goes by character (not by byte), so multibyte input stays complete: an
        emoji or an accented letter is fully under or fully over the line, never divided
        in the middle of its sequence.
        """
        total = 0
        for i, ch in enumerate(self._editor.text):
            total += len(ch.encode("utf-8"))
            if total > limit:
                return i
        return None

    def _byte_counter(self, limit: int) -> Text:
        """The inline ``used/limit`` counter: the shared compose gauge (:func:`byte_counter`)."""
        return byte_counter(self._used_bytes(), limit)

    def _name(self, peer: str | None) -> str | None:
        """Find the contact name for a sender key prefix (exact match, then prefix match)."""
        if not peer:
            return None
        needle = peer.lower()
        if needle in self._names:
            return self._names[needle]
        for prefix, name in self._names.items():
            if prefix.startswith(needle) or needle.startswith(prefix):
                return name
        return None

    # --- grouped rendering ---------------------------------------------------

    def _render_grouped(self, width: int) -> list[str]:
        """Render the transcript to ANSI lines, and keep the row of the selected message.

        This serves direct threads and channel threads. Consecutive messages from the same
        sender on the same day share one coloured header, with each message body indented
        below it. A divider marks each new day, and a blank line separates two different
        sender groups. The render goes one message at a time (not as one Group). Thus we
        can store the body line of the selected message in :attr:`_selected_line`, so that
        the frame can scroll it into the viewport. The user can select in the two types of
        thread (refer to :meth:`handle`).

        Things that change nothing here cause a paint all the time: the idle tick, a key
        press, the ack spinner. Thus this method caches the rendered lines of each message
        in :attr:`_render_cache`, and for each message it puts in the cached slice instead
        of a new render. A message renders again only when this is necessary:

        * it still waits for its ack (the spinner draws its last glyph again at each
          tick),
        * it is the selected message (the selection sets its style, not the message), or
        * its identity or ``acked`` is different from the cache (it was appended, it was
          replaced in place when a pending bubble resolved, or it was removed).

        Most importantly, this works for each message. It is not a cached prefix that
        stops at the first such row. Thus a message stays cheap to draw again, however far
        back it is in a long transcript. With a prefix that stops, a page up through the
        history causes a full render again at each key press. With this method, it does
        not. The grouping context (day and sender headers) is cheap to calculate again in
        all cases. Thus it is calculated new each time, and never taken from the cache.
        """
        messages = self._messages
        total = len(messages)
        cache = self._render_cache
        if cache is not None and cache["width"] == width:
            cached_ids = cache["ids"]
            cached_acked = cache["acked"]
            cached_selected = cache["selected"]
            cached_boundaries = cache["boundaries"]
            cached_lines = cache["lines"]
            cache_count = len(cached_ids)
        else:
            cached_ids = cached_acked = cached_selected = cached_boundaries = ()
            cached_lines = []
            cache_count = 0

        lines: list[str] = []
        # Store each day divider as a sticky block of one row. Thus, when the divider of
        # the top visible message scrolls off, it is pinned to the top row again. This is
        # the same base Screen.sticky_block that the conversation picker uses for its
        # section headings. A day has nothing to say except its date, so the block is only
        # the divider. The heading of a select list can also have its description.
        self._sticky_headers = []
        ids: list[int] = []
        ackeds: list[bool | None] = []
        selecteds: list[bool] = []
        boundaries: list[int] = []
        prev_group: tuple[bool, str] | None = None
        prev_day = None

        for idx in range(total):
            message = messages[idx]
            stamp = message.created_at.astimezone()
            day = stamp.date()
            sender, body = self._sender_and_body(message)
            # Group by (are-we-the-sender, display name), not by the name alone. Thus our
            # messages never join with those of a remote sender that has the same name.
            group = (message.outbound, sender)
            new_day = day != prev_day
            selected = idx == self._selected
            pending = message.acked is None and message.outbound and not message.is_channel
            reusable = (
                not selected
                and not pending
                and idx < cache_count
                and not cached_selected[idx]
                and cached_ids[idx] == id(message)
                and cached_acked[idx] == message.acked
            )
            if reusable:
                start = cached_boundaries[idx - 1] if idx > 0 else 0
                slice_lines = cached_lines[start : cached_boundaries[idx]]
                if new_day:
                    self._sticky_headers.append((len(lines), [slice_lines[0]]))
                lines += slice_lines
            else:
                if new_day:
                    if lines:
                        lines += render_lines(Text(""), width)
                    # A day divider is the section heading of this transcript: it pins to the
                    # top row while its day scrolls under it, and ^PgUp/^PgDn step by it.
                    # Thus it has the heading style of the app (``── Label ──`` in the bold
                    # ``heading`` grey, refer to menus.section_heading). The rule above the
                    # compose line is plain ``muted``, because that rule is chrome.
                    divider = render_lines(
                        Text(f"── {stamp:%a} {stamp:%b} {stamp.day} ──", style="heading"), width
                    )
                    self._sticky_headers.append((len(lines), [divider[0]]))
                    lines += divider
                if new_day or group != prev_group:
                    if not new_day and lines:
                        lines += render_lines(Text(""), width)  # gap between sender groups
                    header = self._group_header(sender, message)
                    lines += render_lines(header, width)
                if selected:
                    # The first message has the head of the transcript: nothing comes before
                    # its day divider. Thus its selection anchors at line 0, not at its own
                    # body. When the anchor was on the body, the viewport at the very top
                    # was scrolled down two lines: the divider was only pinned, the sender
                    # chip was not visible, and the ↑ of the title bar was lit over a
                    # transcript with nothing above it.
                    self._selected_line = 0 if idx == 0 else len(lines)
                lines += self._body_lines(body, message, width, selected=selected)
                if selected:
                    self._selected_end = len(lines)
            prev_group, prev_day = group, day
            ids.append(id(message))
            ackeds.append(message.acked)
            selecteds.append(selected)
            boundaries.append(len(lines))

        self._render_cache = {
            "width": width,
            "ids": ids,
            "acked": ackeds,
            "selected": selecteds,
            "lines": lines,
            "boundaries": boundaries,
        }
        return lines

    def _sender_and_body(self, message: ChatMessage) -> tuple[str, str]:
        """Return the display sender and the clean body of a message.

        Our messages are ``you``. An inbound channel message has a ``Name: `` prefix, which
        we move into the sender (``·`` when there is no prefix). An inbound direct message
        gets the sender from the resolved contact name (or the raw key, or ``?``).
        """
        if message.outbound:
            return "you", message.text
        if message.is_channel:
            name, body = split_channel_sender(message.text)
            return (name or "·"), body
        return (self._name(message.peer) or message.peer or "?"), message.text

    def _group_header(self, sender: str, message: ChatMessage) -> Text:
        """Build the sender header that starts a group: the name, drawn as a chip.

        The chip makes a *label* different from the prose under it. A name alone above its
        messages then reads as a tag, not as a coloured word that a person typed. It is the
        chip of a path line (refer to :func:`~meshterm.ui.widgets.name_chip`). Thus a
        sender looks exactly like that node as a hop in a route, also for our messages:
        the ``★``, never our name.

        Only this name is in a chip. An ``@mention`` in a body (refer to
        :meth:`_render_mentions`) is a part of what the sender said. A chip for it puts a
        chip in the middle of a sentence.
        """
        if message.outbound:
            return name_chip("", you=True)
        # ``·`` is a channel line that arrived with no sender prefix: there is no key to use.
        return name_chip(sender, None if sender == "·" else self._header_key(sender, message))

    def _header_key(self, sender: str, message: ChatMessage) -> str | None:
        """The key that gives the colour of the sender chip of a group: the key of the name.

        This is a hook, not a call, because a room knows more than a name. Each post has the
        key prefix of its author, which gives the colour of the chip with no name lookup
        (refer to :class:`~meshterm.ui.room.RoomScreen`).
        """
        return self._sender_key(sender)

    def _body_lines(
        self, body: str, message: ChatMessage, width: int, *, selected: bool = False
    ) -> list[str]:
        """Render one message to ANSI lines, with its own time and a hanging indent.

        The timestamp of each message is here (not on the group header), so that each
        message shows the time when it was sent, also when one sender has many messages in
        a group. Without this, a run of messages from the same sender seems to share one
        time. A long body wraps with a hanging indent, so that the next lines align under
        the body and not under the timestamp gutter. When ``selected`` is true, the line
        is marked as the reply target (the same as the ``❯`` pointer and the highlight of
        the select screen).

        A URL is never cut where the screen can hold it. A URL that is too long for the
        body lane moves out to the column of the timestamp, on its own line (refer to
        :func:`~meshterm.ui.tui.render.render_hanging`). This is because a terminal opens
        a link, and a user copies one, only while it is on one line.

        Where the platform draws them (:data:`_URL_CODES`, the PicoCalc), each URL in the
        body gets a QR code under it. The codes are side by side, at the indent of the
        body. Thus the user can open a link from the handheld on a phone, and does not
        have to type it.
        """
        stamp = message.created_at.astimezone()
        prefix = Text()
        prefix.append("❯ " if selected else "  ", style="cursor" if selected else None)
        prefix.append(f"{stamp:%H:%M}  ", style="cursor" if selected else "muted")
        body_text = self._body_text(body, message, selected=selected)
        whole = [m.span() for m in _WORD.finditer(body_text.plain) if links.urls(m.group())]
        lines = render_hanging(
            prefix, body_text, width, indent=prefix.cell_len, whole=whole, gutter=_URL_GUTTER
        )
        urls = links.urls(body) if _URL_CODES else []
        if urls:
            lines += render_lines(qr_strip(urls, width, indent=prefix.cell_len), width)
        return lines

    def _body_text(self, body: str, message: ChatMessage, *, selected: bool) -> Text:
        """Build the styled body of a message: mentions in colour, then the glyphs at the end."""
        text = self._render_mentions(body, selected=selected)
        if message.outbound:
            # A direct message shows its own delivery (spinner → ✓/✗, and a retry is
            # possible). A channel broadcast has no ack. Thus mark only a channel message
            # that did not leave the companion.
            if message.is_channel:
                if message.acked is False:
                    text.append("  ⚠ no ack", style="warn")
                text.append_text(self._scope_note(message))
            else:
                text.append("  ")
                text.append_text(self._delivery_glyph(message.acked))
        if message.snr is not None:
            text.append(f"  {message.snr:+.0f} dB", style=snr_style(message.snr))
        return text

    def _scope_note(self, message: ChatMessage) -> Text:
        """A muted note after a sent message that went out under a scope not in the title.

        This note shows only in a channel whose title names a scope, and only for the
        exceptions to that scope: an unscoped resend, or a message sent before the scope
        was set or changed. On each other line, a note only repeats the title. In
        a channel with no scope of its own, the default scope of its messages belongs to
        the device. It is not news to show on each line.
        """
        if not self._scope or not message.scope or message.scope == self._scope:
            return Text()
        if message.scope == WILDCARD:
            return Text("  · unscoped", style="muted")
        return Text(f"  · scope {message.scope}", style="muted")

    def _render_mentions(self, body: str, *, selected: bool) -> Text:
        """Render body text, and change each ``@[Name]`` token to a ``@Name`` in its hue.

        The reply flow puts an ``@[Name]`` token in the compose line (refer to
        :meth:`_begin_reply`). Here it shows as a bare ``@Name`` in the stable hue of that
        sender. Thus the user sees that a mention belongs to the person that it names. The
        text around the mentions keeps the base style of the line (``cursor`` when the
        message is the selected reply target, or else no style).
        """
        base = "cursor" if selected else None
        text = Text()
        pos = 0
        for match in MENTION.finditer(body):
            if match.start() > pos:
                text.append(body[pos : match.start()], style=base)
            name = match.group(1)
            text.append(f"@{name}", style=self._sender_style(name, mention=True))
            pos = match.end()
        if pos < len(body):
            text.append(body[pos:], style=base)
        return text

    def _delivery_glyph(self, acked: bool | None) -> Text:
        """Map the ``acked`` state of an outbound direct message to the mark at its end.

        A message that still waits for its ack (``acked is None``) shows the current
        animation step of the spinner. :meth:`_spin_while` animates it while the send is in
        flight. A resolved message shows the delivered ``✓`` (ok) or the unacknowledged
        ``✗`` (err).
        """
        if acked is None:
            return self._spinner.text()
        glyph, style = _DELIVERED if acked else _FAILED
        return Text(glyph, style=style)

    def _pick_banner(self) -> Text:
        """A one-line note above the input while a message is selected.

        In a channel, it starts with the reply (what Enter does there). In a direct chat,
        it starts with the paths view (what Enter does there). The two say what the
        selection is for. Thus the user never sees a highlight that has no explanation.
        """
        message = self._messages[self._selected]
        sender, _ = self._sender_and_body(message)
        who = "this message" if message.outbound or sender == "·" else sender
        if self._replies:
            return Text(
                f"↩ Enter to reply to {who} with an @mention · End to cancel",
                style="accent",
            )
        return Text("Enter to see the paths this message took · End to cancel", style="accent")

    def _sender_style(self, sender: str, *, is_self: bool = False, mention: bool = False) -> str:
        """Get a stable colour for a sender name, from the node key of the sender.

        Our messages are white. This comes from ``is_self`` (the message is outbound), not
        from the ``"you"`` label. Thus a remote sender with the name ``you`` still gets a
        hue from the palette, and does not look like our node. ``·`` (unknown) takes
        ``node.unknown``. Each other sender name is resolved to a key (the contacts, then
        the names that the recorder stored, and in a direct thread the *sender label*
        falls back to the key of the peer), and takes the hue of that key. A name that no
        known node has also stays ``node.unknown``: the rule of the app is that colour
        marks an identity that has a key.

        Each grey here is the *node* grey, never ``muted``. An unidentified sender is
        content that you can still do actions on (select the message, open its paths),
        not chrome. The two greys must not follow each other: on the console, ``muted`` is
        the dark slot and ``node.unknown`` is the light one (JP, 2026-08-12).

        ``mention=True`` gives the style of an ``@mention`` in the body, not of a sender
        label. A mention names any person, so the fallback to the key of the peer must
        not apply. Thus a mention that is not resolved stays grey in a direct chat,
        exactly as it does in a channel.
        """
        if is_self:
            return "you"  # white, outside the range of sender hues, so always easy to see
        if sender == "·":
            return "node.unknown"
        return name_style(sender, self._sender_key(sender, mention=mention))

    def _sender_key(self, sender: str, *, mention: bool = False) -> str | None:
        """The key of a sender name, or ``None`` when nothing gives a key for it.

        The contacts first, then the names that the recorder stored. In a *direct* thread,
        a sender label with no key falls back to the key of the peer, because the only two
        persons in the conversation are we and the peer. A mention names any person, so
        that fallback must not apply to a mention.

        The colour (:meth:`_sender_style`) and the chip (:meth:`_group_header`) share this
        method. Thus the two cannot disagree about the identity of a name.
        """
        key = self._key_of(sender)
        if not key and not self._multiparty and not mention:
            key = self._peer_key or None
        return key

    # --- input ---------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Dispatch a key: select messages and do actions on them, edit the compose line, or leave.

        The two types of chat share one model. When no message is selected, Enter sends,
        and typing edits the compose line. An amber dialog asks the user to confirm a paste
        (Ctrl-V, or the bracketed paste of a terminal) before the paste goes into the line
        (refer to :meth:`_begin_paste`). ↑ selects the newest message. The selection then
        moves with ↑↓, PgUp/PgDn (one screen), Ctrl+Home (the very first message), and
        Ctrl+PgUp/PgDn (day dividers), and the viewport moves with it. Enter on a selected
        message starts a reply with an ``@mention`` in a channel, and opens the delivery
        paths in a direct chat. In the two types of chat, ^P opens the paths of the
        selected message, and ^U the QR codes of its links. The two do nothing when no
        message is selected. ^End (or a move past the newest message) goes back to the
        compose line. The first Esc clears the selection, and the second Esc leaves the
        screen.
        """
        if action == "enter":
            if self._selected is not None:
                if self._replies:
                    self._begin_reply()
                else:
                    self._open_paths(self._selected)
            else:
                self._submit()
        elif action == "paste":
            self._begin_paste(data)
        elif action == "paths":
            self._open_paths(self._selected)
        elif action == "url_code":
            self._open_codes()
        elif action == "retry":
            if self._is_channel:
                self._begin_resend_unscoped()
            else:
                self._retry()
        elif action == "escape":
            if self._selected is not None:
                self._clear_selection()  # the first Esc clears the selection, the next leaves
                self._session.invalidate()
            else:
                self.resolve(CANCEL)
        elif action == "up":
            self._move_selection(-1)
        elif action == "pageup":
            self._move_selection(-self._page_step)
        elif action == "down":
            self._move_selection(1)
        elif action == "pagedown":
            self._move_selection(self._page_step)
        elif action == "ctrl_home":
            if self._messages:
                self._selected = 0
                self._stick = False
        elif action == "ctrl_pageup":
            self._select_section(-1)
        elif action == "ctrl_pagedown":
            self._select_section(1)
        elif action == "ctrl_end":
            self._clear_selection()
            self._stick = True  # jump back to the live tail and the compose line
        else:
            if self._composing and self._editor.edit(action, data):
                # An edit of the compose line puts the focus there. Nothing stays selected.
                self._status = ""  # an edit clears the "too long" notice
                self._clear_selection()
                self._stick = True

    def _open_paths(self, index: int | None) -> None:
        """Float the delivery-paths view for the selected message (one dialog at a time).

        ``index`` is the selection. Thus ``None`` (nothing selected) opens nothing. A path
        is a route that one *specific* message walked. When the code guessed the latest
        message, it showed the paths of the message that was at the bottom of the
        transcript at that time.
        """
        if index is None or not self._messages or self._paths is None or self._paths_open:
            return
        message = self._messages[max(0, min(index, len(self._messages) - 1))]
        self._paths_open = True

        async def run() -> None:
            try:
                await self._paths(message)
            finally:
                self._paths_open = False
                self._session.invalidate()

        self._session.run_detached(run())

    def _picked_urls(self) -> list[str]:
        """The URLs in the body of the selected message, in order. None if nothing is selected."""
        if self._selected is None or not self._messages:
            return []
        message = self._messages[max(0, min(self._selected, len(self._messages) - 1))]
        return links.urls(self._sender_and_body(message)[1])

    def _open_codes(self) -> None:
        """Show the links of the selected message as QR codes on the share screen (^U).

        This is the share screen of a channel or of a contact card
        (:class:`~meshterm.ui.qr.QrScreen`), full-frame. Thus a phone can read a link from
        here and open it. Many links are on one screen that ←→ step through, in the order of
        the message. The same as ^P, it acts on the *selection*: a link belongs to one
        message, and when nothing is selected, the screen cannot know which message. One
        share screen at a time.
        """
        urls = self._picked_urls()
        if not urls or self._codes_open:
            return
        self._codes_open = True

        async def run() -> None:
            try:
                await self._session.run_screen(QrScreen(*urls, title="Links"))
            finally:
                self._codes_open = False
                self._session.invalidate()

        self._session.run_detached(run())

    def _begin_paste(self, data: str) -> None:
        """Confirm a clipboard paste on an amber dialog, then insert it into the compose line.

        A chat message goes out over the air. A paste can be much larger than a key press,
        or it can have content that the user did not want to broadcast. Thus a paste must
        go through a Cancel/Paste confirm (the amber ``danger`` tier: disruptive, not a loss
        of data), and does not go directly into the line. Newlines and control characters
        become spaces (a message is one line). An empty result never opens the dialog. One
        paste dialog at a time, scheduled from the key handler, the same as the paths view.
        """
        if self._paste_open or not self._composing:
            return
        clean = "".join(ch if ch.isprintable() else " " for ch in data).strip()
        if not clean:
            return
        self._paste_open = True
        count = len(clean)

        async def run() -> None:
            try:
                confirmed = await self._session.button_dialog(
                    Text(
                        f"Paste {count} character{'s' if count != 1 else ''} into your message?",
                        style="warn",
                    ),
                    [("Cancel", False), ("Paste", True)],
                    title="Paste",
                    default=1,
                    border_style="warn",
                    footer_hint="←→ choose · Enter select · Esc cancel",
                )
            finally:
                self._paste_open = False
            if confirmed:
                # Put it in the compose line: clear the selection, go back to the tail, and
                # insert the run through the shared editor. If the result is over the limit,
                # the byte gauge marks it, exactly as it marks typing past the limit.
                self._clear_selection()
                self._stick = True
                self._editor.edit("text", clean)
                self._status = ""
            self._session.invalidate()

        self._spawn(run())

    def _move_selection(self, delta: int) -> None:
        """Move the reply selection by ``delta`` messages (negative = toward older).

        The selection starts from the compose line only on a move up (``delta < 0``). A
        move past the newest message clears the selection and stays at the tail again.
        """
        if not self._messages:
            return
        last = len(self._messages) - 1
        if self._selected is None:
            if delta < 0:
                self._selected = last
                self._stick = False
            return
        target = self._selected + delta
        if target > last:
            self._clear_selection()
            self._stick = True
        else:
            self._selected = max(0, target)
            self._stick = False

    def _select_section(self, direction: int) -> None:
        """Move the reply selection to the first message of the previous or next day.

        This is the day jump of the transcript (Ctrl+PageUp/PageDown), for the selection.
        Down moves to the first message of the next day (or to the tail when there is no
        later day). Up moves to the first message of the current day, or to the first
        message of the previous day when the selection is already on a first message.
        """
        if not self._messages:
            return
        starts = self._day_start_indices()
        current = self._selected if self._selected is not None else len(self._messages) - 1
        if direction > 0:
            target = next((s for s in starts if s > current), None)
            if target is None:
                self._clear_selection()
                self._stick = True
                return
        else:
            governing = max((s for s in starts if s <= current), default=0)
            target = (
                governing
                if governing < current
                else max((s for s in starts if s < current), default=0)
            )
        self._selected = target
        self._stick = False

    def _day_start_indices(self) -> list[int]:
        """The indexes of the messages that start a new local day: the transcript dividers."""
        starts: list[int] = []
        prev_day = None
        for i, message in enumerate(self._messages):
            day = message.created_at.astimezone().date()
            if day != prev_day:
                starts.append(i)
                prev_day = day
        return starts

    def _clear_selection(self) -> None:
        """Clear the reply selection (the focus goes back to the compose line)."""
        self._selected = None
        self._selected_line = None

    def _begin_reply(self) -> None:
        """Start the compose line with an ``@mention`` of the sender of the selected message."""
        message = self._messages[self._selected]
        sender, _ = self._sender_and_body(message)
        named = not message.outbound and sender != "·"
        mention = f"@[{sender}] " if named else "@"
        self._editor = LineEditor(mention + self._editor.text)
        self._clear_selection()
        self._stick = True
        self._session.invalidate()

    def _submit(self) -> None:
        """Send the current input line as a message (scheduled from the key handler).

        A line over the limit is refused. The buffer stays the same (so that the user can
        make it shorter), and the status shows the overage, the same as the red overflow
        that the compose bar already shows.
        """
        text = self._editor.text.strip()
        if not text or self._sending:
            return
        limit = self._byte_limit()
        over = self._used_bytes() - limit
        if over > 0:
            self._status = f"Too long by {over} byte{'s' if over != 1 else ''} — trim to send."
            self._session.invalidate()
            return
        self._editor = LineEditor()
        self._sending = True
        self._stick = True
        if self._is_channel:
            self._status = "sending…"
            self._session.invalidate()
            self._spawn(self._send_channel(text))
        else:
            # A direct chat shows an optimistic bubble. The mark at its end shows the
            # delivery: a spinner now, then ✓/✗ when the ack resolves (or times out).
            # acked=None ⇒ the spinner still turns.
            pending = ChatMessage(text=text, outbound=True, created_at=utcnow())
            self._messages.append(pending)
            self._status = ""
            self._session.invalidate()
            self._spawn(self._send_direct(pending))

    async def _send_channel(self, text: str) -> None:
        """Broadcast a channel message, and append it when the companion accepts it."""
        try:
            message = await self._send(text)
            if message is not None:
                self._messages.append(message)
            self._status = ""
        except Exception as exc:  # noqa: BLE001 - show the error inline, keep the chat open
            self._status = f"send failed: {exc}"
        finally:
            self._sending = False
            self._stick = True
            self._session.invalidate()

    async def _spin_while(self, coro: Awaitable[Any]) -> Any:
        """Await ``coro`` while the delivery spinner animates on the message in flight.

        A background timer moves the shared spinner forward and paints every
        :func:`~meshterm.ui.tui.spinner.spinner_interval` seconds (the spin rate of the
        platform). Thus the glyph at the end of a pending message spins until the ack
        resolves. Before the method returns, it always cancels the timer (and awaits it, so
        that it cannot stay after the send as a stray pending task). The spinner is only
        for the look. Thus the method ignores each problem in the animation, so that the
        problem cannot break the send.
        """
        self._spinner.reset()

        async def animate() -> None:
            while True:
                await asyncio.sleep(spinner_interval())
                self._spinner.tick()
                self._session.invalidate()

        ticker = self._spawn(animate())
        try:
            return await coro
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a spinner problem must never break a send
                pass

    async def _send_direct(self, pending: ChatMessage) -> None:
        """Await the delivery of the optimistic ``pending`` bubble, then put in the stored row.

        On success, the stored message replaces the pending bubble. The stored message has
        its resolved ``acked`` state and its row id, so that the user can retry a ✗ later
        in place. A hard failure (the companion refuses the send) removes the bubble and
        shows the error inline, the same as a failed send always did.
        """
        try:
            message = await self._spin_while(self._send(pending.text))
        except Exception as exc:  # noqa: BLE001 - show the error inline, keep the chat open
            self._discard(pending)
            self._status = f"send failed: {exc}"
        else:
            if message is not None:
                self._swap(pending, message)
            else:
                self._discard(pending)
            self._status = ""
        finally:
            self._sending = False
            self._stick = True
            self._session.invalidate()

    def _retry_target(self) -> ChatMessage | None:
        """The message that ^R sends again, or ``None`` when there is nothing to retry.

        In a direct chat, this is the newest outbound message that went out and was never
        acknowledged. In a channel, nothing is ever acknowledged. There, it is a message
        that *we* sent under a region, and ^R sends it again unscoped: the **selected**
        message when a message is selected (the user pointed at it), or else the newest
        message that we sent. With a selection, only the selection counts. If the selected
        message is not a scoped message of ours, ^R offers nothing, and does not go past it
        to another message without a sign. With no selection, only the newest counts.
        When its unscoped copy has gone, that copy is the newest, and nothing remains to
        offer. In the two cases, this is true only while a resend can start: no send is
        already in flight, and a resend path is connected.
        """
        if self._is_channel:
            if self._sending or self._resend_unscoped is None:
                return None
            if self._selected is not None:
                candidate: ChatMessage | None = self._messages[self._selected]
            else:
                candidate = next((m for m in reversed(self._messages) if m.outbound), None)
            if (
                candidate is None
                or not candidate.outbound
                or not candidate.scope
                or candidate.scope == WILDCARD
            ):
                return None
            return candidate
        if self._sending or self._resend is None:
            return None
        return next(
            (m for m in reversed(self._messages) if m.outbound and m.acked is False),
            None,
        )

    def _retry(self) -> None:
        """Try again to deliver the most recent unacknowledged direct message (Ctrl-R)."""
        target = self._retry_target()
        if target is None:
            return
        self._sending = True
        target.acked = None  # back to the spinner while the retry is in flight
        self._status = "retrying…"
        self._stick = True
        self._session.invalidate()
        self._spawn(self._resend_message(target))

    def _begin_resend_unscoped(self) -> None:
        """Confirm, then send a scoped channel message again, unscoped (^R).

        The dialog is amber (``danger``). Nothing is lost, but each repeater that permits
        an unscoped flood relays it: all the mesh that the scope kept the message out of.
        Thus it is a choice made on purpose, and the dialog names the region that the
        message leaves. The resend is a new message on the air (a channel message has no
        identity to deliver again), so it is appended as a new message. A radio that
        cannot send unscoped refuses before anything goes out, and the status line says
        why.
        """
        target = self._retry_target()
        if target is None or self._resend_open:
            return
        self._resend_open = True
        preview = target.text if len(target.text) <= 32 else target.text[:31] + "…"

        async def run() -> None:
            try:
                confirmed = await self._session.button_dialog(
                    Text(
                        f"Send “{preview}” again, unscoped? Every repeater that relays "
                        f"unscoped floods will carry it, not only those in {target.scope}.",
                        style="warn",
                    ),
                    [("Cancel", False), ("Resend", True)],
                    title="Resend unscoped",
                    default=1,
                    border_style="warn",
                    footer_hint="←→ choose · Enter select · Esc cancel",
                )
            finally:
                self._resend_open = False
            if not confirmed or self._sending or self._resend_unscoped is None:
                self._session.invalidate()
                return
            self._sending = True
            self._status = "resending unscoped…"
            # The resend goes to the tail as a new message. The selection that chose it is
            # done.
            self._clear_selection()
            self._stick = True
            self._session.invalidate()
            try:
                message = await self._resend_unscoped(target)
                if message is not None:
                    self._messages.append(message)
                self._status = ""
            except Exception as exc:  # noqa: BLE001 - show the error inline, keep the chat open
                self._status = f"resend failed: {exc}"
            finally:
                self._sending = False
                self._stick = True
                self._session.invalidate()

        self._spawn(run())

    async def _resend_message(self, message: ChatMessage) -> None:
        """Run a retry to its end, and refresh the delivery state of the message in place."""
        assert self._resend is not None
        try:
            await self._spin_while(self._resend(message))
        except Exception as exc:  # noqa: BLE001 - show the error inline, keep the chat open
            message.acked = False
            self._status = f"retry failed: {exc}"
        else:
            self._status = "" if message.acked else "still no ack — ^R to retry"
        finally:
            self._sending = False
            self._stick = True
            self._session.invalidate()

    def _swap(self, old: ChatMessage, new: ChatMessage) -> None:
        """Replace an optimistic bubble with its stored message, in place."""
        try:
            self._messages[self._messages.index(old)] = new
        except ValueError:  # pragma: no cover - the bubble is always still there
            self._messages.append(new)

    def _discard(self, message: ChatMessage) -> None:
        """Remove an optimistic bubble that never became a real message."""
        try:
            self._messages.remove(message)
        except ValueError:  # pragma: no cover - a defensive check
            pass


async def open_chat(ctx: AppContext, conversation: Conversation) -> int:
    """Open the live chat screen for ``conversation`` and run it until the user closes it.

    This function makes sure that the listener and the message recorder run, reads the
    stored transcript, subscribes to the hub so that inbound messages for this thread are
    appended live, and pushes the screen. At exit, it always removes the subscription and
    the marker of the active conversation.

    Args:
        ctx: The shared application context (it must run the interactive TUI surface).
        conversation: The channel or contact conversation to open.

    Returns:
        The number of messages in the transcript when the screen closed.

    Raises:
        RuntimeError: If the call is not from the interactive menu (no full-screen
            session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - the caller is only in the menu
        raise RuntimeError("live chat is only available in the interactive menu")
    if conversation.is_room:
        from .room import open_room

        return await open_room(ctx, conversation)
    session = ctx.ui.session

    device = await ctx.device()
    try:
        await ctx.chat.start()  # start to store inbound messages, if it did not already
    except Exception:  # noqa: BLE001 - the hub may already run. The recorder is best-effort
        pass

    from ..services import trace_runner

    history = ctx.repo.recent_chat_messages(
        is_channel=conversation.is_channel,
        channel_id=conversation.channel_id,
        peer=conversation.peer,
        limit=ctx.preferences.chat_history_limit,
    )
    contacts = await ctx.devstate.contacts()
    names = _contact_names(contacts)
    key_of = trace_runner.make_name_key_resolver(contacts, ctx.repo.node_names())

    async def send(text: str) -> ChatMessage | None:
        if conversation.is_channel:
            assert conversation.channel_idx is not None
            return await ctx.chat.send_channel(
                conversation.channel_idx, text, label=conversation.label
            )
        assert conversation.contact is not None
        return await _with_restore(ctx, lambda: ctx.chat.send_direct(conversation.contact, text))

    resend: Callable[[ChatMessage], Awaitable[ChatMessage]] | None = None
    resend_unscoped: Callable[[ChatMessage], Awaitable[ChatMessage | None]] | None = None
    scope: str | None = None
    if conversation.is_channel:
        scope = ctx.chat.channel_scope(conversation.channel_id)

        async def resend_unscoped(message: ChatMessage) -> ChatMessage | None:
            assert conversation.channel_idx is not None
            return await ctx.chat.send_channel(
                conversation.channel_idx, message.text, label=conversation.label, scope=WILDCARD
            )

    else:

        async def resend(message: ChatMessage) -> ChatMessage:
            assert conversation.contact is not None
            return await _with_restore(
                ctx, lambda: ctx.chat.resend_direct(conversation.contact, message)
            )

    paths = await _make_paths_presenter(ctx, conversation, device)
    screen = ChatScreen(
        conversation,
        history,
        send=send,
        names=names,
        session=session,
        resend=resend,
        paths=paths,
        key_of=key_of,
        scope=scope,
        resend_unscoped=resend_unscoped,
    )
    ctx.chat.set_active(conversation.key)

    def on_event(event: MeshEvent) -> None:
        message = event.message
        if message is not None and _belongs(message, conversation):
            peer_name = names.get((message.sender or "").lower())
            screen.append(
                ChatMessage.from_message(
                    message, peer_name=peer_name, channel_id=conversation.channel_id
                )
            )

    unsubscribe = ctx.events.subscribe(on_event, EventKind.MESSAGE)
    try:
        await session.run_screen(screen)
    finally:
        unsubscribe()
        ctx.chat.set_active(None)
    return len(screen._messages)


async def _with_restore(ctx: AppContext, attempt: Callable[[], Awaitable[Any]]) -> Any:
    """Run a direct send. If the device forgot the contact, offer to restore it, then retry.

    This is the one rejection from which a send can recover: the firmware has no contact
    for the recipient (refer to :class:`~meshterm.core.connection.ContactNotOnDeviceError`),
    and one write corrects it. The user is asked first (:func:`_restore_contact`). If the
    user declines, the error is raised again. Thus the chat shows the refusal exactly as it
    shows each other failed send.

    Args:
        ctx: The shared application context.
        attempt: The send to run. It is called a second time, with no change, when the
            contact is back.

    Returns:
        The return value of ``attempt``.

    Raises:
        Exception: Each error that ``attempt`` raises. A rejection for a forgotten contact
            is raised again only when the user declined the offer to add it back.
    """
    try:
        return await attempt()
    except ContactNotOnDeviceError as missing:
        if not await _restore_contact(ctx, missing):
            raise
        return await attempt()


async def _restore_contact(ctx: AppContext, missing: ContactNotOnDeviceError) -> bool:
    """Offer to write a forgotten contact back to the device. ``True`` if it now has it.

    A direct message is addressed through the contact entry of the *device*. Thus MeshTerm
    cannot send a message to a contact that the firmware removed, also when MeshTerm still
    lists it. (The list on a screen is the union of the table of the device and the
    contacts that MeshTerm keeps for it: refer to :mod:`meshterm.core.contact_store`.) The
    contact that we already have holds all the data that the entry must have. Thus one
    write is the repair, and the send that got the rejection tries again after it.

    The function offers the write and does not do it silently. The write changes what the
    device stores, and a full contact table refuses it (the user must see that, and it must
    not be hidden).

    Args:
        ctx: The shared application context.
        missing: The rejection, with the contact that the device could not find.

    Returns:
        ``True`` when the contact is on the device (retry the send). ``False`` if the user
        declined. Then the caller raises the error again, so that the chat shows the
        refusal.

    Raises:
        DeviceCommandError: If the device refused the write (most often, a full table).
    """
    contact = missing.contact
    add = await ctx.ui.dialog(
        f"{contact.name} isn't in this device's contacts, so the radio can't address it. "
        "Add it back and send?",
        [("Cancel", False), ("Add & send", True)],
        title="Contact not on device",
        default=1,
    )
    if not add:
        return False
    device = await ctx.device()
    await device.add_contact(contact)
    # The table of the device changed under the session cache. The next read gets it again
    # (with the route that the firmware learns for the contact from now on).
    ctx.devstate.invalidate_contacts()
    return True


def _contact_names(contacts: list[Contact]) -> dict[str, str]:
    """Build a map from key prefix to name, for the labels of inbound direct messages."""
    names: dict[str, str] = {}
    for contact in contacts:
        for key in (contact.key_prefix, contact.public_key[:12]):
            if key:
                names[key.lower()] = contact.name
    return names


def _belongs(message: Message, conversation: Conversation) -> bool:
    """Whether an inbound message belongs to the open conversation.

    Args:
        message: The received message.
        conversation: The conversation that the screen shows now.

    Returns:
        ``True`` if the message must be appended to this transcript.
    """
    if conversation.is_channel:
        return message.is_channel and message.channel == conversation.channel_idx
    if message.is_channel:
        return False
    sender = (message.sender or "").lower()
    peer = (conversation.peer or "").lower()
    if not sender or not peer:
        return False
    return sender.startswith(peer) or peer.startswith(sender)


# --- message paths (the ^P view) -----------------------------------------------------


def _sent_scope_line(ctx: AppContext, message: ChatMessage, *, relayed: bool) -> Text | None:
    """The scope of a message that we sent, for the paths dialog, and why it can be lost.

    The line comes from the scope stored on the message at send time
    (:attr:`ChatMessage.scope`), never from the current scope of the channel, which may
    have changed since then. A scoped message can have no stored relayed copy *and* no
    repeater that was ever heard here to relay its region
    (:meth:`~meshterm.core.region_store.RegionStore.carriers`). Then the line gives the
    most probable reason in plain words: only repeaters that relay its region relay a
    scoped flood, and no known repeater in range does.

    Args:
        ctx: The shared application context (for the region store).
        message: The message whose paths are open.
        relayed: Whether a copy of it was heard when it came back.

    Returns:
        The line, or ``None`` for a message with no stored scope (inbound, direct, or
        sent before MeshTerm stored scopes).
    """
    from ..core.regions import UNSCOPED, Scope
    from .widgets import scope_text

    if not message.outbound or not message.scope:
        return None
    line = Text("sent ", style="muted")
    if message.scope == WILDCARD:
        line.append_text(scope_text(UNSCOPED))
        return line
    line.append("under ", style="muted")
    line.append_text(scope_text(Scope("scoped", message.scope)))
    store = getattr(ctx, "region_store", None)
    carriers = len(store.carriers(message.scope)) if store is not None else 0
    if not relayed and not carriers:
        line.append(" — no repeater known here carries it", style="warn")
    elif carriers:
        line.append(
            f" · {carriers} known repeater{'s' if carriers != 1 else ''} carr"
            f"{'y' if carriers != 1 else 'ies'} it",
            style="muted",
        )
    return line


async def _make_paths_presenter(
    ctx: AppContext,
    conversation: Conversation,
    device,  # noqa: ANN001 - the core Device. Its type is set at the source
) -> Callable[[ChatMessage], Awaitable[None]]:
    """Build the async presenter for the ^P delivery-paths view of the chat.

    It collects what the presenter must have, one time each time the chat opens:

    * a resolver of hop names over the contacts and all the names that the recorder ever
      heard (the rule of the app is that a node that has a name never shows as a bare
      hash),
    * the name of our node, for the white ``you``,
    * the width of the routing prefix, for the lit hashes, and
    * for a channel, its secret. The function reads it from the device when the
      conversation did not have one (a conversation from the picker knows its identity,
      but not always its key).

    Args:
        ctx: The shared application context.
        conversation: The conversation that the chat screen opens.
        device: The connected device (the caller already awaited it).

    Returns:
        An async callable that shows the paths of one message in a floating window.
    """
    from ..services import trace_runner
    from ..services.message_paths import (
        channel_arrivals,
        collapse,
        direct_arrivals,
        distinct_paths,
        message_scope,
    )
    from .message_paths_screen import MessagePathsScreen

    session = ctx.ui.session
    contacts = await ctx.devstate.contacts()
    stored_names = ctx.repo.node_names()
    resolve = trace_runner.make_node_resolver(contacts, stored_names)
    type_of = trace_runner.make_node_type_resolver(contacts)
    key_of = trace_runner.make_name_key_resolver(contacts, stored_names)
    prefix_bytes = await ctx.devstate.routing_prefix_bytes()
    self_name: str | None = None
    self_key: str | None = None
    try:
        info = await ctx.devstate.self_info()
        self_name = str(info.get("name") or "") or None
        # Our key names one end of each direct packet in this conversation. The key of
        # the contact names the other end (refer to :func:`direct_arrivals`).
        self_key = str(info.get("public_key") or "") or None
    except Exception:  # noqa: BLE001 - our node with no name only gets no white highlight
        self_name = None

    secret = conversation.secret
    if conversation.is_channel and not secret and conversation.channel_idx is not None:
        try:
            payload = await device.get_channel(conversation.channel_idx)
            raw = (payload or {}).get("channel_secret")
            secret = bytes(raw) if raw else None
        except Exception:  # noqa: BLE001 - no key, no decrypt. The view says so correctly
            secret = None

    async def present(message: ChatMessage) -> None:
        if conversation.is_channel and secret:
            arrivals = channel_arrivals(
                ctx.repo, message, channel_name=conversation.label, secret=secret
            )
            matched = True
            summary = (
                f"heard {len(arrivals)} time{'s' if len(arrivals) != 1 else ''}"
                f" · {distinct_paths(arrivals)} distinct "
                f"path{'s' if distinct_paths(arrivals) != 1 else ''}"
                if arrivals
                else "no copies in the packet log"
            )
        elif conversation.is_channel:
            await session.scroll(
                Text(
                    "This channel's key isn't at hand, so overheard frames can't be "
                    "matched to the message.",
                    style="muted",
                ),
                title="Message paths",
            )
            return
        else:
            contact = conversation.contact
            peer_key = conversation.peer or (
                (contact.public_key or contact.key_prefix) if contact else None
            )
            arrivals, exact = direct_arrivals(
                ctx.repo, message, self_key=self_key, peer_key=peer_key
            )
            # An exact match says the same as the channel view: these packets are this
            # message. The shared MAC of the packets gives the match. Thus it is correct,
            # also when MeshTerm cannot read one word of them.
            matched = exact
            arrivals = collapse(arrivals)
            heard = sum(a.copies for a in arrivals)
            if exact:
                summary = (
                    f"heard {heard} time{'s' if heard != 1 else ''}"
                    f" · {len(arrivals)} distinct path{'s' if len(arrivals) != 1 else ''}"
                    if arrivals
                    else "no copies in the packet log"
                )
            else:
                summary = (
                    "direct frames are encrypted — matched by address and time"
                    if peer_key and self_key
                    else "direct frames are encrypted — matched by time alone"
                )
        # The two ends of the path: the node that the message started from, and the node
        # that it was addressed to. Our sends go from our node to the peer. An inbound
        # channel message names its sender on the air and is addressed to all nodes, so it
        # ends on our node. The origin of an inbound message in a direct chat is the peer of
        # the conversation.
        destination: str | None = self_name
        if message.outbound:
            source = self_name
            # A channel broadcast is addressed to no specific node, and the copies that we
            # log are its rebroadcasts that come back. Thus it really ends on our node. A
            # direct send does not: it went to one node, and its route ends there.
            if not conversation.is_channel:
                destination = conversation.label
        elif conversation.is_channel:
            source, _body = split_channel_sender(message.text)
        else:
            source = conversation.label
        await session.run_screen(
            MessagePathsScreen(
                message,
                arrivals,
                matched=matched,
                resolve=resolve,
                prefix_bytes=prefix_bytes,
                self_name=self_name,
                summary=summary,
                source=source or None,
                destination=destination or None,
                type_of=type_of,
                key_of=key_of,
                scope=message_scope(arrivals, ctx.region_store.scope_of),
                sent_scope=_sent_scope_line(ctx, message, relayed=bool(arrivals)),
            )
        )

    return present
