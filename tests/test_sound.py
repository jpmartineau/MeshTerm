# SPDX-License-Identifier: Apache-2.0
"""The sound service: the themes, the volume, the tick limit, the flash, and failures."""

from __future__ import annotations

import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshterm.core.preferences import Preferences
from meshterm.platforms import PICOCALC_LYRA, REGULAR, get_platform, set_platform
from meshterm.services import sound
from meshterm.services.sound import SOUNDS_DIR, THEMES, SoundEvent, SoundService


class FakePlayer:
    """Records each file that it plays, and can act as a failed player."""

    def __init__(self, works: bool = True) -> None:
        """Make a player that works, or one that fails."""
        self.played: list[Path] = []
        self.works = works

    def play(self, path: Path) -> bool:
        """Record the file, and say if the sound started."""
        self.played.append(path)
        return self.works


class Clock:
    """A clock that the test moves."""

    def __init__(self) -> None:
        """Start at a fixed time."""
        self.now = 100.0

    def __call__(self) -> float:
        """The time now."""
        return self.now


def _service(tmp_path: Path, works: bool = True, **prefs: object):
    ctx = SimpleNamespace(
        preferences=Preferences(values=prefs),
        settings=SimpleNamespace(config_dir=tmp_path),
    )
    player, clock = FakePlayer(works), Clock()
    service = SoundService(ctx, player=player, clock=clock)  # type: ignore[arg-type]
    flashes: list[int] = []
    bells: list[int] = []
    service.set_hooks(bell=lambda: bells.append(1), flash=lambda: flashes.append(1))
    return service, player, clock, flashes, bells


@pytest.fixture(autouse=True)
def _regular():
    previous = get_platform()
    set_platform(REGULAR)
    yield
    set_platform(previous)


def test_each_theme_file_exists_and_is_16_bit_pcm() -> None:
    """Each file that a theme names is in the package, and the volume code can read it."""
    for files in THEMES.values():
        assert set(files) == set(SoundEvent)
        for name in files.values():
            with wave.open(str(SOUNDS_DIR / name), "rb") as reader:
                assert reader.getsampwidth() == 2


def test_sounds_play_by_default(tmp_path: Path) -> None:
    """The default is the Android theme, not muted (JP, 2026-11-20)."""
    service, player, _, flashes, _ = _service(tmp_path)
    assert service.notify(SoundEvent.DIRECT_MESSAGE) is True
    assert len(player.played) == 1
    assert flashes == []


def test_a_muted_service_is_silent_and_flashes(tmp_path: Path) -> None:
    """A mute plays nothing, and the default flash replaces the sound."""
    service, player, _, flashes, _ = _service(tmp_path, sound_muted=True)
    assert service.notify(SoundEvent.DIRECT_MESSAGE) is False
    assert player.played == []
    assert flashes == [1]


def test_an_event_plays_its_file_of_the_theme(tmp_path: Path) -> None:
    """An event plays its own file of the theme, and the flash stays off."""
    service, player, _, flashes, _ = _service(
        tmp_path,
    )
    assert service.notify(SoundEvent.MENTION) is True
    assert player.played[0].name.startswith("android-Ariel-")
    assert flashes == []  # when silent: a sound played, so no flash


def test_the_volume_scales_a_cached_copy(tmp_path: Path) -> None:
    """A volume below 100 plays a scaled copy from the cache: 50% is a quarter."""
    service, player, _, _, _ = _service(tmp_path, sound_volume=5)
    service.notify(SoundEvent.DIRECT_MESSAGE)
    copy = player.played[0]
    assert copy.parent == tmp_path / "sound-cache"
    with wave.open(str(SOUNDS_DIR / "android/Carme.wav"), "rb") as full:
        loud = max(
            abs(int.from_bytes(full.readframes(4000)[i : i + 2], "little", signed=True))
            for i in range(0, 8000, 2)
        )
    with wave.open(str(copy), "rb") as quiet:
        soft = max(
            abs(int.from_bytes(quiet.readframes(4000)[i : i + 2], "little", signed=True))
            for i in range(0, 8000, 2)
        )
    assert soft == pytest.approx(loud * 0.25, abs=2)


def test_full_volume_plays_the_file_of_the_package(tmp_path: Path) -> None:
    """At 100%, the service plays the file of the package, with no copy."""
    service, player, _, _, _ = _service(tmp_path, sound_volume=10)
    service.notify(SoundEvent.ROOM_POST)
    assert player.played == [SOUNDS_DIR / "android/Ceres.wav"]


def test_the_gain_follows_the_square_of_the_volume() -> None:
    """The gain is the square of the volume fraction."""
    assert sound.gain(100) == 1.0
    assert sound.gain(50) == 0.25
    assert sound.gain(0) == 0.0


def test_the_bell_theme_rings_the_bell(tmp_path: Path) -> None:
    """The bell theme rings the terminal bell and plays no file."""
    service, player, _, _, bells = _service(tmp_path, sound_theme="bell")
    assert service.notify(SoundEvent.CHANNEL_MESSAGE) is True
    assert bells == [1]
    assert player.played == []


def test_the_none_theme_is_silent_and_flashes(tmp_path: Path) -> None:
    """The none theme plays nothing, so the default flash shows the event."""
    service, player, _, flashes, _ = _service(tmp_path, sound_theme="none")
    assert service.notify(SoundEvent.DIRECT_MESSAGE) is False
    assert player.played == []
    assert flashes == [1]


def test_the_tick_is_off_by_default(tmp_path: Path) -> None:
    """The packet tick is off until the user turns it on."""
    service, player, _, flashes, _ = _service(
        tmp_path,
    )
    assert service.notify(SoundEvent.PACKET_TICK) is False
    assert player.played == []
    assert flashes == []  # a tick never flashes


def test_the_tick_plays_at_most_one_each_quarter_second(tmp_path: Path) -> None:
    """The tick plays at most one time each 250 ms."""
    service, player, clock, _, _ = _service(tmp_path, packet_tick=True)
    assert service.notify(SoundEvent.PACKET_TICK) is True
    clock.now += 0.1
    assert service.notify(SoundEvent.PACKET_TICK) is False
    clock.now += 0.2
    assert service.notify(SoundEvent.PACKET_TICK) is True
    assert len(player.played) == 2


def test_no_tick_right_after_another_sound(tmp_path: Path) -> None:
    """A tick does not play over the start of another sound."""
    service, player, clock, _, _ = _service(tmp_path, packet_tick=True)
    service.notify(SoundEvent.DIRECT_MESSAGE)
    clock.now += 0.1
    assert service.notify(SoundEvent.PACKET_TICK) is False
    assert len(player.played) == 1


def test_a_failed_player_counts_as_silent(tmp_path: Path) -> None:
    """A player that fails counts as silent, so the flash shows the event."""
    service, _, _, flashes, _ = _service(
        tmp_path,
        works=False,
    )
    assert service.notify(SoundEvent.DIRECT_MESSAGE) is False
    assert flashes == [1]


@pytest.mark.parametrize(
    ("mode", "muted", "flashes"),
    [
        ("off", True, 0),
        ("off", False, 0),
        ("on", True, 1),
        ("on", False, 1),
        ("silent", True, 1),
        ("silent", False, 0),
    ],
)
def test_the_flash_modes(tmp_path: Path, mode: str, muted: bool, flashes: int) -> None:
    """The flash follows its preference: off, on, or only when no sound plays."""
    service, _, _, flashed, _ = _service(tmp_path, sound_muted=muted, notify_flash=mode)
    service.notify(SoundEvent.DIRECT_MESSAGE)
    assert len(flashed) == flashes


def test_the_picocalc_is_silent_and_flashes(tmp_path: Path) -> None:
    """The PicoCalc plays no sound, also for a preview, and it flashes."""
    set_platform(PICOCALC_LYRA)
    service, player, _, flashes, _ = _service(
        tmp_path,
    )
    assert service.notify(SoundEvent.DIRECT_MESSAGE) is False
    assert service.preview() is False
    assert player.played == []
    assert flashes == [1]


def test_the_preview_plays_also_when_muted(tmp_path: Path) -> None:
    """The preview plays the channel message sound also when the sounds are muted."""
    service, player, _, flashes, _ = _service(tmp_path, sound_muted=True)
    assert service.preview() is True
    assert player.played[0].name.startswith("android-InCallNotification-")
    assert flashes == []


def test_toggle_changes_and_saves_the_preference(tmp_path: Path) -> None:
    """The toggle changes the preference and saves it to the file."""
    prefs = Preferences(path=tmp_path / "preferences.toml")
    ctx = SimpleNamespace(preferences=prefs, settings=SimpleNamespace(config_dir=tmp_path))
    service = SoundService(ctx, player=FakePlayer())  # type: ignore[arg-type]
    assert service.toggle() is False  # muted
    assert Preferences.load(tmp_path / "preferences.toml").sound_muted is True
    assert service.toggle() is True  # on again
    assert Preferences.load(tmp_path / "preferences.toml").sound_muted is False
