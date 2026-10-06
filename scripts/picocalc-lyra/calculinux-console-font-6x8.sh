#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# calculinux-console-font-6x8.sh -- build and install meshterm8.psf.gz, the 6x8 (53x40)
# console font for the PicoCalc panel.
#
# LICENCE -- READ THIS FIRST. The base bitmap embedded below is the Linux kernel's own
# font_6x8 (lib/fonts/font_6x8.c), which is GPL-2.0. This script is therefore GPL-2.0-only
# and so is the PSF it produces; the full licence text sits beside it as
# scripts/picocalc-lyra/LICENSE.GPL-2.0. The rest of MeshTerm is Apache-2.0 and stays that way: the
# produced meshterm8.psf.gz is a DATA FILE the Linux kernel console reads with setfont, on
# the device, at run time. MeshTerm never links it, never bundles it, and never
# redistributes it -- it is built on the machine it runs on, from this script, and the
# Python package excludes scripts/ from its sdist entirely. Nothing GPL-2.0 enters the
# distributed program; MeshTerm merely names a file on the console's filesystem.
#
# WHERE THE SHARED PARTS LIVE. The donor, alias and keeper tables, the braille generator
# and the PSF2 writer are NOT duplicated here. They are read out of
# calculinux-console-font-6x12.sh (Apache-2.0), between its "shared generator (BEGIN)" and
# "(END)" marker lines, and exec'd -- so the two fonts can never drift, and a repo test
# that parses those tables has exactly one file to parse. That file must sit beside this
# one; point SHARED at it if it does not. The shared generator is additionally offered
# under GPL-2.0-only by its source file, permitting its extraction and use here.
#
# WHAT IS ONLY HERE. The GPL-2.0 base bitmap, its CP437 unicode mapping, and the same 18
# marks redrawn for the shorter cell (MARKS8).
#
# The 6x8 base has CP437 coverage, so unlike the Terminus base it has NO CYRILLIC: a node
# named in Cyrillic draws as tofu in this font. The keeper list drops the Cyrillic canary
# accordingly, and only pi is checked.
#
# Run as root on the Lyra, either on its own or through calculinux-console-font-6x12.sh, which
# calls it:
#
#     sh calculinux-console-font-6x8.sh
#
# Idempotent. It installs the font and nothing else -- it never runs setfont, never touches
# /etc/vconsole.conf and never touches the palette. Choose the font live from MeshTerm's
# Preferences page (Display -> Console font), or by hand with
# `setfont /usr/share/consolefonts/meshterm8.psf.gz`.
set -eu

FONT_NAME=meshterm
CONSOLEFONTS=/usr/share/consolefonts
OUT8="$CONSOLEFONTS/${FONT_NAME}8.psf.gz"
HERE=$(dirname "$0")
SHARED=${SHARED:-"$HERE/calculinux-console-font-6x12.sh"}   # Apache-2.0; holds the shared tables

# --- preflight: fail early with a plain reason, never half-apply -----------------------
[ "$(id -u)" = 0 ] || { echo "error: run as root (writes $CONSOLEFONTS)" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "error: python3 not found" >&2; exit 1; }
[ -f "$SHARED" ] || { echo "error: shared generator not found: $SHARED" >&2; exit 1; }

echo "building $OUT8 (6x8) ..."
SHARED="$SHARED" OUT8="$OUT8" python3 - <<'PYEOF'
import base64
import os
import re

# --- the shared generator, borrowed rather than copied ---------------------------------
# Everything between the two marker lines in calculinux-console-font-6x12.sh: the braille
# generator, art(), DONORS, ALIASES, KEEP_COMMON, BANDS8 and build(). One copy of those
# tables, in the Apache-2.0 file, both fonts built from it.
SHARED = os.environ["SHARED"]
_source = open(SHARED, encoding="utf-8").read()
_block = re.search(
    r"^# --- shared generator \(BEGIN\).*?$(.*?)^# --- shared generator \(END\)",
    _source, re.S | re.M)
if _block is None:
    raise SystemExit(
        "no 'shared generator' block in %s -- the two font scripts have drifted" % SHARED)
exec(compile(_block.group(1), SHARED, "exec"), globals())

OUT8 = os.environ["OUT8"]

# The same 18 marks redrawn for the 6x8 cell (first-draft art; a later tweak round
# refines whichever font wins the A/B).
MARKS8 = {
    0x25CF: art([  # BLACK CIRCLE
        "......", "..##..", ".####.", "######", "######", ".####.", "..##..", "......"]),
    0x25C9: art([  # FISHEYE
        "......", "..##..", ".#..#.", "#.##.#", "#.##.#", ".#..#.", "..##..", "......"]),
    0x2605: art([  # BLACK STAR
        "......", "..#...", "..#...", "######", ".####.", "..##..", ".#..#.", "......"]),
    0x2014: art([  # EM DASH
        "......", "......", "......", "######", "######", "......", "......", "......"]),
    0x2713: art([  # CHECK MARK
        "......", "......", ".....#", "....#.", "#..#..", ".##...", ".#....", "......"]),
    0x2717: art([  # BALLOT X
        "......", "#...#.", ".#.#..", "..#...", ".#.#..", "#...#.", "......", "......"]),
    0x25B6: art([  # RIGHT-POINTING TRIANGLE
        "#.....", "##....", "###...", "####..", "####..", "###...", "##....", "#....."]),
    0x276F: art([  # HEAVY RIGHT ANGLE QUOTE -- the list cursor
        "##....", ".##...", "..##..", "...##.", "..##..", ".##...", "##....", "......"]),
    0x25B8: art([  # SMALL RIGHT-POINTING TRIANGLE -- reorder cursor
        "......", "......", ".#....", ".##...", ".###..", ".##...", ".#....", "......"]),
    0x2026: art([  # HORIZONTAL ELLIPSIS
        "......", "......", "......", "......", "......", "#.#.#.", "#.#.#.", "......"]),
    0x26A0: art([  # WARNING SIGN
        "..##..", ".#..#.", "#.##.#", "#.##.#", "#....#", "#.##.#", "######", "......"]),
    0x232B: art([  # ERASE TO THE LEFT
        "......", "..####", ".##.##", "#..#.#", ".##.##", "..####", "......", "......"]),
    0x21E7: art([  # UPWARDS WHITE ARROW -- shift
        "..##..", ".#..#.", "#....#", "##..##", ".#..#.", ".#..#.", ".####.", "......"]),
    0x2699: art([  # GEAR
        "......", "..##..", "######", "##..##", "##..##", "######", "..##..", "......"]),
    0x21BB: art([  # CLOCKWISE OPEN CIRCLE ARROW -- refresh
        "....#.", ".#####", "#...#.", "#.....", "#.....", "#....#", ".####.", "......"]),
    0x25F7: art([  # CLOCK FACE
        "......", ".####.", "#..#.#", "#..###", "#....#", "#....#", ".####.", "......"]),
    0x2316: art([  # POSITION INDICATOR -- crosshair
        "..##..", "......", "#.##.#", "#.##.#", "......", "..##..", "......", "......"]),
    0x26BF: art([  # SQUARED KEY -- padlock
        ".####.", ".#..#.", "######", "##..##", "##..##", "######", "######", "......"]),
}

# --- the base bitmap: the Linux kernel's font_6x8 (GPL-2.0; see the header) ------------
FONT8 = base64.b64decode("""
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
""")
assert len(FONT8) == 2048
glyphs8 = [FONT8[i * 8:(i + 1) * 8] for i in range(256)]
# CP437's graphics mapping: the control range 0x00-0x1F holds pictographs, 0x7F a house;
# the rest decodes through Python's cp437 codec.
CP437_LOW = [
    0x0000, 0x263A, 0x263B, 0x2665, 0x2666, 0x2663, 0x2660, 0x2022,
    0x25D8, 0x25CB, 0x25D9, 0x2642, 0x2640, 0x266A, 0x266B, 0x263C,
    0x25BA, 0x25C4, 0x2195, 0x203C, 0x00B6, 0x00A7, 0x25AC, 0x21A8,
    0x2191, 0x2193, 0x2192, 0x2190, 0x221F, 0x2194, 0x25B2, 0x25BC,
]
entries8 = []
for i in range(256):
    if i < 0x20:
        cp = CP437_LOW[i]
    elif i == 0x7F:
        cp = 0x2302
    else:
        cp = ord(bytes([i]).decode("cp437"))
    entries8.append(chr(cp).encode("utf-8") if cp else b"")
build(glyphs8, entries8, 8, 8, MARKS8, BANDS8, [0x03C0], OUT8)
PYEOF

echo "done -- flip to it with 'setfont $OUT8' (53x40), or from MeshTerm's Preferences page."
