# SPDX-License-Identifier: Apache-2.0
"""The Sound row of a conversation, and its picker."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from meshterm.core.preferences import Preferences
from meshterm.core.sound_store import SoundStore
from meshterm.ui import conversation_sound
from meshterm.ui.node_detail_screen import _sound_key, _sound_kind


class FakeUi:
    """Answers the picker with a fixed value, and records what it asked."""

    def __init__(self, answer: object) -> None:
        """Answer each select with ``answer``."""
        self.answer = answer
        self.asked: list[tuple[str, list, object]] = []

    async def select(self, title: str, items: list, *, prompt: str, default: object) -> object:
        """Record the question, and return the fixed answer."""
        self.asked.append((title, items, default))
        return self.answer


def _ctx(tmp_path: Path, answer: object = None, **prefs: object) -> SimpleNamespace:
    return SimpleNamespace(
        preferences=Preferences(values=prefs),
        sound_store=SoundStore(tmp_path / "sounds.json"),
        sound=SimpleNamespace(available=True),
        ui=FakeUi(answer),
    )


def test_the_description_names_the_default_or_the_own_setting(tmp_path: Path) -> None:
    """With no setting, the row names the default of the kind. With one, it names it."""
    ctx = _ctx(tmp_path)
    assert conversation_sound.describe(ctx, "chan:a", "channel") == "Default: mentions only"
    assert conversation_sound.describe(ctx, "dm:b", "dm") == "Default: every message"
    ctx.sound_store.set("chan:a", "off")
    assert conversation_sound.describe(ctx, "chan:a", "channel") == "No sound"


def test_the_picker_stores_a_setting_and_default_removes_it(tmp_path: Path) -> None:
    """A pick stores the setting. ``Default`` removes it again."""
    ctx = _ctx(tmp_path, answer="all")
    assert asyncio.run(conversation_sound.pick_sound(ctx, "chan:a", "channel", "Public"))
    assert ctx.sound_store.get("chan:a") == "all"
    title, items, default = ctx.ui.asked[0]
    assert title == "Sound — Public"
    assert default == "default"
    assert [i.value for i in items] == ["default", "off", "mentions", "all"]
    ctx.ui.answer = "default"
    assert asyncio.run(conversation_sound.pick_sound(ctx, "chan:a", "channel", "Public"))
    assert ctx.sound_store.get("chan:a") is None


def test_esc_or_the_same_value_changes_nothing(tmp_path: Path) -> None:
    """Esc (``None``) and the current value both return ``False`` and write nothing."""
    ctx = _ctx(tmp_path, answer=None)
    assert not asyncio.run(conversation_sound.pick_sound(ctx, "dm:b", "dm", "Ann"))
    ctx.ui.answer = "default"
    assert not asyncio.run(conversation_sound.pick_sound(ctx, "dm:b", "dm", "Ann"))
    assert not (tmp_path / "sounds.json").exists()


def test_the_row_is_only_where_a_sound_can_play(tmp_path: Path) -> None:
    """No sound service, or one that cannot play, means no row."""
    ctx = _ctx(tmp_path)
    assert conversation_sound.offered(ctx)
    ctx.sound.available = False
    assert not conversation_sound.offered(ctx)
    assert not conversation_sound.offered(SimpleNamespace())


def test_the_node_page_offers_the_row_for_a_companion_and_a_room(tmp_path: Path) -> None:
    """A companion is a DM, a room server is a room, and a repeater has no conversation."""
    ctx = _ctx(tmp_path)
    contact = SimpleNamespace(key_prefix="D4E5F6A7B8C9", public_key="d4e5f6a7b8c9" + "0" * 52)
    assert _sound_kind(ctx, contact, False, 1) == "dm"
    assert _sound_kind(ctx, contact, False, 3) == "room"
    assert _sound_kind(ctx, contact, False, 2) is None
    assert _sound_kind(ctx, contact, True, 1) is None  # our own node
    assert _sound_key(contact) == "dm:d4e5f6a7b8c9"
