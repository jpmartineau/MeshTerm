#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# calculinux-console-font-6x12.sh -- build, install, and persist the "meshterm" console fonts
# and the 16-slot palette.
#
# MeshTerm runs on a Luckfox Lyra inside a ClockworkPi PicoCalc under Calculinux. It draws
# to the bare ILI9488 framebuffer console (fbcon). That console loads one PSF font, and
# each font that Calculinux ships is a classic 256-glyph VGA font. Thus, as it comes, the
# display cannot draw the things that the UI of MeshTerm uses most: braille charts
# (U+2800-U+28FF), the node and status marks, the marks of the P3 compact icons, rounded
# panel corners, and the pointer of the list. This script makes the 512-glyph PSF2 font in
# which MeshTerm runs:
#
#   * meshterm.psf.gz   6x12 (Terminus base)     -> 53x26 -- the installed default
#
# meshterm.psf.gz is a derivative of Terminus Font (OFL-1.1). It must not have the name
# "Terminus". The stock ter-u12n.psf.gz base keeps its own OFL-1.1 licence, which is not
# the Apache-2.0 licence of this repository.
#
# The companion for the taller display, meshterm8.psf.gz (6x8 -> 53x40), is built by
# calculinux-console-font-6x8.sh. This script runs it for you when it is in the same
# directory (set SKIP_6X8=1 to build only the default). It is in its own file because its
# base bitmap is the font_6x8 of the Linux kernel, and that is GPL-2.0. The purpose of the
# split is to keep that material in one file with a clear mark. The two scripts share ONE
# copy of the donor, alias, and keeper tables, and the whole generator. This copy is the
# block between the "shared generator" markers below. The 6x8 script extracts it from this
# file and executes it with exec. If you change those tables here, both fonts change
# together. The shared generator block is also offered under GPL-2.0-only, to permit its
# use by calculinux-console-font-6x8.sh.
#
# Both fonts install into /usr/share/consolefonts. To change the font live, use
# `setfont /usr/share/consolefonts/meshterm8.psf.gz` (and use meshterm.psf.gz to change
# it back). You can also use the Preferences page of MeshTerm (Display -> Console font).
# To make a permanent choice, set FONT= in /etc/vconsole.conf.
#
# Why 512 and not more: fbcon limits a font to 512 glyphs. Braille alone is 256, and the
# base set is another 256. The budget is full, so MeshTerm draws each extra mark into a
# DONOR slot (a pictograph that MeshTerm never sends) and points its codepoint to the new
# mark. To choose a donor, you must know the FULL codepoint list of its slot. Bases alias
# lookalikes onto one glyph (when we used pi as a donor, it erased Cyrillic pe). Thus a
# donor that is not found stops the build, and the script checks a keeper list after the
# build. The rounded corners and the midline ellipsis cost no glyph, because they are
# aliased onto existing bitmaps.
#
# All other glyphs are pure geometry, generated on the handheld. The script needs only the
# stock Terminus font and python3. Run it as root on the Lyra (the serial console is
# acceptable):
#
#     sh calculinux-console-font-6x12.sh
#
# The script is idempotent, so it is safe to run again after an update of MeshTerm or a
# change to a font. It also manages the 16-slot palette. A default run restores the stock
# VT palette (theme._VT_SLOTS) and removes each custom remap. MESHTERM_CUSTOM_PALETTE=1
# installs the archived remap (theme._VT_SLOTS_CUSTOM, which a test pins to
# theme.vtrgb_lines()).
set -eu

FONT_NAME=meshterm
CONSOLEFONTS=/usr/share/consolefonts
BASE="$CONSOLEFONTS/ter-u12n.psf.gz"          # stock Terminus 6x12, 256 glyphs
OUT="$CONSOLEFONTS/$FONT_NAME.psf.gz"          # the 6x12 default
OUT8="$CONSOLEFONTS/${FONT_NAME}8.psf.gz"      # the 6x8 companion, built by its own script
VCONSOLE=/etc/vconsole.conf
HERE=$(dirname "$0")
BUILD8="$HERE/calculinux-console-font-6x8.sh"  # GPL-2.0; see its header

# --- preflight: stop early with a plain reason, and never apply half of the change ------
[ "$(id -u)" = 0 ] || { echo "error: run as root (writes $CONSOLEFONTS and $VCONSOLE)" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "error: python3 not found" >&2; exit 1; }
command -v setfont >/dev/null 2>&1 || { echo "error: setfont not found (install kbd tools)" >&2; exit 1; }
[ -f "$BASE" ] || { echo "error: base font not found: $BASE" >&2; exit 1; }

# --- build the 6x12 font -----------------------------------------------------------------
# The generator is inline (a quoted heredoc, so the shell expands nothing) and reads its
# paths from the environment. It is the only source of truth for the glyphs that the
# script makes. calculinux-console-font-6x8.sh extracts the block between the "shared
# generator" markers and executes it with exec, without a change. Thus the donor, alias,
# and keeper tables exist ONCE.
echo "building $OUT (6x12) ..."
BASE="$BASE" OUT="$OUT" python3 - <<'PYEOF'
import os

# --- shared generator (BEGIN) ---------------------------------------------------------
# Read by calculinux-console-font-6x8.sh out of this very file, between these two markers.
# Keep it self-contained: no os.environ reads, nothing specific to one cell size, and no
# reference to anything defined after the (END) marker.
import gzip
import struct

PSF2_MAGIC = 0x864AB572

# --- braille: 2-wide dot grid; the codepoint's low byte says which dots lit ------------
# MeshTerm never sets braille to be read: every braille cell it draws is pixels -- a chart,
# the map, a meter. So each dot is a solid tile of its whole share of the cell, half the
# width by a quarter of the height, with no gap between dots or between neighbouring
# cells, and a run of lit dots reads as one continuous mark instead of a dotted one.
COLS = [[0, 1, 2], [3, 4, 5]]
BIT = {0: (0, 0), 1: (0, 1), 2: (0, 2), 6: (0, 3),
       3: (1, 0), 4: (1, 1), 5: (1, 2), 7: (1, 3)}

#: Dot-row bands per cell height: four 3px rows at 6x12, four 2px rows at 6x8.
BANDS12 = [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9, 10, 11]]
BANDS8 = [[0, 1], [2, 3], [4, 5], [6, 7]]


def braille_glyph(value, bands, height):
    rows = [0] * height
    for bit in range(8):
        if value >> bit & 1:
            col, row = BIT[bit]
            for x in COLS[col]:
                for y in bands[row]:
                    rows[y] |= 0x80 >> x
    return bytes(rows)


# --- marks + cursors, drawn as pixel art ('#' lit) -------------------------------------
def art(rows):
    return bytes(sum(0x80 >> x for x, ch in enumerate(r) if ch == "#") for r in rows)


MARKS = {
    0x25CF: art([  # BLACK CIRCLE -- node / unread
        "......", "......", "..##..", ".####.", "######", "######",
        "######", "######", ".####.", "..##..", "......", "......"]),
    0x25C9: art([  # FISHEYE -- sensor
        "......", "......", "..##..", ".####.", "##..##", "#.##.#",
        "#.##.#", "##..##", ".####.", "..##..", "......", "......"]),
    0x2605: art([  # BLACK STAR -- you / best
        "......", "..##..", "..##..", "######", ".####.", "..##..",
        ".####.", "##..##", "#....#", "......", "......", "......"]),
    0x2014: art([  # EM DASH -- title separator, full width
        "......", "......", "......", "......", "......", "######",
        "######", "......", "......", "......", "......", "......"]),
    0x2713: art([  # CHECK MARK -- ok
        "......", "......", "......", ".....#", "....#.", "...#..",
        "#.#...", ".#....", "......", "......", "......", "......"]),
    0x2717: art([  # BALLOT X -- error
        "......", "......", "......", "#....#", ".#..#.", "..##..",
        "..##..", ".#..#.", "#....#", "......", "......", "......"]),
    0x25B6: art([  # RIGHT-POINTING TRIANGLE -- run / play
        "......", "#.....", "##....", "###...", "####..", "#####.",
        "#####.", "####..", "###...", "##....", "#.....", "......"]),
    0x276F: art([  # HEAVY RIGHT ANGLE QUOTE -- THE list selection cursor ("> ")
        "......", "##....", ".##...", "..##..", "...##.", "....##",
        "....##", "...##.", "..##..", ".##...", "##....", "......"]),
    0x25B8: art([  # SMALL RIGHT-POINTING TRIANGLE -- grabbed/reorder cursor
        "......", "......", "......", ".#....", ".##...", ".###..",
        ".###..", ".##...", ".#....", "......", "......", "......"]),
    # --- P3 marks: the compact icon language (see meshterm/ui/theme._GLYPH_MAP and
    # --- meshterm/ui/fontset.py, which must mirror every codepoint drawn here) --------
    0x2026: art([  # HORIZONTAL ELLIPSIS -- "opens further prompts", truncation
        "......", "......", "......", "......", "......", "......",
        "......", "......", "#.#.#.", "#.#.#.", "......", "......"]),
    0x26A0: art([  # WARNING SIGN -- the warn status mark
        "......", "..##..", ".#..#.", ".#..#.", "#....#", "#.##.#",
        "#.##.#", "#....#", "#.##.#", "######", "......", "......"]),
    0x232B: art([  # ERASE TO THE LEFT -- the backspace key in footer hints
        "......", "......", "......", "..####", ".#...#", ".##.##",
        "#..#.#", ".##.##", ".#...#", "..####", "......", "......"]),
    0x21E7: art([  # UPWARDS WHITE ARROW -- the shift key in footer hints
        "......", "......", "..##..", ".#..#.", "#....#", "##..##",
        ".#..#.", ".#..#.", ".#..#.", ".####.", "......", "......"]),
    0x2699: art([  # GEAR -- parameter/config
        "......", "......", "..##..", ".####.", "######", "##..##",
        "##..##", "######", ".####.", "..##..", "......", "......"]),
    0x21BB: art([  # CLOCKWISE OPEN CIRCLE ARROW -- re-read/refresh
        "......", "....#.", ".#####", "#...#.", "#.....", "#.....",
        "#.....", "#....#", ".####.", "......", "......", "......"]),
    0x25F7: art([  # WHITE CIRCLE UPPER RIGHT QUADRANT -- the clock face (sync/time)
        "......", "......", ".####.", "#..#.#", "#..#.#", "#..###",
        "#....#", "#....#", ".####.", "......", "......", "......"]),
    0x2316: art([  # POSITION INDICATOR -- the crosshair (trace/map position)
        "......", "..##..", "..##..", "......", "......", "#.##.#",
        "#.##.#", "......", "......", "..##..", "..##..", "......"]),
    0x26BF: art([  # SQUARED KEY (drawn as a padlock) -- private channel
        "......", "......", ".####.", ".#..#.", "######", "######",
        "##..##", "##..##", "######", "######", "......", "......"]),
    # The two powerline chevrons of the path chips (meshterm/ui/pathline.py). They fill
    # the full cell, so that the point of one chip touches the fill of the next.
    0xE0B0: art([  # POWERLINE SOLID RIGHT TRIANGLE: the seam between two chips
        "#.....", "##....", "###...", "####..", "#####.", "######",
        "######", "#####.", "####..", "###...", "##....", "#....."]),
    0xE0B1: art([  # POWERLINE THIN RIGHT CHEVRON: the seam between two chips of one colour
        "#.....", ".#....", "..#...", "...#..", "....#.", ".....#",
        ".....#", "....#.", "...#..", "..#...", ".#....", "#....."]),
}

# Donor codepoints whose glyph slots we may repurpose (glyphs MeshTerm never draws).
# Order matters: MARKS consume donors front to back, one each. See the header comment
# for the shared-slot trap; the tail donors double as MAP TARGETS in meshterm/ui/theme
# (advert / message / data / packet / control marks) and are last-resort spares only.
# NOTE 266C: in the Terminus base it shares 266B's slot (a freebie, never a donor of its
# own -- listing it would abort); it is NOT in this list for that reason.
DONORS = [0x263A, 0x263B, 0x2665, 0x2666, 0x2663, 0x2660, 0x25D8, 0x25D9, 0x266A,
          0x266B, 0x203C, 0x2640, 0x2642, 0x2320, 0x2321, 0x00F7, 0x2552, 0x2558,
          0x2559, 0x255B, 0x255E, 0x255F, 0x2561, 0x2567, 0x2568, 0x256A,
          0x263C, 0x00B6, 0x00A7, 0x25AC, 0x21A8]

# Rounded panel corners aliased onto the existing square corners (no new bitmap), and the
# midline ellipsis onto the baseline mark.
ALIASES = {
    0x256D: 0x250C,  # rounded top-left     -> square top-left
    0x256E: 0x2510,  # rounded top-right    -> square top-right
    0x256F: 0x2518,  # rounded bottom-right -> square bottom-right
    0x2570: 0x2514,  # rounded bottom-left  -> square bottom-left
    0x22EF: 0x2026,  # midline ellipsis     -> ellipsis mark
}

# Codepoints that must survive both builds: the theme's base-font map targets.
KEEP_COMMON = [0x263C, 0x00B6, 0x00A7, 0x25AC, 0x21A8]


def build(glyphs, entries, charsize, height, marks, bands, keep_extra, out_path):
    """Append braille, draw marks into donors, alias, verify, and write one PSF2."""
    glyphs = [bytearray(g) for g in glyphs]
    entries = list(entries)
    glyphs += [bytearray(braille_glyph(v, bands, height)) for v in range(256)]
    entries += [chr(0x2800 + i).encode("utf-8") for i in range(256)]

    def slot_of(cp):
        needle = chr(cp).encode("utf-8")
        for i, e in enumerate(entries):
            if needle in e:
                return i
        return None

    donors = list(DONORS)
    for cp, bitmap in marks.items():
        if not donors:
            raise SystemExit("out of donor slots for U+%04X" % cp)
        donor = donors.pop(0)
        slot = slot_of(donor)
        if slot is None:
            raise SystemExit(
                "donor U+%04X (for mark U+%04X) not found in the base font -- "
                "fix DONORS instead of skipping" % (donor, cp))
        padded = bytearray(bitmap)
        padded += bytes(charsize - len(padded))
        glyphs[slot] = padded
        entries[slot] = chr(cp).encode("utf-8")

    for new_cp, existing_cp in ALIASES.items():
        slot = slot_of(existing_cp)
        if slot is None:
            raise SystemExit("no glyph for U+%04X to alias U+%04X onto" % (existing_cp, new_cp))
        entries[slot] += chr(new_cp).encode("utf-8")

    for cp in list(marks) + list(ALIASES) + KEEP_COMMON + keep_extra:
        if slot_of(cp) is None:
            raise SystemExit("build ate U+%04X -- a donor slot carried it; fix DONORS" % cp)

    header = struct.pack("<IIIIIIII", PSF2_MAGIC, 0, 32, 1, 512, charsize, height, 6)
    out = header + b"".join(bytes(g) for g in glyphs) + b"".join(e + b"\xff" for e in entries)
    gzip.open(out_path, "wb").write(out)
    print("  wrote %s: 512 glyphs (6x%d), +%d marks, +%d aliases"
          % (out_path, height, len(marks), len(ALIASES)))


# --- shared generator (END) -----------------------------------------------------------

BASE = os.environ["BASE"]
OUT = os.environ["OUT"]

# --- 6x12: the Terminus base -----------------------------------------------------------
base = gzip.open(BASE, "rb").read()
magic, ver, hsize, flags, length, charsize, h, w = struct.unpack("<IIIIIIII", base[:32])
assert magic == PSF2_MAGIC and (length, charsize, h, w) == (256, 12, 12, 6), \
    "base is not the expected Terminus 6x12 PSF2 (%r)" % ((length, charsize, h, w),)
glyphs12 = [base[32 + i * charsize:32 + (i + 1) * charsize] for i in range(256)]
entries12 = base[32 + 256 * charsize:].split(b"\xff")[:256]
# Cyrillic pe + pi: the shared-slot canaries (donating pi once erased pe).
build(glyphs12, entries12, 12, 12, MARKS, BANDS12, [0x043F, 0x03C0], OUT)

PYEOF

# --- the 6x8 companion, from its own (GPL-2.0) file -------------------------------------
# It is a separate file because its base bitmap is the font_6x8 of the kernel, and thus
# the PSF that it produces is GPL-2.0. Refer to scripts/picocalc-lyra/calculinux-console-font-6x8.sh and scripts/picocalc-lyra/LICENSE.GPL-2.0.
# It uses the shared generator block above and does not have a second copy of the
# donor, alias, and keeper tables. Thus a run here still produces both fonts, as it
# always did. A failure there is not a failure here, because the handheld boots in the
# 6x12 default font.
if [ "${SKIP_6X8:-0}" = 1 ]; then
    echo "skipping $OUT8 (SKIP_6X8=1)"
elif [ -f "$BUILD8" ]; then
    sh "$BUILD8" || echo "warning: the 6x8 build failed; $OUT is installed either way" >&2
else
    echo "note: $BUILD8 not found -- only $OUT was built (copy the whole scripts/picocalc-lyra/ dir)" >&2
fi

# --- apply live: setfont renders the whole console again immediately ---------------------
TTY=/dev/tty1
[ -c "$TTY" ] || TTY=/dev/tty0
echo "applying $OUT (6x12, the default) to $TTY ..."
setfont -C "$TTY" "$OUT"

# --- persist: systemd-vconsole-setup reads FONT= from vconsole.conf at each boot --------
if [ -f "$VCONSOLE" ] && grep -q '^FONT=' "$VCONSOLE"; then
    sed -i "s/^FONT=.*/FONT=$FONT_NAME/" "$VCONSOLE"
else
    echo "FONT=$FONT_NAME" >> "$VCONSOLE"
fi
echo "persisted FONT=$FONT_NAME in $VCONSOLE (loads on every boot)"
if [ -f "$OUT8" ]; then
    echo "taller: 'setfont $OUT8' for 53x40, 'setfont $OUT' for 53x26 -- or let MeshTerm do"
    echo "it (Preferences -> Display -> Console font); persist a permanent choice in $VCONSOLE"
fi

# --- palette -----------------------------------------------------------------------------
# By design, the console keeps its STANDARD kernel palette (black background, stock hues).
# meshterm/ui/theme.MESH_THEME_16 is designed for that palette (theme._VT_SLOTS). The
# custom tailwind-family remap that we shipped first is ARCHIVED. A user who wants it back
# needs only one environment variable:
#
#     MESHTERM_CUSTOM_PALETTE=1 sh calculinux-console-font-6x12.sh
#
# The values of the opt-in block are exactly the same as theme.vtrgb_lines() and
# theme._VT_SLOTS_CUSTOM (a test makes sure that they stay the same). A DEFAULT run removes
# each remap that was installed before, and sets the live palette to stock.
VTRGB=/etc/vtrgb
APPLIER=/usr/local/sbin/meshterm-vtrgb
UNIT=/etc/systemd/system/meshterm-vtrgb.service

if [ "${MESHTERM_CUSTOM_PALETTE:-0}" = 1 ]; then
    echo "writing $VTRGB (custom palette opt-in) ..."
    cat > "$VTRGB" <<'EOF'
15,239,34,245,99,51,100,203,148,248,74,251,129,165,94,255
23,68,197,158,102,65,116,213,163,113,222,191,140,180,234,255
42,68,94,11,241,85,139,225,184,113,128,36,248,252,212,255
EOF

    mkdir -p "$(dirname "$APPLIER")"
    cat > "$APPLIER" <<'EOF'
#!/bin/sh
# Apply the MeshTerm custom console palette (meshterm/ui/theme._VT_SLOTS_CUSTOM).
if command -v setvtrgb >/dev/null 2>&1; then
    exec setvtrgb /etc/vtrgb
fi
# No kbd setvtrgb: the kernel VT accepts its own OSC palette sequences (ESC ] P n rrggbb).
TTY=${1:-/dev/tty1}
printf '\033]P00f172a\033]P1ef4444\033]P222c55e\033]P3f59e0b\033]P46366f1\033]P5334155\033]P664748b\033]P7cbd5e1\033]P894a3b8\033]P9f87171\033]Pa4ade80\033]Pbfbbf24\033]Pc818cf8\033]Pda5b4fc\033]Pe5eead4\033]Pfffffff' > "$TTY"
EOF
    chmod +x "$APPLIER"

    cat > "$UNIT" <<EOF
[Unit]
Description=MeshTerm console palette (custom 16-slot vtrgb)
After=systemd-vconsole-setup.service

[Service]
Type=oneshot
ExecStart=$APPLIER
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable meshterm-vtrgb.service >/dev/null 2>&1 || true
    "$APPLIER" || echo "warning: could not apply the palette live (boot service will)"
    echo "custom palette installed ($VTRGB + boot oneshot)"
else
    systemctl disable meshterm-vtrgb.service >/dev/null 2>&1 || true
    rm -f "$UNIT" "$VTRGB" "$APPLIER"
    systemctl daemon-reload 2>/dev/null || true
    # Restore the STANDARD palette explicitly. A previous setvtrgb overwrote the default
    # colormap of the kernel. Thus the OSC reset (ESC ] R) alone "resets" the palette to
    # the custom values. Program the stock PC palette, then reset.
    TTY=/dev/tty1
    [ -c "$TTY" ] || TTY=/dev/tty0
    if command -v setvtrgb >/dev/null 2>&1; then
        STOCK=$(mktemp)
        cat > "$STOCK" <<'EOF'
0,170,0,170,0,170,0,170,85,255,85,255,85,255,85,255
0,0,170,85,0,0,170,170,85,85,255,255,85,85,255,255
0,0,0,0,170,170,170,170,85,85,85,85,255,255,255,255
EOF
        setvtrgb "$STOCK" || true
        rm -f "$STOCK"
    fi
    printf '\033]P0000000\033]P1aa0000\033]P200aa00\033]P3aa5500\033]P40000aa\033]P5aa00aa\033]P600aaaa\033]P7aaaaaa\033]P8555555\033]P9ff5555\033]Pa55ff55\033]Pbffff55\033]Pc5555ff\033]Pdff55ff\033]Pe55ffff\033]Pfffffff\033]R' > "$TTY" 2>/dev/null || true
    echo "palette: standard (custom remap removed; opt back in with MESHTERM_CUSTOM_PALETTE=1)"
fi

echo "done -- launch 'meshterm' to see braille charts, node glyphs, framed panels, and the > cursor."
