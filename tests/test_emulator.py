# SPDX-License-Identifier: Apache-2.0
"""Tests for the emulator of the Cardputer: its terminal, keys, font, pixels, and a whole run."""

from __future__ import annotations

import mmap
import os
import threading
import time

import pytest
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.keys import Keys

from meshterm.emulator.font import CELL_H, MARKS, MISSING, build_font, load_bdf
from meshterm.emulator.framebuffer import Framebuffer, KeyState
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
    """The text starts at the cursor and wraps to the next row at the right edge."""
    term = Terminal(6, 3)
    term.feed("\x1b[2;3Habcdef")
    assert term.line(1) == "  abcd"
    assert term.line(2) == "ef    "


def test_erase_and_cursor_moves() -> None:
    """Erase-to-end and erase-to-start clear the cells that they name, and no other cells."""
    term = Terminal(8, 2)
    term.feed("abcdefgh\r\nijklmnop\x1b[1;4H\x1b[K\x1b[2;2H\x1b[1K")
    assert term.text() == "abc     \n  klmnop"


def test_sgr_in_every_colour_depth() -> None:
    """The terminal parses SGR with 16 colours, 256 colours, and truecolor.

    It handles each separator and the flags.
    """
    term = Terminal(8, 1, palette=[(i, i, i) for i in range(16)])
    term.feed("\x1b[31ma\x1b[38;5;196mb\x1b[38;2;1;2;3mc\x1b[38:2::4:5:6md\x1b[1;4;7me\x1b[0mf")
    styles = [style for _, style in term.screen[0][:6]]
    assert styles[0].fg == (1, 1, 1)  # palette slot 1
    assert styles[1].fg == (255, 0, 0)  # the pure red of the 256-colour cube
    assert styles[2].fg == (1, 2, 3) and styles[3].fg == (4, 5, 6)
    assert styles[4].flags == BOLD | UNDERLINE | REVERSE
    assert styles[5].fg is None and styles[5].flags == 0


def test_unknown_sequences_are_skipped_whole() -> None:
    """A sequence that the parser does not model leaves no text on the screen."""
    term = Terminal(10, 1)
    term.feed("\x1b]0;a title\x07\x1b[?2004h\x1b[>4;2m\x1bPq#0;\x1b\\x\x1b(By")
    assert term.line(0) == "xy        "


def test_the_alternate_screen_keeps_the_main_one() -> None:
    """When the terminal leaves the alternate screen, the main screen comes back as it was."""
    term = Terminal(4, 1)
    term.feed("main\x1b[?1049h\x1b[2J\x1b[Halt")
    assert term.line(0) == "alt "
    term.feed("\x1b[?1049l")
    assert term.line(0) == "main"


def test_only_changed_rows_are_dirty() -> None:
    """A change of one cell marks one row to draw again."""
    term = Terminal(4, 3)
    term.take_dirty()
    term.feed("\x1b[3;1Hx")
    assert term.take_dirty() == {2}


def test_prompt_toolkit_draws_into_it() -> None:
    """The output that the host gives to prompt_toolkit puts its text in the grid."""
    term = Terminal(20, 2)
    frames: list[int] = []
    output = panel_output(term, threading.Lock(), lambda: frames.append(1))
    output.cursor_goto(2, 4)  # 1-based, in the same way as a terminal counts
    output.write("hello")
    output.flush()
    assert term.line(1).startswith("   hello") and frames
    assert output.get_size().columns == 20 and output.get_size().rows == 2


# --- keys -----------------------------------------------------------------------------


def test_the_lane_keys_reach_prompt_toolkit_as_the_deck_expects() -> None:
    """Fn+4..8 parse as F4-F8, and Shift with them parses as F16-F20.

    These are the banks of the Cardputer deck.
    """
    plain = [_parsed(encode(Key(name=f"f{n}")))[0] for n in range(4, 9)]
    shifted = [_parsed(encode(Key(name=f"f{n}", shift=True)))[0] for n in range(4, 9)]
    assert [k.value for k in plain] == [f"f{n}" for n in CARDPUTER_ZERO_DECK.keys]
    assert [k.value for k in shifted] == [f"f{n}" for n in CARDPUTER_ZERO_DECK.shift_keys]


def test_modified_navigation_and_control_letters() -> None:
    """The encoder handles Ctrl+PgUp (the section jump), the arrows, and Shift+Tab.

    It also handles the C0 control letters.
    """
    assert _parsed(encode(Key(name="pageup", ctrl=True))) == [Keys.ControlPageUp]
    assert _parsed(encode(Key(name="up"))) == [Keys.Up]
    assert _parsed(encode(Key(name="tab", shift=True))) == [Keys.BackTab]
    assert encode(Key(text="q", ctrl=True)) == "\x11"
    assert encode(Key(text="w", ctrl=True)) == "\x17"
    assert encode(Key(name="escape")) == "\x1b"
    assert encode(Key(name="backspace")) == "\x7f"


def test_tk_events_become_keys() -> None:
    """The window gets Shift, Ctrl, and text from the events of Tk."""
    assert key_from_tk("F4", "", 0x0001) == Key(name="f4", shift=True)
    assert key_from_tk("q", "\x11", 0x0004) == Key(text="q", ctrl=True)
    assert key_from_tk("a", "a", 0) == Key(text="a")
    assert key_from_tk("Shift_L", "", 0) is None


def test_the_device_keyboard_types_what_its_keycaps_say() -> None:
    """The keyboard of the device types the letters and digits that are printed on its keys.

    The placeholder codes of Sym have the names that the keymap of M5 gives.
    """
    keys = KeyState()
    assert keys.event(30, 1) == Key(text="a")  # KEY_A
    keys.event(42, 1)  # Shift down. The driver also holds it for a sticky Shift
    assert keys.event(30, 1) == Key(text="A")
    assert keys.event(62, 1) == Key(name="f4", shift=True)  # Shift+Fn+4
    keys.event(42, 0)
    assert keys.event(26, 1) == Key(text="!")  # Sym+1 arrives as KEY_LEFTBRACE
    assert keys.event(93, 1) == Key(text="?")  # Sym+M
    assert keys.event(389, 1) is None  # the Fn key (KEY_DVD) types nothing
    assert keys.event(30, 0) is None  # a release types nothing


# --- the font and the pixels ------------------------------------------------------------


def test_a_bdf_glyph_lands_in_the_cell(tmp_path) -> None:
    """The loader puts a BDF glyph in the 6x12 cell where its bounding box says."""
    path = tmp_path / "t.bdf"
    path.write_text(_BDF)
    glyph = load_bdf(path)[65]
    assert len(glyph) == CELL_H
    assert glyph[2] == 0x70 and glyph[6] == 0xF8


def test_meshterm_marks_draw_over_the_base_and_gaps_show() -> None:
    """The marks of MeshTerm have priority over the base font.

    A missing glyph draws as a box.
    """
    font = build_font({0x2605: bytes(12)})
    assert font.glyph("★") == MARKS[0x2605]  # the mark has priority over the glyph of the base
    assert font.glyph("⋯") == MARKS[0x2026]  # an alias, as on the PicoCalc
    assert font.glyph("一") == MISSING


def test_braille_draws_as_solid_tiles_with_no_gap() -> None:
    """Braille is pixels here, as on the PicoCalc.

    Each dot is a 3x3 tile, and the tiles fill the cell. The tiles have priority over the
    dotted braille of the base. They also have priority over the bold smear, which would only
    close the seam between the two columns of a cell.
    """
    dotted = bytes([0x6C, 0x6C, 0, 0x6C, 0x6C, 0, 0x6C, 0x6C, 0, 0x6C, 0x6C, 0])
    font = build_font({0x28FF: dotted})
    assert font.glyph("⣿") == bytes([0xFC] * 12)  # each dot: the cell is solid
    assert font.glyph("⣿", bold=True) == bytes([0xFC] * 12)
    assert font.glyph("⡇") == bytes([0xE0] * 12)  # the left column: three pixels wide
    assert font.glyph("⠉") == bytes([0xFC] * 3 + [0] * 9)  # the top row: three pixels tall
    assert font.glyph("⢀", bold=True) == bytes([0] * 9 + [0x1C] * 3)  # dot 8, with no smear


def test_the_raster_draws_a_cell_in_its_colours() -> None:
    """The ink and the paper of a cell go to the correct pixels.

    Only the dirty rows are drawn again.
    """
    term = Terminal(53, 14)
    font = build_font({0x41: bytes([0, 0, 0x70, 0x88, 0x88, 0x88, 0xF8, 0x88, 0x88, 0x88, 0, 0])})
    raster = Raster(term, font, pack=rgb565, default_bg=(0, 0, 0))
    term.feed("\x1b[38;2;255;255;255m\x1b[48;2;0;0;255mA")
    bands = raster.update()
    assert bands == [(raster.top, raster.top + 12 * 14)]  # the first paint has each row
    assert len(raster.pixels) == PANEL_W * PANEL_H * 2

    def pixel(x: int, y: int) -> bytes:
        at = (raster.top + y) * raster.stride + (raster.left + x) * 2
        return bytes(raster.pixels[at : at + 2])

    assert pixel(1, 2) == rgb565((255, 255, 255))  # ink: the top bar of the A
    assert pixel(0, 2) == rgb565((0, 0, 255))  # paper beside it
    term.feed("\x1b[14;1Hz")
    assert raster.update() == [(raster.top + 13 * 12, raster.top + 14 * 12)]


@pytest.mark.skipif(not hasattr(mmap, "MAP_SHARED"), reason="the panel is Linux's")
def test_the_panel_is_written_through_its_mapping(tmp_path, monkeypatch) -> None:
    """The rows go to their stride through the map.

    The panel draws again only what the map makes dirty. A ``pwrite`` to the ``/dev/fb0`` of
    the Cardputer reaches the buffer, but it never reaches the glass. Thus MeshTerm ran
    behind the loading screen of the launcher. A plain file is a stand-in for the device.
    The test makes sure that the bytes go through the mapping, to the correct place.

    The test removes the sysfs read. A Linux CI runner has a real
    ``/sys/class/graphics/fb0``, and its stride is not the stride of this small file. With
    that stride, the map was longer than the file, and ``mmap`` refused it.
    """
    monkeypatch.setattr(Framebuffer, "_sysfs", lambda self, name: None)
    path = tmp_path / "fb0"
    path.write_bytes(bytes(8 * 4))
    fb = Framebuffer(str(path), width=4, height=4)  # no sysfs: the stride is width * 2
    write = os.pwrite
    try:
        os.pwrite = lambda *a: pytest.fail("the panel must not be written with pwrite")  # type: ignore[assignment]
        image = bytes(range(32))
        fb.write(image, 8, 1, 3)
    finally:
        os.pwrite = write  # type: ignore[assignment]
        fb.close()
    assert path.read_bytes() == bytes(8) + image[8:24] + bytes(8)


# --- a whole run ----------------------------------------------------------------------


def test_meshterm_runs_inside_the_host(monkeypatch) -> None:
    """The normal CLI runs in the host. ^Q^Q leaves.

    The menu draws at 53x14 with the lane of the Cardputer. There is no header row. The title
    bar is the top row, and the title of the menu is the wordmark.
    """
    from meshterm import __version__
    from meshterm.ui import menu

    # When the user quits, the app arms a watchdog. It ends the process at once if the
    # teardown does not finish. This is correct for the host, which ends with the TUI. But
    # it would stop the test run that hosts it here.
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
    assert "Quit?" in menu[-1]  # the lane of the main menu, drawn by the Cardputer deck


def test_the_cardputer_inventory_is_what_the_host_draws() -> None:
    """``fontset.CARDPUTER_ZERO_CODEPOINTS`` has each mark, and it is exactly the installed font."""
    from meshterm.emulator.font import ALIASES, BRAILLE, font_dir, load_bdf
    from meshterm.ui.fontset import CARDPUTER_ZERO_CODEPOINTS

    ours = set(MARKS) | set(ALIASES) | set(BRAILLE)
    assert ours <= CARDPUTER_ZERO_CODEPOINTS
    installed = font_dir() / "ter-u12n.bdf"
    if not installed.is_file():
        return  # no Terminus on this machine (CI): the marks are the part that we own
    drawn = {cp for cp in load_bdf(installed) if cp >= 0x20} | ours
    assert drawn == CARDPUTER_ZERO_CODEPOINTS


# --- the devices that the emulator stands in for -------------------------------------------


def test_every_device_is_named_for_its_platform() -> None:
    """``meshterm emulate picocalc-lyra`` and ``--platform picocalc-lyra`` name one device."""
    import pytest

    from meshterm.emulator.devices import DEVICES, device
    from meshterm.platforms import CARDPUTER_ZERO, PICOCALC_LYRA

    assert DEVICES["cardputer-zero"].platform is CARDPUTER_ZERO
    assert DEVICES["picocalc-lyra"].platform is PICOCALC_LYRA
    assert device(" PicoCalc-Lyra ").id == "picocalc-lyra"
    with pytest.raises(ValueError, match="cardputer-zero, picocalc-lyra"):
        device("picocalc")


def test_the_picocalc_s_shift_bank_arrives_as_f6_to_f10() -> None:
    """The keyboard of the PicoCalc sends F6–F10 for Shift+F1–F5.

    The keyboard of the Cardputer sends a shifted F4–F8.
    """
    from meshterm.emulator.devices import CARDPUTER_ZERO_DEVICE, PICOCALC_LYRA_DEVICE

    picocalc = PICOCALC_LYRA_DEVICE.translate
    assert encode(picocalc(Key(name="f1", shift=True))) == encode(Key(name="f6"))
    assert encode(picocalc(Key(name="f5", shift=True))) == encode(Key(name="f10"))
    assert picocalc(Key(name="f1")) == Key(name="f1")  # no Shift, no change
    shifted_f4 = Key(name="f4", shift=True)
    assert CARDPUTER_ZERO_DEVICE.translate(shifted_f4) == shifted_f4


def test_a_console_draws_bold_as_bright_from_the_top_left_corner() -> None:
    """The Linux console of the PicoCalc has its grid at (0, 0).

    Bold is the bright twin of a dim colour. The console never uses a heavier glyph, because
    a console font has no bold face. The panel of the Cardputer keeps its centred grid and
    its bold weight.
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
    assert bytes(palette[9]) in lit  # the bright twin (slot 9)
    assert bytes(palette[1]) not in lit  # not the dim red that the text asked for
    # The console draws row 2 of the regular glyph (0x70), not row 2 of the bold glyph (0xF8).
    row2 = console.pixels[2 * 12 * 3 : 2 * 12 * 3 + 6 * 3]
    assert bytes(row2[:3]) != bytes(palette[9])  # the pixel at the far left of 0x70 is off

    term.feed("\x1b[2J\x1b[H\x1b[1;31mA")
    panel = Raster(term, font, width=16, height=14)
    panel.redraw()
    assert (panel.left, panel.top) == (2, 1)  # centred
    assert bytes(palette[1]) in {
        bytes(panel.pixels[i : i + 3]) for i in range(0, len(panel.pixels), 3)
    }


def test_meshterm_runs_as_a_picocalc_inside_the_emulator(monkeypatch) -> None:
    """The screen is 53x26, with the header and the F1–F5 lane of the PicoCalc.

    MeshTerm draws it. The platform is the PicoCalc in each way but one. In the emulator,
    only MeshTerm draws it. Thus it resolves with ``own_display`` set, and it never offers
    to move to another terminal.
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
    assert screen[-1].startswith("F1") and "F3 Quit?" in screen[-1]  # the lane of the PicoCalc
    assert seen["platform"].name == "picocalc-lyra" and seen["platform"].own_display


def test_emulate_hands_the_global_options_to_the_meshterm_it_runs(monkeypatch, tmp_path) -> None:
    """``meshterm emulate picocalc-lyra --mock``: the MeshTerm in the window gets ``--mock``.

    The outer run parses a global option where the user typed it. If the outer run did not
    pass it on, the emulated MeshTerm would start without it and look for real hardware.
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
    assert "--platform" not in argv  # the emulator gives the name of the platform itself


# --- the lane under the mouse --------------------------------------------------------------


def test_a_clicked_lane_key_types_its_slot_or_with_shift_its_companion() -> None:
    """A click types what the F-key of the desktop types.

    This is the plain bank of the deck, or with Shift its own bank.
    """
    from meshterm.emulator.devices import DEVICES
    from meshterm.emulator.window import lane_press

    for emulated in DEVICES.values():
        deck = emulated.deck
        for slot, (plain, shifted) in enumerate(zip(deck.keys, deck.shift_keys, strict=True)):
            assert _parsed(lane_press(emulated, slot, shift=False))[0].value == f"f{plain}"
            assert _parsed(lane_press(emulated, slot, shift=True))[0].value == f"f{shifted}"


def test_a_click_finds_the_chip_under_it_only_on_a_drawn_lane() -> None:
    """Each cell of a chip is that slot, in each bank.

    A gap between chips, another row, or a frame with no lane on its last row is not a
    slot.
    """
    from meshterm.emulator.window import chip_at
    from meshterm.ui.tui.fkeys import DEFAULT_LANE, PICOCALC_LYRA_DECK

    deck = PICOCALC_LYRA_DECK
    term = Terminal(53, 3)
    term.feed("\x1b[3;1H" + deck.lane_text(DEFAULT_LANE).plain)
    assert [chip_at(term, deck, col, 2) for col in (0, 8, 11, 44, 52)] == [0, 0, 1, 4, 4]
    assert chip_at(term, deck, 9, 2) is None  # the gap between two chips
    assert chip_at(term, deck, 0, 1) is None  # not the row of the lane
    term.feed("\x1b[3;1H" + deck.lane_text(DEFAULT_LANE, shifted=True).plain)
    assert chip_at(term, deck, 46, 2) == 4  # F10, captioned "10"
    term.feed("\x1b[3;1H\x1b[2Kscan me")
    assert chip_at(term, deck, 0, 2) is None  # the last row of a bare frame


def test_the_drawn_keys_wear_the_lane_s_fills() -> None:
    """The drawn keys have the fills of the lane.

    The Cardputer has the fn orange and its Shift blue. The chips of a console are palette
    slots.
    """
    from meshterm.emulator.devices import CARDPUTER_ZERO_DEVICE, PICOCALC_LYRA_DEVICE
    from meshterm.emulator.run import _palette
    from meshterm.emulator.window import lane_fills

    assert lane_fills(CARDPUTER_ZERO_DEVICE) == ("#e34b0f", "#0f72bd")
    palette = _palette()
    plain, shifted = (f"#{r:02x}{g:02x}{b:02x}" for r, g, b in (palette[7], palette[2]))
    assert lane_fills(PICOCALC_LYRA_DEVICE) == (plain, shifted)


@pytest.mark.parametrize("name", ["cardputer-zero", "picocalc-lyra"])
def test_a_shift_click_on_the_menu_s_quit_chip_leaves_at_once(monkeypatch, name) -> None:
    """A click reaches the lane in the same way as the key.

    Shift on ``Quit?`` is ``Quit!``, with no question.
    """
    from meshterm.emulator.devices import device
    from meshterm.emulator.window import chip_at, lane_press
    from meshterm.ui import menu

    monkeypatch.setattr(menu, "_arm_exit_watchdog", lambda *args, **kwargs: None)
    emulated = device(name)
    seen: dict[str, object] = {}
    closed = threading.Event()

    class Headless:
        def __init__(self, terminal, lock, type_text):
            def drive():
                last = terminal.rows - 1
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and "slot" not in seen:
                    with lock:
                        lane = terminal.line(last)
                        if "Quit?" in lane:
                            col = lane.index("Quit?")
                            seen["slot"] = chip_at(terminal, emulated.deck, col, last)
                    time.sleep(0.1)
                type_text(lane_press(emulated, 2, shift=True))
                if not closed.wait(5):
                    seen["stuck"] = True  # still running: leave in the same way as the window
                    type_text("\x11")
                    time.sleep(0.3)
                    type_text("\x11")

            threading.Thread(target=drive, daemon=True).start()

        def frame_ready(self):
            pass

        def close(self):
            closed.set()

    assert run(["--mock"], Headless, emulated) == 0
    assert seen["slot"] == 2  # the click on Quit? is on its chip
    assert "stuck" not in seen  # and Shift with it leaves with no question
