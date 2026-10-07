# SPDX-License-Identifier: Apache-2.0
"""The room screen: the board of a room server, to read and to post to like a chat.

A room server keeps the most recent posts of its members. It sends each member the posts
that the member missed. To the user, this is a conversation with many voices, which is the
shape that a channel already has. Thus the screen *is* the chat screen
(:class:`~meshterm.ui.chat.ChatScreen`). It also shows what a room knows and a channel does
not: who wrote each post, by key and not by a name that somebody typed before it, and
whether the room will take what you post.

Three things make it different, and each one belongs to the room:

* **Authors are keys.** A room relays a post as a direct message from *itself*. The room
  signs the post with the first four bytes of the key of the author
  (:attr:`~meshterm.core.models.ChatMessage.author`). MeshTerm files the post under the
  name of the author when a contact or an overheard advert names that key. When nothing
  names the key, MeshTerm files the post under the bare hash, in the grey of an
  unidentified node. In both cases the colour comes from the key, never from a guess about
  the name.
* **To get in, you log in.** The title shows what the room let us do (``member``,
  ``admin``, ``read-only``) or the state of the login. The Rooms page joins a room
  (:mod:`meshterm.ui.rooms`). Here, when the user opens a joined room, MeshTerm logs in to
  it again only if the room has been quiet
  (:meth:`~meshterm.services.rooms.RoomService.login_due`). It does this quietly, and the
  board is open to read. ^L runs the explained login when the user wants it.
* **Posting waits for the room.** A room drops, with no message, a post from anyone that it
  does not know, or that it knows at the read-only access. Thus the compose line opens only
  when we are logged in and can post. Until then, it says why it is not open and which key
  to press.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from rich.console import RenderableType
from rich.text import Text

from ..core.events import EventKind, MeshEvent
from ..core.models import (
    ChatMessage,
    Contact,
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
#: The title's atom for a board opened on a room not joined.
NOT_JOINED = "not joined"
#: The title's login atom after a login failed outright (the link, the radio).
FAILED = "login failed"


def author_label(author: str, resolve: NodeResolver) -> tuple[str, str | None]:
    """The name and the colour of the author of a post: the name and key, or the bare hash.

    This is the only rule for an author. The room screen and the preview of the Chat picker
    share it, so that they cannot give two different names to one person. A contact or an
    overheard advert can carry a key prefix. MeshTerm resolves such a key prefix to the
    name of that node, and the key itself gives the colour. A key prefix that nothing names
    stands as the hash, with no key to give a colour. It has the grey of an unidentified
    node, because that grey is for a bare hash that stands in as a name.

    Args:
        author: The key prefix of the author, as the post carried it.
        resolve: Gives the name of a node for a key prefix, or gives the key prefix back
            when it is not known (refer to
            :func:`~meshterm.services.trace_runner.make_node_resolver`).

    Returns:
        ``(label, key)``: the name and the key that gives its colour, or the hash and
        ``None``.
    """
    name = resolve(author)
    if not name or name.lower() == author.lower():
        return author, None
    return name, author


class RoomScreen(ChatScreen):
    """The board of a room: posts under their authors, a compose line, and the login state.

    It does everything that a chat does. When the user selects a post, Enter replies with an
    ``@mention``. ^P shows the paths that a post took. ^R tries again a post that the room
    did not acknowledge. It also has ^L, which logs in to the room. It uses the remembered
    password, or it asks for a password when none is remembered or when the last login
    got no answer.
    """

    _empty_text = "No posts yet"

    @property
    def picocalc_lyra_lane(self):
        """The lane of the chat, with *Log in* on the Shift half of F1 (F6), where ^L has no key.

        Each chord that the desktop has needs a chip on the handheld. The Shift half of F1
        is the one that the chat leaves free. The chip is dim while a login is in progress.
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
        auto_login: Callable[[], Awaitable[RoomLogin | None]],
        join: Callable[[bool], Awaitable[RoomLogin | None]],
        joined: bool = True,
        access: RoomAccess | None = None,
        resend: Callable[[ChatMessage], Awaitable[ChatMessage]] | None = None,
        paths: Callable[[ChatMessage], Awaitable[None]] | None = None,
        key_of: Callable[[str], str | None] | None = None,
    ) -> None:
        """Build the room screen.

        Args:
            conversation: The conversation of the room (its label is the title of the
                screen).
            messages: The board as stored, oldest first, with posts only (refer to
                :meth:`~meshterm.persistence.repository.Repository.recent_chat_messages`).
            send: Posts a line to the room and returns the stored post.
            names: Contact key prefix to name (the chat screen labels a peer with it).
            session: The running :class:`~meshterm.ui.tui.session.TuiSession`.
            resolve: Gives the name for the key prefix of an author (refer to
                :func:`author_label`).
            auto_login: Logs in quietly with the remembered password. This is what happens
                when the user opens a quiet room. It is ``None`` when there is no password
                to log in with.
            join: The explained login flow (:func:`~meshterm.ui.rooms.join_room`), with its
                dialogs floating over the board. MeshTerm calls it with a flag that says
                if it must ask for the password also when one is remembered. It returns
                ``None`` when the user backed out.
            joined: Whether the user joined the room. A board that is opened on a room that
                is not joined (its stored posts, read from the Rooms page) takes no posts.
            access: What the room let us do when we last logged in, if we ever logged in.
            resend: Posts again a post that the room did not acknowledge (^R).
            paths: Shows the paths that a selected post took (^P).
            key_of: Gives the key of its node for a name, for the hues of ``@mention``.
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
        self._auto_login = auto_login
        self._join = join
        self._access = access
        #: The state of the login, when the title must say this and not the access (one of
        #: the atoms of the module). If it is ``None``, the title shows the access.
        self._login_state: str | None = None if joined else NOT_JOINED
        self._login_open = False
        self._apply_access()

    # --- the login ----------------------------------------------------------------------

    def begin_login(self, *, ask: bool = False) -> None:
        """^L: the explained login flow, with its prompt and its result floating over the board.

        Args:
            ask: Ask for the password also when one is remembered. A member at the read-only
                access needs this, because the remembered password gave no more than that.
        """
        if self._login_open:
            return
        self._login_open = True
        self._session.run_detached(self._settle(self._join(ask)))

    def begin_auto_login(self) -> None:
        """The user opens a quiet room: log in with what is remembered, with the board open.

        There are no dialogs, because the user asked for the board and not for a login. The
        title says ``logging in…`` while the login runs, and the compose line waits for the
        answer. A room that has forgotten us drops each post that we send. Thus the compose
        line opens only when the room has let us in. If the room does not answer, this shows
        in the same two places, and ^L is the explained retry.
        """
        if self._login_open:
            return
        self._login_open = True
        self._login_state = LOGGING_IN
        self._apply_access()
        self._session.run_detached(self._settle(self._auto_login()))

    async def _settle(self, attempt: Awaitable[RoomLogin | None]) -> None:
        """Wait for one login attempt, and show how it ended."""
        try:
            login = await attempt
        except Exception as exc:  # noqa: BLE001 - shown on the board, which stays open
            self._login_state = FAILED
            self._status = f"login failed: {exc}"
        else:
            if login is not None:
                self._apply_login(login)
            elif self._login_state == LOGGING_IN:
                self._login_state = None  # there was nothing to log in with
        finally:
            self._login_open = False
            self._apply_access()
            self._session.invalidate()

    def _apply_login(self, login: RoomLogin) -> None:
        """Take the result of a login: the access that it gave, or the reason for no access."""
        self._status = ""
        if login.access is not None and login.result is LoginResult.ACCEPTED:
            self._access = login.access
            self._login_state = None
        elif login.result is LoginResult.REFUSED:
            self._login_state = REFUSED
        else:
            self._login_state = NO_REPLY

    def _apply_access(self) -> None:
        """Open the compose line only while we are logged in and the room keeps what we post.

        The line stays closed while a login is in progress or got no answer. A room that has
        forgotten us (it restarted, or it stopped waiting for us) drops a post with no
        message. A compose line that took a post in that state was the one thing on the
        board that was not true.
        """
        self._composing = (
            self._login_state is None and self._access is not None and self._access.can_post
        )
        self._retitle()

    def _retitle(self) -> None:
        """Give the board a title: the room and its one status atom (``Room · member``)."""
        atom = self._login_state or (self._access.value if self._access else None)
        self.title = f"{self._label} · {atom}" if atom else self._label

    # --- the board ----------------------------------------------------------------------

    def append(self, message: ChatMessage) -> None:
        """Add a post that just arrived, one time, also if the room sends it more than once.

        A room sends a post again when it did not hear an acknowledgement. Thus the same
        post can arrive twice. A room also sends other traffic under its key (the replies
        to the commands of an admin). That traffic is not on the board.
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
        """The author of a post (by name, or by hash) and its text. Our post has ``you``."""
        if message.outbound:
            return "you", message.text
        if message.author:
            return author_label(message.author, self._resolve)[0], message.text
        return self._label, message.text

    def _header_key(self, sender: str, message: ChatMessage) -> str | None:
        """The key of the author gives the colour of the chip. A hash that nothing names is grey."""
        if message.author:
            return author_label(message.author, self._resolve)[1]
        return super()._header_key(sender, message)

    def _compose_line(self, width: int) -> RenderableType:
        """The compose line, or, when there can be none, the reason and the key to press."""
        if self._composing:
            return super()._compose_line(width)
        # Each reason is inside the 53 columns of the handhelds, so it is one line everywhere.
        why = {
            LOGGING_IN: "logging in… posting opens once the room lets you in",
            NO_REPLY: "not logged in: no answer from the room · ^L to retry",
            REFUSED: "not logged in: the login was refused · ^L to retry",
            FAILED: "not logged in · ^L to try again",
            NOT_JOINED: "not joined · ^L to join",
        }.get(self._login_state or "")
        if why is None:
            why = (
                "read-only — the room drops your posts · ^L to log in"
                if self._access is RoomAccess.READ_ONLY
                else "not logged in · ^L to log in"
            )
        return Text(why, style="muted")

    def _submit(self) -> None:
        """Post the compose line, unless we know that the room drops our posts."""
        if self._composing:
            super()._submit()

    def handle(self, action: str, data: str = "") -> None:
        """^L logs in. The chat handles all the other actions."""
        if action == "login":
            # The remembered password of a member at the read-only access gave all that it
            # can, so ^L asks for another password. For each other member, the flow tries
            # the remembered password first and asks only if it fails.
            self.begin_login(ask=self._access is RoomAccess.READ_ONLY)
            self._session.invalidate()
            return
        super().handle(action, data)

    def _retry_target(self) -> ChatMessage | None:
        """A post that the room did not acknowledge, to send again.

        There is none for a user whom the room does not hear.
        """
        return super()._retry_target() if self._composing else None

    @property
    def footer_hint(self) -> str:
        """The hint of the chat, in the words of a board, with ^L when a login can start."""
        if self._selected is not None:
            code = " · ^U QR" if self._picked_urls() else ""
            if not self._composing:  # no place for a reply: Enter shows the paths of the post
                return f"Enter paths{code} · ↑↓ pick · ^End/Esc cancel"
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
    """Open the room screen for ``conversation`` and run it until the user leaves.

    The board opens immediately on what is stored. A joined room that has been quiet logs in
    with the password that is remembered for it. This happens in the background, and the
    board is readable. The login starts the catch-up of the room again. A room that MeshTerm
    heard from recently sends nothing. The Rooms page joins a room
    (:mod:`meshterm.ui.rooms`). Thus a room that is not joined opens only to read its
    stored posts, and ^L joins it. Posts that arrive while the board is open are added at
    once, and the unread count of the room stays clear while the user is on the board.

    Args:
        ctx: The shared application context (which runs the interactive TUI surface).
        conversation: The conversation of the room (:attr:`~Conversation.is_room`).

    Returns:
        The number of posts on the board when the screen closed.
    """
    from ..services import trace_runner
    from .rooms import join_room

    session = ctx.ui.session
    room = conversation.contact
    assert room is not None
    device = await ctx.device()
    try:
        await ctx.chat.start()  # store the posts that arrive, if nothing has started it yet
    except Exception:  # noqa: BLE001 - the hub can already be running. This is best effort.
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

    async def fresh() -> Contact:
        """The room as the contact cache holds it now. Its route and heard time change."""
        held = await ctx.devstate.contacts()
        key = room.public_key or room.key_prefix
        return next((c for c in held if (c.public_key or c.key_prefix) == key), room)

    async def auto_login() -> RoomLogin | None:
        password = ctx.rooms.password(room)
        return None if password is None else await ctx.rooms.login(room, password)

    async def join(ask: bool) -> RoomLogin | None:
        return await join_room(ctx, await fresh(), ask=ask)

    screen = RoomScreen(
        conversation,
        board,
        send=send,
        names=_contact_names(contacts),
        session=session,
        resolve=trace_runner.make_node_resolver(contacts, stored_names),
        auto_login=auto_login,
        join=join,
        joined=ctx.rooms.joined(room),
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
        # The screen stays pushed for the visit (one round of it). Thus the dialogs of a
        # login float over the board when it is up, and not over the screen from which the
        # room was opened.
        async with session.stay(screen) as visit:
            if ctx.rooms.login_due(room):
                screen.begin_auto_login()
            await visit.result()  # the board resolves only when the user leaves it
    finally:
        unsubscribe()
        ctx.chat.set_active(None)
    return len(screen._messages)
