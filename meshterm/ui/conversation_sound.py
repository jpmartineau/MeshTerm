# SPDX-License-Identifier: Apache-2.0
"""The *Sound…* row of a conversation, and the picker that it opens.

A DM and a room each have a sound setting (refer to
:class:`~meshterm.core.sound_store.SoundStore`): ``off``, ``mentions``, ``all``, or the
default of its kind (a preference). Two pages show the row: the node page of a contact,
and the page of a room. They both build it here, so that the words and the picker are the
same on each page.

A channel has no sound setting of its own (JP, 2026-11-21). All channels follow the
``channel_sound`` preference, and the mute of a channel makes it silent.

The row is only on a platform that can play a sound. The PicoCalc has no sound, so a
setting there would change nothing that the user can hear.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text

from ..core.preferences import SOUND_WHEN_CHOICES
from .menus import marked_label
from .tui import Choice

if TYPE_CHECKING:
    from ..context import AppContext

#: The icon of the row.
SOUND_ICON = "🔊"

#: The value with which the row resolves.
SOUND_ROW = "sound"

#: The value of the picker for "follow the default of the kind". The store keeps no record
#: for it.
_DEFAULT = "default"

#: The preference that holds the default of each kind of conversation.
KIND_PREFERENCE = {"dm": "dm_sound", "room": "room_sound"}


def offered(ctx: AppContext) -> bool:
    """Whether the pages show the row: the platform can play a sound."""
    sound = getattr(ctx, "sound", None)
    return sound is not None and sound.available


def _default_when(ctx: AppContext, kind: str) -> str:
    """The default sound setting of a kind of conversation, from its preference."""
    return str(ctx.preferences.get(KIND_PREFERENCE[kind]))


def describe(ctx: AppContext, key: str, kind: str) -> str:
    """The description of the row: what the conversation plays now.

    Args:
        ctx: The shared application context.
        key: The key of the conversation (``chan:…`` or ``dm:…``).
        kind: ``dm`` or ``room``.

    Returns:
        A short sentence, for example ``Default: mentions only``.
    """
    own = ctx.sound_store.get(key)
    when = own or _default_when(ctx, kind)
    text = {
        "off": "No sound",
        "mentions": "A sound for mentions only",
        "all": "A sound for each message",
    }[when]
    return text if own else f"Default: {SOUND_WHEN_CHOICES[when]}"


def sound_row(ctx: AppContext, key: str, kind: str, lane: int) -> tuple[Text, str, str]:
    """The *Sound…* row, as a ``(label, description, value)`` row for ``menu_rows``."""
    return (marked_label(SOUND_ICON, "Sound…", "", lane=lane), describe(ctx, key, kind), SOUND_ROW)


async def pick_sound(ctx: AppContext, key: str, kind: str, subject: str) -> bool:
    """Ask when the conversation plays a sound, and store the answer.

    Args:
        ctx: The shared application context.
        key: The key of the conversation.
        kind: ``dm`` or ``room``.
        subject: The name of the conversation, for the title.

    Returns:
        ``True`` if the setting changed.
    """
    own = ctx.sound_store.get(key)
    current = own or _DEFAULT
    default_label = SOUND_WHEN_CHOICES[_default_when(ctx, kind)]
    options = [(_DEFAULT, f"Default ({default_label})"), *SOUND_WHEN_CHOICES.items()]
    items = [
        Choice(title=label + ("  (current)" if value == current else ""), value=value)
        for value, label in options
    ]
    choice = await ctx.ui.select(
        f"Sound — {subject}",
        items,
        prompt=f"When this {'conversation' if kind == 'dm' else 'room'} plays a sound:",
        default=current,
    )
    if choice is None or choice == current:
        return False
    ctx.sound_store.set(key, None if choice == _DEFAULT else choice)
    return True
