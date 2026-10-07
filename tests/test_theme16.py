# SPDX-License-Identifier: Apache-2.0
"""The contracts of the PicoCalc visual language: palette, glyph map, fold, and bindings.

The P3 deliverables are promises about the output: only 16 palette slots, only glyphs that
the console font has, and a layout that is correct after the translation. The gallery
enforces these promises screen by screen. These tests check the code itself. The two
themes have the same style names. The 16-slot theme obeys the rules of the VT for
bold-brightness and for backgrounds. The palette of the deploy script is the same as the
canonical table. The font build script and the frozen inventory change together. The
platform bindings do rebind.
"""

from __future__ import annotations

import re
from pathlib import Path

from rich.cells import cell_len
from rich.default_styles import DEFAULT_STYLES

from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
from meshterm.ui import theme
from meshterm.ui.fontset import FONT_CODEPOINTS
from meshterm.ui.theme import MESH_THEME, MESH_THEME_16, fold_text, glyph, name_style

_FONT_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "picocalc-lyra"
    / "calculinux-console-font-6x12.sh"
)


# -- the two themes -------------------------------------------------------------------


def test_both_themes_define_exactly_the_same_style_names() -> None:
    """A style name that a screen uses must resolve on each platform."""
    assert set(MESH_THEME.styles) == set(MESH_THEME_16.styles)


def _our_styles(theme_obj):
    """The style entries that we declared. Rich adds its own DEFAULT_STYLES to a Theme."""
    return {name: style for name, style in theme_obj.styles.items() if name not in DEFAULT_STYLES}


def test_theme16_uses_only_the_sixteen_slots() -> None:
    """Each 16-slot style uses ``color(0..15)``. It never uses hex or the colour cube."""
    for name, style in _our_styles(MESH_THEME_16).items():
        for color in (style.color, style.bgcolor):
            if color is None:
                continue
            assert color.number is not None and 0 <= color.number <= 15, (
                f"{name!r} uses {color!r}, outside the 16 VT slots"
            )


def test_theme16_bold_never_jumps_a_dim_slot_to_an_unrelated_bright_one() -> None:
    """The VT draws bold as brightness: bold on slots 0-7 becomes slot N+8.

    Slots 5 and 6 have meanings, and their bright partners have different meanings here
    (5 purple is the repeater mark, and 13 pink is the node mark. 6 cyan is hint.brand,
    and 14 is brand). Thus no style can combine bold with them.
    """
    for name, style in _our_styles(MESH_THEME_16).items():
        if style.bold and style.color is not None and style.color.number in (5, 6):
            raise AssertionError(
                f"{name!r} is bold on slot {style.color.number}; the VT would draw "
                f"slot {style.color.number + 8} instead"
            )


def test_theme16_dim_slot_styles_all_answer_the_bold_question() -> None:
    """A dim-slot style must state its intent about bold. It cannot be silent.

    Rich merges a base style into each span that it wraps. Thus a span with ``bold=None``
    in a selected (bold) row inherits bold, and the VT then draws slot N as N+8. The style
    changes colour without a warning, and the colour depends on the place where the style
    is used. Each dim-slot style is therefore set to ``not bold`` (keep the declared
    colour) or ``bold`` (the promotion is intended).
    """
    silent = [
        name
        for name, style in _our_styles(MESH_THEME_16).items()
        if style.color is not None
        and style.color.number is not None
        and style.color.number <= 7
        and style.bold is None
    ]
    assert not silent, (
        f"dim-slot styles that would inherit a row's bold and change colour: {silent}"
    )


def test_unknown_node_grey_survives_a_selected_row() -> None:
    """The reported bug: the hash of an unnamed hop in a selected row showed as white "you".

    ``node.unknown`` is a dim slot. If it inherits bold, bold promotes it to 15, the ``you``
    white. It must render as plain light grey (37) in both cases, on both platforms.
    """
    from io import StringIO

    from rich.console import Console
    from rich.text import Text

    set_platform(PICOCALC_LYRA)
    console = Console(
        theme=MESH_THEME_16,
        width=20,
        file=StringIO(),
        force_terminal=True,
        color_system="standard",
        highlight=False,
    )
    for base in (None, "cursor"):  # a row that is not selected, then the bold cursor row
        row = Text("  ")
        row.append("3d", style="node.unknown")
        if base:
            row.style = base
        with console.capture() as capture:
            console.print(row, end="")
        assert "\x1b[37m3d" in capture.get(), (base, capture.get())


def test_theme16_backgrounds_stay_in_the_dim_bank() -> None:
    """The VT has no bright backgrounds. It has only SGR 40-47."""
    for name, style in _our_styles(MESH_THEME_16).items():
        if style.bgcolor is not None:
            assert style.bgcolor.number <= 7, (
                f"{name!r} background on slot {style.bgcolor.number}; the VT stops at 7"
            )


def test_deploy_script_carries_the_archived_custom_palette() -> None:
    """The deploy script still has the archived custom palette, and it is the same palette.

    The **opt-in** block of the script is the same as ``theme.vtrgb_lines()``, byte for
    byte. The OSC fallback for a console with no keyboard has the same sixteen RGB values.
    Thus the archived palette is one environment variable away, and it is the same as
    `_VT_SLOTS_CUSTOM`.
    """
    script = _FONT_SCRIPT.read_text(encoding="utf-8")
    assert "MESHTERM_CUSTOM_PALETTE" in script, "the opt-in gate vanished"
    assert theme.vtrgb_lines() in script, f"{_FONT_SCRIPT.name} vtrgb drifted"
    for index, (_, _, hex_) in enumerate(theme._VT_SLOTS_CUSTOM):
        sequence = f"\\033]P{index:x}{hex_.lstrip('#').lower()}"
        assert sequence in script, f"OSC fallback missing slot {index}: {sequence}"


# -- the compact glyph map ------------------------------------------------------------


def test_every_glyph_map_target_is_in_the_console_font() -> None:
    """The map can return only characters that the 512-glyph font can draw."""
    for emoji, compact in theme._GLYPH_MAP.items():
        for ch in compact:
            assert ord(ch) in FONT_CODEPOINTS, (
                f"{emoji!r} maps to {compact!r}; {ch!r} is not in the console font"
            )


def test_every_glyph_map_target_stays_inside_the_bmp() -> None:
    """The table has a second consumer, and the console font cannot answer for it.

    The classic Windows console keeps one 16-bit code unit for each cell. Thus it replaces
    a character above U+FFFF with U+FFFD before it uses the font. This is the reason that
    MeshTerm sends icons through this table there too (refer to
    :func:`meshterm.ui.termfont.emoji_support`). Someone can change a substitute to an
    emoji. The emoji draws correctly in the own font of the PicoCalc, but it breaks each
    Windows console without a warning. Thus this test asserts the plane. It does not
    depend on the font whitelist for the same result.
    """
    for emoji, compact in theme._GLYPH_MAP.items():
        for ch in compact:
            assert ord(ch) <= 0xFFFF, (
                f"{emoji!r} maps to {compact!r}; {ch!r} is above U+FFFF and no classic "
                f"Windows console can hold it"
            )


def test_glyph_map_targets_never_widen_their_icon() -> None:
    """A compact form is at most as wide as the measured width of its emoji.

    The fold adds padding for the rest.
    """
    for emoji, compact in theme._GLYPH_MAP.items():
        assert cell_len(compact) <= cell_len(emoji), (
            f"{emoji!r} ({cell_len(emoji)} cells) maps to wider {compact!r}"
        )


def test_glyph_is_identity_on_regular_and_compact_on_picocalc() -> None:
    """Emoji pass through on regular, and they become marks of the console font on the console.

    The console font already has the node marks and the status marks. Thus they stay the
    same on both platforms.
    """
    set_platform(REGULAR)
    assert glyph("📡") == "📡"
    set_platform(PICOCALC_LYRA)
    assert glyph("📡") == "☼"
    assert glyph("★") == "★"  # node and status marks pass through: the font has them


# -- the render-boundary fold ---------------------------------------------------------


def test_fold_is_identity_on_regular_even_after_picocalc_used_it() -> None:
    """The bound functions must not put cached picocalc-lyra folds into a regular render."""
    set_platform(PICOCALC_LYRA)
    assert fold_text("café") == "cafe"
    set_platform(REGULAR)
    assert fold_text("café") == "café"


def test_fold_strips_accents_and_preserves_cell_widths() -> None:
    """The fold leaves only characters that the console font has, and the width stays the same.

    An accent changes to its base letter, and an emoji changes to its mapped mark. Each
    change is in place. Thus no later code must measure the row again.
    """
    set_platform(PICOCALC_LYRA)
    for text in ("café ⚠", "Ĉu vi paroläs", "📡 Advert", "🗑 Clear", "…", "npo Waymarker 🇨🇦"):
        folded = fold_text(text)
        assert cell_len(folded) == cell_len(text), (text, folded)
        for ch in folded:
            assert ord(ch) in FONT_CODEPOINTS or ord(ch) < 0x20, (text, folded, ch)


def test_fold_replaces_the_unmappable_at_width() -> None:
    """CJK and unmapped emoji become ``?`` at their own width. They never become tofu."""
    set_platform(PICOCALC_LYRA)
    assert fold_text("你好") == "????"
    assert cell_len(fold_text("🦕")) == cell_len("🦕")


def test_fold_keeps_ansi_sequences_intact() -> None:
    """The fold changes only text. Colour escapes and newlines pass through without a change."""
    set_platform(PICOCALC_LYRA)
    line = "\x1b[1;93mwarn é\x1b[0m\nnext"
    folded = fold_text(line)
    assert folded == "\x1b[1;93mwarn e\x1b[0m\nnext"


def test_fold_quantizes_embedded_truecolor_to_the_slots() -> None:
    """Truecolour from the canvas (the braille rasters) goes to the nearest palette slot."""
    set_platform(PICOCALC_LYRA)
    folded = fold_text("\x1b[38;2;148;163;184m○\x1b[0m")
    assert "38;2;" not in folded
    # #94a3b8 → the nearest stock slot is 7 (#aaaaaa). A slot in the dim bank states its
    # intensity.
    assert "\x1b[22;37m" in folded
    background = fold_text("\x1b[48;2;94;234;212mX\x1b[0m")
    assert "48;2;" not in background
    assert re.search(r"\x1b\[4[0-7]m", background), background  # the background is in the dim bank
    eight_bit = fold_text("\x1b[38;5;201mX\x1b[0m")
    assert "38;5;" not in eight_bit


def test_fold_quantizes_a_colour_that_shares_its_sequence_with_another() -> None:
    """Rich writes the fg and bg of a style as one sequence. The fold folds each colour on its own.

    The white-on-black of the QR module is the case that found this problem.
    ``ESC[38;2;…;48;2;…m`` is two colours in one list, and a matcher for a colour that is
    alone did not see either colour.
    """
    set_platform(PICOCALC_LYRA)
    folded = fold_text("\x1b[38;2;255;255;255;48;2;0;0;0m█\x1b[0m")
    assert "8;2;" not in folded, folded
    assert folded.startswith("\x1b[97;40m"), folded  # bright white ink on a black field
    with_attribute = fold_text("\x1b[1;38;5;201;48;2;94;234;212mX\x1b[0m")
    assert "8;5;" not in with_attribute and "8;2;" not in with_attribute
    assert with_attribute.startswith("\x1b[1;"), with_attribute  # the bold stays, in place


def test_the_qr_style_is_white_on_black_on_both_themes() -> None:
    """A code is white ink on a black field on each platform, with the own slots of the PicoCalc."""
    from rich.style import Style

    regular = (
        Style.parse(MESH_THEME.styles["qr"])
        if isinstance(MESH_THEME.styles["qr"], str)
        else MESH_THEME.styles["qr"]
    )
    assert regular.color and regular.color.triplet.hex == "#ffffff"
    assert regular.bgcolor and regular.bgcolor.triplet.hex == "#000000"
    console = MESH_THEME_16.styles["qr"]
    assert console.color and console.color.number == 15
    assert console.bgcolor and console.bgcolor.number == 0


def test_fold_drops_zero_width_machinery() -> None:
    """The fold removes variation selectors and zero-width joiners. They have no cell."""
    set_platform(PICOCALC_LYRA)
    assert fold_text("🕸️") == fold_text("🕸")
    assert "‍" not in fold_text("a‍b")


# -- name colouring -------------------------------------------------------------------


def test_names_colour_by_key_on_both_platforms() -> None:
    """One rule everywhere: the key sets the hue, and only its first byte does this."""
    for platform in (REGULAR, PICOCALC_LYRA):
        set_platform(platform)
        hue = name_style("Hilltop-Repeater", "3d63c6429436")
        assert hue.startswith("bold #"), platform.name
        # Each prefix of the key gives the same hue. A new name does not change the colour.
        # A sender that we cannot place gets the grey of an unknown node, because colour is
        # for identities that have a key.
        assert name_style("Hilltop-Repeater", "3d") == hue
        assert name_style("renamed", "3d63c6429436") == hue
        assert name_style("nameless", None) == "node.unknown"


def test_picocalc_node_hues_land_on_their_own_palette_slots() -> None:
    """The quantized wheel uses only the six chromatic bright slots, in equal parts.

    Each hex that the code returns is the own RGB of a slot. Thus each later downsample (the
    downsample of Rich and the downsample of the fold) gives that exact slot. It does not
    guess a neighbour slot.
    """
    set_platform(PICOCALC_LYRA)
    slots = {hex_: slot for slot, _, hex_ in theme._VT_SLOTS}
    landed = [name_style("n", f"{byte:02x}").removeprefix("bold ") for byte in range(256)]
    assert set(landed) == set(theme._NODE_SLOT_HEXES)
    assert {slots[hex_] for hex_ in landed} == {9, 10, 11, 12, 13, 14}
    assert min(landed.count(h) for h in set(landed)) >= 256 // 8  # no sector has too few hues


def test_route_graph_resolves_a_named_marker_colour_to_rgb() -> None:
    """A relay that we cannot name keeps the colour of its marker, also a marker with a type.

    The type marks have theme style names. Thus the label colour of the graph must resolve
    them, and it must not assume a literal hex (it raised ValueError when it assumed a
    hex).
    """
    from meshterm.ui.widgets import route_graph_style

    for platform in (REGULAR, PICOCALC_LYRA):
        set_platform(platform)
        _glyph_of, _label_of, label_rgb_of = route_graph_style(
            resolve=lambda hop: None,
            self_name="Me",
            source="Alice",
            type_of=lambda hop: 2,
        )
        assert label_rgb_of("3d63") == theme.mark_rgb("type.repeater"), platform.name


def test_node_type_marks_stay_distinct_on_the_console() -> None:
    """Each type mark has its own slot. A naive downsample makes the repeater violet grey."""
    set_platform(PICOCALC_LYRA)
    marks = ("type.node", "type.repeater", "type.room", "type.sensor")
    rgbs = [theme.mark_rgb(name) for name in marks]
    assert len(set(rgbs)) == len(marks)
    assert theme.mark_rgb("type.repeater") != theme.mark_rgb("muted")


def test_heat_ladder_walks_jps_seven_slots_in_order() -> None:
    """The heard-age scale has its specified form: seven steps on plain human boundaries.

    The colours are white for less than 5 minutes, then yellow, light red, brown, red, and
    light grey. One cold grey is for both "over a year" and "never heard".
    """
    from meshterm.ui.widgets import _recency_style

    set_platform(PICOCALC_LYRA)
    # fmt: off
    expected = [
        (60, 15), (299, 15),          # under 5 minutes  = white
        (300, 11), (3599, 11),        # 5 minutes        = yellow
        (3600, 9), (86399, 9),        # 1 hour           = light red
        (86400, 3), (604799, 3),      # 1 day            = brown
        (604800, 1), (2591999, 1),    # 1 week           = red
        (2592000, 7), (31535999, 7),  # 1 month          = light grey
        (31536000, 8), (None, 8),     # 1 year, and never = dark grey
    ]
    # fmt: on
    for secs, slot in expected:
        style = MESH_THEME_16.styles[_recency_style(secs)]
        assert style.color is not None and style.color.number == slot, (secs, style)
        assert not style.bold or slot >= 8, f"bold on dim slot {slot} would jump a rung"
    # The regular platform gives the answer to the same question with a continuous hue.
    set_platform(REGULAR)
    assert _recency_style(1200).startswith("#")


def test_canvas_drops_emphasis_where_bold_means_brightness() -> None:
    """A braille canvas must not use bold on the console. The VT changes the colour of the run.

    The canvas quantizes its own truecolour at the fold. Thus a bold run cannot know which
    bank it went to. An unknown label (slot 7) arrives as white (15).
    """
    from meshterm.ui.mapcanvas import MapCanvas

    for platform, expect_bold in ((REGULAR, True), (PICOCALC_LYRA, False)):
        set_platform(platform)
        canvas = MapCanvas(6, 1)
        canvas.marker(0, 0, "x", (148, 163, 184))  # markers always draw in bold
        rendered = "".join(canvas.to_ansi_lines())
        assert ("\x1b[1m" in rendered) is expect_bold, (platform.name, repr(rendered))
    # The colour that the canvas received stays the same: a slot in the dim bank with its
    # intensity stated. Thus the span cannot inherit brightness from the raster cell before
    # it.
    assert "\x1b[22;37m" in rendered


# -- the font build script stays mirrored ---------------------------------------------


def test_font_script_marks_and_fontset_move_together() -> None:
    """The font script and the fontset inventory change together.

    Each codepoint that the script draws or aliases is in the frozen inventory. Each donor
    that the script consumes is not in it. Thus the two files must change in the same
    commit.
    """
    script = _FONT_SCRIPT.read_text(encoding="utf-8")
    marks = {int(m, 16) for m in re.findall(r"^    (0x[0-9A-Fa-f]{4}): art\(", script, re.M)}
    assert marks, "could not parse MARKS out of the font script"
    for cp in marks:
        assert cp in FONT_CODEPOINTS, f"script draws U+{cp:04X} but fontset lacks it"
    aliases = {
        int(m, 16) for m in re.findall(r"^    (0x[0-9A-Fa-f]{4}): 0x[0-9A-Fa-f]{4},", script, re.M)
    }
    for cp in aliases:
        assert cp in FONT_CODEPOINTS, f"script aliases U+{cp:04X} but fontset lacks it"
    donors_match = re.search(r"DONORS = \[(.*?)\]", script, re.S)
    assert donors_match is not None
    # The script consumes the donors from the front to the back, one for each mark. Each
    # donor that it consumes must not be in the inventory. The spare donors at the end can
    # stay in it.
    donor_list = re.findall(r"0x[0-9A-Fa-f]{4}", donors_match.group(1))
    assert len(donor_list) >= len(marks), "fewer donors than marks — the build would fail"
    for cp_hex in donor_list[: len(marks)]:
        cp = int(cp_hex, 16)
        assert cp not in FONT_CODEPOINTS, (
            f"donor U+{cp:04X} is consumed by a mark but still listed in the fontset"
        )


# -- the specimen card ----------------------------------------------------------------


def test_specimen_renders_clean_on_picocalc() -> None:
    """`meshterm specimen` obeys on picocalc-lyra each contract that it shows.

    The contracts are: at most 53 cells in each line, only 16-slot SGR, and only characters
    of the console font.
    """
    from io import StringIO

    from rich.console import Console

    from meshterm.ui.specimen import specimen_lines

    set_platform(PICOCALC_LYRA)
    console = Console(
        theme=MESH_THEME_16,
        width=53,
        file=StringIO(),
        force_terminal=True,
        color_system="standard",
        highlight=False,
    )
    with console.capture() as capture:
        for line in specimen_lines():
            console.print(line)
    for i, line in enumerate(capture.get().splitlines()):
        plain = re.sub(r"\x1b\[[0-9;]*m", "", line)
        assert cell_len(plain) <= 53, f"specimen line {i} is {cell_len(plain)} cells: {plain!r}"
        assert "[38;2;" not in line and "[48;2;" not in line, f"truecolor on line {i}: {line!r}"
        for ch in plain:
            assert ord(ch) in FONT_CODEPOINTS, f"line {i} char {ch!r} outside the font"


def test_every_wordmark_draws_one_cell_per_cell() -> None:
    """No mark has a byte that the terminal does not draw as a glyph.

    An art editor writes directly to video memory. There, the first 32 bytes of codepage 437
    are pictures (►, ◄, ‼) and not commands. No later code reads them as pictures. The
    codecs decode them as the control characters that they nominally are, and Rich measures
    each one as no cells. Thus each one removes a column and moves the rest of its row.
    This is the same fault that NUL cells cause (refer to :mod:`meshterm.ui.logo`). One of
    them, ``0x13``, is XOFF on a real console. This test asks one question that finds all
    of these faults at the same time: does each character that the mark draws use exactly
    the one cell where it was drawn?
    """
    from rich.text import Text

    from meshterm.ui.logo import _rows, _variants

    for name in _variants():
        rows = _rows(name)
        assert rows, f"{name} should be readable"
        for i, row in enumerate(rows):
            plain = Text.from_ansi(row).plain
            odd = [ch for ch in plain if cell_len(ch) != 1 and ch != ""]
            assert not odd, (
                f"{name} row {i} holds {[hex(ord(c)) for c in odd]}, which the terminal "
                "draws in something other than the one cell it was drawn in"
            )


def test_every_mark_is_as_wide_as_its_name_says() -> None:
    """The file name of a mark has the width of its canvas, and the name sets the size ladder.

    :func:`~meshterm.ui.logo.load_logo` goes through the marks from the widest to the
    narrowest, by the width in the name. It takes the first mark that measures small enough
    to fit. If a name states a width that is more than the art, the loader does not draw a
    torn mark. It puts a mark that fits at the back of the queue, and the splash changes to
    a smaller size without a warning. It is easier to find this problem here.
    """
    from meshterm.ui.logo import _NAMED_WIDTH, _rows, _variants, logo_width

    marks = _variants()
    assert marks, "the splash folder should hold at least one mark"
    for name in marks:
        declared = int(_NAMED_WIDTH.search(name).group(1))
        assert logo_width(_rows(name)) == declared, f"{name} is not {declared} cells wide"


def test_the_mark_says_bright_as_a_colour_not_as_bold() -> None:
    """No span of the wordmark depends on a terminal that reads bold as brightness.

    An art editor writes brightness in the DOS way: ``1m`` lifts the foreground into the
    bright bank. This is true on the PicoCalc console and on a DOS console. It is false on
    macOS Terminal, where bold asks for a heavier face and leaves the colour the same. There
    ``1;30`` is plain black, not dark grey. The wide mark has seventy-nine such spans, and
    many of them are on a black ground. Thus the mark drew with muted colours, and the
    dithers that fade one colour into another faded toward the wrong end. So the loader
    writes the bright bank as the colour that it means (``9N``), which is not a font
    weight.
    """
    import re

    from meshterm.ui.logo import _NAMED_WIDTH, _rows, _variants, logo_width

    sgr = re.compile(chr(27) + r"\[([0-9;]*)m")
    for name in _variants():
        rows = _rows(name)
        codes = [m.group(1) for row in rows for m in sgr.finditer(row)]
        assert codes, f"{name} carries no colour at all"

        params = [p for code in codes for p in code.split(";")]
        # Nothing asks for bold: brightness is 90-97 instead.
        assert "1" not in params, f"{name} still leans on bold for brightness"
        assert any(p.isdigit() and 90 <= int(p) <= 97 for p in params), (
            f"{name} lost its bright spans entirely"
        )
        # A foreground on the dim bank still says so clearly. Thus it cannot inherit a bank
        # from the setting that the row above left (each row reaches Rich on its own).
        for code in codes:
            parts = code.split(";")
            if any(re.fullmatch(r"3[0-7]", p) for p in parts):
                assert "22" in parts, f"{name} has a dim foreground that never says so: {code}"

        # The change of the spelling does not move cells: the art has the same size as before.
        assert logo_width(rows) == int(_NAMED_WIDTH.search(name).group(1))


def test_the_mark_the_console_picks_stays_inside_its_font() -> None:
    """The wordmark obeys the glyph contract in the same way as each other screen.

    The console font of the PicoCalc is an inventory of 512 glyphs. A character that is not
    in it is a test failure. It is not a tofu box that someone finds on the handheld. The
    mark is art, not chrome, so it never went through the sweep of the gallery. It is also
    the first thing that the handheld draws.
    """
    from rich.text import Text

    from meshterm.ui.logo import load_logo

    set_platform(PICOCALC_LYRA)
    rows = load_logo(53)
    assert rows, "the 53-column mark should fit a 53-column console"
    for i, row in enumerate(rows):
        for ch in Text.from_ansi(row).plain:
            assert ord(ch) in FONT_CODEPOINTS, (
                f"mark row {i} draws {ch!r} (U+{ord(ch):04X}), which the console font "
                "has no glyph for"
            )


def test_narrow_wordmark_keeps_its_dim_rows_off_the_bright_bank() -> None:
    """Each dim-bank span of the wordmark states its own intensity.

    An art editor writes brightness in the DOS way. A ``1m`` lifts the bank, and each span
    after it inherits that. This is correct only while the escapes are one continuous
    stream. A row reaches the console on its own, and bold is the bright bank there. Thus
    a span that does not state the intensity is read with the setting that is active by
    chance. The letter row of the narrow mark was partly red (slot 1) and partly light red
    (slot 9), and it changed at each span that followed a bevel. The art is drawn again
    from time to time, so this test checks the rule, not the picture: no row index and no
    particular colour.
    """
    from meshterm.ui.logo import load_logo
    from meshterm.ui.tui.frame import _banner_lines

    set_platform(PICOCALC_LYRA)
    rows = _banner_lines(load_logo(53), 53)
    assert rows, "the 53-column mark should fit a 53-column console"
    dim = [
        sgr
        for row in rows
        for sgr in re.findall(r"\x1b\[([0-9;]*)m", row)
        if any(re.fullmatch(r"3[0-7]", part) for part in sgr.split(";"))
    ]
    # Without this check, the loop below passes for a mark that does not use the dim bank.
    assert dim, "the mark paints on the dim bank somewhere, or this check says nothing"
    for sgr in dim:
        parts = sgr.split(";")
        assert any(p in {"0", "1", "2", "22"} for p in parts), f"unstated intensity: {sgr!r}"


# -- relocated marks stay importable from their old homes -----------------------------


def test_mark_constants_reexport_from_their_old_hosts() -> None:
    """The P3 move to ``ui.marks`` left the old names bound in the old modules."""
    from meshterm.ui.mapcanvas import RGB, parse_hex
    from meshterm.ui.marks import NODE_MARK, REPEATER_MARK, SELF_MARK, UNKNOWN_MARK
    from meshterm.ui.pathgraph import DST_NODE, SRC_NODE, GlyphOf, LabelOf, LabelRgbOf

    assert (SELF_MARK, REPEATER_MARK, NODE_MARK, UNKNOWN_MARK) == (
        SELF_MARK,
        REPEATER_MARK,
        NODE_MARK,
        UNKNOWN_MARK,
    )
    assert parse_hex("#facc15") == (0xFA, 0xCC, 0x15)
    assert RGB is not None and SRC_NODE and DST_NODE
    assert GlyphOf is not None and LabelOf is not None and LabelRgbOf is not None
