# SPDX-License-Identifier: Apache-2.0
"""The optional Shift-state watcher for the live change of the PicoCalc F-key lane.

The MCU of the PicoCalc keyboard translates Shift+F1..F5 into plain F6–F10 keycodes.
Thus the terminal never gets the Shift that made them. But the kernel input device gets
it, and the ``LSHIFT DOWN`` event arrives before the translated F-code (measured in P0).
This watcher reads the raw evdev stream and monitors whether a Shift key is physically
held. Thus the footer lane can change to the labels of the F6–F10 bank while the user is
in the middle of a chord.

Use on the handheld (JP, 2026-08-01) found a second quirk. The lane can change back to its
unshifted bank at the instant that an F6–F10 code arrives, also when Shift is still
physically held. This agrees with a key-matrix scan in the MCU that releases Shift and
asserts it again around the chord, instead of a scan that holds Shift down during all of
the translation. :func:`note_shift_bank_key` corrects that problem. The session calls it
each time an F6–F10 press resolves, and then :func:`shift_down` latches ``True`` for a
short grace period. Thus a short dip in the raw signal cannot change the lane in the
middle of a key press. The period is long enough to continue after the flicker, but it
does not continue much after the real key release. In both cases, the lane corrects
itself in one idle paint (the 2 s picocalc-lyra tick).

This watcher is only an experiment on top of a static lane that works, and it is made to
disappear without effect. It operates only when three conditions are true: the platform
asks for it (``Platform.modifier_watch``), the input device exists, and MeshTerm can read
it (the deploy user is in the ``input`` group on the handheld). An ssh session on another
machine, a change of permissions, or any read error has only one result:
:func:`shift_down` stays ``False``, and the lane stays static. The module has its own
30-line evdev reader, instead of the ``keyboard`` library, because then there is no
dependency to build on the stripped image. Also, the watcher reads only two keycodes.
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

#: The directory where the keyboard MCU of the PicoCalc registers (the same i2c chip as
#: the battery, P0).
_SYS_INPUT = Path("/sys/class/input")

_shift_down = False
_thread: threading.Thread | None = None

#: The paint request of the session. :func:`start` keeps it also when it finds no input
#: device, so that a Shift report from the emulator (:func:`report_shift`) still causes a
#: paint.
_on_change: Callable[[], None] | None = None

#: How long a resolved F6–F10 keycode keeps :func:`shift_down` latched ``True`` after the
#: raw signal goes down. It is long enough to continue across the release and assert
#: flicker of the MCU around a chord. It is short enough that a real release of Shift still
#: shows as released well in the time of one held key press. Refer to
#: :func:`note_shift_bank_key`.
_SHIFT_BANK_GRACE_S = 0.6

_last_shift_bank_at: float | None = None


def shift_down() -> bool:
    """Whether a Shift key is physically held now (``False`` when the watcher does not run).

    Also ``True`` for :data:`_SHIFT_BANK_GRACE_S` after the last F6–F10 press resolved
    (refer to :func:`note_shift_bank_key`). This correction is for a firmware quirk: the
    raw signal can dip in the middle of a chord, although Shift never came up.
    """
    if _shift_down:
        return True
    return (
        _last_shift_bank_at is not None
        and time.monotonic() - _last_shift_bank_at < _SHIFT_BANK_GRACE_S
    )


def report_shift(down: bool) -> None:
    """Set the Shift state from a source that knows it directly, and paint on a change.

    The emulator reads the keyboard itself (the window of the simulator, or the event
    stream of the handheld). Thus it knows when Shift goes down before an F-key arrives.
    It tells the lane here, so that no other code must find and read the same keyboard.
    """
    global _shift_down
    if down == _shift_down:
        return
    _shift_down = down
    callback = _on_change
    if callback is not None:
        try:
            callback()
        except Exception:  # noqa: BLE001 - a paint problem must not break the input of the host
            pass


def note_shift_bank_key() -> None:
    """Note that an F6–F10 keycode resolved now, which proves that Shift was down.

    The MCU sends these codes only while Shift is held. Thus their arrival is better
    evidence than the raw state of the watcher (refer to the module docstring). Call this
    function from each place where an F-key press resolves against the Shift bank
    (:meth:`TuiSession._dispatch`).
    """
    global _last_shift_bank_at
    _last_shift_bank_at = time.monotonic()


def _find_keyboard(keyboard: str) -> Path | None:
    """The event device whose name contains ``keyboard``, or ``None`` on another machine."""
    try:
        for entry in sorted(_SYS_INPUT.glob("event*")):
            name = (entry / "device" / "name").read_text().strip().lower()
            if keyboard.lower() in name:
                return Path("/dev/input") / entry.name
    except OSError:
        pass
    return None


def start(on_change: Callable[[], None], keyboard: str) -> bool:
    """Start the watcher thread if this machine has the keyboard (``True`` if it started).

    Args:
        on_change: Called (from the watcher thread) each time the Shift state changes.
            The session puts this callback in a thread-safe paint request.
        keyboard: The keyboard of the platform, by the name of its input device
            (``Platform.modifier_watch``): ``picocalc`` on the PicoCalc, ``tca8418c`` on
            the Cardputer Zero. The driver of the Cardputer Zero holds Shift down in the
            event stream while its sticky Shift is armed. Thus a tapped Shift changes the
            lane, as a held Shift does.
    """
    global _thread, _on_change
    _on_change = on_change
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
                except Exception:  # noqa: BLE001 - a paint problem must not stop the watch
                    pass

    _thread = threading.Thread(target=_watch, name="modifier-watch", daemon=True)
    _thread.start()
    return True
