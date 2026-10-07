# SPDX-License-Identifier: Apache-2.0
"""The shared chrome of select menus: the parts that each list screen builds the same way.

The list screens of MeshTerm (the config editors, the channel manager, Watchtower,
Courier, the pickers) all have the same parts: rows with a label and a description,
padded into aligned lanes. Before this module, each screen made its own copy by hand, and
the copies became different (``← Back``, ``← Close``, no blank lines, ``✖`` for
``✗``). Now these helpers are the only way to build these parts. Thus the row language
and the alignment of the app stay the same everywhere, by design:

* :func:`exit_rows`: the exit group for staged changes. ``✓ Apply n staged changes`` is
  above ``✗ Back — discard staged changes`` (with the ``err`` tint). When nothing is
  staged, there are *no rows at all*.
* :func:`menu_rows`: :class:`Choice` rows with a label and a muted description, in two
  aligned lanes (the form of the Actions on the editor pages). The padding is in display
  cells, so that a double-width emoji cannot move the description column.
  :func:`align_icons` aligns the icons.
* :func:`align_icons`: the only icon column for labels that have their icon in the
  string. With it, the words after a one-cell ``⌨`` and after a two-cell ``📡`` start in
  the same cell. A list that builds its :class:`Choice` rows by hand sends its labels
  through this function.
* :func:`lane_row`: one SETTING / VALUE / DESCRIPTION row for the lists in editor style.
* :func:`column_header` / :class:`Lane`: the header line above a list with aligned lanes.
  On a narrow terminal, it makes its labels shorter. It does not wrap, and it does not
  lose a label. Also :func:`lane_header`, the ready header for the lanes of
  :func:`lane_row`.
* :func:`changes_phrase`: ``"1 staged change"`` / ``"3 staged changes"``.
* :func:`confirm_discard`: the shared confirm dialog when the user leaves staged changes.
* :func:`run_steps`: an entry flow of two or more prompts, run as a stack. The Esc key on
  one step goes back to the step before it, and the answer of that step is still kept.
* :func:`run_wizard`: the same chain, drawn in **one** floating list. Each step is a
  :class:`WizardPage` that the box turns to in place (``… · step 1 of 2``). Thus a flow
  that selects two things opens one dialog, not two dialogs on top of each other. The
  dialog closes before the screen that it collected the answers for opens.

The helpers apply these house rules (refer to the UX standards in ``CLAUDE.md``). A list
has no exit row. The Esc key leaves, and a row that did the same thing was not worth its
two lines. The only exception is the pair for staged changes. That pair is a choice, not
an exit, and it keeps its blank separator line above it. ``✓``/``✗`` (U+2713/U+2717) are
the only status marks, with the styles ``ok``/``err``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
)

from rich.cells import cell_len
from rich.text import Text

from ..platforms import get_platform
from .theme import glyph
from .tui import Choice, Separator
from .tui.emoji_width import cut_cells, drawable

if TYPE_CHECKING:
    from ..context import AppContext

#: The types that the label of a command row can have: plain text, or styled text whose
#: spans stay after the icon is cut (a destructive row with the ``err`` tint, a live unread
#: badge).
LabelT = str | Text


#: The separator between the status atoms of the app, in its two widths. ``·`` joins atoms
#: (the rule of the standards: ``Map · z12 · 34 nodes``). The roomy form is the default. A
#: surface that has too few cells uses the compact form. It does not move an atom to a
#: line of its own. The header puts its segments in this way (refer to
#: :func:`~meshterm.ui.menu._header`), and so does the reception row of the packet card.
#: The same compromise applies wherever cells are few.
SEP_ROOMY = "  ·  "
SEP_COMPACT = " · "


def command_icon(icon: str) -> str:
    """The glyph at the start of a command row, or empty where the platform has no icon lane.

    This is the half of :data:`~meshterm.platforms.Platform.menu_icons` that applies when
    the row is composed. It is for a row that builds its own icon lane (the actions in the
    body of a screen), not for a row that has the icon in its label string
    (:func:`command_label` is for that case). The function returns ``""`` and not a
    substitute glyph, and this is intentional. The caller measures the result, so an empty
    lane uses no cells at all, and the padding of the lane goes to the label.

    Args:
        icon: The icon of the row, as written on the regular platform.

    Returns:
        The icon as :func:`~meshterm.ui.theme.glyph` renders it, or ``""``.
    """
    return glyph(icon) if get_platform().menu_icons else ""


def icon_lane(icons: Iterable[str]) -> int:
    """The width in cells of the icon column: the widest mark in ``icons`` on this platform.

    The icons of a list are **not all the same width**, and the terminal decides which
    icon has which width. In the lexicon of the app, ten icons (``🗑 ✎ ⚙ ▶ ★ ↻ ↕ ⇄ ⌨ #``)
    use one cell, and the others use two. Thus a row that wrote ``icon + " "`` started its
    label one column to the left of the rows with two-cell icons. For this reason,
    ``🗑 Delete contact…`` was out of line under ``💾 Archive contact`` on the node page
    (JP, 2026-09-01). Thus a list declares the icons that it uses, measures the column one
    time, and pads each mark to that width.

    The width is measured, not hard-coded, because the marks change with the platform.
    The PicoCalc draws no icon lane on a command row at all (refer to
    :func:`command_icon`). Thus the column measures zero, and the labels get the cells
    back. The alignment rule is the same wherever the icons are.

    Args:
        icons: All the marks that the rows of the list can start with, as written on the
            regular platform.

    Returns:
        The width of the column in cells, or ``0`` where this platform draws no icons. An
        empty set also gives ``0``, and for a caller that is the same result.
    """
    return max((cell_len(command_icon(icon)) for icon in icons if icon), default=0)


def icon_mark(icon: str, style: str, lane: int) -> Text:
    """The mark of one row, with its tint, padded to ``lane`` cells, with its trailing space.

    This is the only place that writes the icon column of a command row. Thus a mark that
    is one cell narrower than the other marks cannot move the label after it. An empty
    lane (``lane`` of ``0``) adds nothing at all, also no separator: the cells are for the
    label.

    Args:
        icon: The icon of the row, as written on the regular platform.
        style: The theme style for the mark.
        lane: The width of the column, from :func:`icon_lane`.

    Returns:
        The padded mark, or an empty :class:`~rich.text.Text` where there is no lane.
    """
    mark = command_icon(icon) if icon else ""
    if not lane or not mark:
        return Text()
    text = Text(mark, style=style)
    text.append(" " * (lane - cell_len(mark) + 1))
    return text


def marked_label(icon: str, label: str, style: str, *, lane: int | None = None) -> Text:
    """A command row whose icon has a tint, which moves to the label if the icon goes.

    The app marks a destructive command with a tint on its icon, not on its words (``🗑``
    in ``err`` before a plain "Delete all records…"). On a platform without an icon lane,
    the icon is removed. If the tint goes with it, a delete row looks the same as all
    other rows. Thus the tint goes to the label instead. The *Factory reset* row of the
    config editor always used this form.

    Args:
        icon: The icon of the row, as written on the regular platform.
        label: The words of the row, with no icon and no leading space.
        style: The theme style for the mark (or for the label, when there is no icon).
        lane: The width in cells of the icon column, from :func:`icon_lane` over all the
            icons that the list uses. **Give it each time that the list has icons of
            different widths.** If you do not, the narrow icons start their labels one
            column too early. ``None`` (the default) measures only this icon. That is
            correct for a list whose rows all start with the same mark, and for a single
            action row.

    Returns:
        The composed label of the row.
    """
    width = icon_lane((icon,)) if lane is None else lane
    mark = icon_mark(icon, style, width)
    return Text.assemble(mark, label) if mark.plain else Text(label, style=style)


def command_label(label: LabelT) -> LabelT:
    """``label`` without its leading icon, where the platform removes the icon lane.

    The icon of a command row is *decoration*. It names the family of the action, and the
    label names the action. Thus the icon is the first thing to remove when there are too
    few cells (refer to :data:`~meshterm.platforms.Platform.menu_icons`). The icon is all
    the text before the first space of the label, but only when that head starts with a
    character that is not alphanumeric. Thus ``"🗑 Clear this slot…"`` and
    ``"↻ Read settings"`` both lose their head, and ``"Reorder"`` and ``"Trace target"``
    do not change.

    Do *not* send these through this function: a glyph that has data (the openness of a
    channel, the class of a packet, the type of a node), or a status mark on an outcome
    row or a commit row (``✓ Apply…``, ``✗ Back — discard…``, built by
    :func:`exit_rows`). On each platform, those glyphs tell something that the label does
    not tell.

    Args:
        label: The full label of the row, with the icon: a plain string or a styled
            :class:`~rich.text.Text` (its spans stay after the cut).

    Returns:
        The label, without the icon or unchanged. The type is the type that went in.
    """
    if get_platform().menu_icons:
        return label
    plain = label.plain if isinstance(label, Text) else label
    head = _icon_head(plain)
    if not head:
        return label
    # Remove the gap with the icon. A row that padded a one-cell mark to the width of the
    # two-cell marks ("↕  Reorder channels") must not keep the padding.
    return label[_words_start(plain, head) :]


def _icon_head(plain: str) -> int:
    """The length in characters of the leading icon of ``plain``, or ``0`` if it has none.

    This is the only definition of "this label starts with an icon". :func:`command_label`
    (which removes the head) and :func:`align_icons` (which pads it) both use it. The icon
    is all the text before the first space, when that head starts with a character that
    is not alphanumeric and words follow it.
    """
    head, sep, rest = plain.partition(" ")
    if not sep or not head or head[0].isalnum() or not rest.strip():
        return 0
    return len(head)


def _words_start(plain: str, head: int) -> int:
    """The character index where the words start, after an icon of ``head`` characters."""
    return len(plain) - len(plain[head:].lstrip(" "))


def align_icons(labels: Iterable[LabelT]) -> list[LabelT]:
    """``labels``, with each leading icon padded so that all the words start in one cell.

    This is the only icon column for a list whose icons are *in* its label strings. The
    terminal draws ``↻ ⌨ 🗑 ✎ ⚙`` in one cell and ``📡 💾 🔐`` in two cells. Thus rows
    written as ``icon + " "`` start their words one column apart. This fault occurred
    again and again, on one screen after another (the node page, the main menu, the
    repeater admin), until this function measured the column, one time, for each list.
    :func:`menu_rows` sends its labels through this function, so a screen built on
    :func:`menu_rows` never has to think about it. A list that builds its :class:`Choice`
    rows by hand sends its labels through this function instead.

    Each label first goes through :func:`command_label`. Thus, where the platform draws no
    icon lane, the icons are already removed and nothing is padded. A label without an
    icon head is returned unchanged. When all the heads have the same width, the spacing
    of a single icon row also does not change. If a list is already aligned (by
    :func:`marked_label` with a ``lane``, or by an earlier pass), a second alignment
    changes nothing: the function replaces the gap that is there and never adds to it.

    Args:
        labels: The labels of the list, in order, with the icons: plain strings or styled
            :class:`~rich.text.Text` (their spans and base style stay, and the base style
            becomes a span).

    Returns:
        The labels: a padded :class:`~rich.text.Text` for each label that starts with an
        icon, and the original object for each label that does not.
    """
    stripped = [command_label(label) for label in labels]
    plains = [label.plain if isinstance(label, Text) else label for label in stripped]
    heads = [_icon_head(plain) for plain in plains]
    lane = max(
        (cell_len(plain[:head]) for plain, head in zip(plains, heads, strict=True) if head),
        default=0,
    )
    aligned: list[LabelT] = []
    for label, plain, head in zip(stripped, plains, heads, strict=True):
        if not head:
            aligned.append(label)
            continue
        text = label if isinstance(label, Text) else Text(label)
        pad = " " * (lane - cell_len(plain[:head]) + 1)
        aligned.append(Text.assemble(text[:head], pad, text[_words_start(plain, head) :]))
    return aligned


def exit_rows(staged: int, *, apply_value: Any, back_value: Any) -> list:
    """The exit group of an editor: no rows at all until something is staged.

    A list does not show its own exit. The Esc key leaves, on both platforms. A row that
    only did the same thing as Esc used two lines of each screen to say what Esc already
    said (the ``Back`` row of the whole app, which we removed on 2026-08-29).

    The rows here are for the case where to leave is more than only to leave. When
    changes are staged, a ``✓ Apply …`` row with the ``ok`` mark is above a
    ``✗ Back — discard staged changes`` row with the ``err`` mark. That pair is a
    *choice*, not an exit. ``Apply`` has no keyboard key of its own, so it must have a
    visible counterpart that names the cost of the other way out. A choice keeps its rows
    for the same reason that a confirm dialog keeps its Cancel button.

    Args:
        staged: The number of staged changes (0 = no rows at all).
        apply_value: The value that the screen returns when the user selects the Apply
            row.
        back_value: The value that the screen returns when the user selects the Back
            row. It is the same as the value for Esc, because both run the discard
            confirm of the caller.

    Returns:
        The rows to add: nothing when no change is staged, or else a blank separator and
        the pair.
    """
    if not staged:
        return []
    return [
        Separator(" "),
        Choice(
            title=Text.assemble(("✓ ", "ok"), f"Apply {changes_phrase(staged)}"),
            value=apply_value,
        ),
        Choice(
            title=Text.assemble(("✗ ", "err"), "Back — discard staged changes"),
            value=back_value,
        ),
    ]


def menu_rows(rows: Iterable[tuple[str | Text, str, Any]]) -> list:
    """:class:`Choice` rows with a label and a description, in two aligned lanes.

    This is the menu form that the channel detail and the Actions of the Device config and
    repeater admin pages use. It has no header line (these rows are commands, not table
    data). The description column starts two cells after the widest label. The padding is
    calculated in display cells, so that a double-width emoji cannot move the column.

    The labels first go through :func:`align_icons`, and this gives two results. First, on
    a platform that draws no icon lane, the icons are removed *before* the lane is
    measured. Thus the description column moves to the left with the labels, and it does
    not become irregular behind them. Second, where icons are drawn, the words after a
    one-cell ``⌨`` and a two-cell ``📡`` start in the same cell. Thus no caller has to
    measure an icon column for a list that this function builds.

    Each row also sets the description column as its
    :attr:`~meshterm.ui.tui.select.Choice.hscroll_from`. Thus, on an ``hscroll`` list,
    the ←→ keys move the description under a label that does not move.

    Args:
        rows: ``(label, description, value)`` triples. A :class:`Text` label keeps its
            own styling (a destructive row with the ``err`` tint, a live unread badge).

    Returns:
        One :class:`Choice` for each row, with the lanes aligned across all the rows.
    """
    rows = list(rows)
    labels = align_icons(label for label, _, _ in rows)
    prepared = [
        (Text(label) if isinstance(label, str) else label.copy(), help_text, value)
        for label, (_, help_text, value) in zip(labels, rows, strict=True)
    ]
    width = max((cell_len(label.plain) for label, _, _ in prepared), default=0)
    items: list = []
    for label, help_text, value in prepared:
        row = Text()
        row.append_text(label)
        row.append(" " * (width - cell_len(label.plain) + 2))
        row.append(help_text, style="muted")
        # The label lane identifies the row. Only the description overflows, so the
        # h-scroll moves only the description, and the name does not move (refer to
        # Choice.hscroll_from).
        items.append(Choice(title=row, value=value, hscroll_from=width + 2))
    return items


def lane_row(label: str, value: Text, help_text: str, label_w: int, value_w: int) -> Text:
    """Put one row into the SETTING / VALUE / DESCRIPTION lanes (padded in cells).

    This is the form of the editor lists (Device config, repeater admin). The padding is
    calculated in display cells, so that a wide glyph in a value cannot move the lanes.
    The description stays muted under the highlight of the select list, because the
    highlight changes only the base style of the row.
    """
    row = Text(label)
    row.append(" " * (label_w - cell_len(label) + 2))
    row.append_text(value)
    row.append(" " * (value_w - cell_len(value.plain) + 2))
    row.append(help_text, style="muted")
    return row


@dataclass(frozen=True)
class Lane:
    """One lane of a column header: its label, its shorter forms, and its width.

    Attributes:
        label: The label of the lane: a plain string, or its forms with the longest first
            (``("DESCRIPTION", "DESC")``) for a lane that can give cells back on a narrow
            terminal. A lane becomes shorter only when the line is too wide.
        width: The number of cells to which the label is padded: the width of the lane
            *plus* the gap before the next lane. Thus a label that is wider than its
            column uses that gap, and does not move each lane after it to the right
            (``LAST`` above the ages of 3 cells in the chat picker). Zero (the default)
            for a trailing lane, which goes to the edge.
    """

    label: str | Sequence[str]
    width: int = 0

    @property
    def forms(self) -> tuple[str, ...]:
        """The labels of the lane, the longest first (a plain string is its only form)."""
        return (self.label,) if isinstance(self.label, str) else tuple(self.label)


def column_header(lanes: Sequence[Lane], width: int, *, indent: int = 2) -> str:
    """The column header line above a list with aligned lanes, fitted to ``width``.

    This is the only header builder for each list that pads its rows into columns (the
    two setting editors, the chat picker). The lanes go from left to right, each at its
    own width, after ``indent`` cells for the pointer column. Thus each label is exactly
    above the lane that it names.

    A header is one row, and it stays one row. Where the line is wider than ``width``,
    the lanes use their shorter labels. The function starts from the right, because the
    fixed lanes are padded to the content of their rows, and only a trailing lane can
    give a cell back. If the line is still too wide, the function cuts it with an
    ellipsis.

    A trailing lane whose rows can disappear (a chart that becomes shorter to fit, and
    then goes) has ``""`` as its last form. Thus its label goes with it, and the function
    does not cut the line in the lane before it. The padding after the last label that is
    drawn is empty space, so it never counts against ``width``. The header never wraps: a
    pinned header (refer to :attr:`~meshterm.ui.tui.select.Separator.pinned`) is drawn
    outside the slice of the body, where a second header row takes one row from the
    content.

    Args:
        lanes: The lanes, in display order.
        width: The number of cells that the line must fit into: the render width of the
            screen.
        indent: The leading padding in cells: the 2-cell pointer column of the select
            screen, plus any glyph lane that the rows draw before their first value.

    Returns:
        The header line, with a maximum width of ``width`` cells.
    """
    picked = [0] * len(lanes)

    def line() -> str:
        out = " " * indent
        for lane, form in zip(lanes, picked, strict=True):
            label = lane.forms[form]
            out += label + " " * max(0, lane.width - cell_len(label))
        return out.rstrip()

    at = len(lanes) - 1
    while at >= 0 and cell_len(line()) > width:
        if picked[at] + 1 < len(lanes[at].forms):
            picked[at] += 1  # this lane has a shorter form, so use it
        else:
            at -= 1  # no shorter form remains, so try the lane on its left
    text = line()
    return fit_cells(text, width) if cell_len(text) > width else text


def lane_header(label_w: int, value_w: int, width: int) -> str:
    """The SETTING / VALUE / DESCRIPTION header above the lanes of :func:`lane_row`.

    This is the header that goes with the row builder. The two editor lists (Device
    config and repeater admin) both use it, so that the same lanes have the same header.
    ``DESCRIPTION`` becomes ``DESC`` where the two lanes before it leave no space for it.
    Long setting labels and a staged ``current → new`` value can push the full word past
    the edge of a terminal that is 72 cells wide.
    """
    return column_header(
        [
            Lane("SETTING", label_w + 2),
            Lane("VALUE", value_w + 2),
            Lane(("DESCRIPTION", "DESC")),
        ],
        width,
    )


#: The row that both editor pages (Device config, repeater admin) put above their
#: coordinates. It opens the map picker and stages the latitude and the longitude together.
PICK_LOCATION_LABEL = "Pick location on map…"
PICK_LOCATION_HELP = "Point at the map to set both coordinates"


def changes_phrase(count: int) -> str:
    """``"1 staged change"`` / ``"3 staged changes"`` for dialogs and menu rows."""
    return f"{count} staged change{'' if count == 1 else 's'}"


async def confirm_discard(ctx: AppContext, staged: int, *, verb: str = "applying") -> bool:
    """Ask before the user leaves and the staged changes are lost. ``True`` means discard.

    This is the shared dialog for unsaved changes. The safe way out (Keep editing) is on
    the left. The Discard button, which commits, is on the right and is the default, as
    the dialog convention of the app tells.

    Args:
        ctx: The shared application context (for the UI surface).
        staged: The number of changes that the user discards.
        verb: The verb for the action that the changes did not get: ``"applying"`` for
            the local editor, ``"sending"`` for a remote editor. The function puts it
            into the question.

    Returns:
        ``True`` if the user chose Discard. ``False`` if the user chose Keep editing.
    """
    choice = await ctx.ui.dialog(
        f"Discard {changes_phrase(staged)} without {verb} them?",
        [("Keep editing", "keep"), ("Discard", "discard")],
        title="Unsaved changes",
        default=1,
        danger=True,
    )
    return choice == "discard"


async def run_steps(steps: Sequence[Callable[[list], Awaitable[Any]]]) -> list | None:
    """Run a chain of prompts as a stack. Esc on a step goes back to the step before it.

    An entry flow that asks two or more things in sequence (select a slot, then name the
    channel, or select a recipient, write the message, and choose when it goes) is a
    stack like all others. The Esc key undoes the last thing that you did, not the full
    flow. Before, the answers came from one straight sequence of awaits, and each Esc
    abandoned all of the flow. Thus a mistyped channel key of 32 hex digits also lost the
    name typed before it.

    Each step is awaited with the answers collected up to that point. It is the same list
    each time. Thus a step can label itself with an earlier answer, *and* offer its own
    previous answer as its default when the user comes back to it. The step returns its
    value, or ``None`` to step back. ``None`` from the first step ends the flow, because
    there is no step before it. A step whose real answer can be ``None`` (Courier's "when
    it's next heard") returns a sentinel instead, and the caller reads it back.

    Args:
        steps: The prompts, in order. Index ``i`` reads and writes ``values[i]``.

    Returns:
        The answers, in the order of the steps, or ``None`` if the user abandoned the
        flow.
    """
    values: list = [None] * len(steps)
    index = 0
    while index < len(steps):
        answer = await steps[index](values)
        if answer is None:
            index -= 1
            if index < 0:
                return None
            continue
        values[index] = answer
        index += 1
    return values


@dataclass(frozen=True)
class WizardPage:
    """One step of :func:`run_wizard`: what the shared list shows during that step.

    Attributes:
        title: The heading of the box for this step. Name the feature and the step
            (``"TX optimize — node to tune · step 1 of 2"``), because only the title tells
            the user which page is open.
        items: The rows to select from (:class:`Choice` and :class:`Separator`).
        prompt: The question of one line above the rows.
        default: The value to highlight. When the user comes back to a step, the step
            gives its previous answer here. :func:`run_steps` uses the same convention for
            its steps.
    """

    title: str
    items: list
    prompt: str = ""
    default: Any = None


async def run_wizard(
    session: Any,
    pages: Sequence[Callable[[list], WizardPage | Awaitable[Any]]],
) -> list | None:
    """Ask a chain of selections in **one** floating list, and turn its page between steps.

    This is the dialog form of :func:`run_steps`, for a flow whose steps are lists to
    select from. The stack semantics are the same. Esc on a step goes back to the step
    before it, and the answer of that step is still highlighted. Esc on the first step
    abandons the flow. But the chain is drawn as one box that turns its page
    (:meth:`~meshterm.ui.tui.select.SelectScreen.turn_page`), not as one dialog pushed
    over another.

    Two pickers on top of each other look like two places, and a dialog is not a place:
    it informs, confirms, or asks for a choice, and then it closes. Thus the box is pushed
    one time for the full chain, and popped before the answers are returned. Then nothing
    floats under the screen that the caller opens with the answers (a full-screen sweep,
    a session). Esc from *that* screen goes back to where the flow started.

    Also, the box *is* a box. Without this, a dialog that is the only screen on the stack
    (the menu is popped while a tool runs) is drawn full-frame. Thus the visit floats
    over a blank base, the same as each one-shot dialog.

    A step is a callable that gets the answers up to that point (index ``i`` reads
    ``values[i]``). It returns a :class:`WizardPage` for the box to turn to. A step that
    is not a list (a typed value, where there is nothing to list) returns an awaitable
    instead. That awaitable runs its own prompt, which floats over the last page of the
    box, and resolves to the answer, or to ``None`` to step back. The first step must be a
    page, because before it there is no box for anything else to float over.

    Args:
        session: The active :class:`~meshterm.ui.tui.session.TuiSession`.
        pages: The steps, in order.

    Returns:
        The answers, in the order of the steps, or ``None`` if the user abandoned the
        flow.
    """
    from .tui import SelectScreen
    from .tui.screen import CANCEL

    values: list = [None] * len(pages)
    index = 0
    step = pages[0](values)
    if not isinstance(step, WizardPage):
        raise TypeError("the first step of a wizard must be a WizardPage")
    screen = SelectScreen(step.title, step.items, prompt=step.prompt, default=step.default)
    async with session.stay(screen, dialog=True) as visit:
        while True:
            answer = await visit.result() if isinstance(step, WizardPage) else await step
            if answer is CANCEL or answer is None:
                index -= 1
                if index < 0:
                    return None
            else:
                values[index] = answer
                index += 1
                if index == len(pages):
                    return values
            # In both cases, the box now shows the step that asks now. A page that the user
            # turned back to is also built again, so it opens on its previous answer.
            step = pages[index](values)
            if isinstance(step, WizardPage):
                screen.turn_page(
                    step.items, title=step.title, prompt=step.prompt, default=step.default
                )


def section_heading(label: str) -> Separator:
    """A ``── Label ──`` section heading for a grouped select list, in the ``heading`` grey.

    This is the only form for the headings of each grouped list (the categories of the
    main menu, the setting groups of the config editor, the Alerts/Watched nodes of
    Watchtower, the Outbox/Finished of Courier). Thus sections look the same on each
    screen.

    This function also makes a heading a *landmark*. The row is marked
    :attr:`~meshterm.ui.tui.select.Separator.heading`. Thus it pins again to the top row
    when its section scrolls under it, and the Ctrl+PageUp/PageDown jumps go from heading
    to heading. A row built by hand does not get this behaviour. Thus always use this
    function.

    A heading that starts with an icon (``📡 Channels``, the mark of a trophy discipline)
    loses the icon on a platform that draws no icon lane, the same as a command row. A
    heading is the same type of label. Also, a substitute glyph next to the rule, which
    already draws ``──``, looks like noise (refer to :func:`command_label`).

    Grey, never the accent of the frame: a rule in the colour of the border looks like a
    part of the frame, not like a heading in it.
    """
    return Separator(f"── {command_label(label)} ──", style="heading", heading=True)


def fit_cells(text: str, width: int, *, align: str = "left") -> str:
    """Fit ``text`` into exactly ``width`` display cells, with an ellipsis if it is too long.

    This is the helper that fits text into a lane, for each name or label column with a
    fixed width. The padding and the cut are measured in display cells (a wide glyph
    counts 2). Thus a name with an emoji or a CJK character cannot move the lanes, as
    ``str.ljust`` can.

    The text of a lane is very often the **name of a node**. The owner of the node writes
    that name, and the name comes over the radio. Thus this function, and not each
    caller, does two more things:

    * It changes each character that a terminal cannot draw into a space
      (:func:`~meshterm.ui.tui.emoji_width.drawable`). A newline or an escape in a name
      ends the row in the middle of a lane, and no width table can measure that.
    * It cuts **between** glyphs (:func:`~meshterm.ui.tui.emoji_width.cut_cells`), never
      in a glyph. A cut at each code point can divide an emoji sequence in two. The
      joiner that stays at the end then hides the ellipsis that follows it. Then the lane
      measures one cell less than the terminal draws, and each column to its right starts
      too late.

    Args:
        text: The text to fit.
        width: The exact width in cells of the result.
        align: ``"left"`` (pad on the right) or ``"right"`` (pad on the left).

    Returns:
        A string that is exactly ``width`` cells wide.
    """
    if not text.isprintable():
        text = drawable(text)
    if cell_len(text) > width:
        text = cut_cells(text, width - 1) + "…"
    pad = " " * max(0, width - cell_len(text))
    return pad + text if align == "right" else text + pad


__all__ = [
    "SEP_COMPACT",
    "SEP_ROOMY",
    "Lane",
    "WizardPage",
    "align_icons",
    "changes_phrase",
    "column_header",
    "command_label",
    "confirm_discard",
    "exit_rows",
    "fit_cells",
    "icon_lane",
    "icon_mark",
    "lane_header",
    "lane_row",
    "marked_label",
    "menu_rows",
    "run_steps",
    "run_wizard",
    "section_heading",
]
