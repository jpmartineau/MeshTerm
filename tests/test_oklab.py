# SPDX-License-Identifier: Apache-2.0
"""Tests for ``ui/oklab.py``, the perceptual colour space for the seam and its shade."""

from __future__ import annotations

import pytest

from meshterm.ui import oklab
from meshterm.ui.theme import _node_style_spectrum


def _fill(key: str) -> str:
    return _node_style_spectrum(key).split()[-1]


def test_every_node_fill_round_trips_through_oklab_exactly() -> None:
    """Hex to Lab to hex is lossless on all 256 spectrum fills and the fixed greys."""
    for byte in range(256):
        colour = _fill(f"{byte:02x}")
        assert oklab.to_hex(oklab.from_hex(colour)) == colour
    for grey in ("#000000", "#ffffff", "#64748b", "#334155", "#3f3f46"):
        assert oklab.to_hex(oklab.from_hex(grey)) == grey


def test_lightness_runs_black_to_white_and_distance_is_a_metric() -> None:
    """``L`` spans 0–1, the distance from black to white is 1, and the distance is symmetric."""
    black, white = oklab.from_hex("#000000"), oklab.from_hex("#ffffff")
    assert black[0] == pytest.approx(0.0, abs=1e-6) and white[0] == pytest.approx(1.0, abs=1e-3)
    assert oklab.distance("#000000", "#ffffff") == pytest.approx(1.0, abs=1e-3)
    assert oklab.distance("#f25555", "#f25555") == 0.0
    assert oklab.distance("#f25555", "#5557f2") == oklab.distance("#5557f2", "#f25555")


def test_the_wheel_is_not_uniform_to_the_eye() -> None:
    """One hue step is small in green and large in cyan. This is why the module exists."""
    green = oklab.distance(_fill("4c"), _fill("5c"))
    cyan = oklab.distance(_fill("80"), _fill("90"))
    assert green < 0.03 < 0.15 < cyan


def test_shaded_moves_lightness_alone_away_from_the_nearer_end() -> None:
    """A light colour is returned darker and a dark colour lighter. The chroma is the same."""
    light, dark = "#55f255", "#334155"
    for colour, sign in ((light, -1), (dark, +1)):
        before, after = oklab.from_hex(colour), oklab.from_hex(oklab.shaded(colour, 0.1))
        assert (after[0] - before[0]) * sign == pytest.approx(0.1, abs=0.01)
        assert after[1] == pytest.approx(before[1], abs=0.02)  # in-gamut: chroma kept
        assert after[2] == pytest.approx(before[2], abs=0.02)


def test_shaded_floor_clears_a_second_colour_too() -> None:
    """With a floor, the shift starts from the lightness that is further along."""
    a, b = _fill("4c"), _fill("5c")  # b is the darker of two similar greens
    plain = oklab.shaded(a, 0.15)
    floored = oklab.shaded(a, 0.15, floor=oklab.from_hex(b)[0])
    assert oklab.from_hex(floored)[0] <= oklab.from_hex(plain)[0]
    assert oklab.from_hex(b)[0] - oklab.from_hex(floored)[0] == pytest.approx(0.15, abs=0.01)

    slate, lighter_slate = "#334155", "#3f3f46"  # dark: the floor pushes up instead
    floored = oklab.shaded(slate, 0.15, floor=oklab.from_hex(lighter_slate)[0])
    assert oklab.from_hex(floored)[0] >= oklab.from_hex(oklab.shaded(slate, 0.15))[0]


def test_fitted_lowers_chroma_alone_and_keeps_near_hues_apart() -> None:
    """A colour outside the gamut is returned inside it, with its lightness and hue.

    A colour in the gamut is returned unchanged. The clip in ``to_hex`` made two dark
    greens near ``0x50`` almost one colour. After the fit, they are clearly two colours.
    """
    inside = oklab.from_hex(_fill("80"))
    assert oklab.fitted(inside) == inside
    darker = [oklab.from_hex(_fill(key)) for key in ("4c", "5c")]
    outside = [(lab[0] * 0.6, lab[1], lab[2]) for lab in darker]
    fits = [oklab.fitted(lab) for lab in outside]
    clipped = oklab.distance(*(oklab.to_hex(lab) for lab in outside))
    assert oklab.distance(*(oklab.to_hex(lab) for lab in fits)) > 2 * clipped
    for before, after in zip(outside, fits, strict=True):
        assert after[0] == before[0]
        assert after[1] / before[1] == pytest.approx(after[2] / before[2])  # the same hue
        assert abs(after[1]) < abs(before[1])  # less chroma


def test_from_hex_rejects_anything_but_six_digits() -> None:
    """Six hex digits are accepted, and the ``#`` and the case are optional.

    All other text raises a ``ValueError``.
    """
    with pytest.raises(ValueError):
        oklab.from_hex("#fff")
    assert oklab.from_hex("F25555") == oklab.from_hex("#f25555")
