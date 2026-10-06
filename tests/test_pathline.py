# SPDX-License-Identifier: Apache-2.0
"""Path-line widget tests: same words in both modes, three overflow shapes.

The widget's contract is exactness — plain mode must reproduce the app-wide arrow
presentation glyph for glyph, powerline mode must interlock chip fills through the
separator's foreground/background trick, and every overflow shape must respect its
cell budget — so these tests assert rendered strings and span styles, not vibes.
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
    _SELF_INK,
    _YOU_BG,
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
    SEAM_SHADE,
    SELF_GLYPH,
    WRAP_OFFSET,
    PathHop,
    PathLine,
    _seam_ink,
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


def _styles(text) -> dict[str, str]:  # noqa: ANN001
    """Map each styled slice of a Text to its style string, for spot checks."""
    return {text.plain[s.start : s.end]: str(s.style) for s in text.spans}


def _char_styles(text) -> list[tuple[str, str]]:  # noqa: ANN001
    """Every character paired with its effective span style — exact-parity checks.

    Span *boundaries* may legally differ between two builders (one appends a hash in
    two pieces, the other in one); what must agree is the style each character lands
    under, so the comparison is per cell, not per span.
    """
    styles = [""] * len(text.plain)
    for span in text.spans:
        for i in range(span.start, span.end):
            styles[i] = str(span.style)
    return list(zip(text.plain, styles, strict=True))


def test_plain_mode_matches_the_app_wide_arrow_presentation() -> None:
    """Plain mode reproduces the app-wide ``path_text`` look exactly.

    Names in their key hue, us white, keyless muted, arrows muted.
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
    assert styles["77"] == node_style("77")  # the lit hash prefix carries the hue


def test_plain_mode_annotation_dim_and_custom_separator() -> None:
    """Annotations, dimming, and a caller's own separator all survive plain mode.

    The trace ``(3d)`` note rides muted; a dim hop (and the arrow into it) fades;
    a surface's own separator — the mesh walk trail's ``›`` — passes straight through.
    """
    line = PathLine(
        [PathHop("Hub", key="3d", annotation="3d"), PathHop("home", you=True, dim=True)],
        mode="plain",
    )
    text = line.text()
    assert text.plain == "Hub (3d) → home"
    styles = _styles(text)
    assert styles[" (3d)"] == "muted"  # the annotation note, space included
    assert styles["home"] == "faint"
    assert styles[" → "] == "faint"  # the arrow into a dim hop fades with it
    trail = PathLine([PathHop("a"), PathHop("b")], mode="plain", separator=" › ").text()
    assert trail.plain == "a › b"


def test_empty_path_reads_as_the_callers_word() -> None:
    """A hopless line is the muted empty note, in every shape."""
    assert PathLine([], mode="plain").text().plain == "direct"
    assert (
        PathLine([], mode="powerline", empty="direct — no relays").text().plain
        == "direct — no relays"
    )
    assert PathLine([], mode="plain").wrapped(40)[0].plain == "direct"


def test_chips_are_joined_by_one_interlocked_chevron() -> None:
    """A seam is one cell, not two — and the path's own end is not a seam at all.

    The previous chip's point is laid *on* the next chip's fill, so the route reads as
    a ribbon whose segments meet on a chevron. The point is spoken for: it is what says
    the route continues, so a finished path never wears one. It ends square on the last
    chip's own pad, and the only chevrons in the line are the joins.
    """
    alice_fill = node_style("aa").split()[-1]
    line = PathLine([PathHop("Alice", key="aa"), PathHop("you", you=True)], mode="powerline")
    text = line.text()
    assert text.plain == f" Alice {POWERLINE_SEP} you "
    seam_styles = [str(s.style) for s in text.spans if text.plain[s.start : s.end] == POWERLINE_SEP]
    assert seam_styles == [f"{alice_fill} on {_YOU_BG}"]


def test_chips_keep_the_same_words_and_honour_style_overrides() -> None:
    """Chips change colours and separators, never the words.

    An explicit style override (hex or theme name) becomes the chip fill.
    """
    hops = [PathHop("Hub", key="3d", annotation="3d"), PathHop("you", you=True)]
    plain = PathLine(hops, mode="plain").text().plain
    chips = PathLine(hops, mode="powerline").text().plain
    assert plain.replace(" → ", " ") == chips.replace(POWERLINE_SEP, "").replace("  ", " ").strip()
    themed = PathLine([PathHop("X", style="brand")], mode="powerline").text()
    assert pathline._fills(themed)[-1] == "#5eead4"  # the chip runs to the very last cell
    hexed = PathLine([PathHop("X", style="bold #123456")], mode="powerline").text()
    assert pathline._fills(hexed)[-1] == "#123456"


def test_auto_mode_follows_the_terminal_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """``auto`` renders chips exactly when the terminal can draw them."""
    hops = [PathHop("a"), PathHop("b")]
    monkeypatch.setattr(pathline, "powerline_enabled", lambda: True)
    assert POWERLINE_THIN in PathLine(hops).text().plain  # two keyless greys: the thin seam
    monkeypatch.setattr(pathline, "powerline_enabled", lambda: False)
    assert PathLine(hops).text().plain == "a → b"


def test_the_cardputer_draws_chips_in_glyphs_its_font_has(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Cardputer's own font has the chevrons, so its paths are chips that fold clean.

    Its verdict is read off its font (:mod:`~meshterm.ui.termfont`), and the render
    boundary folds whatever that font lacks to ``?`` — so every mark the chip language
    spends (both seams, both cracks, the elision, our star) must come through unchanged.
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
    """A node's chip is the dim twin of its hue, and the label is the hue itself.

    The VT draws a background only from slots 0 to 7. Thus each fill is a dim slot, and
    each label is the bright slot that the name has in arrow mode. A keyless chip is
    white on light grey. Our star and a faded hop stand on the page, because the console
    has no dark grey background. The row is a selected row, whose ``bold`` base style
    must not move a dim fill to its bright twin: each chip cell arrives without bold.
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
    assert at("(0a)").fg == _rgb(red)  # the annotation is in the label's colour too
    assert (at("3d").fg, at("3d").bg) == (_rgb(slot_hex(15)), _rgb(slot_hex(7)))
    assert at("Ridge").bg in (None, _rgb(slot_hex(0)))  # faded: on the page
    assert at("Ridge").fg == _rgb(slot_hex(8))
    assert at(SELF_GLYPH).bg in (None, _rgb(slot_hex(0)))


def test_a_picocalc_hash_label_is_one_colour(picocalc: None) -> None:
    """The rest of a hash is in the hue of its lit bytes, not in a grey.

    A grey after the lit byte was not readable on the green and the cyan fills
    (JP, 2026-10-06).
    """
    for key in ("0a", "1c", "44", "70", "9a", "c4"):
        line = PathLine([PathHop(f"{key}63c6", key=key, lit_bytes=1)]).text()
        cells = _console_cells(line)
        hue = _rgb(_style_hex(node_style(key)))
        label = [style.fg for char, style in cells if char in f"{key}63c6" and char != " "]
        assert label == [hue] * 6, key


def test_picocalc_seams_on_one_colour_and_the_composer_slot(picocalc: None) -> None:
    """Two chips of one colour join on a thin chevron in the hue. The slot is a break.

    The thin chevron is the bright twin of the fill: the colour of the label on the
    chip. The composer's slot has no white background on the console, so it is a white
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
    assert line.plain.count(POWERLINE_SEP) == 2  # a point into the break, a notch out of it


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
    """The composer's insertion point is a slot *in* the route, not a gap between hops.

    It renders as a hop, so chips stay chips and arrows stay arrows.
    """
    hops = [PathHop("a"), PathHop(CURSOR_GLYPH, cursor=True), PathHop("b")]
    plain = PathLine(hops, mode="plain").text()
    assert plain.plain == f"a → {CURSOR_GLYPH} → b"
    assert _styles(plain)[CURSOR_GLYPH] == "selected"  # the reverse-video block

    chips = PathLine(hops, mode="powerline").text()
    assert chips.plain == f" a {POWERLINE_SEP} {CURSOR_GLYPH} {POWERLINE_SEP} b "
    slot = next(s for s in chips.spans if chips.plain[s.start : s.end] == CURSOR_GLYPH)
    assert str(slot.style).endswith(f"on {_style_hex('cursor')}")  # the cursor-white fill


def test_ellipsized_elides_the_middle_and_keeps_both_endpoints() -> None:
    """The fit drops middle hops behind ``⋯`` and keeps both endpoints.

    A route reads origin and destination first, so the tail a right-edge truncation
    would amputate is exactly what must survive.
    """
    hops = [PathHop(label) for label in ("AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF")]
    line = PathLine(hops, mode="plain")
    assert line.ellipsized(200).plain == line.text().plain  # fits → untouched
    fitted = line.ellipsized(25)
    assert fitted.cell_len <= 25
    assert fitted.plain.startswith("AAAA")
    assert fitted.plain.endswith("FFFF")
    assert "⋯" in fitted.plain


def test_ellipsized_eats_the_head_when_told_to() -> None:
    """``ELIDE_HEAD`` spares the origin nothing and keeps the tail instead.

    A breadcrumb's news is where the walk *is*, so the fit keeps as much of the tail
    as the width holds, behind a ``⋯``.
    """
    hops = [PathHop(label) for label in ("AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF")]
    line = PathLine(hops, mode="plain")
    fits = line.ellipsized(200, elide=ELIDE_HEAD).plain
    assert fits == line.text().plain  # fits → untouched, whichever side would give
    fitted = line.ellipsized(25, elide=ELIDE_HEAD)
    assert fitted.cell_len <= 25
    assert fitted.plain.startswith("⋯")  # the origin goes too, unlike the tail-side fit
    assert fitted.plain.endswith("FFFF")
    assert "AAAA" not in fitted.plain


def test_ellipsized_last_resort_truncates_a_single_giant_hop() -> None:
    """When even ``⋯ → last`` overflows, the classic ellipsis truncation steps in."""
    line = PathLine([PathHop("AAAAAAAAAA"), PathHop("BBBBBBBBBB")], mode="plain")
    fitted = line.ellipsized(8)
    assert fitted.cell_len <= 8
    assert fitted.plain.endswith("…")


def _chips() -> PathLine:
    """Three keyed hops in chip mode — each fill a different hash-derived hue."""
    return PathLine(
        [PathHop("AAAA", key="11aa"), PathHop("BBBB", key="22bb"), PathHop("CCCC", key="33cc")],
        mode="powerline",
    )


def test_cut_to_cracks_a_chip_in_its_own_fill_instead_of_ellipsizing() -> None:
    """A chip caught by the cut breaks off on a half block in its own fill.

    The crack reads as a segment that continues, where ``…`` would claim a word was
    shortened.
    """
    full = _chips().text()
    fitted = cut_to(full, 14)
    assert fitted.cell_len == 14
    assert fitted.plain.endswith(CRACK_TAIL)
    assert "…" not in fitted.plain
    # The cut lands inside ``BBBB``, so the crack wears BBBB's hue, not its neighbours'.
    assert str(fitted.spans[-1].style) == _style_hex(node_style("22bb"))
    assert cut_to(full, 200) is full  # fits → untouched, no copy, no mark


def test_cut_to_leaves_arrow_lines_on_the_ellipsis() -> None:
    """Only chips crack; an arrow line keeps the ellipsis.

    It has no fill to shear, so shortening is exactly what happened, and the classic
    mark still says so.
    """
    fitted = cut_to(PathLine(_chips().hops, mode="plain").text(), 10)
    assert fitted.cell_len <= 10
    assert fitted.plain.endswith("…")
    assert CRACK_TAIL not in fitted.plain


def test_cut_mark_mirrors_itself_and_reads_the_visible_side() -> None:
    """The head mark is the tail's mirror, and each takes the nearest drawn cell's fill.

    That means scanning back for a tail cut, and forward for a head cut.
    """
    full = _chips().text()
    body = full.plain.index("AAAA")  # inside the first chip, either way you scan
    head, tail = cut_mark(full, body, ELIDE_HEAD), cut_mark(full, body, ELIDE_TAIL)
    assert (head.plain, tail.plain) == (CRACK_HEAD, CRACK_TAIL)
    assert str(head.style) == str(tail.style) == _style_hex(node_style("11aa"))

    # A cut landing on the interlocked seam itself cracks in the field that cell carries —
    # the chip *ahead*, whose fill is literally the seam's background — so the mark reads
    # as the next segment beginning and being sheared, not as a colour off the line.
    seam = full.plain.index(POWERLINE_SEP)
    assert str(cut_mark(full, seam, ELIDE_TAIL).style) == _style_hex(node_style("22bb"))


def test_cut_mark_falls_back_to_the_ellipsis_off_a_chip() -> None:
    """Off a chip, the same question answers with the muted ``…``.

    The fallback is the whole mode test, so no caller has to know which mode drew the
    line.
    """
    arrows = PathLine(_chips().hops, mode="plain").text()
    for side in (ELIDE_HEAD, ELIDE_TAIL):
        mark = cut_mark(arrows, 6, side)
        assert mark.plain == "…"
        assert str(mark.style) == "muted"


def test_the_elision_sits_between_chips_rather_than_being_one() -> None:
    """Hops dropped out of a path are a break *in* the ribbon, not a node called ``⋯``.

    The chip before it closes onto the page, the mark sits there bare — no fill, no
    padding — and the chip after it opens on its notch. Three cells for the whole gap
    where a filled ``⋯`` chip with its pads and two seams cost seven, which is four more
    hops of route on a line that is short of room by definition.
    """
    line = PathLine([PathHop(f"NODE{i:02d}", key=f"{i:02x}aa") for i in range(5)], mode="powerline")
    fitted = line.ellipsized(30)
    assert fitted.cell_len <= 30
    at = fitted.plain.index("⋯")
    assert fitted.plain[at - 1] == POWERLINE_SEP and fitted.plain[at + 1] == POWERLINE_SEP
    styles = {fitted.plain[s.start : s.end]: str(s.style) for s in fitted.spans}
    assert styles["⋯"] == "faint"  # bare on the page: no ``on`` fill, and no pads either

    # Arrow mode needed no special case — a bare label between two arrows already *is* a gap.
    plain = PathLine(line.hops, mode="plain").ellipsized(30)
    assert " ⋯ " in plain.plain and "→ ⋯ →" in plain.plain


def test_elision_hop_is_the_one_definition_both_surfaces_share() -> None:
    """One elision definition serves both surfaces.

    The widget and the mesh walk's trail elide with the same mark *and* the same gap
    semantics, so a head the widget hid reads as a tail the scroll hid.
    """
    mark = elision_hop()
    assert (mark.label, mark.gap, mark.dim) == ("⋯", True, True)


def test_wrapped_cracks_an_over_wide_lone_chip() -> None:
    """A chip wider than the content column is cracked rather than folded.

    It is the one place a chip line is cut mid-hop: the hop stands alone, cracked, and
    still inside the budget.
    """
    lines = PathLine([PathHop("N" * 40, key="11aa")], mode="powerline").wrapped(20)
    assert len(lines) == 1
    assert lines[0].cell_len <= 20
    assert lines[0].plain.endswith(CRACK_TAIL)


def test_wrapped_breaks_at_hops_under_a_hanging_indent() -> None:
    """A wrapped plain path breaks at hops, under a hanging indent.

    Lines that continue end with the ``→`` cue; continuations hang at the indent,
    stepped in by :data:`WRAP_OFFSET`; every line respects the full width.
    """
    hops = [PathHop(label) for label in ("AAAA", "BBBB", "CCCC", "DDDD")]
    lines = PathLine(hops, mode="plain").wrapped(16, indent=2)
    assert [line.plain for line in lines] == ["AAAA → BBBB →", "    CCCC → DDDD"]
    assert lines[1].plain.startswith(" " * (2 + WRAP_OFFSET))
    assert all(line.cell_len <= 16 for line in lines)


def test_wrapped_evens_the_lines_instead_of_widowing_the_tail() -> None:
    """A hop that misses the first line by a cell doesn't get stranded alone below it.

    Greedy packing would cram ``us → a → b →`` and widow ``us``; the fold takes the
    same number of lines either way, so it spreads the hops across them instead.
    """
    hops = [PathHop(label) for label in ("us", "alpha", "bravo", "us")]
    lines = PathLine(hops, mode="plain").wrapped(20, indent=0)
    assert [line.plain for line in lines] == ["us → alpha →", "  bravo → us"]


def test_wrapped_folds_a_faded_return_leg_at_its_turn() -> None:
    """A boomerang breaks where it turns — composed leg above, its echo below.

    The dimmed hops are the mirrored return, so folding there gives the wrapped route
    a shape instead of an arbitrary mid-leg seam. It is only taken when free: here the
    turn fold costs no more lines than the greedy break would.
    """
    out = [PathHop(n) for n in ("me", "north-relay", "east-relay", "dest")]
    back = [PathHop(n, dim=True) for n in ("east-relay", "north-relay", "me")]
    lines = PathLine(out + back, mode="plain").wrapped(40, indent=0)
    assert [line.plain for line in lines] == [
        "me → north-relay → east-relay → dest →",  # out, ending on the target
        "  east-relay → north-relay → me",  # and the mirror it comes home by
    ]


def test_wrapped_folds_a_walked_boomerang_at_its_turn_too() -> None:
    """A trace that answered dims nothing, but its route is still its own mirror.

    So the turn is found from the hop sequence itself, and the walk folds exactly
    where the plan that armed it folded — one route, one shape.
    """
    walked = [PathHop(n) for n in ("me", "north-relay", "east-relay", "dest")]
    walked += [PathHop(n) for n in ("east-relay", "north-relay", "me")]
    lines = PathLine(walked, mode="plain").wrapped(46, indent=0)
    assert [line.plain for line in lines] == [
        "me → north-relay → east-relay → dest →",
        "  east-relay → north-relay → me",
    ]


def test_wrapped_never_folds_before_a_lone_faded_landing() -> None:
    """A path whose only dim hop is the automatic landing home keeps it on a real line.

    Path-mode walks dim just that last ``us``; folding at "the fade" there would widow
    it, so the seam needs two hops on each side to count as a turn.
    """
    hops = [PathHop(label) for label in ("us", "alpha", "bravo", "charlie")]
    hops.append(PathHop("us", dim=True))
    lines = PathLine(hops, mode="plain").wrapped(26, indent=0)
    assert len(lines) == 2
    assert lines[-1].plain != "us"
    assert lines[-1].plain.endswith("→ us")


def test_wrapped_lines_always_fit_their_width() -> None:
    """No mix of hops, mode, width, or indent ever produces a line past the budget.

    Lines are fitted arithmetically from per-hop measurements rather than by rendering
    every candidate group, so this sweeps that model against what actually gets drawn —
    over-wide lone hops (truncated, cue included) and chip lines (which carry a closing
    edge no arrow line has) are where the two would drift apart.
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
    """The continuation cue is the separator's own mark, not always an arrow.

    A comma-joined line (the trace screen's wire spec) continues on a comma, so the
    spec stays verbatim.
    """
    hops = [PathHop(label) for label in ("3d63ab99", "7f21cd01", "27aa1122")]
    lines = PathLine(hops, mode="plain", separator=",").wrapped(20, indent=2)
    assert lines[0].plain.endswith(",")
    assert "→" not in "".join(line.plain for line in lines)
    assert "".join(line.plain.strip() for line in lines) == "3d63ab99,7f21cd01,27aa1122"


def test_path_line_factory_matches_path_text_character_for_character() -> None:
    """``path_line`` renders the trace flavour exactly as ``path_text`` did.

    The migration bridge: device endpoints with annotated hashes, a named hop, a
    prefix-lit unnamed hop, and a dimmed tail, matching character and style alike.
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
    """The message-paths flavour matches ``path_text`` too.

    An unnamed hop stands as its own muted identity hash, annotated with its addressed
    byte — the same parity guarantee.
    """
    names = {"3d63abcdef00": "Hub"}
    hops = ["3d63abcdef00", "e839f2aabb11"]
    kwargs = dict(prefix_bytes=3, show_hash=True, hash_bytes=1, hash_as_name=True)
    old = path_text(hops, lambda h: names.get(h, h), **kwargs)
    new = path_line(hops, lambda h: names.get(h, h), mode="plain", **kwargs).text()
    assert _char_styles(new) == _char_styles(old)
    assert path_line([], lambda h: h).text().plain == path_text([], lambda h: h).plain


def test_path_line_factory_splices_the_cursor_slot_at_a_hop_index() -> None:
    """``cursor`` splices the insertion slot in at a hop index.

    It names the position a chosen hop would take, counted over the rendered hops and
    applied after them, so the slot never shifts what its neighbours show.
    """
    hops = [None, "aa11bb", "3d63ab", None]

    def built(cursor) -> str:  # noqa: ANN001
        line = path_line(hops, self_name="Me", bare_self=True, mode="plain", cursor=cursor)
        return line.text().plain

    assert built(None) == f"{SELF_GLYPH} → aa11bb → 3d63ab → {SELF_GLYPH}"
    assert built(0) == f"{CURSOR_GLYPH} → {SELF_GLYPH} → aa11bb → 3d63ab → {SELF_GLYPH}"
    assert built(2) == f"{SELF_GLYPH} → aa11bb → {CURSOR_GLYPH} → 3d63ab → {SELF_GLYPH}"
    assert built(99).endswith(f"{SELF_GLYPH} → {CURSOR_GLYPH}")  # clamped to the tail


def test_wrapped_carries_the_cursor_like_any_other_hop() -> None:
    """A folded route can't strand its insertion point.

    The slot is a hop, so it lands on a line of its own accord — wherever it stands,
    and whatever the mode.
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
    """Chip wrapping never splits a chip; each line opens on the break it continues.

    Every line the path *outruns* ends on the pointed edge — the last one ends square,
    the route having finished — and every line *but the first* opens on the break's
    other half: the point notched out of its own fill in reverse video, so the page
    shows through and not a trace of the line above bleeds down. The point always faces
    the way the path flows.
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
    # …past the line's own PATH_INK stamp, which covers the whole body from the same cell
    # (it marks the run as a path line for the cursor fold and draws nothing).
    carried = next(s for s in lines[1].spans if s.start == step and str(s.style) != PATH_INK)
    assert str(carried.style) == f"{_style_hex(node_style('3d'))} reverse"
    assert lines[0].plain.endswith(POWERLINE_SEP)  # …the path runs on past this line
    assert lines[-1].plain.endswith(" ")  # …and stops square on the last chip's pad
    assert all(line.cell_len <= 20 for line in lines)


def test_rounded_caps_finish_a_path_only_where_the_font_has_them(monkeypatch) -> None:  # noqa: ANN001
    """Rounded caps finish a path only where the font actually has them.

    A full Nerd Font rounds the path's two *outer* ends into a lozenge; a core-only
    terminal squares *both* of them off, appending nothing and letting the outer chips'
    own pads be the edges, never drawing tofu. Interior breaks stay angled either way —
    a rounded end would read as the path stopping, and a pointed one at the finish would
    read as the opposite, a route cut off mid-walk.
    """
    hops = [
        PathHop(label, key=key)
        for label, key in (("AAAA", "aa"), ("BBBB", "77"), ("CCCC", "3d"), ("DDDD", "f2"))
    ]
    line = PathLine(hops, mode="powerline")

    monkeypatch.setattr(pathline, "powerline_full", lambda: False)
    assert line.text().plain.startswith(" AAAA")
    assert line.text().plain.endswith("DDDD ")
    # The point is still what a *wrapped* line ends on: there the route really does go on.
    squared = line.wrapped(20, indent=2)
    assert squared[0].plain.endswith(POWERLINE_SEP) and squared[-1].plain.endswith("DDDD ")

    monkeypatch.setattr(pathline, "powerline_full", lambda: True)
    assert line.text().plain.startswith(POWERLINE_ROUND_OPEN + " AAAA")
    assert line.text().plain.endswith(POWERLINE_ROUND_CLOSE)
    wrapped = line.wrapped(20, indent=2)
    assert len(wrapped) == 2
    assert wrapped[0].plain.startswith(POWERLINE_ROUND_OPEN)  # the path opens here…
    assert wrapped[0].plain.endswith(POWERLINE_SEP)  # …but does not end here
    assert wrapped[1].plain[2 + WRAP_OFFSET] == POWERLINE_SEP  # picked up mid-path…
    assert wrapped[1].plain.endswith(POWERLINE_ROUND_CLOSE)  # …and closed off
    assert all(text.cell_len <= 20 for text in wrapped)


def test_a_line_drawing_only_a_route_middle_wears_the_chevron_at_that_end(monkeypatch) -> None:  # noqa: ANN001
    """A chain of relays has no endpoints of its own, so neither end may claim to be one.

    A packet's ``via`` field names the nodes that forwarded it and neither the node it
    came from nor the one that received it. A flat end there would say the first relay
    *is* where the route began — so the line opens on the notch and closes on the point
    instead, the same "there is more of this out there" mark a wrapped line uses. The cap
    is what the flags trade away: a full Nerd Font rounds only the ends that are real.
    """
    hops = [PathHop(label, key=key) for label, key in (("AAAA", "aa"), ("BBBB", "77"))]
    middle = PathLine(hops, mode="powerline", from_origin=False, to_destination=False)

    monkeypatch.setattr(pathline, "powerline_full", lambda: True)
    text = middle.text()
    assert text.plain.startswith(POWERLINE_SEP + " AAAA")  # the notch, not the lozenge
    assert text.plain.endswith("BBBB " + POWERLINE_SEP)  # the point: the route goes on
    opening = next(s for s in text.spans if s.start == 0 and str(s.style) != PATH_INK)
    assert str(opening.style) == f"{_style_hex(node_style('aa'))} reverse"  # cut out of AAAA

    # One end at a time: the half that *is* an endpoint keeps its cap either way.
    head = PathLine(hops, mode="powerline", to_destination=False).text().plain
    assert head.startswith(POWERLINE_ROUND_OPEN) and head.endswith(POWERLINE_SEP)
    tail = PathLine(hops, mode="powerline", from_origin=False).text().plain
    assert tail.startswith(POWERLINE_SEP) and tail.endswith(POWERLINE_ROUND_CLOSE)

    monkeypatch.setattr(pathline, "powerline_full", lambda: False)
    squared = PathLine(hops, mode="powerline").text().plain  # the whole route, for contrast
    assert squared.startswith(" AAAA") and squared.endswith("BBBB ")
    assert middle.text().plain.startswith(POWERLINE_SEP)  # …the chevron is not the cap
    assert middle.text().plain.endswith(POWERLINE_SEP)


def test_arrow_mode_says_a_route_middle_with_the_separator_it_joins_with() -> None:
    """No chevrons to shear on a console, so the arrow itself carries the same claim.

    A leading ``→`` where the origin is not drawn, a trailing one where the destination
    is not — the mark arrow mode already spells "the path goes on" with when a line
    wraps. Both modes tell the reader the same thing.
    """
    hops = [PathHop(label, key=key) for label, key in (("AAAA", "aa"), ("BBBB", "77"))]
    assert PathLine(hops, mode="plain").text().plain == "AAAA → BBBB"
    middle = PathLine(hops, mode="plain", from_origin=False, to_destination=False)
    assert middle.text().plain == "→ AAAA → BBBB →"
    assert PathLine(hops, mode="plain", from_origin=False).text().plain == "→ AAAA → BBBB"
    assert PathLine(hops, mode="plain", to_destination=False).text().plain == "AAAA → BBBB →"


def test_a_wrapped_route_middle_pays_for_both_its_chevrons() -> None:
    """The marks are measured, not appended past the budget — on either mode.

    A relays-only line spends a cell at each outer end that a whole route spends only
    where the font has caps, so the fit has to know about them; the wrap flags and the
    endpoint flags must not both draw at the same break either.
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
    assert lines[0].plain.startswith(POWERLINE_SEP)  # opens mid-route…
    assert lines[0].plain.endswith(POWERLINE_SEP)  # …and the fold goes on
    assert lines[-1].plain.endswith(POWERLINE_SEP)  # …as does the route past the last chip
    # Never both marks at one break: a fold that also happens to be the head or the
    # tail of the drawn route still draws one chevron there, not two.
    assert POWERLINE_SEP * 2 not in "".join(line.plain for line in lines)

    folded = PathLine(hops, mode="plain", from_origin=False, to_destination=False).wrapped(
        22, indent=2
    )
    assert len(folded) > 1 and all(line.cell_len <= 22 for line in folded)
    assert folded[0].plain.startswith("→ AAAA")  # the head mark, once
    assert folded[-1].plain.rstrip().endswith("→")
    assert not folded[-1].plain.strip().startswith("→")  # a continuation opens on its hop


def _seams(*hops: PathHop) -> list[tuple[str, str]]:
    """Every seam cell of a chip line as ``(glyph, style)``, in order."""
    text = PathLine(list(hops), mode="powerline").text()
    return [
        (text.plain[s.start], str(s.style))
        for s in text.spans
        if text.plain[s.start] in (POWERLINE_SEP, POWERLINE_THIN)
    ]


def test_a_seam_between_two_of_one_colour_is_the_thin_chevron_shaded() -> None:
    """Where two chips land on the same fill, the seam is the thin chevron in that fill, shaded.

    The interlock can't draw a solid point in its own background, so the join is drawn
    *on* the shared fill instead — powerline's own mark for a join inside one colour —
    and the ribbon runs on unbroken. Its colour is the previous chip's own, a lightness
    step darker, so nothing on the line is a colour that isn't a chip's; a dark fill (the
    faded slate) steps lighter instead.

    Not a rare accident, either: a mirrored return leg is a run of identically faded hops
    by construction, and a stretch of keyless hops shares one grey. Either way it stays a
    single cell — the fallback trades the interlock for the outline, not for width.
    """
    hue = _style_hex(node_style("aa"))
    glyph, style = _seams(PathHop("A", key="aa"), PathHop("B", key="aa"))[0]
    assert glyph == POWERLINE_THIN and style == f"{_seam_ink(hue, hue)} on {hue}"
    shade = _seam_ink(hue, hue)
    assert oklab.from_hex(shade)[0] < oklab.from_hex(hue)[0]  # darker…
    assert oklab.distance(shade, hue) == pytest.approx(SEAM_SHADE, abs=0.01)  # …by the step
    assert oklab.distance(shade, hue) < oklab.distance(hue, _style_hex(node_style("77")))

    assert _seams(PathHop("A", key="aa"), PathHop("B", key="77"))[0] == (
        POWERLINE_SEP,
        f"{hue} on {_style_hex(node_style('77'))}",
    )  # blended, one cell

    glyph, style = _seams(PathHop("A", dim=True), PathHop("B", dim=True))[0]
    assert glyph == POWERLINE_THIN and style.endswith(f" on {_DIM_BG}")
    lighter = style.split()[0]
    assert oklab.from_hex(lighter)[0] > oklab.from_hex(_DIM_BG)[0]  # a dark fill steps up


def test_a_seam_blurs_by_perceived_distance_not_by_hue_gap() -> None:
    """Two fills under ``SEAM_BLUR`` apart to the eye blur like an exact match.

    The eye, not the wheel: sixteen first-byte steps across the greens are one colour to
    a reader and sixteen across the reds are two, so the same hue gap blurs on one side
    of the wheel and interlocks on the other. The greys follow the same metric with no
    exemption: the keyless grey and the faded slate sit well apart and keep their seam.
    """
    green = _seams(PathHop("A", key="4c"), PathHop("B", key="5c"))[0]
    red = _seams(PathHop("A", key="00"), PathHop("B", key="10"))[0]
    assert green[0] == POWERLINE_THIN and red[0] == POWERLINE_SEP
    assert oklab.distance(_style_hex(node_style("4c")), _style_hex(node_style("5c"))) < SEAM_BLUR
    assert oklab.distance(_style_hex(node_style("00")), _style_hex(node_style("10"))) > SEAM_BLUR

    # The shade is taken from the chip behind but clears the chip ahead as well.
    before, after = _style_hex(node_style("4c")), _style_hex(node_style("5c"))
    assert green[1] == f"{_seam_ink(before, after)} on {after}"
    assert oklab.distance(_seam_ink(before, after), after) >= SEAM_SHADE - 1e-6

    wrapped = _seams(PathHop("A", key="fc"), PathHop("B", key="02"))[0]
    assert wrapped[0] == POWERLINE_THIN  # neighbours across the wheel's seam, too

    glyph, style = _seams(PathHop("A"), PathHop("B", dim=True))[0]
    assert glyph == POWERLINE_SEP and " on " in style  # two greys, still interlocked


def test_bare_self_stands_us_on_a_star_and_fades_both_ends() -> None:
    """``bare_self`` stands us on a ★ at both ends, and fades both.

    Setting out from us and landing back on us are fixtures of the route, not choices,
    so they wear the same automatic grey as a mirrored return leg — and, in chips, the
    same dark slate rather than the loud you white.
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
    assert all("#ffffff" not in fill for fill in star_fills)  # never the you white

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
    """``dim_self=False`` un-mutes the stars: the fade means "not yours to compose".

    A surface that only *shows* a walk (the Trophy case's boards and record card) composes
    nothing, so there is no "not yours" to say and our own node reads in the app-wide ``you``
    white — the same identity colour it wears everywhere else. A ``dim_from`` that reaches a
    star still fades it, so a mirrored return leg keeps its grey either way.
    """
    hops = [None, *(f"{i:02x}aa" for i in range(4)), None]
    text = path_line(
        hops, prefix_bytes=2, self_name="Me", bare_self=True, dim_self=False, mode="plain"
    ).text()
    stars = [s for s in text.spans if text.plain[s.start : s.end] == SELF_GLYPH]
    assert [str(s.style) for s in stars] == ["you", "you"]
    # In chips the same identity is the map's yellow star on the neutral dark grey — the
    # marker itself rather than a hue, since our end is a fixture and not a node to tell apart.
    chips = path_line(
        hops, prefix_bytes=2, self_name="Me", bare_self=True, dim_self=False, mode="powerline"
    ).text()
    fills = [str(s.style) for s in chips.spans if chips.plain[s.start : s.end] == SELF_GLYPH]
    assert fills == [f"bold {_SELF_INK} on {_YOU_BG}"] * 2

    # The explicit fade still rules: a star inside a dimmed return leg stays grey.
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
    """The opens-further ``…`` costs the path no cells — it takes what is left, or nothing.

    Reserving room for it made a route that filled its lane exactly crack on its last chip
    (JP, 2026-08-10): the row claimed the walk ran on when the walk had finished, and the
    two cells the hint wanted were two the reader lost. So the path is fitted first and the
    mark lands in the leftovers.
    """
    hops = [None, "3d63", "f2a1", None]
    line = path_line(
        hops, prefix_bytes=2, self_name="Me", bare_self=True, dim_self=False, mode="plain"
    ).text()
    exact = line.cell_len

    # A lane the route fills exactly: the route survives whole, and the mark is what goes.
    fitted = cut_to(line, exact, action=True)
    assert fitted.plain == line.plain
    assert fitted.cell_len == exact

    # One cell of slack still isn't enough for " …" — the mark is two cells, all or nothing.
    assert cut_to(line, exact + 1, action=True).plain == line.plain

    # Room for both: the mark appears, and the whole thing still fits the lane.
    roomy = cut_to(line, exact + 2, action=True)
    assert roomy.plain == line.plain + " …"
    assert roomy.cell_len <= exact + 2

    # A route that genuinely overflows is cut as always, and then has no room left over.
    cut = cut_to(line, exact - 4, action=True)
    assert cut.cell_len == exact - 4 and not cut.plain.endswith(" …")

    # The mark never widens a line past its lane, whatever it is handed.
    assert with_action_mark(line, exact - 1).plain == line.plain


def test_action_mark_leaves_a_chip_path_closed_rather_than_cracked() -> None:
    """In chips, the whole point: a route that fits keeps its closing cap, not a crack."""
    hops = [None, "3d63", "f2a1", None]
    line = path_line(
        hops, prefix_bytes=2, self_name="Me", bare_self=True, dim_self=False, mode="powerline"
    ).text()
    fitted = cut_to(line, line.cell_len, action=True)
    assert CRACK_TAIL not in fitted.plain
    assert fitted.plain == line.plain


def test_hops_atom_counts_relays_and_says_direct_for_none() -> None:
    """The stats-line atom: ``direct`` for a hopless route, else ``n hop(s)``.

    A path with no relays isn't "0 hops" — ``direct`` is the app's own word for a frame
    that went straight there, and it is what :class:`PathLine` says for an empty path too.
    """
    assert hops_atom(0).plain == "direct"
    assert hops_atom(1).plain == "1 hop"
    assert hops_atom(4).plain == "4 hops"
    assert hops_atom(-1).plain == "direct"  # a caller that subtracted its endpoints twice
    assert all(str(atom.style) == "muted" for atom in (hops_atom(0), hops_atom(3)))
