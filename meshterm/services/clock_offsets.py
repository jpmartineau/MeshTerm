# SPDX-License-Identifier: Apache-2.0
"""How far the clock of each node is from ours, measured from what it sent us.

Each packet that carries the time of its sender, and that we heard at a known time, is one
**clock sample**: the sender time minus our time. A packet takes only seconds to cross the
mesh, so the sample is the error of the sender's clock. Three kinds of packet give one:

* **An advert.** It carries the time when its node made it, and the RX log gives our time
  (``observations.sent_at`` and ``observed_at``). It has a key, so the sample belongs to a
  node with no doubt. Each type of node sends adverts, repeaters too.
* **A direct message**, by its sender time and its arrival time. It comes from a known key
  prefix.
* **A channel message.** It has no sender field, only the ``Name: `` prefix of its text.
  A name that resolves to exactly one key (refer to
  :func:`~meshterm.services.trace_runner.make_name_key_resolver`) gives a sample for that
  node. Each other name is a **sender with no key**, and its samples stay under the name.

A room post is not a sample: its time is when its author posted it. A message that waited
in the companion is not a sample: its arrival time is not known.

The **offset** of a node is the median of its three newest samples. Thus one wrong sample
has no effect, and when the owner sets the clock, the offset follows after two new samples.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ..core.channels import split_channel_sender

if TYPE_CHECKING:
    from ..persistence.repository import Repository

#: The number of the newest samples whose median is the offset of a node.
NEWEST = 3

#: Resolves a channel sender name to the key of its node, or ``None``.
KeyOf = Callable[[str], "str | None"]


@dataclass(frozen=True, slots=True)
class ClockSample:
    """One packet with the time of its sender and our time of arrival.

    Attributes:
        who: The 12-hex id of the node, or the sender name when :attr:`keyed` is false.
        keyed: Whether a key identifies the sender.
        sent_at: The time by the clock of the sender.
        arrived_at: The time by our clock.
    """

    who: str
    keyed: bool
    sent_at: datetime
    arrived_at: datetime

    @property
    def offset_s(self) -> float:
        """The sender time minus our time, in seconds. Positive is a clock ahead of ours."""
        return (self.sent_at - self.arrived_at).total_seconds()


@dataclass(frozen=True, slots=True)
class ClockOffset:
    """How far the clock of one node (or one sender with no key) is from ours.

    Attributes:
        who: The 12-hex id of the node, or the sender name when :attr:`keyed` is false.
        keyed: Whether a key identifies the sender.
        offset_s: The median of the newest :data:`NEWEST` samples, in seconds. Positive is
            a clock ahead of ours.
        samples: The number of samples in all.
        last_at: Our time of the newest sample.
    """

    who: str
    keyed: bool
    offset_s: float
    samples: int
    last_at: datetime

    def out_of_sync(self, tolerance_s: float) -> bool:
        """Whether the clock is farther from ours than the ``clock_tolerance_s`` preference."""
        return abs(self.offset_s) > tolerance_s


def offsets(samples: Iterable[ClockSample]) -> list[ClockOffset]:
    """Make one offset for each sender from its samples.

    Args:
        samples: The samples, in any order.

    Returns:
        One :class:`ClockOffset` for each sender, in no specific order.
    """
    groups: dict[tuple[str, bool], list[ClockSample]] = {}
    for sample in samples:
        groups.setdefault((sample.who, sample.keyed), []).append(sample)
    result = []
    for (who, keyed), group in groups.items():
        group.sort(key=lambda s: s.arrived_at)
        newest = group[-NEWEST:]
        result.append(
            ClockOffset(
                who=who,
                keyed=keyed,
                offset_s=float(statistics.median(s.offset_s for s in newest)),
                samples=len(group),
                last_at=newest[-1].arrived_at,
            )
        )
    return result


def _channel_sample(
    text: str, sent: datetime, arrived: datetime, key_of: KeyOf | None
) -> ClockSample | None:
    """The sample of one channel message, under its node or under its sender name."""
    name, _body = split_channel_sender(text)
    if not name:
        return None  # no prefix: nobody to give the sample to
    key = key_of(name) if key_of is not None else None
    if key:
        return ClockSample(key.lower().removeprefix("0x")[:12], True, sent, arrived)
    return ClockSample(name, False, sent, arrived)


def window_samples(
    repo: Repository, *, since: datetime | None, key_of: KeyOf | None
) -> list[ClockSample]:
    """All the clock samples that arrived since a time, from adverts and messages.

    Args:
        repo: The repository that holds the packet log and the messages.
        since: The start of the window, or ``None`` for all the history.
        key_of: Resolves a channel sender name to a key.

    Returns:
        The samples, oldest first within each source.
    """
    samples = [
        ClockSample(node, True, sent, heard)
        for node, sent, heard in repo.advert_clock_samples(since=since)
    ]
    for is_channel, peer, text, sent, arrived in repo.message_clock_samples(since=since):
        if is_channel:
            sample = _channel_sample(text, sent, arrived, key_of)
            if sample is not None:
                samples.append(sample)
        elif peer:
            samples.append(ClockSample(peer.lower()[:12], True, sent, arrived))
    return samples


def node_offset(
    repo: Repository, node: str, *, name: str | None = None, key_of: KeyOf | None = None
) -> ClockOffset | None:
    """The clock offset of one node, from all its history.

    Args:
        repo: The repository that holds the packet log and the messages.
        node: The key of the node, or its 12-hex id.
        name: The name of the node. Its channel messages count only when ``key_of``
            resolves this name to this node, because another node can use the same name.
        key_of: Resolves a channel sender name to a key.

    Returns:
        The offset, or ``None`` when no packet of the node carried a usable time.
    """
    node_id = node.lower().removeprefix("0x")[:12]
    samples = [
        ClockSample(node_id, True, sent, heard)
        for _node, sent, heard in repo.advert_clock_samples(node=node_id)
    ]
    samples += [
        ClockSample(node_id, True, sent, arrived)
        for _chan, _peer, _text, sent, arrived in repo.message_clock_samples(peer=node_id)
    ]
    resolved = key_of(name) if (name and key_of is not None) else None
    if name and resolved and resolved.lower().removeprefix("0x")[:12] == node_id:
        samples += [
            ClockSample(node_id, True, sent, arrived)
            for _chan, _peer, _text, sent, arrived in repo.message_clock_samples(sender=name)
        ]
    found = offsets(samples)
    return found[0] if found else None
