# SPDX-License-Identifier: Apache-2.0
"""The screen abstraction: one layer in the screen stack of the TUI.

A :class:`Screen` knows how to render its body (as Rich, which
:mod:`~meshterm.ui.tui.render` changes into ANSI lines). It also knows how to respond to
the normalized key *actions* that the :class:`~meshterm.ui.tui.session.TuiSession`
dispatches to it. A screen never uses prompt_toolkit or the terminal directly. Thus the
screens stay small, uniform, and easy to test.

An interactive screen (select, text, confirm) resolves an :class:`asyncio.Future` when the
user commits or cancels. The session awaits that future and then pops the screen. This is
the push/await/pop model of ``await session.select(...)`` and the related calls.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from collections.abc import Sequence as _SequenceABC
from typing import Any

from rich.cells import cell_len
from rich.console import RenderableType
from rich.text import Text

from .render import render_hanging, render_lines
from .spinner import Spinner

#: The sentinel that a screen resolves with when the user presses Esc to cancel. It is
#: different from a ``None`` value that the user selected.
CANCEL = object()

#: The sentinel that the session resolves the future of the top screen with, when the user
#: presses the pop-all key (^W). It is never a valid result: each navigation boundary that
#: gets it changes it immediately into :class:`PopToMenu`. Thus a caller never has to test
#: for it.
POP_ALL = object()

#: The keys of the edge scroll (refer to :meth:`Screen.edge_scroll`). They are the arrows
#: and the direction of each step, the jumps to the two ends, and the keys that act on the
#: highlight. When the highlight is off the screen, a key of this last group (and the arrow
#: that points at the highlight) first makes the highlight visible again, before it does
#: anything.
_EDGE_STEPS = {"up": -1, "down": 1}
_EDGE_ENDS = {"home": -1, "ctrl_home": -1, "end": 1, "ctrl_end": 1}
_SNAP_BACK_ACTIONS = frozenset({"enter", "delete"})


class PopToMenu(BaseException):
    """Raised through each navigation call that awaits a screen, to unwind to the main menu.

    The full path that the user opened is a stack of pending ``await`` expressions. The
    caller of a screen awaits its result, and the caller of that caller awaits the caller in
    turn. Thus the only way to leave all the screens on the stack at the same time is to
    raise through all of these expressions. On the way out, each caller pops its own screen
    in a ``finally``. This is the strict rule of one pop for each push, applied N times: a
    screen is never removed unless its owner unwinds.

    It derives from :class:`BaseException`, not :class:`Exception`, for the same reason as
    :class:`asyncio.CancelledError`. The app has many ``except Exception`` guards. These
    guards stop a tool that has a fault before it can stop the menu
    (:func:`~meshterm.ui.menu._run_selection` has the widest guard). An unwind must go
    through these guards. They must not catch it and report it as a failure of a tool.
    ``finally`` blocks still run, so nothing leaks.

    Only one place catches it: the main menu loop, where the unwind ends.
    """


class Screen:
    """Base class for each TUI layer.

    Attributes:
        title: The heading at the top of the screen or the dialog.
        short_title: The short heading that the frame draws instead of :attr:`title` when
            the full heading does not fit in its rule (refer to
            :func:`~meshterm.ui.tui.frame.fitted_title`). It has the same atoms in fewer
            cells. Thus a narrow frame still shows each figure, instead of a title that is
            cut in the middle of a word. Empty (the default) means that the title has no
            shorter form.
        footer_hint: The key hint of one line in the footer.
        scroll: The current vertical scroll offset in the rendered lines of the body.
        future: Resolved with the result of the screen (or :data:`CANCEL`) when the screen
            commits.
        floating: Whether the session draws this screen as a dialog at the centre, over the
            dimmed screen below it (deeper layers float, the base layer does not).
        modal: Whether this layer owns the keyboard. A modal layer is a prompt, or an
            operation in progress, that owns the keyboard until the user answers it or it
            finishes. This is different from :attr:`floating`, which is only about how the
            layer is drawn. A usual select list floats as a box and is not modal. A
            progress dialog is modal. A full-frame busy splash is modal and does not float.
            The pop-all key (^W) does not operate while a modal layer is on top. Thus an
            unwind can never go past a question that has no answer, or remove the stack
            from under work in progress.
        grow_only: Whether a floating dialog can only grow. The size of its box is the
            size of the tallest body that it showed (and, for a dialog with a natural
            width, the widest), not of the current body. Thus a screen whose content
            changes size (the packet viewer when it pages between packets, the suggestions
            of the path composer, which move) keeps a steady box at the centre, instead of
            a box that changes size at each change. Blank lines fill the box when the
            current body is shorter.
        chrome: Whether this layer, as the base screen, is wrapped in the persistent header
            and footer frame of the session. A startup splash sets this attribute to
            ``False``. Then the session puts the splash at the centre, under the
            :attr:`banner`, with no status bars.
        bare: Whether this layer, as the base screen, is the full frame: its body on blank
            rows, with no header, no footer, no title bar, no box, and no wordmark. It is
            for a screen that shows one thing for a camera or a user to look at (the QR
            code of the share screen, refer to :func:`~meshterm.ui.qr.share_screen`). The
            screen shows no keys: Esc leaves, and each keyboard has an Esc key. This
            attribute is read before :attr:`chrome`. A bare screen never floats.
        banner: Block-glyph art (one string for each line), drawn at the centre above a
            base screen that has no chrome. Ignored while ``chrome`` is ``True``.
        footnote: A short line, drawn muted and at the centre, below the box of a base
            screen that has no chrome (for example, a copyright notice). Ignored while
            ``chrome`` is ``True``.
        bottom_caption: A short credit or label, set right-justified into the **bottom
            border rule**, in the same way as :attr:`title` is in the top rule. It is for
            something that belongs to the picture in the frame, not to a row in it. The
            OpenStreetMap credit of the map is the reason that it exists (refer to
            :mod:`meshterm.ui.attribution`). A frame with a border already draws that
            rule, so a caption in it takes no space from the body. It is read again at
            each paint, so a screen can change it when its state changes. It is drawn only
            where the frame of the platform has a bottom rule. The borderless frame has
            none, and a screen that wants a mark there puts it in its own body instead.
        flush: Whether the body of this base screen, in a frame with a border, goes all the
            way to the side borders of the panel, instead of one blank cell in from each
            border. The padding is space between a border and a line of text. A picture
            that fills the frame (the map) has no text edge to keep away from the border.
            Without this attribute, the picture loses those two cells of drawing for no
            reason (JP, 2026-10-01). It has no effect where the frame of the platform has
            no border, because the body already has the full width there.
    """

    title: str = ""
    short_title: str = ""
    footer_hint: str = "Esc back"
    bottom_caption: str = ""
    flush: bool = False
    floating: bool = True
    modal: bool = False
    grow_only: bool = False
    chrome: bool = True
    bare: bool = False
    banner: Sequence[str] | None = None
    footnote: str | None = None
    #: Whether the page has edge scroll (refer to :meth:`edge_scroll`). It is on for each
    #: screen. It does nothing on a screen whose page has no highlight (:meth:`cursor_line`
    #: is ``None``). It is off only where the line that :meth:`cursor_line` keeps visible is
    #: not a highlight that the arrows move: the prompt of the remote CLI, whose ↑↓ recall
    #: the history, as in a shell.
    edge_scrolls: bool = True
    #: Whether Home and End move the page to its top and its bottom after the screen handles
    #: them (refer to :meth:`edge_scroll`). In a list, Home and End move the highlight to the
    #: first and the last row, and the page follows to its ends. It is off where these keys
    #: move a text caret instead: the compose line of the chat, the field of a prompt.
    home_end_jumps: bool = True

    @property
    def fkey_lane(self):
        """The F-key lane of the screen on the active platform.

        Refer to :mod:`~meshterm.ui.tui.fkeys`. Each handheld has its own lane. This
        property reads the lane definition of the screen that the deck of the platform
        names (:attr:`picocalc_lyra_lane`, :attr:`cardputer_zero_lane`). A platform with no
        lane (the desktop, whose footer is the hint line) gets an empty lane. Override the
        definitions for each deck, never this property.
        """
        from .fkeys import EMPTY_LANE, active_deck

        deck = active_deck()
        return EMPTY_LANE if deck is None else deck.read_lane(self)

    @property
    def cardputer_zero_lane(self):
        """The lane of the screen on the Cardputer Zero: for now, the lane of the PicoCalc.

        JP decided on 2026-09-30 that the Cardputer deals the same entries as the PicoCalc,
        until a new decision. Its Page Up/Down are real keys, but Fn+L/M is hard to reach.
        That the two keyboards both have five slots is a coincidence. Thus this property is
        the only place that ties the two lanes together. A screen whose Cardputer lane must
        be different overrides this property.
        """
        return self.picocalc_lyra_lane

    @property
    def picocalc_lyra_lane(self):
        """The lane of the screen on the PicoCalc (refer to :mod:`~meshterm.ui.tui.fkeys`).

        The lane has five slots, F1–F5. Each slot can have a companion F6–F10 in the Shift
        bank. A slot is for a function that is otherwise hard to reach. Enter and Esc never
        appear, because these two keys are already easy to reach.

        The default puts paging (which has no physical key) on the main F4/F5 pair. It puts
        the pair that jumps to the top and the end behind Shift on the same two keys. Home
        and End are on this keyboard, and they got a chip only for consistency with the
        pager that they belong to. F1–F3 stay free: a screen puts its own verbs there, and a
        screen with no verbs leaves them blank. A screen overrides this property for its own
        verbs, so the lane can be different on each screen.

        The lane is read again at each paint. Thus an override can dim each slot whose
        action has no effect at that time, instead of showing a key that does nothing. This
        is the permanent rule of the lane (refer to :mod:`~meshterm.ui.tui.fkeys`). A screen
        whose navigation keys only scroll controls the shared slots with
        :attr:`content_overflows`. This base class cannot assume as much, because on other
        screens a navigation key moves a highlight, a selection, or the zoom of the map.
        """
        from .fkeys import DEFAULT_LANE

        return DEFAULT_LANE

    def bar_title(self, title: str, style: str) -> Text:
        """The heading as the borderless title bar draws it: ``title`` in the bar ``style``.

        ``title`` is the form that the bar chose (:func:`~meshterm.ui.tui.frame.fitted_title`).
        A heading that is art instead of words overrides this method to keep the colours of
        the art. An example is the wordmark of the main menu, on a frame that has no header
        row for it (refer to :attr:`~meshterm.platforms.Platform.header_row`).
        """
        return Text(title, style=style)

    def __init__(self) -> None:
        """Initialize the scroll state and the result future (assigned later)."""
        self.scroll = 0
        self.future: asyncio.Future | None = None
        # (body-line index, rendered ANSI lines) for each section landmark that can pin to
        # the top lines after it scrolls off. A block is usually one line (a section heading,
        # a day divider). But it can also hold the lines that belong with it (a heading and
        # the description under it). These lines pin together, in order, as they scroll
        # off. A screen that wants sticky headers builds this list again while it renders
        # its body (refer to :meth:`sticky_block`). The default is no landmarks.
        self._sticky_headers: list[tuple[int, list[str]]] = []
        # The only header that pins for the full list instead of for its section: the
        # column header of a table, which has no meaning after it scrolls off (refer to
        # :meth:`sticky_rows`). It is kept in the same form, as (body-line index, rendered
        # line). ``None`` if there is none.
        self._pinned_header: tuple[int, str] | None = None
        # The body height and the viewport of the last render. The frame records them here
        # (:meth:`note_metrics`). Thus the shared scroll helpers can page by one viewport of
        # the current terminal and clamp to the content, and each caller does not have to
        # pass the sizes through.
        self._scroll_total = 1
        self._scroll_viewport = 1
        # The maximum body height and natural width of a grow-only screen: the tallest and
        # the widest that it rendered into a dialog. Thus its box can stay at that size after
        # it gets there (refer to :meth:`ratchet_viewport` / :meth:`ratchet_width`). Zero
        # until the first paint. Not used while :attr:`grow_only` is false.
        self._viewport_floor = 0
        self._width_floor = 0
        # Edge scroll (refer to :meth:`edge_scroll`). These attributes are:
        # - whether ↑↓ scrolled the viewport past the highlight, so that the frame no longer
        #   moves the viewport to keep the highlight visible,
        # - the net arrows given to :meth:`handle` since the last paint, which wait for the
        #   verdict of that paint,
        # - the highlight as the last paint drew it, ``(body line, drawn line)``, which the
        #   verdict compares with,
        # - the side of the viewport where the last paint left the highlight (0 visible, -1
        #   above, +1 below), which decides the snap-back.
        # The frame records the last two (:meth:`note_highlight`, :meth:`note_cursor_side`).
        self._edge_scrolled = False
        self._edge_steps = 0
        self._highlight_mark: tuple[int, str] | None = None
        self._cursor_side = 0

    # --- rendering -----------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Return the ANSI lines of the body at ``width`` cells (not clipped).

        Args:
            width: The inner content width in cells.

        Returns:
            One ANSI string for each body line. The session slices these lines to the
            viewport.
        """
        raise NotImplementedError

    def ratchet_viewport(self, body_h: int) -> int:
        """The body height that sets the size of a :attr:`grow_only` dialog: its maximum so far.

        The frame calls this method at each paint with the height of the current body. A
        grow-only screen returns the tallest height that it showed until now. Thus its box
        never becomes smaller, and the frame adds blank lines to a body that is now shorter,
        to fill the box. A usual screen returns the height without a change, and its size
        follows each body when it comes.
        """
        if not self.grow_only:
            return body_h
        self._viewport_floor = max(self._viewport_floor, body_h)
        return self._viewport_floor

    def ratchet_width(self, natural: int) -> int:
        """The natural width that sets the size of a :attr:`grow_only` dialog: its maximum.

        This method is the twin of :meth:`ratchet_viewport`, for the width. The frame
        applies it to the ``dialog_width`` that a dialog requests, before the terminal
        limit. Thus a grow-only box keeps its widest size when its content becomes narrower,
        but a terminal that becomes smaller still clips it. A dialog at the full limit (with
        no ``dialog_width``) never gets here.
        """
        if not self.grow_only:
            return natural
        self._width_floor = max(self._width_floor, natural)
        return self._width_floor

    def cursor_line(self) -> int | None:
        """Return a body line that must stay visible, or ``None`` for free scroll.

        A selection screen or a text screen returns the active row, so that the session can
        keep it visible. A plain scroll screen returns ``None``. To name the line of the
        highlight here is also all that a screen does for edge scroll (refer to
        :meth:`edge_scroll`). The frame reads the edges from the page, so no screen tells
        where they are.
        """
        return None

    def consume_edge_scrub(self) -> int:
        """The cells at the right edge that the session must write again at the next paint.

        The value is a number of cells (0 = none). The default is 0, because this is not
        necessary for a usual screen. A screen whose body can contain glyphs that the
        terminal renders at an unexpected width (the braille of the map) overrides this
        method. Then the session draws again only the corrupted cells at the edge. Without
        this method, the differential paint of prompt_toolkit never writes these cells
        again (refer to :class:`~meshterm.ui.map_screen.MapScreen`).
        """
        return 0

    def sticky_block(self, scroll: int) -> list[str]:
        """The rendered body lines to pin when ``scroll`` moves past a section landmark.

        With these lines, a grouped list keeps its current section at the top after the rows
        of the section scrolled off. Examples are the ``Channels``/``Direct`` divider of a
        select screen, the discipline heading of the Trophy case together with the
        description under it, and the ``── Wed Jul 8 ──`` day divider of the chat
        transcript. ``scroll`` is the offset at which the body will be sliced. The returned
        lines are drawn as the top lines, under the header of the full list if one is pinned
        above them (:meth:`sticky_rows` composes them for the frame).

        The shared rule: from the blocks that a screen recorded in :attr:`_sticky_headers`
        while it rendered, find the last block that starts at or above ``scroll``. This
        block *governs* the top visible line. Pin exactly the lines of this block that
        ``scroll`` passed. Thus a block gives its lines one at a time as they leave. The
        heading pins while its description is still the top content line, and the two pin
        together after both are gone. The pinned lines always continue smoothly into the
        body below. A screen opts in only by filling :attr:`_sticky_headers`. The empty
        default means no pins.

        A block never takes more than half the viewport. The lines are removed from the end.
        Thus, on a short terminal, the heading (the line that tells which section this is)
        is the last line to be removed.
        """
        governing: tuple[int, list[str]] | None = None
        for entry in self._sticky_headers:
            if entry[0] <= scroll:
                governing = entry
            else:
                break  # blocks are in body order, so no later block can govern
        if governing is None:
            return []  # no landmark above the top visible line
        at, rows = governing
        keep = max(1, self._scroll_viewport // 2)
        return rows[: min(scroll - at, keep)]

    def sticky_rows(self, scroll: int) -> list[str]:
        """All the rendered lines to pin above the body, when the body is sliced at ``scroll``.

        The frame draws these lines, from the top down:

        - The header of the full list from :attr:`_pinned_header`, after it scrolls off.
          This is the column header of a table: the lane names, which have the same meaning
          in all the list.
        - Then the section block that governs, from :meth:`sticky_block`.

        A screen with only section headings pins one line. A grouped table pins both. Thus
        the user can still read a scrolled row by its lanes, and also find its group.

        Args:
            scroll: The offset at which the body will be sliced.

        Returns:
            The lines to pin, in draw order (empty if there is nothing to pin). Each line
            takes one line from the body, so the frame keeps exactly as many lines as it
            gets.
        """
        rows: list[str] = []
        if self._pinned_header is not None and self._pinned_header[0] < scroll:
            rows.append(self._pinned_header[1])
        rows.extend(self.sticky_block(scroll))
        return rows

    # --- input ---------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """Respond to a normalized key action from the session.

        Args:
            action: One of ``up``, ``down``, ``pageup``, ``pagedown``, ``home``, ``end``,
                ``ctrl_home``, ``ctrl_end``, ``ctrl_pageup``, ``ctrl_pagedown``, ``left``,
                ``right``, ``ctrl_left``, ``ctrl_right``, ``ctrl_up``, ``ctrl_down``,
                ``shift_up``, ``shift_down``,
                ``shift_left``, ``shift_right``, ``enter``, ``escape``, ``backspace``,
                ``delete``, ``space``, ``tab``, ``shift_tab``, or ``text`` (with ``data``
                set to the character).
            data: The typed character when ``action`` is ``text``.
        """
        if action == "escape":
            self.resolve(CANCEL)

    # --- edge scroll ---------------------------------------------------------

    def edge_scroll(self, action: str) -> bool:
        """Edge scroll and the snap-back: take ``action`` here, before :meth:`handle` gets it.

        **Edge scroll** (JP, 2026-10-04) is how each page with a highlight scrolls. Where
        the highlight cannot go further, an arrow scrolls the page by one line instead. Thus
        the user can always make visible the lines above the first selectable row or below
        the last row (the vitals of a node, the lead-in of a list, a closing note). While
        the page is edge-scrolled, the frame does not move the viewport to keep the
        highlight visible, so the highlight can scroll off the screen. This is intentional.
        **Home** and **End** work in the same way: the screen handles them as usual (the
        highlight of a list jumps to its first or last row), and the page goes to its top
        or its bottom.

        **The snap-back**: while the highlight is off the screen, a key that moves it toward
        the viewport or acts on it (the arrow that points at it, Enter, Delete) first brings
        it back into the viewport, with a jump if necessary, and does nothing more. Nothing
        runs that the user cannot see.

        **No screen tells where its edges are** (JP, 2026-10-04: one rule, inherited, never
        a gate written screen by screen). An arrow goes to :meth:`handle` like all keys, and
        the next paint reads the answer from the page (:meth:`note_highlight`). The frame
        follows a highlight that moved, as usual. A highlight that did not move was at its
        edge, so the page takes the line instead. A screen gives only the line of its
        highlight, through :meth:`cursor_line`. The screen already gives that line so that
        the frame can keep the highlight visible.

        Any other key ends the edge scroll before :meth:`handle` gets it (a typed filter, a
        tab change, a page key). Thus the frame follows the highlight again from there. An
        arrow that is found at the edge arms the edge scroll again at the paint.

        Returns:
            Whether the key was taken here. The session gives the key to :meth:`handle`
            only when it was not taken.
        """
        if not self.edge_scrolls or self._highlight_mark is None:
            return False  # no highlight on the page: each key belongs to the screen
        step = _EDGE_STEPS.get(action)
        if (
            self._edge_scrolled
            and self._cursor_side
            and (step == self._cursor_side or action in _SNAP_BACK_ACTIONS)
        ):
            self._edge_scrolled = False  # snap-back: this press only brings the highlight back
            self._edge_steps = 0
            return True
        if action in _EDGE_ENDS and self.home_end_jumps:
            self.handle(action)
            self._edge_scrolled = True
            self._edge_steps = 0
            if _EDGE_ENDS[action] < 0:
                self.scroll_to_top()
            else:
                self.scroll = self._scroll_total  # the frame clamps it to the last full viewport
            return True
        if step is not None:
            self._edge_steps += step  # the paint finds whether it moved the highlight
        self._edge_scrolled = False
        return False

    def note_highlight(self, cursor: int | None, lines: Sequence[str]) -> None:
        """Record the highlight as this paint draws it, and judge the arrows that came before.

        The frame calls this method at each paint, before it decides the position of the
        page. ``cursor`` is :meth:`cursor_line`, and ``lines`` is the body that was just
        rendered. Each arrow since the last paint went to :meth:`handle` (refer to
        :meth:`edge_scroll`). Each arrow moved the highlight, or found it at its edge. The
        page tells which: a highlight on the same body line, drawn in the same way, did not
        move. Thus the page scrolls those lines instead, and the frame stops to follow the
        highlight. The comparison of the drawn line, not only of its index, tells a
        highlight that did not move from a highlight in a list window (refer to
        :class:`ListWindow`). There, the highlight stays on one line while the rows go past
        under it.

        Arrows that are pressed faster than the screen paints are judged together. Thus a
        held arrow that gets to the last row in the middle of a batch scrolls from the next
        batch on.
        """
        mark = None
        if self.edge_scrolls and cursor is not None and 0 <= cursor < len(lines):
            mark = (cursor, lines[cursor])
        if mark is None:
            self._edge_scrolled = False
        elif self._edge_steps and mark == self._highlight_mark:
            self._edge_scrolled = True
            self.scroll_lines(self._edge_steps)
        self._edge_steps = 0
        self._highlight_mark = mark

    @property
    def edge_scrolled(self) -> bool:
        """Whether the page is edge-scrolled, so the frame leaves the highlight where it is."""
        return self._edge_scrolled

    def note_cursor_side(self, side: int) -> None:
        """Record where the last paint left the highlight: 0 visible, -1 above, +1 below.

        The frame calls this method. The value decides the snap-back (refer to
        :meth:`edge_scroll`).
        """
        self._cursor_side = side

    def resolve(self, value: Any) -> None:
        """Resolve the future of this screen, and commit ``value`` (or :data:`CANCEL`).

        Args:
            value: The result to return to the caller that awaits it.
        """
        if self.future is not None and not self.future.done():
            self.future.set_result(value)

    # --- scrolling helpers (shared) ------------------------------------------

    def note_metrics(self, total: int, viewport: int) -> None:
        """Record the body height and the viewport of the last render.

        The frame calls this method at each paint. The shared scroll jumps and section jumps
        below get their sizes from these values. Thus a PageDown moves by one viewport of
        the current terminal, not by a fixed constant. Also, the top and bottom clamps
        follow the real content height, and the caller does not have to give the sizes.
        """
        self._scroll_total = max(1, total)
        self._scroll_viewport = max(1, viewport)

    def note_viewport(self, viewport: int) -> None:
        """Record only the viewport height (for callers that do not know the body total)."""
        self._scroll_viewport = max(1, viewport)

    @property
    def _page_step(self) -> int:
        """The lines that PageUp/PageDown move: the viewport, less one line for continuity."""
        return max(1, self._scroll_viewport - 1)

    @property
    def content_overflows(self) -> bool:
        """Whether the last rendered body is taller than its viewport: that is, it can scroll.

        This is the reusable condition for the rule of the app that a footer shows only a
        key that does something. A screen whose body fits has nothing to scroll, so it
        removes its ``↑↓ PgUp/PgDn scroll`` atom (refer to
        :attr:`DashboardScreen.footer_hint`). It reads the metrics that the frame recorded
        at the previous paint (:meth:`note_metrics`). Thus it becomes correct one frame
        after a resize, but the frame paints again immediately.
        """
        return self._scroll_total > self._scroll_viewport

    def scroll_by(self, delta: int, total: int, viewport: int) -> None:
        """Change :attr:`scroll` by ``delta`` lines, clamped to the given content bounds."""
        self.scroll = _clamp_scroll(self.scroll + delta, total, viewport)

    def scroll_lines(self, delta: int) -> None:
        """Scroll by ``delta`` lines, clamped to the content bounds that were noted last."""
        self.scroll = _clamp_scroll(self.scroll + delta, self._scroll_total, self._scroll_viewport)

    def scroll_pages(self, pages: int) -> None:
        """Scroll by ``pages`` page steps (a negative number scrolls up)."""
        self.scroll_lines(pages * self._page_step)

    def scroll_to_top(self) -> None:
        """Scroll to the first line."""
        self.scroll = 0

    def scroll_to_bottom(self) -> None:
        """Scroll to the end, so that the last line is at the bottom."""
        self.scroll = max(0, self._scroll_total - self._scroll_viewport)

    def scroll_to_section_start(self) -> None:
        """Scroll to the top of the section that holds the top visible line.

        The stored :attr:`_sticky_headers` mark the limits of the sections (the group
        dividers of a select list, the day dividers of the chat transcript). If that heading
        is already the top line, scroll to the previous section instead. Thus more presses
        go up one section at a time, as the paragraph jump of a text editor does. If there
        are no sections, scroll to the top.
        """
        offsets = [idx for idx, _ in self._sticky_headers]
        governing = max((o for o in offsets if o <= self.scroll), default=None)
        if governing is None:
            target = 0
        elif governing < self.scroll:
            target = governing  # up to the top of the current section
        else:
            target = max((o for o in offsets if o < self.scroll), default=0)
        self.scroll = _clamp_scroll(target, self._scroll_total, self._scroll_viewport)

    def scroll_to_next_section(self) -> None:
        """Scroll to the start of the next section below the top visible line (or the bottom)."""
        offsets = [idx for idx, _ in self._sticky_headers]
        nxt = min((o for o in offsets if o > self.scroll), default=None)
        target = nxt if nxt is not None else self._scroll_total
        self.scroll = _clamp_scroll(target, self._scroll_total, self._scroll_viewport)


class LazyLines(_SequenceABC):
    """The ANSI lines of a body, each rendered the first time that something reads it.

    A screen renders its full body, and then the frame slices a viewport out of it. Thus a
    list of 141 contacts renders 150 rows to show the twenty that fit, at each key press and
    at each idle tick. Nothing reads the lines outside the slice. They are built and then
    thrown away.

    This class is the same list, deferred. A screen gives one entry for each body line. The
    entry is the finished string (for the few lines that the screen had to render in any
    case: separators, headings, the query echo) or a callable that will render the line.
    Only the entries that the frame indexes are called. It is a plain
    :class:`~collections.abc.Sequence`. Thus ``len()``, slices, iteration, and ``==``
    against a list all behave exactly as the list that it replaces. A slice becomes a real
    list of strings, which the frame then pads and puts in its frame.

    Two properties make it safe as a replacement:

    * **The line count is exact and immediate.** When the drawing of a row is deferred,
      the number of rows is never deferred, because the screen counts the rows while it
      plans. Thus the scroll clamp, the ``↑↓ more`` markers, and the sticky-header offsets
      do not change.
    * **A line is rendered a maximum of one time.** The results are cached for each index.
      Thus, when the frame reads a row again (a pinned header, a slice that settled again),
      it costs nothing more.

    The content of a row is resolved in its callable. Thus a :class:`Choice` with a live
    callable title is resolved only while it is on the screen: the same rows that a person
    can see change.
    """

    __slots__ = ("_entries", "_drawn")

    def __init__(self, entries: list[str | Callable[[], str]]) -> None:
        """Wrap one entry for each body line: a rendered string, or a callable that renders it."""
        self._entries = entries
        self._drawn: dict[int, str] = {}

    def __len__(self) -> int:
        """The height of the body in lines. It is known without a render of any line."""
        return len(self._entries)

    def __getitem__(self, index):  # type: ignore[no-untyped-def]
        """Return line ``index`` (or a real list of lines for a slice), rendered on demand."""
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self._entries)))]
        if index < 0:
            index += len(self._entries)
        if not 0 <= index < len(self._entries):
            raise IndexError(index)
        line = self._drawn.get(index)
        if line is None:
            entry = self._entries[index]
            line = entry if isinstance(entry, str) else entry()
            self._drawn[index] = line
        return line

    def __eq__(self, other: object) -> bool:
        """Compare equal to the plain list of lines that this object replaces."""
        if isinstance(other, (LazyLines, list)):
            return list(self) == list(other)
        return NotImplemented

    def __repr__(self) -> str:
        """Describe the height, and how many lines are drawn."""
        return f"LazyLines({len(self._entries)} lines, {len(self._drawn)} drawn)"


class ListWindow:
    """The scroll state of a list that scrolls inside a fixed screen.

    This is the list-window pattern of the app (first used by the link list of the mesh
    walk). The screen fits its fixed chrome (headings, charts, action rows) to the
    viewport of the frame (refer to :meth:`Screen.note_viewport`: the frame records it
    before ``render_body`` runs). Then the screen gives the remaining lines to this class,
    and only the rows of the list scroll in them. Faint ``↑ n more`` / ``↓ n more`` markers
    (refer to :meth:`marker`) take the edge lines of the list window exactly when rows are
    hidden on that side. The content capacity, after it settles, is kept as :attr:`page`:
    the step that PgUp/PgDn must move by.

    A highlight over such a list **clamps at both ends and does not wrap** (as
    :class:`~meshterm.ui.tui.select.SelectScreen` does, for the same reason, which is
    stronger here). The list window follows the highlight. Thus, if a ↓ off the last row
    rolls over, it does more than move the pointer. It pulls the full list window back to
    the start, and changes the edge markers as it goes. To the user, this looks like a change of the
    screen under them, not like one step. Held-down arrows stop at an end instead (JP,
    2026-09-04). Each screen owns its own highlight, so its ``handle`` keeps this rule.
    This class cannot enforce it.

    **A list window is for a list that the highlight moves through.** For this reason,
    :meth:`fit` and :meth:`fit_blocks` take the highlight with no default. Each row that a
    list window hides must be a row that the arrows can reach when the highlight moves onto
    it, because the list window follows the highlight. Rows that nothing highlights (a log,
    a results table) belong to the page instead. There, the frame scrolls them, and edge
    scroll (:meth:`Screen.edge_scroll`) reaches them. When such rows were in a private list
    window, they made the page exactly as tall as the screen. Thus the frame found nothing
    to scroll: ↓ on the last row did nothing while a ``↓ n more`` was under it (the results
    of the trace screen, JP 2026-10-04). ``None`` is still a highlight argument, for a list
    whose rows the highlight just left for another part of the same screen.

    Attributes:
        top: The first list row that the list window shows (:meth:`fit` clamps it at each
            paint).
        page: The rows that the list window held at the last :meth:`fit`: the paging step.
    """

    def __init__(self) -> None:
        """Start at the top, with a safe paging step for the time before the first paint."""
        self.top = 0
        self.page = 6

    def fit(self, n: int, win: int, index: int | None) -> tuple[int, int]:
        """Settle the list window over ``n`` rows in ``win`` lines: ``(top, count)``.

        The edge markers take the boundary lines of the list window exactly when rows are
        hidden past them. Thus the content capacity changes when the list window slides. A
        few passes settle the top, the capacity, and the highlight clamp together.

        Args:
            n: The total number of list rows.
            win: The lines that the list window can use, for content and for markers.
            index: The highlighted row, which must stay visible, or ``None`` while the
                highlight is in another part of the screen (then ``top`` is only clamped).
                Necessary: a list window over rows that no highlight moves through hides
                them from the arrows (refer to the class docstring).

        Returns:
            The first visible row and how many rows to draw. ``top`` rows are hidden above,
            and ``n - top - count`` rows below. Each side is non-zero exactly when its
            marker line must be drawn.
        """
        if n <= win:
            self.top = 0
            self.page = max(1, win)
            return 0, n
        top = max(0, min(self.top, n - 1))
        count = 1
        for _ in range(4):
            above = 1 if top > 0 else 0
            below = 1 if n - top > win - above else 0
            count = max(1, win - above - below)
            if index is not None:
                if index < top:
                    top = index
                elif index >= top + count:
                    top = index - count + 1
            top = max(0, min(top, n - count))
        self.top = top
        self.page = count
        return top, count

    def fit_blocks(self, heights: list[int], win: int, index: int | None) -> tuple[int, int]:
        """Settle the list window over rows of different heights: ``(top, count)`` blocks.

        This is the sibling of :meth:`fit` for hanging wraps: a list whose rows are blocks
        of rendered lines (a route row and the muted facts under it, a wrapped path line),
        not one line each. A block is never split across the edge of the list window. The
        faint edge markers take a line of the list window exactly when rows are hidden past
        them. The list window moves step by step until the highlighted block is visible.

        Args:
            heights: The rendered line count of each list row.
            win: The lines that the list window can use, for content and for markers.
            index: The highlighted row, which must stay visible, or ``None`` while the
                highlight is in another part of the screen (then ``top`` is only clamped).
                Necessary, as for :meth:`fit`.

        Returns:
            The first visible row and how many rows to draw, as :meth:`fit` returns them.
        """
        n = len(heights)
        if sum(heights) <= win:
            self.top = 0
            self.page = max(1, n)
            return 0, n
        top = max(0, min(self.top, n - 1))
        if index is not None:
            top = min(top, index)
        while True:
            above = 1 if top > 0 else 0
            count = _fill(heights, top, win - above)
            if top + count < n:  # rows are hidden below: the marker takes one of the lines
                count = max(1, _fill(heights, top, win - above - 1))
            if index is None or index < top + count:
                break
            top += 1  # move the list window down until the highlighted row is in it
        self.top = top
        self.page = max(1, count)
        return top, count

    def to_end(self) -> None:
        """Slide the list window to the end of the list at the next :meth:`fit`.

        The value is too large on purpose. The caller seldom knows the list length at the
        time of the key press, and :meth:`fit` clamps to the last full list window in all
        cases.
        """
        self.top = 10**9

    @staticmethod
    def marker(hidden: int, direction: str) -> Text:
        """The faint edge line that counts the rows hidden ``"above"`` or ``"below"``."""
        arrow = "↑" if direction == "above" else "↓"
        return Text(f"  {arrow} {hidden} more", style="faint")


def _fill(heights: list[int], top: int, budget: int) -> int:
    """How many whole blocks from ``top`` fit into ``budget`` lines (greedy, in order)."""
    used = 0
    count = 0
    for h in heights[top:]:
        if used + h > budget:
            break
        used += h
        count += 1
    return count


def _clamp_scroll(scroll: int, total: int, viewport: int) -> int:
    """Clamp a scroll offset to the valid range for the content and the viewport.

    Args:
        scroll: The proposed offset.
        total: The total line count.
        viewport: The height of the viewport in lines.

    Returns:
        The offset clamped to ``0 .. max(0, total - viewport)``.
    """
    return max(0, min(scroll, max(0, total - viewport)))


class ScrollScreen(Screen):
    """A read-only screen that shows a Rich renderable in a viewport that scrolls.

    It is used for the result screens of tools and for all long lists. Content that is
    taller than the viewport scrolls with the arrows, PageUp / PageDown (one viewport), and
    Home / End (or Ctrl+Home / Ctrl+End). Esc closes it. Result screens have no sections, so
    Ctrl+PageUp/PageDown only go to the top or the bottom.
    """

    @property
    def picocalc_lyra_lane(self):
        """The shared lane, with its navigation slots dimmed when the content already fits.

        Each navigation key here only scrolls. Thus a result screen that is short enough to
        read in full has four slots that do nothing. The footer hint on the other platform
        uses the same condition (:attr:`Screen.content_overflows`).
        """
        from .fkeys import default_lane

        return default_lane(nav=self.content_overflows)

    def __init__(
        self,
        renderable: RenderableType,
        *,
        title: str = "",
        footer_hint: str | None = None,
        floating: bool = True,
    ) -> None:
        """Wrap a renderable in a screen that the user can scroll and close.

        Args:
            renderable: The Rich content to show.
            title: The heading of the screen or the dialog.
            footer_hint: The footer key hint, or ``None`` to make it from the state of the
                screen (refer to :attr:`footer_hint`).
            floating: Whether to draw the screen as a dialog at the centre, over the parent
                screen.
        """
        super().__init__()
        self._renderable = renderable
        self.title = title
        self._footer_hint = footer_hint
        self.floating = floating

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The footer keys: the hint that the caller gave, or the correct default.

        A read-only body has only two keys to name: the pager and Esc. Thus the hint can be
        made from the state of the screen, instead of written at each call site. A hint
        made in this way keeps the permanent rule that a footer never shows a key that does
        nothing. The scroll atom appears only while the body is taller than its viewport
        (:attr:`Screen.content_overflows`). The Esc verb follows the surface, as the
        standards say: a floating read-only dialog closes, and a full screen goes back.
        """
        if self._footer_hint is not None:
            return self._footer_hint
        verb = "Esc close" if self.floating else "Esc back"
        return f"↑↓ PgUp/PgDn scroll · {verb}" if self.content_overflows else verb

    def replace_content(self, renderable: RenderableType) -> None:
        """Replace the body in place, and keep the user at the same position.

        This is the :meth:`~meshterm.ui.tui.select.SelectScreen.replace_items` of the
        select list, for a read-only body. When something on a page changes (a value that
        is now shown, a figure that is refreshed), it is still the page that the user was
        part of the way down. Thus the scroll offset stays. (The next scroll clamps the
        offset in any case. Thus a body whose height changed cannot leave the viewport past
        its end.) Build the screen again instead when its content is really a different
        subject.
        """
        self._renderable = renderable

    def render_body(self, width: int) -> list[str]:
        """Render the wrapped content to ANSI lines, and keep the total count."""
        lines = render_lines(self._renderable, width)
        self._scroll_total = max(1, len(lines))
        return lines

    def handle(self, action: str, data: str = "") -> None:
        """Scroll the viewport (by line, by page, or to an edge), or close the screen."""
        if action == "up":
            self.scroll_lines(-1)
        elif action == "down":
            self.scroll_lines(1)
        elif action == "pageup":
            self.scroll_pages(-1)
        elif action in ("pagedown", "space"):
            self.scroll_pages(1)
        elif action in ("home", "ctrl_home"):
            self.scroll_to_top()
        elif action in ("end", "ctrl_end"):
            self.scroll_to_bottom()
        elif action == "ctrl_pageup":
            self.scroll_to_section_start()
        elif action == "ctrl_pagedown":
            self.scroll_to_next_section()
        elif action in ("escape", "enter"):
            self.resolve(None)


class BusyScreen(Screen):
    """A non-interactive splash body: an animated spinner next to a message.

    It keeps the startup splash on the screen, with no change to its wordmark and its box,
    while a short async task runs (for example, a smoke test of a companion that the user
    chose). It is set as a base without chrome. Thus it draws again the same splash at the
    centre under the wordmark, and changes only the contents of the box. It resolves nothing
    and ignores all keys. The session pops it when the awaited task completes. The animation
    itself is the reusable :class:`~meshterm.ui.tui.spinner.Spinner`.
    """

    footer_hint = "working…"
    floating = False
    modal = True  # the work owns the keyboard. ^W must not unwind the stack while it runs.

    @property
    def picocalc_lyra_lane(self):
        """No lane: this screen ignores all keys, so no key has an effect."""
        from .fkeys import EMPTY_LANE

        return EMPTY_LANE

    def __init__(self, message: str, *, title: str = "") -> None:
        """Start a busy splash that shows ``message`` under an optional ``title``."""
        super().__init__()
        self.title = title
        self._message = message
        self._spinner = Spinner()

    def tick(self) -> None:
        """Advance the spinner to its next animation step.

        The animation timer of the session calls this method.
        """
        self._spinner.tick()

    def render_body(self, width: int) -> list[str]:
        """Render the current animation step of the spinner, then the message.

        A caption can contain line feeds: what the task does on one line, and its progress
        on the next line. Each line after the first hangs under the text of the caption,
        not under the spinner. The same is true for each line that is too long for the box.
        This is the rule for wrapped rows that each labelled block follows. When the caption
        wrapped without this rule, a caption that ended in a count moved the last letter of
        the count alone onto a line, at the left edge.
        """
        first, *more = self._message.split("\n")
        prefix = self._spinner.text()
        prefix.append("  ")
        indent = prefix.cell_len
        lines = render_hanging(prefix, Text(first), width, indent=indent)
        for line in more:
            lines += render_hanging(Text(" " * indent), Text(line), width, indent=indent)
        return lines

    def handle(self, action: str, data: str = "") -> None:
        """Ignore all keys: the splash closes itself when the task finishes."""
        return


class BusyDialog(BusyScreen):
    """A busy splash, drawn as a box over the screen that it interrupts.

    It is the twin of :class:`BusyScreen`, for work that starts from a screen, not instead
    of a screen. It has the same spinner, the same caption, and the same
    :meth:`~BusyScreen.handle` that ignores all keys. But it is ``floating``. Thus the hub
    that started the work stays visible below it as a backdrop, and is not replaced.

    It is modal because it inherits from :class:`BusyScreen`, and this is the important
    part. A hub screen that ``session.stay`` keeps on the stack is armed all the time (refer
    to :meth:`~meshterm.ui.tui.session.Visit._arm`). Thus, while the hub waits for a slow
    write to the device, each key that the user presses still gets to it. Letters go into
    its live filter, and Esc resolves it, and that result is returned at the moment that
    the work finishes. A modal screen on top stops this: the key gets to this dialog and
    stops here.

    :meth:`~meshterm.ui.tui.session.TuiSession.busy_overlay` cannot do this job. It is not
    a screen, so it never gets a key press. It paints only when the stack is empty, so over
    a pushed hub it draws nothing.
    """

    floating = True

    @property
    def dialog_width(self) -> int:
        """Fit the box to the caption, instead of to the maximum width of a dialog.

        A spinner and four words must not be in a box of 94 cells. The compositor still
        limits this width to the terminal (refer to
        :func:`~meshterm.ui.tui.frame._dialog_layout`). The width also ratchets (it only
        grows). Thus a caption that becomes longer during a batch never makes the box
        smaller again, and the box does not jitter.
        """
        widest = max(cell_len(line) for line in self._message.split("\n"))
        return widest + 8  # spinner, its two spaces, and the padded border

    @property
    def message(self) -> str:
        """The caption next to the spinner. A line feed starts a line that hangs under its text."""
        return self._message

    @message.setter
    def message(self, text: str) -> None:
        """Change the caption during a batch (a sequence of writes that tells which one runs)."""
        self._message = text
