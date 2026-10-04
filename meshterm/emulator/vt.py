# SPDX-License-Identifier: Apache-2.0
"""The terminal the emulator draws: a VT byte stream parsed into a grid of cells.

Small on purpose. Its one producer is MeshTerm itself — prompt_toolkit's ``Vt100_Output``
and the session's own fast row writer — so it understands what those emit and the handful
of neighbours a stray library might, and quietly ignores the rest of the VT220/xterm
universe: cursor movement and addressing, erase and insert/delete, scroll regions, the
alternate screen, autowrap, and SGR in all three colour depths (16, 256, and truecolor,
with ``;`` or ``:`` separators). What it does not model it skips by the grammar — a
whole escape sequence, never part of one — so an unknown sequence can never leak its
parameters onto the screen as text.

The grid is the source of truth for the pixels: :class:`Terminal` records which rows
changed since the emulator last drew (:meth:`Terminal.take_dirty`), so a keystroke that moves
the highlight one row repaints two rows of the panel, not fourteen.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from rich.cells import cell_len

#: A colour, as 8-bit channels.
RGB = tuple[int, int, int]

#: Style flags (:attr:`Style.flags` is a bitmask of these).
BOLD = 1
DIM = 2
ITALIC = 4
UNDERLINE = 8
REVERSE = 16
HIDDEN = 32
STRIKE = 64


@dataclass(frozen=True, slots=True)
class Style:
    """How a cell is drawn: its colours (``None`` = the terminal's default) and flags."""

    fg: RGB | None = None
    bg: RGB | None = None
    flags: int = 0


#: The style a fresh or erased cell has.
PLAIN = Style()

#: A cell: the character drawn there (``""`` for the right half of a wide character)
#: and its style.
Cell = tuple[str, Style]

_BLANK: Cell = (" ", PLAIN)

#: A conventional 16-colour palette, for an emulator that passes none of its own.
_DEFAULT_PALETTE: tuple[RGB, ...] = (
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
)  # fmt: skip

_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)

# Parser states.
_GROUND, _ESC, _CSI, _OSC, _CHARSET, _STRING = range(6)


def _xterm_256(index: int, palette: Sequence[RGB]) -> RGB:
    """The colour xterm's 256-colour index ``index`` names."""
    if index < 16:
        return palette[index]
    if index < 232:
        index -= 16
        return (
            _CUBE_LEVELS[index // 36],
            _CUBE_LEVELS[index // 6 % 6],
            _CUBE_LEVELS[index % 6],
        )
    level = 8 + 10 * (index - 232)
    return (level, level, level)


class Terminal:
    """A ``cols`` × ``rows`` character grid driven by a VT byte stream.

    Args:
        cols: Width in cells.
        rows: Height in cells.
        palette: The 16 colours SGR 30–37/90–97 (and 256-colour indices 0–15) name.
        reply: Called with the bytes a real terminal would answer a query with (a
            cursor-position report, a device-attributes reply); ``None`` drops them.
    """

    def __init__(
        self,
        cols: int,
        rows: int,
        *,
        palette: Sequence[RGB] = _DEFAULT_PALETTE,
        reply: Callable[[str], None] | None = None,
    ) -> None:
        """Start blank, the cursor home, in the main screen."""
        self.cols = cols
        self.rows = rows
        self.palette = tuple(palette)
        self._reply = reply
        self._main = self._fresh_screen()
        self._alt = self._fresh_screen()
        self.screen = self._main
        self.x = 0
        self.y = 0
        self.style = PLAIN
        self.cursor_visible = True
        self.autowrap = True
        self.application_cursor = False
        self._wrap_pending = False
        self._top = 0
        self._bottom = rows - 1
        self._saved: tuple[int, int, Style] = (0, 0, PLAIN)
        self._state = _GROUND
        self._params = ""
        self._dirty: set[int] = set(range(rows))

    # --- the grid, as the emulator reads it ------------------------------------------------

    def _fresh_screen(self) -> list[list[Cell]]:
        return [[_BLANK] * self.cols for _ in range(self.rows)]

    def take_dirty(self) -> set[int]:
        """The rows changed since the last call, which this call forgets."""
        dirty, self._dirty = self._dirty, set()
        return dirty

    def line(self, row: int) -> str:
        """Row ``row`` as plain text, wide characters once each."""
        return "".join(char for char, _ in self.screen[row])

    def text(self) -> str:
        """The whole screen as plain text, one line per row."""
        return "\n".join(self.line(row) for row in range(self.rows))

    # --- input --------------------------------------------------------------------------

    def feed(self, data: str) -> None:
        """Parse ``data``, updating the grid."""
        for char in data:
            state = self._state
            if state == _GROUND:
                if char >= " " and char != "\x7f":
                    self._print(char)
                elif char == "\x1b":
                    self._state = _ESC
                else:
                    self._control(char)
            elif state == _CSI:
                if "\x40" <= char <= "\x7e":
                    self._state = _GROUND
                    self._csi(self._params, char)
                elif char == "\x1b":
                    self._state = _ESC
                elif char in "\x18\x1a":
                    self._state = _GROUND  # CAN/SUB abort the sequence
                elif char >= " ":
                    self._params += char
                else:
                    self._control(char)  # C0 controls act even mid-sequence
            elif state == _ESC:
                self._escape(char)
            elif state == _OSC:
                if char == "\x07":
                    self._state = _GROUND
                elif char == "\x1b":
                    self._state = _STRING  # ST is ESC \, which _STRING finishes
            elif state == _STRING:
                if char == "\x1b":
                    self._state = _ESC
                elif char == "\x07":
                    self._state = _GROUND
            else:  # _CHARSET: the designator's one final character
                self._state = _GROUND

    def _escape(self, char: str) -> None:
        self._state = _GROUND
        if char == "[":
            self._state, self._params = _CSI, ""
        elif char == "]":
            self._state = _OSC
        elif char in "()*+-./":
            self._state = _CHARSET
        elif char in "P^_X":
            self._state = _STRING
        elif char == "7":
            self._saved = (self.x, self.y, self.style)
        elif char == "8":
            self._restore()
        elif char == "D":
            self._linefeed()
        elif char == "E":
            self.x = 0
            self._linefeed()
        elif char == "M":
            self._reverse_index()
        elif char == "c":
            self.__init__(self.cols, self.rows, palette=self.palette, reply=self._reply)
        # "=", ">", "\\" and anything else: nothing to draw.

    def _control(self, char: str) -> None:
        if char == "\r":
            self.x = 0
            self._wrap_pending = False
        elif char in "\n\x0b\x0c":
            self._linefeed()
        elif char == "\b":
            self.x = max(0, self.x - 1)
            self._wrap_pending = False
        elif char == "\t":
            self.x = min(self.cols - 1, (self.x // 8 + 1) * 8)
            self._wrap_pending = False
        # BEL, SO/SI and the rest draw nothing.

    # --- drawing --------------------------------------------------------------------------

    def _print(self, char: str) -> None:
        width = cell_len(char)
        row = self.screen[self.y]
        if width == 0:
            # A combining mark rides the cell before it.
            if self.x > 0 or self._wrap_pending:
                col = self.x if self._wrap_pending else self.x - 1
                base, style = row[col]
                row[col] = (base + char, style)
                self._dirty.add(self.y)
            return
        if self._wrap_pending or (width == 2 and self.x == self.cols - 1):
            if self.autowrap:
                self.x = 0
                self._linefeed()
                row = self.screen[self.y]
            self._wrap_pending = False
        x = self.x
        self._unsplit(row, x)
        row[x] = (char, self.style)
        if width == 2 and x + 1 < self.cols:
            self._unsplit(row, x + 1)
            row[x + 1] = ("", self.style)
        self._dirty.add(self.y)
        x += width
        if x >= self.cols:
            self.x = self.cols - 1
            self._wrap_pending = self.autowrap
        else:
            self.x = x

    def _unsplit(self, row: list[Cell], x: int) -> None:
        """Before ``x`` is overwritten, blank whatever half of a wide character it was."""
        char, style = row[x]
        if char == "" and x > 0:
            row[x - 1] = (" ", row[x - 1][1])
        elif x + 1 < self.cols and row[x + 1][0] == "" and cell_len(char) == 2:
            row[x + 1] = (" ", style)

    def _erase_style(self) -> Style:
        """What an erased cell carries: the current background, nothing else."""
        return PLAIN if self.style.bg is None else Style(bg=self.style.bg)

    def _erase(self, row: int, first: int, last: int) -> None:
        cell = (" ", self._erase_style())
        line = self.screen[row]
        for x in range(max(0, first), min(self.cols, last + 1)):
            line[x] = cell
        self._dirty.add(row)

    def _linefeed(self) -> None:
        self._wrap_pending = False
        if self.y == self._bottom:
            self._scroll_up(1)
        elif self.y < self.rows - 1:
            self.y += 1

    def _reverse_index(self) -> None:
        self._wrap_pending = False
        if self.y == self._top:
            self._scroll_down(1)
        elif self.y > 0:
            self.y -= 1

    def _scroll_up(self, count: int, top: int | None = None) -> None:
        top = self._top if top is None else top
        bottom = self._bottom
        count = min(count, bottom - top + 1)
        region = self.screen[top : bottom + 1]
        blank = [(" ", self._erase_style())] * self.cols
        region = region[count:] + [list(blank) for _ in range(count)]
        self.screen[top : bottom + 1] = region
        self._dirty.update(range(top, bottom + 1))

    def _scroll_down(self, count: int, top: int | None = None) -> None:
        top = self._top if top is None else top
        bottom = self._bottom
        count = min(count, bottom - top + 1)
        region = self.screen[top : bottom + 1]
        blank = [(" ", self._erase_style())] * self.cols
        region = [list(blank) for _ in range(count)] + region[: len(region) - count]
        self.screen[top : bottom + 1] = region
        self._dirty.update(range(top, bottom + 1))

    def _restore(self) -> None:
        self.x, self.y, self.style = self._saved
        self.x = min(self.x, self.cols - 1)
        self.y = min(self.y, self.rows - 1)
        self._wrap_pending = False

    def _goto(self, x: int, y: int) -> None:
        self.x = min(max(0, x), self.cols - 1)
        self.y = min(max(0, y), self.rows - 1)
        self._wrap_pending = False

    # --- CSI ------------------------------------------------------------------------------

    def _csi(self, raw: str, final: str) -> None:
        private = raw[:1] if raw[:1] in "?<=>" else ""
        body = raw[len(private) :].rstrip(" !\"#$%&'()*+,-./")  # intermediates
        params = [part for part in body.split(";")] if body else []

        def arg(index: int, default: int) -> int:
            if index >= len(params):
                return default
            head = params[index].split(":")[0]
            return int(head) if head.isdigit() and int(head) else default

        if private == "?":
            if final in "hl":
                self._private_mode(params, final == "h")
            return
        if private:
            return  # secondary DA and kin: nothing to draw
        if final == "m":
            self._sgr(params)
        elif final in "Hf":
            self._goto(arg(1, 1) - 1, arg(0, 1) - 1)
        elif final == "A":
            self._goto(self.x, max(self._top if self.y >= self._top else 0, self.y - arg(0, 1)))
        elif final == "B":
            bottom = self._bottom if self.y <= self._bottom else self.rows - 1
            self._goto(self.x, min(bottom, self.y + arg(0, 1)))
        elif final == "C":
            self._goto(self.x + arg(0, 1), self.y)
        elif final == "D":
            self._goto(self.x - arg(0, 1), self.y)
        elif final == "E":
            self._goto(0, self.y + arg(0, 1))
        elif final == "F":
            self._goto(0, self.y - arg(0, 1))
        elif final == "G":
            self._goto(arg(0, 1) - 1, self.y)
        elif final == "d":
            self._goto(self.x, arg(0, 1) - 1)
        elif final == "J":
            self._erase_display(arg(0, 0) if params and params[0] else 0)
        elif final == "K":
            mode = arg(0, 0) if params and params[0] else 0
            if mode == 0:
                self._erase(self.y, self.x, self.cols - 1)
            elif mode == 1:
                self._erase(self.y, 0, self.x)
            else:
                self._erase(self.y, 0, self.cols - 1)
        elif final == "X":
            self._erase(self.y, self.x, self.x + arg(0, 1) - 1)
        elif final == "@":
            self._insert_chars(arg(0, 1))
        elif final == "P":
            self._delete_chars(arg(0, 1))
        elif final == "L":
            if self._top <= self.y <= self._bottom:
                self._scroll_down(arg(0, 1), top=self.y)
                self.x = 0
        elif final == "M":
            if self._top <= self.y <= self._bottom:
                self._scroll_up(arg(0, 1), top=self.y)
                self.x = 0
        elif final == "S":
            self._scroll_up(arg(0, 1))
        elif final == "T":
            self._scroll_down(arg(0, 1))
        elif final == "r":
            top, bottom = arg(0, 1) - 1, arg(1, self.rows) - 1
            if 0 <= top < bottom < self.rows:
                self._top, self._bottom = top, bottom
                self._goto(0, 0)
        elif final == "s":
            self._saved = (self.x, self.y, self.style)
        elif final == "u":
            self._restore()
        elif final == "n" and self._reply is not None:
            if arg(0, 0) == 6:
                self._reply(f"\x1b[{self.y + 1};{self.x + 1}R")
            elif arg(0, 0) == 5:
                self._reply("\x1b[0n")
        elif final == "c" and self._reply is not None:
            self._reply("\x1b[?1;2c")
        # Anything else (window ops, cursor shape, ...) changes nothing here.

    def _erase_display(self, mode: int) -> None:
        if mode == 0:
            self._erase(self.y, self.x, self.cols - 1)
            for row in range(self.y + 1, self.rows):
                self._erase(row, 0, self.cols - 1)
        elif mode == 1:
            for row in range(0, self.y):
                self._erase(row, 0, self.cols - 1)
            self._erase(self.y, 0, self.x)
        else:
            for row in range(self.rows):
                self._erase(row, 0, self.cols - 1)

    def _insert_chars(self, count: int) -> None:
        line = self.screen[self.y]
        count = min(count, self.cols - self.x)
        cell = (" ", self._erase_style())
        line[self.x :] = ([cell] * count + line[self.x :])[: self.cols - self.x]
        self._dirty.add(self.y)

    def _delete_chars(self, count: int) -> None:
        line = self.screen[self.y]
        count = min(count, self.cols - self.x)
        cell = (" ", self._erase_style())
        line[self.x :] = line[self.x + count :] + [cell] * count
        self._dirty.add(self.y)

    def _private_mode(self, params: list[str], on: bool) -> None:
        for raw in params:
            mode = int(raw) if raw.isdigit() else -1
            if mode == 25:
                self.cursor_visible = on
            elif mode == 7:
                self.autowrap = on
            elif mode == 1:
                self.application_cursor = on
            elif mode in (47, 1047, 1049):
                if mode == 1049 and on:
                    self._saved = (self.x, self.y, self.style)
                target = self._alt if on else self._main
                if target is not self.screen:
                    if on:
                        self._alt = self._fresh_screen()
                        target = self._alt
                    self.screen = target
                    self._dirty.update(range(self.rows))
                if mode == 1049 and not on:
                    self._restore()

    # --- SGR ------------------------------------------------------------------------------

    def _sgr(self, params: list[str]) -> None:
        if not params:
            params = ["0"]
        # Flatten "38:2::r:g:b" (colon sub-parameters) into the ";" form's sequence.
        codes: list[int] = []
        for part in params:
            pieces = part.split(":")
            if len(pieces) > 1 and pieces[0] in ("38", "48", "58"):
                head, rest = pieces[0], pieces[1:]
                if rest[:1] == ["2"] and len(rest) == 5:
                    rest = ["2", *rest[2:]]  # drop the colour-space id
                codes.extend(int(p) if p.isdigit() else 0 for p in (head, *rest))
            else:
                codes.append(int(pieces[0]) if pieces[0].isdigit() else 0)
        fg, bg, flags = self.style.fg, self.style.bg, self.style.flags
        i = 0
        while i < len(codes):
            code = codes[i]
            if code == 0:
                fg, bg, flags = None, None, 0
            elif code == 1:
                flags |= BOLD
            elif code == 2:
                flags |= DIM
            elif code == 3:
                flags |= ITALIC
            elif code in (4, 21):
                flags |= UNDERLINE
            elif code == 7:
                flags |= REVERSE
            elif code == 8:
                flags |= HIDDEN
            elif code == 9:
                flags |= STRIKE
            elif code == 22:
                flags &= ~(BOLD | DIM)
            elif code == 23:
                flags &= ~ITALIC
            elif code == 24:
                flags &= ~UNDERLINE
            elif code == 27:
                flags &= ~REVERSE
            elif code == 28:
                flags &= ~HIDDEN
            elif code == 29:
                flags &= ~STRIKE
            elif 30 <= code <= 37:
                fg = self.palette[code - 30]
            elif 40 <= code <= 47:
                bg = self.palette[code - 40]
            elif 90 <= code <= 97:
                fg = self.palette[code - 90 + 8]
            elif 100 <= code <= 107:
                bg = self.palette[code - 100 + 8]
            elif code == 39:
                fg = None
            elif code == 49:
                bg = None
            elif code in (38, 48, 58):
                colour, used = self._extended(codes, i + 1)
                if code == 38:
                    fg = colour
                elif code == 48:
                    bg = colour
                i += used
            i += 1
        self.style = Style(fg, bg, flags)

    def _extended(self, codes: list[int], start: int) -> tuple[RGB | None, int]:
        """Read an extended colour at ``codes[start:]``: ``(colour, codes consumed)``."""
        kind = codes[start] if start < len(codes) else None
        if kind == 5 and start + 1 < len(codes):
            return _xterm_256(min(255, codes[start + 1]), self.palette), 2
        if kind == 2 and start + 3 < len(codes):
            r, g, b = (min(255, c) for c in codes[start + 1 : start + 4])
            return (r, g, b), 4
        return None, len(codes) - start
