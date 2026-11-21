# SPDX-License-Identifier: Apache-2.0
"""The horizontal slider: steps of one, clamped ends, a call at each step, Enter and Esc."""

from __future__ import annotations

import asyncio
import re

from rich.cells import cell_len

from meshterm.ui.slider import SliderScreen
from meshterm.ui.tui.screen import CANCEL


def strip_ansi(text: str) -> str:
    """The text of a rendered line, without its SGR codes."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _slider(value: int = 7) -> tuple[SliderScreen, list[int]]:
    changes: list[int] = []
    return SliderScreen("Volume", "How loud", value, 1, 10, on_change=changes.append), changes


def test_the_arrows_move_one_step_and_call_on_change() -> None:
    """→ adds one, ← removes one, and each step calls ``on_change`` with the new value."""
    slider, changes = _slider()
    slider.handle("right")
    slider.handle("left")
    slider.handle("left")
    assert slider.value == 6
    assert changes == [8, 7, 6]


def test_the_ends_clamp_and_a_step_at_an_end_calls_nothing() -> None:
    """Nothing rolls over, and a key that changes nothing plays no preview."""
    slider, changes = _slider(10)
    slider.handle("right")
    assert slider.value == 10 and changes == []
    slider.handle("home")
    assert slider.value == 1 and changes == [1]
    slider.handle("left")
    assert changes == [1]
    slider.handle("end")
    assert slider.value == 10


def test_enter_sets_and_esc_keeps() -> None:
    """Enter resolves with the value, and Esc resolves with ``CANCEL``."""
    loop = asyncio.new_event_loop()
    try:
        slider, _ = _slider()
        slider.future = loop.create_future()
        slider.handle("right")
        slider.handle("enter")
        assert slider.future.result() == 8
        slider, _ = _slider()
        slider.future = loop.create_future()
        slider.handle("escape")
        assert slider.future.result() is CANCEL
    finally:
        loop.close()


def test_a_start_value_out_of_range_is_clamped() -> None:
    """A value from a file that was edited by hand comes into the range."""
    assert _slider(42)[0].value == 10
    assert _slider(-3)[0].value == 1


def test_the_bar_fills_the_line_on_both_widths() -> None:
    """The bar and its value fill each line exactly, at 72 and at 53 columns."""
    for width in (40, 49):
        lines = _slider()[0].render_body(width)
        assert all(cell_len(strip_ansi(line)) <= width for line in lines)
        assert strip_ansi(lines[-1]).rstrip().endswith(" 7")


def test_the_handheld_lane_dims_a_step_past_an_end() -> None:
    """``More`` is dim at the top, and ``Less`` at the bottom."""
    top = _slider(10)[0].picocalc_lyra_lane
    assert top[0].enabled is False and top[1].enabled is True
    bottom = _slider(1)[0].picocalc_lyra_lane
    assert bottom[0].enabled is True and bottom[1].enabled is False
