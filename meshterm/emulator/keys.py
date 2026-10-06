# SPDX-License-Identifier: Apache-2.0
"""The bytes that a terminal sends for each keyboard key: what the emulator types into MeshTerm.

Both front ends use this module. The handheld reads key codes from the event device file
of the keyboard, and the window reads them from the desktop. Each front end changes its
own events into a :class:`Key`, and :func:`encode` spells it as xterm does. The spelling
of xterm is the contract, because prompt_toolkit parses it. A desktop terminal that runs
``--platform cardputer-zero`` sends it too. Thus the TUI cannot tell the three apart:

* Shift with F4–F8 is xterm's ``CSI 1;2 S`` / ``CSI 15;2 ~`` …, which prompt_toolkit
  reads as F16–F20. These are the Shift bank of the Cardputer deck
  (:data:`~meshterm.ui.tui.fkeys.CARDPUTER_ZERO_DECK`).
* A modified arrow, Home, End, Page key, or F-key has the modifier parameter of xterm
  (``1 + Shift + 2·Alt + 4·Ctrl``). Thus Ctrl+PgUp is ``CSI 5;5 ~``, the section jump.
* Ctrl with a letter is the C0 control of that letter. Alt with any keyboard key is ESC,
  then that keyboard key.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The keyboard keys that are not text, by name. Arrows, Home, and End take the modified
#: form ``CSI 1;m X`` of xterm. The others take ``CSI n;m ~``.
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
    """One key press: a named keyboard key (``"up"``, ``"f4"``, ``"enter"``) or its text."""

    name: str = ""
    text: str = ""
    shift: bool = False
    ctrl: bool = False
    alt: bool = False


def encode(key: Key) -> str:
    """The bytes that a terminal sends for ``key``, or ``""`` if no spelling exists for it."""
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
