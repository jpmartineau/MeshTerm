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

    @staticmethod
    def _peer(room: Contact) -> str:
        """The room's key as its conversation stores it (see ``Conversation.peer``)."""
        return (room.key_prefix or room.public_key[:12] or "").lower()

    def password(self, room: Contact) -> str | None:
        """The password to log in to ``room`` with, or ``None`` when we hold none.

        The room password first — it is what a member was given, and what got us in —
        then the room's admin password, which Repeater admin may already remember and
        which gets us in as its admin. ``""`` is a password (an open room).
        """
        stored = self._ctx.room_store.password(room) if self._ctx.room_store else None
        if stored is not None:
            return stored
        return self._ctx.admin_store.get(room)

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
        """Whether opening ``room`` should log in to it: a password at hand and a quiet room."""
        return self.joined(room) and not self.heard_recently(room)

    async def login(self, room: Contact, password: str) -> RoomLogin:
        """Log in to ``room`` with ``password`` — one exchange — and remember the outcome.

        An accepted login is remembered the way each password is kept: the access and the
        room password in the room store, an admin password in the admin store (shared with
        Repeater admin, which will log in with it silently from now on). A login the node
        *refused* forgets the password that was tried; one that met silence keeps
        everything, since a room also stays silent for a wrong password and only a
        password that once worked is ever stored.

        Args:
            room: The room server to log in to.
            password: The password to offer (``""`` asks the room whether it knows us).

        Returns:
            How the login ended, and the access granted.

        Raises:
            Exception: Propagates a device error (no connection, a contact the companion
                cannot address); nothing is remembered then.
        """
        device = await self._ctx.device()
        login = await device.room_login(room, password)
        if self._ctx.room_store is not None:
            self._ctx.room_store.record(room, password, login)
        if login.access is RoomAccess.ADMIN:
            self._ctx.admin_store.record(room, password, LoginResult.ACCEPTED)
        elif login.result is LoginResult.REFUSED and password == self._ctx.admin_store.get(room):
            self._ctx.admin_store.record(room, password, login.result)
        if login and login.access is not None:
            peer = self._peer(room)
            self._access[peer] = login.access
            self._heard[peer] = time.monotonic()
        return login
