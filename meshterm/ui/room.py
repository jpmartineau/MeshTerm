# SPDX-License-Identifier: Apache-2.0
"""The room view: a room server's board, read and posted to like a chat.

A room server keeps its members' most recent posts and sends each member the ones they
missed. To the reader that is a conversation with many voices — the shape a channel already
has — so the view *is* the chat screen (:class:`~meshterm.ui.chat.ChatScreen`), with what a
room knows that a channel doesn't: who wrote each post, by key rather than by a name typed in
front of it, and whether the room will take what you post.

Three things set it apart, each the room's own:

* **Authors are keys.** A room relays a post as a direct message from *itself*, signed with
  the first four bytes of the author's key (:attr:`~meshterm.core.models.ChatMessage.author`).
  The post is filed under its author's name when a contact or an overheard advert names that
  key, and under the bare hash — in the unidentified-node grey — when nothing does. Either
  way its colour is the key's, never a guess from the name.
* **Getting in is a login.** The title carries what the room let us do (``member``,
  ``admin``, ``read-only``) or where the login stands. Opening a room logs in to it only
  when it has been quiet (:meth:`~meshterm.services.rooms.RoomService.login_due`), asks for
  the room's password the first time, and ^L logs in again on demand. The login runs while
  the board stays open to read, since a room several hops out can take a while to answer.
* **Read-only means silent.** A room drops a read-only member's posts without a word, so
  the compose line says so instead of taking typing that would go nowhere.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from rich.console import RenderableType
from rich.text import Text

from ..core.events import EventKind, MeshEvent
from ..core.models import (
    ChatMessage,
    Conversation,
    LoginResult,
    NodeResolver,
    RoomAccess,
    RoomLogin,
)
from .chat import ChatScreen, _belongs, _contact_names, _make_paths_presenter, _with_restore

if TYPE_CHECKING:
    from ..context import AppContext

#: The title's login atom while a login is in flight.
LOGGING_IN = "logging in…"
#: The title's login atom after a login met silence.
NO_REPLY = "no reply"
#: The title's login atom after the companion refused the login outright.
REFUSED = "refused"
#: The title's atom for a room we hold no password for, the join prompt dismissed.
NOT_JOINED = "not joined"


def author_label(author: str, resolve: NodeResolver) -> tuple[str, str | None]:
    """How a post's author is named and coloured: their name and key, or their bare hash.

    THE rule for an author, shared by the room view and the Chat picker's preview so the
    two cannot name one person differently. A key prefix some contact or overheard advert
    carries resolves to that node's name, coloured by the key itself; one nothing names
    stands as the hash, with no key to colour it — the unidentified-node grey, since a bare
    hash standing in as a name is exactly what that grey is for.

    Args:
        author: The author's key prefix, as the post carried it.
        resolve: Maps a key prefix to a node's name, or back to itself when unknown (see
            :func:`~meshterm.services.trace_runner.make_node_resolver`).

    Returns:
        ``(label, key)``: the name and the key that colours it, or the hash and ``None``.
    """
    name = resolve(author)
    if not name or name.lower() == author.lower():
        return author, None
    return name, author


class RoomScreen(ChatScreen):
    """A room's board: posts under their authors, a compose line, and the login's state.

    Everything a chat does it does — pick a post and Enter replies with an ``@mention``, ^P
    shows the paths a post took, ^R retries a post the room never acknowledged — plus ^L,
    which logs in to the room: with the remembered password, or by asking for one when
    none is remembered or the last login met silence.
    """

    _empty_text = "No posts yet"

    @property
    def picocalc_lyra_lane(self):
        """The chat's lane, with *Log in* on F1's Shift half (F6), where ^L has no key.

        Every chord the desktop reaches needs a chip on the handheld, and F1's Shift half is
        the one the chat leaves free. Dim while a login is already under way.
        """
        from .tui.fkeys import FPair

        lane = list(super().picocalc_lyra_lane)
        day = lane[0]
        lane[0] = FPair(
            day.label,
            day.action,
            "Log in",
            "login",
            enabled=day.enabled,
            opp_enabled=not self._login_open,
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
        resolve: NodeResolver,
        password: Callable[[], str | None],
        ask: Callable[[], Awaitable[str | None]],
        log_in: Callable[[str], Awaitable[RoomLogin]],
        access: RoomAccess | None = None,
        resend: Callable[[ChatMessage], Awaitable[ChatMessage]] | None = None,
        paths: Callable[[ChatMessage], Awaitable[None]] | None = None,
        key_of: Callable[[str], str | None] | None = None,
    ) -> None:
        """Build the room view.

        Args:
            conversation: The room's conversation (its label titles the screen).
            messages: The board as stored, oldest first — posts only (see
                :meth:`~meshterm.persistence.repository.Repository.recent_chat_messages`).
            send: Posts a line to the room and returns the recorded post.
            names: Contact key prefix → name (what the chat screen labels a peer with).
            session: The running :class:`~meshterm.ui.tui.session.TuiSession`.
            resolve: Maps an author's key prefix to a name (see :func:`author_label`).
            password: The password at hand for this room, or ``None`` when none is
                remembered (``""`` is a password: an open room).
            ask: Asks the reader for the room's password; ``None`` when they decline.
            log_in: Logs in with a password — one exchange — and says how it ended.
            access: What the room let us do when we last logged in, if we ever did.
            resend: Posts an unacknowledged post again (^R).
            paths: Presents the paths a picked post took (^P).
            key_of: Maps a name back to its node's key, for ``@mention`` hues.
        """
        super().__init__(
            conversation,
            messages,
            send=send,
            names=names,
            session=session,
            resend=resend,
            paths=paths,
            key_of=key_of,
        )
        self._label = conversation.label
        self._resolve = resolve
        self._password = password
        self._ask = ask
        self._log_in = log_in
        self._access = access
        #: Where the login stands, when that is what the title should say instead of the
        #: access (one of the module's atoms); ``None`` lets the access speak.
        self._login_state: str | None = None
        self._login_open = False
        self._apply_access()

    # --- the login ----------------------------------------------------------------------

    def begin_login(self, *, ask: bool = False) -> None:
        """Log in to the room, off the key handler; one login at a time.

        Args:
            ask: Ask for the password even when one is remembered — what ^L does after a
                login met silence, since a room is just as silent for a wrong password.
        """
        if self._login_open:
            return
        self._login_open = True
        self._session.run_detached(self._run_login(ask))

    async def _run_login(self, ask: bool) -> None:
        """Find a password (asking if need be), log in with it, and show how it went."""
        try:
            password = None if ask else self._password()
            if password is None:
                password = await self._ask()
                if password is None:  # declined: the board stays open to read
                    if self._access is None and self._password() is None:
                        self._login_state = NOT_JOINED
                    return
            self._login_state = LOGGING_IN
            self._status = ""
            self._retitle()
            try:
                login = await self._log_in(password)
            except Exception as exc:  # noqa: BLE001 - report inline, keep the board open
                self._login_state = None
                self._status = f"login failed: {exc}"
                return
            self._apply_login(login)
        finally:
            self._login_open = False
            self._retitle()
            self._session.invalidate()

    def _apply_login(self, login: RoomLogin) -> None:
        """Take in a login's outcome: the access it won, or why there is none."""
        if login.access is not None and login.result is LoginResult.ACCEPTED:
            self._access = login.access
            self._login_state = None
            self._status = ""
        elif login.result is LoginResult.REFUSED:
            self._login_state = REFUSED
            self._status = "The radio refused the login. ^L to enter the password again."
        else:
            # The one failure a room has: silence. It is also how it answers a wrong
            # password, so the line names both and ^L asks for the password next time.
            self._login_state = NO_REPLY
            self._status = "No reply — out of reach, or a wrong password. ^L to try again."
        self._apply_access()

    def _apply_access(self) -> None:
        """Let the compose line take typing only where the room keeps what we post."""
        self._composing = self._access is None or self._access.can_post
        self._retitle()

    def _retitle(self) -> None:
        """Title the board with the room and its one status atom (``Room · member``)."""
        atom = self._login_state or (self._access.value if self._access else None)
        self.title = f"{self._label} · {atom}" if atom else self._label

    # --- the board ----------------------------------------------------------------------

    def append(self, message: ChatMessage) -> None:
        """Add a post that just arrived — once, however many times the room sends it.

        A room re-sends a post it never heard acknowledged, so the same post can land
        twice; and what else a room sends under its key (an admin's command replies) is
        not on the board at all.
        """
        if not message.is_post:
            return
        for held in reversed(self._messages):
            if (
                held.author == message.author
                and held.created_at == message.created_at
                and held.text == message.text
            ):
                return
        super().append(message)

    def _sender_and_body(self, message: ChatMessage) -> tuple[str, str]:
        """A post's author (by name, or by hash), and its text — ``you`` for our own."""
        if message.outbound:
            return "you", message.text
        if message.author:
            return author_label(message.author, self._resolve)[0], message.text
        return self._label, message.text

    def _header_key(self, sender: str, message: ChatMessage) -> str | None:
        """The author's own key colours their chip; a hash nothing names stays grey."""
        if message.author:
            return author_label(message.author, self._resolve)[1]
        return super()._header_key(sender, message)

    def _compose_line(self, width: int) -> RenderableType:
        """The compose line — or, for a read-only member, why there is none."""
        if self._composing:
            return super()._compose_line(width)
        return Text("read-only — this room doesn't keep what you post", style="muted")

    def _submit(self) -> None:
        """Post the compose line, unless the room is known to drop our posts."""
        if self._composing:
            super()._submit()

    def handle(self, action: str, data: str = "") -> None:
        """^L logs in; everything else is the chat's."""
        if action == "login":
            self.begin_login(ask=self._login_state in (NO_REPLY, REFUSED, NOT_JOINED))
            self._session.invalidate()
            return
        super().handle(action, data)

    @property
    def footer_hint(self) -> str:
        """Key hint: the chat's, in a board's words, with ^L while a login could start."""
        if self._selected is not None:
            code = " · ^U QR" if self._picked_urls() else ""
            return f"Enter reply (@mention) · ^P paths{code} · ↑↓ pick · ^End/Esc cancel"
        atoms = ["Enter send"] if self._composing else []
        atoms.append("↑ pick a message")
        if self._retry_target() is not None:
            atoms.append("^R retry failed")
        if not self._login_open:
            atoms.append("^L log in")
        atoms.append("Esc back")
        return " · ".join(atoms)


async def open_room(ctx: AppContext, conversation: Conversation) -> int:
    """Open the room view for ``conversation`` and run it until the reader leaves.

    The board opens at once on what is stored. A room with no password remembered asks for
    one over it (the first visit — joining); one that has been quiet logs in with the
    password it has, which restarts the room's catch-up; one heard from lately sends
    nothing at all. Posts that arrive while it is open append live, and the room's unread
    count stays clear while the reader is on it.

    Args:
        ctx: The shared application context (running the interactive TUI surface).
        conversation: The room's conversation (:attr:`~Conversation.is_room`).

    Returns:
        The number of posts on the board when the view closed.
    """
    from ..services import trace_runner

    session = ctx.ui.session
    room = conversation.contact
    assert room is not None
    device = await ctx.device()
    try:
        await ctx.chat.start()  # record inbound posts, if nothing has started it yet
    except Exception:  # noqa: BLE001 - the hub may already be running; recording is best-effort
        pass

    board = ctx.repo.recent_chat_messages(
        is_channel=False,
        peer=conversation.peer,
        limit=ctx.preferences.chat_history_limit,
        posts_only=True,
    )
    contacts = await ctx.devstate.contacts()
    stored_names = ctx.repo.node_names()

    async def send(text: str) -> ChatMessage | None:
        return await _with_restore(ctx, lambda: ctx.chat.send_post(room, text))

    async def resend(message: ChatMessage) -> ChatMessage:
        return await _with_restore(ctx, lambda: ctx.chat.resend_post(room, message))

    async def ask() -> str | None:
        joined = ctx.rooms.joined(room)
        return await session.text(
            f"Log in to {room.name}" if joined else f"Join {room.name}",
            prompt="Room password (blank if the room already knows you):",
            help_text="Ask whoever runs the room. One left on its stock settings uses hello.",
            password=True,
            floating=True,
        )

    screen = RoomScreen(
        conversation,
        board,
        send=send,
        names=_contact_names(contacts),
        session=session,
        resolve=trace_runner.make_node_resolver(contacts, stored_names),
        password=lambda: ctx.rooms.password(room),
        ask=ask,
        log_in=lambda password: ctx.rooms.login(room, password),
        access=ctx.rooms.access(room),
        resend=resend,
        paths=await _make_paths_presenter(ctx, conversation, device),
        key_of=trace_runner.make_name_key_resolver(contacts, stored_names),
    )
    ctx.chat.set_active(conversation.key)

    def on_event(event: MeshEvent) -> None:
        message = event.message
        if message is not None and message.is_post and _belongs(message, conversation):
            screen.append(ChatMessage.from_message(message, peer_name=conversation.label))

    unsubscribe = ctx.events.subscribe(on_event, EventKind.MESSAGE)
    try:
        # Kept pushed for the visit (one round of it) so the join prompt can float over the
        # board the moment it is up, rather than over whatever the room was opened from.
        async with session.stay(screen) as visit:
            if not ctx.rooms.joined(room):
                screen.begin_login(ask=True)
            elif ctx.rooms.login_due(room):
                screen.begin_login()
            await visit.result()  # the board resolves only when the reader leaves it
    finally:
        unsubscribe()
        ctx.chat.set_active(None)
    return len(screen._messages)
