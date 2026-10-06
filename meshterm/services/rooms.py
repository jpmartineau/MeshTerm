# SPDX-License-Identifier: Apache-2.0
"""The room service: join room servers, and know when to log in to a room server again.

A room server is a message board on a radio. It keeps its most recent posts. After a member
logs in, the room server sends each post that the member did not get yet, one at a time,
and waits for the acknowledgement before it sends the next. Then it sends each new post when
it is written. All that *arrives* from a room is an ordinary inbound message, and the
:class:`~meshterm.services.chat_service.ChatService` stores it as any other message. This
service owns the other half, the part that *asks*: the login, the passwords that it uses,
and the access that it got.

The session state is small, and this is intentional. A login is one exchange on the air. A
room must have a login only when it may have stopped sending: after it forgot us (a restart
removes all members except its admins), or after it stopped sending to us (three posts with
no acknowledgement). Thus the service remembers when each room was last *heard*: a login
that it accepted, or a post that it sent. When the user opens a room again that was heard
within ``preferences.room_relogin_minutes``, the service does not log in. No code here ever
transmits by itself: a login occurs because the user opened a room or asked for one, never
in the background.

State for the session, on the :class:`~meshterm.context.AppContext` (``ctx.rooms``).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from ..core.models import Contact, LoginResult, RoomAccess, RoomLogin

if TYPE_CHECKING:
    from ..context import AppContext


class RoomService:
    """Joins rooms, remembers what they let us do, and decides when to log in again."""

    def __init__(self, ctx: AppContext) -> None:
        """Bind the service to an application context.

        Args:
            ctx: The shared application context (device, stores, preferences).
        """
        self._ctx = ctx
        #: The access that each room gave at a login *in this session*, indexed by room peer.
        self._access: dict[str, RoomAccess] = {}
        #: The time at which each room was last heard in this session (a login or a post),
        #: on the monotonic clock, indexed by room peer.
        self._heard: dict[str, float] = {}
        #: The login in progress to each room, and the password that it carries, indexed by
        #: peer.
        self._inflight: dict[str, tuple[asyncio.Future, str]] = {}

    @staticmethod
    def _peer(room: Contact) -> str:
        """The key prefix of the room, as its conversation stores it (``Conversation.peer``)."""
        return (room.key_prefix or room.public_key[:12] or "").lower()

    def password(self, room: Contact) -> str | None:
        """The password with which to log in to ``room``, or ``None`` when we have none.

        First, the admin password of the room, when Repeater admin (or a join) remembers
        one: it gives us all that the room password gives, and more. The order is
        important. A login to a room *replaces* the role of a known member with the role
        that the password gives. Thus an owner who logged in with the room password
        becomes a member until their next admin login. Then, the room password. ``""`` is
        a password (a blank login that the room accepted).
        """
        admin = self._ctx.admin_store.get(room)
        if admin:
            return admin
        return self._ctx.room_store.password(room) if self._ctx.room_store else None

    def joined(self, room: Contact) -> bool:
        """True if we joined ``room``: a login to it was accepted and not forgotten after that.

        The Rooms page joins rooms, and a join puts a room in Chat, as a channel is in Chat
        when it is on a slot. An admin password that Repeater admin remembers is not a
        join. It only lets a join occur without a question.
        """
        store = self._ctx.room_store
        return store is not None and store.membership(room) is not None

    def worked_before(self, room: Contact, password: str) -> bool:
        """True if ``password`` let us into ``room`` before.

        This is the only evidence about the password that a silent room gives. A password
        that worked the last time is still correct, unless the owner changed it. Thus the
        silence then shows that the room does not hear us. It does not show a wrong
        password.
        """
        if self._ctx.admin_store.get(room) == password and password:
            return True
        store = self._ctx.room_store
        return store is not None and self.joined(room) and store.password(room) == password

    def forget(self, room: Contact) -> None:
        """Leave ``room``, as much as MeshTerm can: stop the logins, and forget its password.

        The room continues to send to our node until it restarts or stops sending to us,
        because a member cannot resign. Its posts that are already stored here are kept.
        An admin password that Repeater admin remembers for the room belongs to Repeater
        admin, and stays.
        """
        if self._ctx.room_store is not None:
            self._ctx.room_store.forget(room)
        peer = self._peer(room)
        self._access.pop(peer, None)
        self._heard.pop(peer, None)

    def access(self, room: Contact) -> RoomAccess | None:
        """What ``room`` lets us do: as given in this session, else at our last login, if any."""
        granted = self._access.get(self._peer(room))
        if granted is not None:
            return granted
        return self._ctx.room_store.access(room) if self._ctx.room_store else None

    def heard(self, peer: str | None) -> None:
        """Note that a room sent us a post a moment ago, thus it still sends to us.

        The chat recorder calls this method for each room post that it stores.

        Args:
            peer: The key prefix of the room, as the sender of the post carried it.
        """
        if peer:
            self._heard[peer.lower()] = time.monotonic()

    def heard_recently(self, room: Contact) -> bool:
        """True if ``room`` was heard within ``room_relogin_minutes``: then no login is necessary.

        A post that carries the prefix of the room at any width counts, because the prefix
        on the wire and the prefix in the contact table can have different lengths.
        """
        peer = self._peer(room)
        window = self._ctx.preferences.room_relogin_minutes * 60
        now = time.monotonic()
        return any(
            (known.startswith(peer) or peer.startswith(known)) and now - at < window
            for known, at in self._heard.items()
        )

    def login_due(self, room: Contact) -> bool:
        """True if the open of ``room`` must log in: joined, with a password, and quiet.

        Never while a login to the room is already in progress: the answer to that login is
        also the answer to this one.
        """
        return (
            self.joined(room)
            and self.password(room) is not None
            and not self.heard_recently(room)
            and self._peer(room) not in self._inflight
        )

    async def login(self, room: Contact, password: str, *, flood: bool = False) -> RoomLogin:
        """Log in to ``room`` with ``password`` (one exchange), and remember the result.

        The service remembers an accepted login in the same places where it keeps each
        password. The access and the room password go in the room store
        (:meth:`~meshterm.core.room_store.RoomStore.record` says what each type of
        acceptance proves). An admin password that the user typed goes in the admin store,
        which Repeater admin shares. From then on, Repeater admin logs in with it silently.
        If the node *refused* a login, the service forgets the password that was tried. If
        a login got no answer, the service keeps everything, because a room also stays
        silent for a wrong password, and only a password that worked one time is ever
        stored.

        Only one login to a room at a time, from any caller. A login can use most of its
        reply budget. Without this rule, a room screen that the user closes and opens again
        during that time sends a second login. The room answers both logins with a new
        start of its catch-up. A caller that asks with the password that is already in
        progress shares the answer of that login. A caller with a different password waits
        until that login finishes, then sends its own.

        Args:
            room: The room server to log in to.
            password: The password to give (``""`` asks the room if it knows us).
            flood: First, forget the route to the room that the device learned, so that the
                login floods the full mesh. This is the way around a route that is no
                longer valid, which is silent exactly as a wrong password is. To forget the
                route is a local action. The login is still the one transmission.

        Returns:
            How the login ended, the access given, and how the device sent it.

        Raises:
            Exception: A device error goes up to the caller (no connection, or a contact
                that the companion cannot address). Then nothing is remembered.
        """
        peer = self._peer(room)
        running = self._inflight.get(peer)
        if running is not None:
            task, sent = running
            if sent == password and not flood:
                return await asyncio.shield(task)
            try:
                await asyncio.shield(task)
            except Exception:  # noqa: BLE001 - its own caller gets the failure
                pass
        task = asyncio.ensure_future(self._login_once(room, password, flood=flood))
        self._inflight[peer] = (task, password)
        # Cleared when the exchange ends, not when this caller stops waiting. If the user
        # closes a room screen during the login, the login continues, and it is still the
        # login in progress.
        task.add_done_callback(lambda done: self._settle(peer, done))
        return await asyncio.shield(task)

    def _settle(self, peer: str, task: asyncio.Future) -> None:
        """Forget a finished login as the login in progress.

        Also get its failure, which no caller awaits.
        """
        if self._inflight.get(peer, (None, None))[0] is task:
            del self._inflight[peer]
        if not task.cancelled():
            task.exception()

    async def _login_once(self, room: Contact, password: str, *, flood: bool) -> RoomLogin:
        """One login exchange, and what its result means for the stores and the session."""
        device = await self._ctx.device()
        if flood:
            await device.reset_route(room)
            # The route in the contact cache is not valid now. The next read gets it again
            # (with the route that the device learns from the answer to this login).
            devstate = getattr(self._ctx, "devstate", None)
            if devstate is not None:
                devstate.invalidate_contacts()
        login = await device.room_login(room, password)
        if self._ctx.room_store is not None:
            self._ctx.room_store.record(room, password, login)
        if login.access is RoomAccess.ADMIN and password:
            # A blank login proves that the room knows us. It does not prove the admin password.
            self._ctx.admin_store.record(room, password, LoginResult.ACCEPTED)
        elif login.result is LoginResult.REFUSED and password == self._ctx.admin_store.get(room):
            self._ctx.admin_store.record(room, password, login.result)
        if login and login.access is not None:
            peer = self._peer(room)
            self._access[peer] = login.access
            self._heard[peer] = time.monotonic()
        return login
