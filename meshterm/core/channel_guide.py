# SPDX-License-Identifier: Apache-2.0
"""The channel guide: the public channels that MeshTerm heard, and their traffic.

MeshTerm stores each channel packet that it hears, also on a channel for which it has no
key (the ``chan_hash``, ``cipher_mac``, and ``crypted`` columns of ``observations``). A
packet names its channel only by a one-byte hash, so the packet alone does not give the
name. But the key of a public channel comes from its name (``sha256("#name")``). Thus
MeshTerm can test a candidate name: it derives the key, and it checks the MAC of the packet
with that key, exactly as the firmware does before it decodes a packet. A name whose key
gives the MAC of the packet is the name of the channel.

The candidate names come from what MeshTerm already knows:

- The channels with a key that MeshTerm has: the slots of the device, and the ``Public``
  channel of the firmware.
- Each ``#name`` that a stored message mentions.
- Each region that MeshTerm knows, as ``#region``.

A match of a guessed name on one message is not proof. The check compares three bytes (the
hash and the two-byte MAC). With thousands of names against thousands of packets,
approximately one false match is probable. Thus the guide names a guessed channel only when
:data:`MIN_MATCHES` messages or more match. A channel with a key that MeshTerm has needs one
match, because its name is not a guess.

The check is fast because the candidates are in groups by their hash byte. Each packet
checks only the candidates with its own hash, which is approximately 1/256 of them. A loop
over all the candidates for each packet took 40 s on a desktop for one history
(2026-10-06).

This module is pure logic. The caller reads the packets from the database and gives them
here. Thus the matching can run in a worker thread, and the database stays on its own
thread.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .channels import (
    DEFAULT_PUBLIC_SECRET,
    channel_hash,
    channel_identity,
    derive_secret,
    effective_secret,
    is_public_channel,
)

#: The number of different messages that a *guessed* name must match before the guide
#: names the channel. Refer to the module docstring for the reason.
MIN_MATCHES = 2

#: The time window of the activity chart of each row: the last seven days.
GUIDE_WINDOW = timedelta(days=7)

#: The number of buckets in the window, newest first. Each bucket is six hours, and a
#: braille cell draws two buckets, so the full chart is 14 cells for 7 days.
GUIDE_BUCKETS = 28

#: A ``#name`` in the text of a message: a ``#`` that does not follow a letter, a digit, or
#: another ``#``, then 2 to 31 letters, digits, ``_``, or ``-``. Thus ``C#`` and ``##`` are
#: not names, and ``#10`` is one.
MENTION = re.compile(r"(?<![\w#])#[A-Za-z0-9][A-Za-z0-9_-]{1,30}")


def mention_names(texts: Iterable[str | None]) -> set[str]:
    """Each ``#name`` that the texts mention, as written and in lowercase.

    The key comes from the exact bytes of the name, so ``#Ottawa`` and ``#ottawa`` are two
    channels. A person who mentions a channel often writes it with a capital letter that
    the channel does not have. Thus the function gives both forms.
    """
    names: set[str] = set()
    for text in texts:
        for found in MENTION.findall(text or ""):
            names.add(found)
            names.add(found.lower())
    return names


@dataclass(frozen=True, slots=True)
class HeardChannel:
    """One public channel that MeshTerm heard, with its traffic.

    Attributes:
        name: The name of the channel.
        key: The 16-byte key that matched the MAC of its packets.
        messages: The number of different messages heard (copies that repeaters relayed
            count one time).
        first_heard: When MeshTerm first heard a message of the channel.
        last_heard: When MeshTerm last heard a message of the channel.
        histogram: The messages in each bucket of :data:`GUIDE_WINDOW`, newest first.
    """

    name: str
    key: bytes
    messages: int
    first_heard: datetime | None
    last_heard: datetime | None
    histogram: tuple[int, ...]

    @property
    def identity(self) -> str:
        """The identity of the channel, the same value that a slot of the device gives."""
        return channel_identity(self.name, self.key)

    @property
    def hash(self) -> str:
        """The channel hash (two hex digits) that the packets of the channel carry."""
        return channel_hash(self.key)

    @property
    def write_secret(self) -> bytes | None:
        """The secret to send when the channel is added: ``None`` when the name gives it."""
        return None if self.key == derive_secret(self.name) else self.key


@dataclass(frozen=True, slots=True)
class Guide:
    """What the guide found in the stored packets.

    Attributes:
        channels: The public channels that it named, the most recently heard first.
        unnamed: The number of different messages that no name matched: messages on
            private channels, or on public channels whose name nobody wrote.
    """

    channels: tuple[HeardChannel, ...] = ()
    unnamed: int = 0


@dataclass(slots=True)
class _Tally:
    """The counts of one candidate while the guide reads the packets."""

    name: str
    key: bytes
    certain: bool
    messages: int = 0
    first: datetime | None = None
    last: datetime | None = None
    histogram: list[int] = field(default_factory=lambda: [0] * GUIDE_BUCKETS)


def build_guide(
    packets: Iterable[tuple[str, str, str, str | None, str | None]],
    known: Iterable[tuple[str, bytes]],
    guesses: Iterable[str],
    *,
    now: datetime,
) -> Guide:
    """Name the channels of the stored packets, and count their traffic.

    Args:
        packets: One row for each different message: its channel hash, its MAC, and its
            ciphertext (all in hex), then when it was first and last heard (ISO 8601).
        known: The channels with a key that MeshTerm has, as ``(name, key)``. The
            ``Public`` channel of the firmware is always added. A private channel here
            names its own packets, so that they do not count as unnamed, but the guide does
            not show it, because it is not public.
        guesses: The candidate names (``#name``) to test.
        now: The present time, for the activity chart.

    Returns:
        The guide.
    """
    tallies: dict[str, _Tally] = {}
    for name, secret in [("Public", DEFAULT_PUBLIC_SECRET), *known]:
        key = effective_secret(name, secret)
        tallies.setdefault(channel_identity(name, key), _Tally(name, key, certain=True))
    for name in guesses:
        key = derive_secret(name)
        tallies.setdefault(channel_identity(name, key), _Tally(name, key, certain=False))
    by_hash: dict[str, list[_Tally]] = {}
    for tally in tallies.values():  # the known channels first, in each group
        by_hash.setdefault(channel_hash(tally.key), []).append(tally)

    bucket_span = GUIDE_WINDOW / GUIDE_BUCKETS
    unnamed = 0
    for chan_hash, cipher_mac, crypted, first_at, last_at in packets:
        tally = _match(by_hash.get((chan_hash or "").lower(), ()), cipher_mac, crypted)
        if tally is None:
            unnamed += 1
            continue
        first, last = _when(first_at), _when(last_at)
        tally.messages += 1
        if first is not None:
            tally.first = first if tally.first is None else min(tally.first, first)
            age = now - first
            if timedelta(0) <= age < GUIDE_WINDOW:
                tally.histogram[int(age / bucket_span)] += 1
        if last is not None:
            tally.last = last if tally.last is None else max(tally.last, last)

    channels: list[HeardChannel] = []
    for tally in tallies.values():
        if tally.messages < (1 if tally.certain else MIN_MATCHES):
            unnamed += tally.messages  # too few to name it, so it stays unnamed
            continue
        if not is_public_channel(tally.name, tally.key):
            continue  # a private channel of the device: named, but not for the guide
        channels.append(
            HeardChannel(
                name=tally.name,
                key=tally.key,
                messages=tally.messages,
                first_heard=tally.first,
                last_heard=tally.last,
                histogram=tuple(tally.histogram),
            )
        )
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    channels.sort(key=lambda c: (c.last_heard or epoch, c.messages), reverse=True)
    return Guide(channels=tuple(channels), unnamed=unnamed)


def _match(candidates: Iterable[_Tally], cipher_mac: str, crypted: str) -> _Tally | None:
    """The candidate whose key gives the MAC of the packet, or ``None``.

    This is the check of :func:`~meshterm.core.channels.identify_channel`, with the
    ``hmac`` module of the standard library. That module does the whole HMAC in C, and it
    needs no import of PyCryptodome on the path of the guide.
    """
    try:
        mac = bytes.fromhex(cipher_mac or "")
        body = bytes.fromhex(crypted or "")
    except ValueError:
        return None
    if not mac or not body:
        return None
    for tally in candidates:
        if hmac.digest(tally.key, body, "sha256")[: len(mac)] == mac:
            return tally
    return None


def _when(text: str | None) -> datetime | None:
    """Parse a stored time. A time with no zone is UTC, as MeshTerm stores it."""
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)
