# SPDX-License-Identifier: Apache-2.0
"""Tell whether a channel message that we sent went past our own repeaters (issue #38).

A channel broadcast has no acknowledgement. But the device hears the relay of our
broadcast by each repeater in range, and its RX log records each copy with the path that
the copy went through (refer to :mod:`~meshterm.services.message_paths`). Each hop in that
path heard the message. Thus the copies that come back are the proof that somebody heard
it.

A copy from a repeater next to us proves little: it shows that the message left our
radio. The user names these repeaters already, as the first hops of the **outbound
paths** of the active site or of a fixed device (refer to
:mod:`~meshterm.core.outbound_store`). This module calls them the **near relays**. Each
other hop in a copy is a relay that is not near, and it proves that the message went
farther. A near relay is near by its identity, not by its place in a path: the path
``UpperSalaberry → Cartierville`` has two near relays at home, and no other relay.

A managed node is not a near relay. The user can manage a repeater on the other side of
the city, and a relay by that repeater is good proof that the message went out.

The verdict of a message has four states:

* :data:`WAITING`: no relay that is not near was heard yet, and the wait
  (``heard_wait_s``) is not over. The chat shows a spinner.
* :data:`BEYOND`: one or more relays that are not near heard it. The chat shows ``✓`` and
  the number of these relays.
* :data:`NEARBY`: after the wait, only near relays sent a copy back. The chat shows ``⚠``.
* :data:`UNHEARD`: after the wait, no copy came back. The chat shows ``✗``.

A copy that comes back late changes the verdict, as a late acknowledgement does for a
direct message. After the match window of the packet log
(:data:`~meshterm.services.message_paths.CHANNEL_WINDOW`), nothing can change it, and
MeshTerm stores the paths, so that the next visit does not decrypt the log again.

Two limits stay. MeshTerm sees only the relays that our radio hears, so a relay farther
away than that never shows. And MeshTerm must be connected while the copies come back.

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
BEYOND = "beyond"
NEARBY = "nearby"
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
        state: :data:`WAITING`, :data:`BEYOND`, :data:`NEARBY`, or :data:`UNHEARD`.
        beyond: The hashes of the relays that are not near, in the order first heard.
            Each one heard the message.
        via: The near relays that started the copies that went farther, in the order first
            heard. Thus the user sees which of the repeaters at home carried the message
            out.
        copies: The number of copies that came back.
    """

    state: str
    beyond: tuple[str, ...] = ()
    via: tuple[str, ...] = ()
    copies: int = 0


def near_relays(paths: Iterable[Sequence[str]]) -> tuple[str, ...]:
    """The first hop of each outbound path, without duplicates, in order.

    Args:
        paths: The outbound paths that apply
            (:attr:`~meshterm.core.outbound_store.OutboundChoice.paths`).

    Returns:
        The near relays, as lowercase hex.
    """
    out: list[str] = []
    for path in paths:
        if path:
            hop = str(path[0]).lower()
            if hop not in out:
                out.append(hop)
    return tuple(out)


def is_near(hop: str, near: Sequence[str]) -> bool:
    """Whether a hop of a path is one of the near relays.

    A hop in a packet path is a hash: one to three bytes of the key. A near relay is a
    longer key prefix. The hop matches when one starts with the other. When two
    repeaters share a one-byte hash, a relay that is not near can match a near relay. Then
    the count is too low, but it never claims a relay that did not occur.
    """
    hop = hop.lower()
    return bool(hop) and any(n.startswith(hop) or hop.startswith(n) for n in near)


def relay_paths(arrivals: Iterable[Arrival]) -> list[RelayPath]:
    """The paths of the copies that tell which relays heard the message.

    A copy with no hops tells nothing (a flood of ours always comes back through a relay),
    and a direct-routed copy that used all its route tells nothing either (refer to
    :attr:`~meshterm.services.message_paths.Arrival.route_known`).
    """
    return [a.hops for a in arrivals if a.hops and a.route_known]


def judge(
    paths: Sequence[RelayPath], near: Sequence[str], *, age_s: float, wait_s: float
) -> HeardBy:
    """The verdict on a message from the paths of its copies.

    Args:
        paths: The relay paths of the copies that came back, in the order heard.
        near: The near relays at the time of the send.
        age_s: The time since the send, in seconds.
        wait_s: The wait before "no relay" becomes the verdict (``heard_wait_s``).

    Returns:
        The verdict.
    """
    beyond: list[str] = []
    via: list[str] = []
    for path in paths:
        far = [hop.lower() for hop in path if not is_near(hop, near)]
        for hop in far:
            if hop not in beyond:
                beyond.append(hop)
        if far:
            for hop in path:
                if hop.lower() == far[0]:
                    break
                if hop.lower() not in via:
                    via.append(hop.lower())
    if beyond:
        state = BEYOND
    elif age_s < wait_s:
        state = WAITING
    elif paths:
        state = NEARBY
    else:
        state = UNHEARD
    return HeardBy(state, tuple(beyond), tuple(via), len(paths))


@dataclass(slots=True)
class _Record:
    """What the tracker knows about one message: its near relays, and the copies so far."""

    near: tuple[str, ...]
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
        near_now: tuple[str, ...],
        wait_s: Callable[[], float],
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        """Make a tracker for one channel.

        Args:
            repo: The repository with the packet log and the stored verdicts.
            channel_name: The name of the channel (for the decryption).
            secret: The key of the channel. With no key, nothing can be matched, and each
                verdict is ``None``.
            near_now: The near relays that apply now. A message sent before MeshTerm kept
                the near relays of each send uses these.
            wait_s: Gives the ``heard_wait_s`` preference, read at each verdict.
            clock: Gives the current time (a fixed clock in the tests).
        """
        self._repo = repo
        self._channel_name = channel_name
        self._secret = secret
        self._near_now = near_now
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
        return judge(record.paths, record.near, age_s=self._age_s(message), wait_s=self._wait_s())

    def note_sent(self, message: ChatMessage) -> None:
        """Start the verdict of a message that was sent while the screen is open."""
        if self._ours(message):
            self._records[message.row_id] = _Record(near=self._near_now)  # type: ignore[index]
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
            near, paths = stored.get(message.row_id, (None, None))  # type: ignore[arg-type]
            if paths is not None:
                self._records[message.row_id] = _Record(  # type: ignore[index]
                    near=near if near is not None else self._near_now,
                    paths=list(paths),
                    settled=True,
                )
        self._revision += 1
        todo = [m for m in reversed(ours) if m.row_id not in self._records]
        for start in range(0, len(todo), _CHUNK):
            for message in todo[start : start + _CHUNK]:
                near, _paths = stored.get(message.row_id, (None, None))  # type: ignore[arg-type]
                self._examine(message, near if near is not None else self._near_now)
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
            self._examine(message, record.near)
            changed = changed or self._records[message.row_id].paths != before  # type: ignore[index]
        if changed:
            self._revision += 1
        return changed

    def waiting(self, messages: Sequence[ChatMessage]) -> bool:
        """Whether one of the messages shows the spinner now."""
        return any((v := self.verdict(m)) is not None and v.state == WAITING for m in messages)

    def _examine(self, message: ChatMessage, near: tuple[str, ...]) -> None:
        """Match the copies of one message in the packet log, and settle it if it is old."""
        assert self._secret is not None and message.row_id is not None
        arrivals = channel_arrivals(
            self._repo, message, channel_name=self._channel_name, secret=self._secret
        )
        paths = relay_paths(arrivals)
        settled = self._age_s(message) > CHANNEL_WINDOW.total_seconds()
        self._records[message.row_id] = _Record(near=near, paths=paths, settled=settled)
        if settled:
            self._repo.settle_heard(message.row_id, near, paths)


async def make_tracker(
    ctx: AppContext, *, channel_name: str, secret: bytes | None
) -> HeardTracker | None:
    """A tracker for one channel, with the near relays that apply now.

    Args:
        ctx: The shared application context (the repository, the outbound paths, and the
            preferences).
        channel_name: The name of the channel.
        secret: The key of the channel. With no key, there is no tracker.

    Returns:
        The tracker, or ``None`` when the channel has no key at hand.
    """
    if not secret:
        return None
    from .outbound import current_choice

    near: tuple[str, ...] = ()
    if getattr(ctx, "outbound_store", None) is not None:
        try:
            near = near_relays((await current_choice(ctx)).paths)
        except Exception:  # noqa: BLE001 - with no outbound paths, each relay counts
            near = ()
    return HeardTracker(
        ctx.repo,
        channel_name=channel_name,
        secret=secret,
        near_now=near,
        wait_s=lambda: float(ctx.preferences.heard_wait_s),
    )
