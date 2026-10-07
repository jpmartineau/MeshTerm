# SPDX-License-Identifier: Apache-2.0
"""Tests for the bundled console font and the code that installs and selects it.

A test cannot run the Win32 part of the code off Windows. A test suite must not run it on
Windows either. The installation of a font changes the machine, and a session cannot undo
the change, because Windows locks the file when it loads the font. Thus these tests check
only what they can check without a change to the system:

- The font that MeshTerm ships is the font that MeshTerm says it ships.
- The font can draw what MeshTerm draws.
- Each entry point does its work in a reduced way, and does not raise an exception, when
  there is no console to talk to.

The tests make assertions about the coverage of the font from its ``cmap``. Thus these
tests are the equivalent, on the desktop, of :mod:`meshterm.ui.fontset`. That module is
the inventory of the PicoCalc, which tests on the handheld verified.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

from meshterm.core import consolefont
from meshterm.ui.termfont import CHART_FONTS, face_draws_charts, installed_chart_font

BRAILLE = range(0x2800, 0x2900)


def _cmap(path: Path) -> set[int]:
    """Each codepoint that a TrueType font maps, read from its ``cmap`` table.

    This is a short reader for format 4, and not a dependency. The question here is narrow.
    The other choice is to trust a font file that MeshTerm ships, with no look inside it.
    """
    data = path.read_bytes()
    count = struct.unpack(">H", data[4:6])[0]
    tables = {}
    for i in range(count):
        offset = 12 + i * 16
        tag, _, start, _length = struct.unpack(">4sIII", data[offset : offset + 16])
        tables[tag.decode("latin-1")] = start

    cmap = tables["cmap"]
    covered: set[int] = set()
    for i in range(struct.unpack(">H", data[cmap + 2 : cmap + 4])[0]):
        record = cmap + 4 + i * 8
        sub = cmap + struct.unpack(">HHI", data[record : record + 8])[2]
        if struct.unpack(">H", data[sub : sub + 2])[0] != 4:
            continue
        seg_bytes = struct.unpack(">H", data[sub + 6 : sub + 8])[0]
        segs = seg_bytes // 2
        ends = struct.unpack(f">{segs}H", data[sub + 14 : sub + 14 + seg_bytes])
        starts_at = sub + 16 + seg_bytes
        starts = struct.unpack(f">{segs}H", data[starts_at : starts_at + seg_bytes])
        deltas_at = starts_at + seg_bytes
        deltas = struct.unpack(f">{segs}h", data[deltas_at : deltas_at + seg_bytes])
        ranges_at = deltas_at + seg_bytes
        ranges = struct.unpack(f">{segs}H", data[ranges_at : ranges_at + seg_bytes])
        for seg in range(segs):
            for cp in range(starts[seg], min(ends[seg], 0xFFFF) + 1):
                if ranges[seg] == 0:
                    glyph = (cp + deltas[seg]) & 0xFFFF
                else:
                    at = ranges_at + seg * 2 + ranges[seg] + (cp - starts[seg]) * 2
                    if at + 2 > len(data):
                        continue
                    glyph = struct.unpack(">H", data[at : at + 2])[0]
                    if glyph:
                        glyph = (glyph + deltas[seg]) & 0xFFFF
                if glyph:
                    covered.add(cp)
    return covered


def test_the_font_we_ship_is_present_with_its_licence() -> None:
    """The font that MeshTerm ships is there with its licence.

    MeshTerm redistributes the font under the SIL OFL. The licence must go with the file.
    """
    assert consolefont.BUNDLED_FONT.is_file()
    licence = consolefont.FONT_DIR / "CascadiaMono-OFL.txt"
    assert licence.is_file()
    text = licence.read_text(encoding="utf-8")
    assert "SIL Open Font License" in text
    assert "Reserved Font Name" in text


def test_the_bundled_font_can_draw_the_charts() -> None:
    """The bundled font can draw the charts.

    This is the reason for this font and not another: it has the braille block, and few
    fonts have it. A measurement of all the fonts on a development machine showed that no
    common coder font has the block. Hack Nerd Font, JetBrains Mono, Fira Code, Source Code
    Pro, and also DejaVu Sans Mono have none of the 256 characters. If MeshTerm shipped a
    font that cannot draw a chart, the offer would succeed in a technical sense, and it
    would have no visible effect.
    """
    covered = _cmap(consolefont.BUNDLED_FONT)
    missing = [cp for cp in BRAILLE if cp not in covered]
    assert not missing, f"{len(missing)} braille cells missing from the bundled font"


def test_the_bundled_font_covers_the_marks_and_the_path_chips() -> None:
    """The bundled font covers the marks and the path chips.

    These are the other characters that a classic console must draw from its font only.
    The powerline separators are the reason that MeshTerm bundles the ``PL`` build and not
    the plain build. The ``PL`` build is 25 KB larger, and the path lines keep their chips.
    """
    covered = _cmap(consolefont.BUNDLED_FONT)
    for mark in "✓❯◉●○▲■─│╭╮╰╯▌▐░▒▓█←↑→↓↔↕…":
        assert ord(mark) in covered, f"{mark!r} is not in the bundled font"
    for chip in "":  # the powerline separator and its round caps
        assert ord(chip) in covered, f"U+{ord(chip):04X} is not in the bundled font"


def test_the_bundled_face_is_what_the_font_calls_itself() -> None:
    """The bundled face is the name that the font gives itself.

    ``SetCurrentConsoleFontEx`` matches the family name exactly, so a typo gives no
    message. The code would install the font and fail to select it. Then it would report a
    console that "kept its own font", and no message would name the real cause.
    """
    data = consolefont.BUNDLED_FONT.read_bytes()
    count = struct.unpack(">H", data[4:6])[0]
    name_table = next(
        struct.unpack(">4sIII", data[12 + i * 16 : 28 + i * 16])[2]
        for i in range(count)
        if struct.unpack(">4sIII", data[12 + i * 16 : 28 + i * 16])[0] == b"name"
    )
    records, strings = struct.unpack(">HH", data[name_table + 2 : name_table + 6])
    families = set()
    for i in range(records):
        at = name_table + 6 + i * 12
        platform, encoding, language, name_id, length, offset = struct.unpack(
            ">HHHHHH", data[at : at + 12]
        )
        if (platform, encoding, language, name_id) == (3, 1, 0x409, 1):
            start = name_table + strings + offset
            families.add(data[start : start + length].decode("utf-16-be"))
    assert consolefont.BUNDLED_FACE in families, f"font calls itself {families}"


def test_the_bundled_face_is_one_we_would_accept() -> None:
    """The bundled face is a face that MeshTerm accepts.

    The font that MeshTerm installs must pass the check that decides whether to make the
    offer. If it does not, MeshTerm installs it, selects it, and makes the offer again at
    the next start.
    """
    assert face_draws_charts(consolefont.BUNDLED_FACE)


def test_chart_fonts_are_matched_by_family_however_spelled() -> None:
    """The code matches chart fonts by family, in any spelling.

    Cascadia has plain, PL, and NF builds, and the Nerd Font patches rename it. The code
    matches the family name. The config of a terminal, the ``HKCU`` registration, and
    ``SetCurrentConsoleFontEx`` all use the family name. They never use the file name. The
    two differ here: ``CascadiaMonoPL.ttf`` calls itself ``Cascadia Mono PL``, with spaces.
    """
    assert face_draws_charts("Cascadia Mono")
    assert face_draws_charts("Cascadia Mono PL")
    assert face_draws_charts("Cascadia Mono NF")
    assert face_draws_charts("  cascadia   code  ")  # the code normalizes the spacing and the case
    assert face_draws_charts("CaskaydiaCove Nerd Font Mono")


def test_the_fonts_that_cannot_draw_charts_are_not_claimed() -> None:
    """The code does not claim the fonts that cannot draw charts.

    A measurement gave zero braille cells for each of these fonts. A wrong yes here is not
    true. Then a user on a classic console would see boxes for charts, with no offer to
    correct them. The whole check exists to prevent exactly this failure.
    """
    for face in (
        "Consolas",
        "Lucida Console",
        "Courier New",
        "Hack Nerd Font Mono",
        "JetBrains Mono",
        "Fira Code",
        "Source Code Pro",
        "DejaVu Sans Mono",
    ):
        assert not face_draws_charts(face), f"{face} does not carry the braille block"
    assert not face_draws_charts(None)
    assert not face_draws_charts("")


def test_every_chart_font_entry_is_normalised() -> None:
    """Each chart font entry is normalized.

    The code compares the list with normalized faces. Thus an entry with capitals never
    matches.
    """
    for entry in CHART_FONTS:
        assert entry == entry.lower().strip()
        assert "  " not in entry


def test_asking_the_machine_what_it_has_never_raises() -> None:
    """A query of the machine for its fonts never raises an exception.

    The code does its best on each platform. A font scan that raises an exception would
    stop the start of the app.
    """
    assert installed_chart_font() is None or isinstance(installed_chart_font(), str)


def test_reading_the_console_font_never_raises() -> None:
    """A read of the console font never raises an exception.

    Under pytest there is no console to ask, and the answer must be None.
    """
    assert consolefont.current_face() is None or isinstance(consolefont.current_face(), str)


def test_the_user_font_directory_is_under_the_users_own_profile() -> None:
    """The font directory of the user is in the user's own profile.

    This is the purpose of the location for each user: no administrator rights are
    necessary.
    """
    directory = consolefont.user_font_dir()
    assert directory.parts[-3:] == ("Microsoft", "Windows", "Fonts")
    assert "AppData" in str(directory) or "Local" in str(directory)


@pytest.mark.skipif(sys.platform == "win32", reason="would touch the real machine")
def test_installing_is_a_no_op_where_there_are_no_windows_fonts() -> None:
    """The install does nothing where there are no Windows fonts.

    Off Windows, MeshTerm never makes the offer. The install refuses, and it does not
    pretend to succeed.
    """
    assert consolefont.install_bundled_font() is False
    assert consolefont.select("Cascadia Mono PL") is False
    assert consolefont.use("Cascadia Mono PL") is False


def test_a_font_resource_is_only_added_from_a_file_that_exists(tmp_path: Path) -> None:
    """The code adds a font resource only from a file that exists.

    The retry path can get a path for a file that is not there. It must not raise an
    exception for it.
    """
    assert consolefont._add_font_resource(tmp_path / "nothing.ttf") is False
