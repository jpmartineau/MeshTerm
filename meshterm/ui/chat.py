# SPDX-License-Identifier: Apache-2.0
"""The live chat screen and its launcher.

This is the interactive, full-screen chat experience: a scrolling transcript with an input
line pinned at the bottom, where messages you send and messages that arrive over the mesh
appear together in real time. Like the device picker and config editor, this module sits in
the UI layer but is allowed to depend on the context and services — it wires the
:class:`~meshterm.services.chat_service.ChatService` send path and the always-on event hub
to a :class:`~meshterm.ui.tui.screen.Screen`.

The screen subscribes to the hub for the duration it is open so inbound messages for the
current conversation append live; :class:`~meshterm.services.chat_service.ChatService`
independently persists every inbound message, so history is complete whether or not the
screen is open.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from rich.console import Group, RenderableType
from rich.text import Text

from ..core.channels import MENTION, split_channel_sender
from ..core.connection import ContactNotOnDeviceError
from ..core.events import EventKind, MeshEvent
from ..core.models import ChatMessage, Contact, Conversation, Message, utcnow
from ..core.regions import WILDCARD
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


#: Delivery-state marks for a *resolved* outbound direct message, shown at the end of its
#: line: acknowledged, or transmitted-but-unacknowledged (retryable via ^R). The app-wide
#: ``✓``/``✗`` status marks in their ok/err styles — not the ✅/❌ emoji, which belong to
#: the packet-class icon lane. While the ack is still pending the line shows an animated
#: spinner instead (see :meth:`ChatScreen._delivery_glyph`).
_DELIVERED = ("✓", "ok")
_FAILED = ("✗", "err")


class ChatScreen(Screen):
    """A live conversation: a scrolling transcript above a pinned input line.

    The transcript auto-sticks to the newest message (and snaps back to the bottom
    whenever you type or send). ↑ picks a message — the pick walks with ↑↓/PgUp/PgDn
    and carries the view with it; ^End (or Esc) returns focus to the compose line.
    Enter sends the current line, or acts on a picked message: in a channel it primes
    a reply ``@mention``, in a direct chat it opens the message's delivery paths. ^P
    opens the picked message's paths in either kind — a path belongs to one message, so
    with nothing picked there is nothing to show. Esc leaves the chat once nothing is
    picked.

    A channel with a send scope says so in the title (``#ops · scope yul``), and ^R there
    resends the newest message *unscoped* — the way out for a scoped message no repeater
    in earshot carries — after an amber confirm, since an unscoped flood reaches every
    repeater the scope was keeping it from.
    """

    floating = False

    @property
    def fkey_lane(self):
        """The PicoCalc lane in the transcript's own words: Latest/Oldest, Paths, Retry.

        The shared lane's Shift bank dispatches the plain ``end``/``home`` actions, but
        here those are already claimed by the compose line's cursor (see :meth:`handle`) —
        so left alone, both keys land on the same "clear the pick, stick to the tail"
        fallthrough and read as if either one scrolls to the bottom. The two companions
        dispatch ``ctrl_end``/``ctrl_home`` instead, and say what those do to a
        conversation: return to the *latest* message and the compose line, or reach back
        to the *oldest*. That is the shared pair relabelled in place, so it keeps the
        shared handedness — each jump still sitting behind the page heading the same way
        (see :data:`~meshterm.ui.tui.fkeys.DEFAULT_LANE`). F4/F5 keep the shared paging
        pair, which is what a screenful of transcript looks like from the outside even
        though it is the pick that moves.

        The F3 pair follows the lane's two claims. *Retry* is absent in a channel — a
        channel message is never acknowledged, so there is no such thing to retry there —
        and merely dim in a direct chat with nothing outstanding. A channel's Shift slot is
        *Resend* instead — the newest message again, unscoped (see :meth:`_retry_target`)
        — present where a resend is wired and dim until the newest message went out under
        a region. *Paths* needs a picked message to have paths of, and both nav slots need
        a transcript to walk.

        F1/F2 carry the **day** jump, the transcript's own section step (its dividers are
        days) — the same claim a grouped select list makes with ``Sect ↑``/``Sect ↓``, on
        the same keys, dispatching the same ``ctrl_pageup``/``ctrl_pagedown``. Naming it
        for what the sections *are* here is the lane's rule that a chip names an action.
        A left-hand pair rises toward F1, so ``Day ↑`` (back through the transcript) sits
        outside ``Day ↓``, matching both the pager on the right and the list it echoes.
        Lit only with more than one day to step between: a conversation held in an
        afternoon has sections the way a one-section list does — none.
        """
        from .tui.fkeys import FPair, default_lane

        lane = list(default_lane(nav=bool(self._messages)))
        live = bool(self._messages)
        # Messages are chronological, so two days exist exactly when the ends disagree —
        # O(1), where walking the dividers would re-date the whole transcript every paint.
        days = live and (
            self._messages[0].created_at.astimezone().date()
            != self._messages[-1].created_at.astimezone().date()
        )
        lane[0] = FPair("Day ↑", "ctrl_pageup", enabled=days)
        lane[1] = FPair("Day ↓", "ctrl_pagedown", enabled=days)
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
        session,  # noqa: ANN001 - TuiSession, imported lazily to avoid a cycle
        resend: Callable[[ChatMessage], Awaitable[ChatMessage]] | None = None,
        paths: Callable[[ChatMessage], Awaitable[None]] | None = None,
        key_of: Callable[[str], str | None] | None = None,
        scope: str | None = None,
        resend_unscoped: Callable[[ChatMessage], Awaitable[ChatMessage | None]] | None = None,
    ) -> None:
        """Build the chat screen.

        Args:
            conversation: The thread being shown (its label titles the screen).
            messages: The initial transcript (history), oldest-first.
            send: Async callable that sends a line and returns the recorded outbound
                message (or ``None`` if nothing was sent).
            names: Map of contact key prefix to friendly name, for labeling inbound
                direct messages.
            session: The running :class:`~meshterm.ui.tui.session.TuiSession`, used to
                request repaints when messages arrive or a send completes.
            resend: Async callable that re-attempts delivery of an unacknowledged direct
                message, updating it in place (direct chats only; ``None`` for channels).
            paths: Async callable that presents the delivery paths of the picked message
                (the ^P view); ``None`` leaves the affordance quietly inert.
            key_of: Maps a sender's display name back to its node's key (see
                :func:`~meshterm.services.trace_runner.make_name_key_resolver`), the seed
                of the sender's hue; ``None`` (or a name it can't place) leaves senders
                muted — colour is reserved for keyed identities.
            scope: The channel's send scope, stated in the title as a ``·`` atom
                (``#ops · scope yul``); ``None`` for a channel sending under the device
                default, and always for a direct chat.
            resend_unscoped: Async callable that sends a channel message's text again,
                unscoped, and returns the recorded message (channels only; ``None`` leaves
                ^R inert there).
        """
        super().__init__()
        self._scope = scope if conversation.is_channel else None
        self.title = (
            f"{conversation.label} · scope {self._scope}" if self._scope else conversation.label
        )
        self._is_channel = conversation.is_channel
        self._key_of: Callable[[str], str | None] = key_of or (lambda name: None)
        # A direct thread's one remote sender is the peer; its key colours the header
        # even when the resolver can't place the display name.
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
        # Cycled while a direct message is in flight, so its trailing glyph spins (rather than
        # a static hourglass) until the ack resolves. Shared across messages: only one send or
        # retry is ever in flight at a time (both gated by ``_sending``).
        self._spinner = Spinner()
        self._status = ""
        self._stick = True  # keep the newest message in view until the user scrolls up
        self._paths_open = False  # one paths dialog at a time
        self._paste_open = False  # one paste-confirm dialog at a time
        self._resend_open = False  # one resend-unscoped confirm at a time
        # The pick: index of the highlighted message (or None when the compose line is
        # focused), plus the body line it rendered on so the frame keeps it in view.
        self._selected: int | None = None
        self._selected_line: int | None = None
        # Memoizes _render_grouped's output per message, so a repaint triggered by
        # something outside the transcript (a keystroke, the 1s idle tick, a spinner
        # frame) only re-renders the rows that actually changed. See _render_grouped.
        self._render_cache: dict | None = None
        # Strong references to the sends, resends and spinner tickers started off a key
        # handler. The event loop only holds a *weak* one, so a task nothing else names can
        # be collected mid-flight — a half-transmitted message, or a spinner that stops
        # turning. Each task discards itself from the set when it finishes.
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> asyncio.Task:  # noqa: ANN001 - any coroutine this screen owns
        """Start ``coro`` on the loop and hold a reference to it until it finishes."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @property
    def footer_hint(self) -> str:
        """Key hint, reflecting whether a message is picked and what Enter does to it.

        Only keys that would act appear: ``^P paths`` needs a picked message to show the
        paths *of*, and ``^R retry failed`` needs something to retry (see
        :meth:`_retry_target`) — the same rules the F-key lane dims its slots by.
        """
        if self._selected is not None:
            if self._is_channel:
                return "Enter reply (@mention) · ^P paths · ↑↓ pick · ^End/Esc cancel"
            return "Enter paths · ↑↓ pick · ^End/Esc cancel"
        if self._retry_target() is not None:
            if self._is_channel:
                return "Enter send · ↑ pick a message · ^R resend unscoped · Esc back"
            return "Enter send · ↑ pick a message · ^R retry failed · Esc back"
        return "Enter send · ↑ pick a message · Esc back"

    # --- live updates --------------------------------------------------------

    def append(self, message: ChatMessage) -> None:
        """Append an inbound message to the transcript and repaint."""
        self._messages.append(message)
        # Don't yank the view to the tail while the user is picking a message to reply to.
        if self._selected is None:
            self._stick = True
        self._session.invalidate()

    # --- rendering -----------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render the transcript, a divider, the input line, and any status."""
        self._selected_line = None
        if not self._messages:
            lines = render_lines(Text("No messages yet — say hello!", style="muted"), width)
        else:
            # Both direct and channel threads use the same grouped, Discord/Slack-style
            # transcript: consecutive messages from one sender share a colored header, with
            # day dividers between them. Rendering per-message also lets us record where the
            # picked message lands (see _selected_line / cursor_line). Both thread kinds
            # pick; what differs is only what Enter then does with the pick — a reply in a
            # channel, the delivery paths in a direct thread.
            if self._selected is not None:
                self._selected = max(0, min(self._selected, len(self._messages) - 1))
            lines = self._render_grouped(width)

        limit = self._byte_limit()
        # The byte budget is pinned to the right edge of the input's *last* line — so a
        # compose that wraps onto a second line keeps the counter in the bottom-right
        # corner rather than letting it trail the cursor down the wrap. Only a last line
        # already full to the edge pushes it onto a right-aligned line of its own.
        input_line = self._editor.render(overflow_at=self._overflow_at(limit))
        counter = self._byte_counter(limit)
        compose = right_aligned_tail(input_line, counter, width)
        footer_parts: list[RenderableType] = [
            Text("─" * width, style="muted"),
            compose,
        ]
        if self._selected is not None:
            footer_parts.append(self._pick_banner())
        if self._status:
            footer_parts.append(Text(self._status, style="muted"))
        lines += render_lines(Group(*footer_parts), width)

        # Sticking to the bottom: hand the frame an over-large offset so it clamps the view
        # to the final lines (input + latest messages). Scrolling up clears the stick.
        if self._stick:
            self.scroll = len(lines)
        return lines

    def cursor_line(self) -> int | None:
        """Keep the picked reply target in view; otherwise free scroll (managed by stick)."""
        return self._selected_line

    # --- outgoing byte budget ------------------------------------------------

    def _byte_limit(self) -> int:
        """The UTF-8 byte ceiling for a message in this conversation (channel vs direct)."""
        return CHANNEL_BYTE_LIMIT if self._is_channel else DM_BYTE_LIMIT

    def _used_bytes(self) -> int:
        """UTF-8 byte length of the current compose buffer — what counts against the limit."""
        return len(self._editor.text.encode("utf-8"))

    def _overflow_at(self, limit: int) -> int | None:
        """Index of the first compose character whose bytes spill past ``limit``, else ``None``.

        Walking by character (not byte) keeps multibyte input intact: an emoji or accented
        letter is entirely under or entirely over the line, never split mid-sequence.
        """
        total = 0
        for i, ch in enumerate(self._editor.text):
            total += len(ch.encode("utf-8"))
            if total > limit:
                return i
        return None

    def _byte_counter(self, limit: int) -> Text:
        """The inline ``used/limit`` budget — the shared compose gauge (:func:`byte_counter`)."""
        return byte_counter(self._used_bytes(), limit)

    def _name(self, peer: str | None) -> str | None:
        """Resolve a sender key prefix to a contact name (exact, then prefix match)."""
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
        """Render the transcript to ANSI lines, tracking the picked message's row.

        Serves both direct and channel threads. Consecutive messages from the same sender on
        the same day share one colored header, with each message body indented below. A muted
        divider marks each new day, and a blank line separates distinct sender groups.
        Rendering message-by-message (rather than as one Group) lets us record the body line
        of the picked message in :attr:`_selected_line` so the frame can scroll it into
        view. Both thread kinds pick — see :meth:`handle`.

        A repaint is triggered constantly by things that touch nothing here — the idle
        tick, a keystroke, the ack spinner — so this memoizes each message's rendered lines
        in :attr:`_render_cache` and, message by message, splices in the cached slice
        instead of re-rendering it. A message is only ever re-rendered when it must be:
        it's still awaiting its ack (the spinner redraws its trailing glyph every tick),
        it's the currently picked one (styled by the pick, not by the message), or its
        identity/``acked`` no longer match what's cached (appended, replaced in place by a
        pending bubble resolving, or removed). Crucially this is per-message, not a cached
        prefix cut off at the first such row — a message stays cheap to redraw no matter
        how far back in a long transcript it sits, so paging up through history doesn't
        regress to a full re-render every keystroke the way a prefix cutoff would.
        Grouping context (day/sender headers) is cheap to recompute either way, so it's
        derived fresh every time and never trusted from the cache.
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
        # Record each day divider as a sticky block of its own one row, so the divider
        # governing the topmost visible message is re-pinned to the top row once it
        # scrolls off — the same base Screen.sticky_block the conversation picker uses for
        # its section headings. A day has nothing to say beyond its date, so the block is
        # the divider alone; a select list's heading may carry its description along.
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
            # Group by (are-we-the-sender, display name), not the name alone, so our own
            # messages never merge with a remote sender who happens to be named the same.
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
                    # A day divider is this transcript's section heading — it pins to the
                    # top row while its day scrolls under it and ^PgUp/^PgDn step by it —
                    # so it wears the app's heading dress (``── Label ──`` in accent, see
                    # menus.section_heading) rather than reading as grey chrome. The rule
                    # above the compose line stays muted: that one really is chrome.
                    divider = render_lines(
                        Text(f"── {stamp:%a} {stamp:%b} {stamp.day} ──", style="accent"), width
                    )
                    self._sticky_headers.append((len(lines), [divider[0]]))
                    lines += divider
                if new_day or group != prev_group:
                    if not new_day and lines:
                        lines += render_lines(Text(""), width)  # gap between sender groups
                    header = self._group_header(sender, is_self=message.outbound)
                    lines += render_lines(header, width)
                if selected:
                    # The first message owns the head of the transcript: nothing precedes
                    # its day divider, so its pick anchors at line 0 rather than at its own
                    # body. Anchoring on the body left the view scrolled two lines in at the
                    # very top — the divider only pinned, the sender chip gone, and the
                    # title bar's ↑ lit over a transcript with nothing above it.
                    self._selected_line = 0 if idx == 0 else len(lines)
                lines += self._body_lines(body, message, width, selected=selected)
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
        """Return the display sender and cleaned body for a message.

        Our own messages are ``you``. Inbound channel messages carry a ``Name: `` prefix we
        lift into the sender (falling back to ``·`` when absent); inbound direct messages take
        the sender from the resolved contact name (or the raw key, or ``?``).
        """
        if message.outbound:
            return "you", message.text
        if message.is_channel:
            name, body = split_channel_sender(message.text)
            return (name or "·"), body
        return (self._name(message.peer) or message.peer or "?"), message.text

    def _group_header(self, sender: str, *, is_self: bool = False) -> Text:
        """Build the sender header that starts a group: the name, drawn as a chip.

        The chip is what separates a *label* from the prose under it — a name standing
        alone above its messages reads as a tag rather than as a coloured word someone
        typed. It is a path line's own chip (see :func:`~meshterm.ui.widgets.name_chip`),
        so a sender wears exactly what that node wears as a hop in a route, our own
        messages included: the ``★``, never our name.

        Only this one is framed: an ``@mention`` inside a body (see
        :meth:`_render_mentions`) is part of what was said, and inscribing it would put a
        chip in the middle of a sentence.
        """
        if is_self:
            return name_chip("", you=True)
        # ``·`` is a channel line that arrived with no sender prefix — nobody to key on.
        return name_chip(sender, None if sender == "·" else self._sender_key(sender))

    def _body_lines(
        self, body: str, message: ChatMessage, width: int, *, selected: bool = False
    ) -> list[str]:
        """Render one message to ANSI lines, stamped with its own time and hanging-indented.

        The per-message timestamp lives here (not on the group header) so every message
        shows the time it was actually sent, even when several are grouped under one
        sender — otherwise a run of same-sender messages would appear to share one time.
        A long body wraps with a hanging indent so continuation lines align under the body
        rather than under the timestamp gutter. When ``selected``, the line is marked as the
        reply target (matching the select screen's ``❯`` pointer and cursor highlight).
        """
        stamp = message.created_at.astimezone()
        prefix = Text()
        prefix.append("❯ " if selected else "  ", style="cursor" if selected else None)
        prefix.append(f"{stamp:%H:%M}  ", style="cursor" if selected else "muted")
        body_text = self._body_text(body, message, selected=selected)
        return render_hanging(prefix, body_text, width, indent=prefix.cell_len)

    def _body_text(self, body: str, message: ChatMessage, *, selected: bool) -> Text:
        """Build the styled body of a message: mentions colored, then any trailing glyphs."""
        text = self._render_mentions(body, selected=selected)
        if message.outbound:
            # Direct messages track per-message delivery (spin → ✅/❌, retryable); channel
            # broadcasts have no ack, so only flag one that failed to leave the companion.
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
        """A muted tail on a sent message that went out under a scope other than the title's.

        Only in a channel whose title names a scope, and only for the exceptions to it — an
        unscoped resend, or a message sent before the scope was set or changed. Every other
        line would repeat the title; and in a channel with no scope of its own, the default
        its messages went under is the device's business, not news on every line.
        """
        if not self._scope or not message.scope or message.scope == self._scope:
            return Text()
        if message.scope == WILDCARD:
            return Text("  · unscoped", style="muted")
        return Text(f"  · scope {message.scope}", style="muted")

    def _render_mentions(self, body: str, *, selected: bool) -> Text:
        """Render body text, rewriting each ``@[Name]`` token to a ``@Name`` in its hue.

        The reply flow primes the compose line with an ``@[Name]`` token (see
        :meth:`_begin_reply`); here it reads back as a bare ``@Name`` colored in that
        sender's stable hue, so a mention is visually tied to the person it names. Text
        around the mentions keeps the line's base style (``cursor`` when the message is the
        picked reply target, otherwise unstyled).
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
        """Map an outbound direct message's ``acked`` state to its trailing mark.

        A message still awaiting its ack (``acked is None``) shows the current spinner frame,
        animated by :meth:`_spin_while` for as long as the send is in flight; a resolved one
        shows the delivered ``✓`` (ok) or the unacknowledged ``✗`` (err).
        """
        if acked is None:
            return self._spinner.text()
        glyph, style = _DELIVERED if acked else _FAILED
        return Text(glyph, style=style)

    def _pick_banner(self) -> Text:
        """One-line cue shown above the input while a message is picked.

        Channels lead with the reply affordance (Enter's job there); direct chats with
        the paths view (their Enter). Both mention what the pick is for, so the state
        never reads as a mystery highlight.
        """
        message = self._messages[self._selected]
        sender, _ = self._sender_and_body(message)
        who = "this message" if message.outbound or sender == "·" else sender
        if self._is_channel:
            return Text(
                f"↩ Enter to reply to {who} with an @mention · End to cancel",
                style="accent",
            )
        return Text("Enter to see the paths this message took · End to cancel", style="accent")

    def _sender_style(self, sender: str, *, is_self: bool = False, mention: bool = False) -> str:
        """Pick a stable color for a sender name — keyed on the sender's node key.

        Our own messages are white — keyed on ``is_self`` (the message being outbound), not
        on the ``"you"`` label, so a remote sender who happens to be named ``you`` still gets
        a hue from the palette rather than masquerading as us. ``·`` (unknown) takes
        ``node.unknown``. Every other sender resolves its name back to a key (contacts, then
        the recorder's stored names; a direct thread's *sender label* falls back to the
        peer's own key) and takes that key's hue; a name no known node carries stays
        ``node.unknown`` too — the app-wide rule that colour marks a keyed identity.

        Every grey here is the *node* grey, never ``muted``: an unidentified sender is
        content you can still act on (pick the message, open its paths), not chrome, and
        the two must not track each other — on the console ``muted`` is the dark slot and
        ``node.unknown`` the light one (JP, 2026-08-12).

        ``mention=True`` styles an ``@mention`` in the body rather than a sender label: a
        mention names an arbitrary person, so the peer-key fallback must not apply — an
        unresolved one stays grey in a direct chat exactly as it does in a channel.
        """
        if is_self:
            return "you"  # white, out of the per-sender hue range — always easy to spot
        if sender == "·":
            return "node.unknown"
        return name_style(sender, self._sender_key(sender, mention=mention))

    def _sender_key(self, sender: str, *, mention: bool = False) -> str | None:
        """The key a sender name resolves back to, or ``None`` when nothing places it.

        Contacts first, then the recorder's stored names; in a *direct* thread a sender
        label that resolves to nothing falls back to the peer's own key, since the only
        two people in the conversation are us and them. A mention names an arbitrary
        person, so that fallback must not apply to one.

        Shared by the colour (:meth:`_sender_style`) and the chip
        (:meth:`_group_header`) so the two can't disagree about who a name is.
        """
        key = self._key_of(sender)
        if not key and not self._is_channel and not mention:
            key = self._peer_key or None
        return key

    # --- input ---------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Dispatch a key: pick and act on messages, edit the compose line, or leave.

        Both chat kinds share one model. With no message picked, Enter sends and typing
        edits the compose line; a paste (Ctrl-V, or a terminal's bracketed paste) is
        confirmed on an amber dialog before it lands there (see :meth:`_begin_paste`). ↑
        picks the newest message; the pick then walks with
        ↑↓, PgUp/PgDn (a screenful), Ctrl+Home (the very first message), and
        Ctrl+PgUp/PgDn (day dividers), carrying the view with it. Enter on a picked
        message primes a reply ``@mention`` in a channel and opens the delivery paths
        in a direct chat; ^P opens the picked message's paths in either kind, and does
        nothing while nothing is picked. ^End (or moving past the newest) returns to the
        compose line; Esc peels the pick first, the screen second.
        """
        if action == "enter":
            if self._selected is not None:
                if self._is_channel:
                    self._begin_reply()
                else:
                    self._open_paths(self._selected)
            else:
                self._submit()
        elif action == "paste":
            self._begin_paste(data)
        elif action == "paths":
            self._open_paths(self._selected)
        elif action == "retry":
            if self._is_channel:
                self._begin_resend_unscoped()
            else:
                self._retry()
        elif action == "escape":
            if self._selected is not None:
                self._clear_selection()  # first Esc unpicks; next leaves the chat
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
            self._stick = True  # jump back to the live tail / compose line
        else:
            if self._editor.edit(action, data):
                # Touching the compose line returns focus there — nothing stays picked.
                self._status = ""  # trimming clears the "too long" notice
                self._clear_selection()
                self._stick = True

    def _open_paths(self, index: int | None) -> None:
        """Float the delivery-paths view for the picked message (one dialog at a time).

        ``index`` is the pick, so ``None`` — nothing picked — opens nothing: a path is a
        route one *particular* message walked, and guessing at the latest one showed the
        paths of whichever message happened to be at the bottom of the transcript.
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

    def _begin_paste(self, data: str) -> None:
        """Confirm a clipboard paste on an amber dialog, then insert it into the compose line.

        A chat message goes out over the air, so a paste — which can be far larger than a
        keystroke, or carry content the user didn't mean to broadcast — is gated behind a
        Cancel/Paste confirm (the amber ``danger`` tier: disruptive, not data loss) rather
        than dropped straight in. Newlines and control characters fold to spaces (a message
        is one line); an empty result never opens the dialog. One paste dialog at a time,
        scheduled off the key handler like the paths view.
        """
        if self._paste_open:
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
                # Land it in the compose line: drop any message pick, snap back to the tail,
                # and insert the run through the shared editor. An over-budget result is
                # flagged by the byte gauge, exactly as typing past the limit already is.
                self._clear_selection()
                self._stick = True
                self._editor.edit("text", clean)
                self._status = ""
            self._session.invalidate()

        self._spawn(run())

    def _move_selection(self, delta: int) -> None:
        """Move the reply selection by ``delta`` messages (negative = toward older).

        Entering from the compose line only happens moving up (``delta < 0``); moving past
        the newest message drops the selection and re-sticks to the tail.
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
        """Jump the reply selection to the first message of the previous/next day.

        The selection-space analogue of the transcript's Ctrl+PageUp/PageDown day jump: down
        moves to the first message of the following day (dropping to the tail when there's no
        later day); up moves to the first message of the current day, or the previous day's
        when already atop one.
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
        """Message indices that begin a new local-day group — matching the transcript dividers."""
        starts: list[int] = []
        prev_day = None
        for i, message in enumerate(self._messages):
            day = message.created_at.astimezone().date()
            if day != prev_day:
                starts.append(i)
                prev_day = day
        return starts

    def _clear_selection(self) -> None:
        """Drop any reply selection (focus returns to the compose line)."""
        self._selected = None
        self._selected_line = None

    def _begin_reply(self) -> None:
        """Prime the compose line with an ``@mention`` of the picked message's sender."""
        message = self._messages[self._selected]
        sender, _ = self._sender_and_body(message)
        named = not message.outbound and sender != "·"
        mention = f"@[{sender}] " if named else "@"
        self._editor = LineEditor(mention + self._editor.text)
        self._clear_selection()
        self._stick = True
        self._session.invalidate()

    def _submit(self) -> None:
        """Send the current input line as a message (scheduled off the key handler).

        Refuses an over-budget line: the buffer is kept intact (so the user can trim it) and
        the overage is reported, matching the red overflow the compose bar already shows.
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
            # Direct chats show an optimistic bubble whose trailing mark tracks delivery:
            # a spinner now, then ✓/✗ once the ack resolves (or times out). acked=None
            # ⇒ still spinning.
            pending = ChatMessage(text=text, outbound=True, created_at=utcnow())
            self._messages.append(pending)
            self._status = ""
            self._session.invalidate()
            self._spawn(self._send_direct(pending))

    async def _send_channel(self, text: str) -> None:
        """Broadcast a channel message and append it once the companion accepts it."""
        try:
            message = await self._send(text)
            if message is not None:
                self._messages.append(message)
            self._status = ""
        except Exception as exc:  # noqa: BLE001 - report inline, keep the chat alive
            self._status = f"send failed: {exc}"
        finally:
            self._sending = False
            self._stick = True
            self._session.invalidate()

    async def _spin_while(self, coro: Awaitable[Any]) -> Any:
        """Await ``coro`` while animating the delivery spinner on the in-flight message.

        A background timer advances the shared spinner and repaints every
        :func:`~meshterm.ui.tui.spinner.spinner_interval` seconds (the platform's spin
        cadence), so a pending message's trailing glyph spins until
        the ack resolves. The timer is always cancelled (and awaited, so it can't outlive the
        send as a stray pending task) before returning. The spinner is cosmetic, so any hiccup
        in the animation is swallowed rather than allowed to break the send.
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
            except Exception:  # noqa: BLE001 - a spinner hiccup must never break a send
                pass

    async def _send_direct(self, pending: ChatMessage) -> None:
        """Await delivery of the optimistic ``pending`` bubble, swapping in the stored row.

        On success the pending bubble is replaced by the recorded message (carrying its
        resolved ``acked`` state and row id, so a ❌ can later be retried in place). A hard
        failure — the companion rejecting the send outright — drops the bubble and reports
        the error inline, matching how a failed send has always surfaced.
        """
        try:
            message = await self._spin_while(self._send(pending.text))
        except Exception as exc:  # noqa: BLE001 - report inline, keep the chat alive
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
        """The message ^R would re-send, or ``None`` when there is nothing to retry.

        In a direct chat, the newest outbound message that went out and was never
        acknowledged. In a channel — where nothing is ever acknowledged — the newest
        message *we* sent, when it went out under a region: ^R sends it again unscoped. The
        newest, not any scoped one: once the unscoped copy has gone it is the newest, and
        there is nothing left to offer. Either way only while a resend could actually
        start — no send already in flight, and a resend path wired.
        """
        if self._is_channel:
            if self._sending or self._resend_unscoped is None:
                return None
            newest = next((m for m in reversed(self._messages) if m.outbound), None)
            if newest is None or not newest.scope or newest.scope == WILDCARD:
                return None
            return newest
        if self._sending or self._resend is None:
            return None
        return next(
            (m for m in reversed(self._messages) if m.outbound and m.acked is False),
            None,
        )

    def _retry(self) -> None:
        """Re-attempt delivery of the most recent unacknowledged direct message (Ctrl-R)."""
        target = self._retry_target()
        if target is None:
            return
        self._sending = True
        target.acked = None  # back to ⏳ while the retry is in flight
        self._status = "retrying…"
        self._stick = True
        self._session.invalidate()
        self._spawn(self._resend_message(target))

    def _begin_resend_unscoped(self) -> None:
        """Confirm, then send the newest scoped channel message again unscoped (^R).

        Amber (``danger``): nothing is lost, but an unscoped flood is relayed by every
        repeater that allows one — the whole mesh the scope was keeping the message out of —
        so it is a choice made on purpose, with the region it is leaving named. The resend
        is a new message on the air (a channel message has no identity to re-deliver), so
        it appends as one; a radio that can't send unscoped refuses before anything goes
        out, and the status line says why.
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
            self._stick = True
            self._session.invalidate()
            try:
                message = await self._resend_unscoped(target)
                if message is not None:
                    self._messages.append(message)
                self._status = ""
            except Exception as exc:  # noqa: BLE001 - report inline, keep the chat alive
                self._status = f"resend failed: {exc}"
            finally:
                self._sending = False
                self._stick = True
                self._session.invalidate()

        self._spawn(run())

    async def _resend_message(self, message: ChatMessage) -> None:
        """Drive a retry to completion, refreshing the message's delivery state in place."""
        assert self._resend is not None
        try:
            await self._spin_while(self._resend(message))
        except Exception as exc:  # noqa: BLE001 - report inline, keep the chat alive
            message.acked = False
            self._status = f"retry failed: {exc}"
        else:
            self._status = "" if message.acked else "still no ack — ^R to retry"
        finally:
            self._sending = False
            self._stick = True
            self._session.invalidate()

    def _swap(self, old: ChatMessage, new: ChatMessage) -> None:
        """Replace an optimistic bubble with its recorded message, in place."""
        try:
            self._messages[self._messages.index(old)] = new
        except ValueError:  # pragma: no cover - the bubble is always still present
            self._messages.append(new)

    def _discard(self, message: ChatMessage) -> None:
        """Drop an optimistic bubble that never became a real message."""
        try:
            self._messages.remove(message)
        except ValueError:  # pragma: no cover - defensive
            pass


async def open_chat(ctx: AppContext, conversation: Conversation) -> int:
    """Open the live chat screen for ``conversation`` and run it until dismissed.

    Ensures listening and message recording are active, loads the stored transcript,
    subscribes to the hub so inbound messages for this thread append live, and pushes the
    screen. The subscription and the active-conversation marker are always cleaned up on
    exit.

    Args:
        ctx: The shared application context (must be running the interactive TUI surface).
        conversation: The channel or contact conversation to open.

    Returns:
        The number of messages shown in the transcript when the screen closed.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("live chat is only available in the interactive menu")
    session = ctx.ui.session

    device = await ctx.device()
    try:
        await ctx.chat.start()  # begin recording inbound if it wasn't already
    except Exception:  # noqa: BLE001 - the hub may already be running; recording is best-effort
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
    """Run a direct send, offering to restore a contact the device has forgotten, then retry.

    The one rejection a send can recover from: the firmware has no contact for the recipient
    (see :class:`~meshterm.core.connection.ContactNotOnDeviceError`), which one write puts
    right. The user is asked first (:func:`_restore_contact`); declining re-raises, so the
    chat reports the refusal exactly as it reports any other failed send.

    Args:
        ctx: The shared application context.
        attempt: The send to run — called a second time, unchanged, once the contact is back.

    Returns:
        Whatever ``attempt`` returns.

    Raises:
        Exception: Anything ``attempt`` raises; a forgotten-contact rejection is re-raised
            only when the offer to add it back was declined.
    """
    try:
        return await attempt()
    except ContactNotOnDeviceError as missing:
        if not await _restore_contact(ctx, missing):
            raise
        return await attempt()


async def _restore_contact(ctx: AppContext, missing: ContactNotOnDeviceError) -> bool:
    """Offer to write a forgotten contact back to the device; ``True`` if it now holds it.

    A direct message is addressed by the *device's* own contact entry, so a contact the
    firmware has dropped can't be messaged even though MeshTerm still lists it (the list a
    screen sees is the union of the device's table and the ones MeshTerm remembers for it —
    see :mod:`meshterm.core.contact_store`). Everything the entry needs is on the contact we
    already hold, so the fix is one write; the send that hit the rejection retries after it.

    The write is offered rather than done silently: it changes what the device stores, and a
    full contact table refuses it (which is worth seeing, not swallowing).

    Args:
        ctx: The shared application context.
        missing: The rejection, carrying the contact the device couldn't find.

    Returns:
        ``True`` once the contact is on the device (retry the send), ``False`` if the user
        declined — in which case the caller re-raises, so the chat reports the refusal.

    Raises:
        DeviceCommandError: If the device refused the write (a full table, most often).
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
    # The device's table just changed under the session cache; the next read re-fetches it
    # (with the route the firmware learns for the contact from here on).
    ctx.devstate.invalidate_contacts()
    return True


def _contact_names(contacts: list[Contact]) -> dict[str, str]:
    """Build a key-prefix → name map for labeling inbound direct messages."""
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
        conversation: The conversation currently shown.

    Returns:
        ``True`` if the message should append to this transcript.
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
    """What a message we sent went out under, for the paths dialog — and why it may be lost.

    Built from the scope recorded on the message at send time (:attr:`ChatMessage.scope`),
    never from the channel's scope now, which may have moved on since. When a scoped
    message has no relayed copy on record *and* no repeater was ever heard here to carry
    its region (:meth:`~meshterm.core.region_store.RegionStore.carriers`), the likeliest
    reason is stated plainly: a scoped flood is relayed only by repeaters carrying its
    region, and nothing known in earshot does.

    Args:
        ctx: Shared application context (for the region store).
        message: The message whose paths are open.
        relayed: Whether any copy of it was heard coming back.

    Returns:
        The line, or ``None`` for a message with no recorded scope (inbound, direct, or
        sent before scopes were recorded).
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
    device,  # noqa: ANN001 - core Device; typed at the source
) -> Callable[[ChatMessage], Awaitable[None]]:
    """Build the async presenter behind the chat's ^P delivery-paths view.

    Gathers what the presenter needs once per chat open: a hop-name resolver over the
    contacts plus every name the recorder ever overheard (the app-wide rule that a
    nameable node never shows as a bare hash), our own node's name for the white
    ``you``, the routing prefix width for the hash highlights, and — for a channel —
    its secret, read from the device when the conversation didn't carry one (a picker
    conversation knows its identity but not always its key).

    Args:
        ctx: The shared application context.
        conversation: The conversation the chat screen is opening.
        device: The connected device (already awaited by the caller).

    Returns:
        An async callable presenting one message's paths in a floating window.
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
        # Our own key names one end of every direct frame in this conversation; the
        # contact's key names the other (see :func:`direct_arrivals`).
        self_key = str(info.get("public_key") or "") or None
    except Exception:  # noqa: BLE001 - a nameless self just skips the white highlight
        self_name = None

    secret = conversation.secret
    if conversation.is_channel and not secret and conversation.channel_idx is not None:
        try:
            payload = await device.get_channel(conversation.channel_idx)
            raw = (payload or {}).get("channel_secret")
            secret = bytes(raw) if raw else None
        except Exception:  # noqa: BLE001 - no key, no decrypt; the view says so honestly
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
            # An exact match is the same claim the channel view makes: these frames are
            # this message. It is earned by the frames' shared MAC, so it survives being
            # unable to read a word of them.
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
        # The path's two ends: who the message set out from, and who it was addressed to.
        # Our own sends leave us for the peer; an inbound channel message names its sender
        # on the wire and is addressed to everyone, so it lands on us; a direct chat's
        # inbound origin is the conversation's peer.
        destination: str | None = self_name
        if message.outbound:
            source = self_name
            # A channel broadcast is addressed to nobody in particular, and the copies we
            # log are its rebroadcasts coming back — so it really does end on us. A direct
            # send does not: it was going to one node, and that is where its route ends.
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
