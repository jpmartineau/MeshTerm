# SPDX-License-Identifier: Apache-2.0
"""OKLab — the perceptual colour space the UI measures and shades ``#rrggbb`` values in.

Where the app has to ask a question about two colours *as the eye sees them* — are these
two chip fills the same to a reader, what is a slightly darker version of this one — it
asks here, not in RGB and not by hue. sRGB is a device encoding: a fixed step in it is a
different size to the eye in every region of the wheel (a step across the greens is a
whisper, the same step across the cyans a shout), and a hue-only comparison is worse,
blind to lightness and saturation altogether. OKLab (Björn Ottosson, 2020) is built so
that Euclidean distance approximates perceived difference and so that changing ``L``
alone changes lightness and nothing else, which is exactly the two operations wanted.

Everything is the reference transform, no dependencies: linear sRGB → LMS → cube root →
Lab. ``L`` runs 0 (black) to 1 (white); ``a``/``b`` are the two chroma axes, roughly
±0.4 at the sRGB gamut's edge. A distance of about 0.02 is a just-noticeable difference
between two large patches.
"""

from __future__ import annotations

import math

#: An OKLab triple ``(L, a, b)``.
Lab = tuple[float, float, float]


def _linear(channel: float) -> float:
    """The sRGB transfer function, 0–1 encoded → 0–1 linear."""
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _encoded(channel: float) -> float:
    """The inverse: 0–1 linear → 0–1 sRGB encoded."""
    return 12.92 * channel if channel <= 0.0031308 else 1.055 * channel ** (1 / 2.4) - 0.055


def from_hex(colour: str) -> Lab:
    """``#rrggbb`` → OKLab.

    Args:
        colour: A six-digit hex colour, ``#`` optional, either case.

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
    """OKLab → ``#rrggbb``, each channel clipped to the sRGB gamut.

    Clipping is the honest answer for the one caller that can leave the gamut — a
    saturated fill darkened at constant chroma pushes a channel below zero — because the
    clipped colour is the nearest the terminal can show, and it stays the right hue to
    the eye at the shifts the app asks for.
    """
    linear = _linear_rgb(lab)
    return "#" + "".join(f"{round(_encoded(min(1.0, max(0.0, c))) * 255):02x}" for c in linear)


def _linear_rgb(lab: Lab) -> tuple[float, float, float]:
    """OKLab → linear sRGB. A channel outside 0 to 1 is outside the sRGB gamut."""
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
    """Whether the sRGB gamut holds ``lab`` (a very small tolerance for rounding)."""
    return all(-1e-6 <= channel <= 1 + 1e-6 for channel in _linear_rgb(lab))


def fitted(lab: Lab) -> Lab:
    """``lab`` with its chroma lowered until the sRGB gamut holds it.

    The lightness and the hue do not change. A colour that is already in the gamut comes
    back unchanged. Use this instead of the clip in :func:`to_hex` when a set of colours
    must stay apart: a dark, saturated colour leaves the gamut, and the clip moves many
    such colours to one corner of the gamut. For example, two greens a few steps apart
    on the hue wheel both clip to ``#008000``.
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
    """Perceived difference between two ``#rrggbb`` colours: Euclidean distance in OKLab.

    ``0`` for the same colour; about ``0.02`` at the threshold of noticing between two
    large patches; ``1`` is black to white.
    """
    return math.dist(from_hex(first), from_hex(second))


def shaded(colour: str, by: float, *, floor: float | None = None) -> str:
    """``colour`` moved ``by`` in lightness, away from whichever end it is nearer.

    A light colour comes back darker and a dark one lighter, at the same chroma, so the
    result is recognisably *the same colour, shaded* rather than a different one — the
    version of a fill that draws a line on itself.

    Args:
        colour: The ``#rrggbb`` to shade.
        by: The lightness shift, in OKLab ``L`` (``0.1`` is a clear step, ``0.25`` bold).
        floor: If given, the shift starts from this lightness rather than the colour's
            own where that is further along — a caller guaranteeing the result clears a
            *second* colour's lightness by ``by`` as well as this one's passes that
            colour's ``L``. Read as "at least this far from both".
    """
    lightness, a, b = from_hex(colour)
    if lightness >= 0.5:
        start = lightness if floor is None else min(lightness, floor)
        return to_hex((start - by, a, b))
    start = lightness if floor is None else max(lightness, floor)
    return to_hex((start + by, a, b))
