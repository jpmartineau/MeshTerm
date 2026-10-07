# SPDX-License-Identifier: Apache-2.0
"""OKLab: the perceptual colour space in which the UI measures and shades ``#rrggbb`` values.

Sometimes the app must compare two colours as the eye sees them. Two examples: are two
chip fills the same to the user, and what is a slightly darker version of this fill? The
app asks these questions here, not in RGB and not by hue. sRGB is a device encoding. A
fixed step in sRGB has a different size to the eye in each region of the colour wheel. A
step across the greens is small, but the same step across the cyans is large. A
comparison of hue only is worse, because it does not see lightness or saturation at all.
OKLab (Björn Ottosson, 2020) is made so that the Euclidean distance approximates the
perceived difference. It is also made so that a change of ``L`` alone changes the
lightness and nothing else. These are the two operations that the app needs.

The module uses the reference transform with no dependencies: linear sRGB → LMS → cube
root → Lab. ``L`` runs from 0 (black) to 1 (white). ``a`` and ``b`` are the two chroma
axes, approximately ±0.4 at the edge of the sRGB gamut. A distance of approximately 0.02
is the smallest difference that a user can see between two large patches.
"""

from __future__ import annotations

import math

#: An OKLab triple ``(L, a, b)``.
Lab = tuple[float, float, float]


def _linear(channel: float) -> float:
    """The sRGB transfer function: 0–1 encoded to 0–1 linear."""
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _encoded(channel: float) -> float:
    """The inverse function: 0–1 linear to 0–1 sRGB encoded."""
    return 12.92 * channel if channel <= 0.0031308 else 1.055 * channel ** (1 / 2.4) - 0.055


def from_hex(colour: str) -> Lab:
    """Change a ``#rrggbb`` colour to OKLab.

    Args:
        colour: A six-digit hex colour. The ``#`` is optional. The case does not matter.

    Raises:
        ValueError: If the string is not six hex digits.
    """
    raw = colour.removeprefix("#")
    if len(raw) != 6:
        raise ValueError(f"not a #rrggbb colour: {colour!r}")
    r, g, b = (_linear(int(raw[i : i + 2], 16) / 255) for i in (0, 2, 4))
    l_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m_ = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s_ = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def to_hex(lab: Lab) -> str:
    """Change an OKLab colour to ``#rrggbb``. Each channel is clipped to the sRGB gamut.

    The clip is the correct answer for the one caller that can leave the gamut. A
    saturated fill that is darkened at constant chroma pushes a channel below zero. The
    clipped colour is the nearest colour that the terminal can show. At the shifts that
    the app uses, it also keeps the correct hue to the eye.
    """
    linear = _linear_rgb(lab)
    return "#" + "".join(f"{round(_encoded(min(1.0, max(0.0, c))) * 255):02x}" for c in linear)


def _linear_rgb(lab: Lab) -> tuple[float, float, float]:
    """Change an OKLab colour to linear sRGB. A channel outside 0 to 1 is outside the sRGB gamut."""
    lightness, a, b = lab
    l_ = lightness + 0.3963377774 * a + 0.2158037573 * b
    m_ = lightness - 0.1055613458 * a - 0.0638541728 * b
    s_ = lightness - 0.0894841775 * a - 1.2914855480 * b
    lms = (l_**3, m_**3, s_**3)
    return (
        4.0767416621 * lms[0] - 3.3077115913 * lms[1] + 0.2309699292 * lms[2],
        -1.2684380046 * lms[0] + 2.6097574011 * lms[1] - 0.3413193965 * lms[2],
        -0.0041960863 * lms[0] - 0.7034186147 * lms[1] + 1.7076147010 * lms[2],
    )


def _in_gamut(lab: Lab) -> bool:
    """Check if the sRGB gamut holds ``lab``. A very small tolerance is for rounding."""
    return all(-1e-6 <= channel <= 1 + 1e-6 for channel in _linear_rgb(lab))


def fitted(lab: Lab) -> Lab:
    """``lab`` with its chroma lowered until the sRGB gamut holds it.

    The lightness and the hue do not change. A colour that is already in the gamut is
    returned unchanged. Use this function instead of the clip in :func:`to_hex` when a set
    of colours must stay apart. A dark, saturated colour leaves the gamut, and the clip
    moves many such colours to one corner of the gamut. For example, two greens that are
    a few steps apart on the hue wheel both clip to ``#008000``.
    """
    if _in_gamut(lab):
        return lab
    lightness, a, b = lab
    low, high = 0.0, 1.0
    for _ in range(24):  # the chroma scale, to approximately 1e-7
        middle = (low + high) / 2
        if _in_gamut((lightness, a * middle, b * middle)):
            low = middle
        else:
            high = middle
    return (lightness, a * low, b * low)


def distance(first: str, second: str) -> float:
    """The perceived difference between two ``#rrggbb`` colours: the Euclidean distance in OKLab.

    The distance is ``0`` for the same colour. Approximately ``0.02`` is the smallest
    difference that a user can see between two large patches. The distance from black to
    white is ``1``.
    """
    return math.dist(from_hex(first), from_hex(second))


def shaded(colour: str, by: float, *, floor: float | None = None) -> str:
    """``colour`` moved ``by`` in lightness, away from the end of the scale that is nearer.

    A light colour is returned darker and a dark colour is returned lighter, at the same
    chroma. Thus the user sees the same colour, shaded, and not a different colour. This
    is the version of a fill that draws a line on itself.

    Args:
        colour: The ``#rrggbb`` colour to shade.
        by: The lightness shift, in OKLab ``L`` (``0.1`` is a clear step, ``0.25`` is bold).
        floor: If it is given, the shift starts from this lightness instead of the
            lightness of the colour, where this lightness is further along. A caller
            that must clear the lightness of a second colour by ``by``, and also that of
            this colour, passes the ``L`` of the second colour. The result is at least
            this far from both colours.
    """
    lightness, a, b = from_hex(colour)
    if lightness >= 0.5:
        start = lightness if floor is None else min(lightness, floor)
        return to_hex((start - by, a, b))
    start = lightness if floor is None else max(lightness, floor)
    return to_hex((start + by, a, b))
