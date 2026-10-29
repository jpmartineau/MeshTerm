# SPDX-License-Identifier: Apache-2.0
"""The contrast between the label and the fill of each node chip, as a table.

    python scripts/ladders/contrast.py            # each 16th byte of the live spectrum
    python scripts/ladders/contrast.py --step 1   # all 256 bytes
    python scripts/ladders/contrast.py --hsv      # the HSV wheel of before 2026-10-29

This script is not a ladder. It is the measurement that comes before one: it shows the
problem as numbers. Three measures are in the table: the OKLab difference in lightness, the
WCAG 2 ratio, and the APCA ``Lc`` (refer to ``ladderkit.apca``). On the HSV wheel, the APCA
contrast went from 19 (blue) to 64 (yellow). This number started the hue ladder.
"""

from __future__ import annotations

import argparse

from hue_ladder import hsv_hex
from ladderkit import apca, console, wcag
from rich.text import Text

from meshterm.ui import oklab, theme
from meshterm.ui.pathline import _chip_fill


def main() -> None:
    """Print the table and the range of each measure."""
    parser = argparse.ArgumentParser(description="Label-on-fill contrast of the node chips.")
    parser.add_argument("--step", type=int, default=16, help="show each Nth byte")
    parser.add_argument("--hsv", action="store_true", help="measure the HSV wheel of before")
    args = parser.parse_args()
    con = console(width=120)
    colour_of = hsv_hex if args.hsv else (lambda byte: theme._NODE_SPECTRUM[byte])

    rows = []
    for byte in range(256):
        label = colour_of(byte)
        fill = _chip_fill(label)
        lab_l, fill_l = oklab.from_hex(label)[0], oklab.from_hex(fill)[0]
        rows.append((byte, label, fill, lab_l, fill_l, wcag(label, fill), apca(label, fill)))

    header = "byte  chip   label    fill      L label/fill   ΔL    WCAG   APCA"
    con.print(Text(header, style="bold"))
    for byte, label, fill, lab_l, fill_l, ratio, lc in rows[:: args.step]:
        line = Text(f" {byte:02x}   ")
        line.append(f" {byte:02x} ", style=f"bold {label} on {fill}")
        line.append(
            f"  {label}  {fill}   {lab_l:.2f} / {fill_l:.2f}   {lab_l - fill_l:.2f}"
            f"   {ratio:4.2f}   {lc:5.1f}"
        )
        con.print(line)
    for name, index in (("L label", 3), ("WCAG", 5), ("APCA", 6)):
        low, high = min(rows, key=lambda r: r[index]), max(rows, key=lambda r: r[index])
        con.print(
            Text(
                f"{name:8} min {low[index]:6.2f} (byte {low[0]:02x})"
                f"   max {high[index]:6.2f} (byte {high[0]:02x})",
                style="#a2a2a2",
            )
        )


if __name__ == "__main__":
    main()
