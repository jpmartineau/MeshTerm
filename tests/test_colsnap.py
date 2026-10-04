# SPDX-License-Identifier: Apache-2.0
"""Column pinning: a row lands on the app's grid however wide the terminal draws its glyphs.

The reasoning lives in :mod:`meshterm.ui.tui.colsnap`. What these tests pin is the claim that
makes it worth having — that alignment no longer depends on the two width tables being *right*
about a glyph, only on the row saying where its columns are.

So the fixture here is a terminal that **disagrees on purpose** (:class:`_Terminal`, handed the
glyphs it draws at a width of its own). That is not a contrived case: it is the situation the
emoji-width module documents as unprobeable and unfixable-in-general, and the one a node name
off the air walks into every time somebody puts an emoji in it. Every assertion below is about
where a glyph *landed*, never about what was measured.
"""

from __future__ import annotations

import re

from rich.cells import cell_len

from meshterm.ui.tui import colsnap
from meshterm.ui.tui.emoji_width import clusters

_WAVE = "👋"  # two cells by both stock tables; one cell on the reference terminal's font
_ROAD = "\U0001f6e3"  # 🛣 one cell by both tables — the glyph a font may still draw wide
_CA = "🇨🇦"  # a flag: one glyph out of two Regional Indicators
_FAMILY = "\U0001f468‍\U0001f469‍\U0001f467"  # 👨‍👩‍👧 one glyph out of three joined people
_ROW = "│ ❯ Lakeside      ● ▲ ★ ─── 5m │"  # chrome only: nothing here needs an address


class _Terminal:
    """A terminal that lays out what it is written using a width table of its own.

    Honours exactly one control sequence — ``CSI n G``, the absolute column address — and
    ignores every other one, which is how a real terminal treats the theme's colour runs for
    the purposes of column arithmetic.

    Args:
        width: The terminal's width in cells.
        draws: Glyphs this terminal draws at a width the app did not measure, as
            ``{glyph: cells}``. Everything absent is drawn at the measured width, so a test
            names its disagreement and nothing else.
    """

    _CSI = re.compile(r"\x1b\[([0-9;]*)([A-Za-z])")

    def __init__(self, width: int, *, draws: dict[str, int] | None = None) -> None:
        """Start with a blank row and the cursor in column 0."""
        self.width = width
        self.draws = draws or {}
        self.cells: list[str] = [" "] * width
        self.column = 0

    def write(self, data: str) -> None:
        """Draw ``data``, obeying any column address in it."""
        position = 0
        for match in self._CSI.finditer(data):
            self._draw(data[position : match.start()])
            if match.group(2) == "G":
                self.column = int(match.group(1) or "1") - 1
            position = match.end()
        self._draw(data[position:])

    def _draw(self, text: str) -> None:
        """Place each glyph of one escape-free stretch, advancing by this terminal's widths."""
        for cluster in clusters(text):
            size = self.draws.get(cluster, cell_len(cluster))
            if 0 <= self.column < self.width:
                self.cells[self.column] = cluster
                for overhang in range(1, size):
                    if self.column + overhang < self.width:
                        self.cells[self.column + overhang] = ""
            self.column += size

    def at(self, column: int) -> str:
        """The glyph drawn in ``column``."""
        return self.cells[column]


def test_a_row_of_chrome_is_handed_back_untouched() -> None:
    """The common row costs one scan and no allocation — the same object comes back.

    Borders, block elements, braille and the node marks are what the app draws by the
    thousand, and pinning after each of them would be five bytes and a cluster walk to
    address the column the cursor is already in.
    """
    assert colsnap.snap_row(_ROW) is _ROW
    assert colsnap.snap_row("plain ascii row") is "plain ascii row"  # noqa: F632 - identity
    assert colsnap.snap_row("⠁⠂⠃⡿ braille chart ▁▃▅█ ╭─╮") is not None


def test_an_emoji_is_followed_by_the_column_it_was_measured_into() -> None:
    """The whole mechanism: the glyph, then the one-based column the next glyph belongs in."""
    assert cell_len(_WAVE) == 2, "the stock table measures the wave at two cells"
    # "a" fills column 0, the wave is measured into columns 1-2, so "b" belongs in column 3.
    assert colsnap.snap_row(f"a{_WAVE}b") == f"a\x1b[2X{_WAVE}\x1b[4Gb"


def test_nothing_is_pinned_after_the_last_glyph_on_the_row() -> None:
    """An address is held over until something drawable follows it, and dropped if none does.

    A chat line ending on an emoji is the common case, and there is nothing after it whose
    column could be wrong.
    """
    assert colsnap.snap_row(f"hi {_WAVE}") == f"hi \x1b[2X{_WAVE}"


def test_a_style_change_occupies_no_column_and_stays_where_the_theme_put_it() -> None:
    """A colour run is copied through without advancing the column it would have shifted."""
    pinned = colsnap.snap_row(f"\x1b[31m{_WAVE}\x1b[0m|")
    assert pinned == f"\x1b[31m\x1b[2X{_WAVE}\x1b[0m\x1b[3G|"


def test_a_flag_and_a_joined_sequence_are_pinned_once_as_whole_glyphs() -> None:
    """One address per glyph, not per codepoint — the cut that halves a flag is never made."""
    for glyph in (_CA, _FAMILY):
        width = cell_len(glyph)
        pinned = colsnap.snap_row(f"{glyph}|")
        assert pinned == f"\x1b[{width}X{glyph}\x1b[{width + 1}G|"
        assert pinned.count("G") == 1


def test_a_bare_text_emoji_is_pinned_even_though_both_tables_agree_about_it() -> None:
    """Agreement is not certainty: what a font draws is still the font's business.

    ``🛣`` is the glyph whose measurement must not be *forced* to two (that pulled the Trophy
    case's border in). Pinning is the other answer to the same doubt: leave the measurement
    alone and say where the next column is, so the row holds either way.
    """
    assert cell_len(_ROAD) == 1
    assert colsnap.snap_row(f"{_ROAD}|") == f"{_ROAD}\x1b[2G|"


def test_the_row_frames_flush_on_a_terminal_that_draws_the_glyph_narrow() -> None:
    """The notch, and its absence: a border lands in its measured column once pinned."""
    row = f"│ {_WAVE} Lakeside │"
    edge = cell_len(row) - 1

    loose = _Terminal(cell_len(row), draws={_WAVE: 1})
    loose.write(row)
    assert loose.at(edge) != "│", "unpinned, the narrow glyph pulls the border a column in"
    assert loose.column == cell_len(row) - 1

    pinned = _Terminal(cell_len(row), draws={_WAVE: 1})
    pinned.write(colsnap.snap_row(row))
    assert pinned.at(edge) == "│"
    assert pinned.column == cell_len(row)


def test_the_row_frames_flush_on_a_terminal_that_draws_the_glyph_wide() -> None:
    """The mirror case: the overhang is written over, and the row still ends where it should.

    One cell of the glyph is lost to its neighbour, which is the trade — a cell of cosmetic
    damage inside the glyph's own lane, in place of every lane after it moving.
    """
    row = f"│ {_ROAD} Lakeside │"
    edge = cell_len(row) - 1

    loose = _Terminal(cell_len(row), draws={_ROAD: 2})
    loose.write(row)
    assert loose.at(edge) != "│"

    pinned = _Terminal(cell_len(row), draws={_ROAD: 2})
    pinned.write(colsnap.snap_row(row))
    assert pinned.at(edge) == "│"


def test_every_lane_of_a_archive_preview_row_lands_where_the_screen_measured_it() -> None:
    """The screen this started on: a swept contact whose name carries an emoji.

    The name lane is already fitted in display cells (:func:`~meshterm.ui.menus.fit_cells`),
    which is the padding that cannot help — it is computed with the same measurement the
    terminal disagrees with. What holds the evidence lanes under their headers is the pinning.
    """
    from meshterm.core.contact_score import ContactSignals, ScoredContact
    from meshterm.core.models import Contact
    from meshterm.ui.sweep_screen import _NAME_W, _lane_widths, _victim_row
    from meshterm.ui.tui.render import render_to_ansi

    key = "ab" * 32
    victim = ScoredContact(
        contact=Contact(name=f"{_WAVE} Lakeside", public_key=key, key_prefix=key[:12]),
        signals=ContactSignals(node=key[:12], packets=12, hops=2.0),
        score=1.0,
        percentile=3,
    )
    row = _victim_row(victim, _lane_widths([victim]))
    measured = cell_len(row.plain)
    ansi = render_to_ansi(row, 72, no_wrap=True)

    loose = _Terminal(80, draws={_WAVE: 1})
    loose.write(ansi)
    assert loose.column != measured, "unpinned, one emoji shifts every lane after it"

    pinned = _Terminal(80, draws={_WAVE: 1})
    pinned.write(colsnap.snap_row(ansi))
    assert pinned.column == measured
    # Not just the right edge: *every* glyph of the row is in the column the screen's own
    # cell arithmetic put it in, which is what keeps a lane under the header naming it.
    column = 0
    for cluster in clusters(row.plain):
        if cluster != " ":
            assert pinned.at(column) == cluster, f"column {column} holds the wrong glyph"
        column += cell_len(cluster)
    assert column == measured
    assert _NAME_W == 22  # the name lane the evidence starts after, gap included


def test_pinning_is_on_with_an_escape_hatch(monkeypatch) -> None:  # noqa: ANN001
    """On by default; only an explicit 0 writes rows the way they were composed."""
    monkeypatch.delenv("MESHTERM_COLUMN_SNAP", raising=False)
    assert colsnap.enabled()
    monkeypatch.setenv("MESHTERM_COLUMN_SNAP", "0")
    assert not colsnap.enabled()


def test_the_row_writer_pins_what_it_writes(monkeypatch) -> None:  # noqa: ANN001
    """The wiring: a composed frame reaches the terminal carrying its column addresses."""
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.styles import Style

    from meshterm.ui.tui.fastrender import FastRenderer

    class _Capturing:
        """Just enough output for one paint: what was written, and how big the screen is."""

        def __init__(self) -> None:
            self.written: list[str] = []

        def write_raw(self, data: str) -> None:
            self.written.append(data)

        def get_size(self) -> Size:
            return Size(rows=26, columns=72)

        def __getattr__(self, name: str):  # noqa: ANN001, ANN204 - the rest is inert here
            return lambda *args, **kwargs: None

    frame = f"contact {_WAVE} 5m\nsecond row\n"
    out = _Capturing()
    renderer = FastRenderer(Style([]), out, full_screen=True, frame_source=lambda: frame)
    renderer.render(None, None)
    assert "\x1b[11G" in "".join(out.written)  # the wave measured into columns 8-9

    monkeypatch.setenv("MESHTERM_COLUMN_SNAP", "0")
    off = _Capturing()
    plain = FastRenderer(Style([]), off, full_screen=True, frame_source=lambda: frame)
    plain.render(None, None)
    assert "\x1b[11G" not in "".join(off.written)


def test_a_platform_that_draws_no_emoji_pins_nothing() -> None:
    """The PicoCalc draws only glyphs its own font has verified, so there is nothing to pin."""
    from meshterm.platforms import PICOCALC_LYRA, set_platform

    assert colsnap.enabled()
    set_platform(PICOCALC_LYRA)
    assert not colsnap.enabled()


# --- prompt_toolkit's renderer: the dialog path -------------------------------------------------


class _Tty:
    """A two-dimensional terminal, for the renderer that moves its cursor relatively.

    Honours what prompt_toolkit's differential renderer and :class:`colsnap.PinnedOutput` send:
    carriage return, newline, backspace, cursor save and restore, and the CSI cursor moves.
    Colour and mode sequences are ignored, which is all a terminal does with them as far as
    column arithmetic goes. Glyph widths come from prompt_toolkit's own measurement unless
    ``draws`` names a glyph this terminal disagrees about.
    """

    _SEQUENCE = re.compile(r"\x1b(?:\[([0-9;?]*)([A-Za-z@`])|([78]))")

    def __init__(self, cols: int, rows: int, *, draws: dict[str, int] | None = None) -> None:
        """Start blank, with the cursor home."""
        self.cols, self.rows = cols, rows
        self.draws = draws or {}
        self.grid = [[" "] * cols for _ in range(rows)]
        self.x = self.y = 0
        self._saved = (0, 0)

    def feed(self, data: str) -> None:
        """Interpret ``data`` as the terminal would."""
        position = 0
        for match in self._SEQUENCE.finditer(data):
            self._text(data[position : match.start()])
            position = match.end()
            if match.group(3) == "7":
                self._saved = (self.x, self.y)
            elif match.group(3) == "8":
                self.x, self.y = self._saved
            else:
                self._csi(match.group(1), match.group(2))
        self._text(data[position:])

    def _csi(self, params: str, final: str) -> None:
        """One control sequence: the cursor moves, the erases, and nothing else."""
        if params.startswith("?"):
            return
        numbers = [int(part) if part else 0 for part in params.split(";")] if params else []
        count = numbers[0] if numbers and numbers[0] else 1
        if final == "A":
            self.y = max(0, self.y - count)
        elif final == "B":
            self.y = min(self.rows - 1, self.y + count)
        elif final == "C":
            self.x = min(self.cols - 1, self.x + count)
        elif final == "D":
            self.x = max(0, self.x - count)
        elif final == "G":
            self.x = count - 1
        elif final in "Hf":
            row = numbers[0] if numbers and numbers[0] else 1
            column = numbers[1] if len(numbers) > 1 and numbers[1] else 1
            self.y, self.x = row - 1, column - 1
        elif final == "X":
            for column in range(self.x, min(self.cols, self.x + count)):
                self.grid[self.y][column] = " "
        elif final == "K":
            self.grid[self.y][self.x :] = [" "] * (self.cols - self.x)
        elif final == "J":
            self.grid[self.y][self.x :] = [" "] * (self.cols - self.x)
            for row in range(self.y + 1, self.rows):
                self.grid[row] = [" "] * self.cols

    def _text(self, text: str) -> None:
        """Place each glyph at the cursor, advancing by this terminal's idea of its width."""
        from prompt_toolkit.utils import get_cwidth

        for cluster in clusters(text):
            if cluster == "\r":
                self.x = 0
            elif cluster == "\n":
                self.y = min(self.rows - 1, self.y + 1)
            elif cluster == "\b":
                self.x = max(0, self.x - 1)
            else:
                size = self.draws.get(cluster, get_cwidth(cluster))
                if self.x < self.cols:
                    self.grid[self.y][self.x] = cluster
                    for overhang in range(1, size):
                        if self.x + overhang < self.cols:
                            self.grid[self.y][self.x + overhang] = ""
                self.x += size

    def glyph_at(self, row: int, column: int) -> str:
        """The glyph drawn at ``(row, column)``."""
        return self.grid[row][column]


def _screen(rows: list[str], cols: int):  # noqa: ANN202 - a prompt_toolkit Screen
    """Lay ``rows`` out exactly as prompt_toolkit lays out a window's text."""
    from prompt_toolkit.layout import Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.mouse_handlers import MouseHandlers
    from prompt_toolkit.layout.screen import Screen, WritePosition

    screen = Screen()
    window = Window(FormattedTextControl("\n".join(rows)), wrap_lines=False)
    window.write_to_screen(
        screen, MouseHandlers(), WritePosition(0, 0, cols, len(rows)), "", False, None
    )
    return screen


def _paint(output, screen, previous, position, cols: int, rows: int):  # noqa: ANN001, ANN202
    """One pass of prompt_toolkit's own differential writer over ``screen``."""
    from types import SimpleNamespace

    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.output.color_depth import ColorDepth
    from prompt_toolkit.renderer import (
        _output_screen_diff,
        _StyleStringHasStyleCache,
        _StyleStringToAttrsCache,
    )
    from prompt_toolkit.styles import DummyStyleTransformation, Style

    attrs = _StyleStringToAttrsCache(Style([]).get_attrs_for_style_str, DummyStyleTransformation())
    position, _style = _output_screen_diff(
        SimpleNamespace(layout=SimpleNamespace(current_window=None)),
        output,
        screen,
        position,
        ColorDepth.DEPTH_24_BIT,
        previous,
        None,
        False,
        True,
        attrs,
        _StyleStringHasStyleCache(attrs),
        Size(rows=rows, columns=cols),
        cols if previous is not None else 0,
    )
    return position


def _vt100(cols: int, rows: int):  # noqa: ANN202 - a Vt100_Output writing into memory
    """A real VT100 output, so every cursor move is spelled the way the terminal receives it."""
    import io

    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.output.vt100 import Vt100_Output

    return Vt100_Output(io.StringIO(), lambda: Size(rows=rows, columns=cols), term="xterm")


def _drained(output) -> str:  # noqa: ANN001
    """Everything written to ``output`` since the last drain."""
    vt100 = getattr(output, "_inner", output)
    data = "".join(vt100._buffer)
    vt100._buffer.clear()
    return data


def _assert_landed(tty: _Tty, row: int, text: str) -> None:
    """Every visible glyph of ``text`` is in the column prompt_toolkit measured it into."""
    from prompt_toolkit.utils import get_cwidth

    column = 0
    for cluster in clusters(text):
        if cluster != " ":
            assert tty.glyph_at(row, column) == cluster, f"column {column} of row {row}"
        column += get_cwidth(cluster)


def test_a_dialog_row_frames_flush_on_a_terminal_that_draws_the_glyph_narrow() -> None:
    """prompt_toolkit's renderer, unpinned and pinned, on a terminal that disagrees about 👋.

    The renderer writes the whole row in one run after its first move, adding two cells for the
    wave to a cursor of its own while the terminal adds one. Unpinned, the rest of the row is
    drawn a column early. Pinned, every glyph is where the renderer measured it.
    """
    cols, rows = 24, 2
    text = [f"│ {_WAVE} Lakeside  5m │", "│ Plain row    5m │"]
    screen = _screen(text, cols)

    loose, loose_tty = _vt100(cols, rows), _Tty(cols, rows, draws={_WAVE: 1})
    _paint(loose, screen, None, _point(), cols, rows)
    loose_tty.feed(_drained(loose))
    assert loose_tty.glyph_at(0, cell_len(text[0]) - 1) != "│", "unpinned, the border moves"

    pinned, pinned_tty = (
        colsnap.PinnedOutput(_vt100(cols, rows)),
        _Tty(cols, rows, draws={_WAVE: 1}),
    )
    _paint(pinned, screen, None, _point(), cols, rows)
    pinned_tty.feed(_drained(pinned))
    for row, line in enumerate(text):
        _assert_landed(pinned_tty, row, line)


def test_a_repaint_after_the_glyph_lands_where_the_renderer_measured_it() -> None:
    """The move that follows a pinned glyph is computed relatively, and still lands.

    A second paint changes the glyph *and* a value later on the same row, with unchanged cells
    between them, so the renderer writes the new glyph and then jumps forward by the distance its
    own arithmetic says. That jump is only right if the terminal's cursor was put back in step
    after the glyph — which is the whole claim of the relative pin.
    """
    cols, rows = 24, 1
    first = [f"│ {_WAVE} Lakeside  5m │"]
    second = [f"│ {_ROCKET} Lakeside  7m │"]
    before, after = _screen(first, cols), _screen(second, cols)

    for pin, lands in ((False, False), (True, True)):
        output = _vt100(cols, rows)
        if pin:
            output = colsnap.PinnedOutput(output)
        tty = _Tty(cols, rows, draws={_WAVE: 1, _ROCKET: 1})
        position = _paint(output, before, None, _point(), cols, rows)
        tty.feed(_drained(output))
        _paint(output, after, before, position, cols, rows)
        tty.feed(_drained(output))
        seven = cell_len(second[0].split("7m")[0])
        assert (tty.glyph_at(0, seven) == "7") is lands


def test_a_glyph_drawn_narrow_leaves_no_character_from_the_last_frame_beside_it() -> None:
    """The cell a narrow-drawn glyph does not cover is blank, not what the last frame put there.

    prompt_toolkit treats the cell after a wide glyph as part of it and never writes it, so a
    repaint that puts a two-cell glyph where two letters were leaves the second letter standing
    on a terminal that draws the glyph in one cell. The pin erases its reservation first.
    """
    cols, rows = 16, 1
    before, after = _screen(["│ ab Lakeside │"], cols), _screen([f"│ {_WAVE} Lakeside │"], cols)

    for pin, stale in ((False, True), (True, False)):
        output = _vt100(cols, rows)
        if pin:
            output = colsnap.PinnedOutput(output)
        tty = _Tty(cols, rows, draws={_WAVE: 1})
        position = _paint(output, before, None, _point(), cols, rows)
        tty.feed(_drained(output))
        _paint(output, after, before, position, cols, rows)
        tty.feed(_drained(output))
        assert tty.glyph_at(0, 2) == _WAVE
        assert (tty.glyph_at(0, 3) == "b") is stale


def test_the_pinned_output_brackets_only_a_lone_uncertain_glyph() -> None:
    """What reaches the terminal for each kind of write prompt_toolkit's renderer makes."""

    class _Recorder:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def write(self, data: str) -> None:
            self.calls.append(("write", data))

        def write_raw(self, data: str) -> None:
            self.calls.append(("raw", data))

    recorder = _Recorder()
    output = colsnap.PinnedOutput(recorder)
    for data in ("a", "\r\n", "│", "⠿", "é", "ab"):
        output.write(data)
    assert recorder.calls == [
        ("write", "a"),
        ("write", "\r\n"),
        ("write", "│"),
        ("write", "⠿"),
        ("write", "é"),
        ("write", "ab"),
    ], "a cell of text passes straight through"

    for glyph in (_WAVE, _FAMILY, _CA):
        recorder.calls.clear()
        output.write(glyph)
        from prompt_toolkit.utils import get_cwidth

        step = get_cwidth(glyph)
        assert recorder.calls == [
            ("raw", f"\x1b7\x1b[{step}X"),
            ("write", glyph),
            ("raw", f"\x1b8\x1b[{step}C"),
        ]

    recorder.calls.clear()
    output.write(f"{_WAVE}{_WAVE}")  # two glyphs is not a cell, and is not pinned
    assert recorder.calls == [("write", f"{_WAVE}{_WAVE}")]
    assert output.calls is recorder.calls  # everything else forwards


_ROCKET = "\U0001f680"  # 🚀 a second emoji the disagreeing terminal draws in one cell


def _point():  # noqa: ANN202 - a prompt_toolkit Point at the origin
    """The cursor at home, where a first paint starts."""
    from prompt_toolkit.data_structures import Point

    return Point(x=0, y=0)
