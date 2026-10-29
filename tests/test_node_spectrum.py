# SPDX-License-Identifier: Apache-2.0
"""Tests for the spectrum of node hues in ``ui/theme.py``: one lightness for each hue."""

from __future__ import annotations

import pytest

from meshterm.ui import oklab, theme


def test_the_written_spectrum_is_the_calculated_one() -> None:
    """The fixed table agrees with :func:`theme._spectrum_hex` for each first key byte.

    If this test fails, a constant of the spectrum changed and the table did not. Write the
    table again from ``_spectrum_hex``.
    """
    assert len(theme._NODE_SPECTRUM) == 256
    assert list(theme._NODE_SPECTRUM) == [theme._spectrum_hex(byte) for byte in range(256)]


def test_every_hue_has_the_same_lightness() -> None:
    """Each hue has :data:`theme._NODE_LIGHTNESS` to the eye, within the rounding to 8 bits."""
    for colour in theme._NODE_SPECTRUM:
        assert oklab.from_hex(colour)[0] == pytest.approx(theme._NODE_LIGHTNESS, abs=0.005)


def test_every_hue_is_its_own_and_none_is_a_console_slot() -> None:
    """The 256 hues are different, and the spectrum never makes a colour of the console.

    ``is_identity_style`` holds both vocabularies in one set. Thus a spectrum hue that is
    also a console slot would make the two vocabularies collide.
    """
    assert len(set(theme._NODE_SPECTRUM)) == 256
    assert not set(theme._NODE_SPECTRUM) & set(theme._NODE_SLOT_HEXES)


def test_a_node_keeps_the_hue_angle_of_the_hsv_wheel() -> None:
    """Only the lightness and the chroma change. The hue angle of each byte is as before."""
    import colorsys
    import math

    for byte in range(0, 256, 8):
        r, g, b = colorsys.hsv_to_rgb(byte / 256, theme._NODE_HUE_SAT, theme._NODE_HUE_VAL)
        _, a0, b0 = oklab.from_hex(f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}")
        _, a1, b1 = oklab.from_hex(theme._NODE_SPECTRUM[byte])
        turn = math.remainder(math.atan2(b1, a1) - math.atan2(b0, a0), math.tau)
        assert abs(math.degrees(turn)) < 3, f"{byte:02x}"
