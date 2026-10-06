# SPDX-License-Identifier: Apache-2.0
"""The transmit lock: it keeps the scope of a channel off the floods of other senders.

The firmware has no scope for each channel. It has a *session* scope: one setting on the
companion, which applies to each flood that the companion sends. This includes channel
messages, flood DMs, adverts, and requests (refer to
:meth:`~meshterm.core.connection.Device.set_flood_scope`). Thus a channel scope is three
commands, not one: set the session scope, send the channel message, and set the scope
back. If the app transmits something else between the first and the third command, it
goes out with the region of that channel too. Examples are a courier retry, a DM that the
user typed on another screen, and the weekly advert. Then only the repeaters that carry
that region relay it, and its sender never chose that region.

Thus each transmission that MeshTerm asks of the companion holds this lock for the time
that it takes to give the command to the companion. A scoped channel send holds the lock
for all three commands. Nothing can go into the window between them, and the window is
only three round trips long. A wait for an acknowledgement is not a transmission, and it
occurs outside the lock. Thus, when a DM waits eight seconds for its ack, it never delays a
channel message.

The lock is **re-entrant for each task**. The scoped send takes the lock, then it calls
the usual channel send, which takes the lock again for itself. The channel send must not
wait for itself.

No client can prevent one leak. When a DM arrives at the companion in the window, the
firmware sends the acknowledgement itself. That ack is a flood with the session scope of
that instant. The only mitigation is that the window is only three round trips long. The
official apps have the same problem.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class TransmitLock:
    """A task-re-entrant :class:`asyncio.Lock` for "something is going on the air now".

    Use :meth:`held`, an async context manager. A task that already holds the lock enters
    again immediately. All other tasks wait until the holder releases the lock.
    """

    def __init__(self) -> None:
        """Start with no holder."""
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0

    @property
    def locked(self) -> bool:
        """Whether a task holds the lock now."""
        return self._lock.locked()

    def held_by_me(self) -> bool:
        """Whether the task that calls this method holds the lock."""
        try:
            task = asyncio.current_task()
        except RuntimeError:  # no loop runs, so no task here holds anything
            return False
        return task is not None and self._owner is task

    @asynccontextmanager
    async def held(self) -> AsyncIterator[None]:
        """Hold the lock for the body, or enter again immediately if this task holds it."""
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
