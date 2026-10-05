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
  ``admin``, ``read-only``) or where the login stands. Joining is the Rooms page's
  (:mod:`meshterm.ui.rooms`); here, opening a joined room logs in to it again only when it
  has been quiet (:meth:`~meshterm.services.rooms.RoomService.login_due`), quietly and with
  the board open to read, and ^L runs the explained login on demand.
* **Posting waits for the room.** A room drops a post from anyone it doesn't know — or
  knows read-only — without a word, so the compose line opens only once we are in and may
  post, and until then says why not and what to press.
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
        auto_login: Callable[[], Awaitable[RoomLogin | None]],
        join: Callable[[bool], Awaitable[RoomLogin | None]],
        joined: bool = True,
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
            auto_login: Logs in quietly with the remembered password — what opening a quiet
                room does; ``None`` when there is none to log in with.
            join: The explained login flow (:func:`~meshterm.ui.rooms.join_room`), its
                dialogs floating over the board; called with whether to ask for the password
                even when one is remembered. ``None`` when the reader backed out.
            joined: Whether the room has been joined. A board opened on one that hasn't
                (its stored posts, read from the Rooms page) takes no posts.
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
        self._auto_login = auto_login
        self._join = join
        self._access = access
        #: Where the login stands, when that is what the title should say instead of the
        #: access (one of the module's atoms); ``None`` lets the access speak.
        self._login_state: str | None = None if joined else NOT_JOINED
        self._login_open = False
        self._apply_access()

    # --- the login ----------------------------------------------------------------------

    def begin_login(self, *, ask: bool = False) -> None:
        """^L: the explained login flow, its prompt and its outcome floating over the board.

        Args:
            ask: Ask for the password even when one is remembered — what a read-only member
                needs, the remembered one having earned no more than that.
        """
        if self._login_open:
            return
        self._login_open = True
        self._session.run_detached(self._settle(self._join(ask)))

    def begin_auto_login(self) -> None:
        """Opening a quiet room: log in with what is remembered, the board open meanwhile.

        No dialogs — the reader asked for the board, not for a login — so the title says
        ``logging in…`` while it runs, and the compose line waits for the answer: a room
        that has forgotten us drops whatever we post, so posting opens only once it has let
        us in. Silence shows in the same two places, and ^L is the explained retry.
        """
        if self._login_open:
            return
        self._login_open = True
        self._login_state = LOGGING_IN
        self._apply_access()
        self._session.run_detached(self._settle(self._auto_login()))

    async def _settle(self, attempt: Awaitable[RoomLogin | None]) -> None:
        """Await one login attempt, and show how it ended."""
        try:
            login = await attempt
        except Exception as exc:  # noqa: BLE001 - shown on the board, which stays open
            self._login_state = FAILED
            self._status = f"login failed: {exc}"
        else:
            if login is not None:
                self._apply_login(login)
            elif self._login_state == LOGGING_IN:
                self._login_state = None  # nothing to log in with after all
        finally:
            self._login_open = False
            self._apply_access()
            self._session.invalidate()

    def _apply_login(self, login: RoomLogin) -> None:
        """Take in a login's outcome: the access it won, or why there is none."""
        self._status = ""
        if login.access is not None and login.result is LoginResult.ACCEPTED:
            self._access = login.access
            self._login_state = None
        elif login.result is LoginResult.REFUSED:
            self._login_state = REFUSED
        else:
            self._login_state = NO_REPLY

    def _apply_access(self) -> None:
        """Open the compose line only while we are in, and the room keeps what we post.

        Not while a login is on its way or has met silence: a room that has forgotten us —
        it restarted, or gave up on us — drops a post without a word, and a compose line
        that took one anyway was the one thing the board said that wasn't so.
        """
        self._composing = (
            self._login_state is None and self._access is not None and self._access.can_post
        )
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
        """The compose line — or, while there can be none, why not and what to press."""
        if self._composing:
            return super()._compose_line(width)
        # Each inside the handhelds' 53 columns, so the reason is one line everywhere.
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
        """Post the compose line, unless the room is known to drop our posts."""
        if self._composing:
            super()._submit()

    def handle(self, action: str, data: str = "") -> None:
        """^L logs in; everything else is the chat's."""
        if action == "login":
            # A read-only member's remembered password has earned all it can, so ^L asks
            # for another; everyone else's is tried first, the flow asking only if it fails.
            self.begin_login(ask=self._access is RoomAccess.READ_ONLY)
            self._session.invalidate()
            return
        super().handle(action, data)

    def _retry_target(self) -> ChatMessage | None:
        """An unacknowledged post to send again — never for a reader the room won't hear."""
        return super()._retry_target() if self._composing else None

    @property
    def footer_hint(self) -> str:
        """Key hint: the chat's, in a board's words, with ^L while a login could start."""
        if self._selected is not None:
            code = " · ^U QR" if self._picked_urls() else ""
            if not self._composing:  # nowhere to put a reply: Enter shows the post's paths
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
    """Open the room view for ``conversation`` and run it until the reader leaves.

    The board opens at once on what is stored. A joined room that has been quiet logs in
    with the password remembered for it — in the background, the board readable meanwhile —
    which restarts the room's catch-up; one heard from lately sends nothing at all. Joining
    is the Rooms page's (:mod:`meshterm.ui.rooms`), so a room not joined opens read-only on
    its stored posts, and ^L joins it. Posts that arrive while the board is open append
    live, and the room's unread count stays clear while the reader is on it.

    Args:
        ctx: The shared application context (running the interactive TUI surface).
        conversation: The room's conversation (:attr:`~Conversation.is_room`).

    Returns:
        The number of posts on the board when the view closed.
    """
    from ..services import trace_runner
    from .rooms import join_room

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

    async def fresh() -> Contact:
        """The room as the contact cache holds it now: its route and heard time move."""
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
        # Kept pushed for the visit (one round of it) so a login's dialogs float over the
        # board the moment it is up, rather than over whatever the room was opened from.
        async with session.stay(screen) as visit:
            if ctx.rooms.login_due(room):
                screen.begin_auto_login()
            await visit.result()  # the board resolves only when the reader leaves it
    finally:
        unsubscribe()
        ctx.chat.set_active(None)
    return len(screen._messages)
