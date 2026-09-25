# SPDX-License-Identifier: Apache-2.0
"""The transmit lock: what keeps a channel's scope from leaking onto someone else's flood.

The firmware has no per-channel scope. What it has is a *session* scope — one setting on
the companion that every flood it sends goes out under, channel messages, flood DMs,
adverts and requests alike (see :meth:`~meshterm.core.connection.Device.set_flood_scope`).
A channel scope is therefore three commands, not one: set the session scope, send the
channel message, set it back. Anything else the app transmits *between* the first and the
third goes out under that channel's region too — a courier retry, a DM typed in another
screen, the weekly advert — and would be relayed only by the repeaters carrying a region
its sender never chose.

So every transmission MeshTerm asks of the companion holds this lock for the moment it
takes to hand the frame over, and a scoped channel send holds it across all three commands.
Nothing can be interleaved into the window, and the window is as short as three round
trips; an acknowledgement *wait* is not a transmission and is done outside the lock, so a
DM waiting eight seconds for its ack never holds a channel message up behind it.

The lock is **re-entrant per task**: the scoped send takes it, then calls the ordinary
channel send, which takes it again on its own account — and must not wait for itself.

One leak is out of any client's reach: a DM *received* inside the window is acknowledged
by the firmware on its own, and that ack is a flood under whatever the session scope is
at that instant. The window being three round trips long is the whole of the mitigation,
and it is what the official apps live with too.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class TransmitLock:
    """A task-re-entrant :class:`asyncio.Lock` for "something is going on the air now".

    Interact through :meth:`held`, an async context manager. A task already holding the
    lock enters again at once; any other task waits for the holder to leave.
    """

    def __init__(self) -> None:
        """Start unheld."""
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0

    @property
    def locked(self) -> bool:
        """Whether any task holds the lock right now."""
        return self._lock.locked()

    def held_by_me(self) -> bool:
        """Whether the calling task is the one holding the lock."""
        try:
            task = asyncio.current_task()
        except RuntimeError:  # no running loop: nobody here holds anything
            return False
        return task is not None and self._owner is task

    @asynccontextmanager
    async def held(self) -> AsyncIterator[None]:
        """Hold the lock for the body; re-enter at once if this task already holds it."""
        if self.held_by_me():
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        async with self._lock:
            self._owner = asyncio.current_task()
            self._depth = 1
            try:
                yield
            finally:
                self._owner = None
                self._depth = 0


__all__ = ["TransmitLock"]
