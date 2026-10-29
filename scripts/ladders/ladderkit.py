# SPDX-License-Identifier: Apache-2.0
"""The shared parts of the ladders: a console that keeps its colour, and two contrast measures.

A ladder draws real colours in the user's own terminal. On Windows, three things can remove
that colour, and :func:`console` stops each of them (refer to ``README.md``, "When Claude
opens the window").
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: The root of the repository. A ladder imports ``meshterm`` from this checkout, and not
#: from an installed package, so that it always reads the live constants.
REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from rich.console import Console  # noqa: E402


def _enable_vt() -> str:
    """Turn on virtual terminal processing for stdout, so that the console reads ANSI codes."""
    if os.name != "nt":
        return "n/a"
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    mode = ctypes.c_uint32()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return "not a console"
    ok = kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # VIRTUAL_TERMINAL_PROCESSING
    return f"mode {mode.value:#x} -> {'on' if ok else 'FAILED'}"


def console(width: int = 220) -> Console:
    """A Rich console that always writes truecolor ANSI codes.

    * ``no_color=False``: a Windows Terminal tab that Claude Code opens gets the environment
      of Claude Code, which has ``NO_COLOR=1``. Rich removes all colour when it sees it.
    * ``legacy_windows=False``, and virtual terminal processing on: when the processing is
      off, Rich uses the console API of Windows. That API has 16 colours, and in a capture
      it writes no colour at all.

    If the environment variable ``LADDER_MARKER`` names a file, the function writes what it
    found to that file. ``launch.ps1`` reads the file to make sure that the ladder started,
    with colour.
    """
    sys.stdout.reconfigure(encoding="utf-8")  # the powerline glyphs
    vt = _enable_vt()
    con = Console(
        force_terminal=True,
        color_system="truecolor",
        no_color=False,
        legacy_windows=False,
        width=width,
        highlight=False,
    )
    marker = os.environ.get("LADDER_MARKER")
    if marker:
        Path(marker).write_text(
            f"color_system={con.color_system} no_color={con.no_color}"
            f" legacy_windows={con.legacy_windows} vt={vt} isatty={sys.stdout.isatty()}\n",
            encoding="utf-8",
        )
    return con


def _channels(colour: str) -> tuple[float, float, float]:
    """The three channels of a ``#rrggbb`` colour, from 0 to 1."""
    return int(colour[1:3], 16) / 255, int(colour[3:5], 16) / 255, int(colour[5:7], 16) / 255


def wcag(text: str, ground: str) -> float:
    """The WCAG 2 contrast ratio of two ``#rrggbb`` colours (1 to 21)."""

    def luminance(colour: str) -> float:
        def linear(c: float) -> float:
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

        r, g, b = (linear(c) for c in _channels(colour))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    lighter, darker = sorted((luminance(text), luminance(ground)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def apca(text: str, ground: str) -> float:
    """The APCA contrast ``Lc`` of text on a ground (0.0.98G-4g), as a positive number.

    APCA is the contrast model of the WCAG 3 draft. Unlike the WCAG 2 ratio, it knows the
    polarity (light text on a dark ground is not the same as dark text on a light ground),
    and it is closer to what the eye sees for text. ``Lc`` 45 is a common minimum for body
    text, and ``Lc`` 30 for large text.
    """

    def screen_luminance(colour: str) -> float:
        r, g, b = _channels(colour)
        y = 0.2126729 * r**2.4 + 0.7151522 * g**2.4 + 0.0721750 * b**2.4
        return y + (0.022 - y) ** 1.414 if y < 0.022 else y

    yt, yg = screen_luminance(text), screen_luminance(ground)
    if yg > yt:  # dark text on a light ground
        s = (yg**0.56 - yt**0.57) * 1.14
        return 0.0 if s < 0.1 else (s - 0.027) * 100
    s = (yg**0.65 - yt**0.62) * 1.14  # light text on a dark ground
    return 0.0 if s > -0.1 else -(s + 0.027) * 100
