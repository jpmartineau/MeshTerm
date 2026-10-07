# SPDX-License-Identifier: Apache-2.0
"""The icon-column guard: in one list, each icon row starts its words in one cell.

An icon row is a row that starts with an icon. The terminal draws some of the icons of the
app in one cell (``🗑 ✎ ⚙ ▶ ★ ↻ ↕ ⇄ ⌨ # ✓ ✗ ⚠``) and most emoji in two cells. Thus a row
that is written ``icon + " " + label`` starts its words one column to the left of its
two-cell neighbours. A review cannot show this fault,
because the source looks the same in both cases. The fault returned again and again, screen
by screen (the node page's ``🗑 Remove contact…`` under ``⏳ Time machine``, the main menu,
the repeater admin). Then we measured the column one time in :mod:`meshterm.ui.menus`. The
tools are :func:`~meshterm.ui.menus.align_icons` (which
:func:`~meshterm.ui.menus.menu_rows` applies for you), or
:func:`~meshterm.ui.menus.icon_lane` with :func:`~meshterm.ui.menus.icon_mark` and
:func:`~meshterm.ui.menus.marked_label`. This file makes sure that a new list does not skip
them.

**The coverage comes from the gallery, not from a list in this file.**
:mod:`tests.test_gallery` already builds each screen that the menu can open, through its
real builders, on both platforms. This sweep imports that inventory (``_ENTRIES`` ×
``_COMBOS``). Thus a screen that is added there is guarded here with no edit. The sweep has
two readings, because the screens in the gallery hold their rows in two ways:

* A :class:`~meshterm.ui.tui.select.SelectScreen` holds :class:`~meshterm.ui.tui.select.Choice`
  items, so the sweep reads its rows as *data*. It reads the natural title of each choice,
  with no effect from the filter or the viewport. This includes the rows below the fold.
* A screen that draws its own action rows (the node page, Trace, the TX sweep, a trophy
  card) has no items to read. Thus the sweep renders its body in a viewport that is tall
  enough to clip nothing. It finds the list by its ``❯`` pointer: the continuous run of
  pointer-column lines (``❯ `` or two spaces, then content) around it, with the blank
  lines.

"Starts with an icon" is :func:`meshterm.ui.menus._icon_head`. The aligner and the
:func:`~meshterm.ui.menus.command_label` function (which removes the icon) use this same
definition. The words of a row start where :func:`meshterm.ui.menus._words_start` says.
The sweep measures this in display cells with :func:`rich.cells.cell_len`, never in
characters.

A glyph that is *data* and not decoration gets no exemption. Examples are the ``● ▲ ■ ◉ ○ ★``
of a node and the ``＃ 🌐 🔒`` of a channel. These glyphs are in the same column as the
command icons around them (the chat picker pads ``●`` to the width of ``🌐``, so each
conversation name is in line). A data lane that is not aligned is the same fault that the
user sees. There is one small allowance, and it is structural. In a Choice list, a
**blank** :class:`~meshterm.ui.tui.select.Separator` starts a new group. This blank line is
how :func:`~meshterm.ui.menus.exit_rows` sets its ``✓ Apply`` and ``✗ Back — discard`` pair
apart. It is also how a table sets its command tail apart (the rows of the Contacts list
under its maintenance rows, the Courier queue row under its outbox). The space means "a
different list", and each side keeps its own column. A section heading (``── Label ──``)
does not break a group: the sections of one list have one column.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import pytest
from rich.cells import cell_len
from rich.text import Text

from meshterm.platforms import REGULAR, Platform, set_platform
from meshterm.ui.menus import _icon_head, _words_start, align_icons, exit_rows, menu_rows
from meshterm.ui.tui import Choice, Screen, SelectScreen, Separator
from tests.conftest import plain as _plain
from tests.test_gallery import _COMBOS, _ENTRIES, _Entry

#: The pointer column that each list draws before its rows: the ``❯ `` of the highlight,
#: or two spaces for the other rows (refer to :class:`~meshterm.ui.tui.select.SelectScreen`).
#: It has one cell of glyph and one cell of space, thus two cells in each place.
_CURSOR = "❯ "
_GUTTER = "  "

#: A viewport that is taller than the content of each gallery screen. Thus a hand-drawn
#: list below the fold is still in the rendered body that the sweep reads.
_TALL_VIEWPORT = 400


@dataclass(frozen=True)
class _IconRow:
    """One row of a list that starts with an icon.

    The row has its label as the user sees it, and the cell where its words start.

    Attributes:
        label: The plain text of the row, with no pointer column.
        start: The display cell (0-based, from the first cell of the label) where its words
            start.
    """

    label: str
    start: int


def _icon_row(label: str) -> _IconRow | None:
    """``label`` measured as an icon row, or ``None`` if it does not start with an icon."""
    head = _icon_head(label)
    if not head:
        return None
    return _IconRow(label, cell_len(label[: _words_start(label, head)]))


def _choice_groups(items: Iterable) -> list[list[_IconRow]]:
    """The icon rows of a Choice list, in groups. A blank separator line starts a new group.

    Refer to the module docstring for the reason that the blank line (and only the blank
    line) separates two lists: it is the seam that :func:`~meshterm.ui.menus.exit_rows`
    draws above its pair.
    """
    groups: list[list[_IconRow]] = [[]]
    for item in items:
        if isinstance(item, Separator):
            title = item.title(_TALL_VIEWPORT) if callable(item.title) else item.title
            text = title.plain if isinstance(title, Text) else title
            if not text.strip():
                groups.append([])
            continue
        if not isinstance(item, Choice):
            continue
        label = item.label
        row = _icon_row(label.plain if isinstance(label, Text) else label)
        if row is not None:
            groups[-1].append(row)
    return [group for group in groups if group]


def _in_pointer_column(line: str) -> bool:
    """Whether a rendered line is part of a hand-drawn list: blank, or a pointer and content."""
    if not line.strip():
        return True
    return line.startswith((_CURSOR, _GUTTER)) and line[2:3] not in ("", " ")


def _rendered_groups(lines: list[str]) -> list[list[_IconRow]]:
    """The icon rows of each hand-drawn list in a rendered body, with one group for each list.

    The ``❯`` row is the anchor of a list. The list goes up and down over each continuous
    line in the pointer column. Blank lines stay in the list. A screen can put its actions
    in clusters (Trace's compose and explore, width and samples, run). It still has one icon
    column for all of them, and the user reads them as one list.
    """
    groups: list[list[_IconRow]] = []
    for anchor, line in enumerate(lines):
        if not line.startswith(_CURSOR):
            continue
        first = anchor
        while first > 0 and _in_pointer_column(lines[first - 1]):
            first -= 1
        last = anchor
        while last + 1 < len(lines) and _in_pointer_column(lines[last + 1]):
            last += 1
        rows = [_icon_row(row[2:]) for row in lines[first : last + 1] if row.strip()]
        groups.append([row for row in rows if row is not None])
    return [group for group in groups if group]


def _screen_groups(screen: Screen, cols: int) -> list[list[_IconRow]]:
    """Each list on ``screen``.

    The sweep reads items if the screen has them, and the rendering if not.
    """
    if isinstance(screen, SelectScreen):
        return _choice_groups(screen._items)
    screen.note_viewport(_TALL_VIEWPORT)
    return _rendered_groups(_plain(screen.render_body(cols)).splitlines())


def _misalignments(where: str, groups: list[list[_IconRow]]) -> list[str]:
    """One failure sentence for each group whose icon rows start in different cells."""
    problems = []
    for number, group in enumerate(groups, start=1):
        if len({row.start for row in group}) < 2:
            continue
        rows = "\n".join(f"    cell {row.start:>2}: {row.label.rstrip()!r}" for row in group)
        problems.append(
            f"{where}, list {number} of {len(groups)}: icon rows start their words in "
            f"different cells —\n{rows}\n"
            "  Pad the marks to one column: build the labels through menus.align_icons "
            "(menu_rows does it for you), or icon_lane + icon_mark / marked_label(lane=...)."
        )
    return problems


def _cases():
    for entry in _ENTRIES:
        for platform, cols, rows in _COMBOS:
            yield pytest.param(
                entry, platform, cols, rows, id=f"{entry.name}-{platform.name}-{cols}x{rows}"
            )


@pytest.mark.parametrize("entry,platform,cols,rows", list(_cases()))
def test_icon_rows_share_one_column(
    entry: _Entry, platform: Platform, cols: int, rows: int
) -> None:
    """In one list, each row that starts with an icon starts its words in the same display cell.

    The test sweeps the whole gallery on both platforms. The PicoCalc removes the icon lane
    of a command row completely. But its data glyphs (the type of a node, the openness of a
    channel) stay and fold to one-cell substitutes. Thus the rule still applies there.
    """
    set_platform(platform)
    screen = entry.factory(cols, rows)
    where = f"{entry.name} on {platform.name} {cols}x{rows}"
    problems = _misalignments(where, _screen_groups(screen, cols))
    assert not problems, "\n\n".join(problems)


@pytest.mark.parametrize("platform", sorted({p for p, _, _ in _COMBOS}, key=lambda p: p.name))
def test_the_sweep_measures_something_on_every_platform(platform: Platform) -> None:
    """The guard is not empty: each platform compares real lists, in each reading that it can.

    Suppose a gallery refactor hides the items of a screen, or a render change moves the
    ``❯`` pointer. Then each case above passes, because it finds nothing to measure. This
    test finds that fault. On the desktop, both readings must reach a list of two or more
    icon rows. On the PicoCalc, the hand-drawn action lists have no icons (the lane is
    removed). Thus there the test needs only the Choice reading: the node-type glyphs, the
    channel glyphs, and the exit pair.
    """
    set_platform(platform)
    cols, rows = next((c, r) for p, c, r in _COMBOS if p is platform)
    measured = {"items": 0, "rendered": 0}
    for entry in _ENTRIES:
        screen = entry.factory(cols, rows)
        reading = "items" if isinstance(screen, SelectScreen) else "rendered"
        measured[reading] += sum(len(g) > 1 for g in _screen_groups(screen, cols))
    assert measured["items"] > 0, f"no Choice list with 2+ icon rows on {platform.name}"
    if platform is REGULAR:
        assert measured["rendered"] > 0, "no hand-drawn list with 2+ icon rows on regular"


def test_guard_catches_a_hand_written_icon_prefix() -> None:
    """A list that is written ``icon + " " + label`` fails.

    The failure names both rows and both start cells. ``🗑`` is one cell and ``💾`` is two
    cells. This exact pair put the delete row of the node page out of line under its archive
    row. If this test stops to fail, the guard does not guard.
    """
    screen = SelectScreen("Node", [Choice("💾 Archive contact", 1), Choice("🗑 Delete contact…", 2)])
    problems = _misalignments("node page", _screen_groups(screen, 72))
    assert len(problems) == 1
    assert "cell  3: '💾 Archive contact'" in problems[0]
    assert "cell  2: '🗑 Delete contact…'" in problems[0]

    aligned = SelectScreen(
        "Node",
        [
            Choice(label, n)
            for n, label in enumerate(align_icons(["💾 Archive contact", "🗑 Delete contact…"]))
        ],
    )
    assert not _misalignments("node page", _screen_groups(aligned, 72))


def test_guard_catches_a_hand_drawn_list_through_its_rendering() -> None:
    """The sweep reads a screen with no items from its rendered body, with no pointer column.

    The highlighted row and the rows with a gutter are one list across a blank line. The
    text above them (a card, not in the pointer column) is not part of the list.
    """
    lines = [
        "route  ★ → 3d63 → ★",
        "",
        "  ✎ Compose path",
        "  ⚡ Explore paths",
        "",
        "❯ ▶ Trace — one transmission",
        "",
        "Per-hop medians",
        "  ↓ 2 more",
    ]
    groups = _rendered_groups(lines)
    assert [[row.start for row in group] for group in groups] == [[2, 3, 2]]
    assert _misalignments("trace", groups)


def test_exit_pair_is_its_own_group() -> None:
    """The test measures the staged-changes pair after its blank line apart from the rows above it.

    ``✓`` and ``✗`` are one-cell status marks on a commit row. MeshTerm does not pad them to
    the emoji column of a list (refer to :func:`~meshterm.ui.menus.exit_rows`). The blank
    separator is the seam. The pair keeps its own column, and the rows above it keep theirs.
    """
    items = menu_rows([("📡 Send advert", "Flood it", 1), ("⌨ Command line", "Raw CLI", 2)])
    items += exit_rows(2, apply_value="apply", back_value="back")
    groups = _choice_groups(items)
    assert [[row.start for row in group] for group in groups] == [[3, 3], [2, 2]]
    assert not _misalignments("editor", groups)
