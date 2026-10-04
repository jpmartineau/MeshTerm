# SPDX-License-Identifier: Apache-2.0
"""The Cardputer's emulator: its terminal, keys, font, pixels, and a whole run."""

from __future__ import annotations

import threading
import time

from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.keys import Keys

from meshterm.emulator.font import CELL_H, MARKS, MISSING, build_font, load_bdf
from meshterm.emulator.framebuffer import KeyState
from meshterm.emulator.keys import Key, encode
from meshterm.emulator.raster import PANEL_H, PANEL_W, Raster, rgb565
from meshterm.emulator.run import panel_output, run
from meshterm.emulator.vt import BOLD, REVERSE, UNDERLINE, Terminal
from meshterm.emulator.window import key_from_tk
from meshterm.ui.tui.fkeys import CARDPUTER_ZERO_DECK

_BDF = """STARTFONT 2.1
FONT test
SIZE 12 72 72
FONTBOUNDINGBOX 6 12 0 -2
FONT_ASCENT 10
FONT_DESCENT 2
CHARS 1
STARTCHAR A
ENCODING 65
BBX 6 12 0 -2
BITMAP
00
00
70
88
88
88
F8
88
88
88
00
00
ENDCHAR
ENDFONT
"""


def _parsed(data: str) -> list[Keys | str]:
    keys: list[Keys | str] = []
    parser = Vt100Parser(lambda press: keys.append(press.key))
    parser.feed(data)
    parser.flush()
    return keys


# --- the terminal ---------------------------------------------------------------------


def test_text_lands_where_the_cursor_is_and_wraps_at_the_edge() -> None:
    """Printing starts at the cursor and wraps onto the next row at the right edge."""
    term = Terminal(6, 3)
    term.feed("\x1b[2;3Habcdef")
    assert term.line(1) == "  abcd"
    assert term.line(2) == "ef    "


def test_erase_and_cursor_moves() -> None:
    """Erase-to-end and erase-to-start clear what they name and nothing else."""
    term = Terminal(8, 2)
    term.feed("abcdefgh\r\nijklmnop\x1b[1;4H\x1b[K\x1b[2;2H\x1b[1K")
    assert term.text() == "abc     \n  klmnop"


def test_sgr_in_every_colour_depth() -> None:
    """16-colour, 256-colour and truecolor SGR, with either separator, and the flags."""
    term = Terminal(8, 1, palette=[(i, i, i) for i in range(16)])
    term.feed("\x1b[31ma\x1b[38;5;196mb\x1b[38;2;1;2;3mc\x1b[38:2::4:5:6md\x1b[1;4;7me\x1b[0mf")
    styles = [style for _, style in term.screen[0][:6]]
    assert styles[0].fg == (1, 1, 1)  # palette slot 1
    assert styles[1].fg == (255, 0, 0)  # the 256-colour cube's pure red
    assert styles[2].fg == (1, 2, 3) and styles[3].fg == (4, 5, 6)
    assert styles[4].flags == BOLD | UNDERLINE | REVERSE
    assert styles[5].fg is None and styles[5].flags == 0


def test_unknown_sequences_are_skipped_whole() -> None:
    """A sequence the parser doesn't model leaves nothing of itself on screen."""
    term = Terminal(10, 1)
    term.feed("\x1b]0;a title\x07\x1b[?2004h\x1b[>4;2m\x1bPq#0;\x1b\\x\x1b(By")
    assert term.line(0) == "xy        "


def test_the_alternate_screen_keeps_the_main_one() -> None:
    """Leaving the alternate screen brings the main one back as it was."""
    term = Terminal(4, 1)
    term.feed("main\x1b[?1049h\x1b[2J\x1b[Halt")
    assert term.line(0) == "alt "
    term.feed("\x1b[?1049l")
    assert term.line(0) == "main"


def test_only_changed_rows_are_dirty() -> None:
    """A one-cell change marks one row for redrawing."""
    term = Terminal(4, 3)
    term.take_dirty()
    term.feed("\x1b[3;1Hx")
    assert term.take_dirty() == {2}


def test_prompt_toolkit_draws_into_it() -> None:
    """The output the host hands prompt_toolkit really does land in the grid."""
    term = Terminal(20, 2)
    frames: list[int] = []
    output = panel_output(term, threading.Lock(), lambda: frames.append(1))
    output.cursor_goto(2, 4)  # 1-based, as a terminal counts
    output.write("hello")
    output.flush()
    assert term.line(1).startswith("   hello") and frames
    assert output.get_size().columns == 20 and output.get_size().rows == 2


# --- keys -----------------------------------------------------------------------------


def test_the_lane_keys_reach_prompt_toolkit_as_the_deck_expects() -> None:
    """Fn+4..8 parse as F4-F8 and Shift with them as F16-F20, the Cardputer deck's banks."""
    plain = [_parsed(encode(Key(name=f"f{n}")))[0] for n in range(4, 9)]
    shifted = [_parsed(encode(Key(name=f"f{n}", shift=True)))[0] for n in range(4, 9)]
    assert [k.value for k in plain] == [f"f{n}" for n in CARDPUTER_ZERO_DECK.keys]
    assert [k.value for k in shifted] == [f"f{n}" for n in CARDPUTER_ZERO_DECK.shift_keys]


def test_modified_navigation_and_control_letters() -> None:
    """Ctrl+PgUp (the section jump), arrows, Shift+Tab, and the C0 control letters."""
    assert _parsed(encode(Key(name="pageup", ctrl=True))) == [Keys.ControlPageUp]
    assert _parsed(encode(Key(name="up"))) == [Keys.Up]
    assert _parsed(encode(Key(name="tab", shift=True))) == [Keys.BackTab]
    assert encode(Key(text="q", ctrl=True)) == "\x11"
    assert encode(Key(text="w", ctrl=True)) == "\x17"
    assert encode(Key(name="escape")) == "\x1b"
    assert encode(Key(name="backspace")) == "\x7f"


def test_tk_events_become_keys() -> None:
    """The window reads Shift, Ctrl and text off Tk's events."""
    assert key_from_tk("F4", "", 0x0001) == Key(name="f4", shift=True)
    assert key_from_tk("q", "\x11", 0x0004) == Key(text="q", ctrl=True)
    assert key_from_tk("a", "a", 0) == Key(text="a")
    assert key_from_tk("Shift_L", "", 0) is None


def test_the_device_keyboard_types_what_its_keycaps_say() -> None:
    """Letters and digits as printed, Sym's placeholder codes as M5's keymap names them."""
    keys = KeyState()
    assert keys.event(30, 1) == Key(text="a")  # KEY_A
    keys.event(42, 1)  # Shift down — the driver holds it for a sticky Shift too
    assert keys.event(30, 1) == Key(text="A")
    assert keys.event(62, 1) == Key(name="f4", shift=True)  # Shift+Fn+4
    keys.event(42, 0)
    assert keys.event(26, 1) == Key(text="!")  # Sym+1 arrives as KEY_LEFTBRACE
    assert keys.event(93, 1) == Key(text="?")  # Sym+M
    assert keys.event(389, 1) is None  # the Fn key itself (KEY_DVD) types nothing
    assert keys.event(30, 0) is None  # a release types nothing


# --- the font and the pixels ------------------------------------------------------------


def test_a_bdf_glyph_lands_in_the_cell(tmp_path) -> None:
    """A BDF glyph is set into the 6x12 cell where its bounding box says."""
    path = tmp_path / "t.bdf"
    path.write_text(_BDF)
    glyph = load_bdf(path)[65]
    assert len(glyph) == CELL_H
    assert glyph[2] == 0x70 and glyph[6] == 0xF8


def test_meshterm_marks_draw_over_the_base_and_gaps_show() -> None:
    """MeshTerm's marks win over the base font, and a missing glyph draws as a box."""
    font = build_font({0x2605: bytes(12)})
    assert font.glyph("★") == MARKS[0x2605]  # the mark wins over the base's glyph
    assert font.glyph("⋯") == MARKS[0x2026]  # aliased, as on the PicoCalc
    assert font.glyph("一") == MISSING


def test_braille_draws_as_solid_tiles_with_no_gap() -> None:
    """Braille is pixels here, as on the PicoCalc: each dot a 3x3 tile, the cell tiled whole.

    The base's dotted braille loses to the tiles, and so does the bold smear, which would
    only close the seam between a cell's two columns.
    """
    dotted = bytes([0x6C, 0x6C, 0, 0x6C, 0x6C, 0, 0x6C, 0x6C, 0, 0x6C, 0x6C, 0])
    font = build_font({0x28FF: dotted})
    assert font.glyph("⣿") == bytes([0xFC] * 12)  # every dot: the cell, solid
    assert font.glyph("⣿", bold=True) == bytes([0xFC] * 12)
    assert font.glyph("⡇") == bytes([0xE0] * 12)  # the left column: three pixels wide
    assert font.glyph("⠉") == bytes([0xFC] * 3 + [0] * 9)  # the top row: three tall
    assert font.glyph("⢀", bold=True) == bytes([0] * 9 + [0x1C] * 3)  # dot 8, unsmeared


def test_the_raster_draws_a_cell_in_its_colours() -> None:
    """A cell's ink and paper land in the right pixels, and only dirty rows redraw."""
    term = Terminal(53, 14)
    font = build_font({0x41: bytes([0, 0, 0x70, 0x88, 0x88, 0x88, 0xF8, 0x88, 0x88, 0x88, 0, 0])})
    raster = Raster(term, font, pack=rgb565, default_bg=(0, 0, 0))
    term.feed("\x1b[38;2;255;255;255m\x1b[48;2;0;0;255mA")
    bands = raster.update()
    assert bands == [(raster.top, raster.top + 12 * 14)]  # the first paint is every row
    assert len(raster.pixels) == PANEL_W * PANEL_H * 2

    def pixel(x: int, y: int) -> bytes:
        at = (raster.top + y) * raster.stride + (raster.left + x) * 2
        return bytes(raster.pixels[at : at + 2])

    assert pixel(1, 2) == rgb565((255, 255, 255))  # ink: the A's top bar
    assert pixel(0, 2) == rgb565((0, 0, 255))  # paper beside it
    term.feed("\x1b[14;1Hz")
    assert raster.update() == [(raster.top + 13 * 12, raster.top + 14 * 12)]


# --- a whole run ----------------------------------------------------------------------


def test_meshterm_runs_inside_the_host(monkeypatch) -> None:
    """The ordinary CLI, hosted: the menu draws at 53x14 with the Cardputer's lane, ^Q^Q leaves.

    No header row: the title bar is the top row, and the menu's title is the wordmark.
    """
    from meshterm import __version__
    from meshterm.ui import menu

    # Quitting arms a watchdog that hard-exits the process if teardown wedges — right for
    # the host, which ends with the TUI, but fatal to the test run it is hosted in here.
    monkeypatch.setattr(menu, "_arm_exit_watchdog", lambda *args, **kwargs: None)
    seen: dict[str, str] = {}
    wordmark = f"MeshTerm v{__version__}"

    class Headless:
        def __init__(self, terminal, lock, type_text):
            self._terminal, self._lock = terminal, lock

            def drive():
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    with self._lock:
                        text = terminal.text()
                    if text.startswith("↑↓") and wordmark in text.splitlines()[0]:
                        seen["menu"] = text
                        break
                    time.sleep(0.1)
                type_text("\x11")
                time.sleep(0.3)
                type_text("\x11")

            threading.Thread(target=drive, daemon=True).start()

        def frame_ready(self):
            pass

        def close(self):
            pass

    assert run(["--mock"], Headless) == 0
    menu = seen["menu"].splitlines()
    assert len(menu) == 14 and all(len(line) == 53 for line in menu)
    assert "Quit?" in menu[-1]  # the main menu's lane, drawn by the Cardputer deck


def test_the_cardputer_inventory_is_what_the_host_draws() -> None:
    """``fontset.CARDPUTER_ZERO_CODEPOINTS`` holds every mark, and the installed font exactly."""
    from meshterm.emulator.font import ALIASES, BRAILLE, font_dir, load_bdf
    from meshterm.ui.fontset import CARDPUTER_ZERO_CODEPOINTS

    ours = set(MARKS) | set(ALIASES) | set(BRAILLE)
    assert ours <= CARDPUTER_ZERO_CODEPOINTS
    installed = font_dir() / "ter-u12n.bdf"
    if not installed.is_file():
        return  # no Terminus on this machine (CI): the marks are the part that is ours
    drawn = {cp for cp in load_bdf(installed) if cp >= 0x20} | ours
    assert drawn == CARDPUTER_ZERO_CODEPOINTS


# --- the devices the emulator stands in for ------------------------------------------------


def test_every_device_is_named_for_its_platform() -> None:
    """``meshterm emulate picocalc-lyra`` and ``--platform picocalc-lyra`` say one device."""
    import pytest

    from meshterm.emulator.devices import DEVICES, device
    from meshterm.platforms import CARDPUTER_ZERO, PICOCALC_LYRA

    assert DEVICES["cardputer-zero"].platform is CARDPUTER_ZERO
    assert DEVICES["picocalc-lyra"].platform is PICOCALC_LYRA
    assert device(" PicoCalc-Lyra ").id == "picocalc-lyra"
    with pytest.raises(ValueError, match="cardputer-zero, picocalc-lyra"):
        device("picocalc")


def test_the_picocalc_s_shift_bank_arrives_as_f6_to_f10() -> None:
    """Its keyboard sends F6–F10 for Shift+F1–F5; the Cardputer's sends a shifted F4–F8."""
    from meshterm.emulator.devices import CARDPUTER_ZERO_DEVICE, PICOCALC_LYRA_DEVICE

    picocalc = PICOCALC_LYRA_DEVICE.translate
    assert encode(picocalc(Key(name="f1", shift=True))) == encode(Key(name="f6"))
    assert encode(picocalc(Key(name="f5", shift=True))) == encode(Key(name="f10"))
    assert picocalc(Key(name="f1")) == Key(name="f1")  # unshifted, untouched
    shifted_f4 = Key(name="f4", shift=True)
    assert CARDPUTER_ZERO_DEVICE.translate(shifted_f4) == shifted_f4


def test_a_console_draws_bold_as_bright_from_the_top_left_corner() -> None:
    """The PicoCalc's Linux console: grid at (0, 0), bold a dim colour's bright twin.

    And never a heavier glyph — a console font has no bold face — where the Cardputer's
    panel keeps its centred grid and bold weight.
    """
    from meshterm.emulator.run import _palette

    palette = _palette()
    font = build_font(
        {0x41: bytes([0, 0, 0x70, 0x88, 0x88, 0x88, 0xF8, 0x88, 0x88, 0x88, 0, 0])},
        {0x41: bytes([0, 0, 0xF8] * 4)},
    )
    term = Terminal(2, 1, palette=palette)
    term.feed("\x1b[1;31mA")  # bold, dim red (slot 1)
    console = Raster(term, font, width=12, height=12, top_left=True, bold_is_bright=True)
    console.redraw()
    assert (console.left, console.top) == (0, 0)
    lit = {bytes(console.pixels[i : i + 3]) for i in range(0, len(console.pixels), 3)}
    assert bytes(palette[9]) in lit  # the bright twin (slot 9) …
    assert bytes(palette[1]) not in lit  # … never the dim red it was asked for
    # The regular glyph's row 2 (0x70) is drawn, not the bold one's (0xF8).
    row2 = console.pixels[2 * 12 * 3 : 2 * 12 * 3 + 6 * 3]
    assert bytes(row2[:3]) != bytes(palette[9])  # the leftmost pixel of 0x70 is off

    term.feed("\x1b[2J\x1b[H\x1b[1;31mA")
    panel = Raster(term, font, width=16, height=14)
    panel.redraw()
    assert (panel.left, panel.top) == (2, 1)  # centred
    assert bytes(palette[1]) in {
        bytes(panel.pixels[i : i + 3]) for i in range(0, len(panel.pixels), 3)
    }


def test_meshterm_runs_as_a_picocalc_inside_the_emulator(monkeypatch) -> None:
    """53x26 with the PicoCalc's header, its F1–F5 lane, drawn by MeshTerm itself.

    The platform is the PicoCalc's in every respect but one: inside the emulator nothing but
    MeshTerm draws it, so it resolves with ``own_display`` set and never offers to move to
    another terminal.
    """
    from meshterm import __version__
    from meshterm.emulator.devices import PICOCALC_LYRA_DEVICE
    from meshterm.platforms import get_platform
    from meshterm.ui import menu

    monkeypatch.setattr(menu, "_arm_exit_watchdog", lambda *args, **kwargs: None)
    seen: dict[str, str] = {}
    wordmark = f"MeshTerm v{__version__}"

    class Headless:
        def __init__(self, terminal, lock, type_text):
            def drive():
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    with lock:
                        text = terminal.text()
                    if text.startswith(wordmark) and "What would you like to do?" in text:
                        seen["menu"] = text
                        seen["platform"] = get_platform()
                        break
                    time.sleep(0.1)
                type_text("\x11")
                time.sleep(0.3)
                type_text("\x11")

            threading.Thread(target=drive, daemon=True).start()

        def frame_ready(self):
            pass

        def close(self):
            pass

    assert run(["--mock"], Headless, PICOCALC_LYRA_DEVICE) == 0
    screen = seen["menu"].splitlines()
    assert len(screen) == 26 and all(len(line) == 53 for line in screen)
    assert screen[-1].startswith("F1") and "F3 Quit?" in screen[-1]  # the PicoCalc's own lane
    assert seen["platform"].name == "picocalc-lyra" and seen["platform"].own_display


def test_emulate_hands_the_global_options_to_the_meshterm_it_runs(monkeypatch, tmp_path) -> None:
    """``meshterm emulate picocalc-lyra --mock``: the window's MeshTerm gets ``--mock``.

    A global is parsed by the outer invocation wherever it was typed, so the emulated one
    would otherwise start without it — and go looking for real hardware.
    """
    from typer.testing import CliRunner

    from meshterm.cli import app
    from meshterm.emulator import __main__ as entry

    calls: list = []
    monkeypatch.setattr(
        entry, "start", lambda name, argv, **kw: calls.append((name, argv, kw)) or 0
    )
    monkeypatch.setenv("MESHTERM_HOME", str(tmp_path))
    result = CliRunner().invoke(app, ["emulate", "picocalc-lyra", "--mock", "--scale", "2"])
    assert result.exit_code == 0, result.output
    ((name, argv, kw),) = calls
    assert name == "picocalc-lyra" and "--mock" in argv and kw["scale"] == 2
    assert "--platform" not in argv  # the emulator names the platform itself
