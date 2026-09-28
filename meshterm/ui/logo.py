# SPDX-License-Identifier: Apache-2.0
"""The MeshTerm wordmark for the startup splash, loaded from the assets folder.

The art itself lives in ``meshterm/assets/splash`` as pre-coloured ``.ans`` files — the
classic ANSI-art extension — one per canvas width, each named for the width it was drawn
at: ``logo.71.ans``, ``logo.53.ans``. Each mark is drawn by hand in an art editor and needs
no source but itself: there is nothing here that generates a wordmark, and nothing to
re-run after editing one. Files are read fresh on every call, so the mark can be re-styled
by editing the art alone: no code change and no restart of the design loop — and a new size
is a file dropped in the folder, since the ladder is read from the names (:func:`_variants`)
rather than listed here.

Three things separate a real ``.ans`` from a text file with colour in it, and :func:`_rows`
handles them all so the art stays authorable in the tools that drew it:

* **Codepage 437.** The blocks and box-drawing (``█▓▒░`` and ``╔═╗``) are single bytes in
  DOS's codepage, not UTF-8. We try UTF-8 first and fall back, so a mark saved as UTF-8 and
  one exported from an art editor both read.
* **Auto-wrap.** An art editor omits the line break on a row that fills the canvas and
  lets the terminal wrap it, so the file's newlines are *not* the picture's rows. The
  canvas width comes from the SAUCE record on the end of the file — which has to be
  trimmed off in the same breath, being metadata rather than art.
* **Blank cells that aren't spaces.** An editor writes an untouched cell as a NUL, and a
  DOS console dutifully leaves it blank. Rich measures it as nothing at all, so every one
  swallowed a column and slid the rest of its row leftwards.

A fourth trap belongs to the *art* rather than the loader: codepage 437's first 32 bytes
are pictures on a DOS console (``►◄‼``) and control characters everywhere else, so a mark
that draws with them measures short here and, at ``0x13``, sends XOFF to a real console.
There is nothing to decode there — the byte has to become a glyph in the file — so it is
held by a test instead (``test_theme16``), alongside the one that keeps the narrow mark
inside the console font.

There is more than one mark, at different widths, and the *screen* picks — not the
platform. A 53-column PicoCalc console and a desktop terminal dragged narrow have the same
problem, so :func:`load_logo` answers the only question that matters: which is the widest
mark that fits the columns I have?
"""

from __future__ import annotations

import re
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text

#: Where the art lives — a sibling of the code, not mixed into it, and inside the package
#: so it ships with an installed wheel.
_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "splash"

#: ``logo.<width>.ans`` — a mark's file is named for the canvas it was drawn on, so the
#: folder listing *is* the size ladder and :func:`_variants` needs to read nothing else.
#: (The old 8.3 spelling is gone with it: the editors that draw this stuff manage a second
#: dot, and a name that states its width is worth more than a name DOS could have opened.)
_NAMED_WIDTH = re.compile(r"logo\.(\d+)\.ans$")

#: A colour change and nothing else. Every escape these marks use is an SGR, so re-breaking
#: a row only ever has to carry a *colour* across the seam — never a cursor move.
_SGR = re.compile(r"\x1b\[[0-9;]*m")

#: A foreground on the dim bank, said without a word about intensity. Bold *is* brightness
#: on the PicoCalc console, so a bare one inherits whatever the span before it left set and
#: lands a bank too high — the theme's own styles state their intent for the same reason.
_DIM_FG = re.compile(r"3[0-7]")

#: The foreground a DOS console stands on after a reset: grey, slot 7 — which bold lifts
#: to white.
_DEFAULT_FG = 37

#: Parameters that settle the intensity question themselves, so a span carrying one needs
#: no help: a reset, bold, faint, or an explicit return to normal.
_SAYS_INTENSITY = frozenset({"", "0", "1", "2", "22"})

#: SAUCE rides on the end of an art file behind DOS's end-of-file mark: 128 bytes naming
#: the author, the canvas and the font. None of it is meant to reach the screen.
_EOF_MARK = b"\x1a"
_SAUCE_LEN = 128

#: Cells a DOS console draws blank but a modern renderer would swallow or mis-measure: NUL
#: is how an art editor spells "nothing here", and codepage 437's 0xff is a hard space.
#: Both become a plain space, so the column they hold survives into the frame.
_BLANK_CELLS = {0x00: " ", 0xA0: " "}


def _variants() -> list[str]:
    """Every mark in the folder, widest first.

    Adding a size is dropping the file in — nothing here lists them. The width in the name
    only *orders* the ladder; which mark fits is still settled by measuring the art itself
    (see :func:`load_logo`), so a mark whose name overstates it is caught rather than
    trusted. A file that doesn't spell a width is not a mark and is passed over.
    """
    named = []
    for path in _ASSETS.glob("logo.*.ans"):
        found = _NAMED_WIDTH.search(path.name)
        if found:
            named.append((int(found.group(1)), path.name))
    return [name for _, name in sorted(named, reverse=True)]


def _split_sauce(raw: bytes) -> tuple[bytes, int | None]:
    """The art alone, and the canvas width SAUCE claims for it (``None`` when unsigned).

    A file with no record is returned whole and unmeasured: its newlines are its rows, and
    re-breaking one would be inventing a canvas the author never declared.
    """
    cut = raw.rfind(_EOF_MARK)
    if cut < 0:
        return raw, None
    trailer = raw[cut + 1 :]
    if not trailer.startswith(b"SAUCE") or len(trailer) < _SAUCE_LEN:
        return raw, None
    width = int.from_bytes(trailer[96:98], "little")  # TInfo1 — characters per row
    return raw[:cut], width or None


def _decode(raw: bytes) -> str:
    """An art file's text, in whichever of the two codepages it was written."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp437")


def _rewrap(line: str, width: int) -> list[str]:
    """Re-break one auto-wrapping line into rows of ``width`` printable cells.

    The rows are drawn independently — each becomes its own ``Text.from_ansi`` — so a
    colour set before the seam has to be restated after it. A real terminal needs no such
    help: it never stopped reading the one stream, so the state simply persisted.
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
                state = ""  # a plain reset leaves nothing to restate
            elif params.split(";")[0] in ("", "0"):
                state = seq  # reset-and-set: this sequence *is* the whole state
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
    if cells or not rows:  # the short last row, or a blank line that is still a row
        rows.append("".join(buf))
    return rows


def _state_intensity(row: str) -> str:
    """Rewrite the row's colour changes so brightness is stated as a colour, not as bold.

    An art editor spells brightness the DOS way — ``1m`` lifts the foreground into the
    bright bank, and each span after it inherits that. Two things then have to be settled
    before the row can be drawn anywhere else.

    The first is that the inheritance does not survive: a row is handed to Rich on its own,
    so a span that never mentions intensity is read against whatever the *previous row* left
    set. That is resolved by working the state out here and saying it outright.

    The second is that **bold is not brightness everywhere**. It is on the PicoCalc console
    and on a DOS one; it is not on macOS Terminal, where bold asks for a heavier face and
    leaves the colour alone. There ``1;30`` is not dark grey but plain black — and this art
    leans on it, seventy-nine spans of the wide mark being bold-black, much of that on a
    black ground. The mark came out muted, and the ``░▒▓`` dithers that fade one colour into
    another faded toward the wrong end. So brightness is emitted as the colour it means:
    ``9N`` (the aixterm bright bank), which needs no bold and is also how the PicoCalc's own
    console spells bright. Nothing about *which* colour is being asked for changes; only the
    spelling, from one a terminal may read as a font weight to one that can only be a colour.
    """
    out: list[str] = []
    bright = False
    standing: str | None = None  # the dim-bank foreground currently in force
    at = 0
    while at < len(row):
        found = _SGR.match(row, at)
        if not found:
            out.append(row[at])
            at += 1
            continue
        params = found.group()[2:-1]
        parts = params.split(";") if params else [""]
        # What this sequence leaves the intensity set to, for the spans that follow it.
        says = [p for p in parts if p in ("", "0", "1", "22")]
        for part in says:
            bright = part == "1"
            if part in ("", "0"):
                standing = None  # a reset takes the foreground with it
        # Whether *this* sequence's own foreground is bright: what it says, else what stands.
        here = bright
        fg = next((p for p in parts if _DIM_FG.fullmatch(p)), None)
        if fg is None:
            # A bare "1" has nothing left to say once brightness travels as a colour, and
            # dropping it keeps bold off glyphs a terminal may redraw at another weight.
            kept = [p for p in parts if p != "1"]
            # But the brightness it was announcing applies to the colour already in force,
            # not only to the next one named. `ESC[1m` is how the art says "everything after
            # this is bright", and with the "1" gone that run would carry on in the dim bank
            # -- two spans meant to read dark then light both coming out dark. So the
            # standing foreground is restated in whichever bank now applies.
            if standing is not None and says:
                if here:
                    kept.append(str(int(standing) + 60))
                else:
                    kept.append(standing)
                    if "22" not in kept:
                        kept.insert(0, "22")
            elif says and here:
                # No colour named since a reset: the one in force is the console's default,
                # grey (7), and brightening it gives white. ``ESC[0m`` then ``ESC[1m`` is how
                # the art says "white", and dropping the "1" without restating anything left
                # those spans in the terminal's own default grey — a white run drawn grey.
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
                rewritten.insert(0, "22")  # say dim outright; never inherit a bank
            out.append("\x1b[" + ";".join(rewritten) + "m")
        at = found.end()
    return "".join(out)


def _rows(name: str) -> list[str]:
    """The named mark's rows, or ``[]`` when it can't be read."""
    try:
        raw = (_ASSETS / name).read_bytes()
    except OSError:
        return []
    art, width = _split_sauce(raw)
    text = _decode(art).translate(_BLANK_CELLS)
    lines = [line.rstrip("\r") for line in text.split("\n")]
    if lines and lines[-1] == "":  # drop the trailing newline's empty row
        lines.pop()
    if width:
        lines = [row for line in lines for row in _rewrap(line, width)]
    return [_state_intensity(row) for row in lines]


def logo_width(rows: list[str]) -> int:
    """The display width of a mark's widest row, escape sequences discounted."""
    return max((cell_len(Text.from_ansi(row).plain) for row in rows), default=0)


def load_logo(max_cols: int | None = None) -> list[str]:
    """Return the widest wordmark that fits ``max_cols``, as pre-coloured ANSI rows.

    Args:
        max_cols: The columns available to draw in. ``None`` asks for the full-size mark
            without fitting — for a caller that will fit it later (see
            :func:`~meshterm.ui.tui.frame.compose_startup`, which knows the real width
            only at compose time).

    Returns:
        The mark's rows, or ``[]`` when none fits (or none could be read) — the splash
        draws no banner at all rather than a torn one, and a missing file degrades the
        same way rather than raising.
    """
    for name in _variants():
        rows = _rows(name)
        if rows and (max_cols is None or logo_width(rows) <= max_cols):
            return rows
    return []
