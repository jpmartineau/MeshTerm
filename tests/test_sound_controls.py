# SPDX-License-Identifier: Apache-2.0
"""The controls of the sounds: the 🔊/🔇 header badge, the Sound row, and ^S."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from meshterm.platforms import CARDPUTER_ZERO, REGULAR, set_platform
from meshterm.tools import all_tools, load_all_tools
from meshterm.ui import menu
from meshterm.ui.tui import Choice
from meshterm.ui.tui.session import _CTRL_LETTER_CHORDS


class FakeSound:
    """The parts of the sound service that the controls read."""

    def __init__(self, available: bool = True, enabled: bool = False, theme: str = "android"):
        """Make a service in a fixed state."""
        self.available = available
        self.enabled = enabled
        self.audible_theme = theme != "none"
        self.previews = 0

    def toggle(self) -> bool:
        """Turn the sounds on or off."""
        self.enabled = not self.enabled
        return self.enabled

    def preview(self) -> bool:
        """Count the previews."""
        self.previews += 1
        return True


@pytest.fixture
def regular() -> Iterator[None]:
    """Run on the regular platform."""
    set_platform(REGULAR)
    yield
    set_platform(REGULAR)


def test_the_badge_shows_the_state_of_the_sounds(regular: None) -> None:
    """🔊 while the sounds are on, 🔇 while they are off."""
    ctx = SimpleNamespace(sound=FakeSound(enabled=True))
    assert menu._sound_badge(ctx).plain == "🔊"
    ctx.sound.enabled = False
    assert menu._sound_badge(ctx).plain == "🔇"


def test_no_badge_where_no_sound_can_play(regular: None) -> None:
    """No speaker with the ``none`` theme, nor on a platform with no sound."""
    assert menu._sound_badge(SimpleNamespace(sound=FakeSound(theme="none"))).plain == ""
    assert menu._sound_badge(SimpleNamespace(sound=FakeSound(available=False))).plain == ""


def test_the_cardputer_badge_is_one_glyph_of_its_font() -> None:
    """The Cardputer draws no emoji, so the badge is the compact glyph."""
    set_platform(CARDPUTER_ZERO)
    try:
        ctx = SimpleNamespace(sound=FakeSound(enabled=True))
        assert menu._sound_badge(ctx).plain == "◄"
        ctx.sound.enabled = False
        assert menu._sound_badge(ctx).plain == "⌫"
    finally:
        set_platform(REGULAR)


def _sound_row(sound: FakeSound | None) -> Choice | None:
    load_all_tools()
    tools = [tool for tool in all_tools() if tool.menu_visible]
    rows = [r for r in menu._menu_items(tools, sound) if isinstance(r, Choice)]
    return next((r for r in rows if r.value == menu._SOUND_VALUE), None)


def test_the_menu_row_names_the_state_now(regular: None) -> None:
    """The row says the state of the sounds at each paint."""
    sound = FakeSound()
    row = _sound_row(sound)
    assert row is not None
    assert "Sound muted" in row.title().plain
    sound.enabled = True
    assert "Sound on" in row.title().plain
    sound.audible_theme = False
    title = row.title().plain
    assert "No sound" in title and "🔊" not in title and "🔇" not in title


def test_no_menu_row_where_no_sound_can_play(regular: None) -> None:
    """The PicoCalc, or no service at all, has no Sounds row."""
    assert _sound_row(FakeSound(available=False)) is None
    assert _sound_row(None) is None


def test_the_toggle_plays_the_preview_only_when_the_sounds_come_on() -> None:
    """Unmute: the channel message sound plays one time. Mute: nothing plays."""
    ctx = SimpleNamespace(sound=FakeSound())
    menu._toggle_sound(ctx)
    assert ctx.sound.enabled and ctx.sound.previews == 1
    menu._toggle_sound(ctx)
    assert not ctx.sound.enabled and ctx.sound.previews == 1


def test_the_toggle_does_nothing_with_the_no_sound_theme() -> None:
    """With the ``none`` theme there is nothing to mute."""
    ctx = SimpleNamespace(sound=FakeSound(theme="none"))
    menu._toggle_sound(ctx)
    assert not ctx.sound.enabled and ctx.sound.previews == 0


def test_ctrl_s_is_the_sound_chord_and_ctrl_e_reveals() -> None:
    """^S turns the sounds on or off, and ^E shows a concealed value (JP, 2026-11-20)."""
    assert _CTRL_LETTER_CHORDS["s"] == "sound"
    assert _CTRL_LETTER_CHORDS["e"] == "reveal"
