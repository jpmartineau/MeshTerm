# SPDX-License-Identifier: Apache-2.0
"""Keys to the bytes a terminal sends for them — what the console host types into MeshTerm.

Both ends of the host meet here: the device reads key codes off the keyboard's event
device, the simulator reads them off a desktop window, and each turns its own events into
a :class:`Key` for :func:`encode` to spell the way xterm does. xterm's spelling is the
contract because it is what prompt_toolkit parses, and what a desktop terminal running
``--platform cardputer-zero`` sends too, so the TUI cannot tell the three apart:

* Shift with F4–F8 is xterm's ``CSI 1;2 S`` / ``CSI 15;2 ~`` …, which prompt_toolkit
  reads as F16–F20 — the Cardputer deck's Shift bank
  (:data:`~meshterm.ui.tui.fkeys.CARDPUTER_ZERO_DECK`).
* A modified arrow, Home, End, Page key or F-key carries xterm's modifier parameter
  (``1 + Shift + 2·Alt + 4·Ctrl``), so Ctrl+PgUp is ``CSI 5;5 ~``, the section jump.
* Ctrl with a letter is its C0 control; Alt with anything is ESC then the key.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The keys that are not text, by name. Arrows, Home and End take xterm's ``CSI 1;m X``
#: modified form; the rest take ``CSI n;m ~``.
_CURSOR_FINAL = {"up": "A", "down": "B", "right": "C", "left": "D", "home": "H", "end": "F"}
_TILDE = {
    "insert": 2,
    "delete": 3,
    "pageup": 5,
    "pagedown": 6,
    "f5": 15,
    "f6": 17,
    "f7": 18,
    "f8": 19,
    "f9": 20,
    "f10": 21,
    "f11": 23,
    "f12": 24,
}
_SS3 = {"f1": "P", "f2": "Q", "f3": "R", "f4": "S"}
_PLAIN = {"escape": "\x1b", "enter": "\r", "tab": "\t", "backspace": "\x7f"}


@dataclass(frozen=True, slots=True)
class Key:
    """One key press: a named key (``"up"``, ``"f4"``, ``"enter"``) or the text it types."""

    name: str = ""
    text: str = ""
    shift: bool = False
    ctrl: bool = False
    alt: bool = False


def encode(key: Key) -> str:
    """The bytes a terminal sends for ``key``, or ``""`` for one it has no spelling for."""
    modifier = 1 + key.shift + 2 * key.alt + 4 * key.ctrl
    name = key.name
    if name in _CURSOR_FINAL:
        final = _CURSOR_FINAL[name]
        return f"\x1b[{final}" if modifier == 1 else f"\x1b[1;{modifier}{final}"
    if name in _SS3:
        final = _SS3[name]
        return f"\x1bO{final}" if modifier == 1 else f"\x1b[1;{modifier}{final}"
    if name in _TILDE:
        number = _TILDE[name]
        return f"\x1b[{number}~" if modifier == 1 else f"\x1b[{number};{modifier}~"
    if name == "tab" and key.shift:
        return "\x1b[Z"
    if name in _PLAIN:
        sequence = _PLAIN[name]
        return "\x1b" + sequence if key.alt and name != "escape" else sequence
    text = key.text
    if not text:
        return ""
    if key.ctrl and len(text) == 1:
        lower = text.lower()
        if "a" <= lower <= "z":
            text = chr(ord(lower) - 96)
        elif text in "@[\\]^_":
            text = chr(ord(text) & 0x1F)
        elif text == " ":
            text = "\x00"
    return "\x1b" + text if key.alt else text
