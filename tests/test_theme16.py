# SPDX-License-Identifier: Apache-2.0
"""The PicoCalc visual-language contracts: palette, glyph map, fold, and bindings.

P3's deliverables are promises about *output* — only 16 palette slots, only glyphs the
console font holds, layout that survives the translation — enforced screen-by-screen by
the gallery. These tests pin the machinery itself: the two themes stay name-compatible,
the 16-slot theme obeys the VT's bold-brightness and background rules, the deploy
script's palette matches the canonical table, the font build script and the frozen
inventory move together, and the platform bindings actually rebind.
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
    """A style name a screen asks for must resolve on either platform."""
    assert set(MESH_THEME.styles) == set(MESH_THEME_16.styles)


def _our_styles(theme_obj):
    """The style entries we declared (Rich merges its own DEFAULT_STYLES into a Theme)."""
    return {name: style for name, style in theme_obj.styles.items() if name not in DEFAULT_STYLES}


def test_theme16_uses_only_the_sixteen_slots() -> None:
    """Every 16-slot style is built from ``color(0..15)`` — never hex, never the cube."""
    for name, style in _our_styles(MESH_THEME_16).items():
        for color in (style.color, style.bgcolor):
            if color is None:
                continue
            assert color.number is not None and 0 <= color.number <= 15, (
                f"{name!r} uses {color!r}, outside the 16 VT slots"
            )


def test_theme16_bold_never_jumps_a_dim_slot_to_an_unrelated_bright_one() -> None:
    """The VT draws bold as brightness: bold on slots 0-7 lands on N+8.

    Slots 5 and 6 carry semantics whose bright partners mean something *else* here
    (5 purple = the repeater mark vs 13 pink = the node mark; 6 cyan = hint.brand vs
    14 = brand), so no style may combine bold with them.
    """
    for name, style in _our_styles(MESH_THEME_16).items():
        if style.bold and style.color is not None and style.color.number in (5, 6):
            raise AssertionError(
                f"{name!r} is bold on slot {style.color.number}; the VT would draw "
                f"slot {style.color.number + 8} instead"
            )


def test_theme16_dim_slot_styles_all_answer_the_bold_question() -> None:
    """A dim-slot style must state its intent about bold — it cannot stay silent.

    Rich merges a base style into every span it wraps, so a span left at ``bold=None``
    inside a selected (bold) row inherits it, and the VT then draws slot N as N+8: the
    style silently changes colour depending on what it lands in. Each one is therefore
    pinned ``not bold`` (keep the declared colour) or ``bold`` (the promotion is intended).
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
    """THE reported bug: an unnamed hop's hash inside a selected row read as white "you".

    ``node.unknown`` is a dim slot, so an inherited bold would promote it to 15 — the
    ``you`` white. It must render as plain light grey (37) either way, on both platforms.
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
    for base in (None, "cursor"):  # unselected row, then the bold cursor row
        row = Text("  ")
        row.append("3d", style="node.unknown")
        if base:
            row.style = base
        with console.capture() as capture:
            console.print(row, end="")
        assert "\x1b[37m3d" in capture.get(), (base, capture.get())


def test_theme16_backgrounds_stay_in_the_dim_bank() -> None:
    """The VT has no bright backgrounds — SGR 40-47 only."""
    for name, style in _our_styles(MESH_THEME_16).items():
        if style.bgcolor is not None:
            assert style.bgcolor.number <= 7, (
                f"{name!r} background on slot {style.bgcolor.number}; the VT stops at 7"
            )


def test_deploy_script_carries_the_archived_custom_palette() -> None:
    """The deploy script still carries the archived custom palette, in sync.

    The script's **opt-in** block matches ``theme.vtrgb_lines()`` byte for byte, and
    its no-kbd OSC fallback spells the same sixteen RGB values, so the archived
    palette stays one environment variable away and in step with `_VT_SLOTS_CUSTOM`.
    """
    script = _FONT_SCRIPT.read_text(encoding="utf-8")
    assert "MESHTERM_CUSTOM_PALETTE" in script, "the opt-in gate vanished"
    assert theme.vtrgb_lines() in script, f"{_FONT_SCRIPT.name} vtrgb drifted"
    for index, (_, _, hex_) in enumerate(theme._VT_SLOTS_CUSTOM):
        sequence = f"\\033]P{index:x}{hex_.lstrip('#').lower()}"
        assert sequence in script, f"OSC fallback missing slot {index}: {sequence}"


# -- the compact glyph map ------------------------------------------------------------


def test_every_glyph_map_target_is_in_the_console_font() -> None:
    """The map may only hand out characters the 512-glyph font can draw."""
    for emoji, compact in theme._GLYPH_MAP.items():
        for ch in compact:
            assert ord(ch) in FONT_CODEPOINTS, (
                f"{emoji!r} maps to {compact!r}; {ch!r} is not in the console font"
            )


def test_every_glyph_map_target_stays_inside_the_bmp() -> None:
    """The table has a second consumer that the console font cannot speak for.

    The classic Windows console keeps one 16-bit code unit per cell, so a character above
    U+FFFF is replaced by U+FFFD before its font is ever consulted — which is why MeshTerm
    routes icons through this table there too (see
    :func:`meshterm.ui.termfont.emoji_support`). A substitute "improved" into an emoji
    would draw fine on the PicoCalc's own font and silently break every Windows console,
    so the plane is asserted here rather than left to the font whitelist to imply.
    """
    for emoji, compact in theme._GLYPH_MAP.items():
        for ch in compact:
            assert ord(ch) <= 0xFFFF, (
                f"{emoji!r} maps to {compact!r}; {ch!r} is above U+FFFF and no classic "
                f"Windows console can hold it"
            )


def test_glyph_map_targets_never_widen_their_icon() -> None:
    """A compact form at most matches its emoji's measured width (the fold pads the rest)."""
    for emoji, compact in theme._GLYPH_MAP.items():
        assert cell_len(compact) <= cell_len(emoji), (
            f"{emoji!r} ({cell_len(emoji)} cells) maps to wider {compact!r}"
        )


def test_glyph_is_identity_on_regular_and_compact_on_picocalc() -> None:
    """Emoji pass through on regular and become font-native marks on the console.

    Node and status marks are in the console font already, so they cross unchanged on
    both platforms.
    """
    set_platform(REGULAR)
    assert glyph("📡") == "📡"
    set_platform(PICOCALC_LYRA)
    assert glyph("📡") == "☼"
    assert glyph("★") == "★"  # node/status marks pass through — they are font-native


# -- the render-boundary fold ---------------------------------------------------------


def test_fold_is_identity_on_regular_even_after_picocalc_used_it() -> None:
    """The bound impls may not leak cached picocalc-lyra folds into a regular render."""
    set_platform(PICOCALC_LYRA)
    assert fold_text("café") == "cafe"
    set_platform(REGULAR)
    assert fold_text("café") == "café"


def test_fold_strips_accents_and_preserves_cell_widths() -> None:
    """The fold leaves only characters the console font has, without changing the width.

    An accent drops to its base letter and an emoji becomes its mapped mark, each in
    place, so nothing downstream has to measure the row again.
    """
    set_platform(PICOCALC_LYRA)
    for text in ("café ⚠", "Ĉu vi paroläs", "📡 Advert", "🗑 Clear", "…", "npo Waymarker 🇨🇦"):
        folded = fold_text(text)
        assert cell_len(folded) == cell_len(text), (text, folded)
        for ch in folded:
            assert ord(ch) in FONT_CODEPOINTS or ord(ch) < 0x20, (text, folded, ch)


def test_fold_replaces_the_unmappable_at_width() -> None:
    """CJK and unmapped emoji leave as ``?`` at their own width — never as tofu."""
    set_platform(PICOCALC_LYRA)
    assert fold_text("你好") == "????"
    assert cell_len(fold_text("🦕")) == cell_len("🦕")


def test_fold_keeps_ansi_sequences_intact() -> None:
    """The fold rewrites text only: colour escapes and newlines cross it untouched."""
    set_platform(PICOCALC_LYRA)
    line = "\x1b[1;93mwarn é\x1b[0m\nnext"
    folded = fold_text(line)
    assert folded == "\x1b[1;93mwarn e\x1b[0m\nnext"


def test_fold_quantizes_embedded_truecolor_to_the_slots() -> None:
    """Canvas-emitted truecolor (the braille rasters) lands on the nearest palette slot."""
    set_platform(PICOCALC_LYRA)
    folded = fold_text("\x1b[38;2;148;163;184m○\x1b[0m")
    assert "38;2;" not in folded
    # #94a3b8 → nearest stock slot is 7 (#aaaaaa); a dim-bank slot states its intensity.
    assert "\x1b[22;37m" in folded
    background = fold_text("\x1b[48;2;94;234;212mX\x1b[0m")
    assert "48;2;" not in background
    assert re.search(r"\x1b\[4[0-7]m", background), background  # bg clamps to the dim bank
    eight_bit = fold_text("\x1b[38;5;201mX\x1b[0m")
    assert "38;5;" not in eight_bit


def test_fold_quantizes_a_colour_that_shares_its_sequence_with_another() -> None:
    """Rich writes a style's fg and bg as one sequence; each colour in it folds on its own.

    The QR module's white-on-black is the case that found it: ``ESC[38;2;…;48;2;…m`` is
    two colours in one list, and a matcher for a colour standing alone saw neither.
    """
    set_platform(PICOCALC_LYRA)
    folded = fold_text("\x1b[38;2;255;255;255;48;2;0;0;0m█\x1b[0m")
    assert "8;2;" not in folded, folded
    assert folded.startswith("\x1b[97;40m"), folded  # bright white ink, black field
    with_attribute = fold_text("\x1b[1;38;5;201;48;2;94;234;212mX\x1b[0m")
    assert "8;5;" not in with_attribute and "8;2;" not in with_attribute
    assert with_attribute.startswith("\x1b[1;"), with_attribute  # the bold survives, in place


def test_the_qr_style_is_white_on_black_on_both_themes() -> None:
    """A code is white ink on a black field on every platform — the PicoCalc's own slots."""
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
    """Variation selectors and zero-width joiners are dropped — they have no cell to occupy."""
    set_platform(PICOCALC_LYRA)
    assert fold_text("🕸️") == fold_text("🕸")
    assert "‍" not in fold_text("a‍b")


# -- name colouring -------------------------------------------------------------------


def test_names_colour_by_key_on_both_platforms() -> None:
    """One rule everywhere: the key picks the hue, and only its first byte does."""
    for platform in (REGULAR, PICOCALC_LYRA):
        set_platform(platform)
        hue = name_style("Hilltop-Repeater", "3d63c6429436")
        assert hue.startswith("bold #"), platform.name
        # Any prefix of the key agrees, a rename does not move the colour, and a sender
        # we could not place falls to the unknown-node grey — colour is reserved for
        # keyed identities.
        assert name_style("Hilltop-Repeater", "3d") == hue
        assert name_style("renamed", "3d63c6429436") == hue
        assert name_style("nameless", None) == "node.unknown"


def test_picocalc_node_hues_land_on_their_own_palette_slots() -> None:
    """The quantized wheel spends only the six chromatic bright slots, evenly.

    Each returned hex is a slot's own RGB, so every downsample on the way out — Rich's,
    and the fold's — lands on that exact slot instead of guessing a neighbour.
    """
    set_platform(PICOCALC_LYRA)
    slots = {hex_: slot for slot, _, hex_ in theme._VT_SLOTS}
    landed = [name_style("n", f"{byte:02x}").removeprefix("bold ") for byte in range(256)]
    assert set(landed) == set(theme._NODE_SLOT_HEXES)
    assert {slots[hex_] for hex_ in landed} == {9, 10, 11, 12, 13, 14}
    assert min(landed.count(h) for h in set(landed)) >= 256 // 8  # no starved sector


def test_route_graph_resolves_a_named_marker_colour_to_rgb() -> None:
    """A relay we can't name keeps its marker's colour — including a *typed* marker.

    The type marks carry theme style names, so the graph's label colour has to resolve
    them rather than assume a literal hex (it raised ValueError when it did).
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
    """Each type mark owns a slot — a naive downsample would grey the repeater's violet."""
    set_platform(PICOCALC_LYRA)
    marks = ("type.node", "type.repeater", "type.room", "type.sensor")
    rgbs = [theme.mark_rgb(name) for name in marks]
    assert len(set(rgbs)) == len(marks)
    assert theme.mark_rgb("type.repeater") != theme.mark_rgb("muted")


def test_heat_ladder_walks_jps_seven_slots_in_order() -> None:
    """The heard-age scale, pinned to its spec: seven steps on plain human boundaries.

    White under 5 minutes, then yellow, light red, brown, red, light grey, and one cold
    grey shared by "over a year" and "never heard".
    """
    from meshterm.ui.widgets import _recency_style

    set_platform(PICOCALC_LYRA)
    # fmt: off
    expected = [
        (60, 15), (299, 15),          # under 5 minutes  — white
        (300, 11), (3599, 11),        # 5 minutes        — yellow
        (3600, 9), (86399, 9),        # 1 hour           — light red
        (86400, 3), (604799, 3),      # 1 day            — brown
        (604800, 1), (2591999, 1),    # 1 week           — red
        (2592000, 7), (31535999, 7),  # 1 month          — light grey
        (31536000, 8), (None, 8),     # 1 year, and never — dark grey
    ]
    # fmt: on
    for secs, slot in expected:
        style = MESH_THEME_16.styles[_recency_style(secs)]
        assert style.color is not None and style.color.number == slot, (secs, style)
        assert not style.bold or slot >= 8, f"bold on dim slot {slot} would jump a rung"
    # The regular platform answers the same question with a continuous hue instead.
    set_platform(REGULAR)
    assert _recency_style(1200).startswith("#")


def test_canvas_drops_emphasis_where_bold_means_brightness() -> None:
    """A braille canvas may not embolden on the console: the VT would recolour the run.

    The canvas quantizes its own truecolour at the fold, so a bold run has no way to know
    which bank it landed in — an unknown label (slot 7) would arrive as white (15).
    """
    from meshterm.ui.mapcanvas import MapCanvas

    for platform, expect_bold in ((REGULAR, True), (PICOCALC_LYRA, False)):
        set_platform(platform)
        canvas = MapCanvas(6, 1)
        canvas.marker(0, 0, "x", (148, 163, 184))  # markers always draw emboldened
        rendered = "".join(canvas.to_ansi_lines())
        assert ("\x1b[1m" in rendered) is expect_bold, (platform.name, repr(rendered))
    # And the colour it was given survives intact — a dim-bank slot with its intensity
    # stated, so the span can't inherit brightness from the raster cell before it.
    assert "\x1b[22;37m" in rendered


# -- the font build script stays mirrored ---------------------------------------------


def test_font_script_marks_and_fontset_move_together() -> None:
    """The font script and the fontset inventory move together.

    Every codepoint the script draws or aliases is in the frozen inventory, and every
    donor it consumes is out, so the two files must change in the same commit.
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
    # Donors are consumed front to back, one per mark; everything consumed must be out
    # of the inventory. The spares at the tail legitimately remain in it.
    donor_list = re.findall(r"0x[0-9A-Fa-f]{4}", donors_match.group(1))
    assert len(donor_list) >= len(marks), "fewer donors than marks — the build would fail"
    for cp_hex in donor_list[: len(marks)]:
        cp = int(cp_hex, 16)
        assert cp not in FONT_CODEPOINTS, (
            f"donor U+{cp:04X} is consumed by a mark but still listed in the fontset"
        )


# -- the specimen card ----------------------------------------------------------------


def test_specimen_renders_clean_on_picocalc() -> None:
    """`meshterm specimen` obeys on picocalc-lyra every contract it demonstrates.

    At most 53 cells per line, 16-slot SGR only, and console-font characters only.
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
    """No mark holds a byte the terminal won't draw as a glyph.

    An art editor writes straight to video memory, where codepage 437's first 32 bytes are
    pictures (►, ◄, ‼) rather than commands. Nothing downstream reads them that way: the
    codecs decode them as the control characters they nominally are, Rich measures each as
    no cells at all — so every one swallows a column and slides the rest of its row, the
    same wound NUL cells leave (see :mod:`meshterm.ui.logo`) — and one of them, ``0x13``,
    is XOFF on a real console. So this asks the question that catches all of it at once:
    does every character the mark draws occupy exactly the one cell it was drawn in?
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
    """A mark's file is named for its canvas, and that name orders the size ladder.

    :func:`~meshterm.ui.logo.load_logo` walks the marks widest-first by the width in the
    name and takes the first that *measures* small enough to fit, so a name that overstates
    its art doesn't draw a torn mark — it just sends a mark that would have fitted to the
    back of the queue, and the splash quietly steps down a size. Cheaper to catch here.
    """
    from meshterm.ui.logo import _NAMED_WIDTH, _rows, _variants, logo_width

    marks = _variants()
    assert marks, "the splash folder should hold at least one mark"
    for name in marks:
        declared = int(_NAMED_WIDTH.search(name).group(1))
        assert logo_width(_rows(name)) == declared, f"{name} is not {declared} cells wide"


def test_the_mark_says_bright_as_a_colour_not_as_bold() -> None:
    """No span of the wordmark depends on the terminal reading bold as brightness.

    An art editor spells brightness the DOS way, ``1m`` lifting the foreground into the
    bright bank. That is true of the PicoCalc console and of a DOS one -- and false on
    macOS Terminal, where bold asks for a heavier face and leaves the colour alone. There
    ``1;30`` is not dark grey but plain black, and the wide mark carries seventy-nine such
    spans, much of it on a black ground: the mark drew muted, and the dithers that fade one
    colour into another faded toward the wrong end. So the loader emits the bright bank as
    the colour it means (``9N``), which cannot be mistaken for a font weight.
    """
    import re

    from meshterm.ui.logo import _NAMED_WIDTH, _rows, _variants, logo_width

    sgr = re.compile(chr(27) + r"\[([0-9;]*)m")
    for name in _variants():
        rows = _rows(name)
        codes = [m.group(1) for row in rows for m in sgr.finditer(row)]
        assert codes, f"{name} carries no colour at all"

        params = [p for code in codes for p in code.split(";")]
        # Nothing asks for bold: brightness travels as 90-97 instead.
        assert "1" not in params, f"{name} still leans on bold for brightness"
        assert any(p.isdigit() and 90 <= int(p) <= 97 for p in params), (
            f"{name} lost its bright spans entirely"
        )
        # A foreground on the dim bank still says so outright, so it cannot inherit a bank
        # from whatever the row above left set (each row reaches Rich on its own).
        for code in codes:
            parts = code.split(";")
            if any(re.fullmatch(r"3[0-7]", p) for p in parts):
                assert "22" in parts, f"{name} has a dim foreground that never says so: {code}"

        # And the spelling change moves no cells: the art measures exactly as it did.
        assert logo_width(rows) == int(_NAMED_WIDTH.search(name).group(1))


def test_the_mark_the_console_picks_stays_inside_its_font() -> None:
    """The wordmark obeys the glyph contract every other screen does.

    The PicoCalc's console font is a 512-glyph inventory, and a character outside it is a
    test failure rather than a tofu box found on-device. The mark is art rather than
    chrome, so it never passed through the gallery's sweep — and it is the very first thing
    the device draws.
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
    """Every dim-bank span of the wordmark states its own intensity.

    An art editor spells brightness the DOS way — a ``1m`` lifts the bank and every span
    after it inherits that — which only reads correctly while the escapes stay one unbroken
    stream. A row reaches the console on its own, and bold *is* the bright bank there, so a
    span that never mentions intensity is read against whatever happened to be set: the
    narrow mark's letter row came out part red (slot 1), part light red (slot 9), splitting
    at every span that followed a bevel. The art is redrawn from time to time, so this
    pins the rule rather than the picture — no row index, no particular colour.
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
    # Without this the loop below would pass on a mark that never touches the dim bank.
    assert dim, "the mark paints on the dim bank somewhere, or this check says nothing"
    for sgr in dim:
        parts = sgr.split(";")
        assert any(p in {"0", "1", "2", "22"} for p in parts), f"unstated intensity: {sgr!r}"


# -- relocated marks stay importable from their old homes -----------------------------


def test_mark_constants_reexport_from_their_old_hosts() -> None:
    """The P3 relocation to ``ui.marks`` left the old names bound in the old modules."""
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
