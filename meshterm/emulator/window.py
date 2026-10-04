# SPDX-License-Identifier: Apache-2.0
"""The emulator's desktop window: a device's panel in a window, keys from the desktop.

Everything between the two ends is the device's own code path — the terminal, the font,
the rasterizer, the key encoding — so what the window shows is the panel's pixels, scaled
up without smoothing, and what the desktop keyboard types is what the device's keyboard
will (:meth:`~.devices.EmulatedDevice.translate`). Where the device's lane keys sit right
under its panel (the Cardputer Zero's 4 to 8), the window draws them there, at the positions
the maker's drawing gives them, so a chip that drifts off its key is visible.

The desktop's own F-keys stand in for the device's lane keys — F4–F8 for the Cardputer
Zero's Fn+4…8, F1–F5 for the PicoCalc's — and Shift with them is the second bank; the
window also reports Shift to the lane as it goes down and up, so the bank flips on screen
the way it will on the device. Closing the window leaves MeshTerm at once, as ^Q twice
would. Ctrl+Shift+S saves the panel at its true size as a PNG in the working directory.

Tk runs on a thread of its own, the TUI on the main thread; the two meet only through the
terminal's lock, a flag saying a frame is ready, and the function that types.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..services import modifier_watch
from .devices import EmulatedDevice
from .font import Font
from .keys import Key, encode
from .raster import Raster
from .run import default_colours
from .vt import Terminal

_NAMED = {
    "Up": "up",
    "Down": "down",
    "Left": "left",
    "Right": "right",
    "Home": "home",
    "End": "end",
    "Prior": "pageup",
    "Next": "pagedown",
    "Insert": "insert",
    "Delete": "delete",
    "Escape": "escape",
    "Return": "enter",
    "KP_Enter": "enter",
    "Tab": "tab",
    "ISO_Left_Tab": "tab",
    "BackSpace": "backspace",
    **{f"F{n}": f"f{n}" for n in range(1, 13)},
}

_SHIFTS = ("Shift_L", "Shift_R")

#: Tk's modifier bits: Shift and Control everywhere, Alt where the platform puts it.
_SHIFT_BIT = 0x0001
_CTRL_BIT = 0x0004
_ALT_BIT = 0x20000 if sys.platform == "win32" else 0x0008


def key_from_tk(keysym: str, char: str, state: int) -> Key | None:
    """A Tk key event as a :class:`Key`, or ``None`` for one that types nothing."""
    shift = bool(state & _SHIFT_BIT) or keysym == "ISO_Left_Tab"
    ctrl = bool(state & _CTRL_BIT)
    alt = bool(state & _ALT_BIT)
    name = _NAMED.get(keysym)
    if name is not None:
        return Key(name=name, shift=shift, ctrl=ctrl, alt=alt)
    if ctrl and len(keysym) == 1:
        return Key(text=keysym, ctrl=True, alt=alt)
    if keysym == "space" and ctrl:
        return Key(text=" ", ctrl=True, alt=alt)
    if char and char.isprintable():
        return Key(text=char, alt=alt)
    return None


class EmulatorWindow:
    """A window showing ``device``'s panel, typing the desktop's keys into the TUI."""

    def __init__(
        self,
        terminal: Terminal,
        lock: threading.Lock,
        type_text: Callable[[str], None],
        *,
        device: EmulatedDevice,
        font: Font,
        scale: int = 3,
    ) -> None:
        """Open the window on a thread of its own, and wait until it is up."""
        self._terminal = terminal
        self._lock = lock
        self._type = type_text
        self._device = device
        self._scale = scale
        ink, paper = default_colours(device)
        width, height = device.panel
        self._raster = Raster(
            terminal,
            font,
            default_fg=ink,
            default_bg=paper,
            width=width,
            height=height,
            top_left=device.console,
            bold_is_bright=device.console,
        )
        self._ready = threading.Event()
        self._ready.set()
        self._closing = False
        self._started = threading.Event()
        threading.Thread(target=self._main, name=f"{device.id}-window", daemon=True).start()
        self._started.wait(5)

    # --- the TUI's side ---------------------------------------------------------------

    def frame_ready(self) -> None:
        """Note that a frame is complete; the window draws it on its next tick."""
        self._ready.set()

    def close(self) -> None:
        """Close the window on its next tick."""
        self._closing = True

    # --- the window's side ------------------------------------------------------------

    def _main(self) -> None:
        import tkinter as tk

        scale = self._scale
        panel_w, panel_h = self._device.panel
        margin = 6 * scale
        keys_h = 26 * scale if self._device.lane_keys else 0
        root = tk.Tk()
        root.title(f"MeshTerm — {self._device.name} emulator")
        root.configure(background="#2b2f36")
        root.resizable(False, False)
        width = panel_w * scale + 2 * margin
        height = panel_h * scale + 2 * margin + keys_h
        canvas = tk.Canvas(
            root, width=width, height=height, background="#2b2f36", highlightthickness=0
        )
        canvas.pack()
        base = tk.PhotoImage(width=panel_w, height=panel_h)
        shown = base.zoom(scale, scale)
        canvas.create_image(margin, margin, image=shown, anchor="nw", tags="panel")
        self._draw_keys(canvas, margin, scale)
        self._tk = (root, canvas, base, shown)
        root.bind("<KeyPress>", self._on_press)
        root.bind("<KeyRelease>", self._on_release)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.focus_force()
        self._started.set()
        root.after(16, self._tick)
        root.mainloop()

    def _draw_keys(self, canvas, margin: int, scale: int) -> None:
        top = margin + self._device.panel[1] * scale + 4 * scale
        size = 18 * scale
        for number, centre in self._device.lane_keys:
            x = margin + centre * scale
            canvas.create_line(x, top - 3 * scale, x, top, fill="#e34b0f", width=scale)
            canvas.create_rectangle(
                x - size // 2, top, x + size // 2, top + size,
                fill="#1d2025", outline="#e34b0f", width=scale,
            )  # fmt: skip
            canvas.create_text(
                x, top + 5 * scale, text=f"F{number}", fill="#ff6633",
                font=("Consolas", 4 * scale, "bold"),
            )  # fmt: skip
            canvas.create_text(
                x, top + 12 * scale, text=str(number), fill="#eef1f4",
                font=("Consolas", 6 * scale, "bold"),
            )  # fmt: skip

    def _tick(self) -> None:
        root, canvas, base, _ = self._tk
        if self._closing:
            root.destroy()
            return
        if self._ready.is_set():
            self._ready.clear()
            with self._lock:
                bands = self._raster.update()
                pixels = bytes(self._raster.pixels)
            if bands:
                panel_w, panel_h = self._device.panel
                header = f"P6 {panel_w} {panel_h} 255\n".encode()
                base.configure(data=header + pixels, format="PPM")
                shown = base.zoom(self._scale, self._scale)
                canvas.itemconfigure("panel", image=shown)
                self._tk = (root, canvas, base, shown)
        root.after(16, self._tick)

    def _on_press(self, event) -> None:
        if event.keysym in _SHIFTS:
            modifier_watch.report_shift(True)
            return
        if event.keysym in ("S", "s") and event.state & _CTRL_BIT and event.state & _SHIFT_BIT:
            self._save()
            return
        key = key_from_tk(event.keysym, event.char, event.state)
        if key is not None:
            data = encode(self._device.translate(key))
            if data:
                self._type(data)

    def _on_release(self, event) -> None:
        if event.keysym in _SHIFTS:
            modifier_watch.report_shift(False)

    def _on_close(self) -> None:
        # ^Q asks; a second ^Q while it asks leaves at once.
        self._type("\x11")
        time.sleep(0.2)
        self._type("\x11")

    def _save(self) -> None:
        _, _, base, _ = self._tk
        path = Path.cwd() / f"{self._device.id}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        base.write(str(path), format="png")


def front_end(device: EmulatedDevice, scale: int = 3):
    """The window as a :data:`~.run.FrontEndFactory`, loading the emulator's font first.

    Raises:
        FileNotFoundError: The font isn't installed (see :func:`~.font.find_font`).
        RuntimeError: This Python has no Tk to open a window with — the one-file builds
            leave it out, so the emulator wants MeshTerm installed with pip or pipx.
    """
    from .font import find_font

    try:
        import tkinter  # noqa: F401 - only asking whether it is there
    except ImportError:
        raise RuntimeError(
            "the emulator's window needs Tk, which this copy of MeshTerm doesn't include "
            "- install MeshTerm with pip or pipx to use it"
        ) from None
    font = find_font()

    def build(terminal: Terminal, lock: threading.Lock, type_text: Callable[[str], None]):
        return EmulatorWindow(terminal, lock, type_text, device=device, font=font, scale=scale)

    return build


__all__ = ["EmulatorWindow", "front_end", "key_from_tk"]
