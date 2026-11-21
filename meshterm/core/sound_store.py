# SPDX-License-Identifier: Apache-2.0
"""Storage for the sound setting of each conversation.

Each conversation (a channel, a DM, or a room) has a **Sound** setting: ``off``,
``mentions``, or ``all`` (refer to
:data:`~meshterm.core.preferences.SOUND_WHEN_CHOICES`). A conversation that has no record
here follows the default of its kind, which is a preference (``channel_sound``,
``dm_sound``, ``room_sound``). Thus a new channel follows the default, and the user changes
the setting only where they want something else.

The sound setting is not the mute of :class:`~meshterm.core.mute_store.MuteStore`. A mute
stops the unread count of a channel, and the setting here stops or starts its sound. The
user can mute the count of a busy public channel and still hear a mention in it.

The key of a record is the key of the conversation
(:func:`~meshterm.core.models.conversation_key`): ``chan:<identity>`` for a channel, and
``dm:<key prefix>`` for a DM or a room. The unread count and the history use the same key.
It does not depend on a slot, so the setting stays with a channel that moves.

The store is a small JSON file (``<config_dir>/sounds.json``), like the other choices of
the user. After the first read, it answers from memory, because the chat service reads it
for each received message. It writes each change immediately and atomically, from its
typed records (refer to :data:`ConversationSound.RETIRED`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .atomicwrite import write_atomically

#: The values of a sound setting that the store keeps. ``default`` is not a value here: it
#: is the absence of a record.
SOUND_WHEN = ("off", "mentions", "all")


@dataclass(slots=True)
class ConversationSound:
    """The sound setting of one conversation.

    Attributes:
        key: The key of the conversation (``chan:…`` or ``dm:…``).
        when: When the conversation plays a sound: ``off``, ``mentions``, or ``all``.
    """

    key: str
    when: str

    #: The field names that this record had once and does not have now. A field name that
    #: leaves is never used again with a different meaning (refer to
    #: :data:`meshterm.core.preferences.RETIRED` for the reason).
    RETIRED: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def read(cls, key: str, raw: object) -> ConversationSound | None:
        """Make a record from its JSON form, or ``None`` if the form is not correct."""
        if not isinstance(raw, dict):
            return None
        when = raw.get("when")
        if when not in SOUND_WHEN:
            return None
        return cls(key=key, when=when)

    def to_json(self) -> dict:
        """The JSON form of the record, without its key."""
        return {"when": self.when}


class SoundStore:
    """Reads and writes the sound setting of each conversation, from memory first."""

    def __init__(self, path: Path) -> None:
        """Open the store on the location of a JSON file.

        Args:
            path: The path to the JSON file. The store makes the file at the first change.
        """
        self._path = path
        self._records: dict[str, ConversationSound] | None = None

    @property
    def _state(self) -> dict[str, ConversationSound]:
        """The records by conversation key, read from disk at the first access."""
        if self._records is None:
            self._records = self._load()
        return self._records

    def _load(self) -> dict[str, ConversationSound]:
        """Read the records (empty for a missing or corrupt file)."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        records = {}
        for key, raw in data.items():
            record = ConversationSound.read(str(key), raw)
            if record is not None:
                records[record.key] = record
        return records

    def get(self, key: str) -> str | None:
        """The sound setting of a conversation, or ``None`` when it follows the default."""
        record = self._state.get(key)
        return record.when if record is not None else None

    def set(self, key: str, when: str | None) -> None:
        """Change the sound setting of a conversation, and store only a real change.

        Args:
            key: The key of the conversation.
            when: ``off``, ``mentions``, or ``all``. ``None`` removes the record, so the
                conversation follows the default of its kind again.

        Raises:
            ValueError: If ``when`` is not one of the permitted values.
        """
        if when is not None and when not in SOUND_WHEN:
            raise ValueError(f"not a sound setting: {when!r}")
        if self.get(key) == when:
            return
        if when is None:
            self._state.pop(key, None)
        else:
            self._state[key] = ConversationSound(key=key, when=when)
        data = {k: r.to_json() for k, r in sorted(self._state.items())}
        write_atomically(self._path, json.dumps(data, indent=2))
