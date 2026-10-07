# SPDX-License-Identifier: Apache-2.0
"""The watcher of the Shift state of the F-key lane: the raw flag and the shift-bank latch."""

from __future__ import annotations

from meshterm.services import modifier_watch


def _reset() -> None:
    modifier_watch._shift_down = False
    modifier_watch._last_shift_bank_at = None


def test_shift_down_reflects_the_raw_flag() -> None:
    """``shift_down()`` reports the flag that the watcher set last, with no change between."""
    _reset()
    try:
        assert modifier_watch.shift_down() is False
        modifier_watch._shift_down = True
        assert modifier_watch.shift_down() is True
    finally:
        _reset()


def test_shift_bank_key_latches_over_the_release_flicker(monkeypatch) -> None:
    """A shift-bank key latches over the flicker at the release.

    A press of F6-F10 keeps the shifted state of the lane for a grace window. This is true
    also if the raw watcher reports that Shift is already up. Because of a quirk of the MCU,
    the watcher can report this wrongly.
    """
    _reset()
    try:
        clock = [100.0]
        monkeypatch.setattr(modifier_watch.time, "monotonic", lambda: clock[0])

        assert modifier_watch.shift_down() is False
        modifier_watch.note_shift_bank_key()
        assert modifier_watch.shift_down() is True  # latched, and the raw flag never became true

        clock[0] += modifier_watch._SHIFT_BANK_GRACE_S - 0.05
        assert modifier_watch.shift_down() is True  # still in the grace window

        clock[0] += 0.1
        assert modifier_watch.shift_down() is False  # the grace window ended, and Shift is up
    finally:
        _reset()
