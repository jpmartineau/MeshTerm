# SPDX-License-Identifier: Apache-2.0
"""Tests for the shared menu chrome: the parts that each select list builds in the same way.

These tests check the exit conventions and the lane conventions of the app at their
source. A screen that builds its rows through :mod:`meshterm.ui.menus` gets the standards.
Thus a regression fails one time here, and not on each screen.
"""

from __future__ import annotations

import pytest
from rich.text import Text

from meshterm.ui.menus import (
    Lane,
    align_icons,
    changes_phrase,
    column_header,
    exit_rows,
    fit_cells,
    icon_lane,
    icon_mark,
    lane_header,
    marked_label,
    menu_rows,
    section_heading,
)
from meshterm.ui.tui import Separator


def test_exit_rows_clean_state_is_nothing_at_all() -> None:
    """A list never advertises its own exit: with nothing staged, there are no rows.

    Esc leaves, on both platforms. Thus we removed the ``Back`` row of the app. The pair
    for staged changes (the next test) remains, because it is a choice and not an exit.
    """
    assert exit_rows(0, apply_value="A", back_value="B") == []


def test_exit_rows_staged_state_spells_out_the_consequence() -> None:
    """Staged changes give "✓ Apply" above "✗ Back — discard", after one blank line."""
    rows = exit_rows(3, apply_value="A", back_value="B")
    assert isinstance(rows[0], Separator) and rows[0].title == " "
    apply_row, back_row = rows[1], rows[2]
    assert apply_row.value == "A" and back_row.value == "B"
    assert apply_row.title.plain == "✓ Apply 3 staged changes"
    assert back_row.title.plain == "✗ Back — discard staged changes"


def test_menu_rows_align_descriptions_in_display_cells() -> None:
    """Each description starts at the same cell column, also after a label with a wide emoji."""
    rows = menu_rows(
        [
            ("🔄 Reboot device…", "Restart it", 1),  # emoji = 2 cells
            ("Plain", "Second", 2),
        ]
    )
    from rich.cells import cell_len

    starts = {
        cell_len(r.title.plain[: r.title.plain.index(d)])
        for r, d in zip(rows, ["Restart it", "Second"], strict=True)
    }
    assert len(starts) == 1  # one description column for all rows


def test_menu_rows_keep_a_styled_label_styled() -> None:
    """A Text label (a destructive row with the err tint) keeps its spans in the built row."""
    rows = menu_rows([(Text("⚠ Danger", style="err"), "Careful", "x")])
    title = rows[0].title
    assert title.plain.startswith("⚠ Danger")
    assert any(span.style == "err" for span in title.spans)


def test_section_heading_wears_the_dashes_and_the_heading_grey() -> None:
    """Headings of a grouped list read ── Label ── in the heading grey, not the frame accent."""
    sep = section_heading("Outbox")
    assert sep.title == "── Outbox ──"
    assert sep.style == "heading"


def test_changes_phrase_pluralizes() -> None:
    """One staged change is singular. Any other count is plural."""
    assert changes_phrase(1) == "1 staged change"
    assert changes_phrase(2) == "2 staged changes"


def test_column_header_lays_each_label_over_its_lane() -> None:
    """Lanes get padding to their own width under the pointer indent, so labels are over columns."""
    header = column_header([Lane("NAME", 8), Lane("KEY", 6), Lane("HEARD")], 40)
    assert header == "  NAME    KEY   HEARD"


def test_column_header_abbreviates_from_the_right_to_fit() -> None:
    """If the width is too narrow for the full labels, lanes give shorter forms, never a wrap."""
    from rich.cells import cell_len

    lanes = [Lane("SETTING", 10), Lane(("DESCRIPTION", "DESC", "?"))]
    assert column_header(lanes, 40) == "  SETTING   DESCRIPTION"  # room for the full word
    assert column_header(lanes, 20) == "  SETTING   DESC"  # one step shorter, and it fits
    assert column_header(lanes, 13) == "  SETTING   ?"  # the last form still fits
    # Nothing is left to give: the line is cropped and does not wrap onto a second row.
    tight = column_header(lanes, 10)
    assert cell_len(tight) == 10 and tight.endswith("…") and "\n" not in tight


def test_column_header_only_shortens_the_lanes_it_has_to() -> None:
    """A lane keeps its full label while a lane to its right can still give cells."""
    from rich.cells import cell_len

    lanes = [Lane(("CONVERSATION", "CHAT"), 14), Lane(("LAST MESSAGE", "LAST MSG", "LAST"))]
    assert column_header(lanes, 24) == "  CONVERSATION  LAST MSG"
    assert column_header(lanes, 20) == "  CONVERSATION  LAST"
    # The left lane abbreviates only after the right lane has no more cells to give. A line
    # that still does not fit is cropped (its padded lanes have no cells to give), and never
    # wraps.
    tight = column_header(lanes, 14)
    assert tight.startswith("  CHAT") and tight.endswith("…") and cell_len(tight) == 14


def test_lane_header_heads_the_editor_lanes_and_shortens_description() -> None:
    """The shared editor header matches the lanes of lane_row and abbreviates the last label."""
    from meshterm.ui.menus import lane_row

    row = lane_row("Node name", Text("MockCompanion"), "Advertised name", 12, 20)
    header = lane_header(12, 20, 80)
    assert header.startswith("  SETTING")
    assert header.index("VALUE") == 2 + 12 + 2  # the pointer indent, then the label lane
    assert header.index("DESCRIPTION") == 2 + 12 + 2 + 20 + 2
    # The lanes of the row start where the labels of the header start. Both have the same
    # padding, and the header has an offset for the pointer column that the rows draw
    # themselves.
    assert row.plain.index("MockCompanion") == header.index("VALUE") - 2
    assert row.plain.index("Advertised name") == header.index("DESCRIPTION") - 2
    assert lane_header(12, 20, 44).endswith("DESC")  # no room for the word, so abbreviate


def test_fit_cells_measures_display_cells_not_characters() -> None:
    """Padding and truncation count display cells, so wide glyphs cannot skew lanes."""
    from rich.cells import cell_len

    assert fit_cells("abc", 5) == "abc  "
    assert cell_len(fit_cells("日本語の名前", 5)) == 5  # wide characters: truncated by cells
    assert fit_cells("abcdef", 5).endswith("…")
    assert fit_cells("ab", 5, align="right") == "   ab"


def test_fit_cells_keeps_a_broken_name_inside_its_lane() -> None:
    """The radio of another user writes a name, and no part of it can skew the lane.

    Two failures affect one lane. First, a cut in the middle of an emoji sequence leaves a
    stranded joiner. The joiner folds the ellipsis that follows it into the glyph before it.
    Then the lane measures one cell less than the terminal draws, and each column to the
    right of the name starts late. Second, a name that has a newline or an escape ends the row
    in the middle of the lane, whatever the measurement is. ``fit_cells`` corrects both
    failures (refer to :mod:`meshterm.ui.tui.emoji_width`). Each column in the app uses this
    one helper to fit its labels, so the correction covers each column.
    """
    from rich.cells import cell_len

    family = "\U0001f468‍\U0001f469‍\U0001f467"  # one glyph, three joined people
    flag = "\U0001f1e8\U0001f1e6"  # one glyph, two Regional Indicators

    for name in (f"Bob {family} Family", f"{flag} Canada Hub", "line\nbreak\x1b[31m"):
        for width in range(4, 24):
            assert cell_len(fit_cells(name, width)) == width, (name, width)

    # The cut is between glyphs: half of a family is not a glyph, and half of a flag is a letter.
    assert family in fit_cells(f"Bob {family}", 8)  # it fits: the glyph stays whole
    assert "‍" not in fit_cells(f"Bob {family}", 5)  # it does not fit: no joiner stays
    assert fit_cells(f"{flag} Hub", 4).startswith(flag)  # the pair stays, or neither stays

    # What the terminal cannot draw never reaches it. An ordinary name does not change.
    assert "\n" not in fit_cells("line\nbreak", 12)
    assert "\x1b" not in fit_cells("esc\x1b[31mape", 12)
    assert fit_cells("plain", 12).strip() == "plain"


def test_main_menu_sections_answer_the_menus_own_question() -> None:
    """Each section is an action, in the order of the work, and holds only what belongs to it.

    The menu asks "What would you like to do?". Thus the groups are by verb and not by
    subject. The address book is with the features that send messages from it. The recorded
    history is with the live views, because it is their past tense. The two walks are with
    the topology that they walk over. A setting goes in the section that says whose it is:
    this radio, the radio of another node over the mesh, or MeshTerm itself.

    This app is the last of these three sections, and this is the reason that it sorts last.
    It holds the preferences that change the program, the diagnostics that state what the
    program is now, and the pages that describe it. None of these is something to do on the
    mesh.
    """
    from meshterm.tools import load_all_tools
    from meshterm.tools.base import _CATEGORY_ORDER, all_tools

    load_all_tools()
    sections: dict[str, list[str]] = {}
    for tool in all_tools():
        if tool.menu_visible:
            sections.setdefault(tool.category, []).append(tool.name)

    assert list(sections) == _CATEGORY_ORDER  # all_tools already sorts them in this order
    assert sections == {
        "Message": ["chat", "channels", "rooms", "courier", "contacts"],
        "Watch": ["dashboard", "livefeed", "watchtower", "timemachine"],
        "Explore": ["map", "walk", "trace", "trace-path", "records"],
        "This node": ["info", "config", "advert"],
        "Other nodes": ["repeater-admin", "tx-optimize"],
        "This app": [
            "preferences",
            "diagnostics",
            "about",
            "about-author",
            "discord",
            "support",
        ],
    }
    # No section is so big that it stops being a group (the old Mesh section had seven of
    # nineteen rows), and no section has only one row. This app is the section with six rows.
    # Its order is changeable, then live, then fixed: the row that changes MeshTerm, the row
    # that states what MeshTerm is now on this machine, then the four pages that say what
    # MeshTerm is in general. Six of twenty-five rows is still a group. Seven of nineteen
    # was not.
    assert all(2 <= len(names) <= 6 for names in sections.values())


#: Two lexicon icons that the terminal draws in different widths. This is the reason that
#: the code must measure the icon column. Of the icons of the app, ten (``🗑 ✎ ⚙ ▶ ★ ↻ ↕ ⇄ ⌨ #``)
#: draw one cell, and the others draw two.
_NARROW, _WIDE = "🗑", "📂"


def test_the_icon_column_is_the_widest_mark_a_list_can_draw() -> None:
    """A list declares its icons and the column measures them. An empty set has no column."""
    from rich.cells import cell_len

    assert cell_len(_NARROW) == 1 and cell_len(_WIDE) == 2, "the premise of this whole lane"
    assert icon_lane((_NARROW,)) == 1  # a list that has only narrow marks keeps a narrow column
    assert icon_lane((_WIDE,)) == 2
    assert icon_lane((_NARROW, _WIDE)) == 2  # a mixed list pads to its widest mark
    assert icon_lane(()) == 0
    assert icon_lane(("",)) == 0  # a row with no icon adds no column


def test_a_narrow_mark_pads_out_to_its_wider_siblings() -> None:
    """The fix for a row whose label started one column early (JP, 2026-09-01).

    ``🗑 Delete contact…`` was one cell to the left of ``💾 Archive contact``. The row wrote
    ``icon + " "``, and the terminal draws the two icons in different widths. Now both
    marks use the same number of cells. Cells are the only measure that the terminal uses.
    The character counts are still different, and a test of the counts hid this fault.
    """
    from rich.cells import cell_len

    lane = icon_lane((_NARROW, _WIDE))
    narrow = icon_mark(_NARROW, "err", lane)
    wide = icon_mark(_WIDE, "", lane)
    assert cell_len(narrow.plain) == cell_len(wide.plain) == lane + 1
    assert len(narrow.plain) != len(wide.plain), "cells, not characters, are the measure"


def test_marked_label_lines_up_a_mixed_list_when_told_its_lane() -> None:
    """Labels start in the same cell when the caller passes the measured column of the list."""
    from rich.cells import cell_len

    lane = icon_lane((_NARROW, _WIDE))
    rows = [
        marked_label(_NARROW, "Archive contacts…", "err", lane=lane),
        marked_label(_WIDE, "View archived contacts", "", lane=lane),
    ]
    starts = {
        cell_len(row.plain[: row.plain.index(word)])
        for row, word in zip(rows, ("Archive", "View"), strict=True)
    }
    assert starts == {lane + 1}

    # Without the lane, each mark measures itself. This is correct for a list whose rows
    # all start with the same icon. It misaligns a mixed list.
    solo = [marked_label(_NARROW, "A", "err"), marked_label(_WIDE, "B", "")]
    assert (
        len(
            {
                cell_len(row.plain[: row.plain.index(letter)])
                for row, letter in zip(solo, ("A", "B"), strict=True)
            }
        )
        == 2
    )


def _plain(label) -> str:
    return label.plain if isinstance(label, Text) else label


def test_align_icons_starts_every_word_in_the_same_cell() -> None:
    """A one-cell icon and a two-cell icon in label strings get padding to one column.

    This fault came back on one screen after another (the node page, the main menu, the
    ``↻`` and ``⌨`` rows of the repeater admin). Each list had to remember to measure its
    own icon column. A label with no icon passes through as the same object.
    """
    plain_row = "Reorder"
    labels = align_icons(
        [f"{_NARROW} Archive contacts…", Text.assemble((_WIDE, "ok"), " View archived"), plain_row]
    )
    assert [_plain(label) for label in labels] == [
        f"{_NARROW}  Archive contacts…",
        f"{_WIDE} View archived",
        "Reorder",
    ]
    assert labels[2] is plain_row
    styled = labels[1]
    assert any(
        span.style == "ok" and styled.plain[span.start : span.end] == _WIDE for span in styled.spans
    ), "the icon's own tint survives the padding"


def test_align_icons_keeps_a_base_style_and_never_adds_to_an_existing_gap() -> None:
    """A tint on the whole row stays as a span. To align a list that is aligned changes nothing."""
    danger = Text(f"{_NARROW} Factory reset…", style="err")
    once = align_icons([danger, f"{_WIDE} Sync clock…"])
    words = once[0].plain.index("Factory")
    assert any(span.style == "err" and span.start <= words < span.end for span in once[0].spans)
    twice = align_icons(once)
    assert [_plain(label) for label in twice] == [_plain(label) for label in once]
    padded_by_hand = marked_label(_NARROW, "Delete", "", lane=icon_lane((_NARROW, _WIDE)))
    assert _plain(align_icons([padded_by_hand, f"{_WIDE} Keep"])[0]) == padded_by_hand.plain


def test_menu_rows_line_up_mixed_icon_widths_without_being_told() -> None:
    """A list that menu_rows builds gets the icon column with no lane to pass."""
    from rich.cells import cell_len

    rows = menu_rows(
        [
            (f"{_NARROW} Read settings", "one", 1),
            (f"{_WIDE} Send advert…", "two", 2),
            (Text(f"{_NARROW} Factory reset…", style="err"), "three", 3),
        ]
    )
    starts = {
        cell_len(row.title.plain[: row.title.plain.index(word)])
        for row, word in zip(rows, ("Read", "Send", "Factory"), strict=True)
    }
    assert starts == {3}


def test_align_icons_pads_nothing_where_the_platform_draws_no_icons() -> None:
    """With no icon lane, the icons go, and no padding stays in their place."""
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform

    set_platform(PICOCALC_LYRA)
    try:
        labels = align_icons([f"{_NARROW} Archive contacts…", f"{_WIDE} View archived"])
        assert [_plain(label) for label in labels] == ["Archive contacts…", "View archived"]
    finally:
        set_platform(REGULAR)


def test_the_main_menu_starts_every_title_in_the_same_cell() -> None:
    """The main menu follows the icon-column rule of the app (JP, 2026-09-06).

    The menu is the only list that never declares its icons. It takes the icons that the
    tool registry has. Thus it is also the list that drifts without a warning when a tool
    has a mark of a different width. ``⚙`` was that mark: one cell among twenty-three
    two-cell siblings. It started the row *Preferences* one column to the left of each other
    row. The test checks the shared start column and not the gear itself. Thus the next
    narrow icon does not cause the fault again.
    """
    from rich.cells import cell_len

    from meshterm.tools import all_tools, load_all_tools
    from meshterm.ui.menu import _menu_labels

    load_all_tools()
    tools = [tool for tool in all_tools() if tool.menu_visible]
    labels = _menu_labels(tools)

    widths = {cell_len(tool.icon) for tool in tools if tool.icon}
    assert widths == {1, 2}, "a menu of one icon width would prove nothing"
    starts = {
        label.cell_len - cell_len(tool.title or tool.name)
        for tool, label in zip(tools, labels, strict=True)
    }
    assert len(starts) == 1, "a title starts a column early"


def test_an_iconless_platform_collapses_the_column_and_keeps_the_tint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no icon lane, the row uses no cells and no separator, and the tint moves to the words.

    A destructive row shows itself with its red mark. If the row has no mark, the tint must
    go somewhere. If it does not, a delete reads as any other action.
    """
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform

    set_platform(PICOCALC_LYRA)
    try:
        assert icon_lane((_NARROW, _WIDE)) == 0
        assert icon_mark(_NARROW, "err", 0).plain == ""
        row = marked_label(_NARROW, "Delete contact…", "err", lane=0)
        assert row.plain == "Delete contact…"
        assert any(span.style == "err" for span in row.spans) or row.style == "err"
    finally:
        set_platform(REGULAR)
