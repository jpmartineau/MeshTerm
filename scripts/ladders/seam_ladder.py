# SPDX-License-Identifier: Apache-2.0
"""Seam ladder: real pairs of chips, sorted by the OKLab distance of their fills.

    python scripts/ladders/seam_ladder.py                 # the ladder at the live SEAM_BLUR
    python scripts/ladders/seam_ladder.py --blur 0.08     # move the threshold, without an edit
    python scripts/ladders/seam_ladder.py --per-bin 3 --max 0.20 --step 0.01

Each pair is drawn in two ways: with the solid interlock and with the thin chevron. A line
crosses the ladder at the threshold. Under the line, the widget draws the thin seam, and
over it, the solid seam. The question for the user: on which pairs can you find the solid
point without a search? The last part draws the wheel in steps of ``0x08``, as the widget
draws it now.

JP set ``SEAM_BLUR`` to 0.06 on the first version of this ladder (2026-09-16). This version
was made again on 2026-10-28. JP set the value on the HSV wheel. The spectrum of one
lightness (2026-10-29) has fills that differ only in hue and chroma.
"""

from __future__ import annotations

import argparse

from ladderkit import console
from rich.text import Text

from meshterm.ui import oklab, pathline
from meshterm.ui.pathline import (
    POWERLINE_SEP,
    POWERLINE_THIN,
    PathHop,
    PathLine,
    _spectrum_colours,
    _spectrum_seam,
)

JND = 0.02  # the smallest difference that a user can see between two large patches


def hop(byte: int) -> PathHop:
    """The hop of a node whose key starts with ``byte``."""
    return PathHop(f"{byte:02x}", key=f"{byte:02x}")


def pair(a: PathHop, b: PathHop, form: str) -> Text:
    """Two chips with a seam between them: ``solid``, ``thin``, or what the widget draws."""
    ca, cb = _spectrum_colours(a), _spectrum_colours(b)
    text = Text()
    text.append_text(PathLine._chip(a, ca))
    if form == "solid":
        text.append(POWERLINE_SEP, style=f"{ca.fill} on {cb.fill}")
    elif form == "thin":
        text.append(POWERLINE_THIN, style=f"{_spectrum_seam(ca)} on {cb.fill}")
    else:
        text.append_text(PathLine._seam(ca, cb))
    text.append_text(PathLine._chip(b, cb))
    return text


def around(x: float, y: float) -> float:
    """The distance between two positions on the wheel of 256 bytes."""
    return min((x - y) % 256, (y - x) % 256)


def main() -> None:
    """Draw the ladder of pairs, then the wheel as the widget draws it."""
    parser = argparse.ArgumentParser(description="Pairs of chips around the SEAM_BLUR line.")
    parser.add_argument("--blur", type=float, default=pathline.SEAM_BLUR)
    parser.add_argument("--per-bin", type=int, default=2)
    parser.add_argument("--step", type=float, default=0.005)
    parser.add_argument("--max", type=float, default=0.15)
    parser.add_argument("--max-gap", type=int, default=48, help="largest first-byte gap sampled")
    args = parser.parse_args()
    in_code = pathline.SEAM_BLUR
    pathline.SEAM_BLUR = args.blur  # _fills_blur reads the module global when it runs

    con = console()
    fills = {b: _spectrum_colours(hop(b)).fill for b in range(256)}

    pairs = []
    for gap in range(1, args.max_gap + 1):
        for a in range(256):
            b = (a + gap) % 256
            pairs.append((oklab.distance(fills[a], fills[b]), a, b))

    bins: dict[int, list[tuple[float, int, int]]] = {}
    for d, a, b in pairs:
        if d < args.max:
            bins.setdefault(int(d / args.step), []).append((d, a, b))

    rows = []
    for index in sorted(bins):
        members = bins[index]
        # Take one pair from each sector of the wheel, and turn the sectors a little for each
        # bin. Thus the ladder shows each part of the wheel, and not only the reds at its start.
        offset = (index * 53) % 256
        chosen = set()
        for i in range(args.per_bin):
            centre = (offset + i * 256 / args.per_bin) % 256
            best = min(
                members,
                key=lambda p, centre=centre: around(p[1] + ((p[2] - p[1]) % 256) / 2, centre),
            )
            chosen.add(best)
        rows += chosen
    rows.sort()

    con.print()
    con.print(
        Text("Seam ladder", style="bold"),
        Text(
            f"  ·  SEAM_BLUR {args.blur:.3f}  (code: {in_code:.3f})  ·  1 JND = {JND}",
            style="#a2a2a2",
        ),
    )
    con.print(
        Text(
            "Under the line the seam is thin; over it, solid. Which one can you find "
            "without a search?",
            style="#a2a2a2",
        )
    )
    con.print()
    con.print(Text("   dist   JND   pair      solid         thin          today", style="bold"))
    ruled = False
    for d, a, b in rows:
        if not ruled and d >= args.blur:
            con.print(Text(f"  ── SEAM_BLUR {args.blur:.3f} " + "─" * 46, style="bold #e5c07b"))
            ruled = True
        verdict = "thin" if d < args.blur else "solid"
        line = Text(f"  {d:.3f}  {d / JND:4.1f}   {a:02x}→{b:02x}    ")
        line.append_text(pair(hop(a), hop(b), "solid"))
        line.append("    ")
        line.append_text(pair(hop(a), hop(b), "thin"))
        line.append("    ")
        line.append(verdict, style="#a2a2a2" if verdict == "thin" else "bold")
        con.print(line)

    con.print()
    con.print(Text("Around the wheel, as the widget draws it now (steps of 0x08):", style="bold"))
    for start in range(0, 256, 64):
        ribbon = Text("  ")
        bytes_ = list(range(start, start + 64, 8))
        for i, byte in enumerate(bytes_):
            c = _spectrum_colours(hop(byte))
            if i:
                ribbon.append_text(PathLine._seam(_spectrum_colours(hop(bytes_[i - 1])), c))
            ribbon.append_text(PathLine._chip(hop(byte), c))
        con.print(ribbon)
    con.print()


if __name__ == "__main__":
    main()
