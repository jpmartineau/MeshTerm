# SPDX-License-Identifier: Apache-2.0
"""The main menu's Quit row shares the tool rows' icon column (JP, 2026-09-11).

The menu closes on a ``🚪 Quit`` row below its tool rows, and for as long as it has existed
that row sat *outside* the measured icon column the tools are drawn through: a literal
string whose word happened to start in the right cell because ``🚪`` happened to be as wide
as the widest tool icon. These tests pin the alignment itself rather than today's icons, so
a narrower quit mark or a wider tool mark cannot quietly reopen it, and they pin the
PicoCalc case — no icon lane, so the bare word and not a stray indent.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from rich.cells import cell_len

from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
from meshterm.tools import all_tools, load_all_tools
from meshterm.ui import menu
from meshterm.ui.tui import Choice

#: A mark the terminal draws in one cell — the width a quit or tool icon must not be
#: allowed to mis-align by.
_NARROW = "⚙"

#: A mark the terminal draws in two.
_WIDE = "📡"


@pytest.fixture
def tools() -> list:
    """The real menu-visible tools, in drawing order — the registry the menu is built from."""
    load_all_tools()
    return [tool for tool in all_tools() if tool.menu_visible]


@pytest.fixture
def picocalc_lyra() -> Iterator[None]:
    """Run a test on the PicoCalc platform, restoring the regular one afterwards."""
    set_platform(PICOCALC_LYRA)
    try:
        yield
    finally:
        set_platform(REGULAR)


def _fake_tool(name: str, icon: str) -> SimpleNamespace:
    """A stand-in tool carrying only what the menu reads, with an icon of our choosing."""
    return SimpleNamespace(
        name=name, title=name.capitalize(), icon=icon, category="Test", help="does a thing"
    )


def _word_starts(items: list, tools: list) -> dict[str, int]:
    """The cell each row's words start in, keyed by row value — the tools' titles and Quit.

    Measured from the rendered title rather than from the lane, so it checks what the
    reader sees: everything before the title (the mark and its padding) in display cells.
    """
    words = {tool.name: tool.title or tool.name for tool in tools}
    words[menu._QUIT_VALUE] = "Quit"
    starts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, Choice):
            continue
        plain = item.title.plain
        starts[item.value] = cell_len(plain[: plain.index(words[item.value])])
    return starts


def test_the_quit_row_starts_its_word_in_the_tool_titles_cell(tools: list) -> None:
    """On the real menu, *Quit* lines up under every tool title above it."""
    items = menu._menu_items(tools)
    starts = _word_starts(items, tools)

    assert menu._QUIT_VALUE in starts, "the menu lost its Quit row"
    assert set(starts) == {tool.name for tool in tools} | {menu._QUIT_VALUE}
    assert len(set(starts.values())) == 1, f"a row starts a column off: {starts}"
    # The row closes the list, behind its blank separator, with its wording intact.
    assert items[-1].value == menu._QUIT_VALUE
    assert items[-1].title.plain == f"{menu._QUIT_ICON} Quit"


def test_a_narrower_quit_mark_still_lines_up(tools: list, monkeypatch: pytest.MonkeyPatch) -> None:
    """A one-cell quit icon pads out to the tools' two-cell column instead of starting early.

    This is the case the old literal ``🚪 Quit`` would have got wrong: its word followed
    the mark by exactly one space, whatever the mark's width.
    """
    assert cell_len(_NARROW) == 1, "the substitute must actually be narrower"
    monkeypatch.setattr(menu, "_QUIT_ICON", _NARROW)

    starts = _word_starts(menu._menu_items(tools), tools)

    assert len(set(starts.values())) == 1, f"a row starts a column off: {starts}"


def test_the_quit_mark_is_measured_into_the_column(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Quit's mark is the widest in the list, the tool titles move over to meet it.

    Every tool here leads with a one-cell mark and Quit with a two-cell one. A column
    measured over the tools alone would be one cell wide: the titles would start in cell 2
    and Quit's mark, with no room left for its separator, would run into its word
    (``📡Quit``). The menu's column counts Quit's icon too, so every word starts in cell 3.
    """
    fakes = [_fake_tool("alpha", _NARROW), _fake_tool("beta", _NARROW)]
    monkeypatch.setattr(menu, "_QUIT_ICON", _WIDE)

    starts = _word_starts(menu._menu_items(fakes), fakes)

    assert set(starts.values()) == {cell_len(_WIDE) + 1}


@pytest.mark.usefixtures("picocalc_lyra")
def test_no_icon_lane_leaves_the_bare_word_flush_with_the_titles(tools: list) -> None:
    """No icon lane on the PicoCalc: the row is just *Quit*, with no padding left behind."""
    items = menu._menu_items(tools)
    starts = _word_starts(items, tools)

    assert items[-1].value == menu._QUIT_VALUE
    assert items[-1].title.plain == "Quit"
    assert set(starts.values()) == {0}


def test_the_quit_confirm_keeps_the_chip_that_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """The confirm's F3 reads ``Quit!`` and sends ``quit`` — the menu's own F3, again."""
    import asyncio

    from meshterm.ui.tui import fkeys

    asked: dict = {}

    class Session:
        async def button_dialog(self, prompt, buttons, **kwargs):  # noqa: ANN001, ANN202
            asked.update(kwargs)
            return "cancel"

    async def no_bond(ctx) -> bool:  # noqa: ANN001
        return False

    monkeypatch.setattr(menu, "_can_unpair", no_bond)
    assert asyncio.run(menu._confirm_quit(SimpleNamespace(), Session())) is False
    assert asked["lane"][2].label == "Quit!"
    assert fkeys.PICOCALC_LYRA_DECK.action_for(asked["lane"], 3) == "quit"  # F3
    assert fkeys.CARDPUTER_ZERO_DECK.action_for(asked["lane"], 6) == "quit"  # Fn+6, its middle chip
    assert [slot for i, slot in enumerate(asked["lane"]) if i != 2] == [None] * 4
