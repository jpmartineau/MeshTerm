# SPDX-License-Identifier: Apache-2.0
"""Reserve-two emoji widths: one rule in each width authority, and each glyph kept whole.

:mod:`meshterm.ui.tui.emoji_width` explains how MeshTerm measures a width.
:mod:`meshterm.ui.tui.colsnap` places the glyphs. These tests prove that:

* Each glyph that a terminal can draw as an emoji has a reserve of two cells, in Rich and in
  prompt_toolkit.
* The marks of the app, the single-cell ranges of Rich, and ordinary text keep their stock
  widths.
* The three measurements of Rich and the measurement of prompt_toolkit agree for each
  string. Thus the segment splitter of Rich does not walk past a cut that it can never meet.
* The source of the app draws no text-default emoji that its vocabulary does not name.
* A glyph that has several codepoints is split, cut, and laid out as the one glyph that it is.

:func:`~meshterm.ui.tui.emoji_width.install` patches tables that are global to the process.
Thus each test that needs it uses the ``installed`` fixture, which puts each table and cache
back afterwards.
"""

from __future__ import annotations

import ast
import pathlib
import threading
from collections.abc import Iterator

import prompt_toolkit.utils as ptu
import pytest
import rich.cells as cells
import rich.segment as segment
from prompt_toolkit.utils import get_cwidth

from meshterm.ui.tui import emoji_width as ew

_ZWJ = "\u200d"  # the joiner: it shows that the characters next to it are one glyph
_VS16 = "\ufe0f"  # the request for the emoji presentation
_WAVE = "\U0001f44b"  # 👋 emoji presentation by default: the stock tables already give two cells
_DISH = "\U0001f4e1"  # 📡 a menu icon, with the same width
_CA = "\U0001f1e8\U0001f1e6"  # 🇨🇦 a flag: two Regional Indicators, one glyph
_CN = "\U0001f1e8\U0001f1f3"  # 🇨🇳 a second flag with the same "C" indicator
_SUN = "☀"  # ☀ a text-default emoji, by itself
_PLANE = "\U0001f6e9"  # 🛩 another one. A font can draw it in either way
_HEART = "❤"  # ❤ another one
_ROAD = "\U0001f6e3"  # 🛣 text-default also, but one of the marks of the app
_WEB = "\U0001f578"  # 🕸 the same
_BIN = "\U0001f5d1"  # 🗑 the same
_WARN = "⚠"  # ⚠ the same
_SHRUG = f"\U0001f937{_ZWJ}♂{_VS16}"  # the shrug: base, joiner, sign, selector
_FAMILY = f"\U0001f468{_ZWJ}\U0001f469{_ZWJ}\U0001f467"  # the family: three joined people
_THUMB = "\U0001f44d\U0001f3fd"  # a thumb and its skin tone, no joiner: it is still one glyph
_TECHIE = f"\U0001f468\U0001f3fb{_ZWJ}\U0001f4bb"  # the technologist: toned, then joined
_KEYCAP = f"1{_VS16}\u20e3"  # a keycap: a digit, a selector, and the enclosing mark
_FLAG = "\U0001f3f4"  # a black flag, the base of a pirate flag
_STRANDED = f"{_FLAG}{_ZWJ}"  # a pirate flag that the byte limit of a name cut after its joiner

#: Strings that each authority must measure in the same way: emoji of each shape, the marks of
#: the app, and the scripts and marks that the rule must not change.
_SAMPLES = (
    "plain ascii",
    f"Bob {_FAMILY} x",
    f"{_CA}{_CN}",
    f"{_SUN}{_VS16}{_SHRUG}",
    f"{_SUN} bare, {_PLANE} bare, {_HEART} bare",
    f"a{_ZWJ}b",
    f"{_STRANDED}  x",
    f"{_KEYCAP}!",
    f"│ {_WARN}{_VS16} warn │ {_WARN} mark │",
    f"Tech {_TECHIE} x {_THUMB}{_THUMB}",
    "漢字 names",
    "ष\u094d\u200dक",  # a Devanagari conjunct: a joiner between letters, not pictographs
    f"{_CA[0]} a lone indicator",
    "e\u0301 a combining accent",
    f"{_ROAD}{_WEB}{_BIN} ↔ ↕",
)


@pytest.fixture
def installed() -> Iterator[None]:
    """Install the reserve-two rule for one test, and put each table and cache back after it."""
    from meshterm.ui.tui import colsnap
    from meshterm.ui.tui.render import _ANSI_CACHE

    saved = (
        cells._cell_len,
        cells.get_character_cell_size,
        cells.split_graphemes,
        segment.get_character_cell_size,
        ptu._CHAR_SIZES_CACHE,
        ew._INSTALLED,
        ew._CLUSTERS,
    )
    ew._INSTALLED = False
    ew.install()
    try:
        yield
    finally:
        (
            cells._cell_len,
            cells.get_character_cell_size,
            cells.split_graphemes,
            segment.get_character_cell_size,
            ptu._CHAR_SIZES_CACHE,
            ew._INSTALLED,
            ew._CLUSTERS,
        ) = saved
        cells.cached_cell_len.cache_clear()
        _ANSI_CACHE.clear()
        colsnap._CACHE.clear()


def test_whatever_may_be_drawn_as_an_emoji_is_reserved_two_cells(installed) -> None:  # noqa: ANN001
    """Each shape of emoji measures two in both authorities, whatever the stock tables said.

    The stock answers were not consistent. prompt_toolkit gave a flag four cells, a sun that
    asks for the emoji presentation one cell, a toned thumb four cells, and a family six
    cells. An answer below two is not safe for a glyph that a font can draw in two cells,
    because the pin after the glyph would overwrite its right half.
    """
    for glyph in (
        _WAVE,
        _DISH,
        _SUN,
        f"{_SUN}{_VS16}",
        _PLANE,
        f"{_PLANE}{_VS16}",
        _HEART,
        _CA,
        _CA[0],
        _SHRUG,
        _FAMILY,
        _THUMB,
        _TECHIE,
        _KEYCAP,
        f"{_WARN}{_VS16}",  # a mark that asks for the emoji presentation is an emoji
    ):
        assert cells.cell_len(glyph) == 2, f"Rich: {glyph!r}"
        assert get_cwidth(glyph) == 2, f"prompt_toolkit: {glyph!r}"


def test_text_the_app_draws_keeps_its_stock_width(installed) -> None:  # noqa: ANN001
    """The rule reserves nothing without a reason: text, chrome, and the marks of the app.

    The marks are also text-default emoji. But they are the vocabulary of the app, and the
    terminals that run the app draw them in one cell. Thus they keep the one cell that each
    screen uses in its layout. Rich measures its single-cell ranges with a fast path that no
    patch reaches, so all other measurements must agree with that path.
    """
    for text, width in (
        ("a", 1),
        ("#", 1),
        ("1", 1),
        ("é", 1),
        ("漢", 2),
        ("─", 1),
        ("⠿", 1),
        ("©", 1),
        ("▶", 1),
        (_WARN, 1),
        (_ROAD, 1),
        (_WEB, 1),
        (_BIN, 1),
        ("↕", 1),
        ("↔", 1),
        ("↩", 1),
        ("⌨", 1),
        ("⚙", 1),
        ("a\U0001f3fd", 1),  # a skin tone after a letter leaves the letter a letter
    ):
        assert cells.cell_len(text) == width, f"Rich: {text!r}"
        assert get_cwidth(text) == width, f"prompt_toolkit: {text!r}"


def test_every_authority_measures_every_string_the_same(installed) -> None:  # noqa: ANN001
    """The whole-string width of Rich, its grapheme spans, and the cache of prompt_toolkit agree.

    Also, each single character has the same width in all of them. These are the four places
    that read a width. MeshTerm lays out a row correctly only if they give one answer.
    """
    for text in _SAMPLES:
        spans, total = cells.split_graphemes(text)
        assert cells.cell_len(text) == total == get_cwidth(text), text
        assert total == sum(width for _start, _end, width in spans), text
        assert spans[0][0] == 0 and spans[-1][1] == len(text), f"spans cover {text!r}"
        for char in text:
            size = cells.get_character_cell_size(char)
            assert size == segment.get_character_cell_size(char) == get_cwidth(char), repr(char)


def test_no_prefix_outgrows_the_character_that_ends_it(installed) -> None:  # noqa: ANN001
    """The invariant that the segment splitter of Rich needs to end, then the splitter itself.

    ``Segment.split_cells`` steps through a string one codepoint at a time until a whole-string
    width reaches the cut. It steps over a two-cell character only where that character
    measures two by itself. Suppose a prefix grew by two at a character that measures one.
    The cut would be a place that the walk steps past in both directions for ever. Thus the
    patch changes the character width and the string width. This test checks the invariant
    before it uses the splitter.
    """
    for text in _SAMPLES:
        for index, char in enumerate(text):
            grew = cells.cell_len(text[: index + 1]) - cells.cell_len(text[:index])
            assert 0 <= grew <= 2, f"{text!r} at {index}"
            if grew == 2:
                assert cells.get_character_cell_size(char) == 2, f"{text!r} at {index}"

    def split_everywhere() -> None:
        for text in _SAMPLES:
            for cut in range(cells.cell_len(text) + 1):
                segment.Segment(text).split_cells(cut)

    worker = threading.Thread(target=split_everywhere, daemon=True)
    worker.start()
    worker.join(timeout=20)
    assert not worker.is_alive(), "a segment split never met its cut"


def test_a_stranded_joiner_leaves_the_next_character_its_cell(installed) -> None:  # noqa: ANN001
    """A joiner with no pictograph after it joins nothing, in both authorities.

    MeshCore cuts a name at a byte limit. Thus a name that ends with a pirate flag arrives as
    the black flag and a bare joiner, followed by the padding of the lane. The stock loop of
    Rich folded the padding into the flag. Thus the name lane measured one cell less than
    the terminal drew.
    """
    padded = f"{_STRANDED}  x"  # the two cells of the flag, two of padding, then the next lane
    assert cells.cell_len(padded) == 5 and get_cwidth(padded) == 5
    # A real sequence still joins: the male sign of the shrug is a pictograph, not a space.
    assert cells.cell_len(f"{_SHRUG} x") == 4 and get_cwidth(f"{_SHRUG} x") == 4


def test_install_happens_once(installed) -> None:  # noqa: ANN001
    """A second call does nothing: the tables that the first call patched do not change."""
    patched = (cells._cell_len, ptu._CHAR_SIZES_CACHE)
    ew.install()
    assert (cells._cell_len, ptu._CHAR_SIZES_CACHE) == patched


def test_the_app_draws_no_text_default_emoji_its_vocabulary_does_not_name() -> None:
    """Each text-default emoji that the package draws is one of its marks, and each mark is used.

    This test keeps :data:`~meshterm.ui.tui.emoji_width._APP_TEXT_MARKS` a closed vocabulary.
    Without it, the list would depend on care. A new one-cell mark that a developer adds to a
    screen, and does not name in the list, fails here. Without the test, the mark would get
    a blank cell next to it with no warning. The test skips prose, because MeshTerm reads
    docstrings and bare strings and does not draw them. It also skips the module that defines
    the list.
    """
    from rich._unicode_data import load

    text_default = {
        char
        for char in load("auto").narrow_to_wide
        if not char.isascii() and char not in cells._SINGLE_CELLS
    }
    # Inputs to the substitution table of the PicoCalc, which maps them to glyphs that its
    # font has. A terminal that shows emoji never draws them, so they are not marks.
    fold_inputs = {"↪", "✔", "✖"}

    package = pathlib.Path(ew.__file__).parents[2]
    drawn: set[str] = set()
    for path in package.rglob("*.py"):
        if path.name == "emoji_width.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        prose = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in prose:
                    drawn |= set(node.value) & text_default

    assert drawn - fold_inputs <= ew._APP_TEXT_MARKS, (
        f"name these in _APP_TEXT_MARKS: {sorted(drawn - fold_inputs - ew._APP_TEXT_MARKS)}"
    )
    assert ew._APP_TEXT_MARKS <= drawn, (
        f"no longer drawn anywhere: {sorted(ew._APP_TEXT_MARKS - drawn)}"
    )


def test_clusters_split_between_glyphs_and_never_inside_one() -> None:
    """The unit where a lane can cut and by which a width is measured: one entry for each glyph.

    A joined run, the indicator pair of a flag, a toned base, and a keycap each return whole,
    because each is one glyph. A joiner that joins nothing is a separate entry (refer to
    :func:`~meshterm.ui.tui.emoji_width._joins`), and the letters around a joiner are also
    separate entries.
    """
    text = f"a{_FAMILY}{_CA}{_THUMB}{_KEYCAP}b"
    assert list(ew.clusters(text)) == ["a", _FAMILY, _CA, _THUMB, _KEYCAP, "b"]
    # Two flags in a row make two pairs, one at a time, and do not run together into four.
    assert list(ew.clusters(_CA + _CN)) == [_CA, _CN]
    # A name that the byte limit cut just after a joiner: the joiner is not part of the flag.
    assert list(ew.clusters(_STRANDED)) == [_FLAG, _ZWJ]
    # A joiner between letters joins nothing that a font draws as one emoji.
    assert list(ew.clusters(f"a{_ZWJ}b")) == ["a", _ZWJ, "b"]


def test_cut_cells_keeps_every_glyph_whole_and_strands_no_joiner() -> None:
    """The cut of a lane is between glyphs, so MeshTerm measures nothing that it does not draw."""
    assert ew.cut_cells(f"Bob {_FAMILY}", 6) == f"Bob {_FAMILY}"  # it fits, so it stays
    assert ew.cut_cells(f"Bob {_FAMILY}", 5) == "Bob "  # it does not fit: the cut removes it whole
    assert ew.cut_cells(_CA, 1) == ""  # half of a flag is a letter, not half of a glyph
    assert ew.cut_cells(_STRANDED, 4) == _FLAG  # the joiner does not stay at the end of the cut
    assert ew.cut_cells("plain name", 5) == "plain"


def test_drawable_folds_only_what_no_terminal_can_draw() -> None:
    """A newline, an escape, or a bidi override in an advert name changes to a space.

    None of them is a question of width. They are text that must not reach the terminal at
    all. The joiner is also in the format class, but the function keeps it, because it holds
    an emoji together.
    """
    assert ew.drawable("line\nbreak") == "line break"
    assert ew.drawable("esc\x1b[31mape") == "esc [31mape"
    assert ew.drawable("flip\u202eme") == "flip me"
    assert ew.drawable(f"Bob {_FAMILY}{_THUMB}") == f"Bob {_FAMILY}{_THUMB}"


def test_join_clusters_merges_every_multi_codepoint_glyph() -> None:
    """The fragment merge puts each glyph that has several codepoints into one fragment.

    The ANSI text of prompt_toolkit arrives with one codepoint in each fragment. Thus a glyph
    is a run that the merge must gather. The merge now gathers each such run, not only the
    joined runs. A mark that measures one cell and has a selector after it must not fold
    into a one-cell slot.
    """

    def line(text: str) -> list:
        return [("", char) for char in text]

    def texts(fragments: list) -> list:
        return [text for _style, text in fragments]

    # A line with nothing to merge is returned as it is: the same object, not a copy.
    plain = line(f"hi {_DISH} {_WARN}")
    assert ew._join_clusters(plain) is plain

    assert texts(ew._join_clusters(line(f"|{_SHRUG}|"))) == ["|", _SHRUG, "|"]
    assert texts(ew._join_clusters(line(_FAMILY))) == [_FAMILY]
    burning = f"❤{_VS16}{_ZWJ}\U0001f525"  # ❤\ufe0f\u200d🔥 a selector on the base, then a join
    assert texts(ew._join_clusters(line(burning))) == [burning]
    # A selector pair, a toned emoji, and a keycap are one glyph each. The merge joins them
    # like the other glyphs.
    assert texts(ew._join_clusters(line(f"{_SUN}{_VS16}{_SHRUG}"))) == [f"{_SUN}{_VS16}", _SHRUG]
    assert texts(ew._join_clusters(line(f"{_WARN}{_VS16}|"))) == [f"{_WARN}{_VS16}", "|"]
    assert texts(ew._join_clusters(line(f"{_THUMB}{_KEYCAP}"))) == [_THUMB, _KEYCAP]
    # The two indicators of a flag are one glyph, and the code writes them as one. If a cursor
    # pin were between the halves, the terminal would draw two letters instead of a flag.
    assert texts(ew._join_clusters(line(f"|{_CA}|"))) == ["|", _CA, "|"]
    assert texts(ew._join_clusters(line(f"{_CA}{_CN}"))) == [_CA, _CN]
    # A single indicator is not a pair, and a trailing or stranded joiner joins nothing.
    assert texts(ew._join_clusters(line(f"{_CA[0]} x"))) == [_CA[0], " ", "x"]
    assert texts(ew._join_clusters(line(f"a{_ZWJ}"))) == ["a", _ZWJ]
    assert texts(ew._join_clusters(line(f"{_STRANDED} x"))) == [_FLAG, _ZWJ, " ", "x"]

    # A merged fragment iterates as the whole glyph. Thus prompt_toolkit builds one Char for
    # it instead of one Char for each codepoint.
    (merged,) = texts(ew._join_clusters(line(_SHRUG)))
    assert list(merged) == [_SHRUG] and merged == _SHRUG


def test_cluster_control_lays_each_glyph_into_a_single_screen_cell(installed) -> None:  # noqa: ANN001
    """The delivery half: one two-cell ``Char`` for each glyph, with each codepoint still written.

    A correct measurement of a glyph is not enough. prompt_toolkit lays out one codepoint at
    a time. Thus an unmerged shrug puts its male sign in a separate cell, and a warning mark
    with a selector becomes a two-cell glyph in a one-cell slot. When the glyphs are merged,
    each is one ``Char`` that is two cells wide, and the text after it is where the pins
    will put it.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.input import DummyInput
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.layout.mouse_handlers import MouseHandlers
    from prompt_toolkit.layout.screen import Screen, WritePosition
    from prompt_toolkit.output import DummyOutput

    warn = f"{_WARN}{_VS16}"
    text = f"|{_SHRUG}|{_FAMILY}|{warn}|end"
    window = Window(ew.ClusterTextControl(lambda: ANSI(text)), always_hide_cursor=True)
    app = Application(layout=Layout(window), input=DummyInput(), output=DummyOutput())
    with set_app(app):
        screen = Screen(default_char=None, initial_width=40, initial_height=1)
        window.write_to_screen(screen, MouseHandlers(), WritePosition(0, 0, 40, 1), "", False, None)
    row = screen.data_buffer[0]

    assert (row[1].char, row[1].width) == (_SHRUG, 2)
    assert (row[4].char, row[4].width) == (_FAMILY, 2)
    assert (row[7].char, row[7].width) == (warn, 2)
    assert "".join(row[x].char for x in range(9, 13)) == "|end"
    # Nothing was lost: the row still spells the source exactly.
    assert "".join(row[x].char for x in range(40)).rstrip() == text
