# SPDX-License-Identifier: Apache-2.0
"""Find the arrival time of old channel messages in the stored packet log.

Before schema 21, MeshTerm kept only the sender time of a received message. A sender with
a wrong clock then put its message on the wrong day, and nothing on the row can tell. But
the recorder also stored each channel packet that it heard (``GRP_TXT`` observations), with
the time when our node heard it, and with its ciphertext. MeshTerm has the key of each
channel in its slots. Thus it can decrypt each stored packet, and a packet whose text and
sender time are equal to those of a stored message *is* that message. The first time that
our node heard it is the arrival time of the message.

The match is exact, not a guess. The sender time is in the encrypted body, to the second,
so two messages with the same text ("hi") are two different keys. A message with no
matching packet (the monitor was not running, or the packet came before MeshTerm kept the
ciphertext) keeps no arrival time, and the app uses its sender time, as before.

Direct messages cannot use this. Only the recipient can decrypt a direct packet.

The backfill runs one time for each channel. It records each channel that it searched
(:meth:`~meshterm.persistence.repository.Repository.arrival_backfill_done`), so the next
start does not decrypt the log again. The work is cut into small parts, with a yield to
the event loop between them. A large log costs seconds on a desktop, and much longer on a
handheld, and the screen must stay quick during that time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING

from ..core.channels import channel_hash, decrypt_channel_text, effective_secret

if TYPE_CHECKING:
    from ..persistence.repository import Repository

#: The number of stored packets to decrypt before each yield to the event loop.
_CHUNK = 200


def _key(channel_id: str, sent: datetime, text: str) -> tuple[str, int, str]:
    """The identity of one channel message: its channel, its sender time, and its text."""
    return channel_id, int(sent.timestamp()), text.strip()


async def backfill_arrivals(repo: Repository, channels: Mapping[str, tuple[str, bytes]]) -> int:
    """Give the old messages of these channels the arrival time from the packet log.

    Args:
        repo: The repository that holds the messages and the packet log.
        channels: The channels on the device: identity to ``(name, secret)``.

    Returns:
        The number of messages that got an arrival time.
    """
    done = repo.arrival_backfill_done()
    todo = {cid: pair for cid, pair in channels.items() if cid not in done}
    if not todo:
        return 0
    rows = repo.channel_messages_without_arrival(todo)
    wanted: dict[tuple[str, int, str], list[int]] = {}
    for row_id, channel_id, created_at, text in rows:
        try:
            sent = datetime.fromisoformat(created_at)
        except (TypeError, ValueError):
            continue
        wanted.setdefault(_key(channel_id, sent, text), []).append(row_id)

    found: dict[int, str] = {}
    if wanted:
        # A packet names its channel only by a one-byte hash. Thus each packet is tried only
        # with the channels whose hash it carries. The MAC check in the decryption is the
        # proof.
        by_hash: dict[str, list[tuple[str, str, bytes]]] = {}
        for cid, (name, secret) in todo.items():
            fingerprint = channel_hash(effective_secret(name, secret))
            by_hash.setdefault(fingerprint.lower(), []).append((cid, name, secret))
        packets = repo.channel_packets()
        for start in range(0, len(packets), _CHUNK):
            for chan_hash, cipher_mac, crypted, first_at, _last in packets[start : start + _CHUNK]:
                for cid, name, secret in by_hash.get(str(chan_hash).lower(), ()):
                    text = decrypt_channel_text(chan_hash, cipher_mac, crypted, [(name, secret)])
                    if text is None:
                        continue
                    if text.sent_at is not None:
                        for row_id in wanted.get(_key(cid, text.sent_at, text.text), ()):
                            # The copies of one message are separate groups (each retry has
                            # its own ciphertext). The earliest copy is the arrival.
                            if row_id not in found or first_at < found[row_id]:
                                found[row_id] = first_at
                    break
            await asyncio.sleep(0)

    changed = repo.set_arrival_times(found.items()) if found else 0
    repo.mark_arrival_backfill_done(done | set(todo))
    return changed
