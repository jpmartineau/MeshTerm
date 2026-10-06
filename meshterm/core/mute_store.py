# SPDX-License-Identifier: Apache-2.0
"""Storage for the channels whose notifications are muted.

Some channels carry traffic that you want MeshTerm to store, but you do not want a
notification for it: an automatic ``#wardriving`` beacon, or a public relay with much
traffic. When you mute a channel here, its new messages do not increase the unread badge.
They do not increase the unread count of the conversation, so the channel list, the
conversation picker, and the unread total in the header do not count them. But MeshTerm
still writes the transcript to history, so the full conversation is there when you open it.
The default is the opposite of a mute: each channel notifies until you change it.

The store finds a mute by the *intrinsic identity* of the channel (refer to
:func:`~meshterm.core.channels.channel_identity`). The chat history and the unread counter of
the channel use the same identifier, which does not depend on the slot. It is never a slot
index. Thus a muted channel stays muted after the user moves it to another slot. Also, two
devices that share the key of a channel share the mute. The mute is global state of the
machine, in a small JSON file (``<config_dir>/mutes.json``), the same as the other choices of
the user (remembered devices, watched nodes, admin passwords). It is not in the SQLite
database of each invocation. After the first read, the store answers reads from memory,
because the chat recorder examines it for each received channel message, and the channel
list examines it at each paint. The writes are rare, and only the user causes them. The
store writes them immediately and atomically.
"""

from __future__ import annotations

import json
from pathlib import Path

from .atomicwrite import write_atomically


class MuteStore:
    """Reads and writes the set of channels whose notifications are muted, from memory first.

    Use :meth:`is_muted` (the check on the hot path), :meth:`set_muted` (the toggle), and
    :meth:`muted` (the full set). The store reads the set one time, at the first access, and
    then keeps it in memory. Each change stores the full set atomically.
    """

    def __init__(self, path: Path) -> None:
        """Open the store on the location of a JSON file.

        Args:
            path: The path to the JSON state file. The store makes the file at the first
                mute.
        """
        self._path = path
        self._muted: set[str] | None = None

    @property
    def _state(self) -> set[str]:
        """The set of muted channel identities, read from disk at the first access."""
        if self._muted is None:
            self._muted = self._load()
        return self._muted

    def _load(self) -> set[str]:
        """Parse the file into a set of channel identities (empty for a bad or missing file)."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        if not isinstance(data, dict):
            return set()
        return {str(x) for x in data.get("muted", []) if isinstance(x, str)}

    def is_muted(self, channel_id: str | None) -> bool:
        """Whether the notifications of the channel with this intrinsic identity are muted.

        Args:
            channel_id: The identity of the channel. ``None`` (a channel that is not
                resolved) is never muted, so a message on it always notifies.

        Returns:
            ``True`` if the channel is muted.
        """
        return channel_id is not None and channel_id in self._state

    def muted(self) -> set[str]:
        """The muted channel identities, as a copy that the caller can safely keep."""
        return set(self._state)

    def set_muted(self, channel_id: str, muted: bool) -> None:
        """Mute or unmute the notifications of a channel, and store only a real change.

        Args:
            channel_id: The intrinsic identity of the channel.
            muted: ``True`` to mute, ``False`` to restore notifications.
        """
        if muted == (channel_id in self._state):
            return  # already in the requested state, so there is nothing to write
        if muted:
            self._state.add(channel_id)
        else:
            self._state.discard(channel_id)
        self._save()

    def _save(self) -> None:
        """Store the full muted set atomically (a crash during the write keeps the old file)."""
        data = {"muted": sorted(self._state)}
        write_atomically(self._path, json.dumps(data, indent=2))
