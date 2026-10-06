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
the way it will on the device. The mouse presses them too: a click on a drawn key, or on
a chip of the lane itself, types that slot's key, and Shift held through the click types
its Shift companion — the app reads the bank off the key that arrives, never off the
Shift it saw, so the window sends the shifted key itself (:func:`lane_press`). The drawn
keys wear the lane's own fill, and take its Shift fill while Shift is down, as the chips
above them do (:func:`lane_fills`). Closing the window leaves MeshTerm at once, as ^Q
twice would. Ctrl+Shift+S saves the panel at its true size as a PNG in the working
directory. Esc held in the window quits, as it does on either device
(:mod:`~meshterm.services.hold_to_quit`): the window sees a key come up, as a terminal
never does.

Tk runs on a thread of its own, the TUI on the main thread; the two meet only through the
terminal's lock, a flag saying a frame is ready, and the function that types.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ..services import hold_to_quit, modifier_watch
from ..ui.theme import MESH_THEME, MESH_THEME_16
from ..ui.tui.fkeys import LaneDeck
from .devices import EmulatedDevice
from .font import CELL_H, CELL_W, Font
from .keys import Key, encode
from .raster import Raster
from .run import _palette, default_colours
from .vt import Terminal, _xterm_256

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

#: How long a released Esc waits to be sure it was let go. An X server repeats a held key
#: as a release and a press back to back, and the press arrives within this; a desktop
#: that repeats with presses alone never sends the release until the key is up.
_REPEAT_GAP_MS = 30

#: A drawn lane key's cap, and the ink of the digit printed on it.
_KEY_BODY = "#1d2025"
_KEY_DIGIT = "#eef1f4"

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


def lane_press(device: EmulatedDevice, slot: int, *, shift: bool) -> str:
    """What pressing lane slot ``slot`` types on ``device``, with Shift held or not.

    The slot's plain key, spelled as the desktop's F-key standing in for it would be —
    so Shift becomes the PicoCalc's F6–F10 and the Cardputer Zero's shifted F4–F8 just as
    it does from the keyboard (:meth:`~.devices.EmulatedDevice.translate`).
    """
    key = Key(name=f"f{device.deck.keys[slot]}", shift=shift)
    return encode(device.translate(key))


def lane_fills(device: EmulatedDevice) -> tuple[str, str]:
    """The lane's chip fills on ``device`` as Tk colours: the plain bank's, then Shift's.

    Resolved from the deck's own styles in the theme the device runs, through the palette
    the panel is drawn with — a numbered colour is a palette slot, as the console's every
    colour is, and anything else its own RGB — so a drawn key is the colour the chip
    above it is.
    """
    theme = MESH_THEME if device.platform.truecolor else MESH_THEME_16
    palette = _palette()
    deck = device.deck

    def fill(style: str) -> str:
        colour = theme.styles[style].bgcolor
        if colour is None:
            return _KEY_BODY
        if colour.number is not None:
            red, green, blue = _xterm_256(colour.number, palette)
        else:
            red, green, blue = colour.get_truecolor()
        return f"#{red:02x}{green:02x}{blue:02x}"

    return fill(deck.fill), fill(deck.shift_fill)


def chip_at(terminal: Terminal, deck: LaneDeck, col: int, row: int) -> int | None:
    """The lane slot whose chip is drawn at cell ``(col, row)``, or ``None``.

    The lane is the frame's footer, its last row, and every chip there opens on its key's
    caption — a live chip, a dimmed one and an unassigned slot's bare caption alike — so
    a caption at the chip's own column is what says the lane is drawn. A frame without
    one (a bare share screen, the chromeless splash) leaves the row to its own content,
    and a click there presses nothing.
    """
    if row != terminal.rows - 1:
        return None
    cells = terminal.screen[row]
    for slot, start in enumerate(deck.columns):
        if not start <= col < start + deck.chip_width:
            continue
        for caption in (deck.captions[slot], deck.shift_captions[slot]):
            if "".join(char for char, _ in cells[start : start + len(caption)]) == caption:
                return slot
        return None
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
        self._margin = 6 * scale
        self._fills = lane_fills(device)
        self._shifted = False
        self._pressed: int | None = None
        self._esc = hold_to_quit.EscKey(type_text)
        self._esc_down = False
        self._esc_letting_go: str | None = None
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
        margin = self._margin
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
        canvas.tag_bind("panel", "<ButtonPress-1>", self._on_panel_click)
        self._tk = (root, canvas, base, shown)
        self._draw_keys(canvas, margin, scale)
        root.bind("<KeyPress>", self._on_press)
        root.bind("<KeyRelease>", self._on_release)
        # A Shift let go in another window never reaches this one; leaving it held here
        # would keep the lane and the drawn keys in the Shift bank — and an Esc left held
        # would quit three seconds later.
        root.bind("<FocusOut>", self._on_focus_out)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.focus_force()
        self._started.set()
        root.after(16, self._tick)
        root.mainloop()

    def _draw_keys(self, canvas, margin: int, scale: int) -> None:
        top = margin + self._device.panel[1] * scale + 4 * scale
        size = 18 * scale
        for slot, (number, centre) in enumerate(self._device.lane_keys):
            x = margin + centre * scale
            key = f"key{slot}"
            canvas.create_line(x, top - 3 * scale, x, top, width=scale, tags="tick")
            canvas.create_rectangle(
                x - size // 2, top, x + size // 2, top + size,
                width=scale, tags=(key, f"cap{slot}"),
            )  # fmt: skip
            canvas.create_text(
                x, top + 5 * scale, font=("Consolas", 4 * scale, "bold"),
                tags=(key, f"legend{slot}"),
            )  # fmt: skip
            canvas.create_text(
                x, top + 12 * scale, text=str(number), fill=_KEY_DIGIT,
                font=("Consolas", 6 * scale, "bold"), tags=key,
            )  # fmt: skip
            canvas.tag_bind(key, "<ButtonPress-1>", lambda event, n=slot: self._press_key(n, event))
            canvas.tag_bind(key, "<ButtonRelease-1>", lambda _: self._release_key())
            canvas.tag_bind(key, "<Enter>", lambda _: canvas.configure(cursor="hand2"))
            canvas.tag_bind(key, "<Leave>", lambda _: canvas.configure(cursor=""))
        self._paint_keys()

    def _paint_keys(self) -> None:
        """Colour the drawn keys in the lane's bank: its fill, and its captions.

        A key held down under the mouse is filled the way its chip is, white on the fill.
        """
        canvas = self._tk[1]
        deck = self._device.deck
        fill = self._fills[self._shifted]
        captions = deck.shift_captions if self._shifted else deck.captions
        canvas.itemconfigure("tick", fill=fill)
        for slot in range(len(self._device.lane_keys)):
            down = slot == self._pressed
            canvas.itemconfigure(f"cap{slot}", outline=fill, fill=fill if down else _KEY_BODY)
            canvas.itemconfigure(
                f"legend{slot}", text=captions[slot], fill=_KEY_DIGIT if down else fill
            )

    def _press_key(self, slot: int, event) -> None:
        self._pressed = slot
        self._paint_keys()
        self._press_lane(slot, event.state)

    def _release_key(self) -> None:
        self._pressed = None
        self._paint_keys()

    def _on_panel_click(self, event) -> None:
        """A click on the panel presses the lane chip it lands on, if it lands on one."""
        x = (event.x - self._margin) // self._scale - self._raster.left
        y = (event.y - self._margin) // self._scale - self._raster.top
        if x < 0 or y < 0:
            return
        with self._lock:
            slot = chip_at(self._terminal, self._device.deck, x // CELL_W, y // CELL_H)
        if slot is not None:
            self._press_lane(slot, event.state)

    def _press_lane(self, slot: int, state: int) -> None:
        data = lane_press(self._device, slot, shift=bool(state & _SHIFT_BIT))
        if data:
            self._esc.before()
            self._type(data)

    def _set_shift(self, down: bool) -> None:
        """Tell the lane Shift went down or up, and flip the drawn keys' bank with it."""
        modifier_watch.report_shift(down)
        if down != self._shifted:
            self._shifted = down
            self._paint_keys()

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
            self._set_shift(True)
            return
        if event.keysym == "Escape":
            self._esc_pressed()
            return
        if event.keysym in ("S", "s") and event.state & _CTRL_BIT and event.state & _SHIFT_BIT:
            self._save()
            return
        key = key_from_tk(event.keysym, event.char, event.state)
        if key is not None:
            data = encode(self._device.translate(key))
            if data:
                self._esc.before()
                self._type(data)

    def _on_release(self, event) -> None:
        if event.keysym in _SHIFTS:
            self._set_shift(False)
        elif event.keysym == "Escape" and self._esc_down:
            root = self._tk[0]
            self._esc_letting_go = root.after(_REPEAT_GAP_MS, self._esc_released)

    def _on_focus_out(self, _event) -> None:
        self._set_shift(False)
        if self._esc_down:
            self._esc_released()

    def _esc_pressed(self) -> None:
        """Esc went down — or, straight after its release, the X server repeated it."""
        if self._esc_letting_go is not None:
            self._tk[0].after_cancel(self._esc_letting_go)
            self._esc_letting_go = None
            return
        self._esc_down = True
        self._esc.down()

    def _esc_released(self) -> None:
        """Esc is up, and no repeat followed: the hold is over."""
        if self._esc_letting_go is not None:
            self._tk[0].after_cancel(self._esc_letting_go)
            self._esc_letting_go = None
        self._esc_down = False
        self._esc.up()

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


__all__ = ["EmulatorWindow", "chip_at", "front_end", "key_from_tk", "lane_fills", "lane_press"]
