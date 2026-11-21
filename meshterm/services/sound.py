# SPDX-License-Identifier: Apache-2.0
"""The sound service: play a sound, or flash the title bar, for a notification.

A notification is an event that the user wants to know about now: a direct message, a
mention of our node, a channel message, a room post, a low battery, or (if the user wants
it) each packet heard. The callers decide *whether* an event notifies (the rules of each
conversation are in :class:`~meshterm.services.chat_service.ChatService`). This service
decides *how*: which sound, how loud, through which player, and whether the title bar
flashes.

MeshTerm has no audio dependency. It plays a WAV file through the player of the operating
system:

* **Windows**: ``winsound``, from the standard library.
* **macOS**: ``afplay``.
* **Linux** (the Cardputer Zero too): the first of ``pw-play``, ``paplay``, and ``aplay``
  that is installed.

The volume is in MeshTerm, not in the player: ``winsound`` has no volume control. Thus the
service writes a scaled copy of each sound into a cache folder, one time for each volume,
and plays the copy. The ``bell`` theme plays no file. It sends the terminal bell (BEL),
which rings on the computer of the user, also over SSH.

A player can fail: no audio device, no player installed, a server that is not running.
The first failure goes to the log one time. After it, the service does not try the player
again in this session, and each notification counts as silent. Thus the flash (in the
``silent`` mode) replaces the sound. A failure never stops the app.

The PicoCalc has no sound (:attr:`~meshterm.platforms.Platform.sound`). There, each
notification is silent, and only the flash can show it.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
import wave
from array import array
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from ..platforms import get_platform

if TYPE_CHECKING:
    from ..context import AppContext

log = logging.getLogger(__name__)


class SoundEvent(str, Enum):
    """The events that can play a sound."""

    DIRECT_MESSAGE = "direct_message"
    MENTION = "mention"
    CHANNEL_MESSAGE = "channel_message"
    ROOM_POST = "room_post"
    BATTERY_LOW = "battery_low"
    PACKET_TICK = "packet_tick"


#: The folder of the sound files in the package.
SOUNDS_DIR = Path(__file__).resolve().parent.parent / "assets" / "sounds"

#: The file of each event, for each theme that plays files. The keys are the values of the
#: ``sound_theme`` preference. JP selected the sounds of ``android`` by ear on 2026-11-20
#: (refer to ``meshterm/assets/sounds/android/README.md``).
THEMES: dict[str, dict[SoundEvent, str]] = {
    "android": {
        SoundEvent.DIRECT_MESSAGE: "android/Carme.wav",
        SoundEvent.MENTION: "android/Ariel.wav",
        SoundEvent.CHANNEL_MESSAGE: "android/InCallNotification.wav",
        SoundEvent.ROOM_POST: "android/Ceres.wav",
        SoundEvent.BATTERY_LOW: "android/LowBattery.wav",
        SoundEvent.PACKET_TICK: "android/Effect_Tick.wav",
    },
}

#: The minimum seconds between the start of a sound and a packet tick. It limits the tick
#: to four each second on a busy mesh, and a tick does not play over the start of another
#: sound (JP, 2026-11-20).
TICK_GAP_S = 0.25


def gain(volume_pct: int) -> float:
    """The factor for the samples at a volume, on a curve that follows the ear.

    The ear hears loudness approximately on a logarithmic scale. A straight factor puts
    almost all of the change in the lowest part of the range. The square of the fraction
    is a simple curve that is near the ear: 50% is a factor of 0.25 (-12 dB).

    Args:
        volume_pct: The volume, from 0 to 100.

    Returns:
        The factor, from 0.0 to 1.0.
    """
    fraction = max(0, min(100, volume_pct)) / 100
    return fraction * fraction


def scaled(source: Path, volume_pct: int, cache_dir: Path) -> Path:
    """A copy of a 16-bit WAV file at a volume, made one time and kept in a cache.

    Args:
        source: The WAV file at full volume. It must be 16-bit PCM.
        volume_pct: The volume, from 0 to 100. At 100, the function returns ``source``.
        cache_dir: The folder of the scaled copies.

    Returns:
        The path of the file to play.
    """
    if volume_pct >= 100:
        return source
    target = cache_dir / f"{source.parent.name}-{source.stem}-{volume_pct}.wav"
    if target.exists():
        return target
    with wave.open(str(source), "rb") as reader:
        params = reader.getparams()
        frames = reader.readframes(params.nframes)
    if params.sampwidth != 2:
        return source
    samples = array("h")
    samples.frombytes(frames)
    if sys.byteorder == "big":
        samples.byteswap()  # WAV data is little-endian.
    factor = gain(volume_pct)
    out = array("h", (int(s * factor) for s in samples))
    if sys.byteorder == "big":
        out.byteswap()
    cache_dir.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".part")
    with wave.open(str(partial), "wb") as writer:
        writer.setparams(params)
        writer.writeframes(out.tobytes())
    os.replace(partial, target)
    return target


class Player:
    """Plays a WAV file through the player of the operating system, without a wait.

    The first failure goes to the log, and then the player stays off for the session.
    """

    def __init__(self) -> None:
        """Make a player. It finds its command at the first sound."""
        self.broken = False
        self._command: list[str] | None = None
        self._running: list[subprocess.Popen[bytes]] = []

    def play(self, path: Path) -> bool:
        """Start the sound, and return at once.

        Args:
            path: The WAV file.

        Returns:
            ``True`` if the sound started. ``False`` if the player is off or failed.
        """
        if self.broken:
            return False
        try:
            if sys.platform == "win32":
                import winsound

                flags = winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
                winsound.PlaySound(str(path), flags)
                return True
            return self._spawn(path)
        except Exception as error:  # noqa: BLE001 - any failure of the player makes it off.
            log.warning("sound: the player failed, sounds are off for this session: %s", error)
            self.broken = True
            return False

    def _spawn(self, path: Path) -> bool:
        """Start the command of the player on a file (macOS and Linux)."""
        if self._command is None:
            self._command = _player_command()
        if not self._command:
            log.warning("sound: no player found (afplay, pw-play, paplay, or aplay)")
            self.broken = True
            return False
        # Collect the players that stopped, so that no process stays as a zombie.
        self._running = [p for p in self._running if p.poll() is None]
        self._running.append(
            subprocess.Popen(  # noqa: S603 - a fixed command and a file of the package.
                [*self._command, str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        return True


def _player_command() -> list[str]:
    """The command that plays a WAV file on this system, or an empty list."""
    if sys.platform == "darwin":
        return ["afplay"] if shutil.which("afplay") else []
    for command in (["pw-play"], ["paplay"], ["aplay", "-q"]):
        if shutil.which(command[0]):
            return command
    return []


class SoundService:
    """Plays the sound of a notification, and flashes the title bar if the user wants it.

    The interactive session gives the service two hooks: :meth:`set_hooks` names the
    function that rings the terminal bell and the function that flashes the title bar.
    Without them (a scripted run, a test), the bell and the flash do nothing.
    """

    def __init__(
        self,
        ctx: AppContext,
        player: Player | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Make the service.

        Args:
            ctx: The shared application context, read for the preferences and the config
                folder.
            player: The player. Tests give a fake one.
            clock: The clock of the tick limit. Tests give a fake one.
        """
        self._ctx = ctx
        self._player = player or Player()
        self._clock = clock
        self._last_sound = float("-inf")
        self._bell: Callable[[], None] | None = None
        self._flash: Callable[[], None] | None = None

    def set_hooks(
        self,
        bell: Callable[[], None] | None = None,
        flash: Callable[[], None] | None = None,
    ) -> None:
        """Name the functions that ring the terminal bell and flash the title bar."""
        self._bell = bell
        self._flash = flash

    @property
    def _prefs(self):  # noqa: ANN202 - the Preferences of the context.
        return self._ctx.preferences

    @property
    def available(self) -> bool:
        """Whether this platform can play a sound."""
        return get_platform().sound

    @property
    def enabled(self) -> bool:
        """Whether sounds are on: the platform can play them, and the user turned them on."""
        return self.available and bool(self._prefs.sounds)

    @property
    def audible_theme(self) -> bool:
        """Whether the theme makes a sound (``android`` or ``bell``, not ``none``)."""
        return self._prefs.sound_theme != "none"

    def toggle(self) -> bool:
        """Turn sounds on or off (the ^S key), and save the preference.

        Returns:
            ``True`` if sounds are now on.
        """
        now_on = not bool(self._prefs.sounds)
        self._prefs.set("sounds", now_on)
        try:
            self._prefs.save()
        except OSError as error:
            log.warning("sound: could not save the preferences: %s", error)
        return now_on

    def notify(self, event: SoundEvent) -> bool:
        """Play the sound of an event, and flash the title bar if the preference says so.

        Args:
            event: The event.

        Returns:
            ``True`` if a sound started (a file or the bell).
        """
        tick = event is SoundEvent.PACKET_TICK
        if tick:
            if not self._prefs.packet_tick:
                return False
            if self._clock() - self._last_sound < TICK_GAP_S:
                return False
        audible = self.enabled and self._emit(event)
        if not tick:
            mode = self._prefs.notify_flash
            if mode == "on" or (mode == "silent" and not audible):
                if self._flash is not None:
                    self._flash()
        return audible

    def preview(self, event: SoundEvent = SoundEvent.CHANNEL_MESSAGE) -> bool:
        """Play the sound of an event at the current volume, also when sounds are off.

        The Preferences page calls it while the user changes the volume or the theme
        (JP, 2026-11-20: "while adjusting the volume, play the channel message sound").

        Returns:
            ``True`` if a sound started.
        """
        return self.available and self._emit(event)

    def _emit(self, event: SoundEvent) -> bool:
        """Play the sound of an event in the current theme, with no rules."""
        theme = self._prefs.sound_theme
        if theme == "bell":
            if self._bell is None:
                return False
            self._bell()
            self._last_sound = self._clock()
            return True
        files = THEMES.get(theme)
        if not files or event not in files:
            return False
        try:
            path = scaled(
                SOUNDS_DIR / files[event],
                int(self._prefs.sound_volume_pct),
                self._ctx.settings.config_dir / "sound-cache",
            )
        except (OSError, wave.Error, EOFError) as error:
            log.warning("sound: could not prepare %s: %s", files[event], error)
            return False
        if not self._player.play(path):
            return False
        self._last_sound = self._clock()
        return True
