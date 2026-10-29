# SPDX-License-Identifier: Apache-2.0
"""Hue ladder: the node wheel at several OKLab lightnesses, as chips and as names.

    python scripts/ladders/hue_ladder.py                     # L 0.70 0.75 0.80 0.85
    python scripts/ladders/hue_ladder.py --L 0.70 0.72 0.74  # a narrower round
    python scripts/ladders/hue_ladder.py --chroma max        # the most chroma at that L
    python scripts/ladders/hue_ladder.py --chroma 0.12       # one chroma for each hue

The first block is the HSV wheel that the spectrum used before 2026-10-29. The second is the
live spectrum (``theme._NODE_SPECTRUM``). The other blocks are candidates: each byte keeps
the hue angle of the HSV wheel, at one lightness for all hues. With ``--chroma keep`` (the
default), each byte also keeps its HSV chroma, and ``oklab.fitted`` lowers it where the
gamut cannot hold it.

The real ``PathLine`` draws each chip and each name. Thus the fills
(``CHIP_LIGHTNESS``), the seams (``SEAM_BLUR`` and the thin seam), and the labels follow
the live rules. A line of numbers is on each block: the lightness of the labels, the
difference in lightness between label and fill, the APCA contrast of the chips, and the
smallest distance between two names 16 bytes apart (how well the wheel keeps nodes apart).

JP chose ``_NODE_LIGHTNESS`` 0.72 on this ladder (2026-10-29).
"""

from __future__ import annotations

import argparse
import colorsys
import math
from collections.abc import Callable

from ladderkit import apca, console
from rich.console import Console
from rich.text import Text

from meshterm.ui import oklab, theme
from meshterm.ui.pathline import CHIP_LIGHTNESS, SEAM_BLUR, PathHop, PathLine, _chip_fill

Wheel = Callable[[int], str]


def hsv_hex(byte: int) -> str:
    """The colour of a byte on the HSV wheel that the spectrum used before 2026-10-29."""
    r, g, b = colorsys.hsv_to_rgb(byte / 256, theme._NODE_HUE_SAT, theme._NODE_HUE_VAL)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def oklch_hex(byte: int, lightness: float, chroma: str) -> str:
    """The HSV hue angle of a byte at one lightness, with the chroma that ``--chroma`` names."""
    _, a, b = oklab.from_hex(hsv_hex(byte))
    angle, own = math.atan2(b, a), math.hypot(a, b)
    c = 0.5 if chroma == "max" else own if chroma == "keep" else float(chroma)
    return oklab.to_hex(oklab.fitted((lightness, c * math.cos(angle), c * math.sin(angle))))


def live_hex(byte: int) -> str:
    """The colour of a byte on the live spectrum."""
    return theme._NODE_SPECTRUM[byte]


def stats(colour_of: Wheel) -> str:
    """The line of numbers for one wheel."""
    labels = [colour_of(byte) for byte in range(256)]
    fills = [_chip_fill(label) for label in labels]
    ls = [oklab.from_hex(label)[0] for label in labels]
    dl = [oklab.from_hex(lb)[0] - oklab.from_hex(f)[0] for lb, f in zip(labels, fills, strict=True)]
    lc = [apca(lb, f) for lb, f in zip(labels, fills, strict=True)]
    step = [oklab.distance(labels[i], labels[(i + 16) % 256]) for i in range(256)]
    return (
        f"label L {min(ls):.2f}–{max(ls):.2f} · ΔL {min(dl):.2f}–{max(dl):.2f}"
        f" · APCA {min(lc):.0f}–{max(lc):.0f} · 16-step name distance min {min(step):.3f}"
    )


def hops(colour_of: Wheel, bytes_: list[int]) -> list[PathHop]:
    """The hops of a line: each byte as its own label, in the colour of the wheel."""
    return [PathHop(f"{b:02x}", style=f"bold {colour_of(b)}") for b in bytes_]


WHEEL = list(range(0, 256, 16))
RUNS = {
    "greens": list(range(0x40, 0x60, 4)),  # near hues blur here
    "blues ": list(range(0x98, 0xB8, 4)),  # the weakest contrast on the HSV wheel
    "yellow": list(range(0x18, 0x38, 4)),  # the strongest contrast on the HSV wheel
}


def block(con: Console, title: str, colour_of: Wheel) -> None:
    """One wheel: the whole wheel as chips, three runs of near hues, and the names."""
    con.print(Text(f"── {title} ──", style="bold"), Text(stats(colour_of), style="#a2a2a2"))
    wheel = PathLine(hops(colour_of, WHEEL), mode="powerline").text()
    con.print(Text("  wheel   ").append_text(wheel))
    for name, run in RUNS.items():
        chips = PathLine(hops(colour_of, run), mode="powerline").text()
        names = PathLine(hops(colour_of, run), mode="plain").text()
        con.print(Text(f"  {name}  ").append_text(chips).append("   ").append_text(names))
    names = PathLine(hops(colour_of, WHEEL), mode="plain").text()
    con.print(Text("  names   ").append_text(names))
    con.print()


def main() -> None:
    """Draw the HSV wheel, the live spectrum, and one wheel for each ``--L``."""
    parser = argparse.ArgumentParser(description="The node wheel at several OKLab lightnesses.")
    parser.add_argument("--L", type=float, nargs="+", default=[0.70, 0.75, 0.80, 0.85])
    parser.add_argument("--chroma", default="keep", help="keep | max | a number such as 0.12")
    args = parser.parse_args()
    con = console()
    con.print(
        Text(
            f"CHIP_LIGHTNESS {CHIP_LIGHTNESS} · SEAM_BLUR {SEAM_BLUR} · chroma {args.chroma}"
            " · chips are byte labels on the live fill rule",
            style="#a2a2a2",
        )
    )
    con.print()
    block(con, "HSV S 0.65 V 0.95 (before 2026-10-29)", hsv_hex)
    block(con, f"live: theme spectrum, L {theme._NODE_LIGHTNESS:.2f}", live_hex)
    for lightness in args.L:
        block(
            con,
            f"OKLCH L {lightness:.2f}",
            lambda b, lightness=lightness: oklch_hex(b, lightness, args.chroma),
        )


if __name__ == "__main__":
    main()
