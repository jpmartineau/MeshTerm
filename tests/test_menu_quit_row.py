# SPDX-License-Identifier: Apache-2.0
"""The main menu's Quit row shares the tool rows' icon column (JP, 2026-09-11).

The menu ends with a ``🚪 Quit`` row below its tool rows. The Quit row was always outside
the measured icon column that the code uses to draw the tools. It was a literal string. Its
word started in the correct cell only because ``🚪`` has the same width as the widest tool
icon. These tests check the alignment itself and not the icons of today. Thus a narrower
quit mark or a wider tool mark cannot cause the fault again. The tests also check the
PicoCalc case. There is no icon lane, so the row has the bare word and no extra indent.
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

#: A mark that the terminal draws in one cell. A quit icon or a tool icon of this width
#: must not cause a misalignment.
_NARROW = "⚙"

#: A mark that the terminal draws in two cells.
_WIDE = "📡"


@pytest.fixture
def tools() -> list:
    """The real tools that the menu shows, in drawing order (the registry of the menu)."""
    load_all_tools()
    return [tool for tool in all_tools() if tool.menu_visible]


@pytest.fixture
def picocalc_lyra() -> Iterator[None]:
    """Run a test on the PicoCalc platform, and restore the regular platform afterwards."""
    set_platform(PICOCALC_LYRA)
    try:
        yield
    finally:
        set_platform(REGULAR)


def _fake_tool(name: str, icon: str) -> SimpleNamespace:
    """A stand-in tool with only the attributes that the menu reads, and an icon that we choose."""
    return SimpleNamespace(
        name=name, title=name.capitalize(), icon=icon, category="Test", help="does a thing"
    )


def _word_starts(items: list, tools: list) -> dict[str, int]:
    """The cell where the words of each row start, by row value: the tool titles and Quit.

    The function measures the rendered title and not the lane. Thus it checks what the user
    sees: all the text before the title (the mark and its padding), in display cells.
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
    """On the real menu, Quit aligns under each tool title above it."""
    items = menu._menu_items(tools)
    starts = _word_starts(items, tools)

    assert menu._QUIT_VALUE in starts, "the menu lost its Quit row"
    assert set(starts) == {tool.name for tool in tools} | {menu._QUIT_VALUE}
    assert len(set(starts.values())) == 1, f"a row starts a column off: {starts}"
    # The row ends the list, after its blank separator, with its words unchanged.
    assert items[-1].value == menu._QUIT_VALUE
    assert items[-1].title.plain == f"{menu._QUIT_ICON} Quit"


def test_a_narrower_quit_mark_still_lines_up(tools: list, monkeypatch: pytest.MonkeyPatch) -> None:
    """A one-cell quit icon gets padding to the two-cell column of the tools, not an early start.

    The old literal ``🚪 Quit`` was wrong in this case. Its word followed the mark after
    exactly one space, whatever the width of the mark.
    """
    assert cell_len(_NARROW) == 1, "the substitute must actually be narrower"
    monkeypatch.setattr(menu, "_QUIT_ICON", _NARROW)

    starts = _word_starts(menu._menu_items(tools), tools)

    assert len(set(starts.values())) == 1, f"a row starts a column off: {starts}"


def test_the_quit_mark_is_measured_into_the_column(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the mark of Quit is the widest in the list, the tool titles move to meet it.

    Each tool here starts with a one-cell mark, and Quit starts with a two-cell mark. If
    the code measured the column over the tools only, the column would be one cell wide.
    The titles would start in cell 2. The mark of Quit would have no room for its
    separator, and it would touch its word (``📡Quit``). The column of the menu also counts
    the icon of Quit, so each word starts in cell 3.
    """
    fakes = [_fake_tool("alpha", _NARROW), _fake_tool("beta", _NARROW)]
    monkeypatch.setattr(menu, "_QUIT_ICON", _WIDE)

    starts = _word_starts(menu._menu_items(fakes), fakes)

    assert set(starts.values()) == {cell_len(_WIDE) + 1}


@pytest.mark.usefixtures("picocalc_lyra")
def test_no_icon_lane_leaves_the_bare_word_flush_with_the_titles(tools: list) -> None:
    """There is no icon lane on the PicoCalc: the row is only Quit, with no padding."""
    items = menu._menu_items(tools)
    starts = _word_starts(items, tools)

    assert items[-1].value == menu._QUIT_VALUE
    assert items[-1].title.plain == "Quit"
    assert set(starts.values()) == {0}


def test_the_quit_confirm_keeps_the_chip_that_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """The F3 of the confirm reads ``Quit!`` and sends ``quit``. It is the F3 of the menu again."""
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
