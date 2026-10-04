# SPDX-License-Identifier: Apache-2.0
"""On the Cardputer Zero: pixels to the framebuffer, keys from the keyboard's event device.

The launcher starts an app with the screen to itself and tells it where things are:
``APPLAUNCH_LINUX_FBDEV_DEVICE`` names the panel's framebuffer and
``APPLAUNCH_LINUX_KEYBOARD_DEVICE`` its keyboard, which it shares rather than grabs — so the
app reads key events itself, while the launcher watches the same stream for a held Esc
(3 s, then SIGTERM to the app's process group). This front end follows that contract.

**Written ahead of the hardware** (2026-09-30), from M5's published sources: the
framebuffer and keyboard paths and the Esc policy from the launcher, the key codes from the
keyboard driver's keymaps, and the Sym layer's characters from M5's console keymap
(``tca8418_keypad_m5stack_keymap.map``). The mapping is unit-tested; the I/O waits for a
device to run on.

The keyboard driver does the Fn layer in the kernel, so Fn+4 arrives as a plain ``KEY_F4``
and the arrows as real arrow keys; Shift is a real ``KEY_LEFTSHIFT`` held down for as long
as the sticky Shift is armed. Sym is the exception: its layer sends *placeholder* key codes
(``KEY_LEFTBRACE`` for ``!``, …) that M5's own keymap gives their symbols, so this module
gives them the same ones, and is the only place that knows.
"""

from __future__ import annotations

import os
import signal
import struct
import threading
from collections.abc import Callable
from pathlib import Path

from ..services import modifier_watch
from .font import Font
from .keys import Key, encode
from .raster import Raster, rgb565
from .run import DEFAULT_BG, DEFAULT_FG
from .vt import Terminal

FB_ENV = "APPLAUNCH_LINUX_FBDEV_DEVICE"
KEYBOARD_ENV = "APPLAUNCH_LINUX_KEYBOARD_DEVICE"
#: Where the launcher's own default puts the keyboard, when no variable says.
DEFAULT_KEYBOARD = "/dev/input/by-path/platform-3f804000.i2c-event"

#: ``struct input_event`` on a 64-bit kernel: timeval, then type, code, value.
_EVENT = struct.Struct("llHHi")
_EV_KEY = 0x01

# Modifier key codes (linux/input-event-codes.h).
_SHIFT = {42, 54}
_CTRL = {29, 97}
_ALT = {56, 100}

#: Key codes that are not text, by the name :mod:`.keys` spells them.
_NAMED: dict[int, str] = {
    1: "escape",
    14: "backspace",
    15: "tab",
    28: "enter",
    96: "enter",
    102: "home",
    103: "up",
    104: "pageup",
    105: "left",
    106: "right",
    107: "end",
    108: "down",
    109: "pagedown",
    110: "insert",
    111: "delete",
    **{59 + n: f"f{n + 1}" for n in range(10)},  # KEY_F1..KEY_F10
    87: "f11",
    88: "f12",
}

#: The base layer's characters.
_TEXT: dict[int, str] = {
    **{2 + n: "1234567890"[n] for n in range(10)},
    **{16 + n: "qwertyuiop"[n] for n in range(10)},
    **{30 + n: "asdfghjkl"[n] for n in range(9)},
    **{44 + n: "zxcvbnm"[n] for n in range(7)},
    57: " ",
}

#: The Sym layer's placeholder codes and the characters M5's console keymap gives them.
_SYM: dict[int, str] = {
    26: "!", 27: "@", 39: "#", 40: "$", 41: "%", 43: "^", 51: "&", 52: "*",
    53: "(", 94: ")", 55: "~", 69: "`", 70: "_", 71: "-", 72: "+", 73: "=",
    74: "[", 75: "]", 76: "{", 77: "}", 79: ";", 80: ":", 81: "'", 82: '"',
    83: "<", 85: ">", 86: "\\", 89: "|", 90: ",", 91: ".", 92: "/", 93: "?",
}  # fmt: skip


class KeyState:
    """Turns the keyboard's events into :class:`~.keys.Key` presses, tracking modifiers."""

    def __init__(self) -> None:
        """Start with no modifier held."""
        self._held: set[int] = set()

    @property
    def shift(self) -> bool:
        """Whether a Shift key is held — or armed, which the driver reports the same way."""
        return bool(self._held & _SHIFT)

    def event(self, code: int, value: int) -> Key | None:
        """One ``EV_KEY`` event (``value`` 1 down, 2 repeat, 0 up): the press it makes."""
        if code in _SHIFT | _CTRL | _ALT:
            if value:
                self._held.add(code)
            else:
                self._held.discard(code)
            return None
        if value == 0:
            return None
        shift = self.shift
        ctrl = bool(self._held & _CTRL)
        alt = bool(self._held & _ALT)
        name = _NAMED.get(code)
        if name is not None:
            return Key(name=name, shift=shift, ctrl=ctrl, alt=alt)
        text = _SYM.get(code) or _TEXT.get(code)
        if text is None:
            return None  # Fn and Sym themselves, media keys, brightness: nothing to type
        if shift and text.isalpha():
            text = text.upper()
        return Key(text=text, ctrl=ctrl, alt=alt)


class Framebuffer:
    """The panel's framebuffer, written a band of pixel rows at a time."""

    def __init__(self, path: str, *, width: int, height: int) -> None:
        """Open the framebuffer at ``path`` for a ``width`` × ``height`` panel."""
        self.path = path
        self._fd = os.open(path, os.O_RDWR)
        stride = self._sysfs("stride")
        self.stride = int(stride) if stride else width * 2
        self.width = width
        self.height = height

    def _sysfs(self, name: str) -> str | None:
        node = Path("/sys/class/graphics") / Path(self.path).name / name
        try:
            return node.read_text().strip()
        except OSError:
            return None

    def write(self, pixels: bytes | bytearray, row_bytes: int, first: int, last: int) -> None:
        """Write pixel rows ``first`` to ``last`` (exclusive) of a ``row_bytes``-wide image."""
        view = memoryview(pixels)
        for y in range(first, min(last, self.height)):
            os.pwrite(self._fd, view[y * row_bytes : (y + 1) * row_bytes], y * self.stride)

    def close(self) -> None:
        """Close the framebuffer."""
        os.close(self._fd)


class Device:
    """The front end on the device: a framebuffer writer and a keyboard reader."""

    def __init__(
        self,
        terminal: Terminal,
        lock: threading.Lock,
        type_text: Callable[[str], None],
        *,
        font: Font,
        framebuffer: str,
        keyboard: str,
    ) -> None:
        """Open the framebuffer, and start the drawing and key-reading threads."""
        self._lock = lock
        self._type = type_text
        self._raster = Raster(
            terminal, font, pack=rgb565, default_fg=DEFAULT_FG, default_bg=DEFAULT_BG
        )
        self._fb = Framebuffer(framebuffer, width=self._raster.width, height=self._raster.height)
        self._keyboard = keyboard
        self._ready = threading.Event()
        self._closing = False
        threading.Thread(target=self._draw, name="cardputer-fb", daemon=True).start()
        threading.Thread(target=self._read_keys, name="cardputer-keys", daemon=True).start()

    def frame_ready(self) -> None:
        """Wake the drawing thread: a frame is complete."""
        self._ready.set()

    def close(self) -> None:
        """Stop both threads."""
        self._closing = True
        self._ready.set()

    def _draw(self) -> None:
        while not self._closing:
            self._ready.wait()
            self._ready.clear()
            if self._closing:
                break
            with self._lock:
                bands = self._raster.update()
                pixels = bytes(self._raster.pixels)
            for first, last in bands:
                self._fb.write(pixels, self._raster.stride, first, last)
        self._fb.close()

    def _read_keys(self) -> None:
        state = KeyState()
        with open(self._keyboard, "rb", buffering=0) as stream:
            while not self._closing:
                data = stream.read(_EVENT.size)
                if len(data) < _EVENT.size:
                    break
                _, _, kind, code, value = _EVENT.unpack(data)
                if kind != _EV_KEY:
                    continue
                was_shifted = state.shift
                key = state.event(code, value)
                if state.shift != was_shifted:
                    modifier_watch.report_shift(state.shift)
                if key is not None:
                    sequence = encode(key)
                    if sequence:
                        self._type(sequence)


def _leave_on_sigterm() -> None:
    """Treat the launcher's SIGTERM (a held Esc) as an interrupt, so MeshTerm cleans up."""
    signal.signal(signal.SIGTERM, signal.default_int_handler)


def front_end():
    """The device as a :data:`~.run.FrontEndFactory`, from the launcher's environment."""
    from .font import find_font

    font = find_font()
    framebuffer = os.environ.get(FB_ENV) or "/dev/fb0"
    keyboard = os.environ.get(KEYBOARD_ENV) or DEFAULT_KEYBOARD
    _leave_on_sigterm()

    def build(terminal: Terminal, lock: threading.Lock, type_text: Callable[[str], None]):
        return Device(
            terminal, lock, type_text, font=font, framebuffer=framebuffer, keyboard=keyboard
        )

    return build
