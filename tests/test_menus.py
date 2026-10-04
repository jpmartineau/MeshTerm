# SPDX-License-Identifier: Apache-2.0
"""Shared menu-chrome tests: the furniture every select list builds the same way.

These pin the app's exit-affordance and lane conventions at their source, so a screen
that builds its rows through :mod:`meshterm.ui.menus` inherits the standards and a
regression here fails once rather than on every screen.
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
    """A list never advertises its own exit: with nothing staged there are no rows.

    Esc leaves, on both platforms, so the app-wide ``Back`` row was retired — what
    survives is the staged-changes pair below, which is a choice rather than an exit.
    """
    assert exit_rows(0, apply_value="A", back_value="B") == []


def test_exit_rows_staged_state_spells_out_the_consequence() -> None:
    """Staged changes produce ✓ Apply above ✗ Back — discard, after one blank line."""
    rows = exit_rows(3, apply_value="A", back_value="B")
    assert isinstance(rows[0], Separator) and rows[0].title == " "
    apply_row, back_row = rows[1], rows[2]
    assert apply_row.value == "A" and back_row.value == "B"
    assert apply_row.title.plain == "✓ Apply 3 staged changes"
    assert back_row.title.plain == "✗ Back — discard staged changes"


def test_menu_rows_align_descriptions_in_display_cells() -> None:
    """Every description starts at the same cell column, wide emoji labels included."""
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
    assert len(starts) == 1  # one shared description column


def test_menu_rows_keep_a_styled_label_styled() -> None:
    """A Text label (an err-tinted destructive row) keeps its spans in the built row."""
    rows = menu_rows([(Text("⚠ Danger", style="err"), "Careful", "x")])
    title = rows[0].title
    assert title.plain.startswith("⚠ Danger")
    assert any(span.style == "err" for span in title.spans)


def test_section_heading_wears_the_dashes_and_the_heading_grey() -> None:
    """Grouped-list headings read ── Label ── in the heading grey, never the frame's accent."""
    sep = section_heading("Outbox")
    assert sep.title == "── Outbox ──"
    assert sep.style == "heading"


def test_changes_phrase_pluralizes() -> None:
    """One staged change is singular; any other count is plural."""
    assert changes_phrase(1) == "1 staged change"
    assert changes_phrase(2) == "2 staged changes"


def test_column_header_lays_each_label_over_its_lane() -> None:
    """Lanes pad to their own width under the pointer indent, so labels sit over columns."""
    header = column_header([Lane("NAME", 8), Lane("KEY", 6), Lane("HEARD")], 40)
    assert header == "  NAME    KEY   HEARD"


def test_column_header_abbreviates_from_the_right_to_fit() -> None:
    """Too narrow for the full labels, lanes give their shorter forms — never a wrap."""
    from rich.cells import cell_len

    lanes = [Lane("SETTING", 10), Lane(("DESCRIPTION", "DESC", "?"))]
    assert column_header(lanes, 40) == "  SETTING   DESCRIPTION"  # room for the full word
    assert column_header(lanes, 20) == "  SETTING   DESC"  # one step shorter, and it fits
    assert column_header(lanes, 13) == "  SETTING   ?"  # the last form still fits
    # Nothing left to give: the line crops rather than wrapping onto a second row.
    tight = column_header(lanes, 10)
    assert cell_len(tight) == 10 and tight.endswith("…") and "\n" not in tight


def test_column_header_only_shortens_the_lanes_it_has_to() -> None:
    """A lane keeps its full label while a lane to its right can still give cells back."""
    from rich.cells import cell_len

    lanes = [Lane(("CONVERSATION", "CHAT"), 14), Lane(("LAST MESSAGE", "LAST MSG", "LAST"))]
    assert column_header(lanes, 24) == "  CONVERSATION  LAST MSG"
    assert column_header(lanes, 20) == "  CONVERSATION  LAST"
    # Only once the right-hand lane is spent does the left one abbreviate — and a line that
    # still cannot fit crops (its padded lanes have no cells to give), never wraps.
    tight = column_header(lanes, 14)
    assert tight.startswith("  CHAT") and tight.endswith("…") and cell_len(tight) == 14


def test_lane_header_heads_the_editor_lanes_and_shortens_description() -> None:
    """The shared editor header matches lane_row's lanes and abbreviates the last label."""
    from meshterm.ui.menus import lane_row

    row = lane_row("Node name", Text("MockCompanion"), "Advertised name", 12, 20)
    header = lane_header(12, 20, 80)
    assert header.startswith("  SETTING")
    assert header.index("VALUE") == 2 + 12 + 2  # the pointer indent, then the label lane
    assert header.index("DESCRIPTION") == 2 + 12 + 2 + 20 + 2
    # The row's own lanes start where the header's labels do (both padded the same way,
    # the header offset by the pointer column the rows draw for themselves).
    assert row.plain.index("MockCompanion") == header.index("VALUE") - 2
    assert row.plain.index("Advertised name") == header.index("DESCRIPTION") - 2
    assert lane_header(12, 20, 44).endswith("DESC")  # no room for the word — abbreviate


def test_fit_cells_measures_display_cells_not_characters() -> None:
    """Padding and truncation count display cells, so wide glyphs can't skew lanes."""
    from rich.cells import cell_len

    assert fit_cells("abc", 5) == "abc  "
    assert cell_len(fit_cells("日本語の名前", 5)) == 5  # wide chars: truncated by cells
    assert fit_cells("abcdef", 5).endswith("…")
    assert fit_cells("ab", 5, align="right") == "   ab"


def test_fit_cells_keeps_a_broken_name_inside_its_lane() -> None:
    """A name is written by a stranger's radio, and no part of one may skew the lane.

    Two failures, one lane. A cut through the middle of an emoji sequence leaves a stranded
    joiner, which folds the ellipsis appended after it into the glyph before it — the lane
    then measures a cell short of what the terminal draws, and every column right of the
    name starts late. And a name carrying a newline or an escape ends the row mid-lane
    whatever it measures. Both are settled inside ``fit_cells`` (see
    :mod:`meshterm.ui.tui.emoji_width`), so every column in the app is covered by the one
    helper they all fit their labels through.
    """
    from rich.cells import cell_len

    family = "\U0001f468‍\U0001f469‍\U0001f467"  # one glyph, three joined people
    flag = "\U0001f1e8\U0001f1e6"  # one glyph, two Regional Indicators

    for name in (f"Bob {family} Family", f"{flag} Canada Hub", "line\nbreak\x1b[31m"):
        for width in range(4, 24):
            assert cell_len(fit_cells(name, width)) == width, (name, width)

    # The cut falls between glyphs: half a family is not a glyph, and half a flag is a letter.
    assert family in fit_cells(f"Bob {family}", 8)  # it fits: the glyph is kept whole
    assert "‍" not in fit_cells(f"Bob {family}", 5)  # it doesn't: no joiner left behind
    assert fit_cells(f"{flag} Hub", 4).startswith(flag)  # the pair survives, or neither does

    # What the terminal cannot draw never reaches it; an ordinary name is left alone.
    assert "\n" not in fit_cells("line\nbreak", 12)
    assert "\x1b" not in fit_cells("esc\x1b[31mape", 12)
    assert fit_cells("plain", 12).strip() == "plain"


def test_main_menu_sections_answer_the_menus_own_question() -> None:
    """Each section is a *doing*, in workflow order, and holds only what shares it.

    The menu asks "What would you like to do?", so the grouping is by verb rather than
    by subject: the address book sits with the features that message from it, the
    recorded history with the live views it is the past tense of, the two walks with the
    topology they walk over, and a setting lands under the scope it changes — this radio,
    someone else's over the mesh, or MeshTerm itself.

    This app is the last of the three scopes and the reason it sorts last: it holds the
    preferences that change the program, the diagnostics that state what it currently is,
    and the pages that describe it — none of which is a thing to do *on the mesh*.
    """
    from meshterm.tools import load_all_tools
    from meshterm.tools.base import _CATEGORY_ORDER, all_tools

    load_all_tools()
    sections: dict[str, list[str]] = {}
    for tool in all_tools():
        if tool.menu_visible:
            sections.setdefault(tool.category, []).append(tool.name)

    assert list(sections) == _CATEGORY_ORDER  # all_tools already sorts them this way
    assert sections == {
        "Message": ["chat", "channels", "courier", "contacts"],
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
    # No section is so big it stops being a grouping (the old Mesh bucket held seven of
    # nineteen rows), and none is a bucket of one. This app is the one at six: it runs
    # mutable, then live, then fixed — the row that *changes* MeshTerm, the row that states
    # what MeshTerm is *right now* on this machine, then the four pages that say what
    # MeshTerm is in general. Six of twenty-four rows is still a grouping; seven of
    # nineteen was not.
    assert all(2 <= len(names) <= 6 for names in sections.values())


#: Two lexicon icons the terminal genuinely draws in different widths — the whole reason
#: the icon column has to be measured. Of the app's icons, ten (``🗑 ✎ ⚙ ▶ ★ ↻ ↕ ⇄ ⌨ #``)
#: draw one cell and the rest two.
_NARROW, _WIDE = "🗑", "📂"


def test_the_icon_column_is_the_widest_mark_a_list_can_draw() -> None:
    """A list declares its icons and the column measures them; an empty set has no column."""
    from rich.cells import cell_len

    assert cell_len(_NARROW) == 1 and cell_len(_WIDE) == 2, "the premise of this whole lane"
    assert icon_lane((_NARROW,)) == 1  # a list of only narrow marks keeps a narrow column
    assert icon_lane((_WIDE,)) == 2
    assert icon_lane((_NARROW, _WIDE)) == 2  # a mixed list pads up to its widest
    assert icon_lane(()) == 0
    assert icon_lane(("",)) == 0  # a row with no icon contributes no column


def test_a_narrow_mark_pads_out_to_its_wider_siblings() -> None:
    """THE fix for a row whose label started a column early (JP, 2026-09-01).

    ``🗑 Delete contact…`` sat one cell left of ``💾 Archive contact`` because the row wrote
    ``icon + " "`` and the terminal draws the two icons in different widths. Both marks now
    occupy the same number of *cells*, which is the only measure the terminal cares about —
    the character counts still differ, and asserting on those is what hid this.
    """
    from rich.cells import cell_len

    lane = icon_lane((_NARROW, _WIDE))
    narrow = icon_mark(_NARROW, "err", lane)
    wide = icon_mark(_WIDE, "", lane)
    assert cell_len(narrow.plain) == cell_len(wide.plain) == lane + 1
    assert len(narrow.plain) != len(wide.plain), "cells, not characters, are the measure"


def test_marked_label_lines_up_a_mixed_list_when_told_its_lane() -> None:
    """Labels start in the same cell once the caller passes the list's measured column."""
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

    # Without the lane each mark measures itself, which is right for a list whose rows all
    # lead with the same icon — and is exactly what misaligns a mixed one.
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
    """A one-cell and a two-cell icon inside label strings pad out to one column.

    The fault this closes kept reappearing a screen at a time (the node page, the main
    menu, the repeater admin's ``↻``/``⌨`` rows), because each list had to remember to
    measure its own icon column. Iconless labels pass through as the object they were.
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
    """A whole-row tint survives as a span; aligning an aligned list changes nothing."""
    danger = Text(f"{_NARROW} Factory reset…", style="err")
    once = align_icons([danger, f"{_WIDE} Sync clock…"])
    words = once[0].plain.index("Factory")
    assert any(span.style == "err" and span.start <= words < span.end for span in once[0].spans)
    twice = align_icons(once)
    assert [_plain(label) for label in twice] == [_plain(label) for label in once]
    padded_by_hand = marked_label(_NARROW, "Delete", "", lane=icon_lane((_NARROW, _WIDE)))
    assert _plain(align_icons([padded_by_hand, f"{_WIDE} Keep"])[0]) == padded_by_hand.plain


def test_menu_rows_line_up_mixed_icon_widths_without_being_told() -> None:
    """A list built through menu_rows gets the icon column for free — no lane to pass."""
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
    """No icon lane: the icons go, and no padding is left standing in their place."""
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform

    set_platform(PICOCALC_LYRA)
    try:
        labels = align_icons([f"{_NARROW} Archive contacts…", f"{_WIDE} View archived"])
        assert [_plain(label) for label in labels] == ["Archive contacts…", "View archived"]
    finally:
        set_platform(REGULAR)


def test_the_main_menu_starts_every_title_in_the_same_cell() -> None:
    """The app's front door obeys its own icon-column rule (JP, 2026-09-06).

    The menu is the one list that never declares its icons — it takes whatever the tool
    registry carries — so it is also the one that quietly drifts when a tool arrives with
    a mark of a different width. ``⚙`` is that mark today: a single cell among
    twenty-three two-cell siblings, which started *Preferences* a column left of every
    other row. Asserting the shared start column rather than the gear itself keeps the
    next narrow icon from re-opening it.
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
    """No icon lane means no cells spent and no separator — and the tint moves to the words.

    A destructive row announces itself by its red mark; drop the mark and the claim has to
    land somewhere, or a delete reads like any other action.
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
