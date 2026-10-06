# SPDX-License-Identifier: Apache-2.0
"""On the Cardputer Zero: pixels to the framebuffer, key events from the keyboard's device file.

The launcher starts an app with the screen for that app only, and tells it where things
are. ``APPLAUNCH_LINUX_FBDEV_DEVICE`` names the framebuffer of the display, and
``APPLAUNCH_LINUX_KEYBOARD_DEVICE`` names its keyboard. The launcher shares the keyboard
and does not grab it. Thus the app reads the key events itself, while the launcher
watches the same stream for a held Esc key (3 s, then SIGTERM to the process group of the
app, and SIGKILL 3 s after that).

This front end obeys that contract. The Esc key goes through
:mod:`~meshterm.services.hold_to_quit`, which shows the hold on the display, where the
launcher does not draw any more. The front end takes the SIGTERM as a quit, because that is
what it is (:func:`~meshterm.services.hold_to_quit.leave_on_sigterm`).

**Written before the hardware was available** (2026-09-30), from the published sources of
M5:

* the framebuffer and keyboard paths, and the Esc policy, from the launcher,
* the key codes, from the keymaps of the keyboard driver, and
* the characters of the Sym layer, from the console keymap of M5
  (``tca8418_keypad_m5stack_keymap.map``).

The key mapping has unit tests. **Run on the handheld since 2026-10-05.** There, the key
codes and the Sym layer agreed with the keyboard and with the installed keymap of M5. We
also found there that the display must get its pixels through a memory mapping
(:class:`Framebuffer`).

The keyboard driver does the Fn layer in the kernel. Thus Fn+4 arrives as a plain
``KEY_F4``, and the arrows arrive as real arrow keys. Shift is a real ``KEY_LEFTSHIFT``
that stays down while the sticky Shift is armed. Sym is the exception. Its layer sends
placeholder key codes (``KEY_LEFTBRACE`` for ``!``, …), and the keymap of M5 gives them
their symbols. This module gives them the same symbols, and it is the only place in
MeshTerm that knows them.
"""

from __future__ import annotations

import mmap
import os
import struct
import threading
from collections.abc import Callable
from pathlib import Path

from ..services import hold_to_quit, modifier_watch
from .font import Font
from .keys import Key, encode
from .raster import Raster, rgb565
from .run import DEFAULT_BG, DEFAULT_FG
from .vt import Terminal

FB_ENV = "APPLAUNCH_LINUX_FBDEV_DEVICE"
KEYBOARD_ENV = "APPLAUNCH_LINUX_KEYBOARD_DEVICE"
#: The keyboard device file in the launcher's own default, when no variable names one.
DEFAULT_KEYBOARD = "/dev/input/by-path/platform-3f804000.i2c-event"

#: ``struct input_event`` on a 64-bit kernel: timeval, then type, code, and value.
_EVENT = struct.Struct("llHHi")
_EV_KEY = 0x01

#: ``KEY_ESC``. When it is held, it is the launcher's way out (refer to
#: :mod:`~meshterm.services.hold_to_quit`).
_ESC = 1

# Modifier key codes (linux/input-event-codes.h).
_SHIFT = {42, 54}
_CTRL = {29, 97}
_ALT = {56, 100}

#: Key codes that are not text, mapped to the names that :mod:`.keys` spells them with.
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

#: The characters of the base layer.
_TEXT: dict[int, str] = {
    **{2 + n: "1234567890"[n] for n in range(10)},
    **{16 + n: "qwertyuiop"[n] for n in range(10)},
    **{30 + n: "asdfghjkl"[n] for n in range(9)},
    **{44 + n: "zxcvbnm"[n] for n in range(7)},
    57: " ",
}

#: The placeholder codes of the Sym layer, and the characters that the console keymap of
#: M5 gives them.
_SYM: dict[int, str] = {
    26: "!", 27: "@", 39: "#", 40: "$", 41: "%", 43: "^", 51: "&", 52: "*",
    53: "(", 94: ")", 55: "~", 69: "`", 70: "_", 71: "-", 72: "+", 73: "=",
    74: "[", 75: "]", 76: "{", 77: "}", 79: ";", 80: ":", 81: "'", 82: '"',
    83: "<", 85: ">", 86: "\\", 89: "|", 90: ",", 91: ".", 92: "/", 93: "?",
}  # fmt: skip


class KeyState:
    """Changes keyboard events into :class:`~.keys.Key` presses, and tracks the modifiers."""

    def __init__(self) -> None:
        """Start with no modifier held."""
        self._held: set[int] = set()

    @property
    def shift(self) -> bool:
        """Whether a Shift key is held or armed. The driver reports the two in the same way."""
        return bool(self._held & _SHIFT)

    def event(self, code: int, value: int) -> Key | None:
        """The key press that one ``EV_KEY`` event makes (``value`` 1 down, 2 repeat, 0 up)."""
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
    """The display's framebuffer, written a band of pixel rows at a time through a memory mapping.

    **Through a memory mapping, never ``write()``.** The display has a DRM driver
    (``panel-mipi-dbi``), and its ``/dev/fb0`` is the fbdev emulation of the kernel. That
    is a buffer that the kernel copies to the display only where it knows that something
    changed. What the kernel watches is the memory mapping. A write through the mapping
    causes a page fault, and the kernel sends that page at the next update of the display.

    Bytes from ``pwrite`` go into the same buffer, and a read of ``/dev/fb0`` shows them.
    But they never get to the display. On the handheld, MeshTerm ran while the loading
    screen of the launcher stayed visible over it, and the user quit a splash that they
    could not see. The apps of M5 also draw through the memory mapping (LVGL's
    ``lv_linux_fbdev``). We found this on the hardware, 2026-10-05.
    """

    def __init__(self, path: str, *, width: int, height: int) -> None:
        """Open and map the framebuffer at ``path``, for a ``width`` × ``height`` display."""
        self.path = path
        self._fd = os.open(path, os.O_RDWR)
        stride = self._sysfs("stride")
        self.stride = int(stride) if stride else width * 2
        self.width = width
        self.height = height
        self._map = mmap.mmap(
            self._fd,
            self.stride * height,
            mmap.MAP_SHARED,  # type: ignore[attr-defined]  # Linux only, as is the display
            mmap.PROT_READ | mmap.PROT_WRITE,  # type: ignore[attr-defined]
        )

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
            start = y * self.stride
            self._map[start : start + row_bytes] = view[y * row_bytes : (y + 1) * row_bytes]

    def close(self) -> None:
        """Unmap and close the framebuffer."""
        self._map.close()
        os.close(self._fd)


class Device:
    """The front end on the handheld: a framebuffer writer and a keyboard reader."""

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
        """Open the framebuffer, then start two threads: one draws, one reads the keyboard."""
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
        """Wake the thread that draws: a frame is complete."""
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
        esc = hold_to_quit.EscKey(self._type)
        with open(self._keyboard, "rb", buffering=0) as stream:
            while not self._closing:
                data = stream.read(_EVENT.size)
                if len(data) < _EVENT.size:
                    break
                _, _, kind, code, value = _EVENT.unpack(data)
                if kind != _EV_KEY:
                    continue
                if code == _ESC:
                    # Down and its repeats are one hold. A tap is typed when it comes up.
                    if value:
                        esc.down()
                    else:
                        esc.up()
                    continue
                was_shifted = state.shift
                key = state.event(code, value)
                if state.shift != was_shifted:
                    modifier_watch.report_shift(state.shift)
                if key is not None:
                    sequence = encode(key)
                    if sequence:
                        esc.before()
                        self._type(sequence)


def front_end():
    """The handheld as a :data:`~.run.FrontEndFactory`, from the launcher's environment."""
    from .font import find_font

    font = find_font()
    framebuffer = os.environ.get(FB_ENV) or "/dev/fb0"
    keyboard = os.environ.get(KEYBOARD_ENV) or DEFAULT_KEYBOARD
    # From here, not only from the CLI: the launcher's SIGTERM can arrive while MeshTerm
    # still imports its modules. Then MeshTerm must stop as an interrupt, not unhandled.
    hold_to_quit.leave_on_sigterm()

    def build(terminal: Terminal, lock: threading.Lock, type_text: Callable[[str], None]):
        return Device(
            terminal, lock, type_text, font=font, framebuffer=framebuffer, keyboard=keyboard
        )

    return build
