# SPDX-License-Identifier: Apache-2.0
"""Shared select-menu chrome: the pieces every list screen builds the same way.

MeshTerm's list screens (the config editors, the channel manager, Watchtower, Courier,
the pickers) all share the same furniture: label-and-description rows padded into
aligned lanes. Before this module each screen hand-rolled its own copy, and they drifted
(``← Back``, ``← Close``, missing blank lines, ``✖`` for ``✗``). These helpers are now
the one way to build it, so the app's row language and alignment stay uniform by
construction:

* :func:`exit_rows` — the staged-changes exit group: ``✓ Apply n staged changes`` above
  an err-tinted ``✗ Back — discard staged changes``, and *nothing at all* when clean.
* :func:`menu_rows` — label + muted description :class:`Choice` rows in two aligned
  lanes, padded in display cells so double-width emoji can't skew the description
  column (the editor pages' Actions presentation), its icons lined up by :func:`align_icons`.
* :func:`align_icons` — THE icon column for labels that carry their icon inside the string:
  a one-cell ``⌨`` and a two-cell ``📡`` start their words in the same cell. A list that
  builds its :class:`Choice` rows by hand runs its labels through it.
* :func:`lane_row` — one SETTING / VALUE / DESCRIPTION row for the editor-style lists.
* :func:`column_header` / :class:`Lane` — the header line over a lane-aligned list, which
  abbreviates its labels to fit a narrow terminal instead of wrapping or losing one, and
  :func:`lane_header`, the ready-made header for :func:`lane_row`'s lanes.
* :func:`changes_phrase` — ``"1 staged change"`` / ``"3 staged changes"``.
* :func:`confirm_discard` — the shared are-you-sure dialog for leaving staged changes.
* :func:`run_steps` — an entry flow of two or more prompts, run as a stack: Esc on one
  step goes back to the step before it, with what was answered there still in hand.
* :func:`run_wizard` — the same chain drawn in **one** floating list: each step is a
  :class:`WizardPage` the box turns to in place (``… · step 1 of 2``), so a flow that
  picks two things opens one dialog, not two stacked ones — and the dialog is gone
  before the screen it was gathering for opens.

House rules the helpers encode (see the UX standards in ``CLAUDE.md``): a list carries
no exit row — Esc leaves, and a row repeating it earned nothing for its two lines; the
one survivor is the staged-changes pair, which is a choice rather than an exit and keeps
its blank separator line above it. ``✓``/``✗`` (U+2713/U+2717) are THE status marks,
styled ``ok``/``err``.
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

#: What a command row's label may be: plain text, or styled text whose spans survive the
#: icon cut (an err-tinted destructive row, a live unread badge).
LabelT = str | Text


#: The app's status-atom separator, in its two widths. ``·`` chains atoms (the standards'
#: rule — ``Map · z12 · 34 nodes``); the roomy form is the default, and a surface running
#: out of cells falls back to the compact one rather than folding an atom onto a line of
#: its own. The header lays its segments out this way (see :func:`~meshterm.ui.menu._header`)
#: and so does the packet card's reception row — the same trade, wherever the cells are the
#: scarce thing.
SEP_ROOMY = "  ·  "
SEP_COMPACT = " · "


def command_icon(icon: str) -> str:
    """The glyph a command row leads with — empty where the platform drops the icon lane.

    The compose-time half of :data:`~meshterm.platforms.Platform.menu_icons`, for a row
    that builds its own icon lane (a screen's body actions) rather than carrying the icon
    inside a label string — :func:`command_label` is that case. Returning ``""`` rather
    than a stand-in is the point: the caller measures what comes back, so an emptied lane
    costs no cells at all, and the padding it would have taken goes to the label.

    Args:
        icon: The row's icon, as written on the regular platform.

    Returns:
        :func:`~meshterm.ui.theme.glyph`'s rendering of it, or ``""``.
    """
    return glyph(icon) if get_platform().menu_icons else ""


def icon_lane(icons: Iterable[str]) -> int:
    """THE icon column's width in cells: the widest mark among ``icons`` on this platform.

    A list's icons are **not all the same width**, and the terminal is the authority on
    which are which: of the app's lexicon, ten (``🗑 ✎ ⚙ ▶ ★ ↻ ↕ ⇄ ⌨ #``) draw one cell
    and the rest two. A row that simply wrote ``icon + " "`` therefore started its label a
    column left of its two-cell siblings — which is what put ``🗑 Delete contact…`` out of
    line under ``💾 Archive contact`` on the node page (JP, 2026-09-01). So a list declares
    the icons it uses, measures the column once, and pads every mark out to it.

    Measured rather than hard-coded because the marks themselves change with the platform:
    the PicoCalc draws no icon lane on a command row at all (see :func:`command_icon`), so
    the column measures zero and the labels take back the cells — the same alignment rule
    wherever the icons land.

    Args:
        icons: Every mark the list's rows can lead with, as written on the regular platform.

    Returns:
        The column width in cells, or ``0`` where this platform draws no icons (and for an
        empty set, which is the same thing to a caller).
    """
    return max((cell_len(command_icon(icon)) for icon in icons if icon), default=0)


def icon_mark(icon: str, style: str, lane: int) -> Text:
    """One row's mark, tinted, padded to ``lane`` cells, its trailing space included.

    The single place a command row's icon column is written, so a mark one cell narrower
    than its neighbours cannot shift the label after it. An emptied lane (``lane`` of
    ``0``) appends nothing at all, separator included — the cells belong to the label.

    Args:
        icon: The row's icon, as written on the regular platform.
        style: The theme style the mark is drawn in.
        lane: The column width from :func:`icon_lane`.

    Returns:
        The padded mark, or empty :class:`~rich.text.Text` where there is no lane.
    """
    mark = command_icon(icon) if icon else ""
    if not lane or not mark:
        return Text()
    text = Text(mark, style=style)
    text.append(" " * (lane - cell_len(mark) + 1))
    return text


def marked_label(icon: str, label: str, style: str, *, lane: int | None = None) -> Text:
    """A command row whose icon carries a tint — the tint moving to the label if it goes.

    The app marks a destructive command by tinting its icon, not its words (``🗑`` in
    ``err`` before a plain "Delete all records…"). Drop the icon on a platform without an
    icon lane and the tint would go with it, leaving a delete row looking like any other,
    so it lands on the label instead — the presentation the config editor's *Factory
    reset* has always used.

    Args:
        icon: The row's icon, as written on the regular platform.
        label: The row's words, with no icon and no leading space.
        style: The theme style the mark (or, iconless, the label) is drawn in.
        lane: The icon column's width in cells, from :func:`icon_lane` over every icon the
            surrounding list uses. **Pass it whenever the list mixes icon widths**, or the
            narrow ones start their labels a column early. ``None`` (the default) measures
            this icon alone, which is right for a list whose rows all lead with the same
            mark, and for the lone action row.

    Returns:
        The composed row label.
    """
    width = icon_lane((icon,)) if lane is None else lane
    mark = icon_mark(icon, style, width)
    return Text.assemble(mark, label) if mark.plain else Text(label, style=style)


def command_label(label: LabelT) -> LabelT:
    """``label`` with its leading icon dropped where the platform drops the icon lane.

    A command row's icon is *decoration*: it names the action's family while the label
    names the action, so it is the first thing to go when cells are scarce (see
    :data:`~meshterm.platforms.Platform.menu_icons`). The icon is taken to be everything
    before the label's first space, and only when that head starts with a non-alphanumeric
    character — so ``"🗑 Clear this slot…"`` and ``"↻ Read settings"`` both lose their
    head while ``"Reorder"`` and ``"Trace target"`` pass through untouched.

    What must *not* come through here: a glyph carrying data (a channel's openness, a
    packet's class, a node's type) or a status mark on an outcome or commit row
    (``✓ Apply…``, ``✗ Back — discard…``, built by :func:`exit_rows`). Those say something
    the label doesn't, on every platform.

    Args:
        label: The row's full label, icon included — a plain string or a styled
            :class:`~rich.text.Text` (whose spans survive the cut).

    Returns:
        The label, iconless or unchanged. The type is the one that went in.
    """
    if get_platform().menu_icons:
        return label
    plain = label.plain if isinstance(label, Text) else label
    head = _icon_head(plain)
    if not head:
        return label
    # Take the gap with the icon: a row that padded a one-cell mark out to the width of
    # its two-cell siblings ("↕  Reorder channels") must not leave the padding behind.
    return label[_words_start(plain, head) :]


def _icon_head(plain: str) -> int:
    """How many characters of ``plain`` are its leading icon — ``0`` when it has none.

    The one definition of "this label leads with an icon", shared by :func:`command_label`
    (which drops the head) and :func:`align_icons` (which pads it): everything before the
    first space, when that head starts with a non-alphanumeric character and words follow.
    """
    head, sep, rest = plain.partition(" ")
    if not sep or not head or head[0].isalnum() or not rest.strip():
        return 0
    return len(head)


def _words_start(plain: str, head: int) -> int:
    """The character index where the words after an ``head``-long icon begin."""
    return len(plain) - len(plain[head:].lstrip(" "))


def align_icons(labels: Iterable[LabelT]) -> list[LabelT]:
    """``labels`` with every leading icon padded out so the words all start in one cell.

    THE icon column for a list whose icons ride *inside* its label strings. The terminal
    draws ``↻ ⌨ 🗑 ✎ ⚙`` in one cell and ``📡 💾 🔐`` in two, so rows written ``icon + " "``
    start their words a column apart — the fault that kept reappearing screen by screen
    (the node page, the main menu, the repeater admin) until the column was measured here,
    once, for every list. :func:`menu_rows` runs its labels through it, so a screen built on
    that never has to think about it; a list that builds its :class:`Choice` rows by hand
    passes its labels through this instead.

    Each label first passes :func:`command_label`, so where the platform draws no icon lane
    the icons are already gone and nothing is padded. A label with no icon head is returned
    as it came, and so is a lone icon row's spacing when every head is the same width.
    Re-aligning a list that was already aligned (by :func:`marked_label` with a ``lane``, or
    by an earlier pass) changes nothing: the existing gap is replaced, never added to.

    Args:
        labels: The list's labels in order, icons included — plain strings or styled
            :class:`~rich.text.Text` (whose spans and base style survive, the base style
            carried as a span).

    Returns:
        The labels, a padded :class:`~rich.text.Text` for every one that leads with an
        icon and the original object for every one that doesn't.
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
    """The editor exit group — nothing at all until there is something staged.

    A list does not advertise its own exit: Esc leaves, on both platforms, and a row
    that only repeated it cost two lines of every screen to say what Esc already said
    (the app-wide ``Back`` row, retired 2026-08-29). What survives here is the case
    where leaving is not merely leaving: with changes staged, an ok-marked
    ``✓ Apply …`` row sits above an err-marked ``✗ Back — discard staged changes``.
    That pair is a *choice*, not an exit — ``Apply`` has no key of its own, so it needs
    a visible counterpart naming what the other way out costs — and a choice keeps its
    rows for the same reason a confirm dialog keeps its Cancel button.

    Args:
        staged: How many changes are staged (0 = no rows at all).
        apply_value: The value the Apply row resolves with.
        back_value: The value the Back row resolves with — identical to what Esc
            resolves, since both run the caller's discard confirm.

    Returns:
        The rows to append: nothing when clean, else a blank separator and the pair.
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
    """Label + description :class:`Choice` rows in two aligned lanes.

    The menu-style presentation shared by the channel detail and the Actions of the Device
    config and repeater-admin pages: no header line (these are commands, not tabular data), the
    description column starting two cells past the widest label, padding computed in
    display cells so a double-width emoji can't skew it.

    The labels pass through :func:`align_icons` first, which is two guarantees at once: a
    platform that draws no icon lane loses the icons *before* the lane is measured (so the
    description column moves left with the labels rather than going ragged behind them),
    and where icons are drawn, a one-cell ``⌨`` and a two-cell ``📡`` start their words in
    the same cell — no caller has to measure an icon column for a list built here. Each row
    also pins the description column as its
    :attr:`~meshterm.ui.tui.select.Choice.hscroll_from`, so on an ``hscroll`` list ←→ slide
    the description under a label that stays put.

    Args:
        rows: ``(label, description, value)`` triples. A :class:`Text` label keeps its
            own styling (an err-tinted destructive row, a live unread badge).

    Returns:
        One :class:`Choice` per row, lanes aligned across them all.
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
        # The label lane is the row's identity; only the description overflows, so the
        # h-scroll rides it alone and the name stays pinned (see Choice.hscroll_from).
        items.append(Choice(title=row, value=value, hscroll_from=width + 2))
    return items


def lane_row(label: str, value: Text, help_text: str, label_w: int, value_w: int) -> Text:
    """Lay one row out in the SETTING / VALUE / DESCRIPTION lanes (cell-padded).

    The editor-list presentation (device configuration, repeater admin): padding is
    computed in display cells so a wide glyph in a value can't skew the lanes, and the
    description stays muted under the select highlight (which only tints the row's
    base style).
    """
    row = Text(label)
    row.append(" " * (label_w - cell_len(label) + 2))
    row.append_text(value)
    row.append(" " * (value_w - cell_len(value.plain) + 2))
    row.append(help_text, style="muted")
    return row


@dataclass(frozen=True)
class Lane:
    """One lane of a column header: its label, any shorter forms, and its width.

    Attributes:
        label: The lane's label — a plain string, or its forms longest-first
            (``("DESCRIPTION", "DESC")``) for a lane that can give cells back on a narrow
            terminal. Only a lane the line actually overruns is ever shortened.
        width: Cells the label is padded to — the lane's own width *plus* the gap before
            the next lane, so a label wider than its column absorbs that gap instead of
            shifting every lane after it right (``WHEN`` over the chat picker's 3-cell ages).
            Zero (the default) for a trailing lane, which just runs to the edge.
    """

    label: str | Sequence[str]
    width: int = 0

    @property
    def forms(self) -> tuple[str, ...]:
        """The lane's labels, longest first (a bare string is its own only form)."""
        return (self.label,) if isinstance(self.label, str) else tuple(self.label)


def column_header(lanes: Sequence[Lane], width: int, *, indent: int = 2) -> str:
    """The column-header line over a lane-aligned list, fitted to ``width``.

    THE header builder for every list that pads its rows into columns (the two setting
    editors, the chat picker). Lanes are laid out left to right at their own widths, after
    ``indent`` cells clearing the pointer column, so each label lands exactly over the lane
    it names.

    A header is one row and stays one row. Where the line overruns ``width`` the lanes fall
    back to their shorter labels — from the right, since the fixed lanes are padded to their
    rows' content and only a trailing lane can actually give a cell back — and a line that
    still won't fit is ellipsized. It never wraps: a pinned header (see
    :attr:`~meshterm.ui.tui.select.Separator.pinned`) is drawn outside the body slice, where
    a second row would cost the content one.

    Args:
        lanes: The lanes in display order.
        width: Cells the line has to fit into — the screen's render width.
        indent: Leading pad in cells: the select screen's 2-cell pointer column, plus any
            glyph lane the rows draw before their first value.

    Returns:
        The header line, at most ``width`` cells wide.
    """
    picked = [0] * len(lanes)

    def line() -> str:
        out = " " * indent
        for lane, form in zip(lanes, picked, strict=True):
            label = lane.forms[form]
            out += label + " " * max(0, lane.width - cell_len(label))
        return out

    at = len(lanes) - 1
    while at >= 0 and cell_len(line()) > width:
        if picked[at] + 1 < len(lanes[at].forms):
            picked[at] += 1  # this lane has something shorter to offer — take it
        else:
            at -= 1  # spent; ask the lane to its left
    text = line()
    return fit_cells(text, width) if cell_len(text) > width else text


def lane_header(label_w: int, value_w: int, width: int) -> str:
    """The SETTING / VALUE / DESCRIPTION header over :func:`lane_row`'s lanes.

    The row builder's header twin, shared by the two editor lists (device configuration
    and repeater admin) so they head identical lanes identically. ``DESCRIPTION`` shortens
    to ``DESC`` where the two value lanes leave it no room — long setting labels and a
    staged ``current → new`` value can push the full word past a 72-column terminal.
    """
    return column_header(
        [
            Lane("SETTING", label_w + 2),
            Lane("VALUE", value_w + 2),
            Lane(("DESCRIPTION", "DESC")),
        ],
        width,
    )


#: The row both editor pages (Device config, repeater admin) head their coordinates with: it
#: opens the map picker and stages latitude and longitude together.
PICK_LOCATION_LABEL = "Pick location on map…"
PICK_LOCATION_HELP = "Point at the map to set both coordinates"


def changes_phrase(count: int) -> str:
    """``"1 staged change"`` / ``"3 staged changes"`` for dialogs and menu rows."""
    return f"{count} staged change{'' if count == 1 else 's'}"


async def confirm_discard(ctx: AppContext, staged: int, *, verb: str = "applying") -> bool:
    """Ask before dropping staged changes on the way out; ``True`` means discard.

    The shared unsaved-changes dialog: the safe way out (keep editing) sits left, the
    committing Discard right and default, per the app's dialog convention.

    Args:
        ctx: Shared application context (for the UI surface).
        staged: How many changes would be dropped.
        verb: What applying them would have done — ``"applying"`` for the local
            editor, ``"sending"`` for a remote one — woven into the question.

    Returns:
        ``True`` if the user chose to discard; ``False`` to keep editing.
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
    """Run a chain of prompts as a stack — Esc on a step goes back to the one before it.

    An entry flow that asks two or more things in a row (pick a slot, then name the
    channel; pick a recipient, write the message, choose when it goes) is a stack like any
    other: Esc undoes the last thing you did, not the whole flow. Gathering the answers as
    one straight run of awaits instead made every Esc abandon everything, so a mistyped
    32-hex channel key also cost the name typed before it.

    Each step is awaited with the answers gathered so far — the same list each time, so a
    step can label itself with an earlier answer *and* offer its own previous answer as its
    default when it is come back to — and returns its value, or ``None`` to step back.
    ``None`` from the first step ends the flow: there is nothing behind it. A step whose
    real answer can be ``None`` (Courier's "when it's next heard") returns a sentinel
    instead and the caller reads it back.

    Args:
        steps: The prompts, in order. Index ``i`` reads and writes ``values[i]``.

    Returns:
        The answers, positionally, or ``None`` if the flow was abandoned.
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
    """One step of :func:`run_wizard`: what the shared list shows while that step is asked.

    Attributes:
        title: The box's heading for this step — name the feature and the step
            (``"TX optimize — node to tune · step 1 of 2"``), since the title is the one
            thing that says which page the reader is on.
        items: The rows to pick from (:class:`Choice` and :class:`Separator`).
        prompt: The one-line question above the rows.
        default: The value to highlight — a step come back to hands over its previous
            answer here, the same convention :func:`run_steps` gives its steps.
    """

    title: str
    items: list
    prompt: str = ""
    default: Any = None


async def run_wizard(
    session: Any,
    pages: Sequence[Callable[[list], WizardPage | Awaitable[Any]]],
) -> list | None:
    """Ask a chain of picks in **one** floating list, turning its page between steps.

    The dialog form of :func:`run_steps`, for a flow whose steps are lists to pick from:
    the same stack semantics — Esc on a step goes back to the one before it, with its
    answer still highlighted; Esc on the first step abandons — but drawn as a single box
    that turns its page (:meth:`~meshterm.ui.tui.select.SelectScreen.turn_page`) rather
    than as one popup pushed over another. Two stacked pickers read as two places to be,
    and a popup is not a place: it informs, confirms, or chooses, and then it is gone.
    So the box is pushed once for the whole chain and popped before the answers are
    returned — whatever the caller opens with them (a full-screen sweep, a session) has
    nothing floating under it, and Esc from *there* lands where the flow was launched.
    And it *is* a box: a popup that happens to be the only frame on the stack (the menu is
    popped while a tool runs) would otherwise be drawn full-frame, so the visit floats over
    a blank base the way every one-shot dialog does.

    A step is a callable handed the answers so far (index ``i`` reads ``values[i]``). It
    returns a :class:`WizardPage` for the box to turn to, or — for a step that is not a
    list (a typed value where there is nothing to list) — an awaitable that runs its own
    prompt, floated over the box on its last page, and resolves to the answer or ``None``
    to step back. The first step must be a page; there is no box yet for anything else to
    float over.

    Args:
        session: The running :class:`~meshterm.ui.tui.session.TuiSession`.
        pages: The steps, in order.

    Returns:
        The answers, positionally, or ``None`` if the flow was abandoned.
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
            # Whichever way it went, the box now shows the step being asked — a page
            # turned back to is rebuilt too, so it opens on its previous answer.
            step = pages[index](values)
            if isinstance(step, WizardPage):
                screen.turn_page(
                    step.items, title=step.title, prompt=step.prompt, default=step.default
                )


def section_heading(label: str) -> Separator:
    """A ``── Label ──`` section heading for a grouped select list, in the ``heading`` grey.

    The one form every grouped list's headings take (the main menu's categories, the
    config editor's setting groups, Watchtower's Alerts/Watched nodes, Courier's
    Outbox/Finished), so sections read the same on every screen.

    It is also what makes a heading a *landmark*: the row is marked
    :attr:`~meshterm.ui.tui.select.Separator.heading`, so it re-pins to the top row once its
    section scrolls under it and the Ctrl+PageUp/PageDown jumps step by it. Building the row
    by hand is how a section loses that — go through here.

    A heading that leads with an icon (``📡 Channels``, a trophy discipline's mark) loses it
    on a platform that draws no icon lane, exactly as a command row does — a heading is the
    same kind of label, and a stand-in glyph beside the rule already drawing ``──`` reads as
    noise (see :func:`command_label`).

    Grey, never the frame's accent: a rule in the border's own colour reads as part of the
    frame rather than as a heading inside it.
    """
    return Separator(f"── {command_label(label)} ──", style="heading", heading=True)


def fit_cells(text: str, width: int, *, align: str = "left") -> str:
    """Fit ``text`` into exactly ``width`` display cells, ellipsizing overflow.

    The lane-fitting helper every fixed-width name/label column uses: padding and
    truncation are measured in display cells (wide glyphs count 2), so a name carrying
    an emoji or CJK character can't skew the lanes the way ``str.ljust`` would.

    A lane's text is very often a **node's name**, which is written by whoever owns the
    radio and arrives over the air, so two more things are settled here rather than by
    every caller: whatever a terminal cannot draw is folded to a space
    (:func:`~meshterm.ui.tui.emoji_width.drawable` — a newline or an escape in a name
    would end the row mid-lane, and no width table can measure that away), and the cut
    falls **between** glyphs (:func:`~meshterm.ui.tui.emoji_width.cut_cells`), never
    inside one. A cut a codepoint at a time slices an emoji sequence in half, and the
    stranded joiner it leaves swallows the ellipsis appended after it — the lane then
    measures one cell short of what the terminal draws, and every column to its right
    starts late.

    Args:
        text: The text to fit.
        width: The exact cell width to return.
        align: ``"left"`` (pad on the right) or ``"right"`` (pad on the left).

    Returns:
        A string measuring exactly ``width`` cells.
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
