# SPDX-License-Identifier: Apache-2.0
"""The Cardputer's frame: no header row, its atoms on the title bar, dialogs over the bar."""

from __future__ import annotations

from types import SimpleNamespace

from rich.cells import cell_len
from rich.text import Text

from meshterm import __version__
from meshterm.platforms import CARDPUTER_ZERO, PICOCALC_LYRA, set_platform
from meshterm.ui import menu
from meshterm.ui.tui import frame
from meshterm.ui.tui.screen import ScrollScreen
from meshterm.ui.widgets import battery_cell
from tests.conftest import plain as _plain

ROWS, COLS = CARDPUTER_ZERO.readable_rows, CARDPUTER_ZERO.readable_cols


def _status() -> Text:
    """Three unread and a pack at 87%, as the header callback hands them to the frame."""
    status = Text()
    status.append("●", style="err")
    status.append(" 3", style="warn")
    return Text(" ").join([status, battery_cell(87)])


def _screen(title: str = "Chrome probe", hint: str = "↑↓ move · Esc back") -> ScrollScreen:
    body = Text("\n".join(f"row {i}" for i in range(40)))
    screen = ScrollScreen(body, title=title, floating=False)
    screen._footer_hint = hint
    return screen


def _rows(header: Text, cols: int = COLS, rows: int = ROWS, **lane) -> list[str]:
    """The composed base frame, one ANSI string per row."""
    return frame.compose_base(header, _screen(), "", cols, rows, **lane).split("\n")


def test_the_cardputer_has_no_header_row() -> None:
    """The title bar is the top row, and every screen gets the header's row for its body."""
    set_platform(CARDPUTER_ZERO)
    assert not CARDPUTER_ZERO.header_row
    assert frame.header_lines(_status(), COLS) == []
    composed = [_plain(line) for line in _rows(_status())]
    assert len(composed) == ROWS
    assert composed[0].startswith("↑↓ ") and "Chrome probe" in composed[0]
    assert composed[1].startswith("row 0")
    # Bar, then twelve body rows, then the footer: the header's row went to the body.
    assert composed[12].startswith("row 11")


def test_a_platform_with_a_header_row_keeps_it() -> None:
    """The PicoCalc is unchanged: header on top, the bar under it, no status on the bar."""
    set_platform(PICOCALC_LYRA)
    composed = [_plain(line) for line in _rows(Text("hdr"), 53, 26)]
    assert composed[0].startswith("hdr")
    assert composed[1].startswith("↑↓ ") and "hdr" not in composed[1]


def test_the_status_sits_in_the_corner_after_the_way_out() -> None:
    """``↑↓ ── Title ──── Esc back ● 3 ⣷ 87%``: the badges and battery keep their corner."""
    set_platform(CARDPUTER_ZERO)
    bar = frame._title_bar(_screen(), COLS, False, True, _status()).plain
    assert cell_len(bar) == COLS
    assert bar.endswith("Esc back " + _status().plain)
    assert bar.startswith("↑↓ ─") and "Chrome probe" in bar


def test_an_empty_status_draws_the_bar_as_it_always_was() -> None:
    """Nothing unread and no pack: the bar is exactly the one a header row would sit over."""
    set_platform(CARDPUTER_ZERO)
    plain_bar = frame._title_bar(_screen(), COLS, False, True).plain
    assert frame._title_bar(_screen(), COLS, False, True, Text()).plain == plain_bar


def test_a_long_title_gives_way_before_the_status_does() -> None:
    """The way out sheds first, then the title is cut short; the status is never pushed off."""
    set_platform(CARDPUTER_ZERO)
    status = _status()
    long_title = "Trace — Hilltop-Repeater over a much longer spec than fits"
    bar = frame._title_bar(_screen(long_title), COLS, False, True, status).plain
    assert cell_len(bar) == COLS
    assert bar.endswith(" " + status.plain)
    assert "Esc" not in bar
    assert "…" in bar and "Trace — Hilltop" in bar


def test_the_header_is_its_atoms_alone_without_a_row() -> None:
    """No version, no pulse, no padding: the badges and the battery, a space apart."""
    set_platform(CARDPUTER_ZERO)
    ctx = SimpleNamespace(
        mock=False,
        chat=SimpleNamespace(unread_total=lambda: 3),
        watchtower=SimpleNamespace(unacked_count=lambda: 1),
        battery=SimpleNamespace(reading=lambda: SimpleNamespace(percent=87, charging=False)),
    )
    header = menu._header(ctx, {}, COLS)
    assert header.plain == f"● 3 ▲ 1 {battery_cell(87).plain}"

    ctx.chat = SimpleNamespace(unread_total=lambda: 0)
    ctx.watchtower = SimpleNamespace(unacked_count=lambda: 0)
    ctx.battery = SimpleNamespace(reading=lambda: None)
    assert menu._header(ctx, {}, COLS).plain == ""


def test_the_main_menu_wears_the_wordmark_where_no_header_row_does() -> None:
    """The root screen's title is ``MeshTerm vX`` in the header's colours, or else the question."""
    set_platform(CARDPUTER_ZERO)
    assert menu._menu_title() == f"MeshTerm v{__version__}"
    screen = menu._MainMenu(menu._menu_title(), [], footer_hint="↑↓ move · ^Q quit?")
    bar = frame._title_bar(screen, COLS, False, False, _status())
    start = bar.plain.index("MeshTerm")
    styles = {str(span.style) for span in bar.spans if span.start <= start < span.end}
    assert "brand" in styles

    set_platform(PICOCALC_LYRA)
    assert menu._menu_title() == "What would you like to do?"


def test_a_tall_dialog_covers_the_bar_and_blanks_its_ends() -> None:
    """Over the bar, never over the lane — and no fragment of the bar shows beside the box.

    A dialog's lane is the dialog's own, naming keys nowhere else, so the box stops a row
    short of it. On the bar, the cells either side of the box are blanked: there, a stray
    ``7%`` would read as the battery.
    """
    set_platform(CARDPUTER_ZERO)
    assert ROWS - 2 - CARDPUTER_ZERO.dialog_row_margin == 11  # the dialog's budget
    base = _rows(_status(), footer_lane=lambda: Text("LANE"))
    box = "\n".join("|" + "x" * 45 + "|" for _ in range(ROWS - 1))
    rows = [_plain(row) for row in frame.composite_float(base, box, COLS, ROWS)]
    assert rows[0].strip() == "|" + "x" * 45 + "|"  # the bar's ends are gone
    assert rows[1].startswith("ro")  # the backdrop still shows in the gutter below the bar
    assert rows[ROWS - 1].startswith("LANE")


def test_a_short_dialog_leaves_the_bar_whole() -> None:
    """A box that doesn't reach the top row leaves the bar, and its status, in view."""
    set_platform(CARDPUTER_ZERO)
    base = _rows(_status())
    rows = frame.composite_float(base, "\n".join(["[box]"] * 5), COLS, ROWS)
    assert rows[0] == base[0]
    assert _plain(rows[0]).endswith(_status().plain)
