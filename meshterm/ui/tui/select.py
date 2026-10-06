# SPDX-License-Identifier: Apache-2.0
"""The selectable-list screen: the reusable replacement for ``questionary.select``.

A :class:`SelectScreen` shows grouped choices that the arrows move through, with
type-to-filter. The list is limited to the viewport, and it scrolls to keep the highlighted
row visible. A choice value can be any object. Thus the same screen operates the main menu
(tool names), the device picker (device objects), and the config editor (setting keys).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from rich.cells import cell_len
from rich.text import Text

from ..pathline import ELIDE_HEAD, ELIDE_TAIL, cut_mark, cut_to
from .render import crop_cells, query_line, render_lines, render_to_ansi
from .screen import LazyLines, Screen


def _wants_width(fn: Callable) -> bool:
    """Whether a callable :attr:`Choice.title` takes the render width.

    A title callable with one or more required positional parameters is the width-aware
    form. A title callable with none (also a signature with only defaults) is the
    zero-argument form, which is called again at each paint. The signature decides this one
    time, at construction. A trial call never decides it, so that a ``TypeError`` raised in
    a title is not mistaken for a wrong arity.
    """
    try:
        parameters = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):  # a builtin with a signature that cannot be examined
        return False
    return any(
        p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        for p in parameters
    )


@dataclass
class Choice:
    """One selectable row.

    Attributes:
        title: The text of the row: a plain string or a Rich :class:`~rich.text.Text` (for
            a coloured segment such as an unread badge). Instead, each of them can be a
            zero-argument callable, resolved again at each paint. Thus a row can follow a
            state that changes while the list is open. It can also be a callable that takes
            the **render width** (one required positional argument). This is the ability of
            :class:`Separator`, extended to rows. Thus a row with a route can elide its
            middle to fit the terminal (``PathLine.ellipsized``), instead of a cut at the
            right edge.
        value: The value that is returned when the user chooses the row.
        deletable: Whether a press of Delete on this row asks to remove the row. When it is
            set, Delete resolves the list with a :class:`DeleteRequest` that wraps the value
            of this row, instead of a choice of the row. Thus the caller can run a remove
            flow and open the list again. It is off by default, so a usual list ignores
            Delete.
        detail: An optional second line, drawn under ``title`` at the indent of the
            pointer. It gives context that does not belong in the selectable line itself
            (for example, the weakest SNR, the sample count, or the provenance tag of a
            route). It never scrolls or wraps. If it is too wide, it ellipsizes itself.
            ``None`` (the default) draws nothing, so a usual row stays exactly one line. It
            can be a zero-argument callable, as ``title`` can.
        hscroll_from: The cells at the head of the row that stay pinned while ``←→``
            scroll all the text to their right (only in ``hscroll`` lists. With ``0``, the
            default, the full line slides). Some rows are fixed lanes, then one long run:
            for example, the rank, date, and score columns of the Trophy case, in front of
            the walk. The lanes tell the user where the row is in the list. If they slide
            off when the user reads the walk, the row loses its identity, and nothing is
            gained (the columns are the part that already fits). Set it to the cell width
            of that fixed block, measured from the row itself. Thus the scroll moves only
            the run that overflows. A value other than 0 also turns on the scroll of the
            list (refer to :class:`SelectScreen`). A head block has a meaning only as the
            part that does not move. Thus a row that pins one is a row made to scroll, on
            whichever screen its list is shown.
        fitted: The width-aware title is the full row at each width, not a cut of a longer
            row. Its lanes are fixed, and only a chart at the end gives cells back (the
            activity sparkline of the Channels list, which removes its oldest buckets to
            fit). Then the highlighted row of an ``hscroll`` list also draws that fitted
            form, instead of its natural form for ←→ to slide over. Past the edge of such a
            row, there is nothing that is worth a slide. Also, if the highlight changes the
            row back to a chart that is cut off, the fit is lost on the row that the user
            reads.
        pans: The row is a line of a table that ←→ pan as one unit. Each row and each
            column header that pans (:attr:`Separator.pans`) slides by the same shift. Thus
            the lanes stay under their labels, however far the user panned. The shift also
            stays when ↑↓ move, and does not go back to zero on each new row. A row that
            declares it turns on the pan of the list, in the same way as
            :attr:`hscroll_from` turns on the scroll. Rows that do not pan (an action row
            under the table) do not move. Refer to :class:`SelectScreen`.
    """

    title: str | Text | Callable[[], str | Text] | Callable[[int], str | Text]
    value: Any
    deletable: bool = False
    detail: str | Text | Callable[[], str | Text] | None = None
    hscroll_from: int = 0
    fitted: bool = False
    pans: bool = False

    def __post_init__(self) -> None:
        """Read the arity of the title callable one time, so that no paint must ask again."""
        # The arity of the callable form, read one time. Refer to _wants_width.
        self._title_wants_width = callable(self.title) and _wants_width(self.title)

    def text(self, width: int) -> str | Text:
        """The content of the row at ``width`` cells. This method resolves the callable forms."""
        if not callable(self.title):
            return self.title
        return self.title(width) if self._title_wants_width else self.title()

    @property
    def label(self) -> str | Text:
        """The natural (unbounded) text of the row. The filter and the measurements read it."""
        return self.text(_UNBOUNDED)

    def scroll_text(self, width: int) -> str | Text:
        """What this row draws (and ←→ slide) as the highlighted row of an ``hscroll`` list.

        It is the natural form, so that the full line can slide. For a row whose fitted
        form is complete (:attr:`fitted`), it is that form at ``width``, which by design
        leaves nothing to slide. The drawing, the clamp of the slide, and the ←→ atom of the
        footer all read this one answer. Thus they cannot disagree about whether the row
        moves.
        """
        return self.text(width) if self.fitted else self.label

    @property
    def detail_label(self) -> str | Text | None:
        """The current detail line of the row. A callable is resolved at each read."""
        return self.detail() if callable(self.detail) else self.detail


@dataclass
class DeleteRequest:
    """A request from a select list to remove the highlighted row.

    A press of Delete on a :class:`Choice` marked ``deletable`` resolves the select with
    this wrapper, instead of with the value of the row itself. Thus the caller can tell "the
    user wants to remove this" from "the user chose this". The caller usually runs a flow
    that confirms, then forgets, and then opens the list again.

    Attributes:
        value: The :attr:`Choice.value` of the row that the user asked to remove.
    """

    value: Any


@dataclass
class KeyRequest:
    """A shortcut key pressed on a select list, with the row that it was pressed on.

    The sibling of :class:`DeleteRequest`, for a list that declares its own bare-key
    shortcuts (refer to the ``keys`` argument of :class:`SelectScreen`). The list resolves
    with this request instead of with the value of a row. Thus the caller can tell "the user
    pressed this key" from "the user chose this row", and act on it before it opens the
    list again.

    Attributes:
        action: The token that the pressed key was declared for.
        value: The :attr:`Choice.value` of the highlighted row, or ``None`` if the list
            had no rows to highlight. A shortcut for the full screen ignores it.
    """

    action: Any
    value: Any = None


def _plain(label: str | Text) -> str:
    """The plain-text form of a row label, for the filter (a :class:`Text` gives its ``plain``)."""
    return label.plain if isinstance(label, Text) else label


def splice_hint(base: str, segment: str) -> str:
    """Insert ``segment`` into a footer hint, immediately before its last ``Esc`` clause.

    This keeps the "Esc last" rule of the hint grammar when a hint for one row (for
    example, ``Del remove`` on a deletable row) is added while the highlight is on that
    row. The segment goes in as its own ``·`` atom immediately before the last
    ``· Esc …``, not after it. A hint with no ``Esc`` clause gets the segment at the end.
    """
    marker = " · Esc"
    idx = base.rfind(marker)
    if idx == -1:
        return f"{base} · {segment}"
    return f"{base[:idx]} · {segment}{base[idx:]}"


def _esc_verb(base: str, verb: str) -> str:
    """Change the last ``Esc`` clause of a footer hint to ``Esc <verb>``.

    The verb of Esc is fixed for each surface (``back``, ``cancel``, ``keep``, and others).
    The exception is while a find-as-you-type filter stands. Then the press clears the
    filter instead of a leave, and the line must tell this. The map and the mesh walk
    always wrote this as ``Esc clear``. This function does the same change for a list. The
    list keeps its other atoms, and does not give all the line to the query.

    A hint with no ``Esc`` clause is returned without a change: it makes no claim to
    correct.
    """
    marker = " · Esc "
    idx = base.rfind(marker)
    if idx == -1:
        return base
    return f"{base[:idx]}{marker}{verb}"


def _insert_atom(base: str, atom: str, index: int = 1) -> str:
    """Insert ``atom`` as atom number ``index`` of a footer hint (atoms between ` · `).

    This function adds a conditional navigation atom (the ←→ scroll of one row)
    immediately after the first move atom. Thus the hint keeps its form: navigation, then
    actions, then ``Esc``. :func:`splice_hint`, by contrast, puts an action atom
    immediately before the last ``Esc``.
    """
    parts = base.split(" · ")
    parts.insert(min(index, len(parts)), atom)
    return " · ".join(parts)


@dataclass
class Separator:
    """A non-selectable row between choices.

    Attributes:
        title: The text of the row: a plain string, drawn all in :attr:`style`, or a Rich
            :class:`~rich.text.Text` with its own spans (for example, a column header that
            marks only its active sort column). A ``Text`` is rendered as written, and
            ``style`` is ignored. Instead, it can be a callable that takes the **render
            width** and returns one of the two. This is different from the zero-argument
            callable of :attr:`Choice.title`. Thus a column header can fit its labels to the
            terminal that draws it (refer to :func:`~meshterm.ui.menus.column_header`).
        style: The theme style for a title that is a string. Section headings give
            ``"heading"``, so that they look like landmarks. The default ``"muted"`` fits
            the structural rows (blank spacers, column-header lines, inline notes). A
            ``Text`` title has its own style, so this attribute is not used for it.
        pinned: Whether this row stays on the screen for the full list after it scrolls
            past, above each section heading that pins under it. It is for a column header,
            whose lane names have the same meaning in each section (refer to
            :meth:`~meshterm.ui.tui.screen.Screen.sticky_rows`). It is off by default: a
            usual separator pins only while its own section is on the screen. A maximum of
            one row in each list must set it. If more rows set it, the last row that is
            recorded wins.
        heading: Whether this row is a *section landmark*. A landmark pins again to the top
            line while the user scrolls through its section. It also marks the limits of
            the section jumps of Ctrl+PageUp/PageDown. It is off by default, because most
            separators label nothing below them: a blank spacer, an empty-state note, the
            wrapped description of a discipline. If one of these pins at the top, it tells
            nothing, it takes a content line, and it hides the heading above it. It is set
            through :func:`~meshterm.ui.menus.section_heading`, or by
            hand on a column header that is the only landmark of its block.
        pans: The column header of a table that ←→ pan as one unit (refer to
            :attr:`Choice.pans`). It is drawn in its fullest form, and it slides with the
            rows under it. Thus each label stays above its lane. Its layout is the same as a
            row: its first two cells are the pointer column, which does not move while the
            rest slides. :func:`~meshterm.ui.menus.column_header` already indents it in this
            way.
    """

    title: str | Text | Callable[[int], str | Text]
    style: str = "muted"
    pinned: bool = False
    heading: bool = False
    pans: bool = False

    def text(self, width: int) -> str | Text:
        """The content of the row at ``width`` cells. This method resolves a width-aware title."""
        return self.title(width) if callable(self.title) else self.title


Item = "Choice | Separator"

#: The width given to a width-aware :class:`Separator` or :class:`Choice` when the code
#: measures a natural width, not the width of a real terminal
#: (:attr:`SelectScreen.dialog_width`, :attr:`Choice.label`). It is wide enough that a
#: column header that fits itself, or a row that elides itself, returns its fullest form.
_UNBOUNDED = 10_000

#: The cells that the pointer of a choice row (``❯ `` / ``  ``) takes. A header that pans
#: also keeps this column still, so that its labels slide in step with the rows (refer to
#: :attr:`Separator.pans`).
_POINTER = 2


def _pans(items: list) -> bool:
    """Whether one of ``items`` is a table row or a header that pans (:attr:`Choice.pans`)."""
    return any(getattr(item, "pans", False) for item in items)


#: The navigation actions that move the highlight to another row. Thus an ``hscroll`` list
#: removes the horizontal shift of the current row (each row scrolls by itself, refer to
#: :meth:`SelectScreen.handle`). The filter edits reset the shift in their own branches. A
#: list that pans keeps its shift, because the table moved, not the row.
_HSHIFT_RESET_ACTIONS = frozenset(
    {
        "up",
        "down",
        "pageup",
        "pagedown",
        "home",
        "ctrl_home",
        "end",
        "ctrl_end",
        "ctrl_pageup",
        "ctrl_pagedown",
    }
)


class SelectScreen(Screen):
    """A grouped, filterable, single-choice list.

    Resolves with the value of the chosen :class:`Choice`, or with
    :data:`~meshterm.ui.tui.screen.CANCEL` if the user presses Esc.
    """

    #: The number of cells that one ←/→ press shifts an ``hscroll`` list (refer to ``hscroll``).
    _HSCROLL_STEP = 8

    @property
    def picocalc_lyra_lane(self):
        """The shared pager, the section jumps on F1/F2, and the delete of a row on F3.

        Ctrl+PageUp/PageDown move the highlight from one heading to the next. On the
        PicoCalc, they are the only binding in the app that the user cannot physically
        press: there is no paging key to hold Ctrl over. Thus a grouped list puts them on
        the lane. It keeps the shared handedness: a pair rises toward its outer key, so on
        this pair at the left edge, up takes F1 (refer to
        :data:`~meshterm.ui.tui.fkeys.DEFAULT_LANE`).

        Their two conditions tell different things, by the empty-or-dim rule of the lane. A
        list that has no headings leaves the two slots blank, because sections do not exist
        here. A grouped list whose live filter shows rows from only one section keeps the
        labels and dims them. The sections still exist, but there is no place to jump to at
        this time.

        ``Delete`` makes the same claim as the delete atom of the footer for each row, on a
        platform that draws no footer. Only a list that has :attr:`Choice.deletable` rows
        shows the slot, and the slot is enabled only on the rows that the key acts on. The
        chip shows ``Delete`` on each such list, instead of the hint text of each list. The
        reason is that six cells hold the verb of the key, not the object that it removes,
        and the highlighted row already names that object.
        """
        from .fkeys import FPair, default_lane

        # One pass over the shown rows for the full lane: this code runs at each paint on
        # the platform that draws the lane, and `_rows()` filters the item list again at each
        # call.
        rows = self._rows()
        lane = list(default_lane(nav=len(self._choices(rows)) > 1))
        if len(self._all_section_starts()) >= 2:
            live = len(self._section_starts(rows)) > 1
            lane[0] = FPair("Sect ↑", "ctrl_pageup", enabled=live)
            lane[1] = FPair("Sect ↓", "ctrl_pagedown", enabled=live)
        if self._delete_hint:
            choices = self._choices(rows)
            current = choices[max(0, min(self._index, len(choices) - 1))] if choices else None
            lane[2] = FPair("Delete", "delete", enabled=current is not None and current.deletable)
        return lane

    def __init__(
        self,
        title: str,
        items: list,
        *,
        prompt: str = "",
        default: Any = None,
        footer_hint: str | None = None,
        delete_hint: str = "",
        filterable: bool = True,
        hscroll: bool | None = None,
        hscroll_hint: str = "←→ scroll",
        keys: Mapping[str, Any] | None = None,
        key_hint: Callable[[Any], str] | None = None,
    ) -> None:
        """Build a select screen.

        Args:
            title: The short heading in the border.
            items: A list of :class:`Choice` and :class:`Separator`, in the order to show.
            prompt: An optional instruction in the box, above the list. Thus a floating
                select looks like the other dialogs (a prompt above its controls).
            default: A choice value to highlight at the start, if it is present.
            footer_hint: The footer key hint. The default hint mentions type-to-filter only
                when ``filterable`` is true (a fixed list must not show a filter that it
                ignores).
            delete_hint: A key-hint atom (for example, ``"Del remove"``) that the footer
                shows only while the highlighted row is :attr:`Choice.deletable`. Thus the
                hint shows the remove key exactly when the key acts, and hides it on rows
                that the key cannot change. Empty (the default) keeps the footer fixed.
            filterable: Whether typed text narrows the list. It is off for short, fixed lists
                (for example, the device picker at startup), where type-to-filter only causes
                problems.
            hscroll: Whether ←/→ scroll the highlighted row horizontally, so that the user can
                read a long line to its end (the alert log of the Watchtower). ``None`` (the
                default) lets the rows decide. A row that pins a head block
                (:attr:`Choice.hscroll_from`) turns the scroll on. Without one, the rows
                ellipsize at the right edge, and ←/→ do nothing. ``False`` keeps the scroll
                off, whatever the rows declare. This is for the editor pages (Device config,
                repeater admin), whose Actions rows pin heads, but whose lanes must end at
                the edge and not slide. Only a row that is wider than the width scrolls. A
                short row (and each separator or column header) does not move. The shift
                goes back to the start when the highlight moves to another row or when the
                filter changes: each row scrolls by itself, independently of the rest of the
                screen. A row can keep a head block out of the scroll
                (:attr:`Choice.hscroll_from`), so only its run that overflows slides. Each
                edge past which the line continues then shows a
                :func:`~meshterm.ui.pathline.cut_mark`. A list whose rows pan
                (:attr:`Choice.pans`) scrolls in a different way, and this way has priority.
                ←→ slide the full table: its column header and each row that pans move by
                one shared shift, clamped to the widest of them. The shift does not change
                when the highlight moves.
            hscroll_hint: The footer atom that the footer shows (as the second ` · ` atom,
                immediately after the move atom) while ``hscroll`` is on and the highlighted
                row overflows. Thus the hint shows ←→ exactly when these keys do something.
                Ignored when ``hscroll`` is off.
            keys: The bare-key shortcuts that this list answers. Each maps the pressed
                character to a token that names what it means.
                :meth:`~meshterm.ui.tui.session.TuiSession.button_dialog` already uses this
                idiom for the y/n accelerators of a dialog. The list resolves with a
                :class:`KeyRequest` that holds the token and the value of the highlighted
                row. Thus the caller can act and open the list again. **Only a list that is
                not filterable obeys them**, because there a bare letter has no other
                function. On a list with a filter, each letter belongs to the query. A
                shortcut that takes a letter is a key that stops the user, without a sign,
                when the user types a name.
            key_hint: The names of those shortcuts in the footer. At each paint, the screen
                calls it with the value of the highlighted row, and inserts the result
                immediately before the last ``Esc`` clause. Return one run of atoms joined
                by ` · `, or ``""`` for a row that no shortcut changes. ``delete_hint``
                follows the same rule, for the same reason: the footer must never name a key
                that does nothing on the row where the user is. The caller keeps this
                policy, because only the caller knows on which rows its keys have a meaning.
        """
        super().__init__()
        self.title = title
        # A row that pins a head block (:attr:`Choice.hscroll_from`) is a row made to
        # scroll, because the head has a meaning only as "the part that stays put". Thus a
        # row that declares one turns on the scroll of the list, and the builder does not
        # have to reach the screen that will show it. Because of this, each
        # label+description list (:func:`~meshterm.ui.menus.menu_rows`, the main menu)
        # scrolls its description wherever it is opened, also through ``ctx.ui.select``.
        # The exception is a list that itself says ``hscroll=False``. The rows cannot
        # override that.
        self._hscroll_auto = hscroll is None
        self._hscroll = bool(hscroll) or (
            self._hscroll_auto and any(getattr(item, "hscroll_from", 0) > 0 for item in items)
        )
        self._hscroll_hint = hscroll_hint
        # A row made as a line of a table turns on the pan, as a pinned head turns on the
        # scroll (refer to Choice.pans). The value is read from the rows each time they
        # change.
        self._pan = _pans(items)
        # Shortcuts compensate a list that is not filterable for its missing filter. The
        # letters are free, so the list can use them (refer to the ``keys`` argument).
        self._keys: dict[str, Any] = {} if filterable else dict(keys or {})
        self._key_hint = key_hint
        self._hshift = 0
        self._last_width = 0  # the last render width, for the overflow check of the footer
        if footer_hint is None:
            footer_hint = (
                "↑↓ move · type to filter · Enter select · Esc back"
                if filterable
                else "↑↓ move · Enter select · Esc back"
            )
        self._footer_base = footer_hint
        self._delete_hint = delete_hint
        self._prompt = prompt
        self._filterable = filterable
        self._items = items
        self._filter = ""
        # The index in the choices that can be selected now (after the filter).
        self._index = 0
        self._highlight(default)

    def _highlight(self, default: Any) -> None:
        """Put the highlight on the choice whose value is ``default`` (or else on the first row)."""
        self._index = 0
        if default is None:
            return
        for i, choice in enumerate(self._choices()):
            if choice.value == default:
                self._index = i
                return

    def turn_page(self, items: list, *, title: str, prompt: str = "", default: Any = None) -> None:
        """Show a different list in the same box: the next or previous page of a stepped dialog.

        This is the other way that the rows of a list change while the user looks at them.
        It makes the opposite claim to :meth:`replace_items`. That method refreshes the same
        list (its rows are data that moved), so the filter and the highlight stay. A new
        page is a new question (select the node to tune, then select where to measure), so
        nothing stays:

        - The query that the user typed to find a row on the previous page is cleared. If it
          stays on this page, it narrows the list to rows that it was not meant for.
        - The scroll starts at the top.
        - The highlight goes to ``default`` or to the first row. (A page that the user turns
          back to offers its previous answer, exactly as a step of
          :func:`~meshterm.ui.menus.run_steps` does.)

        Args:
            items: The rows of the new page, in the order to show.
            title: The new heading. It is the only place where the user sees which step this
                is.
            prompt: The instruction above the rows, or ``""`` for none.
            default: The choice value to highlight, if it is present.
        """
        self._items = items
        if self._hscroll_auto:
            self._hscroll = any(getattr(item, "hscroll_from", 0) > 0 for item in items)
        self._pan = _pans(items)
        self.title = title
        self._prompt = prompt
        self._filter = ""
        self._hshift = 0
        self.scroll_to_top()
        self._highlight(default)

    def replace_items(
        self, items: list, *, title: str | None = None, prompt: str | None = None
    ) -> None:
        """Replace the rows (and the title, if given) in place, and keep the place of the user.

        This is the counterpart of :meth:`~meshterm.ui.tui.session.TuiSession.stay` for a
        list whose content is data: the rows of an editor with a staged ``current → new``
        value, a title that counts what is staged, a queue that just lost the message that
        it sent. Before, these lists were built again as a new screen at each round. That
        removed the typed filter, and only a ``default=`` restore put the highlight back at
        an approximate place.

        The place is kept by value, not by index. The highlight goes back to the row that it
        was on, wherever that row moved to. If the row is gone, the highlight goes to the
        same position in the list (clamped). A user expects this after the user deletes the
        row that the highlight was on. The filter and the horizontal shift stay. The shift
        goes back to the start when the highlighted row changes, exactly as when the user
        moves the highlight.

        Args:
            items: The new rows, in the order to show.
            title: A new heading, or ``None`` to keep the current heading.
            prompt: A new instruction or summary line above the rows, or ``None`` to keep
                the current line. An example is the vital signs of the channel detail,
                which age and count unread messages next to the rows below them.
        """
        current = self._current_choice()
        was = current.value if current is not None else None
        position = self._index
        self._items = items
        if self._hscroll_auto:
            self._hscroll = self._hscroll or any(
                getattr(item, "hscroll_from", 0) > 0 for item in items
            )
        self._pan = _pans(items)
        if title is not None:
            self.title = title
        if prompt is not None:
            self._prompt = prompt
        selectable = [it for it in self._rows() if isinstance(it, Choice)]
        index = next((i for i, c in enumerate(selectable) if c.value == was), None)
        if index is None:
            index = max(0, min(position, len(selectable) - 1))
            if not self._pan:
                self._hshift = 0  # another row is highlighted now. A pan belongs to the table.
        self._index = index

    # --- filtering -----------------------------------------------------------

    def _rows(self) -> list:
        """Return the items to show with the active filter.

        Without a filter, groups and headings show as written. With a filter, only the
        choices that match are shown. But each separator stays, so the list keeps its
        section landmarks (and column headers) while it narrows.
        """
        needle = self._filter.strip().lower()
        if not needle:
            return self._items
        return [
            it
            for it in self._items
            if isinstance(it, Separator) or needle in _plain(it.label).lower()
        ]

    def _choices(self, rows: list | None = None) -> list:
        """Return only the selectable choices in ``rows`` (or in the current rows)."""
        rows = self._rows() if rows is None else rows
        return [it for it in rows if isinstance(it, Choice)]

    def _section_starts(self, rows: list | None = None) -> list[int]:
        """The choice indices that start a section: the first choice after each run of separators.

        The section jumps of Ctrl+PageUp/PageDown use them. By default, they are calculated
        from the shown rows. Thus the jumps continue to work while a filter narrows the
        list (the headings stay, refer to :meth:`_rows`, and an empty section gives no
        stop).

        Args:
            rows: The rows to read the sections from, or ``None`` for the shown rows.
                :meth:`_all_section_starts` gives the unfiltered items instead, to ask the
                structural question instead of the live question.
        """
        starts: list[int] = []
        idx = 0
        fresh = True  # the next choice starts a section (top of the list, or after a heading)
        for item in self._rows() if rows is None else rows:
            if isinstance(item, Separator):
                fresh = True
            elif isinstance(item, Choice):
                if fresh:
                    starts.append(idx)
                    fresh = False
                idx += 1
        return starts

    def _all_section_starts(self) -> list[int]:
        """The section starts of the unfiltered list: they tell if this list has sections.

        The F-key lane must have the answers to the two questions separately (refer to
        :attr:`picocalc_lyra_lane`). A flat list never offers a section jump. A grouped list
        whose filter shows only one section offers the jump again when the filter changes.
        """
        return self._section_starts(self._items)

    def _jump_section(self, direction: int) -> None:
        """Move the highlight to the next section (``+1``), or the current or previous one (``-1``).

        It is the same as the section jump of the read-only screens, which scrolls, but it
        moves through the choices. Down goes to the first choice of the next section. Up
        goes to the first choice of the current section, or to the first choice of the
        previous section when the highlight is already at the start of a section.
        """
        starts = self._section_starts()
        if not starts:
            return
        if direction > 0:
            nxt = next((s for s in starts if s > self._index), None)
            if nxt is not None:
                self._index = nxt
        else:
            at_or_before = [s for s in starts if s <= self._index]
            if not at_or_before:
                self._index = 0
            elif at_or_before[-1] < self._index:
                self._index = at_or_before[-1]
            else:
                self._index = at_or_before[-2] if len(at_or_before) >= 2 else 0

    def _current_choice(self) -> Choice | None:
        """The choice that the highlight is on now, or ``None`` when the list is empty."""
        choices = self._choices()
        if not choices:
            return None
        return choices[max(0, min(self._index, len(choices) - 1))]

    @property
    def footer_hint(self) -> str:
        """The footer key hint, with dynamic atoms only where their keys act.

        Conditional atoms go in exactly where they apply. Thus the bottom border never
        shows a key that does nothing:

        * The :attr:`_delete_hint` atom, inserted immediately before the last ``Esc``
          clause (refer to :func:`splice_hint`), while the highlighted row is
          :attr:`Choice.deletable`.
        * The names that ``key_hint`` gives for the value of the highlighted row: the
          shortcut keys (``keys``) that act on that row, inserted in the same way.
        * The :attr:`_hscroll_hint` atom (only in ``hscroll`` lists), inserted immediately
          after the move atom (refer to :func:`_insert_atom`), while the highlighted row
          is wider than the width. A short row cannot scroll, so ←→ stays hidden on it.

        Also, the last ``Esc`` clause changes to ``Esc clear`` while a filter is typed,
        because the press then clears the filter (refer to :meth:`handle`).
        """
        base = self._footer_base
        current = self._current_choice()
        if self._delete_hint and current is not None and current.deletable:
            base = splice_hint(base, self._delete_hint)
        if self._key_hint is not None:
            atoms = self._key_hint(current.value if current is not None else None)
            if atoms:
                base = splice_hint(base, atoms)
        if (self._hscroll or self._pan) and self._hscroll_hint and self._selected_overflows():
            base = _insert_atom(base, self._hscroll_hint)
        if self._filter:
            base = _esc_verb(base, "clear")
        return base

    def _selected_overflows(self) -> bool:
        """Whether the label of the highlighted row is too wide for the last render width.

        This is the condition for the ←→ scroll and for its footer atom: a row that fits
        has nothing to scroll. The label is measured against the content area of the row
        (the width less the pointer of 2 cells), at the width of the last
        :meth:`render_body` (``0`` before the first paint, so nothing overflows until a
        real width is known). The question goes to :meth:`_max_hshift`, not to the raw
        label width. Thus a row that pins a head block (:attr:`Choice.hscroll_from`) is
        measured by the run that moves, with the cells for the marks of a scrolled row. A
        list that pans asks the same question of the full table instead of one row,
        because ←→ move the table wherever the highlight is.
        """
        if self._pan and self._last_width > 0:
            return self._pan_limit(self._last_width) > 0
        if not self._hscroll or self._last_width <= 0:
            return False
        current = self._current_choice()
        if current is None:
            return False
        avail = max(1, self._last_width - 2)
        return (
            self._max_hshift(
                cell_len(_plain(current.scroll_text(avail))), current.hscroll_from, avail
            )
            > 0
        )

    @property
    def sizing_footer_hint(self) -> str:
        """The longest form of the footer, for a stable box size (refer to :attr:`footer_hint`).

        The delete-hint atom is always in it. Thus a compositor that sets the size of a box
        from the width of the footer (:func:`~meshterm.ui.tui.frame.compose_startup` of the
        startup splash) keeps space for the atom from the start. Then the box never becomes
        wider when the highlight goes onto a deletable row.
        """
        if self._delete_hint:
            return splice_hint(self._footer_base, self._delete_hint)
        return self._footer_base

    # --- rendering -----------------------------------------------------------

    @property
    def dialog_width(self) -> int:
        """The natural outer width, so that a floating select fits its widest row, not the terminal.

        It is the widest of the prompt, the title, the footer, and each row (with space for
        the pointer). Thus a short menu is a small dialog, not a banner of full width. The
        compositor still limits this width to the available space. Only a floating select
        reads it: ``compose_base`` sets the layout of the full-screen base menu and ignores
        it. The footer is measured at its longest, with the delete-hint atom in it. Thus a
        deletable row that shows that hint never makes the box wider during navigation. A
        width-aware separator is also measured at its fullest (refer to
        :data:`_UNBOUNDED`). The box asks for space for the full header, and the header
        becomes shorter only when the terminal limits the box.
        """
        footer = (
            splice_hint(self._footer_base, self._delete_hint)
            if self._delete_hint
            else self._footer_base
        )
        widths = [cell_len(self.title), cell_len(footer), cell_len(self._prompt)]
        for item in self._items:
            label = item.text(_UNBOUNDED)  # the two types of row measure at their fullest form
            widths.append(cell_len(_plain(label)) + 2)  # + the "❯ " / "  " pointer column
            if isinstance(item, Choice) and item.detail_label is not None:
                widths.append(cell_len(_plain(item.detail_label)) + 2)  # same hanging indent
        return max(widths, default=20) + 8

    def render_body(self, width: int) -> list[str]:
        """Plan the lines of the body, and draw only the rows that the viewport shows.

        The method goes through each row to make the layout of the body: how many lines the
        body takes, where the highlight is, and which separators are sticky landmarks. The
        scroll clamp and the pins of the frame must have all of this, exactly. But the
        render of a choice row is given as a callable, and is not done here (refer to
        :class:`~meshterm.ui.tui.screen.LazyLines`). A list of 141 contacts, 150 rows tall,
        shows only the twenty rows that fit. Before, the other hundred and thirty rows were
        built and thrown away at each key press and at each idle tick. Separators are
        rendered immediately: they are few, and :meth:`sticky_rows` pins their lines outside
        the slice.
        """
        rows = self._rows()
        choices = self._choices(rows)
        self._index = max(0, min(self._index, len(choices) - 1)) if choices else 0
        selected = choices[self._index] if choices else None

        # The horizontal scroll moves only the highlighted row, and only as far as its own
        # end: → stops when the end of that row is visible, and a short (or not selected)
        # row cannot shift. It is measured again at each paint, because a callable title
        # can change its width. It is measured against the content area of the row (the
        # width less the pointer of 2 cells).
        self._last_width = width
        if self._pan:
            # A list that pans slides the table as one unit, as far as the end of its
            # widest line.
            self._hshift = max(0, min(self._hshift, self._pan_limit(width)))
        elif self._hscroll and self._hshift:
            avail = max(1, width - 2)
            sel_len = cell_len(_plain(selected.scroll_text(avail))) if selected is not None else 0
            anchor = selected.hscroll_from if selected is not None else 0
            self._hshift = max(
                0, min(self._hshift, self._max_hshift(sel_len, anchor, max(1, width - 2)))
            )

        # One entry for each body line: a finished string, or a callable that draws the line
        # when the frame asks. The positions are exact in the two cases, and the layout below
        # reads only the positions.
        lines: list[str | Callable[[], str]] = []
        # A prompt (when set) is above the list, and moves each row below it down. Each index
        # recorded below (the line of the highlight, the sticky headers) comes from
        # ``len(lines)``, which already counts the lines of the prompt. Thus no index adds
        # the offset again.
        if self._prompt:
            lines.extend(render_lines(Text(self._prompt), width))
            lines.append("")
        # Record each section heading (a Separator that says that it is one) as a sticky
        # block. Thus, when one scrolls off, the base Screen.sticky_block pins it again to the
        # top lines. Also record a pinned separator (a column header) as the header of the
        # full list, pinned above it. The block of a heading continues through the separators
        # that follow it immediately, before the first row of the section. Prose there is the
        # preamble of the section (the "what this discipline scores" of the Trophy case).
        # Thus it belongs at the top with the heading, and does not scroll away from the rows
        # that it explains. All the other separators (blank spacers, empty-state notes under
        # a section that has rows, a stray line between choices) are the landmark of
        # nothing, and are not candidates. Thus the lines pinned at the top are always from
        # the section that governs. Separators stay when the filter applies (refer to
        # _rows), so the pins continue to work while the list narrows.
        self._sticky_headers = []
        self._pinned_header = None
        block: list[str] | None = None  # the heading block that still takes lines, if any
        if self._filter:
            lines.append(query_line(self._filter, width))
        # The detail line of a row (refer to Choice.detail) can make the row two lines tall.
        # Thus the code follows the line of the highlight while it draws the rows, and does
        # not calculate it from the row index.
        cursor_at: int | None = None
        for item in rows:
            if isinstance(item, Separator):
                # A Text title has its own spans (a column header with two colours). A plain
                # string is drawn all in the style of the separator. A separator scrolls
                # horizontally only as the column header of a table that pans. The shift for
                # each row moves only the highlighted choice row.
                title = item.text(_UNBOUNDED if item.pans and self._pan else width)
                content = title if isinstance(title, Text) else Text(title, style=item.style)
                if item.pans and self._pan:
                    # Its fullest form, slid with the rows, with its pointer column held
                    # still. It is one line, however wide, because its labels must stay above
                    # their lanes.
                    slid = self._scroll_window(content, _POINTER, width)
                    drawn = [render_to_ansi(slid, width, no_wrap=True)]
                    if item.pinned:
                        self._pinned_header = (len(lines), drawn[0])
                    elif item.heading:
                        block = list(drawn)
                        self._sticky_headers.append((len(lines), block))
                elif item.pinned:
                    # A pinned header is drawn outside the body slice and is exactly one
                    # line tall. Thus it crops like a row and does not wrap. A column header
                    # that is too wide for the terminal ellipsizes, and does not take a
                    # second reserved line from the content.
                    drawn = [render_to_ansi(content, width, no_wrap=True)]
                    # A header for the full list is also outside the sequence of sections.
                    # It pins above the section headings, and does not take a turn among
                    # them. Thus it is also not a section boundary for the
                    # Ctrl+PageUp/PageDown jumps.
                    self._pinned_header = (len(lines), drawn[0])
                else:
                    # A prose separator (a note above a startup list) can wrap. Each line that
                    # it takes is its own body line, so the viewport still counts all of
                    # them. Also, each of them joins the block, the wrapped lines too.
                    drawn = render_lines(content, width)
                    if item.heading:
                        block = list(drawn)
                        self._sticky_headers.append((len(lines), block))
                    elif block is not None:
                        block.extend(drawn)  # the preamble of the heading, pinned with it
                lines.extend(drawn)
                continue
            block = None  # a row ends the heading block. Prose after it is not in the block.
            is_sel = item is selected
            if is_sel:
                cursor_at = len(lines)
            lines.append(self._row_drawer(item, is_sel, width))
            # Resolved here, not in the drawer. Whether the row is one line tall or two is
            # part of the layout, so a callable detail is read during the plan. The drawer
            # below renders this one resolved value, and never calls the callable again.
            detail = item.detail_label if isinstance(item, Choice) else None
            if detail is not None and _plain(detail):
                lines.append(self._detail_drawer(detail, width))
        if not choices:
            # With no row to highlight, the empty state takes its place: it is kept visible,
            # and the page still has edge scroll past it, to the lines above and below it.
            cursor_at = len(lines)
            lines.append(render_to_ansi(Text("no matches", style="muted"), width))
        # Keep the line of the highlighted row, so that the session can keep it visible.
        self._cursor = cursor_at
        return LazyLines(lines)

    def _row_drawer(self, item: Choice, is_sel: bool, width: int) -> Callable[[], str]:
        """A callable that renders one choice row. It runs only if the row is on the screen."""

        def draw() -> str:
            # A width-aware title fits itself to the content area of the row (the width less
            # the pointer of 2 cells). The exception is the highlighted row of an ``hscroll``
            # list, which keeps its natural form: ←→ slide the full line, and a row that
            # elided itself before has nothing to slide. But a row can declare that its
            # fitted form is complete (``Choice.fitted``). All the rows of a table that pans
            # keep their natural form, highlighted or not, because the pan slides each of
            # them.
            panned = self._pan and item.pans
            if panned or (self._hscroll and not self._pan and is_sel):
                label = item.scroll_text(max(1, width - 2))
            else:
                label = item.text(max(1, width - 2))
            pointer = "❯ " if is_sel else "  "
            style = "cursor" if is_sel else ""
            # A Text label has its own spans (for example, a red badge). Keep them, and put the
            # base style of the row under them. Thus the highlight tints the row, and the
            # badge keeps its colour. A plain string gets one style for all its text, as
            # before.
            text = Text(pointer, style=style)
            label_text = label if isinstance(label, Text) else Text(label)
            if panned or (self._hscroll and not self._pan and self._hshift and is_sel):
                # Only the label slides. The pointer of 2 cells stays pinned. The scroll for
                # each row moves only the highlighted row. A pan moves each row of the table,
                # and also draws its cut marks at a shift of 0, where a line goes past the
                # edge.
                label_text = self._scroll_window(label_text, item.hscroll_from, max(1, width - 2))
            text.append_text(label_text)
            text.style = style
            text.no_wrap = True
            # Cut, do not truncate. Thus a row with a path line (a trophy walk, a probe
            # candidate) breaks its chip off on the crack, and each usual row still ends in
            # the ellipsis. ``cut_to`` decides this from the row itself.
            text = cut_to(text, width)
            text.no_wrap = True
            return render_to_ansi(text, width)

        return draw

    def _max_hshift(self, label_cells: int, anchor: int, avail: int) -> int:
        """How far ←→ can slide a row of ``label_cells`` whose head holds ``anchor`` cells.

        The stop is the first whole :attr:`_HSCROLL_STEP` that brings the end of the run
        into the lane, not the exact cell that puts the end at the right edge. A scrolled
        row gave one cell to its left :func:`~meshterm.ui.pathline.cut_mark`. Thus its last
        visible slice is one cell shorter than the lane. If the slide stops before a whole
        step, the right mark stays drawn at the far end, and promises more text that ←→ can
        no longer reach. (The two path rows that cut their own visible slice by
        hand, the routes of the node page and the lanes of Message paths, clamp in the same
        way.)
        """
        lane = max(1, avail - max(0, anchor))
        run = max(0, label_cells - max(0, anchor))
        if run <= lane:
            return 0  # the full run is visible without a scroll: nothing to slide to
        steps = -(-max(0, run - (lane - 1)) // self._HSCROLL_STEP)
        return steps * self._HSCROLL_STEP

    def _pan_limit(self, width: int) -> int:
        """How far ←→ can pan the table of this list at ``width``: to the end of its widest line.

        Each line that pans is measured in the same way: a row in the content area after
        its pointer, and a column header over the full width with its pointer column held.
        Thus the two measure the same table and agree on where it ends.

        The stop is the exact shift that brings the end of the widest line to the edge. It
        is not the whole :attr:`_HSCROLL_STEP` past it that :meth:`_max_hshift` takes for
        one row. Here each line moves. Thus a stop at a whole step makes the full table end
        before the edge. Also, a box with the size of its content (the startup splash) then
        becomes smaller under the user at the last press, and removes hint atoms as it
        becomes smaller. At the exact stop, the widest line touches the edge with no mark at
        the right: its first hidden cell is after its own end.
        """
        avail = max(1, width - _POINTER)

        def tail(cells: int, anchor: int, room: int) -> int:
            lane = max(1, room - anchor)
            run = max(0, cells - anchor)
            return 0 if run <= lane else run - (lane - 1)  # the left mark takes a cell

        limit = 0
        for item in self._rows():
            if not getattr(item, "pans", False):
                continue
            if isinstance(item, Separator):
                cells = cell_len(_plain(item.text(_UNBOUNDED)))
                limit = max(limit, tail(cells, _POINTER, width))
            else:
                cells = cell_len(_plain(item.scroll_text(avail)))
                limit = max(limit, tail(cells, item.hscroll_from, avail))
        return limit

    def _scroll_window(self, label: Text, anchor: int, avail: int) -> Text:
        """The label of the highlighted row: head pinned, run slid in by ``_hshift``.

        The first ``anchor`` cells are drawn in full and never move. A row that declares
        them (:attr:`Choice.hscroll_from`) has columns, then content, and the columns tell
        the user which row the highlight is on. All the text after them is the run that
        scrolls, shown one lane at a time. On each side where the run continues past the
        edge, the edge shows :func:`~meshterm.ui.pathline.cut_mark`: a chip broken off in its
        own fill where the run is a path drawn in chips, or the faint ``…`` where it is not.
        These marks are chrome in the lane, not more width. Thus each mark takes a cell from
        the visible slice, and the crop is measured only when the two marks are known. If
        the crop is measured before, the run draws one cell past the row. With ``anchor`` at
        ``0`` (the default), the full line is the run. This is the behaviour that each
        hscroll list had before rows could pin a head.
        """
        head = crop_cells(label, 0, anchor) if anchor > 0 else Text()
        anchor = head.cell_len  # a head wider than the label keeps only the cells that exist
        run = max(0, label.cell_len - anchor)
        shift = self._hshift
        left = 1 if shift else 0
        inner = max(1, avail - anchor - left)
        right = 1 if shift + inner < run else 0
        window = max(1, inner - right)
        out = Text(no_wrap=True)
        out.append_text(head)
        if left:
            out.append_text(cut_mark(label, anchor + shift, ELIDE_HEAD))
        out.append_text(crop_cells(label, anchor + shift, window))
        if right:
            out.append_text(cut_mark(label, anchor + shift + window - 1, ELIDE_TAIL))
        return out

    @staticmethod
    def _detail_drawer(detail: str | Text, width: int) -> Callable[[], str]:
        """A callable that renders the detail line of a row (already resolved), on demand.

        The line hangs under the row, at the indent of the pointer. It never scrolls or
        wraps. It ellipsizes itself if it is too wide to fit.
        """

        def draw() -> str:
            detail_text = detail if isinstance(detail, Text) else Text(detail)
            line = Text("  ")
            line.append_text(detail_text)
            line.no_wrap = True
            line.overflow = "ellipsis"
            line.truncate(width)
            return render_to_ansi(line, width)

        return draw

    def cursor_line(self) -> int | None:
        """Return the body line index of the highlighted row."""
        return getattr(self, "_cursor", None)

    # --- input ---------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Move the highlight, edit the filter, or commit or cancel the selection."""
        choices = self._choices()
        if self._hscroll and not self._pan and action in _HSHIFT_RESET_ACTIONS:
            self._hshift = 0  # a move off a row resets its scroll. Each row scrolls by itself.
        if action == "up":
            if choices:
                # Both ends clamp. Nothing in the app rolls a highlight over. If a ↓ off the
                # last row rolls over, it pulls the viewport back to the start and changes
                # the edge markers with it. To the user, this looks like a change of the screen
                # under them, not like one step. Across the section headings of a grouped
                # list, it looks like a jump, not like a scroll that continues. Held-down
                # arrows stop at an end instead.
                self._index = max(0, self._index - 1)
        elif action == "down":
            if choices:
                self._index = min(len(choices) - 1, self._index + 1)
        elif action == "pageup":
            self._index = max(0, self._index - self._page_step)
        elif action == "pagedown":
            self._index = min(len(choices) - 1, self._index + self._page_step) if choices else 0
        elif action in ("home", "ctrl_home"):
            # The jump goes to the top of the page, not only to the first choice. The lines
            # above it (a heading, its preamble, a lead-in note) also scroll back onto the
            # screen, and do not stay hidden behind their pinned copies.
            self._index = 0
            self.scroll_to_top()
        elif action in ("end", "ctrl_end"):
            self._index = max(0, len(choices) - 1)
        elif action == "ctrl_pagedown":
            self._jump_section(1)
        elif action == "ctrl_pageup":
            self._jump_section(-1)
        elif action == "enter":
            if choices:
                self.resolve(choices[self._index].value)
        elif action == "delete":
            # Delete asks to remove the highlighted row, but only where the row opted in (for
            # example, a remembered network device in the picker). On other rows, it does
            # nothing.
            if choices and choices[self._index].deletable:
                self.resolve(DeleteRequest(choices[self._index].value))
        elif action == "left" and (self._hscroll or self._pan):
            self._hshift = max(0, self._hshift - self._HSCROLL_STEP)
        elif action == "right" and (self._hscroll or self._pan):
            self._hshift += self._HSCROLL_STEP  # clamped at render to the end of the row or table
        elif action == "escape":
            # A typed filter is the most recent thing that the user entered. Thus Esc clears
            # it before Esc leaves. This was the rule of the map and the mesh walk, and now it
            # is the rule of each screen with a filter. The second Esc leaves a narrowed list.
            if self._filterable and self._filter:
                self._filter = ""
                self._index = 0
                self._hshift = 0
            else:
                super().handle("escape")
        elif action == "backspace" and self._filterable:
            self._filter = self._filter[:-1]
            self._index = 0
            self._hshift = 0
        elif action == "text" and data in self._keys:
            # A declared shortcut resolves the list with the pressed key and the row of the
            # highlight. Only a list that is not filterable can declare shortcuts.
            self.resolve(
                KeyRequest(self._keys[data], choices[self._index].value if choices else None)
            )
        elif action == "text" and self._filterable and data.isprintable():
            # A space at the start is ignored (the filter never starts with whitespace). A
            # space at the end is removed when the filter matches (refer to _rows). Thus
            # spaces count only in the middle of the query, in a name of more than one word
            # such as "Homestead R&D".
            if not data.isspace() or self._filter:
                self._filter += data
                self._index = 0
                self._hshift = 0


class ReorderScreen(Screen):
    """A list whose rows the user puts in a new order in place, with the arrow keys.

    Move the highlight with ↑/↓. Press Enter on a row to "grab" it. Then ↑/↓ move it up and
    down the list. Press Enter again to "drop" it.

    The action rows are below the list, in the same pattern as the config editor. When the
    order has changed, an "Apply" row in the ``ok`` tint and a "Back — discard" row in the
    ``err`` tint appear. While the order has no change, there are no action rows, because
    Esc already leaves. Enter on Apply commits. It resolves with the final order as a list
    of the original row indices (thus ``[2, 0, 1]`` means "the row that started third is
    now first"). Enter on Back, like Esc on all screens, resolves :data:`CANCEL`, so the
    caller keeps the original order.
    """

    #: Sentinels for the action rows (different from list positions, which are ints).
    _APPLY = "apply"
    _BACK = "back"

    #: The footer hints for the two grab states. ``dialog_width`` uses the longer one, so the
    #: box never changes size when a grab starts.
    _HINT_IDLE = "↑↓ move · Enter grab / select · Esc cancel"
    _HINT_GRABBED = "↑↓ move row · Enter drop · Esc cancel"

    @property
    def picocalc_lyra_lane(self):
        """No lane: the full screen uses ↑↓ and Enter, three keys that are on the keyboard."""
        from .fkeys import EMPTY_LANE

        return EMPTY_LANE

    def __init__(self, title: str, labels: list[str]) -> None:
        """Build a reorder screen.

        Args:
            title: The heading above the list.
            labels: The row labels, in their current order.
        """
        super().__init__()
        self.title = title
        self._labels = list(labels)
        # order[position] == the original index of the label. A moved row keeps its index.
        self._order = list(range(len(labels)))
        self._index = 0
        self._grabbed = False

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The key hint, with words that depend on whether a row is grabbed now."""
        return self._HINT_GRABBED if self._grabbed else self._HINT_IDLE

    @property
    def dialog_width(self) -> int:
        """The natural outer width, so that a floating reorder box fits its widest row.

        Refer to SelectScreen. The size is for the largest form of the box: the two footer
        hints and the action rows of the changed state. Thus the box never becomes wider
        during the interaction, when a grab starts or when Apply appears. The compositor
        still limits this width to the terminal.
        """
        widths = [cell_len(self.title), cell_len(self._HINT_IDLE), cell_len(self._HINT_GRABBED)]
        rows = self._labels + [label.plain for _key, label in self._dirty_actions()]
        widths += [cell_len(row) + 2 for row in rows]  # + the "❯ " / "  " pointer column
        return max(widths, default=20) + 8

    def _dirty(self) -> bool:
        """Whether the rows are no longer in their original order."""
        return self._order != list(range(len(self._order)))

    @staticmethod
    def _dirty_actions() -> list[tuple[str, Text]]:
        """The full exit group, shown after the order changes (it also sets dialog_width)."""
        return [
            (ReorderScreen._APPLY, Text.assemble(("✓ ", "ok"), "Apply new order")),
            (ReorderScreen._BACK, Text.assemble(("✗ ", "err"), "Back — discard changes")),
        ]

    def _actions(self) -> list[tuple[str, Text]]:
        """The action rows below the list, the same as the exit group of the config editor.

        An order with no change offers no rows: Esc leaves, and we removed the row that told
        this from all the app. A changed order offers the pair, because Apply has no key of
        its own, and its counterpart tells what a leave costs.
        """
        return self._dirty_actions() if self._dirty() else []

    def render_body(self, width: int) -> list[str]:
        """Render the rows, a blank spacer, then the action group, and mark the highlight."""
        n = len(self._order)
        lines: list[str] = []
        for pos, orig in enumerate(self._order):
            is_cursor = pos == self._index
            if is_cursor and self._grabbed:
                pointer, style = "▸ ", "cursor"
            elif is_cursor:
                pointer, style = "❯ ", "cursor"
            else:
                pointer, style = "  ", ""
            text = Text(
                pointer + self._labels[orig], style=style, no_wrap=True, overflow="ellipsis"
            )
            text.truncate(width)
            lines.append(render_to_ansi(text, width))
        actions = self._actions()
        if actions:
            lines.append("")
        for i, (_key, label) in enumerate(actions):
            if n + i == self._index:
                # The highlighted action row gets the ``cursor`` style, as the list rows
                # above it do. It loses the ✓/✗ tint for this style.
                text = Text("❯ " + label.plain, style="cursor", no_wrap=True)
            else:
                text = Text("  ", no_wrap=True)
                text.append_text(label)
            text.truncate(width, overflow="ellipsis")
            lines.append(render_to_ansi(text, width))
        # The spacer line moves each action row down by one line on the screen.
        self._cursor = self._index if self._index < n else self._index + 1
        return lines

    def cursor_line(self) -> int | None:
        """Return the body line index of the highlight, so that the session keeps it visible."""
        return getattr(self, "_cursor", None)

    def handle(self, action: str, data: str = "") -> None:
        """Move the highlight or a grabbed row, grab or drop, run an action, or cancel on Esc."""
        n = len(self._order)
        actions = self._actions()
        total = n + len(actions)
        # If a change is undone while the highlight is on an action row, the highlight is
        # past the end. Clamp it before anything reads it.
        self._index = min(self._index, max(0, total - 1))
        if action == "up":
            if self._grabbed and self._index > 0:
                self._order[self._index - 1], self._order[self._index] = (
                    self._order[self._index],
                    self._order[self._index - 1],
                )
                self._index -= 1
            elif not self._grabbed and total:
                # Clamp, as each other row highlight does, and as the grabbed row above does:
                # the same two keys must not mean "stop at the end" in one mode and "leap
                # to the other end" in the other mode.
                self._index = max(0, self._index - 1)
        elif action == "down":
            if self._grabbed and self._index < n - 1:
                self._order[self._index + 1], self._order[self._index] = (
                    self._order[self._index],
                    self._order[self._index + 1],
                )
                self._index += 1
            elif not self._grabbed and total:
                self._index = min(total - 1, self._index + 1)
        elif action in ("home", "ctrl_home") and not self._grabbed and total:
            # Home and End jump to the first row and the last, as on each list, and the edge
            # scroll moves the page to its ends with them. A grabbed row moves only one step
            # at a time, so these keys do not move it.
            self._index = 0
        elif action in ("end", "ctrl_end") and not self._grabbed and total:
            self._index = total - 1
        elif action == "enter":
            if self._index < n:
                self._grabbed = not self._grabbed  # Enter grabs a row, Enter again drops it
            elif actions[self._index - n][0] == self._APPLY:
                self.resolve(list(self._order))
            else:
                super().handle("escape")  # Back resolves CANCEL, same as Esc
        elif action == "escape":
            super().handle("escape")
