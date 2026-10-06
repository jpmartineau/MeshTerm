# SPDX-License-Identifier: Apache-2.0
"""The emulator's desktop window: a handheld's display, with key presses from the desktop.

All the code between the two ends is the code path of the handheld itself: the terminal,
the font, the rasterizer, and the key encoding. Thus the window shows the pixels of the
display, at a larger scale and without smoothing. The desktop keyboard types what the
keyboard of the handheld will type (:meth:`~.devices.EmulatedDevice.translate`).

When the lane keys of the handheld are directly under its display (the Cardputer Zero's 4
to 8), the window draws them there, at the positions that the drawing of the maker gives
them. Thus a chip that moves away from its keyboard key is visible.

The F-keys of the desktop act as the lane keys of the handheld: F4–F8 for the Cardputer
Zero's Fn+4…8, and F1–F5 for the PicoCalc's lane keys. Shift with them is the second
bank. The window also reports Shift to the lane when Shift goes down and up. Thus the bank
changes on the screen as it will on the handheld.

The mouse also presses the lane keys. A click on a drawn keyboard key, or on a chip of the
lane itself, types the keyboard key of that slot. If Shift is held during the click, the
click types the Shift companion of that slot. The app reads the bank from the keyboard key
that arrives, never from the Shift that it saw. Thus the window sends the shifted keyboard
key itself (:func:`lane_press`). The drawn keyboard keys have the fill of the lane, and
they take its Shift fill while Shift is down, as the chips above them do
(:func:`lane_fills`).

A close of the window quits MeshTerm immediately, the same as ^Q pressed two times.
Ctrl+Shift+S saves the display at its true size as a PNG file in the working directory. A
held Esc key in the window quits MeshTerm, as it does on each handheld
(:mod:`~meshterm.services.hold_to_quit`). The window sees a keyboard key come up, and a
terminal never does.

Tk runs on its own thread, and the TUI runs on the main thread. The two threads share only
the lock of the terminal, a flag that says that a frame is ready, and the function that
types.
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

#: How long a released Esc key waits, to be sure that the user let it go. An X server
#: repeats a held keyboard key as a release and a press, one directly after the other, and
#: the press arrives within this time. A desktop that repeats with presses only never sends
#: the release before the keyboard key is up.
_REPEAT_GAP_MS = 30

#: The cap of a drawn lane key, and the ink of the digit that is printed on it.
_KEY_BODY = "#1d2025"
_KEY_DIGIT = "#eef1f4"

#: The modifier bits of Tk: Shift and Control on all platforms, Alt where the platform puts it.
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
    """What a press of lane slot ``slot`` types on ``device``, with Shift held or not.

    The plain keyboard key of the slot, spelled as the desktop F-key that acts for it is
    spelled. Thus Shift becomes the PicoCalc's F6–F10 and the Cardputer Zero's shifted
    F4–F8, the same as from the keyboard (:meth:`~.devices.EmulatedDevice.translate`).
    """
    key = Key(name=f"f{device.deck.keys[slot]}", shift=shift)
    return encode(device.translate(key))


def lane_fills(device: EmulatedDevice) -> tuple[str, str]:
    """The chip fills of the lane on ``device``, as Tk colours: the plain bank, then Shift.

    The fills come from the styles of the deck itself, in the theme that the handheld
    runs, through the palette that the display is drawn with. A numbered colour is a
    palette slot, as each colour of the console is. Any other colour is its own RGB. Thus a
    drawn keyboard key has the same colour as the chip above it.
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

    The lane is the footer of the frame, its last row. Each chip there starts with the
    caption of its keyboard key: a live chip, a dim chip, and the bare caption of an
    unassigned slot. Thus a caption at the chip's own column shows that the lane is drawn.
    A frame without a lane (a bare share screen, the chromeless splash) leaves the row to
    its own content, and a click there presses nothing.
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
    """A window that shows the display of ``device``, and types desktop key presses into the TUI."""

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
        """Open the window on its own thread, and wait until it is open."""
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
        """Note that a frame is complete. The window draws it on its next tick."""
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
        # When the user lets go of Shift in a different window, this window never gets the
        # release. If Shift stays held here, the lane and the drawn keyboard keys stay in
        # the Shift bank. Also, an Esc key that stays held quits three seconds later.
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
        """Colour the drawn keyboard keys in the bank of the lane: its fill, and its captions.

        A keyboard key that is held down under the mouse has the same fill as its chip:
        white on the fill.
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
        """A click on the display presses the lane chip at that position, if a chip is there."""
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
        """Tell the lane that Shift went down or up, and change the drawn keys to that bank."""
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
        """The Esc key went down. Or, directly after its release, the X server repeated it."""
        if self._esc_letting_go is not None:
            self._tk[0].after_cancel(self._esc_letting_go)
            self._esc_letting_go = None
            return
        self._esc_down = True
        self._esc.down()

    def _esc_released(self) -> None:
        """The Esc key is up, and no repeat followed: the hold is over."""
        if self._esc_letting_go is not None:
            self._tk[0].after_cancel(self._esc_letting_go)
            self._esc_letting_go = None
        self._esc_down = False
        self._esc.up()

    def _on_close(self) -> None:
        # ^Q asks. A second ^Q while it asks quits immediately.
        self._type("\x11")
        time.sleep(0.2)
        self._type("\x11")

    def _save(self) -> None:
        _, _, base, _ = self._tk
        path = Path.cwd() / f"{self._device.id}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        base.write(str(path), format="png")


def front_end(device: EmulatedDevice, scale: int = 3):
    """The window as a :data:`~.run.FrontEndFactory`. It reads the emulator's font first.

    Raises:
        FileNotFoundError: The font is not installed (refer to :func:`~.font.find_font`).
        RuntimeError: This Python has no Tk to open a window with. The one-file builds do
            not include Tk, so the emulator must have MeshTerm installed with pip or pipx.
    """
    from .font import find_font

    try:
        import tkinter  # noqa: F401 - only a check that it is there
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
