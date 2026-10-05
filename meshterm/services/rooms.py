# SPDX-License-Identifier: Apache-2.0
"""The room service: joining room servers, and knowing when to log in to one again.

A room server is a bulletin board on a radio. It keeps its most recent posts and, once a
member has logged in, sends each post that member hasn't seen — one at a time, waiting for
the acknowledgement before the next — and then each new one as it is written. Everything
that *arrives* from a room is an ordinary inbound message, recorded by the
:class:`~meshterm.services.chat_service.ChatService` like any other; this service owns the
other half, the part that *asks*: the login, the passwords it uses, and the access it won.

The session state is small and deliberately so. A login is one exchange on the air, and a
room needs one only when it may have stopped sending: after it forgot us (a restart drops
every member but its admins) or gave up on us (three unacknowledged posts). So the service
remembers when each room was last *heard* — a login it accepted, or a post it sent — and a
room heard within ``preferences.room_relogin_minutes`` is left alone when it is opened again.
Nothing here ever transmits on its own: a login happens because a reader opened a room or
asked for one, never in the background.

Session-scoped state on the :class:`~meshterm.context.AppContext` (``ctx.rooms``).
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
        #: The access each room granted at a login *this session*, keyed by room peer.
        self._access: dict[str, RoomAccess] = {}
        #: When each room was last heard from this session (a login or a post), on the
        #: monotonic clock, keyed by room peer.
        self._heard: dict[str, float] = {}
        #: The login on its way to each room, and the password it carries, keyed by peer.
        self._inflight: dict[str, tuple[asyncio.Future, str]] = {}

    @staticmethod
    def _peer(room: Contact) -> str:
        """The room's key as its conversation stores it (see ``Conversation.peer``)."""
        return (room.key_prefix or room.public_key[:12] or "").lower()

    def password(self, room: Contact) -> str | None:
        """The password to log in to ``room`` with, or ``None`` when we hold none.

        The room's admin password first, when Repeater admin (or a join) remembers one: it
        gets us everything the room password does and more. The order is not taste — a
        room's login *replaces* a known member's role with the one the password earns, so
        an owner who logged in with the room password would be demoted to member until
        their next admin login. Then the room password. ``""`` is a password (a blank
        login the room accepted).
        """
        admin = self._ctx.admin_store.get(room)
        if admin:
            return admin
        return self._ctx.room_store.password(room) if self._ctx.room_store else None

    def joined(self, room: Contact) -> bool:
        """Whether MeshTerm can log in to ``room`` without asking for a password."""
        return self.password(room) is not None

    def access(self, room: Contact) -> RoomAccess | None:
        """What ``room`` lets us do: as granted this session, else at our last login, if any."""
        granted = self._access.get(self._peer(room))
        if granted is not None:
            return granted
        return self._ctx.room_store.access(room) if self._ctx.room_store else None

    def heard(self, peer: str | None) -> None:
        """Note that a room just sent us a post, so it is still sending to us.

        Called by the chat recorder for every room post it stores.

        Args:
            peer: The room's key prefix, as the post's sender carried it.
        """
        if peer:
            self._heard[peer.lower()] = time.monotonic()

    def heard_recently(self, room: Contact) -> bool:
        """Whether ``room`` was heard within ``room_relogin_minutes``, so needs no login.

        A post carrying any width of the room's prefix counts, since the wire's prefix and
        the contact table's need not be the same length.
        """
        peer = self._peer(room)
        window = self._ctx.preferences.room_relogin_minutes * 60
        now = time.monotonic()
        return any(
            (known.startswith(peer) or peer.startswith(known)) and now - at < window
            for known, at in self._heard.items()
        )

    def login_due(self, room: Contact) -> bool:
        """Whether opening ``room`` should log in: a password at hand, and a quiet room.

        Never while a login to it is already on its way — that one's answer is this one's.
        """
        return (
            self.joined(room)
            and not self.heard_recently(room)
            and self._peer(room) not in self._inflight
        )

    async def login(self, room: Contact, password: str) -> RoomLogin:
        """Log in to ``room`` with ``password`` — one exchange — and remember the outcome.

        An accepted login is remembered the way each password is kept: the access and the
        room password in the room store (:meth:`~meshterm.core.room_store.RoomStore.
        record` says what each kind of acceptance proves), and an admin password typed in
        in the admin store, shared with Repeater admin, which logs in with it silently from
        then on. A login the node *refused* forgets the password that was tried; one that
        met silence keeps everything, since a room also stays silent for a wrong password
        and only a password that once worked is ever stored.

        One login to a room at a time, whoever asks. A login can take most of its reply
        budget, and a room view closed and opened again inside it would otherwise send a
        second one — both of which the room answers by restarting its catch-up. A caller
        asking with the password already on its way shares that login's answer; one with
        a different password waits for it to finish, then sends its own.

        Args:
            room: The room server to log in to.
            password: The password to offer (``""`` asks the room whether it knows us).

        Returns:
            How the login ended, and the access granted.

        Raises:
            Exception: Propagates a device error (no connection, a contact the companion
                cannot address); nothing is remembered then.
        """
        peer = self._peer(room)
        running = self._inflight.get(peer)
        if running is not None:
            task, sent = running
            if sent == password:
                return await asyncio.shield(task)
            try:
                await asyncio.shield(task)
            except Exception:  # noqa: BLE001 - its own caller hears about its failure
                pass
        task = asyncio.ensure_future(self._login_once(room, password))
        self._inflight[peer] = (task, password)
        # Cleared when the exchange ends, not when this caller stops waiting: a room view
        # closed mid-login leaves the login running, and it is still the one in flight.
        task.add_done_callback(lambda done: self._settle(peer, done))
        return await asyncio.shield(task)

    def _settle(self, peer: str, task: asyncio.Future) -> None:
        """Forget a finished login as the one in flight, retrieving a failure no one awaits."""
        if self._inflight.get(peer, (None, None))[0] is task:
            del self._inflight[peer]
        if not task.cancelled():
            task.exception()

    async def _login_once(self, room: Contact, password: str) -> RoomLogin:
        """One login exchange, and what its outcome means for the stores and the session."""
        device = await self._ctx.device()
        login = await device.room_login(room, password)
        if self._ctx.room_store is not None:
            self._ctx.room_store.record(room, password, login)
        if login.access is RoomAccess.ADMIN and password:
            # A blank login proves the room knows us, not what the admin password is.
            self._ctx.admin_store.record(room, password, LoginResult.ACCEPTED)
        elif login.result is LoginResult.REFUSED and password == self._ctx.admin_store.get(room):
            self._ctx.admin_store.record(room, password, login.result)
        if login and login.access is not None:
            peer = self._peer(room)
            self._access[peer] = login.access
            self._heard[peer] = time.monotonic()
        return login
