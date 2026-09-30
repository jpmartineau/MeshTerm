# SPDX-License-Identifier: Apache-2.0
"""The optional Shift-state watcher behind the PicoCalc F-key lane's live flip.

The PicoCalc keyboard's MCU translates Shift+F1..F5 into plain F6–F10 keycodes, so the
terminal never sees the Shift that produced them — but the kernel input device does, and
the ``LSHIFT DOWN`` event arrives *before* the translated F-code (measured in P0). This
watcher reads the raw evdev stream and tracks whether a Shift key is physically held, so
the footer lane can flip to the F6–F10 bank's labels while the user is mid-chord.

On-device use (JP, 2026-08-01) turned up a follow-on quirk: the lane can flip back to its
unshifted bank the instant an F6–F10 code lands, even with Shift still physically held —
consistent with the MCU's key-matrix scan releasing/re-asserting Shift around the chord
rather than holding it down continuously through the translation. :func:`note_shift_bank_key`
bridges that: the session calls it whenever an F6–F10 press actually resolves, and
:func:`shift_down` latches ``True`` for a short grace window after, so a momentary dip in
the raw signal can't flip the display mid-keystroke. The window is generous enough to
outlast the flicker without noticeably outlasting the actual key release — the lane still
self-corrects within one idle repaint (the 2 s picocalc tick) either way.

Strictly an experiment layered over a working static lane, and built to disappear: it
only ever engages when the platform asks for it (``Platform.modifier_watch``), the input
device exists, and it is readable (the deploy user is in the ``input`` group on the device) — an ssh
session on any other machine, a permissions change, or any read error at all just means
:func:`shift_down` stays ``False`` and the lane stays static. Hand-rolled 30-line evdev
reader instead of the ``keyboard`` library: no dependency to build on the stripped image,
and we need exactly two keycodes.
"""

from __future__ import annotations

import struct
import threading
import time
from collections.abc import Callable
from pathlib import Path

#: struct input_event on 32-bit ARM: struct timeval (2 × long = 8 bytes), then
#: __u16 type, __u16 code, __s32 value.
_EVENT_FORMAT = "llHHi"
_EVENT_SIZE = struct.calcsize(_EVENT_FORMAT)

_EV_KEY = 0x01
_KEY_LEFTSHIFT = 42
_KEY_RIGHTSHIFT = 54

#: Where the PicoCalc's keyboard MCU registers (same i2c chip as the battery; P0).
_SYS_INPUT = Path("/sys/class/input")

_shift_down = False
_thread: threading.Thread | None = None

#: How long a resolved F6–F10 keycode keeps :func:`shift_down` latched ``True`` after the
#: raw signal drops — long enough to bridge the MCU's release/re-assert flicker around a
#: chord, short enough that letting Shift go for real still reads as released well within
#: one held keypress. See :func:`note_shift_bank_key`.
_SHIFT_BANK_GRACE_S = 0.6

_last_shift_bank_at: float | None = None


def shift_down() -> bool:
    """Whether a Shift key is physically held right now (``False`` when not watching).

    Also ``True`` for :data:`_SHIFT_BANK_GRACE_S` after the last F6–F10 press resolved
    (see :func:`note_shift_bank_key`), bridging a firmware quirk where the raw signal can
    dip mid-chord even though Shift never actually came up.
    """
    if _shift_down:
        return True
    return (
        _last_shift_bank_at is not None
        and time.monotonic() - _last_shift_bank_at < _SHIFT_BANK_GRACE_S
    )


def note_shift_bank_key() -> None:
    """Record that an F6–F10 keycode just resolved — proof Shift was physically down.

    The MCU only ever emits these codes while Shift is held, so their arrival is stronger
    evidence than the watcher's own raw state (see the module docstring). Call this from
    wherever an F-key press resolves against the Shift bank (:meth:`TuiSession._dispatch`).
    """
    global _last_shift_bank_at
    _last_shift_bank_at = time.monotonic()


def _find_keyboard(keyboard: str) -> Path | None:
    """The event device whose name contains ``keyboard``, or ``None`` when it isn't this machine."""
    try:
        for entry in sorted(_SYS_INPUT.glob("event*")):
            name = (entry / "device" / "name").read_text().strip().lower()
            if keyboard.lower() in name:
                return Path("/dev/input") / entry.name
    except OSError:
        pass
    return None


def start(on_change: Callable[[], None], keyboard: str) -> bool:
    """Start the watcher thread if this machine has the keyboard; ``True`` if it engaged.

    Args:
        on_change: Called (from the watcher thread) whenever the Shift state flips —
            the session wraps this in a thread-safe repaint request.
        keyboard: The platform's keyboard, as its input device names itself
            (``Platform.modifier_watch``): ``picocalc`` on the PicoCalc, ``tca8418c`` on the
            Cardputer Zero, whose driver holds Shift down in the event stream for as long
            as its sticky Shift is armed, so a tapped Shift flips the lane as a held one does.
    """
    global _thread
    if _thread is not None:
        return True
    device = _find_keyboard(keyboard)
    if device is None:
        return False
    try:
        stream = open(device, "rb", buffering=0)
    except OSError:
        return False

    def _watch() -> None:
        global _shift_down
        held = {_KEY_LEFTSHIFT: False, _KEY_RIGHTSHIFT: False}
        while True:
            try:
                data = stream.read(_EVENT_SIZE)
            except OSError:
                break
            if not data or len(data) < _EVENT_SIZE:
                break
            _, _, etype, code, value = struct.unpack(_EVENT_FORMAT, data)
            if etype != _EV_KEY or code not in held:
                continue
            held[code] = value != 0  # 1 down, 2 auto-repeat, 0 up
            now = any(held.values())
            if now != _shift_down:
                _shift_down = now
                try:
                    on_change()
                except Exception:  # noqa: BLE001 - a repaint hiccup must not kill the watch
                    pass

    _thread = threading.Thread(target=_watch, name="modifier-watch", daemon=True)
    _thread.start()
    return True
