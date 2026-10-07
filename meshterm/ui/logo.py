# SPDX-License-Identifier: Apache-2.0
"""The MeshTerm wordmark for the startup splash, loaded from the assets folder.

The art is in ``meshterm/assets/splash`` as pre-coloured ``.ans`` files (the classic
extension for ANSI art). There is one file for each canvas width, and each file has the
name of the width at which it was drawn: ``logo.71.ans``, ``logo.53.ans``. A person draws
each mark by hand in an art editor, and the mark needs no source but itself. No code here
generates a wordmark, and nothing must run again after a mark is edited. MeshTerm reads the
files again on each call. Thus the art alone can change the style of the mark, with no code
change and no restart of the design loop. To add a new size, put a file in the folder.
The code reads the ladder of sizes from the names (:func:`_variants`) and does not list
them here.

Three things make a real ``.ans`` different from a text file that has colour in it.
:func:`_rows` handles all three, so that the tools that drew the art can still edit it:

* **Codepage 437.** The blocks and box-drawing characters (``█▓▒░`` and ``╔═╗``) are
  single bytes in the codepage of DOS, not UTF-8. We try UTF-8 first and use the codepage
  if that fails. Thus both a mark that is saved as UTF-8 and a mark that is exported from
  an art editor can be read.
* **Auto-wrap.** An art editor omits the line break on a row that fills the canvas, and
  it lets the terminal wrap the row. Thus the newlines of the file are *not* the rows of
  the picture. The canvas width comes from the SAUCE record at the end of the file. The
  code must also trim this record off at the same time, because it is metadata and not
  art.
* **Blank cells that are not spaces.** An editor writes a cell that nobody touched as a
  NUL, and a DOS console leaves it blank. Rich measures it as no width at all. Thus each
  NUL removed one column and moved the rest of its row to the left.

A fourth trap belongs to the *art* and not to the loader. The first 32 bytes of codepage
437 are pictures on a DOS console (``►◄‼``) and control characters everywhere else. A mark
that draws with them measures short here and, at ``0x13``, sends XOFF to a real console.
Nothing can decode these bytes, because the byte must become a glyph in the file. Thus a
test (``test_theme16``) holds this rule. The same test also keeps the narrow mark inside
the console font.

There is more than one mark, at different widths, and the *screen* chooses, not the
platform. A PicoCalc console of 53 columns and a desktop terminal that the user drags
narrow have the same problem. Thus :func:`load_logo` answers the only question that
matters: which is the widest mark that fits the columns that I have?
"""

from __future__ import annotations

import re
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text

#: The folder of the art. It is next to the code, not mixed into the code, and it is inside
#: the package so that it ships in an installed wheel.
_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "splash"

#: ``logo.<width>.ans``. The file of a mark has the name of the canvas on which it was
#: drawn. Thus the listing of the folder *is* the ladder of sizes, and :func:`_variants`
#: does not need to read anything else. (The old 8.3 name is gone. The editors that draw
#: this art can manage a second dot, and a name that states its width is more useful than
#: a name that DOS could open.)
_NAMED_WIDTH = re.compile(r"logo\.(\d+)\.ans$")

#: A colour change and nothing else. Each escape sequence that these marks use is an SGR.
#: Thus when the code breaks a row again, it must carry only a *colour* across the seam,
#: and never a cursor move.
_SGR = re.compile(r"\x1b\[[0-9;]*m")

#: A foreground on the dim bank, written with no word about intensity. Bold *is*
#: brightness on the PicoCalc console. Thus a bare foreground keeps the intensity that the
#: span before it set, and it lands one bank too high. The styles of the theme state their
#: intent for the same reason.
_DIM_FG = re.compile(r"3[0-7]")

#: The foreground of a DOS console after a reset: grey, slot 7. Bold makes it white.
_DEFAULT_FG = 37

#: Parameters that settle the question of intensity themselves. A span that has one of
#: these needs no help: a reset, bold, faint, or an explicit return to normal.
_SAYS_INTENSITY = frozenset({"", "0", "1", "2", "22"})

#: SAUCE is at the end of an art file, after the end-of-file mark of DOS. It has 128 bytes
#: that name the author, the canvas, and the font. None of it is for the screen.
_EOF_MARK = b"\x1a"
_SAUCE_LEN = 128

#: Cells that a DOS console draws blank but that a modern renderer would remove or measure
#: wrong. An art editor uses NUL to say "nothing here", and 0xff of codepage 437 is a hard
#: space. Both become a plain space, so that the column that they hold stays in the frame.
_BLANK_CELLS = {0x00: " ", 0xA0: " "}


def _variants() -> list[str]:
    """All the marks in the folder, widest first.

    To add a size, put the file in the folder. No code here lists the marks. The width in
    the name only *orders* the ladder. The measure of the art itself still decides which
    mark fits (refer to :func:`load_logo`). Thus the code finds a mark whose name states a
    width that is too large, and does not trust the name. A file whose name has no width
    is not a mark, and the code ignores it.
    """
    named = []
    for path in _ASSETS.glob("logo.*.ans"):
        found = _NAMED_WIDTH.search(path.name)
        if found:
            named.append((int(found.group(1)), path.name))
    return [name for _, name in sorted(named, reverse=True)]


def _split_sauce(raw: bytes) -> tuple[bytes, int | None]:
    """The art alone, and the canvas width that SAUCE gives for it (``None`` if no record).

    A file with no record is returned whole, with no width. Its newlines are its rows. If
    the code broke a row again, it would invent a canvas that the author never declared.
    """
    cut = raw.rfind(_EOF_MARK)
    if cut < 0:
        return raw, None
    trailer = raw[cut + 1 :]
    if not trailer.startswith(b"SAUCE") or len(trailer) < _SAUCE_LEN:
        return raw, None
    width = int.from_bytes(trailer[96:98], "little")  # TInfo1: the characters in each row
    return raw[:cut], width or None


def _decode(raw: bytes) -> str:
    """The text of an art file, in the one of the two codepages in which it was written."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp437")


def _rewrap(line: str, width: int) -> list[str]:
    """Break one line that wraps automatically into rows of ``width`` printable cells.

    The code draws the rows independently, because each row becomes its own
    ``Text.from_ansi``. Thus a colour that was set before the seam must be stated again
    after the seam. A real terminal needs no such help. It reads the one stream without a
    stop, so the state stays.
    """
    rows: list[str] = []
    buf: list[str] = []
    state = ""
    cells = 0
    at = 0
    while at < len(line):
        found = _SGR.match(line, at)
        if found:
            seq = found.group()
            buf.append(seq)
            params = seq[2:-1]
            if params in ("", "0"):
                state = ""  # a plain reset leaves nothing to state again
            elif params.split(";")[0] in ("", "0"):
                state = seq  # reset and set: this sequence *is* the whole state
            else:
                state += seq
            at = found.end()
            continue
        buf.append(line[at])
        at += 1
        cells += 1
        if cells == width:
            rows.append("".join(buf))
            buf = [state] if state else []
            cells = 0
    if cells or not rows:  # the short last row, or a blank line (it is also a row)
        rows.append("".join(buf))
    return rows


def _state_intensity(row: str) -> str:
    """Rewrite the colour changes of the row so that brightness is a colour, not bold.

    An art editor writes brightness the DOS way. ``1m`` puts the foreground in the bright
    bank, and each span after it keeps that. Two things must be settled before the code can
    draw the row in any other place.

    The first is that the kept state does not survive. The code gives each row to Rich on
    its own. Thus Rich reads a span that has no word about intensity against the state that
    the *previous row* left. The code solves this: it works out the state here and states
    it outright.

    The second is that **bold is not brightness everywhere**. It is brightness on the
    PicoCalc console and on a DOS console. It is not brightness on macOS Terminal, where
    bold asks for a heavier face and does not change the colour. There ``1;30`` is plain
    black, not dark grey. This art uses it often: seventy-nine spans of the wide mark are
    bold black, and many of them are on a black ground. The mark came out muted, and the
    ``░▒▓`` dithers that fade one colour into another faded toward the wrong end. Thus the
    code writes brightness as the colour that it means: ``9N`` (the aixterm bright bank).
    This needs no bold, and it is also the way in which the console of the PicoCalc writes
    bright. The colour that the art asks for does not change. Only the way it is written
    changes: from a form that a terminal can read as a font weight to a form that a
    terminal can read only as a colour.
    """
    out: list[str] = []
    bright = False
    standing: str | None = None  # the foreground of the dim bank that is in force now
    at = 0
    while at < len(row):
        found = _SGR.match(row, at)
        if not found:
            out.append(row[at])
            at += 1
            continue
        params = found.group()[2:-1]
        parts = params.split(";") if params else [""]
        # The intensity that this sequence sets, for the spans that follow it.
        says = [p for p in parts if p in ("", "0", "1", "22")]
        for part in says:
            bright = part == "1"
            if part in ("", "0"):
                standing = None  # a reset also removes the foreground
        # Whether the foreground of *this* sequence is bright: what it says, or else what
        # stands.
        here = bright
        fg = next((p for p in parts if _DIM_FG.fullmatch(p)), None)
        if fg is None:
            # A bare "1" has nothing more to say when brightness travels as a colour. If
            # the code removes it, bold stays off the glyphs that a terminal can redraw at
            # another weight.
            kept = [p for p in parts if p != "1"]
            # But the brightness that it announced applies to the colour that is in force
            # now, not only to the next colour that the art names. `ESC[1m` is the way in
            # which the art says "everything after this is bright". Without the "1", that
            # run would continue in the dim bank. Two spans that must read dark and then
            # light would both come out dark. Thus the code states the foreground that
            # stands again, in the bank that applies now.
            if standing is not None and says:
                if here:
                    kept.append(str(int(standing) + 60))
                else:
                    kept.append(standing)
                    if "22" not in kept:
                        kept.insert(0, "22")
            elif says and here:
                # No colour was named after the last reset. The colour in force is the default of
                # the console, grey (7), and brightness makes it white. ``ESC[0m`` then
                # ``ESC[1m`` is the way in which the art says "white". The code once
                # removed the "1" and stated nothing again. Those spans stayed in the
                # default grey of the terminal, so a white run was drawn grey.
                kept.append(str(_DEFAULT_FG + 60))
            if kept:
                out.append("\x1b[" + ";".join(kept) + "m")
        else:
            standing = fg
            rewritten = []
            for part in parts:
                if part == "1":
                    continue  # brightness is in the colour now
                if part == fg:
                    rewritten.append(str(int(fg) + 60) if here else fg)
                else:
                    rewritten.append(part)
            if not here and "22" not in rewritten:
                rewritten.insert(0, "22")  # say dim outright. Never keep a bank.
            out.append("\x1b[" + ";".join(rewritten) + "m")
        at = found.end()
    return "".join(out)


def _rows(name: str) -> list[str]:
    """The rows of the named mark, or ``[]`` if the file cannot be read."""
    try:
        raw = (_ASSETS / name).read_bytes()
    except OSError:
        return []
    art, width = _split_sauce(raw)
    text = _decode(art).translate(_BLANK_CELLS)
    lines = [line.rstrip("\r") for line in text.split("\n")]
    if lines and lines[-1] == "":  # remove the empty row after the last newline
        lines.pop()
    if width:
        lines = [row for line in lines for row in _rewrap(line, width)]
    return [_state_intensity(row) for row in lines]


def logo_width(rows: list[str]) -> int:
    """The display width of the widest row of a mark. Escape sequences have no width."""
    return max((cell_len(Text.from_ansi(row).plain) for row in rows), default=0)


def load_logo(max_cols: int | None = None) -> list[str]:
    """Return the widest wordmark that fits ``max_cols``, as pre-coloured ANSI rows.

    Args:
        max_cols: The columns in which the mark can be drawn. ``None`` asks for the
            full-size mark, with no fit. This is for a caller that will fit it later
            (refer to :func:`~meshterm.ui.tui.frame.compose_startup`, which knows the real
            width only when it composes).

    Returns:
        The rows of the mark, or ``[]`` if no mark fits (or if no file could be read). In
        this case the splash draws no banner, instead of a banner that is cut. A missing
        file has the same result, and does not raise an error.
    """
    for name in _variants():
        rows = _rows(name)
        if rows and (max_cols is None or logo_width(rows) <= max_cols):
            return rows
    return []
