# SPDX-License-Identifier: Apache-2.0
"""The screen abstraction: one layer in the TUI's window stack.

A :class:`Screen` knows how to render its body (as Rich, converted to ANSI lines by
:mod:`~meshterm.ui.tui.render`) and how to react to normalized key *actions* dispatched by
the :class:`~meshterm.ui.tui.session.TuiSession`. Screens never touch prompt_toolkit or the
terminal directly, so they stay small, uniform, and testable.

Interactive screens (select, text, confirm) resolve an :class:`asyncio.Future` when the user
commits or cancels; the session awaits that future and then pops the screen, giving the
push/await/pop model behind ``await session.select(...)`` and friends.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from collections.abc import Sequence as _SequenceABC
from typing import Any

from rich.cells import cell_len
from rich.console import RenderableType
from rich.text import Text

from .render import render_lines
from .spinner import Spinner

#: Sentinel a screen resolves with when the user presses Esc to cancel, distinct from a
#: legitimately selected ``None`` value.
CANCEL = object()

#: Sentinel the session resolves the top screen's future with when the pop-all key is
#: pressed (^W). It is never a legitimate result: every navigation boundary that sees it
#: turns it straight into :class:`PopToMenu`, so no caller ever has to test for it.
POP_ALL = object()

#: Edge scroll's keys (see :meth:`Screen.edge_scroll`): the arrows and the way each steps,
#: the jumps to either end, and the keys that bring an off-screen highlight back into view
#: before doing anything — every key that would move it or act on it.
_EDGE_STEPS = {"up": -1, "down": 1}
_EDGE_ENDS = {"home": -1, "ctrl_home": -1, "end": 1, "ctrl_end": 1}
_SNAP_BACK_ACTIONS = frozenset({"up", "down", "enter", "delete"})


class PopToMenu(BaseException):
    """Raised through every awaiting navigation frame to unwind to the main menu.

    The whole realized path is a stack of pending ``await`` expressions — a screen's caller
    awaiting its result, that caller's caller awaiting *it* — so the one way to leave every
    frame at once is to raise through them all. Each frame pops its own screen in a ``finally`` on
    the way out, which is exactly the strict single-pop discipline applied N times: no
    screen is ever dropped without its owner unwinding.

    It derives from :class:`BaseException`, not :class:`Exception`, for the same reason
    :class:`asyncio.CancelledError` does: the app is full of ``except Exception`` guards
    that stop a misbehaving tool from taking down the menu
    (:func:`~meshterm.ui.menu._run_selection` is the widest), and an unwind must pass
    through them rather than be swallowed and reported as a tool failure. ``finally``
    blocks still run, so nothing leaks.

    Caught in exactly one place — the main menu loop, which is the unwind's destination.
    """


class Screen:
    """Base class for every TUI layer.

    Attributes:
        title: Heading shown at the top of the screen/dialog.
        short_title: The abbreviated heading the frame draws instead of :attr:`title` where
            the full one would not fit the rule it sits in (see
            :func:`~meshterm.ui.tui.frame.fitted_title`) — the same atoms said tighter, so a
            narrow frame still carries every figure rather than a title cut off mid-word.
            Empty (the default) means the title has no shorter form.
        footer_hint: One-line key hint shown in the footer.
        scroll: Current vertical scroll offset into the body's rendered lines.
        future: Resolved with the screen's result (or :data:`CANCEL`) when it commits.
        floating: Whether the session should draw this screen as a centered dialog over
            the dimmed screen beneath it (deeper layers float; the base does not).
        modal: Whether this layer is a *dialog* — a prompt or a running operation that
            owns the keyboard until it is answered or finishes. Distinct from
            :attr:`floating`, which is only about how the layer is drawn: an ordinary
            select list floats as a box and is not modal, while a progress dialog is
            modal and a full-frame busy splash is modal without floating at all. The
            pop-all key (^W) refuses to fire while a modal layer is on top, so an unwind
            can never blow past an unanswered question or yank the stack out from under
            work in flight.
        grow_only: Whether a floating dialog may only ever grow. Its box is sized to the
            tallest body — and, for a natural-width dialog, the widest — it has shown, not
            the current one, so a screen whose content swings as it changes (the packet
            viewer paging between packets, the path composer's shifting suggestions) keeps
            a steady, centred box — blank-padded when the current body is shorter — instead
            of resizing on every change.
        chrome: Whether, as the base screen, this layer is wrapped in the session's
            persistent header/footer frame. A startup splash sets this ``False`` so the
            session instead centers it under the :attr:`banner` with no status bars.
        bare: Whether, as the base screen, this layer is the *whole frame*: its body on
            blank rows with no header, no footer, no title bar, no box and no wordmark —
            for a screen that is one thing a camera or a reader looks at (the share
            screen's QR code, see :func:`~meshterm.ui.qr.share_screen`). Nothing is
            advertised on it: Esc leaves, and Esc is on every keyboard. Read before
            :attr:`chrome`; a bare screen is never floating.
        banner: Block-glyph art (one string per row) drawn, centered, above a chromeless
            base screen. Ignored while ``chrome`` is ``True``.
        footnote: A short line drawn muted and centered *below* a chromeless base screen's
            box (e.g. a copyright notice). Ignored while ``chrome`` is ``True``.
        bottom_caption: A short credit or label set into the **bottom border rule**,
            right-justified, the way :attr:`title` sits in the top one — for something
            that belongs to the framed picture rather than to any row inside it. The map's
            OpenStreetMap credit is the reason it exists (see
            :mod:`meshterm.ui.attribution`): a bordered frame already draws that rule, so
            a caption in it costs the body nothing at all. Read fresh every paint, so a
            screen may change it as its state changes. Drawn only where the platform's
            frame *has* a bottom rule — the borderless one has none, and a screen wanting
            a mark there puts it in its own body instead.
        flush: Whether, as the base screen in a bordered frame, the body runs right up to
            the panel's side borders instead of sitting one blank column in from each. The
            padding is air between a border and a row of *text*; a picture that fills the
            frame (the map) has no text edge to keep off the border, and those two columns
            are drawing it would otherwise give up for nothing (JP, 2026-10-01). Moot where
            the platform's frame has no border — the body is the full width there already.
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
    #: Whether Home and End jump the highlight to the first and last row, so edge scroll
    #: takes the page to its very top and bottom with it (see :meth:`edge_scroll`). Off where
    #: they move a text caret instead — the chat's compose line.
    home_end_jumps: bool = True

    @property
    def fkey_lane(self):
        """The screen's F-key lane on the active platform (see :mod:`~meshterm.ui.tui.fkeys`).

        Each handheld deals its own: this reads whichever of the screen's lane definitions
        the platform's deck names (:attr:`picocalc_lane`, :attr:`cardputer_lane`). A
        platform with no lane — the desktop, whose footer is the hint line — gets an empty
        one. Override the per-deck definitions, never this.
        """
        from .fkeys import EMPTY_LANE, active_deck

        deck = active_deck()
        return EMPTY_LANE if deck is None else deck.read_lane(self)

    @property
    def cardputer_lane(self):
        """The screen's lane on the Cardputer Zero: the PicoCalc's, for now.

        JP, 2026-09-30: the Cardputer deals the same entries as the PicoCalc until decided
        otherwise — its Page Up/Down are real keys, but Fn+L/M is an awkward reach. Both
        having five slots is a coincidence of two keyboards, so this is the one place the
        two are tied; a screen whose Cardputer lane should differ overrides this.
        """
        return self.picocalc_lane

    @property
    def picocalc_lane(self):
        """The screen's lane on the PicoCalc (see :mod:`~meshterm.ui.tui.fkeys`).

        Five slots, F1–F5, each with an optional F6–F10 Shift-bank companion. A slot is
        for functionality that is otherwise hard to reach — Enter/Esc never appear, since
        both keys are already close at hand. The default puts paging (no physical key at
        all) on the prime F4/F5 pair and the jump-to-top/end pair behind Shift on those
        same two keys, since Home and End *are* on this keyboard and only ever wanted a
        chip for consistency with the pager they belong to. F1–F3 are left free: they are
        where a screen's own verbs go, and a screen with none leaves them blank. A screen
        overrides this property for its own verbs; the lane may differ screen to screen.

        Read fresh every paint, so an override dims the slots whose action would do
        nothing right now rather than advertising a dead key — the lane's standing rule
        (see :mod:`~meshterm.ui.tui.fkeys`). Screens whose nav keys only scroll gate the
        shared slots on :attr:`content_overflows`; this base can't assume that much,
        since a nav key elsewhere moves a cursor, a pick, or the map's zoom.
        """
        from .fkeys import DEFAULT_LANE

        return DEFAULT_LANE

    def bar_title(self, title: str, style: str) -> Text:
        """The heading as the borderless title bar draws it: ``title`` in the bar's ``style``.

        ``title`` is whichever form the bar chose (:func:`~meshterm.ui.tui.frame.fitted_title`).
        A heading that is a *mark* rather than words keeps the mark's own colours by
        overriding this — the main menu's wordmark, on a frame with no header row to carry
        it (see :attr:`~meshterm.platforms.Platform.header_row`).
        """
        return Text(title, style=style)

    def __init__(self) -> None:
        """Initialize scroll state and the (later-assigned) result future."""
        self.scroll = 0
        self.future: asyncio.Future | None = None
        # (body-line index, rendered ANSI lines) for each section landmark eligible to be
        # pinned to the top rows once it scrolls off. A block is usually one line — a section
        # heading, a day divider — but may carry the rows that belong with it (a heading and
        # the description under it), which pin together as they scroll off, in order. A screen
        # that wants sticky headers rebuilds this list while rendering its body (see
        # :meth:`sticky_block`); the default is no landmarks.
        self._sticky_headers: list[tuple[int, list[str]]] = []
        # The one header that pins for the *whole* list rather than for its section — a
        # table's column header, which means nothing scrolled off (see :meth:`sticky_rows`).
        # Recorded the same way, as (body-line index, rendered line); ``None`` for none.
        self._pinned_header: tuple[int, str] | None = None
        # The last render's body height and viewport, recorded by the frame (:meth:`note_metrics`)
        # so the shared scroll helpers can page by a screenful of the *current* terminal and
        # clamp to the content without every caller threading the sizes through.
        self._scroll_total = 1
        self._scroll_viewport = 1
        # A grow-only screen's high-water body height and natural width: the tallest and
        # widest it has rendered into a dialog, so its box can be held at that size once
        # reached (see :meth:`ratchet_viewport` / :meth:`ratchet_width`). Zero until the
        # first paint; irrelevant while not :attr:`grow_only`.
        self._viewport_floor = 0
        self._width_floor = 0
        # Edge scroll (see :meth:`edge_scroll`): whether ↑↓ have scrolled the window past the
        # highlight, so the frame has stopped pulling it into view; and whether the last
        # paint showed it, which decides the snap-back. Recorded by the frame.
        self._edge_scrolled = False
        self._cursor_shown = True

    # --- rendering -----------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Return the body's ANSI lines at ``width`` columns (unclipped).

        Args:
            width: Inner content width in columns.

        Returns:
            One ANSI string per body line; the session slices these to the viewport.
        """
        raise NotImplementedError

    def ratchet_viewport(self, body_h: int) -> int:
        """The body height a :attr:`grow_only` dialog is sized to: its running maximum.

        The frame calls this each paint with the current body's height. A grow-only
        screen returns the tallest height it has yet shown — so its box never shrinks,
        and the frame blank-pads a now-shorter body to fill it — while an ordinary
        screen returns the height unchanged and is sized to each body as it comes.
        """
        if not self.grow_only:
            return body_h
        self._viewport_floor = max(self._viewport_floor, body_h)
        return self._viewport_floor

    def ratchet_width(self, natural: int) -> int:
        """The natural width a :attr:`grow_only` dialog is sized to: its running maximum.

        The width-side twin of :meth:`ratchet_viewport`, applied by the frame to a
        dialog's requested ``dialog_width`` before the terminal cap — so a grow-only
        box holds its widest extent as its content narrows, but a shrinking terminal
        still clips it. Full-cap dialogs (no ``dialog_width``) never reach here.
        """
        if not self.grow_only:
            return natural
        self._width_floor = max(self._width_floor, natural)
        return self._width_floor

    def cursor_line(self) -> int | None:
        """Return a body line that must stay visible, or ``None`` for free scrolling.

        Selection/text screens return the active row so the session can keep it in view;
        plain scroll screens return ``None``.
        """
        return None

    def consume_edge_scrub(self) -> int:
        """Right-edge columns the session should force-repaint on the next paint (0 = none).

        The default is 0 — no screen needs this. A screen whose body can emit glyphs the
        terminal renders at an unexpected width (the map's braille) overrides this to have the
        session redraw just the smeared edge cells, which prompt_toolkit's differential paint
        would otherwise never rewrite (see :class:`~meshterm.ui.map_screen.MapScreen`).
        """
        return 0

    def sticky_block(self, scroll: int) -> list[str]:
        """The already-rendered body lines to pin as ``scroll`` moves past a section landmark.

        Lets a grouped list keep its current section overhead after the section's own rows have
        scrolled off — a select screen's ``Channels``/``Direct`` divider, the Trophy case's
        discipline heading *and the description under it*, the chat transcript's
        ``── Wed Jul 8 ──`` day divider. ``scroll`` is the offset the body is about to be
        sliced at; the returned lines are drawn as top rows, under any whole-list header pinned
        over them (:meth:`sticky_rows` composes them for the frame).

        The shared rule: among the blocks a screen recorded in :attr:`_sticky_headers` while
        rendering, find the last one starting at or above ``scroll`` (the one *governing* the
        top visible row) and pin exactly the rows of it that ``scroll`` has passed. So a block
        hands its rows over one at a time as they leave — the heading pins while its
        description is still the top content row, and the two pin together once both are gone —
        and the pinned rows always continue seamlessly into the body below. A screen opts in
        simply by populating :attr:`_sticky_headers`; the empty default means no pinning.

        A block never takes more than half the viewport: the rows are given up from the *end*,
        so the heading — the row that says which section this is — is the last thing dropped on
        a short terminal.
        """
        governing: tuple[int, list[str]] | None = None
        for entry in self._sticky_headers:
            if entry[0] <= scroll:
                governing = entry
            else:
                break  # blocks are recorded in body order; nothing past here can govern
        if governing is None:
            return []  # no landmark above the top visible row
        at, rows = governing
        keep = max(1, self._scroll_viewport // 2)
        return rows[: min(scroll - at, keep)]

    def sticky_rows(self, scroll: int) -> list[str]:
        """Every already-rendered line to pin above the body sliced at ``scroll``.

        What the frame actually draws, top-down: the whole-list header from
        :attr:`_pinned_header` (a table's column header — the lane names, which mean the
        same everywhere in the list) once it has scrolled off, then the governing section
        block from :meth:`sticky_block`. A screen with only section headings pins one
        line; a grouped table pins both, so a scrolled row can still be read off its lanes
        *and* placed in its group.

        Args:
            scroll: The offset the body is about to be sliced at.

        Returns:
            The lines to pin, in draw order (empty for nothing to pin). Each one costs the
            body a row, so the frame reserves exactly as many as come back.
        """
        rows: list[str] = []
        if self._pinned_header is not None and self._pinned_header[0] < scroll:
            rows.append(self._pinned_header[1])
        rows.extend(self.sticky_block(scroll))
        return rows

    # --- input ---------------------------------------------------------------

    def handle(self, action: str, data: str = "") -> None:
        """React to a normalized key action from the session.

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

    def cursor_at_edge(self, step: int) -> bool | None:
        """Whether the highlight can go no further ``step`` way (-1 up, +1 down).

        The gate of :meth:`edge_scroll`. ``None`` (the default) where ↑↓ move no highlight —
        a plain scroll screen, a prompt whose arrows recall history — and edge scroll stays
        out of the way. A screen whose ↑↓ walk a highlight over rows answers for it: ``True``
        on the first row going up and the last going down (and either way with no row to
        highlight at all), ``False`` everywhere else — most simply through
        :meth:`row_at_edge`.
        """
        return None

    @staticmethod
    def row_at_edge(index: int, count: int, step: int) -> bool:
        """:meth:`cursor_at_edge`'s answer for a highlight at ``index`` of ``count`` rows."""
        if count <= 0:
            return True
        return index <= 0 if step < 0 else index >= count - 1

    def edge_scroll(self, action: str) -> bool:
        """Edge scroll and the snap-back: take ``action`` here, before :meth:`handle` sees it.

        **Edge scroll** (JP, 2026-10-04): where the highlight can go no further, an arrow
        scrolls the page a line instead, so whatever sits above the first selectable row or
        below the last — a node's vitals, a list's lead-in, a closing note — can always be
        brought into view. The frame stops pulling the highlight into view while the page is
        edge-scrolled, so it may scroll off screen; that is deliberate. **Home** and **End**
        go the same way: the highlight jumps to the first or last row as ever, and the page
        to its very top or bottom.

        **The snap-back**: while the highlight is off screen, a key that would move it or act
        on it — the opposite arrow, Enter, Delete — first brings it back into view,
        jumping if it has to, and does nothing else. Nothing is run that cannot be seen.

        Any other key ends the edge scroll before :meth:`handle` sees it (a typed filter, a
        tab switch, a page key), so the frame follows the highlight again from there.

        Returns:
            Whether the key was taken here; the session passes it to :meth:`handle` only
            when it wasn't.
        """
        if self.cursor_at_edge(-1) is None:
            return False
        step = _EDGE_STEPS.get(action)
        if step is not None and self.cursor_at_edge(step):
            self._edge_scrolled = True
            self.scroll_lines(step)
            return True
        if self._edge_scrolled and not self._cursor_shown and action in _SNAP_BACK_ACTIONS:
            self._edge_scrolled = False  # the snap-back: this press only brings it home
            return True
        if action in _EDGE_ENDS and self.home_end_jumps:
            self.handle(action)
            self._edge_scrolled = True
            if _EDGE_ENDS[action] < 0:
                self.scroll_to_top()
            else:
                self.scroll = self._scroll_total  # the frame clamps it to the last screenful
            return True
        self._edge_scrolled = False
        return False

    @property
    def edge_scrolled(self) -> bool:
        """Whether the page is edge-scrolled, so the frame leaves the highlight where it is."""
        return self._edge_scrolled

    def note_cursor_shown(self, shown: bool) -> None:
        """Record whether the last paint showed the highlight (the frame calls this)."""
        self._cursor_shown = shown

    def resolve(self, value: Any) -> None:
        """Resolve this screen's future, committing ``value`` (or :data:`CANCEL`).

        Args:
            value: The result to hand back to the awaiting caller.
        """
        if self.future is not None and not self.future.done():
            self.future.set_result(value)

    # --- scrolling helpers (shared) ------------------------------------------

    def note_metrics(self, total: int, viewport: int) -> None:
        """Record the last render's body height and viewport (the frame calls this each paint).

        The shared scroll/section jumps below size themselves from these, so a PageDown moves
        by a screenful of the current terminal rather than a fixed constant, and the top/bottom
        clamps track the real content height without the caller passing the sizes in.
        """
        self._scroll_total = max(1, total)
        self._scroll_viewport = max(1, viewport)

    def note_viewport(self, viewport: int) -> None:
        """Record just the viewport height (for callers that don't know the body total)."""
        self._scroll_viewport = max(1, viewport)

    @property
    def _page_step(self) -> int:
        """Lines a PageUp/PageDown moves: a screenful, bar one row kept for continuity."""
        return max(1, self._scroll_viewport - 1)

    @property
    def content_overflows(self) -> bool:
        """Whether the last-rendered body is taller than its viewport — i.e. it can scroll.

        The reusable gate for the app-wide rule that a footer only advertises a key that
        would do something: a screen whose body fits has nothing to scroll, so it drops its
        ``↑↓ PgUp/PgDn scroll`` atom (see :attr:`DashboardScreen.footer_hint`). Reads the
        metrics the frame recorded on the *previous* paint (:meth:`note_metrics`), so it is
        honest one frame after a resize — the frame repaints again immediately.
        """
        return self._scroll_total > self._scroll_viewport

    def scroll_by(self, delta: int, total: int, viewport: int) -> None:
        """Adjust :attr:`scroll` by ``delta`` lines, clamped to the given content bounds."""
        self.scroll = _clamp_scroll(self.scroll + delta, total, viewport)

    def scroll_lines(self, delta: int) -> None:
        """Scroll by ``delta`` lines, clamped to the last-recorded content bounds."""
        self.scroll = _clamp_scroll(self.scroll + delta, self._scroll_total, self._scroll_viewport)

    def scroll_pages(self, pages: int) -> None:
        """Scroll by ``pages`` screenfuls (negative scrolls up)."""
        self.scroll_lines(pages * self._page_step)

    def scroll_to_top(self) -> None:
        """Jump the view to the first line."""
        self.scroll = 0

    def scroll_to_bottom(self) -> None:
        """Jump the view to the last screenful."""
        self.scroll = max(0, self._scroll_total - self._scroll_viewport)

    def scroll_to_section_start(self) -> None:
        """Move to the top of the section holding the top visible line.

        Sections are delimited by the recorded :attr:`_sticky_headers` (a select list's group
        dividers, the chat transcript's day dividers). If that heading is already the top line,
        move to the *previous* section instead — so repeated presses walk up section by section,
        the way a text editor's paragraph jump does. Falls back to the very top with no sections.
        """
        offsets = [idx for idx, _ in self._sticky_headers]
        governing = max((o for o in offsets if o <= self.scroll), default=None)
        if governing is None:
            target = 0
        elif governing < self.scroll:
            target = governing  # up to the top of the section we're inside
        else:
            target = max((o for o in offsets if o < self.scroll), default=0)
        self.scroll = _clamp_scroll(target, self._scroll_total, self._scroll_viewport)

    def scroll_to_next_section(self) -> None:
        """Move to the start of the next section below the top visible line (else the bottom)."""
        offsets = [idx for idx, _ in self._sticky_headers]
        nxt = min((o for o in offsets if o > self.scroll), default=None)
        target = nxt if nxt is not None else self._scroll_total
        self.scroll = _clamp_scroll(target, self._scroll_total, self._scroll_viewport)


class LazyLines(_SequenceABC):
    """A body's ANSI lines, each rendered the first time something actually reads it.

    A screen renders its body *whole* and the frame then slices a viewport out of it — so a
    141-contact list rasterizes 150 rows to show the twenty that fit, on every keystroke and
    on every idle tick. The lines outside the slice are never looked at; they are built and
    thrown away.

    This is the same list, deferred. A screen hands over one entry per body line — either the
    finished string (for the few lines it had to render anyway: separators, headings, the
    query echo) or a callable that will render it — and only the entries the frame indexes
    are ever called. It is a plain :class:`~collections.abc.Sequence`, so ``len()``, slicing,
    iteration and ``==`` against a list all behave exactly as the list it replaces; a slice
    materializes to a real list of strings, which is what the frame goes on to pad and frame.

    Two properties make it safe to substitute:

    * **The line count is exact and immediate.** Deferring the *drawing* of a row never
      defers *how many rows there are* — the screen counts them while it plans — so the
      scroll clamp, the ``↑↓ more`` markers and the sticky-header offsets are unchanged.
    * **A line is rendered at most once.** Results are memoized per index, so the frame
      re-reading a row (a pinned header, a re-settled slice) costs nothing extra.

    A row's content is resolved inside its callable, which means a :class:`Choice` with a
    live callable title is resolved only while it is on screen — the same rows a person can
    actually see changing.
    """

    __slots__ = ("_entries", "_drawn")

    def __init__(self, entries: list[str | Callable[[], str]]) -> None:
        """Wrap one entry per body line: a rendered string, or a callable rendering it."""
        self._entries = entries
        self._drawn: dict[int, str] = {}

    def __len__(self) -> int:
        """The body's height in lines — known without rendering any of them."""
        return len(self._entries)

    def __getitem__(self, index):  # type: ignore[no-untyped-def]
        """Return line ``index`` (or a real list of lines for a slice), rendering on demand."""
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
        """Compare equal to the plain list of lines this stands in for."""
        if isinstance(other, (LazyLines, list)):
            return list(self) == list(other)
        return NotImplemented

    def __repr__(self) -> str:
        """Describe the height and how much of it has actually been drawn."""
        return f"LazyLines({len(self._entries)} lines, {len(self._drawn)} drawn)"


class ListWindow:
    """Scroll state for a list that scrolls *inside* a fixed screen.

    The app-wide windowed-list pattern (established by the mesh walk's link list): the
    screen sizes its fixed chrome — headings, charts, action rows — to the frame's
    viewport (see :meth:`Screen.note_viewport`; the frame records it before
    ``render_body`` runs) and hands the leftover rows here; only the list's rows
    scroll within them. Faint ``↑ n more`` / ``↓ n more`` markers (see
    :meth:`marker`) take the window's edge rows exactly when rows are hidden on
    that side, and the settled content capacity is remembered as :attr:`page`, the
    stride a PgUp/PgDn should move by.

    A highlight over such a list **clamps at both ends rather than wrapping** (as
    :class:`~meshterm.ui.tui.select.SelectScreen` does, and for the same reason, sharper
    here): the window follows the highlight, so a ↓ off the last row would not
    merely move the pointer — it would haul the whole window back to the head, flipping
    the edge markers as it went, which reads as the screen changing under the reader
    rather than as one step. Held-down arrows settle at an end instead (JP, 2026-09-04).
    Each screen owns its own cursor, so this is a rule its ``handle`` keeps, not something
    this class can enforce.

    Attributes:
        top: First list row the window shows (clamped by :meth:`fit` each paint).
        page: Rows the window carried on the last :meth:`fit` — the paging stride.
    """

    def __init__(self) -> None:
        """Start at the top with a conservative pre-first-paint stride."""
        self.top = 0
        self.page = 6

    def fit(self, n: int, win: int, index: int | None = None) -> tuple[int, int]:
        """Settle the window over ``n`` rows into ``win`` lines: ``(top, count)``.

        The edge markers eat the window's boundary rows exactly when rows hide
        beyond them, so the content capacity shifts as the window slides; a few
        passes settle top, capacity, and the highlight clamp together.

        Args:
            n: Total list rows.
            win: Lines the window may spend — on content and markers alike.
            index: A highlighted row that must stay visible, or ``None`` when the
                window scrolls free (a cursor-less log; ``top`` is just clamped).

        Returns:
            The first visible row and how many rows to draw. Rows hidden above
            number ``top``; below, ``n - top - count`` — each side non-zero exactly
            when its marker row should be drawn.
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

    def fit_blocks(self, heights: list[int], win: int, index: int | None = None) -> tuple[int, int]:
        """Settle the window over variable-height rows: ``(top, count)`` blocks to draw.

        The hanging-wrap sibling of :meth:`fit`, for a list whose rows are *blocks* of
        rendered lines — a route row and the muted facts under it, a wrapped path line —
        rather than one line each. A block is never split across the window's edge, the
        faint edge markers eat a window line exactly when rows hide beyond them, and the
        highlighted block is walked into view.

        Args:
            heights: Rendered line count per list row.
            win: Lines the window may spend — on content and markers alike.
            index: A highlighted row that must stay visible, or ``None`` when the window
                scrolls free (``top`` is then just clamped).

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
            if top + count < n:  # rows hide below: the marker takes one of the lines
                count = max(1, _fill(heights, top, win - above - 1))
            if index is None or index < top + count:
                break
            top += 1  # walk the window down until the cursor's row is inside
        self.top = top
        self.page = max(1, count)
        return top, count

    def to_end(self) -> None:
        """Slide the window to the list's tail on the next :meth:`fit`.

        The overshoot is deliberate — the caller rarely knows the list length at
        keypress time, and :meth:`fit` clamps to the last windowful either way.
        """
        self.top = 10**9

    @staticmethod
    def marker(hidden: int, direction: str) -> Text:
        """The faint edge row counting rows hidden ``"above"`` or ``"below"``."""
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
    """Clamp a scroll offset to the valid range for the content and viewport.

    Args:
        scroll: Proposed offset.
        total: Total line count.
        viewport: Visible height in lines.

    Returns:
        The offset clamped to ``0 .. max(0, total - viewport)``.
    """
    return max(0, min(scroll, max(0, total - viewport)))


class ScrollScreen(Screen):
    """A read-only screen that shows a Rich renderable in a scrollable viewport.

    Used for tool result windows and any long list. Content taller than the viewport scrolls
    with the arrows / PageUp / PageDown (a screenful) / Home / End (or Ctrl+Home / Ctrl+End);
    Esc dismisses it. Result windows carry no sections, so Ctrl+PageUp/PageDown just reach the
    top/bottom.
    """

    @property
    def picocalc_lane(self):
        """The shared lane, its nav slots dimmed when the content already fits.

        Every nav key here scrolls and nothing else, so a result window short enough to
        read whole has four inert slots — the same gate the footer hint uses on the other
        platform (:attr:`Screen.content_overflows`).
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
        """Wrap a renderable in a dismissable, scrollable screen.

        Args:
            renderable: The Rich content to display.
            title: Heading for the screen/dialog.
            footer_hint: Footer key hint, or ``None`` to derive it (see
                :attr:`footer_hint`).
            floating: Whether to draw as a centered dialog over the parent.
        """
        super().__init__()
        self._renderable = renderable
        self.title = title
        self._footer_hint = footer_hint
        self.floating = floating

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The footer keys — whatever the caller passed, or the honest default.

        A read-only body has only two keys to name, the pager and Esc, so the hint can
        be derived rather than spelled out at each call site — and derived, it keeps the
        standing rule that a footer never advertises a key that would do nothing: the
        scroll atom appears only while the body is actually taller than its viewport
        (:attr:`Screen.content_overflows`). The Esc verb follows the surface the standards
        assign it: a floating read-only view *closes*, a full screen goes *back*.
        """
        if self._footer_hint is not None:
            return self._footer_hint
        verb = "Esc close" if self.floating else "Esc back"
        return f"↑↓ PgUp/PgDn scroll · {verb}" if self.content_overflows else verb

    def replace_content(self, renderable: RenderableType) -> None:
        """Swap the body in place, keeping the reader where they were.

        The select list's :meth:`~meshterm.ui.tui.select.SelectScreen.replace_items` for a
        read-only body: the same page with something in it changed — a value uncovered, a
        figure refreshed — is still the page the reader was part-way down, so the scroll
        offset stands. (It is clamped on the next scroll anyway, so a body that did change
        height cannot strand the viewport past its end.) Rebuild the screen instead when
        what is on it is genuinely a different subject.
        """
        self._renderable = renderable

    def render_body(self, width: int) -> list[str]:
        """Render the wrapped content to ANSI lines and remember the total count."""
        lines = render_lines(self._renderable, width)
        self._scroll_total = max(1, len(lines))
        return lines

    def handle(self, action: str, data: str = "") -> None:
        """Scroll the viewport (by line, page, or to an edge) or dismiss the screen."""
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
    """A non-interactive splash body: an animated spinner beside a message.

    Used to keep the startup splash on screen — its wordmark and box unchanged — while a
    short async task runs (e.g. smoke-testing a chosen companion device). It set as a
    chromeless base so it redraws the same centered-under-the-wordmark splash, only swapping
    the box's contents. It resolves nothing and ignores every key; the session pops it when
    the awaited task completes. The animation itself is the reusable
    :class:`~meshterm.ui.tui.spinner.Spinner`.
    """

    footer_hint = "working…"
    floating = False
    modal = True  # work in flight owns the keyboard; ^W must not unwind out from under it

    @property
    def picocalc_lane(self):
        """No lane at all: this screen swallows every key, so none of them do anything."""
        from .fkeys import EMPTY_LANE

        return EMPTY_LANE

    def __init__(self, message: str, *, title: str = "") -> None:
        """Start a busy splash showing ``message`` under an optional ``title``."""
        super().__init__()
        self.title = title
        self._message = message
        self._spinner = Spinner()

    def tick(self) -> None:
        """Advance the spinner to its next frame (driven by the session's animation timer)."""
        self._spinner.tick()

    def render_body(self, width: int) -> list[str]:
        """Render the current spinner frame followed by the message."""
        line = self._spinner.text()
        line.append("  ")
        line.append(self._message)
        return render_lines(line, width)

    def handle(self, action: str, data: str = "") -> None:
        """Swallow all keys: the splash dismisses itself when the task finishes."""
        return


class BusyDialog(BusyScreen):
    """A busy splash drawn as a box over the screen it interrupts.

    :class:`BusyScreen`'s twin, for work started *from* a screen rather than instead of one:
    same spinner, same caption, same swallow-everything :meth:`~BusyScreen.handle` — but
    ``floating``, so the hub that started the work stays visible beneath it as a backdrop
    instead of being replaced.

    Modal by inheritance, and that is the half that matters. A hub screen kept on the stack
    with ``session.stay`` is *armed the whole time* (see
    :meth:`~meshterm.ui.tui.session.Visit._arm`), so while it sits there waiting for a slow
    device write every key a reader presses still reaches it: letters land in its live filter
    and Esc resolves it, to be handed back the instant the work finishes. A modal screen on
    top is what stops that — the key reaches this dialog and dies here.

    :meth:`~meshterm.ui.tui.session.TuiSession.busy_overlay` cannot do the job: it is not a
    screen, so it is never consulted for a keypress, and it paints only when the stack is
    empty — over a pushed hub it draws nothing at all.
    """

    floating = True

    @property
    def dialog_width(self) -> int:
        """Hug the caption rather than stretching to the dialog cap.

        A spinner and four words have no business in a 94-cell box. The compositor still
        caps this to the terminal (see :func:`~meshterm.ui.tui.frame._dialog_layout`), and
        the width ratchets, so a caption that grows mid-batch never makes the box shrink
        back and jitter.
        """
        return cell_len(self._message) + 8  # spinner, its two spaces, and the padded border

    @property
    def message(self) -> str:
        """The caption beside the spinner."""
        return self._message

    @message.setter
    def message(self, text: str) -> None:
        """Retitle the card mid-batch (a sequence of writes reporting which one it is on)."""
        self._message = text
