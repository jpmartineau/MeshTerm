# SPDX-License-Identifier: Apache-2.0
"""Tell whether a repeater relayed a channel message that we sent (issue #38).

A channel broadcast has no acknowledgement. But the device hears the relay of our
broadcast by each repeater in range, and its RX log records each copy with the path that
the copy went through (refer to :mod:`~meshterm.services.message_paths`). Each hop in that
path heard the message. Thus a copy that comes back is the proof that a repeater heard the
message and sent it on.

Each relay counts, the repeater next to us too. A first version counted only the relays
past the first hops of the outbound paths. In practice, nothing past them ever came back:
each repeater relays a packet one time, and the relays farther out are too far for our
radio to hear (JP, 2026-11-20, from 23 of 23 messages on his Public channel).

The verdict of a message has three states, the same as the delivery of a direct message:

* :data:`WAITING`: no copy came back yet, and the wait (``heard_wait_s``) is not over. The
  chat shows a spinner.
* :data:`HEARD`: one or more relays sent a copy back. The chat shows ``✓`` and the number
  of the relays.
* :data:`UNHEARD`: after the wait, no copy came back. The chat shows ``✗``.

A copy that comes back late changes the verdict, as a late acknowledgement does for a
direct message. After the match window of the packet log
(:data:`~meshterm.services.message_paths.CHANNEL_WINDOW`), nothing can change it, and
MeshTerm stores the paths, so that the next visit does not decrypt the log again.

Two limits stay. MeshTerm sees only the relays that our radio hears. And MeshTerm must be
connected while the copies come back.

Nothing in this module transmits. It is a read-model over the repository.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from ..core.models import ChatMessage, utcnow
from .message_paths import CHANNEL_WINDOW, Arrival, channel_arrivals

if TYPE_CHECKING:
    from ..context import AppContext
    from ..persistence.repository import Repository

#: The verdict states (refer to the module docstring).
WAITING = "waiting"
HEARD = "heard"
UNHEARD = "unheard"

#: The number of messages that :meth:`HeardTracker.fill` examines before each yield to the
#: event loop. Each one is a query of the packet log and some decryptions.
_CHUNK = 4

#: A relay path, as hop hashes in the order of propagation.
RelayPath = tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HeardBy:
    """The verdict on one channel message that we sent.

    Attributes:
        state: :data:`WAITING`, :data:`HEARD`, or :data:`UNHEARD`.
        relays: The hashes of the relays in the copies, in the order first heard. Each one
            heard the message.
        copies: The number of copies that came back.
    """

    state: str
    relays: tuple[str, ...] = ()
    copies: int = 0


def relay_paths(arrivals: Iterable[Arrival]) -> list[RelayPath]:
    """The paths of the copies that tell which relays heard the message.

    A copy with no hops tells nothing (a flood of ours always comes back through a relay),
    and a direct-routed copy that used all its route tells nothing either (refer to
    :attr:`~meshterm.services.message_paths.Arrival.route_known`).
    """
    return [a.hops for a in arrivals if a.hops and a.route_known]


def judge(paths: Sequence[RelayPath], *, age_s: float, wait_s: float) -> HeardBy:
    """The verdict on a message from the paths of its copies.

    Args:
        paths: The relay paths of the copies that came back, in the order heard.
        age_s: The time since the send, in seconds.
        wait_s: The wait before "no relay" becomes the verdict (``heard_wait_s``).

    Returns:
        The verdict.
    """
    relays: list[str] = []
    for path in paths:
        for hop in path:
            if hop.lower() not in relays:
                relays.append(hop.lower())
    if relays:
        state = HEARD
    elif age_s < wait_s:
        state = WAITING
    else:
        state = UNHEARD
    return HeardBy(state, tuple(relays), len(paths))


@dataclass(slots=True)
class _Record:
    """What the tracker knows about one message: the copies so far, and if more can come."""

    paths: list[RelayPath] = field(default_factory=list)
    settled: bool = False


class HeardTracker:
    """The verdicts on our channel messages in one conversation, kept current.

    The chat screen reads :meth:`verdict` at each paint, and that read never touches the
    packet log. The work is in :meth:`fill`, which runs in the background when the screen
    opens, and in :meth:`refresh`, which the screen calls when the device hears a channel
    packet. A message that is out of the match window is **settled**: MeshTerm stores its
    paths one time, and reads them back on the next visit.
    """

    def __init__(
        self,
        repo: Repository,
        *,
        channel_name: str,
        secret: bytes | None,
        wait_s: Callable[[], float],
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        """Make a tracker for one channel.

        Args:
            repo: The repository with the packet log and the stored verdicts.
            channel_name: The name of the channel (for the decryption).
            secret: The key of the channel. With no key, nothing can be matched, and each
                verdict is ``None``.
            wait_s: Gives the ``heard_wait_s`` preference, read at each verdict.
            clock: Gives the current time (a fixed clock in the tests).
        """
        self._repo = repo
        self._channel_name = channel_name
        self._secret = secret
        self._wait_s = wait_s
        self._clock = clock
        self._records: dict[int, _Record] = {}
        self._revision = 0

    @property
    def revision(self) -> int:
        """A number that changes each time that a verdict can have changed."""
        return self._revision

    def _ours(self, message: ChatMessage) -> bool:
        return bool(self._secret and message.outbound and message.is_channel and message.row_id)

    def _age_s(self, message: ChatMessage) -> float:
        return (self._clock() - message.created_at).total_seconds()

    def verdict(self, message: ChatMessage) -> HeardBy | None:
        """The verdict on one message, or ``None`` when there is none to show.

        ``None`` is for a message that is not our own channel message, and for one that
        :meth:`fill` did not examine yet.
        """
        if not self._ours(message):
            return None
        record = self._records.get(message.row_id)  # type: ignore[arg-type]
        if record is None:
            return None
        return judge(record.paths, age_s=self._age_s(message), wait_s=self._wait_s())

    def note_sent(self, message: ChatMessage) -> None:
        """Start the verdict of a message that was sent while the screen is open."""
        if self._ours(message):
            self._records[message.row_id] = _Record()  # type: ignore[index]
            self._revision += 1

    async def fill(self, messages: Sequence[ChatMessage]) -> None:
        """Examine each of our channel messages, the newest first, in small steps.

        The settled verdicts come from storage in one query. Each other message costs a
        query of the packet log, so the work yields to the event loop after each few.

        Args:
            messages: The transcript of the conversation.
        """
        ours = [m for m in messages if self._ours(m)]
        stored = self._repo.heard_records([m.row_id for m in ours])  # type: ignore[misc]
        for message in ours:
            paths = stored.get(message.row_id)  # type: ignore[arg-type]
            if paths is not None:
                self._records[message.row_id] = _Record(paths=list(paths), settled=True)  # type: ignore[index]
        self._revision += 1
        todo = [m for m in reversed(ours) if m.row_id not in self._records]
        for start in range(0, len(todo), _CHUNK):
            for message in todo[start : start + _CHUNK]:
                self._examine(message)
            self._revision += 1
            await asyncio.sleep(0)

    def refresh(self, messages: Sequence[ChatMessage]) -> bool:
        """Examine again the messages that are not settled, after a new channel packet.

        Args:
            messages: The transcript of the conversation.

        Returns:
            ``True`` when a verdict can have changed.
        """
        changed = False
        for message in messages:
            if not self._ours(message):
                continue
            record = self._records.get(message.row_id)  # type: ignore[arg-type]
            if record is None or record.settled:
                continue
            before = list(record.paths)
            self._examine(message)
            changed = changed or self._records[message.row_id].paths != before  # type: ignore[index]
        if changed:
            self._revision += 1
        return changed

    def waiting(self, messages: Sequence[ChatMessage]) -> bool:
        """Whether one of the messages shows the spinner now."""
        return any((v := self.verdict(m)) is not None and v.state == WAITING for m in messages)

    def _examine(self, message: ChatMessage) -> None:
        """Match the copies of one message in the packet log, and settle it if it is old."""
        assert self._secret is not None and message.row_id is not None
        arrivals = channel_arrivals(
            self._repo, message, channel_name=self._channel_name, secret=self._secret
        )
        paths = relay_paths(arrivals)
        settled = self._age_s(message) > CHANNEL_WINDOW.total_seconds()
        self._records[message.row_id] = _Record(paths=paths, settled=settled)
        if settled:
            self._repo.settle_heard(message.row_id, paths)


def make_tracker(
    ctx: AppContext, *, channel_name: str, secret: bytes | None
) -> HeardTracker | None:
    """A tracker for one channel.

    Args:
        ctx: The shared application context (the repository and the preferences).
        channel_name: The name of the channel.
        secret: The key of the channel. With no key, there is no tracker.

    Returns:
        The tracker, or ``None`` when the channel has no key at hand.
    """
    if not secret:
        return None
    return HeardTracker(
        ctx.repo,
        channel_name=channel_name,
        secret=secret,
        wait_s=lambda: float(ctx.preferences.heard_wait_s),
    )
