# SPDX-License-Identifier: Apache-2.0
"""Tests for the path-line widget: the same words in both modes, and three overflow shapes.

The contract of the widget is exactness. Plain mode must reproduce the arrow presentation
of the app glyph for glyph. Powerline mode must interlock the chip fills with the
foreground and background of the separator. Each overflow shape must keep its budget of
cells. Thus these tests assert rendered strings and span styles, and not impressions.
"""

from __future__ import annotations

import random

import pytest
from rich.text import Text

import meshterm.ui.pathline as pathline
from meshterm.emulator import vt
from meshterm.emulator.vt import Style, Terminal
from meshterm.platforms import CARDPUTER_ZERO, PICOCALC_LYRA, set_platform
from meshterm.ui import oklab
from meshterm.ui.fontset import FONT_CODEPOINTS
from meshterm.ui.pathline import (
    _DIM_BG,
    _DIM_FG,
    _SELF_INK,
    _YOU_BG,
    CHIP_LIGHTNESS,
    CRACK_HEAD,
    CRACK_TAIL,
    CURSOR_GLYPH,
    ELIDE_HEAD,
    ELIDE_TAIL,
    PATH_INK,
    POWERLINE_ROUND_CLOSE,
    POWERLINE_ROUND_OPEN,
    POWERLINE_SEP,
    POWERLINE_THIN,
    SEAM_BLUR,
    SELF_GLYPH,
    WRAP_OFFSET,
    PathHop,
    PathLine,
    _style_hex,
    cut_mark,
    cut_to,
    elision_hop,
    hops_atom,
    path_line,
    with_action_mark,
)
from meshterm.ui.theme import DIM_TWIN, fold_text, node_style, slot_hex
from meshterm.ui.tui.render import render_to_ansi
from meshterm.ui.widgets import path_text


def _fill(key: str) -> str:
    """The fill of the chip of a node on a truecolor terminal: its hue, but darker."""
    return pathline._chip_fill(_style_hex(node_style(key)))


def _styles(text) -> dict[str, str]:  # noqa: ANN001
    """Map each styled slice of a Text to its style string, for spot checks."""
    return {text.plain[s.start : s.end]: str(s.style) for s in text.spans}


def _char_styles(text) -> list[tuple[str, str]]:  # noqa: ANN001
    """Each character, paired with its effective span style, for checks of exact parity.

    The span *boundaries* can differ between two builders (one appends a hash in two
    pieces, and the other appends it in one piece). What must be the same is the style of
    each character. Thus the comparison is for each cell, and not for each span.
    """
    styles = [""] * len(text.plain)
    for span in text.spans:
        for i in range(span.start, span.end):
            styles[i] = str(span.style)
    return list(zip(text.plain, styles, strict=True))


def test_plain_mode_matches_the_app_wide_arrow_presentation() -> None:
    """Plain mode reproduces the look of ``path_text`` of the app exactly.

    Names have their key hue, we are white, keyless hops and arrows are muted.
    """
    line = PathLine(
        [
            PathHop("Alice", key="aa"),
            PathHop("77", key="77", lit_bytes=1),
            PathHop("you", you=True),
        ],
        mode="plain",
    )
    text = line.text()
    assert text.plain == "Alice → 77 → you"
    styles = _styles(text)
    assert styles["Alice"] == node_style("aa")
    assert styles["you"] == "you"
    assert styles["77"] == node_style("77")  # the lit hash prefix has the hue


def test_plain_mode_annotation_dim_and_custom_separator() -> None:
    """Annotations, dimming, and a caller's own separator all stay in plain mode.

    The ``(3d)`` note of a trace is muted. A dim hop (and the arrow into it) fades. The own
    separator of a surface passes through without a change. An example is the ``›`` of the
    trail of the mesh walk.
    """
    line = PathLine(
        [PathHop("Hub", key="3d", annotation="3d"), PathHop("home", you=True, dim=True)],
        mode="plain",
    )
    text = line.text()
    assert text.plain == "Hub (3d) → home"
    styles = _styles(text)
    assert styles[" (3d)"] == "muted"  # the annotation note, with its space
    assert styles["home"] == "faint"
    assert styles[" → "] == "faint"  # the arrow into a dim hop fades with the hop
    trail = PathLine([PathHop("a"), PathHop("b")], mode="plain", separator=" › ").text()
    assert trail.plain == "a › b"


def test_empty_path_reads_as_the_callers_word() -> None:
    """A line with no hops is the muted empty note, in each shape."""
    assert PathLine([], mode="plain").text().plain == "direct"
    assert (
        PathLine([], mode="powerline", empty="direct — no relays").text().plain
        == "direct — no relays"
    )
    assert PathLine([], mode="plain").wrapped(40)[0].plain == "direct"


def test_chips_are_joined_by_one_interlocked_chevron() -> None:
    """A seam is one cell and not two, and the path's own end is not a seam at all.

    The point of the previous chip is laid *on* the fill of the next chip. Thus the route
    reads as a ribbon whose segments meet on a chevron. The point has a meaning: it says
    that the route continues, so a finished path never has one. It ends square on the last
    chip's own pad, and the only chevrons in the line are the joins.
    """
    alice_fill = _fill("aa")
    line = PathLine([PathHop("Alice", key="aa"), PathHop("you", you=True)], mode="powerline")
    text = line.text()
    assert text.plain == f" Alice {POWERLINE_SEP} you "
    seam_styles = [str(s.style) for s in text.spans if text.plain[s.start : s.end] == POWERLINE_SEP]
    assert seam_styles == [f"{alice_fill} on {_YOU_BG}"]


def test_chips_keep_the_same_words_and_honour_style_overrides() -> None:
    """Chips change the colours and the separators, and never the words.

    An explicit style override (hex or theme name) becomes the colour of the chip. The
    label has this colour, on a darker fill of it, as the hue of a node does.
    """
    hops = [PathHop("Hub", key="3d", annotation="3d"), PathHop("you", you=True)]
    plain = PathLine(hops, mode="plain").text().plain
    chips = PathLine(hops, mode="powerline").text().plain
    assert plain.replace(" → ", " ") == chips.replace(POWERLINE_SEP, "").replace("  ", " ").strip()
    themed = PathLine([PathHop("X", style="brand")], mode="powerline").text()
    brand = pathline._chip_fill("#5eead4")
    assert pathline._fills(themed)[-1] == brand  # the chip goes to the last cell
    assert any(str(s.style) == f"bold #5eead4 on {brand}" for s in themed.spans)
    hexed = PathLine([PathHop("X", style="bold #123456")], mode="powerline").text()
    assert pathline._fills(hexed)[-1] == pathline._chip_fill("#123456")


def test_a_chip_is_the_name_colour_on_a_darker_fill_of_it() -> None:
    """The label of a node is its hue, the colour of its name on each surface.

    The fill is the same hue, but darker (``CHIP_LIGHTNESS`` of its OKLab lightness). A hash
    label and an annotation also have the hue, and only the lit bytes are bold. Where the
    darker colour leaves the sRGB gamut, the chroma comes down and the hue stays. Thus two
    greens that are a few steps apart on the wheel keep two fills.
    """
    hue = _style_hex(node_style("3d"))
    line = PathLine([PathHop("3d63c6", key="3d", lit_bytes=1, annotation="3d")], mode="powerline")
    styles = {line.text().plain[s.start : s.end]: str(s.style) for s in line.text().spans}
    assert styles["3d"] == f"bold {hue} on {_fill('3d')}"
    assert styles["63c6"] == styles[" (3d)"] == f"{hue} on {_fill('3d')}"
    fill = oklab.from_hex(_fill("3d"))
    assert fill[0] == pytest.approx(oklab.from_hex(hue)[0] * CHIP_LIGHTNESS, abs=0.01)
    assert _fill("4c") != _fill("5c")  # a clip made both of them #008000


def test_auto_mode_follows_the_terminal_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """``auto`` renders chips only when the terminal can draw them."""
    hops = [PathHop("a"), PathHop("b")]
    monkeypatch.setattr(pathline, "powerline_enabled", lambda: True)
    assert POWERLINE_THIN in PathLine(hops).text().plain  # two keyless greys give the thin seam
    monkeypatch.setattr(pathline, "powerline_enabled", lambda: False)
    assert PathLine(hops).text().plain == "a → b"


def test_the_cardputer_draws_chips_in_glyphs_its_font_has(monkeypatch: pytest.MonkeyPatch) -> None:
    """The own font of the Cardputer has the chevrons, so its paths are chips that fold cleanly.

    MeshTerm reads its verdict from its font (:mod:`~meshterm.ui.termfont`). The render
    boundary folds each glyph that the font does not have to ``?``. Thus each mark that the
    chip language uses (both seams, both cracks, the elision, and our star) must come
    through unchanged.
    """
    monkeypatch.delenv("MESHTERM_POWERLINE")
    set_platform(CARDPUTER_ZERO)
    hops = [PathHop(SELF_GLYPH, you=True), PathHop("Hub", key="3d"), elision_hop()]
    line = PathLine([*hops, PathHop("a"), PathHop("b")]).text()  # auto: the font decides
    drawn = [line, cut_to(line, 12), cut_mark(line, 1, ELIDE_HEAD)]
    ink = "".join(text.plain for text in drawn)
    assert {POWERLINE_SEP, POWERLINE_THIN, CRACK_TAIL, CRACK_HEAD, "⋯", SELF_GLYPH} <= set(ink)
    assert fold_text(ink) == ink


def _rgb(colour: str) -> tuple[int, int, int]:
    return (int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16))


def _console_cells(row: Text) -> list[tuple[str, Style]]:
    """The cells that the PicoCalc console gets for one row, read back through a VT."""
    palette = tuple(_rgb(slot_hex(n)) for n in range(16))
    term = Terminal(53, 1, palette=palette)
    term.feed(render_to_ansi(row, 53, no_wrap=True))
    return term.screen[0]


def _selected(line: Text) -> Text:
    """``line`` on a selected row, whose base style is the bold ``cursor``."""
    row = Text(style="cursor")
    row.append("❯ ")
    row.append_text(line)
    return row


@pytest.fixture
def picocalc(monkeypatch: pytest.MonkeyPatch) -> None:
    """The PicoCalc, with the chip verdict that its own font gives."""
    monkeypatch.delenv("MESHTERM_POWERLINE")
    set_platform(PICOCALC_LYRA)


def test_the_picocalc_draws_chips_from_its_eight_backgrounds(picocalc: None) -> None:
    """The chip of a node is the dim twin of its hue, and the label is the hue itself.

    The VT draws a background only from slots 0 to 7. Thus each fill is a dim slot, and each
    label is the bright slot that the name has in arrow mode. A keyless chip is white on
    light grey. Our star and a faded hop stand on the page, because the console has no dark
    grey background. The row is a selected row. Its ``bold`` base style must not move a dim
    fill to its bright twin, so each chip cell arrives without bold.
    """
    hops = [
        PathHop(SELF_GLYPH, you=True),
        PathHop("Hilltop", key="1c"),
        PathHop("Alice", key="0a", annotation="0a"),
        PathHop("3d"),
        PathHop("Ridge", key="1c", dim=True),
        PathHop(SELF_GLYPH, you=True),
    ]
    line = PathLine(hops).text()  # auto: the console font has the chevrons
    assert POWERLINE_SEP in line.plain
    cells = _console_cells(_selected(line))
    text = "".join(char for char, _ in cells)
    dim_slots = {_rgb(slot_hex(n)) for n in range(8)}
    for char, style in cells[2 : 2 + len(line.plain)]:
        assert style.bg is None or style.bg in dim_slots, f"{char!r} on {style.bg}"
        assert not style.flags & vt.BOLD, f"{char!r} is bold, so a dim slot turns bright"

    def at(word: str) -> Style:
        return cells[text.index(word)][1]

    yellow, red = _style_hex(node_style("1c")), _style_hex(node_style("0a"))
    assert (at("Hilltop").fg, at("Hilltop").bg) == (_rgb(yellow), _rgb(DIM_TWIN[yellow]))
    assert (at("Alice").fg, at("Alice").bg) == (_rgb(red), _rgb(DIM_TWIN[red]))
    assert at("(0a)").fg == _rgb(red)  # the annotation also has the colour of the label
    assert (at("3d").fg, at("3d").bg) == (_rgb(slot_hex(15)), _rgb(slot_hex(7)))
    assert at("Ridge").bg in (None, _rgb(slot_hex(0)))  # faded: on the page
    assert at("Ridge").fg == _rgb(slot_hex(8))
    assert at(SELF_GLYPH).bg in (None, _rgb(slot_hex(0)))


def test_a_picocalc_hash_label_is_one_colour(picocalc: None) -> None:
    """The rest of a hash has the hue of its lit bytes, and not a grey.

    A grey after the lit byte was not readable on the green and the cyan fills (JP,
    2026-10-06).
    """
    for key in ("0a", "1c", "44", "70", "9a", "c4"):
        line = PathLine([PathHop(f"{key}63c6", key=key, lit_bytes=1)]).text()
        cells = _console_cells(line)
        hue = _rgb(_style_hex(node_style(key)))
        label = [style.fg for char, style in cells if char in f"{key}63c6" and char != " "]
        assert label == [hue] * 6, key


def test_picocalc_seams_on_one_colour_and_the_composer_slot(picocalc: None) -> None:
    """Two chips of one colour join on a thin chevron in the hue. The slot is a break.

    The thin chevron is the bright twin of the fill: the colour of the label on the chip.
    The slot of the composer has no white background on the console. Thus it is a white
    ``+`` on the page, and the chips on each side close on a point and open on a notch.
    """
    same = PathLine([PathHop("Mill", key="1a"), PathHop("Tower", key="20")]).text()
    seam = same.plain.index(POWERLINE_THIN)
    cell = _console_cells(same)[seam][1]
    yellow = _style_hex(node_style("1a"))
    assert (cell.fg, cell.bg) == (_rgb(yellow), _rgb(DIM_TWIN[yellow]))

    slot = [
        PathHop("Hilltop", key="1c"),
        PathHop(CURSOR_GLYPH, cursor=True),
        PathHop("Alice", key="0a"),
    ]
    line = PathLine(slot).text()
    cells = _console_cells(line)
    plus = cells[line.plain.index(CURSOR_GLYPH)][1]
    assert plus.fg == _rgb(slot_hex(15)) and plus.bg in (None, _rgb(slot_hex(0)))
    assert line.plain.count(POWERLINE_SEP) == 2  # a point into the break, and a notch out of it


def test_every_picocalc_chip_glyph_is_in_its_font(picocalc: None) -> None:
    """Each mark of the chip language is in the console font, so nothing folds to ``?``."""
    hops = [PathHop(SELF_GLYPH, you=True), PathHop("Hilltop", key="1c"), elision_hop()]
    line = PathLine([*hops, PathHop("a"), PathHop("b")]).text()
    drawn = [line, cut_to(line, 12), cut_mark(line, 1, ELIDE_HEAD)]
    ink = "".join(text.plain for text in drawn)
    assert {POWERLINE_SEP, POWERLINE_THIN, CRACK_TAIL, CRACK_HEAD, SELF_GLYPH} <= set(ink)
    assert all(ord(char) in FONT_CODEPOINTS for char in ink)
    assert fold_text(ink) == ink


def test_the_cursor_is_a_hop_of_its_own_in_either_mode() -> None:
    """The insertion point of the composer is a slot *in* the route, and not a gap between hops.

    It renders as a hop, so chips stay chips and arrows stay arrows.
    """
    hops = [PathHop("a"), PathHop(CURSOR_GLYPH, cursor=True), PathHop("b")]
    plain = PathLine(hops, mode="plain").text()
    assert plain.plain == f"a → {CURSOR_GLYPH} → b"
    assert _styles(plain)[CURSOR_GLYPH] == "selected"  # the block in reverse video

    chips = PathLine(hops, mode="powerline").text()
    assert chips.plain == f" a {POWERLINE_SEP} {CURSOR_GLYPH} {POWERLINE_SEP} b "
    slot = next(s for s in chips.spans if chips.plain[s.start : s.end] == CURSOR_GLYPH)
    assert str(slot.style).endswith(f"on {_style_hex('cursor')}")  # the fill in the cursor white


def test_ellipsized_elides_the_middle_and_keeps_both_endpoints() -> None:
    """The fit removes the middle hops behind ``⋯`` and keeps both endpoints.

    A route reads the origin and the destination first. A truncation at the right edge
    removes the tail, and the tail is what must stay.
    """
    hops = [PathHop(label) for label in ("AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF")]
    line = PathLine(hops, mode="plain")
    assert line.ellipsized(200).plain == line.text().plain  # it fits, so it is unchanged
    fitted = line.ellipsized(25)
    assert fitted.cell_len <= 25
    assert fitted.plain.startswith("AAAA")
    assert fitted.plain.endswith("FFFF")
    assert "⋯" in fitted.plain


def test_ellipsized_eats_the_head_when_told_to() -> None:
    """``ELIDE_HEAD`` does not spare the origin, and it keeps the tail instead.

    The news of a breadcrumb is where the walk *is*. Thus the fit keeps as much of the tail
    as the width holds, behind a ``⋯``.
    """
    hops = [PathHop(label) for label in ("AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF")]
    line = PathLine(hops, mode="plain")
    fits = line.ellipsized(200, elide=ELIDE_HEAD).plain
    assert fits == line.text().plain  # it fits, so it is unchanged for each side
    fitted = line.ellipsized(25, elide=ELIDE_HEAD)
    assert fitted.cell_len <= 25
    assert fitted.plain.startswith("⋯")  # the origin also goes, unlike the fit on the tail side
    assert fitted.plain.endswith("FFFF")
    assert "AAAA" not in fitted.plain


def test_ellipsized_last_resort_truncates_a_single_giant_hop() -> None:
    """When even ``⋯ → last`` overflows, the classic truncation with an ellipsis is used."""
    line = PathLine([PathHop("AAAAAAAAAA"), PathHop("BBBBBBBBBB")], mode="plain")
    fitted = line.ellipsized(8)
    assert fitted.cell_len <= 8
    assert fitted.plain.endswith("…")


def _chips() -> PathLine:
    """Three keyed hops in chip mode. Each fill is a different hue from a hash."""
    return PathLine(
        [PathHop("AAAA", key="11aa"), PathHop("BBBB", key="22bb"), PathHop("CCCC", key="33cc")],
        mode="powerline",
    )


def test_cut_to_cracks_a_chip_in_its_own_fill_instead_of_ellipsizing() -> None:
    """A chip that the cut catches breaks off on a half block in its own fill.

    The crack reads as a segment that continues. The ``…`` claims that a word was made
    shorter.
    """
    full = _chips().text()
    fitted = cut_to(full, 14)
    assert fitted.cell_len == 14
    assert fitted.plain.endswith(CRACK_TAIL)
    assert "…" not in fitted.plain
    # The cut lands inside ``BBBB``, so the crack has the hue of BBBB, and not the hue of
    # its neighbours.
    assert str(fitted.spans[-1].style) == _fill("22bb")
    assert cut_to(full, 200) is full  # it fits, so there is no change, no copy, and no mark


def test_cut_to_leaves_arrow_lines_on_the_ellipsis() -> None:
    """Only chips crack, and an arrow line keeps the ellipsis.

    It has no fill to shear. A shorter text is what happened, and the classic mark says so.
    """
    fitted = cut_to(PathLine(_chips().hops, mode="plain").text(), 10)
    assert fitted.cell_len <= 10
    assert fitted.plain.endswith("…")
    assert CRACK_TAIL not in fitted.plain


def test_cut_mark_mirrors_itself_and_reads_the_visible_side() -> None:
    """The head mark mirrors the tail mark, and each takes the fill of the nearest drawn cell.

    This means a scan back for a tail cut, and a scan forward for a head cut.
    """
    full = _chips().text()
    body = full.plain.index("AAAA")  # inside the first chip, in both scan directions
    head, tail = cut_mark(full, body, ELIDE_HEAD), cut_mark(full, body, ELIDE_TAIL)
    assert (head.plain, tail.plain) == (CRACK_HEAD, CRACK_TAIL)
    assert str(head.style) == str(tail.style) == _fill("11aa")

    # A cut that lands on the interlocked seam itself cracks in the field that the cell
    # has: the chip *ahead*, whose fill is the background of the seam. Thus the mark reads
    # as the next segment that begins and is sheared, and not as a colour that is not
    # in the line.
    seam = full.plain.index(POWERLINE_SEP)
    assert str(cut_mark(full, seam, ELIDE_TAIL).style) == _fill("22bb")


def test_cut_mark_falls_back_to_the_ellipsis_off_a_chip() -> None:
    """Off a chip, the same question gives the muted ``…``.

    The fallback is the whole test of the mode. Thus no caller must know which mode drew the
    line.
    """
    arrows = PathLine(_chips().hops, mode="plain").text()
    for side in (ELIDE_HEAD, ELIDE_TAIL):
        mark = cut_mark(arrows, 6, side)
        assert mark.plain == "…"
        assert str(mark.style) == "muted"


def test_the_elision_sits_between_chips_rather_than_being_one() -> None:
    """Hops that MeshTerm removed from a path are a break *in* the ribbon, not a node ``⋯``.

    The chip before the break closes onto the page. The mark is bare there, with no fill and
    no padding. The chip after the break opens on its notch. The whole gap costs three
    cells. A filled ``⋯`` chip with its pads and two seams costs seven cells, which is four
    more cells of route on a line that by definition has little room.
    """
    line = PathLine([PathHop(f"NODE{i:02d}", key=f"{i:02x}aa") for i in range(5)], mode="powerline")
    fitted = line.ellipsized(30)
    assert fitted.cell_len <= 30
    at = fitted.plain.index("⋯")
    assert fitted.plain[at - 1] == POWERLINE_SEP and fitted.plain[at + 1] == POWERLINE_SEP
    styles = {fitted.plain[s.start : s.end]: str(s.style) for s in fitted.spans}
    assert styles["⋯"] == "faint"  # bare on the page: no ``on`` fill, and no pads

    # Arrow mode needs no special case, because a bare label between two arrows *is* a gap.
    plain = PathLine(line.hops, mode="plain").ellipsized(30)
    assert " ⋯ " in plain.plain and "→ ⋯ →" in plain.plain


def test_elision_hop_is_the_one_definition_both_surfaces_share() -> None:
    """One elision definition serves both surfaces.

    The widget and the trail of the mesh walk elide with the same mark *and* the same gap
    semantics. Thus a head that the widget hid reads as a tail that the scroll hid.
    """
    mark = elision_hop()
    assert (mark.label, mark.gap, mark.dim) == ("⋯", True, True)


def test_wrapped_cracks_an_over_wide_lone_chip() -> None:
    """A chip that is wider than the content column is cracked and not folded.

    This is the one place where a chip line is cut in the middle of a hop. The hop stands
    alone, cracked, and still inside the budget.
    """
    lines = PathLine([PathHop("N" * 40, key="11aa")], mode="powerline").wrapped(20)
    assert len(lines) == 1
    assert lines[0].cell_len <= 20
    assert lines[0].plain.endswith(CRACK_TAIL)


def test_wrapped_breaks_at_hops_under_a_hanging_indent() -> None:
    """A wrapped plain path breaks at hops, under a hanging indent.

    Lines that continue end with the ``→`` cue. Continuations hang at the indent, stepped in
    by :data:`WRAP_OFFSET`. Each line keeps the full width.
    """
    hops = [PathHop(label) for label in ("AAAA", "BBBB", "CCCC", "DDDD")]
    lines = PathLine(hops, mode="plain").wrapped(16, indent=2)
    assert [line.plain for line in lines] == ["AAAA → BBBB →", "    CCCC → DDDD"]
    assert lines[1].plain.startswith(" " * (2 + WRAP_OFFSET))
    assert all(line.cell_len <= 16 for line in lines)


def test_wrapped_evens_the_lines_instead_of_widowing_the_tail() -> None:
    """A hop that misses the first line by a cell is not left alone below it.

    Greedy packing puts ``us → a → b →`` on the first line and leaves ``us`` as a widow. The
    fold takes the same number of lines in both cases, so it spreads the hops across them.
    """
    hops = [PathHop(label) for label in ("us", "alpha", "bravo", "us")]
    lines = PathLine(hops, mode="plain").wrapped(20, indent=0)
    assert [line.plain for line in lines] == ["us → alpha →", "  bravo → us"]


def test_wrapped_folds_a_faded_return_leg_at_its_turn() -> None:
    """A boomerang breaks where it turns: the composed leg is above, and its echo is below.

    The dimmed hops are the mirrored return. A fold there gives the wrapped route a shape,
    and not an arbitrary seam in the middle of a leg. MeshTerm takes it only when it costs
    nothing. Here the fold at the turn costs no more lines than the greedy break.
    """
    out = [PathHop(n) for n in ("me", "north-relay", "east-relay", "dest")]
    back = [PathHop(n, dim=True) for n in ("east-relay", "north-relay", "me")]
    lines = PathLine(out + back, mode="plain").wrapped(40, indent=0)
    assert [line.plain for line in lines] == [
        "me → north-relay → east-relay → dest →",  # out, ending on the target
        "  east-relay → north-relay → me",  # and the mirror by which it comes home
    ]


def test_wrapped_folds_a_walked_boomerang_at_its_turn_too() -> None:
    """A trace that answered dims nothing, but its route is still its own mirror.

    Thus MeshTerm finds the turn from the sequence of hops itself. The walk folds in the
    same place as the plan that armed it. One route has one shape.
    """
    walked = [PathHop(n) for n in ("me", "north-relay", "east-relay", "dest")]
    walked += [PathHop(n) for n in ("east-relay", "north-relay", "me")]
    lines = PathLine(walked, mode="plain").wrapped(46, indent=0)
    assert [line.plain for line in lines] == [
        "me → north-relay → east-relay → dest →",
        "  east-relay → north-relay → me",
    ]


def test_wrapped_never_folds_before_a_lone_faded_landing() -> None:
    """A path whose only dim hop is the automatic return home keeps it on a real line.

    Walks in path mode dim only that last ``us``. A fold at "the fade" there leaves it as a
    widow. Thus the seam needs two hops on each side to count as a turn.
    """
    hops = [PathHop(label) for label in ("us", "alpha", "bravo", "charlie")]
    hops.append(PathHop("us", dim=True))
    lines = PathLine(hops, mode="plain").wrapped(26, indent=0)
    assert len(lines) == 2
    assert lines[-1].plain != "us"
    assert lines[-1].plain.endswith("→ us")


def test_wrapped_lines_always_fit_their_width() -> None:
    """No mix of hops, mode, width, or indent gives a line that is past the budget.

    MeshTerm fits lines with arithmetic from the measurement of each hop, and does not
    render each candidate group. Thus this test sweeps that model against what MeshTerm
    really draws. The two can differ for lone hops that are too wide (truncated, with the
    cue), and for chip lines (which have a closing edge that an arrow line does not have).
    """
    rng = random.Random(7)
    for _ in range(200):
        hops = [
            PathHop(
                "N" * rng.randint(1, 16),
                key=f"{i:02x}aa",
                annotation=f"{i:02x}" if rng.random() < 0.5 else None,
                dim=rng.random() < 0.3,
                you=rng.random() < 0.2,
                lit_bytes=rng.choice((0, 1, 2)),
            )
            for i in range(rng.randint(1, 9))
        ]
        for mode in ("plain", "powerline"):
            for width, indent in ((20, 0), (34, 2), (47, 7), (72, 16)):
                lines = PathLine(hops, mode=mode).wrapped(width, indent=indent)
                assert all(line.cell_len <= width for line in lines)


def test_wrapped_continuation_cue_follows_the_separator() -> None:
    """The continuation cue is the separator's own mark, and it is not always an arrow.

    A line that commas join (the wire spec of the trace screen) continues on a comma. Thus
    the spec stays exactly as it is.
    """
    hops = [PathHop(label) for label in ("3d63ab99", "7f21cd01", "27aa1122")]
    lines = PathLine(hops, mode="plain", separator=",").wrapped(20, indent=2)
    assert lines[0].plain.endswith(",")
    assert "→" not in "".join(line.plain for line in lines)
    assert "".join(line.plain.strip() for line in lines) == "3d63ab99,7f21cd01,27aa1122"


def test_path_line_factory_matches_path_text_character_for_character() -> None:
    """``path_line`` renders the trace form in the same way as ``path_text`` did.

    This is the migration bridge. It has device endpoints with annotated hashes, a named
    hop, an unnamed hop with a lit prefix, and a dimmed tail. The characters and the styles
    are the same.
    """
    names = {"aa11bb": "Alice", "3d63ab": "Hub"}
    hops = [None, "aa11bb", "77ccddee", "3d63ab", None]
    kwargs = dict(
        prefix_bytes=2,
        self_name="Me",
        show_hash=True,
        hash_bytes=3,
        device_hash="A1B2C3D4",
        dim_from=3,
    )
    old = path_text(hops, lambda h: names.get(h, h), **kwargs)
    new = path_line(hops, lambda h: names.get(h, h), mode="plain", **kwargs).text()
    assert _char_styles(new) == _char_styles(old)


def test_path_line_factory_matches_path_text_hash_as_name_flavour() -> None:
    """The form for message paths also matches ``path_text``.

    An unnamed hop is its own muted identity hash, with the byte by which it is addressed.
    The guarantee of parity is the same.
    """
    names = {"3d63abcdef00": "Hub"}
    hops = ["3d63abcdef00", "e839f2aabb11"]
    kwargs = dict(prefix_bytes=3, show_hash=True, hash_bytes=1, hash_as_name=True)
    old = path_text(hops, lambda h: names.get(h, h), **kwargs)
    new = path_line(hops, lambda h: names.get(h, h), mode="plain", **kwargs).text()
    assert _char_styles(new) == _char_styles(old)
    assert path_line([], lambda h: h).text().plain == path_text([], lambda h: h).plain


def test_path_line_factory_splices_the_cursor_slot_at_a_hop_index() -> None:
    """``cursor`` inserts the insertion slot at a hop index.

    It names the position that a chosen hop takes. MeshTerm counts it over the rendered hops
    and applies it after them, so the slot never changes what its neighbours show.
    """
    hops = [None, "aa11bb", "3d63ab", None]

    def built(cursor) -> str:  # noqa: ANN001
        line = path_line(hops, self_name="Me", bare_self=True, mode="plain", cursor=cursor)
        return line.text().plain

    assert built(None) == f"{SELF_GLYPH} → aa11bb → 3d63ab → {SELF_GLYPH}"
    assert built(0) == f"{CURSOR_GLYPH} → {SELF_GLYPH} → aa11bb → 3d63ab → {SELF_GLYPH}"
    assert built(2) == f"{SELF_GLYPH} → aa11bb → {CURSOR_GLYPH} → 3d63ab → {SELF_GLYPH}"
    assert built(99).endswith(f"{SELF_GLYPH} → {CURSOR_GLYPH}")  # limited to the tail


def test_wrapped_carries_the_cursor_like_any_other_hop() -> None:
    """A folded route cannot strand its insertion point.

    The slot is a hop, so it lands on a line by itself, in any position and in any mode.
    """
    labels = ("AAAA", "BBBB", "CCCC", "DDDD")
    for at in range(len(labels) + 1):
        hops = [PathHop(label) for label in labels]
        hops.insert(at, PathHop(CURSOR_GLYPH, cursor=True))
        lines = PathLine(hops, mode="plain").wrapped(16, indent=2)
        assert sum(text.plain.count(CURSOR_GLYPH) for text in lines) == 1
        assert all(text.cell_len <= 16 for text in lines)
        carried = next(t for t in lines if CURSOR_GLYPH in t.plain)
        assert any(str(s.style) == "selected" for s in carried.spans)


def test_wrapped_chip_lines_open_on_the_break_they_continue() -> None:
    """Chip wrapping never splits a chip, and each line opens on the break that it continues.

    Each line that the path *outruns* ends on the pointed edge. The last line ends square,
    because the route is finished. Each line *but the first* opens on the other half of the
    break. This is the point that is notched out of its own fill in reverse video. Thus the
    page shows through, and no trace of the line above bleeds down. The point always faces
    the direction in which the path flows.
    """
    hops = [
        PathHop(label, key=key)
        for label, key in (("AAAA", "aa"), ("BBBB", "77"), ("CCCC", "3d"), ("DDDD", "f2"))
    ]
    lines = PathLine(hops, mode="powerline").wrapped(20, indent=2)
    assert len(lines) == 2
    assert lines[0].plain.startswith(" AAAA")  # the square edge: this is the start
    step = 2 + WRAP_OFFSET
    assert lines[1].plain[:step].isspace() and lines[1].plain[step] == POWERLINE_SEP
    # …past the line's own PATH_INK stamp, which covers the whole body from the same
    # cell (it marks the run as a path line for the cursor fold, and it draws nothing).
    carried = next(s for s in lines[1].spans if s.start == step and str(s.style) != PATH_INK)
    assert str(carried.style) == f"{_fill('3d')} reverse"
    assert lines[0].plain.endswith(POWERLINE_SEP)  # …the path runs on past this line
    assert lines[-1].plain.endswith(" ")  # …and stops square on the pad of the last chip
    assert all(line.cell_len <= 20 for line in lines)


def test_rounded_caps_finish_a_path_only_where_the_font_has_them(monkeypatch) -> None:  # noqa: ANN001
    """Rounded caps finish a path only where the font really has them.

    A full Nerd Font rounds the two *outer* ends of the path into a lozenge. A terminal that
    has only the core glyphs makes *both* of them square. It appends nothing, and the
    own pads of the outer chips are the edges. It never draws tofu. Interior breaks stay angled
    in both cases. A rounded end reads as the path that stops. A pointed end at the finish
    reads as the opposite: a route that is cut off in the middle of a walk.
    """
    hops = [
        PathHop(label, key=key)
        for label, key in (("AAAA", "aa"), ("BBBB", "77"), ("CCCC", "3d"), ("DDDD", "f2"))
    ]
    line = PathLine(hops, mode="powerline")

    monkeypatch.setattr(pathline, "powerline_full", lambda: False)
    assert line.text().plain.startswith(" AAAA")
    assert line.text().plain.endswith("DDDD ")
    # The point is still the end of a *wrapped* line, because there the route really goes on.
    squared = line.wrapped(20, indent=2)
    assert squared[0].plain.endswith(POWERLINE_SEP) and squared[-1].plain.endswith("DDDD ")

    monkeypatch.setattr(pathline, "powerline_full", lambda: True)
    assert line.text().plain.startswith(POWERLINE_ROUND_OPEN + " AAAA")
    assert line.text().plain.endswith(POWERLINE_ROUND_CLOSE)
    wrapped = line.wrapped(20, indent=2)
    assert len(wrapped) == 2
    assert wrapped[0].plain.startswith(POWERLINE_ROUND_OPEN)  # the path opens here…
    assert wrapped[0].plain.endswith(POWERLINE_SEP)  # …but does not end here
    assert wrapped[1].plain[2 + WRAP_OFFSET] == POWERLINE_SEP  # it continues mid-path…
    assert wrapped[1].plain.endswith(POWERLINE_ROUND_CLOSE)  # …and it closes
    assert all(text.cell_len <= 20 for text in wrapped)


def test_a_line_drawing_only_a_route_middle_wears_the_chevron_at_that_end(monkeypatch) -> None:  # noqa: ANN001
    """A chain of relays has no endpoints of its own, so neither end can claim to be one.

    The ``via`` field of a packet names the nodes that forwarded it. It does not name the
    node that it came from, and it does not name the node that received it. A flat end there
    says that the first relay *is* where the route began. Thus the line opens on the notch
    and closes on the point instead. This is the same mark "there is more of this out
    there" that a wrapped line uses. The flags give up the cap. A full Nerd Font rounds only
    the ends that are real.
    """
    hops = [PathHop(label, key=key) for label, key in (("AAAA", "aa"), ("BBBB", "77"))]
    middle = PathLine(hops, mode="powerline", from_origin=False, to_destination=False)

    monkeypatch.setattr(pathline, "powerline_full", lambda: True)
    text = middle.text()
    assert text.plain.startswith(POWERLINE_SEP + " AAAA")  # the notch, and not the lozenge
    assert text.plain.endswith("BBBB " + POWERLINE_SEP)  # the point: the route goes on
    opening = next(s for s in text.spans if s.start == 0 and str(s.style) != PATH_INK)
    assert str(opening.style) == f"{_fill('aa')} reverse"  # cut out of AAAA

    # One end at a time: the half that *is* an endpoint keeps its cap in both cases.
    head = PathLine(hops, mode="powerline", to_destination=False).text().plain
    assert head.startswith(POWERLINE_ROUND_OPEN) and head.endswith(POWERLINE_SEP)
    tail = PathLine(hops, mode="powerline", from_origin=False).text().plain
    assert tail.startswith(POWERLINE_SEP) and tail.endswith(POWERLINE_ROUND_CLOSE)

    monkeypatch.setattr(pathline, "powerline_full", lambda: False)
    squared = PathLine(hops, mode="powerline").text().plain  # the whole route, for the contrast
    assert squared.startswith(" AAAA") and squared.endswith("BBBB ")
    assert middle.text().plain.startswith(POWERLINE_SEP)  # …the chevron is not the cap
    assert middle.text().plain.endswith(POWERLINE_SEP)


def test_arrow_mode_says_a_route_middle_with_the_separator_it_joins_with() -> None:
    """A console has no chevrons to shear, so the arrow itself makes the same claim.

    There is a leading ``→`` where the origin is not drawn, and a trailing ``→`` where the
    destination is not drawn. This is the mark with which arrow mode already spells "the
    path goes on" when a line wraps. Both modes tell the user the same thing.
    """
    hops = [PathHop(label, key=key) for label, key in (("AAAA", "aa"), ("BBBB", "77"))]
    assert PathLine(hops, mode="plain").text().plain == "AAAA → BBBB"
    middle = PathLine(hops, mode="plain", from_origin=False, to_destination=False)
    assert middle.text().plain == "→ AAAA → BBBB →"
    assert PathLine(hops, mode="plain", from_origin=False).text().plain == "→ AAAA → BBBB"
    assert PathLine(hops, mode="plain", to_destination=False).text().plain == "AAAA → BBBB →"


def test_a_wrapped_route_middle_pays_for_both_its_chevrons() -> None:
    """MeshTerm measures the marks, and does not append them past the budget, in both modes.

    A line with only relays uses a cell at each outer end. A whole route uses this cell
    only where the font has caps. Thus the fit must know about the cells. Also, the wrap
    flags and the endpoint flags must not both draw at the same break.
    """
    hops = [
        PathHop(label, key=key)
        for label, key in (("AAAA", "aa"), ("BBBB", "77"), ("CCCC", "3d"), ("DDDD", "f2"))
    ]
    lines = PathLine(hops, mode="powerline", from_origin=False, to_destination=False).wrapped(
        20, indent=2
    )
    assert len(lines) == 2
    assert all(line.cell_len <= 20 for line in lines)
    assert lines[0].plain.startswith(POWERLINE_SEP)  # opens in the middle of the route…
    assert lines[0].plain.endswith(POWERLINE_SEP)  # …and the fold goes on
    assert lines[-1].plain.endswith(POWERLINE_SEP)  # …as does the route past the last chip
    # Never both marks at one break. A fold that is also the head or the tail of the drawn
    # route still draws one chevron there, and not two.
    assert POWERLINE_SEP * 2 not in "".join(line.plain for line in lines)

    folded = PathLine(hops, mode="plain", from_origin=False, to_destination=False).wrapped(
        22, indent=2
    )
    assert len(folded) > 1 and all(line.cell_len <= 22 for line in folded)
    assert folded[0].plain.startswith("→ AAAA")  # the head mark, one time
    assert folded[-1].plain.rstrip().endswith("→")
    assert not folded[-1].plain.strip().startswith("→")  # a continuation opens on its hop


def _seams(*hops: PathHop) -> list[tuple[str, str]]:
    """Each seam cell of a chip line as ``(glyph, style)``, in order."""
    text = PathLine(list(hops), mode="powerline").text()
    return [
        (text.plain[s.start], str(s.style))
        for s in text.spans
        if text.plain[s.start] in (POWERLINE_SEP, POWERLINE_THIN)
    ]


def test_a_seam_between_two_of_one_colour_is_the_thin_chevron_in_its_label() -> None:
    """Where two chips have the same fill, the seam is the thin chevron in the label colour.

    The interlock cannot draw a solid point in its own background. Thus MeshTerm draws the
    join *on* the shared fill instead (powerline's own mark for a join inside one
    colour), and the ribbon continues without a break. Its colour is the label of the
    previous chip. It is the light half of the chip's own pair, so it is visible on the
    fill, and nothing on the line has a colour that is not a colour of a chip. The faded
    slate draws it in its muted text.

    This is not a rare accident. A mirrored return leg is a run of hops that are identically
    faded, by construction, and a stretch of keyless hops has one grey. In both cases the
    seam stays a single cell. The fallback changes the interlock for the outline, and does
    not change the width.
    """
    hue = _style_hex(node_style("aa"))
    glyph, style = _seams(PathHop("A", key="aa"), PathHop("B", key="aa"))[0]
    assert (glyph, style) == (POWERLINE_THIN, f"{hue} on {_fill('aa')}")

    assert _seams(PathHop("A", key="aa"), PathHop("B", key="77"))[0] == (
        POWERLINE_SEP,
        f"{_fill('aa')} on {_fill('77')}",
    )  # a blend, one cell

    glyph, style = _seams(PathHop("A", dim=True), PathHop("B", dim=True))[0]
    assert (glyph, style) == (POWERLINE_THIN, f"{_DIM_FG} on {_DIM_BG}")


def test_a_seam_blurs_by_perceived_distance_not_by_hue_gap() -> None:
    """Two fills that are less than ``SEAM_BLUR`` apart to the eye blur, as an exact match does.

    The eye decides, and not the wheel. Sixteen steps of the first byte across the greens
    are one colour to a user. Sixteen steps across the reds are two colours. Thus the same
    gap of hue blurs on one side of the wheel and interlocks on the other. The test measures
    the fills, because the solid point is a fill on a fill. The greys follow the same metric
    with no exception. The keyless grey and the faded slate are far apart, and they keep
    their seam.
    """
    green = _seams(PathHop("A", key="4c"), PathHop("B", key="5c"))[0]
    red = _seams(PathHop("A", key="00"), PathHop("B", key="10"))[0]
    assert green[0] == POWERLINE_THIN and red[0] == POWERLINE_SEP
    assert oklab.distance(_fill("4c"), _fill("5c")) < SEAM_BLUR
    assert oklab.distance(_fill("00"), _fill("10")) > SEAM_BLUR
    assert green[1] == f"{_style_hex(node_style('4c'))} on {_fill('5c')}"

    wrapped = _seams(PathHop("A", key="fc"), PathHop("B", key="02"))[0]
    assert wrapped[0] == POWERLINE_THIN  # neighbours across the seam of the wheel, also

    glyph, style = _seams(PathHop("A"), PathHop("B", dim=True))[0]
    assert glyph == POWERLINE_SEP and " on " in style  # two greys, which still interlock


def test_bare_self_stands_us_on_a_star_and_fades_both_ends() -> None:
    """``bare_self`` puts a ★ for us at both ends, and fades both.

    To set out from us and to land back on us are fixtures of the route and not choices.
    Thus they have the same automatic grey as a mirrored return leg. In chips, they have the
    same dark slate, and not the loud white of you.
    """
    hops = [None, *(f"{i:02x}aa" for i in range(6)), None]
    line = path_line(
        hops,
        prefix_bytes=2,
        self_name="Me",
        show_hash=True,
        device_hash="a1b2",
        bare_self=True,
        mode="plain",
    )
    text = line.text()
    body = " → ".join(f"{i:02x}aa" for i in range(6))
    assert text.plain == f"{SELF_GLYPH} → {body} → {SELF_GLYPH}"
    assert "Me" not in text.plain and "a1b2" not in text.plain
    stars = [s for s in text.spans if text.plain[s.start : s.end] == SELF_GLYPH]
    assert [str(s.style) for s in stars] == ["faint", "faint"]
    chips = PathLine(line.hops, mode="powerline").text()
    star_fills = [str(s.style) for s in chips.spans if chips.plain[s.start : s.end] == SELF_GLYPH]
    assert all("#ffffff" not in fill for fill in star_fills)  # never the white of you

    wrapped = path_line(
        hops, prefix_bytes=2, self_name="Me", bare_self=True, mode="powerline"
    ).wrapped(28, indent=2)
    assert len(wrapped) > 1
    assert all(text.plain[2 + WRAP_OFFSET] == POWERLINE_SEP for text in wrapped[1:])
    assert all(text.cell_len <= 28 for text in wrapped)

    lines = path_line(
        hops, prefix_bytes=2, self_name="Me", bare_self=True, mode="powerline"
    ).wrapped(28, indent=2)
    assert len(lines) > 1
    assert all(line.plain[2 + WRAP_OFFSET] == POWERLINE_SEP for line in lines[1:])
    assert all(line.cell_len <= 28 for line in lines)


def test_bare_self_keeps_us_in_the_you_white_where_nothing_is_composed() -> None:
    """``dim_self=False`` makes the stars not muted: the fade means "not for you to compose".

    A surface that only *shows* a walk (the boards and the record card of the Trophy case)
    composes nothing. Thus there is no "not yours" to say, and our node reads in the ``you``
    white of the app. This is the same identity colour that it has in other places. A
    ``dim_from`` that reaches a star still fades it, so a mirrored return leg keeps its grey
    in both cases.
    """
    hops = [None, *(f"{i:02x}aa" for i in range(4)), None]
    text = path_line(
        hops, prefix_bytes=2, self_name="Me", bare_self=True, dim_self=False, mode="plain"
    ).text()
    stars = [s for s in text.spans if text.plain[s.start : s.end] == SELF_GLYPH]
    assert [str(s.style) for s in stars] == ["you", "you"]
    # In chips the same identity is the yellow star of the map on the neutral dark grey. It
    # is the marker itself and not a hue, because our end is a fixture and not a node that
    # the user must tell from other nodes.
    chips = path_line(
        hops, prefix_bytes=2, self_name="Me", bare_self=True, dim_self=False, mode="powerline"
    ).text()
    fills = [str(s.style) for s in chips.spans if chips.plain[s.start : s.end] == SELF_GLYPH]
    assert fills == [f"bold {_SELF_INK} on {_YOU_BG}"] * 2

    # The explicit fade still wins: a star inside a dimmed return leg stays grey.
    faded = path_line(
        hops,
        prefix_bytes=2,
        self_name="Me",
        bare_self=True,
        dim_self=False,
        dim_from=3,
        mode="plain",
    ).text()
    tail = [s for s in faded.spans if faded.plain[s.start : s.end] == SELF_GLYPH]
    assert [str(s.style) for s in tail] == ["you", "faint"]


def test_action_mark_rides_outside_the_width_budget() -> None:
    """The ``…`` for "opens further" costs the path no cells. It takes what is left, or nothing.

    When the function kept room for it first, a route that filled its lane exactly cracked
    on its last chip (JP, 2026-08-10). The row claimed that the walk ran on, when the walk
    was finished, and the two cells that the hint needed were two cells that the user lost.
    Thus MeshTerm fits the path first, and the mark lands in the space that is left.
    """
    hops = [None, "3d63", "f2a1", None]
    line = path_line(
        hops, prefix_bytes=2, self_name="Me", bare_self=True, dim_self=False, mode="plain"
    ).text()
    exact = line.cell_len

    # A lane that the route fills exactly: the route stays whole, and the mark goes.
    fitted = cut_to(line, exact, action=True)
    assert fitted.plain == line.plain
    assert fitted.cell_len == exact

    # One cell of slack is still not enough for " …". The mark is two cells, all or nothing.
    assert cut_to(line, exact + 1, action=True).plain == line.plain

    # Room for both: the mark appears, and the whole line still fits the lane.
    roomy = cut_to(line, exact + 2, action=True)
    assert roomy.plain == line.plain + " …"
    assert roomy.cell_len <= exact + 2

    # A route that really overflows is cut as always, and then it has no room that is left.
    cut = cut_to(line, exact - 4, action=True)
    assert cut.cell_len == exact - 4 and not cut.plain.endswith(" …")

    # The mark never makes a line wider than its lane, for any input.
    assert with_action_mark(line, exact - 1).plain == line.plain


def test_action_mark_leaves_a_chip_path_closed_rather_than_cracked() -> None:
    """In chips, this is the whole purpose: a route that fits keeps its closing cap, not a crack."""
    hops = [None, "3d63", "f2a1", None]
    line = path_line(
        hops, prefix_bytes=2, self_name="Me", bare_self=True, dim_self=False, mode="powerline"
    ).text()
    fitted = cut_to(line, line.cell_len, action=True)
    assert CRACK_TAIL not in fitted.plain
    assert fitted.plain == line.plain


def test_hops_atom_counts_relays_and_says_direct_for_none() -> None:
    """The atom of the statistics line: ``direct`` for a route with no hops, else ``n hop(s)``.

    A path with no relays is not "0 hops". ``direct`` is the app's own word for a
    packet that went straight there, and :class:`PathLine` says it for an empty path also.
    """
    assert hops_atom(0).plain == "direct"
    assert hops_atom(1).plain == "1 hop"
    assert hops_atom(4).plain == "4 hops"
    assert hops_atom(-1).plain == "direct"  # a caller that subtracted its endpoints two times
    assert all(str(atom.style) == "muted" for atom in (hops_atom(0), hops_atom(3)))
