# SPDX-License-Identifier: Apache-2.0
"""The 6×12 bitmap font that the emulator draws cells in.

The base is Terminus at 12 pixels. It is also the base of the PicoCalc's font, so the two
handhelds look the same. It has 1,356 glyphs. These cover all that MeshTerm draws on this
platform, except a small number of its own marks. The emulator is not a kernel console, so
no limit of 512 glyphs applies: no donor slots, and no braille folded away.

MeshTerm's marks are drawn over the base from the same pixel art that the PicoCalc's font
build uses (:data:`MARKS`). Thus ``★``, ``❯``, ``⚿``, and the other marks look the same on
both handhelds. The braille is also the same (:data:`BRAILLE`): solid tiles, where
Terminus draws the dots apart.

Terminus has the SIL Open Font License. Thus it is never shipped in MeshTerm: the
emulator reads it from a BDF file on the machine (:func:`find_font`). On the desktop,
``scripts/cardputer-zero/fetch-terminus.py`` puts the two 12-pixel BDF files, with their
licence, where :func:`find_font` looks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..core.config import default_config_dir

#: The cell, in pixels.
CELL_W = 6
CELL_H = 12

#: The file names that :func:`find_font` looks for: regular, then bold.
REGULAR_BDF = "ter-u12n.bdf"
BOLD_BDF = "ter-u12b.bdf"

#: The environment variable that names another directory for the fonts (refer to
#: :func:`font_dir`).
FONT_ENV = "MESHTERM_EMULATOR_FONTS"


def _art(rows: list[str]) -> bytes:
    """Pixel art as one byte for each row.

    A ``#`` is a pixel that is on. The leftmost pixel is the high bit.
    """
    return bytes(sum(0x80 >> x for x, ch in enumerate(row) if ch == "#") for row in rows)


#: MeshTerm's own marks, drawn over the base font. The pixel art is the same as in the
#: PicoCalc's font build (``scripts/picocalc-lyra/calculinux-console-font-6x12.sh``,
#: ``MARKS``). Thus a mark has the same picture on each handheld.
# fmt: off
MARKS: dict[int, bytes] = {
    0x25CF: _art([  # BLACK CIRCLE: node / unread
        "......", "......", "..##..", ".####.", "######", "######",
        "######", "######", ".####.", "..##..", "......", "......"]),
    0x25C9: _art([  # FISHEYE: sensor
        "......", "......", "..##..", ".####.", "##..##", "#.##.#",
        "#.##.#", "##..##", ".####.", "..##..", "......", "......"]),
    0x2605: _art([  # BLACK STAR: you / best
        "......", "..##..", "..##..", "######", ".####.", "..##..",
        ".####.", "##..##", "#....#", "......", "......", "......"]),
    0x2014: _art([  # EM DASH: title separator, full width
        "......", "......", "......", "......", "......", "######",
        "######", "......", "......", "......", "......", "......"]),
    0x2713: _art([  # CHECK MARK: ok
        "......", "......", "......", ".....#", "....#.", "...#..",
        "#.#...", ".#....", "......", "......", "......", "......"]),
    0x2717: _art([  # BALLOT X: error
        "......", "......", "......", "#....#", ".#..#.", "..##..",
        "..##..", ".#..#.", "#....#", "......", "......", "......"]),
    0x25B6: _art([  # RIGHT-POINTING TRIANGLE: run / play
        "......", "#.....", "##....", "###...", "####..", "#####.",
        "#####.", "####..", "###...", "##....", "#.....", "......"]),
    0x276F: _art([  # HEAVY RIGHT ANGLE QUOTE: the mark of the highlight in a list
        "......", "##....", ".##...", "..##..", "...##.", "....##",
        "....##", "...##.", "..##..", ".##...", "##....", "......"]),
    0x25B8: _art([  # SMALL RIGHT-POINTING TRIANGLE: the mark of a grabbed row (reorder)
        "......", "......", "......", ".#....", ".##...", ".###..",
        ".###..", ".##...", ".#....", "......", "......", "......"]),
    0x2026: _art([  # HORIZONTAL ELLIPSIS: "opens further prompts", truncation
        "......", "......", "......", "......", "......", "......",
        "......", "......", "#.#.#.", "#.#.#.", "......", "......"]),
    0x26A0: _art([  # WARNING SIGN: the warn status mark
        "......", "..##..", ".#..#.", ".#..#.", "#....#", "#.##.#",
        "#.##.#", "#....#", "#.##.#", "######", "......", "......"]),
    0x232B: _art([  # ERASE TO THE LEFT: the Backspace key in footer hints
        "......", "......", "......", "..####", ".#...#", ".##.##",
        "#..#.#", ".##.##", ".#...#", "..####", "......", "......"]),
    0x21E7: _art([  # UPWARDS WHITE ARROW: the Shift key in footer hints
        "......", "......", "..##..", ".#..#.", "#....#", "##..##",
        ".#..#.", ".#..#.", ".#..#.", ".####.", "......", "......"]),
    0x2699: _art([  # GEAR: parameter/config
        "......", "......", "..##..", ".####.", "######", "##..##",
        "##..##", "######", ".####.", "..##..", "......", "......"]),
    0x21BB: _art([  # CLOCKWISE OPEN CIRCLE ARROW: re-read/refresh
        "......", "....#.", ".#####", "#...#.", "#.....", "#.....",
        "#.....", "#....#", ".####.", "......", "......", "......"]),
    0x25F7: _art([  # WHITE CIRCLE UPPER RIGHT QUADRANT: the clock face (sync/time)
        "......", "......", ".####.", "#..#.#", "#..#.#", "#..###",
        "#....#", "#....#", ".####.", "......", "......", "......"]),
    0x2316: _art([  # POSITION INDICATOR: the crosshair (trace/map position)
        "......", "..##..", "..##..", "......", "......", "#.##.#",
        "#.##.#", "......", "......", "..##..", "..##..", "......"]),
    0x26BF: _art([  # SQUARED KEY (drawn as a padlock): private channel
        "......", "......", ".####.", ".#..#.", "######", "######",
        "##..##", "##..##", "######", "######", "......", "......"]),
}
# fmt: on

#: For each braille dot: the byte bits that turn on its pixel columns, and its pixel rows.
#: There are two columns of three pixels and four rows of three pixels. Together, they
#: cover the whole cell.
_BRAILLE_COLS = (0xE0, 0x1C)
_BRAILLE_ROWS = ((0, 1, 2), (3, 4, 5), (6, 7, 8), (9, 10, 11))

#: The dot bit for each (column, row) in a braille cell: the standard layout of Unicode.
_BRAILLE_DOTS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))


def _braille(value: int) -> bytes:
    """The braille cell whose low byte is ``value``. Each dot is a solid 3×3 tile."""
    rows = [0] * CELL_H
    for col, bits in enumerate(_BRAILLE_DOTS):
        for row, bit in enumerate(bits):
            if value & bit:
                for y in _BRAILLE_ROWS[row]:
                    rows[y] |= _BRAILLE_COLS[col]
    return bytes(rows)


#: All the braille cells, drawn over the base font as solid tiles. There is no gap between
#: the dots, or between cells that are next to each other. The tiles are the same as in the
#: PicoCalc's font build (``scripts/picocalc-lyra/calculinux-console-font-6x12.sh``,
#: ``BANDS12``). MeshTerm never uses braille as text to read. Each braille cell is pixels:
#: a chart, the map, a QR code. A braille cell with separate dots breaks each line that it
#: draws into a row of separate dots.
BRAILLE: dict[int, bytes] = {0x2800 + value: _braille(value) for value in range(256)}

#: Codepoints that are drawn with the glyph of a different codepoint, as the PicoCalc's
#: font aliases them.
ALIASES: dict[int, int] = {
    0x22EF: 0x2026,  # midline ellipsis -> the ellipsis mark
}

#: The glyph for a character that the font has no glyph for: a hollow box. Thus a gap in
#: the font is visible, and not a blank cell that gives no warning.
MISSING = _art(
    ["......", "#####.", "#...#.", "#...#.", "#...#.", "#...#."]
    + ["#...#.", "#...#.", "#...#.", "#####.", "......", "......"]
)


def load_bdf(path: Path) -> dict[int, bytes]:
    """The glyphs of a BDF font, sized to the 6×12 cell, by codepoint.

    Each glyph is one byte for each pixel row, and the leftmost pixel is the high bit. The
    bounding box of the glyph and the ascent of the font set its position in the cell.
    Thus a glyph that is narrower or shorter than the cell is at the position that the
    designer of the font gave it.

    Raises:
        OSError: The file cannot be read.
        ValueError: The file is not a BDF font.
    """
    glyphs: dict[int, bytes] = {}
    ascent = CELL_H - 2
    encoding = -1
    box = (CELL_W, CELL_H, 0, -2)
    bitmap: list[int] | None = None
    with path.open(encoding="latin-1") as handle:
        first = handle.readline()
        if not first.startswith("STARTFONT"):
            raise ValueError(f"{path} is not a BDF font")
        for raw in handle:
            words = raw.split()
            if not words:
                continue
            key = words[0]
            if bitmap is not None:
                if key == "ENDCHAR":
                    glyphs[encoding] = _place(bitmap, box, ascent)
                    bitmap = None
                else:
                    bitmap.append(int(key, 16))
            elif key == "FONT_ASCENT":
                ascent = int(words[1])
            elif key == "ENCODING":
                encoding = int(words[1])
            elif key == "BBX":
                box = (int(words[1]), int(words[2]), int(words[3]), int(words[4]))
            elif key == "BITMAP":
                bitmap = []
    glyphs.pop(-1, None)
    return glyphs


def _place(bitmap: list[int], box: tuple[int, int, int, int], ascent: int) -> bytes:
    """The BDF rows of one glyph, set into the 6×12 cell."""
    width, height, x_off, y_off = box
    row_bits = max(8, (width + 7) // 8 * 8)
    top = ascent - (y_off + height)
    rows = [0] * CELL_H
    for index, bits in enumerate(bitmap[:height]):
        y = top + index
        if 0 <= y < CELL_H:
            # Align the row to the left of its byte. Then shift it right by the x offset of
            # the glyph.
            value = bits >> (row_bits - 8) if row_bits > 8 else bits
            rows[y] = (value >> max(0, x_off)) & 0xFF
    return bytes(rows)


@dataclass(frozen=True, slots=True)
class Font:
    """A regular face and a bold face. Each face is a codepoint → 12-byte glyph table."""

    regular: dict[int, bytes]
    bold: dict[int, bytes]

    def glyph(self, char: str, *, bold: bool = False) -> bytes:
        """The glyph for ``char`` (its first codepoint), or :data:`MISSING`."""
        if not char:
            return bytes(CELL_H)
        code = ord(char[0])
        code = ALIASES.get(code, code)
        table = self.bold if bold else self.regular
        found = table.get(code)
        if found is None and bold:
            found = self.regular.get(code)
        return MISSING if found is None else found

    def has(self, code: int) -> bool:
        """Whether the font draws codepoint ``code`` with a glyph of its own."""
        return ALIASES.get(code, code) in self.regular


def build_font(regular: dict[int, bytes], bold: dict[int, bytes] | None = None) -> Font:
    """A :class:`Font` from base glyph tables, with MeshTerm's marks and braille over both.

    If the bold face is missing, the font uses the regular face, made bold by a one-pixel
    smear. The braille is not smeared. Braille is pixels, not letters, so bold has no
    weight to add to it. On braille, the smear only closes the seam between the two
    columns of a cell.
    """
    regular = {**regular, **MARKS, **BRAILLE}
    if bold is None:
        bold = {code: bytes(row | row >> 1 for row in rows) for code, rows in regular.items()}
        bold.update(BRAILLE)
    else:
        bold = {**bold, **MARKS, **BRAILLE}
    return Font(regular=regular, bold=bold)


def font_dir() -> Path:
    """Where :func:`find_font` looks: ``$MESHTERM_EMULATOR_FONTS``, else ``fonts`` in config."""
    override = os.environ.get(FONT_ENV, "").strip()
    return Path(override) if override else default_config_dir() / "fonts"


def find_font() -> Font:
    """Read the font of the emulator from :func:`font_dir`.

    Raises:
        FileNotFoundError: There is no regular face in that directory. The error tells how
            to download one.
    """
    directory = font_dir()
    regular_path = directory / REGULAR_BDF
    if not regular_path.is_file():
        raise FileNotFoundError(
            f"no {REGULAR_BDF} in {directory} - fetch the Terminus font once with "
            "`meshterm emulate --fetch-fonts` (or set "
            f"{FONT_ENV} to a directory holding it)"
        )
    bold_path = directory / BOLD_BDF
    bold = load_bdf(bold_path) if bold_path.is_file() else None
    return build_font(load_bdf(regular_path), bold)
