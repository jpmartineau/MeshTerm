# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the reusable text-UI library (``meshterm.ui.tui``).

These tests examine the pure logic. They do not start a real prompt_toolkit application,
so they run fast and without a display. The logic includes:

- The rendering and slicing of ANSI text.
- The filtering of a selection, and the navigation in it.
- The scroll calculation.
- The guarantee that a composed frame fits the terminal.
- The editing and validation of a prompt.
- The parity of the progress handle with the Rich ``Progress``.
"""

from __future__ import annotations

import asyncio

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.cells import cell_len
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from meshterm import copyright_notice
from meshterm.ui.menus import section_heading
from meshterm.ui.pathline import CRACK_TAIL, PathHop, PathLine
from meshterm.ui.tui import frame
from meshterm.ui.tui.progress import ProgressScreen
from meshterm.ui.tui.prompt import (
    AutocompleteScreen,
    ButtonDialog,
    ConfirmScreen,
    TextScreen,
)
from meshterm.ui.tui.render import render_lines, render_to_ansi
from meshterm.ui.tui.screen import CANCEL, Screen, ScrollScreen
from meshterm.ui.tui.select import Choice, ReorderScreen, SelectScreen, Separator
from meshterm.ui.tui.session import TuiSession
from meshterm.ui.tui.spinner import spinner_interval
from tests.conftest import plain as _plain

_UNSET = object()


class _Fut:
    """A minimal stand-in for an asyncio.Future that captures the result of a screen."""

    def __init__(self) -> None:
        self.result = _UNSET
        self._done = False

    def done(self) -> bool:
        return self._done

    def set_result(self, value: object) -> None:
        self.result = value
        self._done = True


def _run(screen, action: str, data: str = ""):
    """Attach a fake future, send one action, and return the result that it captured."""
    screen.future = _Fut()
    screen.handle(action, data)
    return screen.future.result


# --- render ------------------------------------------------------------------


def test_render_lines_counts_visible_rows() -> None:
    """A table of three rows renders to three ANSI lines, with no extra blank line."""
    table = Table(show_header=False, box=None)
    table.add_column("a")
    for value in ("one", "two", "three"):
        table.add_row(value)
    lines = render_lines(table, 40)
    assert len(lines) == 3
    assert not lines[-1].strip().endswith("\n")


def test_render_to_ansi_wraps_to_width() -> None:
    """The render uses the width that the caller asks for, and wraps long text onto more lines."""
    long = Text("word " * 40)  # The text has 200 characters, so it must wrap at width 20.
    lines = render_to_ansi(long, 20).split("\n")
    assert len(lines) > 1


# --- select ------------------------------------------------------------------


def _menu() -> SelectScreen:
    items = [
        Separator("── group ──"),
        Choice("alpha", 1),
        Choice("beta", 2),
        Choice("gamma", 3),
    ]
    return SelectScreen("pick", items, default=2)


def test_select_default_and_arrows_walk_the_choices() -> None:
    """The default choice is selected at the start, and the arrows step over the choices."""
    screen = _menu()
    assert _run(screen, "enter") == 2  # The default is beta.
    screen = _menu()
    screen.handle("down")  # From beta to gamma.
    screen.handle("up")  # From gamma to beta.
    screen.handle("up")  # From beta to alpha.
    assert _run(screen, "enter") == 1


def test_select_filter_narrows_but_keeps_separators() -> None:
    """Typing filters the list to the choices that match, and the section headings stay."""
    screen = _menu()
    screen.handle("text", "a")  # This matches alpha, beta, and gamma. Each one has an 'a'.
    rows = screen._rows()
    assert [r.title for r in rows if isinstance(r, Separator)] == ["── group ──"]
    assert {r.title for r in rows if isinstance(r, Choice)} == {"alpha", "beta", "gamma"}
    screen.handle("text", "l")  # The filter is now 'al', so only alpha matches. The heading stays.
    rows = screen._rows()
    assert [r.title for r in rows if isinstance(r, Choice)] == ["alpha"]
    assert [r.title for r in rows if isinstance(r, Separator)] == ["── group ──"]
    assert _run(screen, "enter") == 1


def test_select_filter_ignores_leading_and_trailing_spaces() -> None:
    """A leading space never starts the filter. The match ignores a trailing space."""
    screen = _menu()
    screen.handle("text", " ")  # The screen ignores it. The filter never starts with a space.
    assert screen._filter == ""
    for ch in "alpha ":  # The word "alpha", then a trailing space.
        screen.handle("text", ch)
    assert screen._filter == "alpha "  # The space stays in the buffer...
    # ...but the match removes it. Thus the trailing space does not stop "alpha" from matching.
    assert [r.title for r in screen._rows() if isinstance(r, Choice)] == ["alpha"]


def test_select_non_filterable_ignores_typing() -> None:
    """If filtering is off, typed keys do not narrow the list and do not add a filter line."""
    screen = SelectScreen("pick", [Choice("alpha", 1), Choice("beta", 2)], filterable=False)
    screen.handle("text", "a")
    screen.handle("backspace")
    assert screen._filter == ""
    assert len(screen._rows()) == 2  # The filter removed no row.


def test_select_delete_hint_follows_the_highlight() -> None:
    """The 'Del remove' atom shows only while the highlight is on a row that the user can delete."""
    screen = SelectScreen(
        "pick",
        [Choice("keep", 1), Choice("drop", 2, deletable=True)],
        footer_hint="↑↓ move · Enter select · Esc quit",
        delete_hint="Del remove",
        filterable=False,
    )
    # On the row that the user cannot delete, the footer is the plain base hint.
    assert "Del remove" not in screen.footer_hint
    screen.handle("down")  # Move to the row that the user can delete.
    # The atom appears. It goes in before the Esc atom at the end, so Esc stays last.
    assert screen.footer_hint == "↑↓ move · Enter select · Del remove · Esc quit"
    screen.handle("up")  # Move back to the plain row.
    assert "Del remove" not in screen.footer_hint
    # The box has the size of the fullest footer, so it does not become wider on the move.
    assert "Del remove" in screen.sizing_footer_hint


def test_select_no_delete_hint_leaves_footer_fixed() -> None:
    """If there is no delete_hint, a row that the user can delete does not change the footer."""
    screen = SelectScreen(
        "pick", [Choice("drop", 1, deletable=True)], footer_hint="↑↓ move · Esc quit"
    )
    assert screen.footer_hint == "↑↓ move · Esc quit"
    assert screen.sizing_footer_hint == "↑↓ move · Esc quit"


def test_select_escape_cancels() -> None:
    """Esc resolves the sentinel and not a value."""
    assert _run(_menu(), "escape") is CANCEL


def test_select_cursor_line_tracks_selection() -> None:
    """The line that the screen reports for the highlight counts the separators and filter line."""
    screen = _menu()  # The default is beta, which is row index 2 (separator, alpha, beta).
    screen.render_body(40)
    assert screen.cursor_line() == 2


def test_select_cursor_line_counts_a_prompt_once() -> None:
    """Under a prompt, the line that the screen reports for the highlight is the line of the row.

    The screen lays out the rows after the lines of the prompt. Thus the index at which it
    records them already counts the prompt. The screen once added the height of the prompt
    again, and it reported a line that was too low. The frame kept that line visible. Then
    ↑ moved the real highlight above the top edge of the list window. This happened in the
    archive preview, where the prompt is over a long list.
    """
    items = [Choice(f"c{i}", i) for i in range(12)]
    screen = SelectScreen("pick", items, prompt="Read the list, then pick one.", default=3)
    lines = screen.render_body(40)
    assert "❯ c3" in Text.from_ansi(lines[screen.cursor_line()]).plain


def test_select_callable_title_re_renders_live() -> None:
    """MeshTerm resolves a callable title on each paint, so a live badge follows the state."""
    unread = {"n": 0}
    screen = SelectScreen("pick", [Choice(lambda: f"chan ● {unread['n']}", 1)])
    assert "chan ● 0" in "\n".join(screen.render_body(40))
    unread["n"] = 3  # A message arrived while the list was open.
    assert "chan ● 3" in "\n".join(screen.render_body(40))


def test_select_width_aware_title_fits_itself_to_the_row() -> None:
    """A title that knows the width fits itself to the row on which MeshTerm draws it.

    A route elides its middle. The natural form of the title is still the text that the
    filter uses, and the text that the dialog measures to find its own width.
    """
    seen: list[int] = []

    def fitted(width: int) -> str:
        seen.append(width)
        return "you → hub → far" if width >= 20 else "you ⋯ far"

    screen = SelectScreen("pick", [Choice(fitted, 1)])
    assert "you ⋯ far" in "\n".join(screen.render_body(12))
    assert seen[-1] == 10  # The content area of the row: the width minus the pointer of 2 cells.
    assert "you → hub → far" in "\n".join(screen.render_body(40))
    # The screen measures the fullest form.
    assert screen.dialog_width > cell_len("you → hub → far")
    screen.handle("text", "hub")  # The filter reads the natural form, which has no width limit.
    assert screen._rows() == screen._items


def test_select_row_cracks_a_chip_path_and_ellipsizes_everything_else() -> None:
    """A row that is too wide for the list is cut at the crack, or ends with an ellipsis.

    A row that has a path line (a trophy walk, a probe candidate) breaks its chip off at
    the crack. An ordinary row of prose keeps the ellipsis. The row decides which one,
    because the list does not know that it holds a route.
    """
    route = PathLine(
        [PathHop(f"NODE{i:02d}", key=f"{i:02x}aa") for i in range(8)], mode="powerline"
    ).text()
    screen = SelectScreen("pick", [Choice(route, 1), Choice("a plainly worded row", 2)])
    rows = [ln for ln in _plain(screen.render_body(16)).split("\n") if ln.strip()]
    route_row = next(ln for ln in rows if "NODE" in ln)
    prose_row = next(ln for ln in rows if "plainly" in ln)
    assert route_row.rstrip().endswith(CRACK_TAIL) and "…" not in route_row
    assert prose_row.rstrip().endswith("…")  # The prose was shortened, and the ellipsis shows it.


def test_select_hscroll_highlight_keeps_the_natural_row() -> None:
    """In an hscroll list, the highlighted row keeps its natural text, which is not fitted.

    ←→ slide the full line in that row. Each other row still elides itself to the width.
    """

    def fitted(width: int) -> str:
        return "start middle end" if width >= 20 else "start ⋯ end"

    items = [Choice(fitted, 1), Choice(fitted, 2)]
    screen = SelectScreen("pick", items, hscroll=True)
    body = "\n".join(screen.render_body(14))
    # The highlighted row has its natural form, and the screen crops it.
    assert "start middl" in body
    assert "start ⋯ end" in body  # The row that is not highlighted fitted itself.


def test_select_hscroll_highlight_draws_a_fitted_row_fitted() -> None:
    """A row whose fitted form is complete (``Choice.fitted``) stays fitted under the highlight.

    The rows of the Channels list shorten their last chart to the width. When the screen
    drew the natural form under the highlight, the chart came back cut off in the row that
    the user was reading. There is nothing past the edge of such a row to slide to. Thus
    the row does not claim to overflow: it has no ←→ atom, and → leaves the row where it is.
    """

    def chart_row(width: int) -> str:
        return "lanes " + "⣀" * max(0, min(12, width - 6))

    screen = SelectScreen("pick", [Choice(chart_row, 1, fitted=True)], hscroll=True)
    lines = [Text.from_ansi(line).plain for line in screen.render_body(14)]
    assert lines[0] == "❯ lanes " + "⣀" * 6  # The row is fitted to the content area of 12 cells.
    assert not screen._selected_overflows()
    screen.handle("right")
    assert [Text.from_ansi(line).plain for line in screen.render_body(14)] == lines


def test_select_filter_matches_callable_title() -> None:
    """The filter that the user types matches the current text of a callable title."""
    screen = SelectScreen("pick", [Choice(lambda: "alpha", 1), Choice("beta", 2)])
    screen.handle("text", "alp")
    assert [r.label for r in screen._rows()] == ["alpha"]
    assert _run(screen, "enter") == 1


# --- edge scroll ---------------------------------------------------------------


def _edge_list() -> SelectScreen:
    """Six lines of lead-in over four choices and a closing note, taller than 4 rows."""
    items: list = [Separator(f"lead {i}") for i in range(6)]
    items += [Choice(name, name) for name in ("alpha", "beta", "gamma", "delta")]
    items.append(Separator("closing note"))
    return SelectScreen("pick", items)


def _press(screen, action: str, data: str = "") -> None:
    """Send one key as the session does: the edge scroll first, then the screen."""
    if not screen.edge_scroll(action):
        screen.handle(action, data)


def _view(screen, viewport: int = 4) -> list[str]:
    """Paint one time through the slicer of the frame, and return the text of the visible rows."""
    visible, _above, _below = frame._visible_slice(screen, screen.render_body(40), viewport)
    return [_plain([line]).strip() for line in visible]


def test_edge_scroll_brings_back_what_sits_above_the_first_row() -> None:
    """↑ on the first row scrolls the page one line, up to its very top.

    When the highlight followed the user down, it scrolled the lead-in off. Nothing in
    the lead-in can take the highlight. Thus, before the edge scroll, the user had no way
    back to it. The highlight can scroll off the screen in the meantime. The snap-back
    brings it back, and it does not move it.
    """
    screen = _edge_list()
    assert _view(screen) == ["lead 3", "lead 4", "lead 5", "❯ alpha"]
    _press(screen, "up")  # Alpha is the first row, so the page scrolls one line.
    assert _view(screen)[0] == "lead 2"
    for _ in range(5):
        _press(screen, "up")
    view = _view(screen)
    # This is the top. Alpha is off the screen.
    assert view == ["lead 0", "lead 1", "lead 2", "lead 3"]
    _press(screen, "up")
    assert _view(screen) == view  # Nothing is further up, so the view stays.
    # The snap-back: ↓ only brings alpha back, and it stays highlighted. The next ↓ moves it.
    _press(screen, "down")
    assert "❯ alpha" in _view(screen)
    _press(screen, "down")
    assert "❯ beta" in _view(screen)


def test_edge_scroll_brings_back_what_sits_below_the_last_row() -> None:
    """↓ on the last row scrolls the closing note into view. ↑ moves the highlight as before."""
    screen = _edge_list()
    _view(screen)
    for _ in range(3):
        _press(screen, "down")
    assert _view(screen) == ["alpha", "beta", "gamma", "❯ delta"]  # The note is below.
    _press(screen, "down")  # Delta is the last row.
    assert _view(screen) == ["beta", "gamma", "❯ delta", "closing note"]
    _press(screen, "up")  # The highlight is visible, so ↑ moves it.
    assert "❯ gamma" in _view(screen)


def test_enter_on_a_highlight_scrolled_off_brings_it_back_instead() -> None:
    """Nothing runs that the user cannot see. The first Enter only snaps the highlight back."""
    screen = _edge_list()
    screen.future = _Fut()
    _view(screen)
    for _ in range(3):
        _press(screen, "up")
    assert "❯ alpha" not in _view(screen)
    _press(screen, "enter")
    assert screen.future.result is _UNSET  # The user selected nothing...
    assert "❯ alpha" in _view(screen)  # ...and the highlight came back.
    _press(screen, "enter")
    assert screen.future.result == "alpha"


def test_home_and_end_take_the_page_to_its_very_ends() -> None:
    """End shows the closing note under the last row. Home shows the lead-in over the first row."""
    screen = _edge_list()
    _view(screen)
    _press(screen, "end")
    assert _view(screen) == ["beta", "gamma", "❯ delta", "closing note"]
    _press(screen, "home")
    assert _view(screen) == ["lead 0", "lead 1", "lead 2", "lead 3"]
    _press(screen, "down")  # The snap-back: alpha, where Home left the highlight, comes into view.
    assert "❯ alpha" in _view(screen)


def test_any_other_key_ends_the_edge_scroll() -> None:
    """A filter that the user types moves the highlight, so the frame follows it again at once."""
    screen = _edge_list()
    _view(screen)
    for _ in range(3):
        _press(screen, "up")
    _view(screen)
    assert screen.edge_scrolled
    _press(screen, "text", "g")
    assert not screen.edge_scrolled
    assert "❯ gamma" in _view(screen)


def test_a_screen_without_a_highlight_keeps_its_own_arrows() -> None:
    """The edge scroll does not act on a plain scroll screen, because the screen keeps its ↑↓."""
    screen = ScrollScreen(Text("\n".join(f"row {i}" for i in range(20))), title="t")
    for action in ("up", "down", "home", "end", "enter"):
        assert not screen.edge_scroll(action)


class _Rows(Screen):
    """A screen that has no edge-scroll code: a lead-in, a highlight over rows, and a note.

    Its ↑↓ clamp, as the arrows of each highlight do. Its Home and End move nothing. It
    names the line of its highlight in ``cursor_line``, and a screen supplies only this.
    ``window`` is the number of rows that the screen draws at one time. The rows scroll
    with the highlight, as a ListWindow does.
    """

    def __init__(self, count: int = 3, *, window: int | None = None) -> None:
        super().__init__()
        self._count = count
        self._window = window or count
        self._index = 0
        self._top = 0
        self._cursor: int | None = None

    def render_body(self, width: int) -> list[str]:
        self._top = max(min(self._top, self._index), self._index - self._window + 1)
        lines = [f"lead {i}" for i in range(4)]
        for i in range(self._top, self._top + self._window):
            if i == self._index:
                self._cursor = len(lines)
            lines.append(("❯ " if i == self._index else "  ") + f"row {i}")
        lines.append("note")
        return lines

    def cursor_line(self) -> int | None:
        return self._cursor

    def handle(self, action: str, data: str = "") -> None:
        if action == "up":
            self._index = max(0, self._index - 1)
        elif action == "down":
            self._index = min(self._count - 1, self._index + 1)


def test_every_screen_with_a_highlight_edge_scrolls() -> None:
    """A screen inherits the edge scroll: a screen that only names the line of its highlight has it.

    _Rows does not say where its edges are. The paint reads them from the page.
    """
    screen = _Rows()
    assert _view(screen) == ["lead 1", "lead 2", "lead 3", "❯ row 0"]
    _press(screen, "up")  # Row 0 is the first row, so the page takes the line.
    assert _view(screen) == ["lead 0", "lead 1", "lead 2", "lead 3"]
    _press(screen, "down")  # The snap-back.
    assert "❯ row 0" in _view(screen)
    for _ in range(2):
        _press(screen, "down")
        _view(screen)
    _press(screen, "down")  # Row 2 is the last row, so the note comes into view.
    assert _view(screen) == ["row 0", "row 1", "❯ row 2", "note"]


def test_several_arrows_before_a_paint_scroll_as_many_lines() -> None:
    """Arrows that come faster than the paint are judged together.

    Three arrows at the edge scroll three lines.
    """
    screen = _Rows()
    _press(screen, "up")  # This is before any paint, so there is nothing on the page to judge.
    _view(screen)
    for _ in range(2):
        _press(screen, "down")
        _view(screen)
    for _ in range(3):
        _press(screen, "down")
    view = _view(screen)
    assert view == ["row 0", "row 1", "❯ row 2", "note"]  # The view clamps at the end of the page.


def test_a_list_scrolling_in_its_own_window_is_not_at_its_edge() -> None:
    """A highlight that keeps its line while rows pass under it has moved: no edge scroll.

    When the user walks ↓ in a list that has a window, the highlight stays on the last line
    of the window. Thus the index of the line alone does not show if the highlight moved.
    The line as drawn does show it.
    """
    screen = _Rows(6, window=2)
    _view(screen)
    _press(screen, "down")
    _view(screen)
    before = screen.scroll
    _press(screen, "down")  # Row 2 is on the same body line as row 1, but the text is different.
    assert "❯ row 2" in _view(screen)
    assert not screen.edge_scrolled and screen.scroll == before
    for _ in range(3):
        _press(screen, "down")
        _view(screen)
    _press(screen, "down")  # Row 5 is the last row.
    assert _view(screen)[-1] == "note" and screen.edge_scrolled


def test_a_screen_can_keep_its_arrows_from_edge_scroll() -> None:
    """If ``edge_scrolls`` is off, the screen never edge scrolls.

    The remote CLI uses ↑↓ to recall history.
    """
    screen = _Rows()
    screen.edge_scrolls = False
    view = _view(screen)
    _press(screen, "up")
    assert _view(screen) == view and not screen.edge_scrolled


def test_home_that_moves_no_highlight_leaves_the_arrows_to_it() -> None:
    """Home takes the page to its top. Only the page says where the highlight went.

    The Home of _Rows moves nothing, so ↑ after Home is still a step up the rows. The
    arrow that points away from a highlight that is off the screen goes to the screen.
    Then the highlight comes back into the viewport.
    """
    screen = _Rows()
    _view(screen)
    for _ in range(2):
        _press(screen, "down")
        _view(screen)
    _press(screen, "home")
    assert _view(screen)[0] == "lead 0"
    _press(screen, "up")
    assert "❯ row 1" in _view(screen)


def test_a_list_with_no_rows_still_edge_scrolls() -> None:
    """If there is nothing to highlight, the empty state takes the place of the highlight."""
    screen = _edge_list()
    _view(screen)
    _press(screen, "text", "z")
    _press(screen, "text", "z")
    assert _view(screen)[-1] == "no matches"
    _press(screen, "up")
    view = _view(screen)
    assert "no matches" not in view and view[-1] == "closing note"


def test_reorder_home_and_end_reach_the_ends() -> None:
    """Home and End also move the highlight of the reorder list to its ends, as on each list."""
    screen = ReorderScreen("order", ["a", "b", "c"])
    screen.handle("end")
    assert any(line.startswith("❯ c") for line in _view(screen))
    screen.handle("home")
    assert any(line.startswith("❯ a") for line in _view(screen))


def test_autocomplete_keeps_its_highlighted_suggestion_in_view() -> None:
    """A box that is too short for each suggestion follows the highlight, and edge scrolls."""
    screen = AutocompleteScreen("t", [f"opt{i}" for i in range(8)], prompt="pick one")
    _view(screen, 5)
    for _ in range(7):
        _press(screen, "down")
        _view(screen, 5)
    assert _view(screen, 5)[-1] == "❯ opt7"
    for _ in range(7):
        _press(screen, "up")
        _view(screen, 5)
    assert _view(screen, 5)[0] == "❯ opt0"
    _press(screen, "up")  # This is the first suggestion, so the field comes back above it.
    assert _view(screen, 5)[1] == "❯ opt0"


def test_select_clamps_at_the_ends() -> None:
    """Up on the first row and Down on the last row stay where they are.

    No highlight in the app rolls over.
    """
    items = [Choice("alpha", 1), Choice("beta", 2), Choice("gamma", 3)]
    screen = SelectScreen("pick", items)
    screen.handle("up")  # The highlight is on the first choice. It must not jump to the last.
    assert _run(screen, "enter") == 1
    screen = SelectScreen("pick", items, default=3)  # The last choice.
    screen.handle("down")  # The highlight is on the last choice. It must not wrap to the first.
    assert _run(screen, "enter") == 3


def test_select_pageup_pagedown_jump_by_a_screenful() -> None:
    """PageDown and PageUp move the highlight one screenful at a time. They clamp to the choices."""
    items = [Choice(f"c{i}", i) for i in range(30)]
    screen = SelectScreen("pick", items)  # The highlight starts on the first choice.
    screen.note_metrics(total=30, viewport=11)  # A screenful is viewport - 1 = 10 rows.
    screen.handle("pagedown")
    assert _run(screen, "enter") == 10  # The highlight moved one page (viewport - 1) down.
    screen = SelectScreen("pick", items, default=25)
    screen.note_metrics(total=30, viewport=11)
    screen.handle("pageup")
    assert _run(screen, "enter") == 25 - 10  # Then it moved one page back up.


def _grouped_menu(default: object = None) -> SelectScreen:
    """A menu of two sections. Each section is long enough to scroll past a small viewport."""
    items: list = [section_heading("Channels")]
    items += [Choice(f"chan{i}", ("c", i)) for i in range(6)]
    items += [section_heading("Direct")]
    items += [Choice(f"peer{i}", ("d", i)) for i in range(8)]
    return SelectScreen("pick", items, default=default)


def _top_plain(screen: SelectScreen, viewport: int) -> str:
    """Slice the screen at the scroll that its selection sets. Return the top row as plain text."""
    lines = screen.render_body(40)
    visible, _above, _below = frame._visible_slice(screen, lines, viewport)
    return Text.from_ansi(visible[0]).plain.strip()


def test_select_pins_section_heading_when_it_scrolls_off() -> None:
    """If the user selects a row deep in a section, the heading of the section stays on top."""
    # A highlight on a channel that is far enough down pushes the "Channels" heading off the
    # top. The screen pins the heading again, and it does not vanish.
    assert _top_plain(_grouped_menu(default=("c", 5)), viewport=6) == "── Channels ──"
    # Deep in the Direct group, the pinned heading changes to the heading of that section.
    assert _top_plain(_grouped_menu(default=("d", 6)), viewport=6) == "── Direct ──"


def test_select_does_not_pin_a_heading_that_is_still_visible() -> None:
    """If the list is at the top, the real heading shows, and nothing is pinned over it."""
    screen = _grouped_menu()  # The default is the first choice, so the scroll stays at 0.
    lines = screen.render_body(40)
    visible, above, _below = frame._visible_slice(screen, lines, 6)
    assert Text.from_ansi(visible[0]).plain.strip() == "── Channels ──"
    # This is the top of the list. There is no pinned copy and no "more above".
    assert above is False


def _trophy_shaped() -> SelectScreen:
    """The shape of the Trophy case: a heading, its description, then the rows of that board."""
    items: list = [section_heading("Longest haul")]
    items += [Separator(f"   description line {i}", style="muted") for i in range(2)]
    items += [Choice(f"rec{i}", ("l", i)) for i in range(6)]
    items += [section_heading("Widest arc"), Separator("   no records yet", style="muted")]
    items += [Choice(f"arc{i}", ("a", i)) for i in range(6)]
    return SelectScreen("Trophy case", items, default=("l", 5))


def _blocks(screen: SelectScreen) -> list[list[str]]:
    """The rows of each sticky block that the screen recorded, as plain text."""
    return [
        [Text.from_ansi(line).plain.strip() for line in rows]
        for _idx, rows in screen._sticky_headers
    ]


def test_select_blocks_a_heading_with_the_prose_written_under_it() -> None:
    """The landmark of a heading continues through the separators that follow it at once.

    This is the shape of the Trophy case. The ``── heading ──`` of each discipline has a
    wrapped description after it. The description explains the rows below it, so it belongs
    overhead with the heading. Prose that follows a row (a stray note, or the blank line
    of the exit group) labels nothing, and it is not a landmark.
    """
    screen = _trophy_shaped()
    screen.render_body(40)
    assert _blocks(screen) == [
        ["── Longest haul ──", "description line 0", "description line 1"],
        ["── Widest arc ──", "no records yet"],
    ]


def test_select_pins_a_block_row_only_once_it_has_scrolled_off() -> None:
    """A block gives its rows one at a time, so the pins continue into the body.

    While the description is the top content row, only the heading pins over it. When
    both have scrolled off, the two pin together. The prose never pins alone, because the
    row that says which section this is always leads the rows overhead.
    """
    screen = _trophy_shaped()
    screen.render_body(40)
    screen.note_metrics(total=20, viewport=12)
    plain = lambda scroll: [  # noqa: E731 - a one-line reader for the assertions below
        Text.from_ansi(line).plain.strip() for line in screen.sticky_rows(scroll)
    ]
    assert plain(0) == []  # The heading is the top row, so there is nothing to copy.
    assert plain(1) == ["── Longest haul ──"]  # Its description is still on the screen.
    assert plain(2) == ["── Longest haul ──", "description line 0"]
    assert plain(4) == [
        "── Longest haul ──",
        "description line 0",
        "description line 1",
    ]
    # A block never takes more than half the viewport. The rows go from the end, so a short
    # terminal gives up the heading last.
    screen.note_metrics(total=20, viewport=4)
    assert plain(4) == ["── Longest haul ──", "description line 0"]
    screen.note_metrics(total=20, viewport=2)
    assert plain(4) == ["── Longest haul ──"]


def test_select_pins_the_heading_not_the_prose_beneath_it() -> None:
    """The pinned rows always start with the heading, never with the last muted line under it."""
    screen = _trophy_shaped()
    assert _top_plain(screen, viewport=6) == "── Longest haul ──"
    # Also, the note for an empty state cannot take the place of its heading.
    assert _top_plain(_reselect(screen, ("a", 4)), viewport=6) == "── Widest arc ──"


def _reselect(screen: SelectScreen, value: object) -> SelectScreen:
    """Move the highlight of a select screen to ``value``. The helper walks Down to it."""
    while screen._choices()[screen._index].value != value:
        screen.handle("down")
    return screen


def test_select_pinned_heading_keeps_the_last_row_reachable() -> None:
    """Also with a pinned heading, the last choice stays fully visible and is not clipped."""
    screen = _grouped_menu()
    screen.handle("end")  # Highlight the last choice.
    lines = screen.render_body(40)
    visible, _above, below = frame._visible_slice(screen, lines, 6)
    assert Text.from_ansi(visible[0]).plain.strip() == "── Direct ──"  # The heading is pinned.
    assert any("peer7" in Text.from_ansi(row).plain for row in visible)  # The last row shows.
    assert below is False  # Also, the user can see that this is the bottom.


def _columned_menu(default: object = None) -> SelectScreen:
    """A grouped menu that starts with a pinned column header, as the config editor does."""
    items: list = [Separator("  SETTING          VALUE", pinned=True)]
    items += [section_heading("Channels")]
    items += [Choice(f"chan{i}", ("c", i)) for i in range(6)]
    items += [section_heading("Direct")]
    items += [Choice(f"peer{i}", ("d", i)) for i in range(8)]
    return SelectScreen("pick", items, default=default)


def test_select_pins_a_column_header_above_the_section_heading() -> None:
    """A pinned column header stays for the whole list, and the heading that governs is under it."""
    screen = _columned_menu(default=("d", 6))  # This is deep in the second section.
    lines = screen.render_body(40)
    visible, above, _below = frame._visible_slice(screen, lines, 7)
    assert [Text.from_ansi(row).plain.strip() for row in visible[:2]] == [
        "SETTING          VALUE",  # The lanes, which are pinned for each section.
        "── Direct ──",  # This is over the section that has the highlight.
    ]
    assert above is True
    assert any("peer6" in Text.from_ansi(row).plain for row in visible)  # The highlight shows.
    # The pinned header is not a section landmark. Only the two headings are landmarks.
    assert _blocks(screen) == [["── Channels ──"], ["── Direct ──"]]


def test_select_column_header_shows_itself_at_the_top_and_pins_alone() -> None:
    """At the top, the header only draws. Past the top, it pins before any heading scrolls off."""
    screen = _columned_menu()  # The highlight is on the first choice, so the list is at the top.
    lines = screen.render_body(40)
    visible, above, _below = frame._visible_slice(screen, lines, 8)
    assert Text.from_ansi(visible[0]).plain.strip() == "SETTING          VALUE"
    assert above is False  # Nothing is pinned over the real row, and nothing is above it.
    # After a scroll of one row, the header pins while the heading of its own section is
    # still the top content row. Thus the header is the only pin.
    assert screen.sticky_rows(1) == [screen._pinned_header[1]]


def test_select_pinned_column_header_keeps_the_last_row_reachable() -> None:
    """Two pinned rows still leave the last choice fully visible, with no clip."""
    screen = _columned_menu()
    screen.handle("end")  # Highlight the last choice.
    lines = screen.render_body(40)
    visible, _above, below = frame._visible_slice(screen, lines, 7)
    assert Text.from_ansi(visible[0]).plain.strip() == "SETTING          VALUE"
    assert any("peer7" in Text.from_ansi(row).plain for row in visible)
    assert below is False


def test_select_walking_up_from_the_bottom_keeps_the_highlight_in_view() -> None:
    """↑ from the last row to the first never leaves the highlight outside the window.

    At the bottom, the window slides down under its pinned rows. Thus the pinned rows use
    old lines at the top, and not the last lines. When the user walks back up, the
    highlight reaches the top line of the scroll window before the scroll must move. A
    slide past that line hid the row that the user had just moved to. It hid the row for
    one step with a pinned column header, and for two steps with a section heading under it.
    """
    for viewport in (5, 6, 7, 8):
        screen = _columned_menu()
        screen.handle("end")
        for _ in range(len(screen._choices())):
            visible, _above, _below = frame._visible_slice(screen, screen.render_body(40), viewport)
            rows = [Text.from_ansi(row).plain for row in visible]
            current = screen._current_choice().label
            assert any(f"❯ {current}" in row for row in rows), (viewport, current, rows)
            screen.handle("up")


def test_select_resolves_a_width_aware_separator_at_the_render_width() -> None:
    """A callable separator title gets the render width, so a header can fit itself."""
    screen = SelectScreen(
        "pick", [Separator(lambda w: f"HEADER@{w}", pinned=True), Choice("row", 1)]
    )
    assert Text.from_ansi(screen.render_body(30)[0]).plain.strip() == "HEADER@30"
    assert Text.from_ansi(screen.render_body(48)[0]).plain.strip() == "HEADER@48"
    # The measure of the natural width asks for the fullest form, not a form that has the
    # size of the terminal. Thus the box has the size of the whole header, and only the
    # terminal can force the header to abbreviate.
    natural = SelectScreen(
        "pick",
        [Separator(lambda w: "H" * min(w, 120), pinned=True), Choice("row", 1)],
        footer_hint="Esc back",
    )
    assert natural.dialog_width >= 120


def test_select_pinned_header_crops_where_a_plain_separator_wraps() -> None:
    """A pinned row must stay one row. If it is too wide, it ends with an ellipsis, not a wrap."""
    wide = "SETTING" + " " * 40 + "DESCRIPTION"
    screen = SelectScreen("pick", [Separator(wide, pinned=True), Choice("row", 1)])
    lines = screen.render_body(24)
    assert screen._pinned_header == (0, lines[0])
    assert Text.from_ansi(lines[0]).plain.rstrip().endswith("…")
    assert len(lines) == 2  # The header and the one choice. Nothing wrapped onto a new row.
    # An ordinary separator still wraps. Each row of it is a body line.
    plain = SelectScreen("pick", [Separator(wide), Choice("row", 1)])
    assert len(plain.render_body(24)) == 3


def test_screen_sticky_rows_stack_the_pinned_header_over_the_section_heading() -> None:
    """The shared rule that composes both pins: the header of the whole list, then the section."""
    screen = Screen()
    screen.note_metrics(total=40, viewport=12)
    screen._pinned_header = (0, "COLUMNS")
    screen._sticky_headers = [(1, ["A"]), (5, ["B"])]
    assert screen.sticky_rows(0) == []  # Nothing has scrolled off.
    assert screen.sticky_rows(1) == ["COLUMNS"]  # Heading A is the top row.
    assert screen.sticky_rows(3) == ["COLUMNS", "A"]  # This is inside section A.
    assert screen.sticky_rows(9) == ["COLUMNS", "B"]  # Below each heading, the last one pins.
    assert Screen().sticky_rows(9) == []  # The screen recorded nothing, so nothing is pinned.


def test_screen_sticky_block_picks_the_governing_recorded_block() -> None:
    """The logic of the base Screen.sticky_block works for any list of recorded landmarks.

    The select list and the chat transcript both use it. They fill ``_sticky_headers``.
    This test examines the shared rule directly. The rule takes the last block that starts
    at or above the offset. It pins exactly the rows of that block that the offset passed.
    """
    screen = Screen()
    screen.note_metrics(total=40, viewport=12)
    screen._sticky_headers = [(0, ["A"]), (5, ["B", "b"]), (12, ["C"])]
    assert screen.sticky_block(0) == []  # Block A is the top row.
    assert screen.sticky_block(3) == ["A"]  # The scroll is past A and before B, so A governs.
    assert screen.sticky_block(5) == []  # The heading of block B is now the top row.
    assert screen.sticky_block(6) == ["B"]  # Its second row is still on the screen.
    assert screen.sticky_block(7) == ["B", "b"]  # Both rows are gone, so both pin.
    assert screen.sticky_block(20) == ["C"]  # Below each block, the last block pins.
    assert Screen().sticky_block(9) == []  # There are no recorded landmarks, so nothing pins.


def test_select_ctrl_page_jumps_between_sections() -> None:
    """Ctrl+PageDown goes to the first choice of the next section. Ctrl+PageUp goes back up."""
    screen = _grouped_menu()  # Channels (6) then Direct (8). The highlight is on the first choice.
    screen.handle("ctrl_pagedown")
    assert _run(screen, "enter") == ("d", 0)  # The highlight jumped to the first Direct choice.
    screen.handle("ctrl_pageup")
    # The highlight was at the top of Direct, so it went back to the first choice of Channels.
    assert _run(screen, "enter") == ("c", 0)
    screen.handle("ctrl_pagedown")
    assert _run(screen, "enter") == ("d", 0)  # Then it went forward to Direct again.


# --- scroll ------------------------------------------------------------------


def test_screen_scroll_helpers_page_and_clamp_to_metrics() -> None:
    """The shared scroll helpers move one screenful for a page, and clamp to the body."""
    screen = Screen()
    screen.note_metrics(total=100, viewport=10)
    screen.scroll_pages(1)
    assert screen.scroll == 9  # A page is viewport - 1.
    screen.scroll_to_bottom()
    assert screen.scroll == 90  # This is total - viewport.
    screen.scroll_lines(50)
    assert screen.scroll == 90  # The scroll clamps and never goes past the bottom.
    screen.scroll_to_top()
    assert screen.scroll == 0


def test_screen_section_scroll_walks_recorded_headers() -> None:
    """Ctrl+PageUp and Ctrl+PageDown move the scroll offset between the recorded section limits."""
    screen = Screen()
    screen.note_metrics(total=100, viewport=10)
    screen._sticky_headers = [(0, ["A"]), (20, ["B"]), (60, ["C"])]
    screen.scroll_to_next_section()
    assert screen.scroll == 20  # From the top to the start of section B.
    screen.scroll_to_next_section()
    assert screen.scroll == 60  # To the start of C.
    screen.scroll_to_next_section()
    assert screen.scroll == 90  # There is no section after C, so the scroll clamps to the bottom.
    screen.scroll = 40  # This is in the middle of section B.
    screen.scroll_to_section_start()
    assert screen.scroll == 20  # Up to the start of B.
    screen.scroll_to_section_start()
    assert screen.scroll == 0  # It was at the top of B, so it goes to the section before (A).


def test_scroll_screen_ctrl_edges_and_sectionless_fallback() -> None:
    """Ctrl+Home and Ctrl+End reach the edges. With no sections, Ctrl+PageUp/PageDown do too."""
    body = Text("\n".join(f"line {i}" for i in range(100)))
    screen = ScrollScreen(body, title="log")
    screen.render_body(40)  # This sets total = 100.
    screen.note_viewport(10)
    screen.handle("ctrl_end")
    assert screen.scroll == 90
    screen.handle("ctrl_home")
    assert screen.scroll == 0
    screen.handle("ctrl_pagedown")  # The screen has no sections, so it goes to the bottom.
    assert screen.scroll == 90
    screen.handle("ctrl_pageup")
    assert screen.scroll == 0


# --- scroll (existing) -------------------------------------------------------


def test_scroll_screen_paging_and_clamp() -> None:
    """PageDown moves one page and End jumps to the bottom. Both clamp to the content."""
    body = Text("\n".join(f"line {i}" for i in range(100)))
    screen = ScrollScreen(body, title="log")
    screen.render_body(40)  # This sets total = 100.
    screen.note_viewport(10)
    screen.handle("pagedown")
    assert screen.scroll == 9  # A page is viewport - 1.
    screen.handle("end")
    assert screen.scroll == 90  # This is total - viewport.
    screen.handle("home")
    assert screen.scroll == 0
    screen.handle("up")
    assert screen.scroll == 0  # The scroll clamps and is never negative.


def test_scroll_screen_escape_resolves_none() -> None:
    """A result dialog closes with ``None`` when the user presses Esc or Enter."""
    assert _run(ScrollScreen(Text("x")), "escape") is None
    assert _run(ScrollScreen(Text("x")), "enter") is None


# --- reorder -----------------------------------------------------------------


def test_reorder_apply_row_commits_new_order() -> None:
    """Enter grabs and drops a list row. Enter on Apply commits the new order."""
    screen = ReorderScreen("order", ["a", "b", "c"])
    screen.handle("enter")  # Grab "a".
    assert screen._grabbed and "Enter drop" in screen.footer_hint
    screen.handle("down")  # Carry it past "b".
    screen.handle("enter")  # Drop it.
    assert not screen._grabbed and "Enter grab" in screen.footer_hint
    screen.handle("down")  # The highlight moves from position 1 past "c"...
    screen.handle("down")  # ...onto the Apply row.
    assert _run(screen, "enter") == [1, 0, 2]


def test_reorder_actions_follow_the_dirty_state() -> None:
    """An order that the user did not change has no action row. A change adds Apply and Back.

    Esc leaves in each case. Thus a list that the user did not change shows no row for it.
    The pair appears only when there is something to apply, and Apply has no key of its own.
    """
    screen = ReorderScreen("order", ["a", "b", "c"])
    assert screen._actions() == []
    screen.handle("enter")
    screen.handle("down")  # The order is now changed.
    assert [key for key, _ in screen._actions()] == ["apply", "back"]
    screen.handle("up")  # The row is back at its place, so the order is clean again.
    assert screen._actions() == []


def test_reorder_back_row_and_escape_cancel_discarding_moves() -> None:
    """Enter on Back resolves the sentinel, as Esc does, so the caller keeps the old order."""
    screen = ReorderScreen("order", ["a", "b", "c"])
    screen.handle("enter")
    screen.handle("down")
    assert _run(screen, "escape") is CANCEL

    screen = ReorderScreen("order", ["a", "b", "c"])
    screen.handle("enter")
    screen.handle("down")
    screen.handle("enter")  # Drop it at position 1. The order is now changed.
    for _ in range(3):  # The highlight goes from 1 to 2, then to Apply, then to Back.
        screen.handle("down")
    assert _run(screen, "enter") is CANCEL


def test_reorder_cursor_runs_into_the_action_rows_and_clamps() -> None:
    """↑ on the first row stays. ↓ walks the list and then the action rows, and then stops.

    If the order is clean, the list is the whole space of the highlight. If the order is
    changed, the two action rows join the space. The highlight goes through them to the
    bottom, and it never goes round to the top.
    """
    screen = ReorderScreen("order", ["a", "b"])
    screen.handle("up")  # The highlight is at the top, and there is no last row to go to.
    assert screen._index == 0
    screen.handle("down")  # It goes to the last list row, because there are no action rows.
    assert screen._index == 1
    screen.handle("down")  # It stops there.
    assert screen._index == 1

    screen.handle("enter")  # Grab row 1...
    # ...and carry it up. The order is changed, so Apply and Back join the space.
    screen.handle("up")
    screen.handle("enter")  # Drop it. The highlight moved with it to the first list row.
    screen.handle("down")  # To the second list row.
    screen.handle("down")  # Off the list, onto Apply.
    screen.handle("down")  # ...then to the Back row that discards the changes, below it.
    assert screen._index == 3
    screen.handle("down")  # This is the bottom of the space, so the highlight stays.
    assert screen._index == 3


def test_reorder_ignores_typed_characters_including_space() -> None:
    """Typed characters, also the space bar, do not grab a row and do not resolve the screen."""
    screen = ReorderScreen("order", ["a", "b"])
    screen.future = _Fut()
    screen.handle("text", "x")
    screen.handle("text", " ")  # Space does not grab now. Enter is the key that grabs.
    screen.handle("space")
    assert not screen._grabbed
    assert not screen.future.done()


def test_reorder_dialog_width_is_stable_across_states() -> None:
    """The reorder dialog has one width in each state that it can have.

    The natural width fits the widest of the rows, the hints, and the actions for a changed
    order. It does not change when the user grabs a row or changes the order. Thus the
    dialog does not change its size.
    """
    screen = ReorderScreen("order", ["🔒 alpha", "＃ b"])
    w = screen.dialog_width
    screen.handle("enter")  # Grab.
    assert screen.dialog_width == w
    screen.handle("down")  # The order is changed, so the Apply and discard rows appear.
    assert screen.dialog_width == w


# --- frame -------------------------------------------------------------------


def test_compose_base_fills_exactly_terminal_height() -> None:
    """The composed base frame has exactly ``rows`` lines, for any size of the content."""
    tall = Text("\n".join(f"row {i}" for i in range(200)))
    screen = ScrollScreen(tall, title="big")
    for rows in (10, 24, 50):
        out = frame.compose_base(Text("header"), screen, "Esc back", 80, rows)
        assert out.count("\n") + 1 == rows


def test_a_flush_screen_draws_up_to_the_side_borders() -> None:
    """A ``flush`` base gets the two padding columns back. An ordinary base keeps its padding."""

    class Fill(Screen):
        floating = False

        def render_body(self, width: int) -> list[str]:
            return ["#" * width] * 3

    for flush, row in ((True, "│" + "#" * 78 + "│"), (False, "│ " + "#" * 76 + " │")):
        screen = Fill()
        screen.flush = flush
        lines = [_plain(ln) for ln in frame.compose_base(Text("h"), screen, "", 80, 12).split("\n")]
        assert row in lines, (flush, lines)


def test_compose_dialog_is_bounded() -> None:
    """A dialog for a very large renderable never goes over the terminal height."""
    tall = Text("\n".join(f"row {i}" for i in range(200)))
    out = frame.compose_dialog(ScrollScreen(tall, title="d"), 80, 20)
    assert out.count("\n") + 1 <= 20


def _box_height(screen: Screen) -> int:
    """The height in rows of the dialog box that ``compose_dialog`` draws for ``screen``."""
    return frame.compose_dialog(screen, 80, 40).count("\n") + 1


def test_ordinary_dialog_box_resizes_to_each_body() -> None:
    """An ordinary dialog (not grow-only) has the size of the body that it shows now."""
    short = _box_height(ScrollScreen(Text("one line"), title="d"))
    tall = _box_height(ScrollScreen(Text("\n".join(f"row {i}" for i in range(15))), title="d"))
    assert tall > short


class _GrowScreen(Screen):
    """A grow-only dialog whose body height is set for each paint, for the ratchet test."""

    grow_only = True

    def __init__(self) -> None:
        super().__init__()
        self.title = "d"
        self.footer_hint = ""
        self.n = 1

    def render_body(self, width: int) -> list[str]:
        return [f"row {i}" for i in range(self.n)]


def test_grow_only_dialog_box_holds_its_tallest_size() -> None:
    """A grow-only dialog makes its box larger for a taller body, and never makes it smaller."""
    screen = _GrowScreen()
    screen.n = 2
    small = _box_height(screen)
    screen.n = 16
    grown = _box_height(screen)
    assert grown > small  # A taller body makes the box larger.
    screen.n = 2
    assert _box_height(screen) == grown  # A shorter body after that keeps the larger box.


def _box_width(screen: Screen) -> int:
    """The width in cells of the dialog box that ``compose_dialog`` draws for ``screen``."""
    out = Text.from_ansi(frame.compose_dialog(screen, 80, 40)).plain
    return max(cell_len(line.rstrip()) for line in out.split("\n"))


def test_grow_only_dialog_box_holds_its_widest_size() -> None:
    """The natural width of a grow-only dialog also ratchets. It becomes wider, never narrower."""
    screen = _GrowScreen()
    screen.dialog_width = 30
    narrow = _box_width(screen)
    screen.dialog_width = 60
    wide = _box_width(screen)
    assert wide > narrow  # A wider body makes the box larger.
    screen.dialog_width = 30
    assert _box_width(screen) == wide  # A narrower body after that keeps the wider box.
    # An ordinary dialog keeps the size of each width that it gets.
    plain = ScrollScreen(Text("x"), title="d")
    plain.dialog_width = 60
    wide = _box_width(plain)
    plain.dialog_width = 30
    assert _box_width(plain) < wide


def test_compose_startup_is_chromeless_and_shows_banner() -> None:
    """The startup splash fills the height, draws the banner, and has no header or footer bar."""
    screen = SelectScreen("pick", [Choice("alpha", 1), Choice("beta", 2)])
    screen.chrome = False
    screen.banner = ["LOGO-ROW-A", "LOGO-ROW-B"]
    out = frame.compose_startup(screen, 80, 24)
    assert out.count("\n") + 1 == 24  # The splash fills the terminal height exactly.
    plain = Text.from_ansi(out).plain
    assert "LOGO-ROW-A" in plain and "LOGO-ROW-B" in plain  # The banner is drawn.
    assert "pick" in plain  # The box keeps its title.
    # The box has the size of its content and not the full width. No line that MeshTerm
    # renders spans the whole terminal.
    assert all(len(line.rstrip()) < 80 for line in plain.split("\n"))


def test_compose_bare_is_the_body_alone_on_blank_rows() -> None:
    """A bare frame has no header, footer, title, or box. It has the body, a little above centre."""
    body = Group(Text("CODE", justify="center"), Text("https://x", justify="center"))
    screen = ScrollScreen(body, title="Share x", floating=False)
    screen.bare = True
    out = frame.compose_bare(screen, 53, 26)
    rows = out.split("\n")
    assert len(rows) == 26  # The frame fills the terminal height exactly.
    plain = [Text.from_ansi(row).plain for row in rows]
    assert not any("Share x" in row or "Esc" in row for row in plain)  # No title and no hint.
    assert not any("─" in row or "│" in row or "╭" in row for row in plain)  # No box.
    filled = [i for i, row in enumerate(plain) if row.strip()]
    assert filled == [9, 10]  # Two body rows, at 2/5 of the blank space.
    assert plain[9].rstrip() == " " * 24 + "CODE"  # The text is centred across the whole width.
    assert screen._scroll_viewport == 26  # The frame states the height before it asks for the body.


def test_compose_bare_windows_a_body_taller_than_the_frame_from_the_top() -> None:
    """A body that is too tall for the frame scrolls. The top (the code) is whole first."""
    body = Group(*(Text(f"row {i}") for i in range(40)))
    screen = ScrollScreen(body, floating=False)
    screen.bare = True
    rows = Text.from_ansi(frame.compose_bare(screen, 53, 26)).plain.split("\n")
    assert len(rows) == 26
    assert rows[0].strip() == "row 0" and rows[25].strip() == "row 25"
    screen.scroll_to_bottom()
    rows = Text.from_ansi(frame.compose_bare(screen, 53, 26)).plain.split("\n")
    assert rows[25].strip() == "row 39"


def test_startup_splash_gives_rows_back_in_order_when_short() -> None:
    """A short terminal gives up the blank line first, then the top of the mark, and never the box.

    The lettering says what the app is. The box is what the user came to use. The globe
    over the lettering is decoration, so it is what the splash gives up. The crop is from
    the top. Thus the lettering keeps its place while the mark becomes thinner above it.
    """
    marks = [f"MARK{i:02d}" for i in range(16)]  # A stand-in that has the height of the real mark.

    def splash(rows: int) -> list[str]:
        screen = SelectScreen("pick", [Choice("alpha", 1), Choice("beta", 2)])
        screen.chrome = False
        screen.banner = list(marks)
        return Text.from_ansi(frame.compose_startup(screen, 80, rows)).plain.split("\n")

    tall = splash(40)
    # There is room: each row of the mark shows, with a blank line between it and the box.
    assert all(m in "\n".join(tall) for m in marks)
    mark_end = max(i for i, line in enumerate(tall) if marks[-1] in line)
    assert tall[mark_end + 1].strip() == ""

    # Make the terminal shorter by one row at a time, and note when the splash gives up
    # each part. The exact heights depend on the height of the box below, so the test
    # checks only the order.
    gap_lost = cropped = None
    for rows in range(40, 8, -1):
        lines = splash(rows)
        end = max((i for i, ln in enumerate(lines) if marks[-1] in ln), default=None)
        has_gap = end is not None and end + 1 < len(lines) and lines[end + 1].strip() == ""
        if gap_lost is None and not has_gap:
            gap_lost = rows
        if cropped is None and marks[0] not in "\n".join(lines):
            cropped = rows
        if end is not None:  # While the splash draws any part of the mark, it draws the last row.
            assert marks[-1] in "\n".join(lines), f"{rows} rows: lost the mark's last row"
    assert gap_lost is not None, "the blank line was never given back"
    assert cropped is not None, "the mark was never cropped"
    assert gap_lost > cropped, "the blank line must go before the mark is cropped"

    # The crop has a limit. The first row after _BANNER_CROP_ROWS is never sheared, for any
    # height of the terminal. (Below a certain height, the block is taller than the screen,
    # and the clip at the end takes the bottom. This is a different mechanism, not the crop.)
    keeper = marks[frame._BANNER_CROP_ROWS]
    for rows in range(20, 5, -1):
        assert keeper in "\n".join(splash(rows)), f"{rows} rows: cropped past the bound"


def test_startup_splash_fits_every_platform_width() -> None:
    """The real wordmark, drawn at the width of each platform, never goes over that width.

    The full-size mark is 71 cells, and a PicoCalc console is 53 cells. The splash is the
    one screen that the gallery does not cover. It shipped torn on the PicoCalc until the
    narrow mark was added. Thus this test is the gate.
    """
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
    from meshterm.ui.logo import load_logo

    try:
        for platform in (REGULAR, PICOCALC_LYRA):
            set_platform(platform)
            cols = platform.readable_cols
            screen = SelectScreen("Choose a device", [Choice("alpha", 1)])
            screen.chrome = False
            screen.banner = load_logo()  # A real caller sets this: the full-size mark.
            screen.footnote = copyright_notice()
            out = frame.compose_startup(screen, cols, platform.readable_rows)
            for i, line in enumerate(Text.from_ansi(out).plain.split("\n")):
                assert cell_len(line) <= cols, (
                    f"{platform.name} splash line {i} is {cell_len(line)} cells, over {cols}"
                )
    finally:
        set_platform(REGULAR)


def test_logo_takes_the_widest_mark_that_fits_the_columns() -> None:
    """The screen picks the size, not the platform. A narrow desktop gets the small mark."""
    from meshterm.ui.logo import load_logo, logo_width

    wide = logo_width(load_logo())
    narrow = logo_width(load_logo(53))
    assert 0 < narrow <= 53 < wide
    # If there is room for the big mark, the screen picks it.
    assert logo_width(load_logo(wide)) == wide
    # If the room is one column short, the screen picks the small mark.
    assert logo_width(load_logo(wide - 1)) == narrow
    assert load_logo(narrow - 1) == []  # nothing fits: no banner beats a torn one


def test_compose_startup_shows_footnote_under_logo() -> None:
    """The startup footnote is directly under the logo.

    MeshTerm draws a footnote (for example a copyright) muted, and aligned to the right
    edge of the logo. Thus the logo and the footnote read as one signed block.
    """
    screen = SelectScreen("pick", [Choice("a", 1)])
    screen.chrome = False
    screen.banner = ["A" * 40, "B" * 40]  # A wide wordmark, to align the note with.
    screen.footnote = "note-xyz"
    lines = Text.from_ansi(frame.compose_startup(screen, 80, 20)).plain.split("\n")
    logo_rows = [i for i, ln in enumerate(lines) if set(ln.strip()) in ({"A"}, {"B"})]
    note_row = next(i for i, ln in enumerate(lines) if "note-xyz" in ln)
    assert lines[note_row].strip() == "note-xyz"  # The note has its own line.
    assert note_row == logo_rows[-1] + 1  # The note is directly under the logo, with no gap.
    # The right edges align: the note ends at the same column as the logo.
    assert len(lines[note_row].rstrip()) == len(lines[logo_rows[-1]].rstrip())


def test_compose_startup_box_is_horizontally_centered() -> None:
    """The box has the size of its content and is centred, so its rows have a left margin."""
    screen = SelectScreen("pick", [Choice("a", 1)])
    screen.chrome = False
    lines = Text.from_ansi(frame.compose_startup(screen, 80, 20)).plain.split("\n")
    box_lines = [ln for ln in lines if ln.strip()]
    assert box_lines and all(ln.startswith("  ") for ln in box_lines)  # The box is centred.


# --- frames ------------------------------------------------------------------


def _cell_color(line: str, idx: int) -> tuple[int, int, int]:
    """Find the foreground RGB of the character at ``idx`` in an ANSI line."""
    from meshterm.ui.tui.render import _console

    style = Text.from_ansi(line).get_style_at_offset(_console(80), idx)
    assert style.color is not None
    triplet = style.color.get_truecolor()
    return (triplet.red, triplet.green, triplet.blue)


_ACCENT = (129, 140, 248)  # The accent border of the theme, #818cf8.


def _find(plain: str, glyphs: frozenset[str]) -> int:
    """The index of the first box glyph from ``glyphs``.

    On legacy Windows, the render console can use square corners in place of rounded
    corners. Thus a test matches the whole family and not one character.
    """
    return next(i for i, ch in enumerate(plain) if ch in glyphs)


_CORNERS = frozenset("╭┌╮┐╰└╯┘")
_EDGES = frozenset("─│")


def _border_colours(lines: list[str]) -> set[tuple[int, int, int]]:
    """Each different colour in which MeshTerm paints the box-drawing glyphs in ``lines``."""
    return {
        _cell_color(line, i)
        for line in lines
        for i, ch in enumerate(Text.from_ansi(line).plain)
        if ch in _CORNERS or ch in _EDGES
    }


def test_a_dialog_leaves_its_hint_to_the_footer_line() -> None:
    """On the desktop, the box says nothing about keys, because the footer row does.

    The frame draws the hint of the top screen on its footer line. A dialog that also had
    the hint in its own border put the same sentence on one frame two times (JP,
    2026-08-31). Now the border has no hint, and the footer is the only place for hints.
    """
    hint = "←→ choose · Enter select · Esc cancel"
    screen = ScrollScreen(Text("Delete this contact?"), title="Delete contact")
    screen._footer_hint = hint
    box = _plain(frame.compose_dialog(screen, 72, 20))
    assert "Esc cancel" not in box and "Enter select" not in box

    base = ScrollScreen(Text("row"), title="Contacts", floating=False)
    footer = _plain(frame.compose_base(Text("h"), base, hint, 72, 20).split("\n")[-1])
    assert footer.strip() == hint


def test_a_silent_dialog_border_still_says_there_is_more() -> None:
    """If the border has no hint, the clip arrows keep the ``more`` words of the base frame."""
    tall = ScrollScreen(Text("\n".join(f"line {i}" for i in range(40))), title="Packet")
    tall._footer_hint = "↑↓ newer/older · Esc close"
    assert "↓ more" in _plain(frame.compose_dialog(tall, 72, 16))


def test_a_frame_draws_in_one_colour() -> None:
    """A border has one colour all the way round, with no lit corner and no fading edge.

    For a time, the frames had light from the top left. The corner blended toward white,
    and the light decayed along the top edge and the left edge. It looked like a gradient
    over the chrome, and not like a box. Thus we removed the pass, and the border draws
    flat. The test checks the nested panels with the outer frame, because the pass also
    lit those.
    """
    inner = Panel(Text("body"), border_style="accent", width=20)
    screen = ScrollScreen(Group(Text("above"), inner), title="outer")
    lines = frame.compose_base(Text("h"), screen, "hint", 60, 20).split("\n")
    assert _border_colours(lines) == {_ACCENT}, "the base frame draws in more than one colour"

    dialog = frame.compose_dialog(ScrollScreen(Text("body"), title="d"), 80, 20)
    assert _border_colours(dialog.split("\n")) == {_ACCENT}, "so does a dialog's"


# --- prompts -----------------------------------------------------------------


def test_text_screen_edits_and_validates() -> None:
    """Typing edits the buffer. A validator that fails blocks the submit and shows the error."""
    screen = TextScreen("name?", validate=lambda v: True if v == "ok" else "nope")
    for ch in "xy":
        screen.handle("text", ch)
    screen.future = _Fut()
    screen.handle("enter")  # The text 'xy' fails the validation.
    assert not screen.future.done()
    assert screen._error == "nope"
    screen.handle("backspace")
    screen.handle("backspace")
    for ch in "ok":
        screen.handle("text", ch)
    assert _run(screen, "enter") == "ok"


def test_text_screen_byte_limit_gauges_and_blocks_an_oversize_entry() -> None:
    """A field with a byte limit shows the used/limit gauge, and blocks Enter over the cap."""
    import re

    screen = TextScreen("msg?", byte_limit=10)
    for ch in "hello":
        screen.handle("text", ch)
    body = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(screen.render_body(40)))
    assert "5/10" in body  # The gauge shows used/limit.
    for ch in " world":  # The total is 11 bytes, which is over the cap of 10 bytes.
        screen.handle("text", ch)
    screen.future = _Fut()
    screen.handle("enter")
    assert not screen.future.done()  # The screen blocks the entry that is over the limit.
    assert "Too long by 1 byte" in screen._error
    # If the user trims the text to the limit, the screen lets it submit.
    for _ in range(2):
        screen.handle("backspace")
    assert _run(screen, "enter") == "hello wor"


def test_line_editor_word_motion() -> None:
    """Ctrl+Left and Ctrl+Right move by word.

    They go to the start of the current word. If the cursor is already there, they go to
    the previous word or the next word.
    """
    from meshterm.ui.tui.prompt import LineEditor

    editor = LineEditor("the quick  brown fox")  # The cursor is at the end (len 20).
    editor.edit("ctrl_left")
    assert editor.cursor == 17  # The start of "fox".
    editor.edit("ctrl_left")
    assert editor.cursor == 11  # It skips the double space, to the start of "brown".
    editor.edit("ctrl_left")
    assert editor.cursor == 4  # The start of "quick".
    editor.edit("ctrl_right")
    assert editor.cursor == 11  # Forward over "quick" and the spaces, to the start of "brown".
    editor.cursor = 13  # The middle of "brown".
    editor.edit("ctrl_left")
    assert editor.cursor == 11  # To the start of the current word, not the previous word.


def test_text_screen_password_masks() -> None:
    """A password field renders bullets and not the characters that the user typed."""
    screen = TextScreen("pw?", password=True)
    for ch in "secret":
        screen.handle("text", ch)
    rendered = "\n".join(screen.render_body(40))
    assert "secret" not in rendered
    assert "•" in rendered


def test_line_editor_caps_length_and_truncates_paste() -> None:
    """An editor with a max_length ignores keys past the cap, and cuts a paste that is too long."""
    from meshterm.ui.tui.prompt import LineEditor

    editor = LineEditor("", max_length=6)
    for ch in "123456":
        assert editor.edit("text", ch) is True
    # The editor is full. It ignores the key and keeps the buffer.
    assert editor.edit("text", "9") is False
    assert editor.text == "123456"

    pasted = LineEditor("", max_length=6)
    pasted.edit("text", "12345678")  # One insert that is too long.
    assert pasted.text == "123456"  # The editor filled only the six slots that were free.


def test_line_editor_paste_folds_controls_and_respects_max_length() -> None:
    """A ``paste`` action changes newlines and controls to spaces, and inserts the text."""
    from meshterm.ui.tui.prompt import LineEditor

    editor = LineEditor("ab")
    editor.cursor = 1
    assert editor.edit("paste", "X\nY") is True
    # The newline became a space, and it went in the middle of the buffer.
    assert editor.text == "aX Yb"

    capped = LineEditor("", max_length=3)
    capped.edit("paste", "hello")
    # The editor cuts a paste to the room that is left, as for a big insert.
    assert capped.text == "hel"

    # A paste of nothing does not change the buffer.
    assert LineEditor("z").edit("paste", "") is False


def test_pin_dialog_shows_six_slots_with_dots_for_blanks() -> None:
    """The PIN field has six fixed slots: bullets for typed digits and centre dots for blanks."""
    from meshterm.ui.tui.prompt import PinDialog

    dialog = PinDialog("MeshCore-Testbench")
    # Empty: six blank centre dots and no bullets.
    field = dialog._editor.render(slots=PinDialog.PIN_LENGTH).plain
    assert field.count("·") == 6
    assert "•" not in field

    # After three digits: three bullets and three centre dots.
    for ch in "123":
        dialog.handle("text", ch)
    field = dialog._editor.render(slots=PinDialog.PIN_LENGTH).plain
    assert field.count("•") == 3
    assert field.count("·") == 3

    # A PIN of six digits fills each slot. The editor caps more typing at six.
    for ch in "456999":
        dialog.handle("text", ch)
    assert dialog._editor.text == "123456"
    field = dialog._editor.render(slots=PinDialog.PIN_LENGTH).plain
    assert field.count("•") == 6
    assert "·" not in field


def test_confirm_toggle_and_default() -> None:
    """The confirm toggles with the arrows or letters, and returns the bool that the user chose."""
    screen = ConfirmScreen("sure?", default=True)
    assert _run(screen, "enter") is True
    screen = ConfirmScreen("sure?", default=True)
    screen.handle("left")  # Toggle to No.
    assert _run(screen, "enter") is False
    screen = ConfirmScreen("sure?", default=True)
    screen.handle("text", "n")
    assert _run(screen, "enter") is False


def test_button_dialog_enter_commits_highlighted() -> None:
    """Enter returns the value of the highlighted button. The default sets the highlight."""
    screen = ButtonDialog("quit?", [("Yes", True), ("No", False)], default=0)
    assert _run(screen, "enter") is True
    screen = ButtonDialog("quit?", [("Yes", True), ("No", False)], default=1)
    assert _run(screen, "enter") is False


def test_button_dialog_arrows_move_highlight() -> None:
    """←/→ move the highlight between the buttons and clamp at the ends. Tab still cycles.

    Tab is the exception. It has no reverse key of its own, so on the last chip it goes
    round to the first chip and does not stop.
    """
    screen = ButtonDialog("quit?", [("Yes", True), ("No", False)], default=0)
    screen.handle("right")  # To No.
    assert _run(screen, "enter") is False
    screen = ButtonDialog("quit?", [("Yes", True), ("No", False)], default=0)
    screen.handle("left")  # The highlight is already at the left end, so it stays on Yes.
    assert _run(screen, "enter") is True
    screen = ButtonDialog("quit?", [("Yes", True), ("No", False)], default=1)
    screen.handle("right")  # The highlight is already at the right end, so it stays on No.
    assert _run(screen, "enter") is False
    screen = ButtonDialog("quit?", [("Yes", True), ("No", False)], default=1)
    screen.handle("tab")  # From No, round to Yes.
    assert _run(screen, "enter") is True


def test_button_dialog_shortcut_keys_commit_instantly() -> None:
    """A shortcut key that is in the map commits its value at once. It ignores the highlight."""
    screen = ButtonDialog(
        "quit?", [("Yes", True), ("No", False)], default=1, keys={"y": True, "n": False}
    )
    assert _run(screen, "text", "Y") is True  # The match ignores case, and ignores the default No.
    screen = ButtonDialog(
        "quit?", [("Yes", True), ("No", False)], default=0, keys={"y": True, "n": False}
    )
    assert _run(screen, "text", "n") is False


def test_button_dialog_escape_cancels() -> None:
    """Esc resolves with CANCEL, so the caller can treat it as 'stay'."""
    from meshterm.ui.tui.screen import CANCEL

    screen = ButtonDialog("quit?", [("Yes", True), ("No", False)])
    assert _run(screen, "escape") is CANCEL


def test_button_dialog_renders_a_styled_text_prompt_line_per_line() -> None:
    """A multi-line Text prompt that has styles renders each line, and keeps its content."""
    message = Text.from_markup("[ok]✓[/ok] clock set\n[ok]●[/ok] wrote backup.toml")
    screen = ButtonDialog(message, [("OK", "ok")])
    body = "\n".join(screen.render_body(60))
    plain = Text.from_ansi(body).plain
    assert "✓ clock set" in plain
    assert "● wrote backup.toml" in plain
    assert "OK" in plain


def test_button_dialog_sizes_to_the_widest_prompt_line() -> None:
    """dialog_width follows the longest line of a multi-line Text prompt."""
    wide = "a really quite long outcome line for sizing"
    message = Text(f"short\n{wide}")
    screen = ButtonDialog(message, [("OK", "ok")])
    assert screen.dialog_width == len(wide) + 12  # This is the margin that the dialog adds.


def test_autocomplete_suggests_and_tab_completes() -> None:
    """Suggestions ignore case. Tab fills the highlighted suggestion, and Enter commits."""
    screen = AutocompleteScreen("target?", ["Alice", "Bob", "alfred"])
    for ch in "al":
        screen.handle("text", ch)
    assert screen._suggestions() == ["Alice", "alfred"]
    screen.handle("down")  # Highlight 'alfred'.
    screen.handle("tab")  # Fill it.
    assert screen._editor.text == "alfred"
    assert _run(screen, "enter") == "alfred"


def test_autocomplete_accepts_free_text() -> None:
    """Text that has no matching suggestion still commits as typed (for example a hex prefix)."""
    screen = AutocompleteScreen("target?", ["Alice"])
    for ch in "3d":
        screen.handle("text", ch)
    assert _run(screen, "enter") == "3d"


# --- device picker -----------------------------------------------------------


def test_device_picker_builds_aligned_columns(tmp_path) -> None:
    """The picker puts devices in columns that line up across rows of different widths."""
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import DiscoveredDevice
    from meshterm.ui.device_picker import prompt_device

    devices = [
        DiscoveredDevice(port="COM5", product="Wio SX1262", vid=0x2886),
        DiscoveredDevice(port="/dev/ttyUSB0", product="FT232R USB UART", vid=0x0403),
    ]
    captured: dict = {}

    class _Ui:
        async def select_startup(  # noqa: ANN001, ANN201, ANN003
            self, title, items, *, default=None, banner=None, footnote=None, **_kw
        ):
            captured["items"] = items
            captured["banner"] = banner
            captured["footnote"] = footnote
            return None  # The user skips, so the flow never reaches the smoke test.

    async def _never(_device):  # The check is not used when the user skips.
        raise AssertionError("verify should not run when selection is skipped")

    store = DeviceStore(tmp_path / "devices.json")
    asyncio.run(prompt_device(_Ui(), devices, store, _never))
    # The picker passes the banner (the wordmark) through, so the splash can draw it.
    assert captured["banner"] and any("█" in row for row in captured["banner"])
    # There is no footnote. The wordmark has its own copyright, so the splash adds none.
    assert captured["footnote"] is None
    # The port of each device row is at the same column. This proves that the name column
    # has padding.
    rows = [
        it.label.plain if hasattr(it.label, "plain") else it.label
        for it in captured["items"]
        if isinstance(it, Choice)
    ]
    # Two devices, then the action rows at the end (add a network device, then Quit).
    assert len(rows) == 4
    assert rows[-2].strip().endswith("Add a network device…")  # No caveat tag follows it.
    assert rows[-1].strip().endswith("Quit")
    device_rows = rows[:2]
    assert all(port in row for port, row in zip(("COM5", "/dev/ttyUSB0"), device_rows, strict=True))
    assert device_rows[0].index("COM5") == device_rows[1].index("/dev/ttyUSB0")


def test_device_picker_shortens_nothing_and_puts_the_address_last() -> None:
    """MeshTerm draws each field whole, and the connection target is the last column of the row.

    The lanes once had a budget, so that the row fitted the box. Beside a CoreBluetooth
    UUID of 36 cells, this cut a name to ``Johnp…`` and cut the UUID to its tail. Now a row
    that is wider than the box goes past its edge, and ←→ pan to the rest. The address is
    the lane that the user needs least, so it is last. There, the loss at the edge is
    smallest. The transport badge starts the row, and it has no heading over it.
    """
    from meshterm.core.discovery import DiscoveredDevice
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
    from meshterm.ui.device_picker import _build_items, _display_name

    name = "MeshCore-Johnputer Wardriver"
    uuid = "12345678-1234-1234-1234-123456789ABC"
    adapter = "CP2102 USB to UART Bridge Controller"
    devices = [
        DiscoveredDevice(transport="ble", address=uuid, name=name, product=name),
        DiscoveredDevice("/dev/cu.Bluetooth-Incoming-Port"),
        DiscoveredDevice("/dev/ttyUSB0", product=adapter, vid=0x10C4),
    ]
    try:
        for platform in (REGULAR, PICOCALC_LYRA):
            set_platform(platform)
            items = _build_items(devices, None, {})
            rows = {
                it.value.stable_id: it.title.plain
                for it in items
                if isinstance(it, Choice) and it.value in devices
            }
            for device in devices:
                row = rows[device.stable_id].rstrip()
                assert "…" not in row, platform.name
                # The target is whole, and nothing comes after it.
                assert row.endswith(device.target), platform.name
            assert name in rows[devices[0].stable_id] and adapter in rows[devices[2].stable_id]
            # The header is complete at each width that the row needs. ADDRESS is last, and
            # the badge column over which the header starts has no label.
            header = items[0].text(1000)
            assert header.split() == ["DEVICE", "HARDWARE", "PORT", "/", "ADDRESS"]
            for device in devices:
                row = rows[device.stable_id]
                name_at = row.index(_display_name(device, {}))
                assert header.index("DEVICE") == cell_len(row[:name_at]) + 2  # Plus the pointer.
                assert row[:name_at].strip(), "the badge comes before the name"
    finally:
        set_platform(REGULAR)


def test_device_picker_names_and_sorts_known_devices(tmp_path) -> None:
    """A confirmed device shows its node name (in white), goes to the top, and marks its type."""
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import DiscoveredDevice
    from meshterm.ui.device_picker import _BLE_ICON, _SERIAL_ICON, prompt_device

    # A serial node that the user confirmed before, an unknown serial port, and a BLE companion.
    known = DiscoveredDevice(
        port="COM11", serial_number="SN1", description="USB Serial Device (COM11)"
    )
    unknown = DiscoveredDevice(port="COM3", product="Some Adapter", vid=0x1234)
    ble = DiscoveredDevice(
        transport="ble", address="AA:BB:CC:DD:EE:FF", name="MeshCore-Roam", product="MeshCore-Roam"
    )
    devices = [unknown, known, ble]  # The order of discovery: the known device is not first.

    store = DeviceStore(tmp_path / "devices.json")
    store.remember(known, node_name="BaseStation")

    captured: dict = {}

    class _Ui:
        async def select_startup(  # noqa: ANN001, ANN201, ANN003
            self, title, items, *, default=None, banner=None, footnote=None, **_kw
        ):
            captured["items"] = items
            return None  # The user skips, so the flow never reaches the smoke test.

    async def _never(_device):
        raise AssertionError("verify should not run when selection is skipped")

    asyncio.run(prompt_device(_Ui(), devices, store, _never))
    rows = [it.title for it in captured["items"] if isinstance(it, Choice)]
    device_rows = rows[:-1]  # Remove the Quit row at the end.

    # The confirmed device is at the very top. The row shows its mesh node name, not the
    # generic description of the operating system, "USB Serial Device".
    top = device_rows[0]
    assert "BaseStation" in top.plain
    assert "USB Serial Device" not in top.plain
    # The name is also white ("device.known"), so that it is easy to see.
    assert any(span.style == "device.known" for span in top.spans)

    # The TYPE column marks the transport: a serial glyph for the wired node, and the
    # Bluetooth rune for the companion that advertises over BLE.
    assert _SERIAL_ICON in top.plain
    assert any(_BLE_ICON in row.plain for row in device_rows)


def test_device_picker_ranks_likely_companions_above_a_bare_port() -> None:
    """The UART of a board (with no USB identity) goes below companions on any transport.

    On a Cardputer Zero, ``/dev/ttyS0`` is the GPS of the Cap. MeshTerm scans serial first,
    so it listed this port first. Then the splash opened with the highlight on a port that
    cannot be a node.
    """
    from meshterm.core.config import SpiWiring
    from meshterm.core.discovery import DiscoveredDevice, spi_device
    from meshterm.ui.device_picker import _order

    uart = DiscoveredDevice(port="/dev/ttyS0", description="n/a")
    ble = DiscoveredDevice(transport="ble", address="AA:BB", name="MeshCore-Roam")
    cap = spi_device(SpiWiring(bus_id=0, cs_id=1), name="Cap LoRa-1262")
    assert _order([uart, ble, cap], {}) == [ble, cap, uart]


def test_device_picker_reinjects_remembered_tcp_device(tmp_path) -> None:
    """A remembered TCP companion appears in the picker again, also if no scan can find it."""
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import tcp_device
    from meshterm.ui.device_picker import _TCP_ICON, prompt_device

    store = DeviceStore(tmp_path / "devices.json")
    store.remember(tcp_device("192.168.1.50", 5000), node_name="WifiNode")

    captured: dict = {}

    class _Ui:
        async def select_startup(  # noqa: ANN001, ANN201, ANN003
            self, title, items, *, default=None, banner=None, footnote=None, **_kw
        ):
            captured["items"] = items
            return None  # The user skips, so the flow never reaches the smoke test.

    async def _never(_device):
        raise AssertionError("verify should not run when selection is skipped")

    # The scan found nothing in this session, but MeshTerm builds the remembered network
    # device into the list again.
    asyncio.run(prompt_device(_Ui(), [], store, _never))
    rows = [it.title for it in captured["items"] if isinstance(it, Choice)]
    device_rows = [r for r in rows if hasattr(r, "plain") and "WifiNode" in r.plain]
    assert device_rows, "the remembered TCP device should be listed"
    top = device_rows[0]
    assert "192.168.1.50:5000" in top.plain  # Its host and port are in the address column.
    assert _TCP_ICON in top.plain  # It has the network glyph in the TYPE column.


def test_device_picker_lists_configured_tcp_profile(tmp_path) -> None:
    """A ``[profiles.*]`` TCP entry is in the picker under its alias, and the user can select it."""
    from meshterm.core.config import DeviceProfile
    from meshterm.core.device_store import DeviceStore
    from meshterm.ui.device_picker import _TCP_ICON, prompt_device

    store = DeviceStore(tmp_path / "devices.json")
    profiles = {
        "bridge": DeviceProfile(name="bridge", transport="tcp", host="127.0.0.1", tcp_port=5000)
    }

    captured: dict = {}

    class _Ui:
        async def select_startup(  # noqa: ANN001, ANN201, ANN003
            self, title, items, *, default=None, banner=None, footnote=None, **_kw
        ):
            captured["items"] = items
            return None  # The user skips, so the flow never reaches the smoke test.

    async def _never(_device):
        raise AssertionError("verify should not run when selection is skipped")

    # The scan found nothing and nothing is remembered. The profile alone puts the endpoint
    # in the list.
    asyncio.run(prompt_device(_Ui(), [], store, _never, profiles))
    rows = [it.title for it in captured["items"] if isinstance(it, Choice)]
    device_rows = [r for r in rows if hasattr(r, "plain") and "bridge" in r.plain]
    assert device_rows, "the configured TCP profile should be listed"
    top = device_rows[0]
    assert "127.0.0.1:5000" in top.plain  # Its host and port are in the address column.
    assert _TCP_ICON in top.plain  # It has the network glyph in the TYPE column.


def test_device_picker_lists_configured_serial_profile(tmp_path) -> None:
    """A serial profile in the config is in the picker under its alias.

    The user can select a soldered ``/dev/ttyS1``, although the scan of pyserial never
    produces that platform port.
    """
    from meshterm.core.config import DeviceProfile
    from meshterm.core.device_store import DeviceStore
    from meshterm.ui.device_picker import prompt_device

    store = DeviceStore(tmp_path / "devices.json")
    profiles = {"picocalc": DeviceProfile(name="picocalc", port="/dev/ttyS1", baudrate=115200)}

    captured: dict = {}

    class _Ui:
        async def select_startup(  # noqa: ANN001, ANN201, ANN003
            self, title, items, *, default=None, banner=None, footnote=None, **_kw
        ):
            captured["items"] = items
            return None  # The user skips, so the flow never reaches the smoke test.

    async def _never(_device):
        raise AssertionError("verify should not run when selection is skipped")

    # The scan found nothing and nothing is remembered. The serial profile alone puts the
    # port in the list.
    asyncio.run(prompt_device(_Ui(), [], store, _never, profiles))
    rows = [it.title for it in captured["items"] if isinstance(it, Choice)]
    device_rows = [r for r in rows if hasattr(r, "plain") and "picocalc" in r.plain]
    assert device_rows, "the configured serial profile should be listed"
    assert "/dev/ttyS1" in device_rows[0].plain  # Its port is in the address column.


def test_device_picker_serial_profile_yields_to_scanned_port(tmp_path) -> None:
    """A serial profile yields to the scanned port when both name the same port.

    The row that pyserial found wins, because it has real USB metadata and the bare
    profile does not.
    """
    from meshterm.core.config import DeviceProfile
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import DiscoveredDevice
    from meshterm.ui.device_picker import prompt_device

    store = DeviceStore(tmp_path / "devices.json")
    scanned = [DiscoveredDevice(port="/dev/ttyUSB0", product="XIAO", vid=0x2886, pid=0x8044)]
    profiles = {"radio": DeviceProfile(name="radio", port="/dev/ttyUSB0")}

    captured: dict = {}

    class _Ui:
        async def select_startup(  # noqa: ANN001, ANN201, ANN003
            self, title, items, *, default=None, banner=None, footnote=None, **_kw
        ):
            captured["items"] = items
            return None

    async def _never(_device):
        raise AssertionError("verify should not run when selection is skipped")

    asyncio.run(prompt_device(_Ui(), scanned, store, _never, profiles))
    rows = [it.title for it in captured["items"] if isinstance(it, Choice)]
    usb_rows = [r for r in rows if hasattr(r, "plain") and "/dev/ttyUSB0" in r.plain]
    assert len(usb_rows) == 1, "the scanned port must not be duplicated by the profile"


def test_device_picker_profile_yields_to_remembered_endpoint(tmp_path) -> None:
    """A profile at an endpoint that is already remembered is not listed twice.

    The row that has more information wins.
    """
    from meshterm.core.config import DeviceProfile
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import tcp_device
    from meshterm.ui.device_picker import prompt_device

    store = DeviceStore(tmp_path / "devices.json")
    # The user confirmed it before, so it has the real node name that MeshTerm learned at
    # connect time.
    store.remember(tcp_device("127.0.0.1", 5000), node_name="uConsole")
    profiles = {
        "bridge": DeviceProfile(name="bridge", transport="tcp", host="127.0.0.1", tcp_port=5000)
    }

    captured: dict = {}

    class _Ui:
        async def select_startup(  # noqa: ANN001, ANN201, ANN003
            self, title, items, *, default=None, banner=None, footnote=None, **_kw
        ):
            captured["items"] = items
            return None

    async def _never(_device):
        raise AssertionError("verify should not run when selection is skipped")

    asyncio.run(prompt_device(_Ui(), [], store, _never, profiles))
    rows = [it.title for it in captured["items"] if isinstance(it, Choice)]
    endpoint_rows = [r for r in rows if hasattr(r, "plain") and "127.0.0.1:5000" in r.plain]
    assert len(endpoint_rows) == 1, "the endpoint should appear exactly once"
    # The remembered node name wins over the plain alias of the profile.
    assert "uConsole" in endpoint_rows[0].plain
    assert "bridge" not in endpoint_rows[0].plain


def test_device_picker_adds_network_device(tmp_path) -> None:
    """The 'add a network device' row asks for the host and port, and confirms the TCP companion."""
    from meshterm.core.device_store import DeviceStore
    from meshterm.ui.device_picker import _ADD_TCP, prompt_device

    store = DeviceStore(tmp_path / "devices.json")
    probed: dict = {}

    class _Ui:
        async def select_startup(  # noqa: ANN001, ANN201, ANN003
            self, title, items, *, default=None, banner=None, footnote=None, **_kw
        ):
            # Select the action row "add a network device".
            return next(it.value for it in items if isinstance(it, Choice) and it.value is _ADD_TCP)

        async def prompt_text_startup(
            self,
            title,
            *,
            prompt="",
            default="",
            validate=None,
            help_text="",
            banner=None,
            footnote=None,
        ):
            # The validator accepts a correct endpoint.
            assert validate("192.168.1.50:5000") is True
            return "192.168.1.50:5000"

        async def busy_startup(self, message, coro, *, title="", banner=None, footnote=None):
            return await coro

    async def verify(device, pin=None):
        probed["target"] = device.target
        return {"adv_name": "WifiNode"}  # A real companion answers.

    chosen = asyncio.run(prompt_device(_Ui(), [], store, verify))
    assert chosen is not None and chosen.is_tcp and chosen.target == "192.168.1.50:5000"
    assert probed["target"] == "192.168.1.50:5000"
    # MeshTerm remembers the confirmed network device for ever, with the node name that it
    # reported.
    remembered = store.load()
    assert remembered is not None and remembered.is_tcp and remembered.node_name == "WifiNode"


def test_device_picker_removes_network_device_on_delete(tmp_path) -> None:
    """Delete on a network row asks to confirm, then forgets the device and removes the row."""
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import tcp_device
    from meshterm.ui.device_picker import _QUIT, prompt_device
    from meshterm.ui.tui import DeleteRequest

    store = DeviceStore(tmp_path / "devices.json")
    store.remember(tcp_device("192.168.1.50", 5000), node_name="WifiNode")

    seen_rows: list[list] = []
    confirmed_prompts: list = []

    class _Ui:
        def __init__(self) -> None:
            self._passes = 0

        async def select_startup(  # noqa: ANN001, ANN201, ANN003
            self, title, items, *, default=None, banner=None, footnote=None, **_kw
        ):
            names = [
                it.label.plain
                for it in items
                if isinstance(it, Choice) and hasattr(it.label, "plain")
            ]
            seen_rows.append(names)
            self._passes += 1
            if self._passes == 1:
                # First pass: the network row is present, and the user can delete it. Press
                # Delete.
                row = next(
                    it
                    for it in items
                    if isinstance(it, Choice) and getattr(it.value, "is_tcp", False)
                )
                assert row.deletable
                return DeleteRequest(row.value)
            # Second pass (after the removal): quit the picker.
            return _QUIT

        async def confirm_startup(
            self,
            prompt,
            *,
            title="",
            confirm_label="Remove",
            banner=None,
            footnote=None,
            backdrop_items=None,
            backdrop_default=None,
        ):
            confirmed_prompts.append(prompt)
            # The confirm floats over the picker. It gets the rows to draw behind it, with
            # the row to remove already highlighted.
            assert backdrop_items is not None
            assert getattr(backdrop_default, "is_tcp", False)
            return True  # The user confirms the removal.

    async def _never(_device, _pin=None):
        raise AssertionError("verify should not run when a row is deleted, not chosen")

    result = asyncio.run(prompt_device(_Ui(), [], store, _never))
    assert result is None  # The user quit on the second pass.
    # The confirm named the device and its endpoint.
    assert confirmed_prompts and "WifiNode" in confirmed_prompts[0]
    assert "192.168.1.50:5000" in confirmed_prompts[0]
    # The row was in the first paint and gone in the second, and the store forgot the device.
    assert any("WifiNode" in name for name in seen_rows[0])
    assert not any("WifiNode" in name for name in seen_rows[1])
    assert store.load() is None


def test_device_picker_keeps_network_device_when_removal_cancelled(tmp_path) -> None:
    """If the user cancels the Delete confirm, the remembered network device stays."""
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import tcp_device
    from meshterm.ui.device_picker import _QUIT, prompt_device
    from meshterm.ui.tui import DeleteRequest

    store = DeviceStore(tmp_path / "devices.json")
    store.remember(tcp_device("192.168.1.50", 5000), node_name="WifiNode")

    class _Ui:
        def __init__(self) -> None:
            self._passes = 0

        async def select_startup(  # noqa: ANN001, ANN201, ANN003
            self, title, items, *, default=None, banner=None, footnote=None, **_kw
        ):
            self._passes += 1
            if self._passes == 1:
                row = next(
                    it
                    for it in items
                    if isinstance(it, Choice) and getattr(it.value, "is_tcp", False)
                )
                return DeleteRequest(row.value)
            return _QUIT

        async def confirm_startup(
            self,
            prompt,
            *,
            title="",
            confirm_label="Remove",
            banner=None,
            footnote=None,
            backdrop_items=None,
            backdrop_default=None,
        ):
            return False  # The user backs out (Cancel or Esc).

    async def _never(_device, _pin=None):
        raise AssertionError("verify should not run")

    asyncio.run(prompt_device(_Ui(), [], store, _never))
    # MeshTerm forgot nothing, so it still remembers the device.
    remembered = store.load()
    assert remembered is not None and remembered.node_name == "WifiNode"


def test_confirm_startup_floats_red_over_the_picker_backdrop() -> None:
    """The removal confirm floats as a red dialog over a picker that MeshTerm draws again.

    It is not a full splash.
    """
    from meshterm.core.discovery import tcp_device

    session = TuiSession()
    device = tcp_device("192.168.1.50", 5000, name="WifiNode")
    items = [Choice("WifiNode (192.168.1.50:5000)", device, deletable=True)]

    async def main() -> None:
        task = asyncio.ensure_future(
            session.confirm_startup(
                "Remove WifiNode?",
                title="Remove network device",
                banner=["MESHTERM"],
                backdrop_items=items,
                backdrop_default=device,
            )
        )
        # Let confirm_startup push the backdrop list and float the confirm over it.
        for _ in range(3):
            await asyncio.sleep(0)

        base = session._base_screen()
        floats = session._float_layers()
        # MeshTerm draws the picker again as the base screen that has no chrome. The confirm
        # floats over it. It is not a full-screen splash that replaces the list.
        assert isinstance(base, SelectScreen) and base.chrome is False
        assert len(floats) == 1
        dialog = floats[0]
        assert isinstance(dialog, ButtonDialog)
        assert dialog.border_style == "err"  # The red that is reserved for data loss.

        dialog.resolve(True)  # Commit the removal.
        result = await task
        assert result is True
        assert session._stack == []  # MeshTerm removes the backdrop with the dialog.

    asyncio.run(main())


class _PickerUi:
    """A fake splash UI that always selects the first device, and then closes messages."""

    def __init__(self) -> None:
        self.notes: list = []

    async def select_startup(  # noqa: ANN001, ANN201, ANN003
        self, title, items, *, default=None, banner=None, footnote=None, **_kw
    ):
        return next(it.value for it in items if isinstance(it, Choice))

    async def notify_startup(self, renderable, *, title="", banner=None, footnote=None):
        self.notes.append(renderable)

    async def busy_startup(self, message, coro, *, title="", banner=None, footnote=None):
        return await coro


def test_device_picker_smoke_tests_and_reprompts(tmp_path) -> None:
    """A smoke test that fails asks again. MeshTerm remembers a test that passes as confirmed."""
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import DiscoveredDevice
    from meshterm.ui.device_picker import prompt_device

    devices = [DiscoveredDevice(port="COM5", serial_number="SN1", product="Wio SX1262")]
    store = DeviceStore(tmp_path / "devices.json")
    ui = _PickerUi()

    # The first probe fails (not MeshCore). The second probe answers with its own info.
    results = [None, {"adv_name": "BaseStation"}]

    async def verify(_device, _pin=None):
        return results.pop(0)

    chosen = asyncio.run(prompt_device(ui, devices, store, verify))
    assert chosen is devices[0]
    assert len(ui.notes) == 1  # The picker showed the "not a MeshCore device" message one time.
    remembered = store.load()
    assert remembered is not None and remembered.node_name == "BaseStation"
    assert store.is_known(devices[0])


def test_device_picker_leaves_the_copyright_to_the_wordmark(tmp_path) -> None:
    """No splash states the copyright, because the art of the wordmark already has it."""
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import DiscoveredDevice
    from meshterm.ui.device_picker import prompt_device

    devices = [DiscoveredDevice(port="COM5", serial_number="SN1", product="Wio SX1262")]
    store = DeviceStore(tmp_path / "devices.json")
    footnotes: list = []

    class _Ui:
        async def select_startup(  # noqa: ANN001, ANN201, ANN003
            self, title, items, *, default=None, banner=None, footnote=None, **_kw
        ):
            footnotes.append(("select", footnote))
            return next(it.value for it in items if isinstance(it, Choice))

        async def notify_startup(self, renderable, *, title="", banner=None, footnote=None):
            footnotes.append(("notify", footnote))

        async def busy_startup(self, message, coro, *, title="", banner=None, footnote=None):
            footnotes.append(("busy", footnote))
            return await coro

    # The first probe fails (the user selects again). The second probe passes. Thus the test
    # runs each screen that comes after a selection.
    results = [None, {"adv_name": "BaseStation"}]

    async def verify(_device, _pin=None):
        return results.pop(0)

    asyncio.run(prompt_device(_Ui(), devices, store, verify))
    # Each splash draws the logo, and the copyright is now in the logo. Thus no splash adds
    # a line of its own: not the first picker, not the spinner of the smoke test, not the
    # failure notice, and not the picker that opens again.
    assert len(footnotes) > 1
    assert all(footnote is None for _kind, footnote in footnotes)


def test_device_picker_prompts_and_retries_ble_pin(tmp_path) -> None:
    """A device that has a PIN opens the dialog, asks again for a wrong code, then connects."""
    from meshterm.core.connection import DeviceAuthenticationError
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import DiscoveredDevice
    from meshterm.ui.device_picker import prompt_device

    devices = [
        DiscoveredDevice(transport="ble", address="00:11:22:33:44:55", name="MeshCore-Testbench")
    ]
    store = DeviceStore(tmp_path / "devices.json")

    entered = iter(["000000", "654321"])  # A wrong code, then the correct code.
    errors: list = []

    class _Ui:
        async def select_startup(  # noqa: ANN001, ANN201, ANN003
            self, title, items, *, default=None, banner=None, footnote=None, **_kw
        ):
            return next(it.value for it in items if isinstance(it, Choice))

        async def notify_startup(self, renderable, *, title="", banner=None, footnote=None):
            raise AssertionError("a successful PIN connect shows no failure notice")

        async def busy_startup(self, message, coro, *, title="", banner=None, footnote=None):
            return await coro

        async def prompt_pin_startup(
            self, device_name, *, error="", help_text="", banner=None, footnote=None
        ):
            errors.append(error)
            return next(entered)

    async def verify(_device, pin=None):
        if pin != "654321":  # The device rejects the first probe (no PIN) and the wrong code.
            # The connection names the refusal. The picker puts its hint into the dialog.
            hint = "That PIN was rejected — check the code and try again." if pin else ""
            raise DeviceAuthenticationError("needs a Bluetooth pairing PIN", hint=hint)
        return {"adv_name": "Pinned", "model": "Seeed Tracker T1000-E"}

    chosen = asyncio.run(prompt_device(_Ui(), devices, store, verify))
    assert chosen is devices[0]
    # The picker asked two times: first with no error, then with the hint of the refusal
    # after the wrong code.
    assert errors[0] == ""
    assert "rejected" in errors[1].lower()
    remembered = store.load()
    assert remembered is not None
    assert remembered.node_name == "Pinned"
    # MeshTerm learned the model when it connected.
    assert remembered.hardware_model == "Seeed Tracker T1000-E"


def test_device_picker_pin_cancel_returns_to_list(tmp_path) -> None:
    """Esc on the PIN dialog goes back to the device list. It does not connect."""
    from meshterm.core.connection import DeviceAuthenticationError
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import DiscoveredDevice
    from meshterm.ui.device_picker import _QUIT, prompt_device

    devices = [
        DiscoveredDevice(transport="ble", address="00:11:22:33:44:55", name="MeshCore-Testbench")
    ]
    store = DeviceStore(tmp_path / "devices.json")
    picks = iter([0, "quit"])  # Select the device one time, then quit the list that opens again.

    class _Ui:
        async def select_startup(  # noqa: ANN001, ANN201, ANN003
            self, title, items, *, default=None, banner=None, footnote=None, **_kw
        ):
            choices = [it for it in items if isinstance(it, Choice)]
            step = next(picks)
            if step == "quit":
                return next(it.value for it in choices if it.value is _QUIT)
            return choices[step].value

        async def busy_startup(self, message, coro, *, title="", banner=None, footnote=None):
            return await coro

        async def prompt_pin_startup(
            self, device_name, *, error="", help_text="", banner=None, footnote=None
        ):
            return None  # The user cancels the PIN entry.

    async def verify(_device, pin=None):
        raise DeviceAuthenticationError("needs a Bluetooth pairing PIN")

    result = asyncio.run(prompt_device(_Ui(), devices, store, verify))
    assert result is None  # The user cancels the PIN and quits, so the picker has no device.
    assert store.load() is None  # MeshTerm remembered nothing.


def test_device_picker_quit_row_returns_none(tmp_path) -> None:
    """If the user selects the Quit row at the end, the picker returns None. No smoke test runs."""
    from meshterm.core.device_store import DeviceStore
    from meshterm.core.discovery import DiscoveredDevice
    from meshterm.ui.device_picker import _QUIT, prompt_device

    devices = [DiscoveredDevice(port="COM5", product="Wio SX1262")]

    class _QuitUi:
        async def select_startup(  # noqa: ANN001, ANN201, ANN003
            self, title, items, *, default=None, banner=None, footnote=None, **_kw
        ):
            # The last choice is the Quit row. When the user selects it, the app exits.
            quit_choice = [it for it in items if isinstance(it, Choice) and it.value is _QUIT]
            assert quit_choice, "the picker offers a Quit row"
            return quit_choice[0].value

    async def _never(_device):
        raise AssertionError("verify must not run when the user quits")

    store = DeviceStore(tmp_path / "devices.json")
    assert asyncio.run(prompt_device(_QuitUi(), devices, store, _never)) is None


# --- busy splash --------------------------------------------------------------


def test_a_busy_caption_hangs_its_lines_under_its_text() -> None:
    """A line feed starts a line under the caption. A line that is too long for the box does too.

    A user reported this card. A long room name pushed the last letter of the count alone
    onto a line at column zero. Now the break is on purpose. The second line starts under
    the text of the first line, and the card has the width of its widest line.
    """
    from rich.text import Text as RichText

    from meshterm.ui.tui.screen import BusyDialog

    card = BusyDialog("logging in to YJN-ROOM-OBS St-Jean\nalong its 6-hop route… 37 s")
    lines = RichText.from_ansi("\n".join(card.render_body(60))).plain.splitlines()
    assert lines[0][3:] == "logging in to YJN-ROOM-OBS St-Jean"
    assert lines[1] == "   along its 6-hop route… 37 s"
    assert card.dialog_width == len("logging in to YJN-ROOM-OBS St-Jean") + 8
    narrow = RichText.from_ansi("\n".join(card.render_body(24))).plain.splitlines()
    assert all(line.startswith("   ") for line in narrow[1:]), narrow


def test_busy_screen_spins_over_its_message() -> None:
    """The busy splash shows an ASCII spinner beside its message, and it advances on a tick."""
    from rich.text import Text as RichText

    from meshterm.ui.tui.screen import BusyScreen
    from meshterm.ui.tui.spinner import Spinner

    screen = BusyScreen("Talking to Wio on COM5…")

    def glyph() -> str:
        """The first character (the spinner) of the rendered body, with no ANSI codes."""
        return RichText.from_ansi("\n".join(screen.render_body(60))).plain.lstrip()[0]

    plain = RichText.from_ansi("\n".join(screen.render_body(60))).plain
    assert "Talking to Wio on COM5" in plain
    assert glyph() in Spinner.BRAILLE  # A glyph of the spinner starts the line.

    # The ticks cycle through each animation step and return to the first.
    seen = {glyph()}
    for _ in range(len(Spinner.BRAILLE) - 1):
        screen.tick()
        seen.add(glyph())
    assert seen == set(Spinner.BRAILLE)


def test_spinner_cycles_and_resets() -> None:
    """The reusable Spinner advances through its animation steps, wraps, and resets."""
    from meshterm.ui.tui.spinner import Spinner

    spinner = Spinner("ab", style="warn")
    assert spinner.frame == "a"
    assert spinner.text().plain == "a" and spinner.text().style == "warn"
    spinner.tick()
    assert spinner.frame == "b"
    spinner.tick()  # It wraps back to the first animation step.
    assert spinner.frame == "a"
    spinner.tick()
    spinner.reset()
    assert spinner.frame == "a"


# --- progress ----------------------------------------------------------------


def test_progress_handle_matches_rich_api() -> None:
    """add_task, advance, and update are the same as the part of Rich Progress that tools use."""
    screen = ProgressScreen("work")
    task = screen.add_task("tracing", total=5)
    screen.advance(task)
    screen.advance(task, 2)
    assert screen._tasks[task].completed == 3
    screen.update(task, description="tracing more", completed=5, total=5)
    assert screen._tasks[task].description == "tracing more"
    assert screen._tasks[task].completed == 5
    # The screen renders without an error at a realistic width.
    assert screen.render_body(60)


def test_progress_chip_animates_between_advances() -> None:
    """The working chip spins on a tick alone, so a task that seldom advances looks alive.

    A trace advances only one time, at the very end. Until then, the meter does not move.
    A tick of the dialog must change what the dialog renders, also if the count of the
    task does not change.
    """
    screen = ProgressScreen("trace")
    screen.add_task("tracing", total=1)  # It stays at 0/1 for the whole wait.
    before = "\n".join(screen.render_body(60))
    screen.tick()
    after = "\n".join(screen.render_body(60))
    assert before != after  # The chip moved, but nothing advanced.


def test_progress_completed_task_shows_check_and_indeterminate_has_no_track() -> None:
    """A finished task changes its chip to ✓. A task with an unknown total draws no dead track."""
    from rich.text import Text as RichText

    screen = ProgressScreen("work")
    done = screen.add_task("tracing", total=2)
    screen.advance(done, 2)
    screen.add_task("optimizing", total=None)
    plain = RichText.from_ansi("\n".join(screen.render_body(60))).plain
    assert "✓ tracing" in plain
    # The row with no known total has only the spinning chip and its count. It never has a
    # glyph of a track.
    indet_line = next(line for line in plain.splitlines() if "optimizing" in line)
    assert "⠶" not in indet_line and "⣿" not in indet_line


# --- session stack -----------------------------------------------------------


async def test_session_runs_and_exits_when_main_returns() -> None:
    """The app starts, runs the main coroutine, and exits cleanly when the coroutine returns."""
    with create_pipe_input() as inp:
        session = TuiSession(input=inp, output=DummyOutput())
        ran = {}

        async def main() -> None:
            ran["done"] = True

        await asyncio.wait_for(session.run(main()), timeout=5)
    assert ran["done"] is True


async def test_session_select_dispatches_piped_keys() -> None:
    """A select resolves the value that the piped Down and Enter key presses choose, end to end."""
    with create_pipe_input() as inp:
        session = TuiSession(input=inp, output=DummyOutput())
        captured = {}

        async def main() -> None:
            captured["value"] = await session.select(
                "pick", [Choice("a", 1), Choice("b", 2), Choice("c", 3)]
            )

        inp.send_text("\x1b[B\x1b[B\r")  # Down, Down, Enter: the third choice.
        await asyncio.wait_for(session.run(main()), timeout=5)
    assert captured["value"] == 3


async def test_session_busy_startup_animates_and_returns() -> None:
    """busy_startup awaits the task behind a spinner splash that has no chrome, then pops it."""
    from meshterm.ui.tui.screen import BusyScreen

    with create_pipe_input() as inp:
        session = TuiSession(input=inp, output=DummyOutput())
        captured = {}

        async def work() -> str:
            # While the task runs, the busy splash is the base screen that has no chrome.
            assert isinstance(session.top, BusyScreen)
            assert session.top.chrome is False
            await asyncio.sleep(0.3)  # This is long enough for the spinner to tick one time.
            return "ok"

        async def main() -> None:
            captured["value"] = await session.busy_startup("checking…", work())

        await asyncio.wait_for(session.run(main()), timeout=5)
    assert captured["value"] == "ok"
    assert session.top is None  # MeshTerm popped the splash when the task finished.


async def test_busy_screens_spin_at_the_platform_cadence() -> None:
    """Neither busy screen has a spin rate of its own.

    A paint on the PicoCalc console costs more than the time that these loops once waited
    between paints. Thus a fixed cadence used the event loop to paint the spinner, and it
    starved the device read that the spinner covered. The wait became slower because it
    was animated. Both screens now use the one decision of the platform, as each other
    animated wait in the app does.
    """
    import inspect

    from meshterm.ui.tui.session import TuiSession as _Session

    for name in ("busy_startup", "busy_overlay"):
        interval = inspect.signature(getattr(_Session, name)).parameters["interval"]
        assert interval.default is None, f"{name} pins its own spin rate: {interval.default!r}"


async def test_busy_startup_keeps_ticking_through_a_slow_await() -> None:
    """The spinner animates while the awaited work runs, not only before and after it.

    The animation uses the same event loop as the request that it reports on. Thus a call
    that blocks the loop stops the animation. For this reason, MeshTerm runs the slow parts
    of a device read (the list of serial ports, the read of the history) in a thread.
    """
    from meshterm.ui.tui.screen import BusyScreen

    with create_pipe_input() as inp:
        session = TuiSession(input=inp, output=DummyOutput())
        frames: list[str] = []

        async def work() -> str:
            screen = session.top
            assert isinstance(screen, BusyScreen)
            for _ in range(6):  # Span more than one spin, and do not block the loop.
                await asyncio.sleep(spinner_interval())
                frames.append(screen._spinner.frame)
            return "ok"

        async def main() -> None:
            await session.busy_startup("checking…", work(), interval=spinner_interval() / 4)

        await asyncio.wait_for(session.run(main()), timeout=15)
    assert len(set(frames)) > 1, f"the spinner never advanced: {frames}"


async def test_session_text_dispatches_typed_keys() -> None:
    """A text prompt takes the typed characters and commits on Enter, end to end."""
    with create_pipe_input() as inp:
        session = TuiSession(input=inp, output=DummyOutput())
        captured = {}

        async def main() -> None:
            captured["value"] = await session.text("name?")

        inp.send_text("hi\r")
        await asyncio.wait_for(session.run(main()), timeout=5)
    assert captured["value"] == "hi"


def test_session_stack_and_float_selection() -> None:
    """The stack tracks the top and the base, and floats only a deeper screen over its parent."""
    session = TuiSession()
    assert session.top is None
    base = ScrollScreen(Text("base"), title="base")
    session.push(base)
    assert session.top is base
    assert session._base_screen() is base
    assert not session._has_float()  # There is one screen, so there is no float.

    dialog = SelectScreen("pick", [Choice("a", 1)])
    session.push(dialog)
    assert session._has_float()  # A deeper screen floats.
    assert session._base_screen() is base  # The base is the parent under the dialog.
    session.pop(dialog)
    assert session.top is base
    assert not session._has_float()


def test_session_stacks_every_dialog_over_one_full_frame_background() -> None:
    """A dialog over a dialog: both float over the one background, and neither is full-frame.

    The old compositor drew only the top float. It rendered the second screen from the top
    as the full-frame base. Thus, when a confirm opened over a dialog, that dialog became
    as large as the frame. Now the background is the deepest full-frame screen, and each
    floating layer above it stays its own box at the centre.
    """
    session = TuiSession()
    channels = SelectScreen("Channels", [Choice("Ops", 1)])  # The floating list of a tool.
    detail = SelectScreen("Ops (private)", [Choice("Clear", "clr")])  # Its item dialog.
    confirm = ButtonDialog("Clear Ops?", [("Cancel", 0), ("Clear", 1)], border_style="err")
    for screen in (channels, detail, confirm):
        session.push(screen)

    # The bottom screen (all the screens float) is the one full-frame background. The detail
    # dialog and the confirm both float over it. The code does not make the detail the base.
    assert session._base_screen() is channels
    assert session._float_layers() == [detail, confirm]

    session.pop(confirm)
    assert session._float_layers() == [detail]  # The detail stays a float. It is not full-frame.
    assert session._base_screen() is channels


def test_session_background_is_the_topmost_full_frame_screen() -> None:
    """A screen that does not float (a map, a scroll screen) is the background under dialogs."""
    session = TuiSession()
    full = ScrollScreen(Text("map"), title="map")  # floating=False
    dialog = ButtonDialog("go?", [("No", 0), ("Yes", 1)])
    session.push(full)
    session.push(dialog)
    assert session._base_screen() is full
    assert session._float_layers() == [dialog]


def test_dispatch_promotes_nav_actions_while_right_ctrl_is_held(monkeypatch) -> None:
    """A bare navigation key becomes its Ctrl chord while the user holds right Ctrl.

    This is the rescue for keyboard layouts (Canadian Multilingual Standard) that use right
    Ctrl as a modifier for characters. These layouts remove the ctrl flag from the arrow
    event of the console. Actions that have no Ctrl version pass through with no change.
    Each action also passes through with no change after the user releases the key.
    """
    from meshterm.ui.tui import session as session_mod

    session = TuiSession()
    seen: list[str] = []

    class Probe(ScrollScreen):
        def handle(self, action: str, data: str = "") -> None:
            seen.append(action)

    session.push(Probe(Text("x")))

    monkeypatch.setattr(session_mod, "_right_ctrl_down", lambda: True)
    session._dispatch("left")
    session._dispatch("home")
    # Enter also has a Ctrl version. A terminal cannot spell this key as a chord by itself.
    session._dispatch("enter")
    session._dispatch("escape")  # It has no Ctrl version, so it does not change while held.
    monkeypatch.setattr(session_mod, "_right_ctrl_down", lambda: False)
    session._dispatch("left")
    assert seen == ["ctrl_left", "ctrl_home", "ctrl_enter", "escape", "left"]


def test_dispatch_promotes_letter_chords_while_right_ctrl_is_held(monkeypatch) -> None:
    """A bare letter becomes its Ctrl-letter chord while the user holds right Ctrl.

    This is the same rescue as for the navigation keys. It is for the ^R and ^P shortcuts.
    If a keyboard layout uses right Ctrl, the layout changes these shortcuts to plain text.
    Only the letters in the map change. The code makes them lower case and removes the old
    data. Other text stays text, and each letter stays text after the user releases the key.
    """
    from meshterm.ui.tui import session as session_mod

    session = TuiSession()
    seen: list[tuple[str, str]] = []

    class Probe(ScrollScreen):
        def handle(self, action: str, data: str = "") -> None:
            seen.append((action, data))

    session.push(Probe(Text("x")))

    monkeypatch.setattr(session_mod, "_right_ctrl_down", lambda: True)
    session._dispatch("text", "r")  # ^R retry.
    session._dispatch("text", "P")  # ^P paths. The code makes the letter lower case.
    session._dispatch("text", "x")  # This letter is not in the map, so it stays text while held.
    monkeypatch.setattr(session_mod, "_right_ctrl_down", lambda: False)
    session._dispatch("text", "r")  # The user released the key, so this is plain text again.
    assert seen == [("retry", ""), ("paths", ""), ("text", "x"), ("text", "r")]


def test_right_ctrl_rescue_covers_the_sessions_own_chords(monkeypatch) -> None:
    """The right-Ctrl rescue reaches the chords of the session itself, not only screen actions.

    The session itself answers ^V and ^C. The rescue must reach them too, for a keyboard
    layout that uses right Ctrl. Right Ctrl-V pastes the clipboard into a compose line and
    does not type a ``v``. Right Ctrl-C quits. The session never sends either
    pseudo-action to a screen.
    """
    from meshterm.ui.tui import session as session_mod

    session = TuiSession()
    seen: list[tuple[str, str]] = []

    class Probe(ScrollScreen):
        def handle(self, action: str, data: str = "") -> None:
            seen.append((action, data))

    class FakeApp:
        def __init__(self) -> None:
            self.exited = False
            self.is_running = True

        def exit(self) -> None:
            self.exited = True

        def invalidate(self) -> None:
            pass

    session.push(Probe(Text("x")))
    session._app = FakeApp()
    monkeypatch.setattr(session_mod, "_read_clipboard", lambda: "pasted")

    monkeypatch.setattr(session_mod, "_right_ctrl_down", lambda: True)
    session._dispatch("text", "v")  # ^V: the clipboard reaches the screen as a paste.
    assert seen == [("paste", "pasted")]
    session._dispatch("text", "c")  # ^C: the app quits, and nothing reaches the screen.
    assert seen == [("paste", "pasted")]
    assert session._app.exited is True

    monkeypatch.setattr(session_mod, "_right_ctrl_down", lambda: False)
    session._dispatch("text", "v")  # The user released the key: this is a typed character again.
    assert seen[-1] == ("text", "v")


def test_every_ctrl_letter_chord_is_bound_on_both_ctrl_keys() -> None:
    """Each Ctrl-letter chord is bound on both Ctrl keys.

    The chord table sets the prompt_toolkit bindings. Thus a chord is never bound for the
    left Ctrl without its right-Ctrl rescue. The two tables could once differ, when a
    person maintained the letter map by hand.
    """
    from prompt_toolkit.keys import Keys

    from meshterm.ui.tui.session import _CTRL_LETTER_CHORDS, _KEY_ACTIONS

    for letter, action in _CTRL_LETTER_CHORDS.items():
        key = getattr(Keys, f"Control{letter.upper()}")
        assert _KEY_ACTIONS[key] == action
    # The terminal spells Enter, Tab, and Backspace as c-m, c-i, and c-h. If the table used
    # those letters, it would bind these keys again.
    assert not {"m", "i", "h"} & set(_CTRL_LETTER_CHORDS)
    assert _KEY_ACTIONS[Keys.Enter] == "enter"


def test_wide_glyph_detection_flags_emoji_not_marks() -> None:
    """The wide-glyph check flags emoji, and does not flag the marks of the app.

    The desync comes only from a glyph of width 2 that the terminal can draw narrower.
    The node-type marks, the status marks, and the chart braille have width 1 on each
    terminal. Thus they must not cause the check to flag them. We once thought that they
    did, and this was a wrong diagnosis.
    """
    from meshterm.ui.tui.session import _has_wide_glyph

    assert _has_wide_glyph("👋")
    assert _has_wide_glyph("Bob 👋 waved")
    assert _has_wide_glyph("clock 🕒 sync")
    assert _has_wide_glyph("⚡ explore")  # An icon of width 2 also counts.
    assert not _has_wide_glyph("plain ascii row")
    assert not _has_wide_glyph("★ ▲ ● ■ ◉ ○")  # Node-type marks: width 1.
    assert not _has_wide_glyph("⠿⣿⡇ chart")  # Braille: width 1.
    assert not _has_wide_glyph("✓ ✗ ⚠ … done")  # Status marks: width 1.


def _repaint_harness():
    """A session that uses a fake pt app, and its remembered frame of 3 rows of ``A`` cells."""
    import types

    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.layout.screen import Char
    from prompt_toolkit.layout.screen import Screen as PtScreen

    remembered = PtScreen()
    for row in range(3):
        for x in range(10):
            remembered.data_buffer[row][x] = Char("A")
    session = TuiSession()
    session._app = types.SimpleNamespace(
        renderer=types.SimpleNamespace(_last_screen=remembered),
        output=types.SimpleNamespace(get_size=lambda: Size(rows=10, columns=60)),
        invalidate=lambda: None,
    )
    return session, remembered


def _row_text(screen, row: int) -> str:
    """A row of the remembered frame as plain characters. The sentinel of the scrub shows."""
    return "".join(screen.data_buffer[row][x].char for x in range(10))


def test_a_wide_glyph_frame_upgrades_to_a_full_repaint() -> None:
    """A frame that has a wide glyph changes the next paint to a full paint.

    prompt_toolkit paints only the differences, with a relative cursor. This is correct
    only while each glyph is one cell wide. The terminal can draw a glyph of width 2 (an
    emoji in a chat line) in one cell. Then the cursor model of the row is wrong. A later
    paint skips the emoji, because it did not change, and it leaves old cells to the right
    of it. For such a frame, MeshTerm drops the cached frame of pt. There is then no
    remembered frame to compare against, so pt does an erase_down and a new draw. A frame
    that has only glyphs of width 1 keeps the fast paint of the differences. This is
    true for a glyph in any place, in a floating dialog or not.
    """
    session, _remembered = _repaint_harness()
    session._emit("Bob 👋 says hi")
    assert session._app.renderer._last_screen is None

    # A frame that has only glyphs of width 1 (plain text, node marks, chart braille) keeps
    # the efficient paint of the differences.
    session, remembered = _repaint_harness()
    session._emit("★ you  ▲ repeater  ● node  ⠿ chart")
    assert session._app.renderer._last_screen is remembered
    session._emit("★ you  ▲ repeater  ● node  ⠿ chart · moved")  # It changed, but all width 1.
    assert session._app.renderer._last_screen is remembered
    assert _row_text(remembered, 0) == "A" * 10  # MeshTerm also scrubbed nothing.


def test_only_the_rows_that_changed_are_repainted() -> None:
    """A header that ticks paints the header again, and does not paint the screen under it.

    A wide glyph makes this necessary: MeshTerm must write its row whole again, from
    column 0. pt goes down one row with a carriage return, so the wrong alignment cannot
    reach the rows below. The background has one line for each terminal row. Thus the rows
    that changed are exactly the rows that MeshTerm must write again. An idle frame changes
    none of them. The 1 Hz refresh once caused flicker because it did not do this.
    """
    session, remembered = _repaint_harness()
    frame_1 = "🎯 Farthest node\nrow one\nrow two"
    session._emit(frame_1)
    assert session._app.renderer._last_screen is None  # There is nothing to compare against yet.

    session._app.renderer._last_screen = remembered
    session._emit(frame_1)  # The timer tick: it is the same frame, so nothing is touched.
    assert session._app.renderer._last_screen is remembered
    assert [_row_text(remembered, y) for y in range(3)] == ["A" * 10] * 3

    session._emit("🎯 Farthest node\nrow one changed\nrow two")
    assert session._app.renderer._last_screen is remembered  # No erase and no full draw.
    assert _row_text(remembered, 1) == "￿" * 10  # The one row that changed, marked whole.
    assert _row_text(remembered, 0) == "A" * 10  # The rows around it stay as they were.
    assert _row_text(remembered, 2) == "A" * 10

    # A frame that has a different height has no row mapping to trust, so MeshTerm paints
    # everything again.
    session._emit("🎯 Farthest node\nrow one changed")
    assert session._app.renderer._last_screen is None


def test_a_changed_layer_repaints_over_a_still_wide_glyph_frame() -> None:
    """A change in any layer upgrades the paint, while a wide glyph is drawn in any place.

    A plain dialog that moves over a base row with an emoji is written again from a model
    of that row, and the terminal does not agree with the model. Thus the whole frame
    decides, and not the layer that changed. A layer that leaves is also a change. A
    dialog that closes only stops rendering. If the paint did not change, MeshTerm would
    write the cells that the dialog gives back to the base again, in parts.
    """
    import types

    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.layout.screen import Screen as PtScreen

    session = TuiSession()
    remembered = PtScreen()
    session._app = types.SimpleNamespace(
        renderer=types.SimpleNamespace(_last_screen=remembered),
        output=types.SimpleNamespace(get_size=lambda: Size(rows=10, columns=60)),
        invalidate=lambda: None,
    )
    session._emit("🎯 the board behind", "base")  # A wide glyph on the background.
    session._app.renderer._last_screen = remembered
    session._emit("plain dialog, frame 1", "float0")  # It has no emoji of its own...
    assert session._app.renderer._last_screen is None  # ...but the frame has one.

    session._app.renderer._last_screen = remembered
    session._emit("plain dialog, frame 1", "float0")  # It did not change, so MeshTerm leaves it.
    assert session._app.renderer._last_screen is remembered

    # The dialog closes. One screen on the stack means no float layer in this paint, so the
    # reconcile drops the layer. The base with the emoji is still drawn, so MeshTerm paints
    # the cells that the layer gives back whole, and not in parts.
    session.push(Screen())
    session._app.renderer._last_screen = remembered
    session._reconcile_layers()
    assert "float0" not in session._layers
    assert session._app.renderer._last_screen is None

    # If no wide glyph is drawn, a layer that leaves causes no new paint.
    session._layers.clear()
    session._emit("plain base", "base")
    session._emit("plain dialog", "float0")
    session._app.renderer._last_screen = remembered
    session._reconcile_layers()
    assert session._app.renderer._last_screen is remembered


def test_floating_text_prompt_is_a_popup_over_a_blank_base() -> None:
    """``text(floating=True)`` floats as a dialog at the centre, also on an empty stack.

    A modal in the middle of a flow must float like the button dialogs. One example is a
    remote-admin password, between the node picker and the admin menu. Thus MeshTerm puts a
    blank base under it. The prompt must not fill the frame, as the first entry screen of
    a tool does.
    """
    session = TuiSession()

    async def main() -> None:
        task = asyncio.ensure_future(session.text("Password", password=True, floating=True))
        for _ in range(5):
            await asyncio.sleep(0)
            if session._has_float():
                break
        floats = session._float_layers()
        assert len(floats) == 1 and isinstance(floats[0], TextScreen)
        assert session._base_screen() is not floats[0]  # A blank base is under it.

        floats[0].resolve("hunter2")
        assert await task == "hunter2"
        assert session._stack == []  # MeshTerm removes the blank base with the prompt.

    asyncio.run(main())


def test_default_text_prompt_is_the_full_frame_base() -> None:
    """A default ``text`` prompt on an empty stack is the frame. It is the first entry of a tool.

    The typed fallback of the Trace target takes the place of the select picker. Thus it
    fills the frame. It does not float over a blank base. The floating dialog is optional
    (refer to the test above).
    """
    session = TuiSession()

    async def main() -> None:
        task = asyncio.ensure_future(session.text("Target node"))
        for _ in range(5):
            await asyncio.sleep(0)
            if session.top is not None:
                break
        assert isinstance(session.top, TextScreen)
        assert not session._has_float()  # There is no float. The prompt is the background.
        assert session._base_screen() is session.top

        session.top.resolve("Hub")
        assert await task == "Hub"

    asyncio.run(main())


# --- busy skeleton card ------------------------------------------------------


def test_busy_overlay_renders_only_its_title_chip_and_caption() -> None:
    """A card with a title and a caption shows its heading, the working chip, and the caption.

    The card once had a Knight-Rider scanning bar under the caption (JP, 2026-08-29). Now
    the chip is the whole animation. Thus the card has two lines, and nothing moves across
    it.
    """
    import re

    from meshterm.ui.tui.overlay import BusyOverlay

    overlay = BusyOverlay("reading from Waymarker…", title="Nodes", fade=0.0)  # Bright at once.
    ansi = overlay.render()
    assert "Nodes" in ansi  # The heading that names the screen that MeshTerm gets data for.
    assert "reading from Waymarker" in ansi  # The caption beside the chip.
    assert overlay.spinner.frame in ansi  # The working chip, which is one cell wide.
    plain = re.sub(r"\[[0-9;]*m", "", ansi)
    rows = [line for line in plain.splitlines() if line.strip()]
    assert len(rows) == 2  # The title and the chip line. There is no bar and no spacer row.
    assert "⠶" not in plain  # The LED lamps are removed, not only unlit.


def test_busy_overlay_chip_ticks_with_the_animation() -> None:
    """The working chip of the card is the reusable Spinner, and it advances on a tick."""
    from meshterm.ui.tui.overlay import BusyOverlay
    from meshterm.ui.tui.spinner import Spinner

    overlay = BusyOverlay()
    assert isinstance(overlay.spinner, Spinner)
    first = overlay.spinner.frame
    overlay.tick()
    assert overlay.spinner.frame != first


def test_busy_overlay_holds_black_then_fades_in() -> None:
    """The brightness is 0 during the hold. Then it climbs to full colour in the fade time."""
    from meshterm.ui.tui.overlay import BusyOverlay

    overlay = BusyOverlay(hold=0.1, fade=0.2)
    assert overlay.brightness == 0.0  # Nothing paints during the hold.
    overlay.started_at -= 0.1  # Move to the very end of the hold.
    assert overlay.brightness < 0.2  # The glow from black only starts now.
    overlay.started_at -= 0.2  # Move past the whole fade time.
    assert overlay.brightness == 1.0  # The card is fully lit.


def test_dim_color_scales_hex_toward_black() -> None:
    """The fade dimmer scales the hex channels and keeps attribute words such as ``bold``."""
    from meshterm.ui.tui.overlay import dim_color

    assert dim_color("#38bdf8", 1.0) == "#38bdf8"  # No change at full brightness.
    assert dim_color("#ffffff", 0.0) == "#000000"  # Black at zero.
    # It keeps 'bold', and halves the colour.
    assert dim_color("bold #ffffff", 0.5) == "bold #808080"


def test_overlay_fade_restarts_when_re_exposed_after_a_prompt() -> None:
    """A pop back to an empty stack plays the black hold and the fade again.

    It does not snap to full brightness.
    """
    from meshterm.ui.tui.overlay import BusyOverlay

    session = TuiSession()
    overlay = BusyOverlay()
    session._overlay = overlay
    overlay.started_at -= 10  # Act as if the intro already finished.
    assert overlay.brightness == 1.0

    screen = ScrollScreen(Text("prompt"))
    session.push(screen)  # A prompt covers the card.
    session.pop(screen)  # The prompt closes, and the card shows again on the empty stack.
    assert overlay.brightness == 0.0  # The fade restarted from black.


async def test_session_busy_overlay_shows_between_screens_and_clears() -> None:
    """The overlay floats while a block runs on an empty stack, and MeshTerm drops it after."""
    with create_pipe_input() as inp:
        session = TuiSession(input=inp, output=DummyOutput())
        seen = {}

        async def main() -> None:
            async with session.busy_overlay("working…"):
                await asyncio.sleep(0.05)  # This is still in the first hold.
                seen["hidden_during_hold"] = not session._overlay_visible()
                await asyncio.sleep(0.3)  # This is past the hold of 200 ms. The ring faded in.
                seen["active"] = session._overlay is not None
                seen["visible_empty_stack"] = session._overlay_visible()
                seen["rendered"] = bool(session._render_overlay().value.strip())
                # If a screen is on the stack, the ring stays hidden. Thus it cannot cover a
                # prompt.
                session.push(ScrollScreen(Text("prompt"), title="p"))
                seen["hidden_over_screen"] = not session._overlay_visible()
                session.pop()

        await asyncio.wait_for(session.run(main()), timeout=5)

    assert seen["hidden_during_hold"] is True
    assert seen["active"] is True
    assert seen["visible_empty_stack"] is True
    assert seen["rendered"] is True
    assert seen["hidden_over_screen"] is True
    assert session._overlay is None  # MeshTerm cleared it on exit.


# --- horizontal scroll (opt-in) -------------------------------------------------------


def _hscroll_screen(width_of_rows: int = 60) -> SelectScreen:
    from meshterm.ui.tui.select import Choice, SelectScreen, Separator

    return SelectScreen(
        "long",
        [
            Separator("HEAD-" + "h" * width_of_rows),
            Choice("row-one-" + "x" * width_of_rows + "-tail", 1),
            Choice("short", 2),
        ],
        hscroll=True,
        filterable=True,
    )


def _row_plains(screen, width: int) -> list[str]:
    import re

    return [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in screen.render_body(width)]


def test_select_hscroll_shifts_only_the_highlighted_row() -> None:
    """→ slides the highlighted row under its pinned pointer. Other rows and headers stay."""
    screen = _hscroll_screen()  # The long "row-one" is highlighted by default.
    before = _row_plains(screen, 40)
    assert any("row-one-" in ln for ln in before)
    screen.handle("right")
    shifted = _row_plains(screen, 40)
    # The head of the highlighted row scrolled off, under the pointer that is still pinned...
    assert any(ln.startswith("❯ ") for ln in shifted)
    assert not any("row-one-" in ln for ln in shifted)
    # ...but the section header did not move, and the short row did not change.
    assert any(ln.strip().startswith("HEAD-") for ln in shifted)
    assert any("short" in ln for ln in shifted)
    screen.handle("left")
    assert any("row-one-" in ln for ln in _row_plains(screen, 40))


def test_select_hscroll_clamps_at_the_highlighted_rows_tail() -> None:
    """→ stops when the end of the highlighted row is visible, not the end of the widest row."""
    screen = _hscroll_screen()
    for _ in range(50):
        screen.handle("right")
    plains = _row_plains(screen, 40)  # The render clamps the shift.
    assert any("-tail" in ln for ln in plains)  # The end of the highlighted row is visible.
    row_len = len("row-one-" + "x" * 60 + "-tail")
    # The shift clamps to the first whole step that brings the tail into the lane. The lane
    # is the width minus the pointer, minus the cell that a scrolled row uses for its left
    # cut mark. If the shift stopped on the exact cell at the right edge, a right mark
    # would promise more text.
    step = screen._HSCROLL_STEP
    assert screen._hshift == -(-(row_len - (40 - 2 - 1)) // step) * step


def test_select_hscroll_resets_when_the_highlight_moves() -> None:
    """The shift belongs to one row. If the highlight moves or the filter changes, it goes to 0."""
    screen = _hscroll_screen()
    screen.handle("right")
    assert screen._hshift > 0
    screen.handle("down")  # A move to another row drops the scroll of that row.
    assert screen._hshift == 0
    screen.handle("right")
    screen.handle("text", "r")  # A change of the filter also resets it.
    assert screen._hshift == 0


def test_select_hscroll_only_acts_on_an_overflowing_row() -> None:
    """←→ and their footer atom act only while the highlighted row is wider than the width."""
    screen = _hscroll_screen()
    screen.render_body(40)  # The long row-one is highlighted, and it is wider than 40 cells.
    assert "←→ scroll" in screen.footer_hint
    screen.handle("down")  # The short row fits, so there is nothing to scroll.
    screen.render_body(40)
    assert "←→ scroll" not in screen.footer_hint
    screen.handle("right")
    screen.render_body(40)
    assert screen._hshift == 0  # A row that fits cannot shift.


def test_select_hscroll_from_pins_the_rows_head_and_slides_only_its_run() -> None:
    """A row that declares a head block keeps it drawn while ←→ scroll all the text after it."""
    from meshterm.ui.tui.select import Choice, SelectScreen

    lanes = "#1 Aug 09  "
    screen = SelectScreen(
        "long",
        [Choice(lanes + "run-" + "y" * 60 + "-end", 1, hscroll_from=len(lanes))],
        hscroll=True,
    )
    screen.handle("right")
    row = _row_plains(screen, 40)[0]
    assert row.startswith("❯ " + lanes)  # The lanes never move...
    assert "run-" not in row  # ...while the run behind them slid off to the left.
    for _ in range(50):
        screen.handle("right")
    end = _row_plains(screen, 40)[0]
    assert end.startswith("❯ " + lanes) and "-end" in end  # The user can reach the tail.


def test_select_scrolls_for_a_row_that_pins_a_head_without_being_told() -> None:
    """A row that declares ``hscroll_from`` turns on the scrolling of its list by itself.

    The builders that lay out rows with a label and a description (for example
    :func:`~meshterm.ui.menus.menu_rows` and the main menu) never see the screen in which
    their list opens, because ``ctx.ui.select`` builds it. Thus the intent must be in the row.
    """
    from meshterm.ui.tui.select import Choice, SelectScreen

    lanes = "Send advert  "
    row = Choice(lanes + "Announce this node " + "and then some " * 6, 1, hscroll_from=len(lanes))
    screen = SelectScreen("menu", [row])  # There is no hscroll= in any place.
    screen.handle("right")
    drawn = _row_plains(screen, 40)[0]
    assert drawn.startswith("❯ " + lanes)  # The name stays pinned...
    assert "Announce this node" not in drawn  # ...and the description slid under it.
    # A list of plain rows still ignores ←→ completely.
    plain = SelectScreen("menu", [Choice("x" * 80, 1)])
    before = _row_plains(plain, 40)[0]
    plain.handle("right")
    assert _row_plains(plain, 40)[0] == before


def test_a_list_that_says_no_hscroll_is_not_overruled_by_its_rows() -> None:
    """``hscroll=False`` holds against rows that pin a head, also through a swap of rows.

    The editor pages end their lanes at the edge. Their Actions rows pin heads. The rows
    once turned on the ←→ scrolling of the whole page, in spite of the list.
    """
    from meshterm.ui.tui.select import Choice, SelectScreen

    lanes = "Send advert  "
    row = Choice(lanes + "Announce this node " + "and then some " * 6, 1, hscroll_from=len(lanes))
    screen = SelectScreen("editor", [row], hscroll=False)
    before = _row_plains(screen, 40)[0]
    screen.handle("right")
    assert _row_plains(screen, 40)[0] == before
    assert "←→" not in screen.footer_hint
    screen.replace_items([row])  # A refresh must not turn it on again.
    screen.handle("right")
    assert _row_plains(screen, 40)[0] == before


def test_menu_rows_pin_their_label_lane_so_only_the_description_slides() -> None:
    """The shared builder for a label and a description gives each row its own head block."""
    from meshterm.ui.menus import menu_rows

    rows = menu_rows(
        [("Sync clock…", "Set the device clock", 1), ("Reboot device…", "Restart the companion", 2)]
    )
    lane = rows[0].hscroll_from
    assert lane and all(row.hscroll_from == lane for row in rows)
    # The head block ends exactly where the descriptions start, on each row.
    for row in rows:
        assert row.label.plain[lane:].startswith(("Set the", "Restart the"))


def test_select_hscroll_hint_is_gated_on_the_run_not_the_whole_row() -> None:
    """A row whose run fits has no ←→ atom, for any width of its pinned head.

    The hint can advertise only a key that does something (the rule of the footer). On a
    row that pins a head block, the key moves only the run. Thus a long lane block in front
    of a short tail is not an overflow. It is only a wide row.
    """
    from meshterm.ui.tui.select import Choice, SelectScreen

    lanes = "#1 ✓ replied  min -6.0 dB  248 ms  via "
    fits = SelectScreen(
        "probe", [Choice(lanes + "3d,f2", 1, hscroll_from=len(lanes))], hscroll=True
    )
    fits.render_body(60)
    assert "←→ scroll" not in fits.footer_hint

    overflows = SelectScreen(
        "probe",
        [Choice(lanes + ",".join(["3d"] * 20), 1, hscroll_from=len(lanes))],
        hscroll=True,
    )
    overflows.render_body(60)
    assert "←→ scroll" in overflows.footer_hint


def test_select_hscroll_marks_both_edges_the_run_continues_past() -> None:
    """A scrolled row has a crack or an ellipsis at each side on which its run continues."""
    from meshterm.ui.pathline import _ELLIPSIS
    from meshterm.ui.tui.select import Choice, SelectScreen

    screen = SelectScreen("long", [Choice("z" * 200, 1)], hscroll=True)
    screen.handle("right")
    row = _row_plains(screen, 40)[0]
    # Plain prose has no chip fill to shear, so both marks are the ellipsis.
    assert row.startswith("❯ " + _ELLIPSIS) and row.endswith(_ELLIPSIS)


def test_select_without_hscroll_ignores_left_right() -> None:
    """The flag is off by default. ←/→ do nothing, and rows render as before."""
    from meshterm.ui.tui.select import Choice, SelectScreen

    screen = SelectScreen("plain", [Choice("row", 1)])
    before = screen.render_body(40)
    screen.handle("right")
    screen.handle("left")
    assert screen.render_body(40) == before and screen._hshift == 0


# --- row detail lines (opt-in) -------------------------------------------------------


def test_select_choice_detail_hangs_under_its_row() -> None:
    """A Choice.detail draws as a second line with an indent, directly under its title."""
    from meshterm.ui.tui.select import Choice, SelectScreen

    screen = SelectScreen(
        "pick", [Choice("first row", 1, detail="weakest -6.0 dB  ·  3×"), Choice("second", 2)]
    )
    lines = _row_plains(screen, 40)
    idx = next(i for i, ln in enumerate(lines) if "first row" in ln)
    assert lines[idx + 1].startswith("  weakest -6.0 dB")
    assert "second" in lines[idx + 2]


def test_select_choice_without_detail_draws_one_line() -> None:
    """A Choice with no detail (the default, or a detail that resolves empty) stays one line."""
    from meshterm.ui.tui.select import Choice, SelectScreen

    screen = SelectScreen("pick", [Choice("first row", 1), Choice("second", 2, detail="")])
    lines = _row_plains(screen, 40)
    assert lines[1].strip() == "second"


def test_select_cursor_tracks_the_highlight_past_a_detail_line() -> None:
    """The highlight is on the title line, also when an earlier row has a detail line."""
    from meshterm.ui.tui.select import Choice, SelectScreen

    screen = SelectScreen(
        "pick",
        [Choice("first row", 1, detail="has detail"), Choice("second", 2)],
        default=2,
    )
    lines = _row_plains(screen, 40)
    assert screen.cursor_line() == next(i for i, ln in enumerate(lines) if "second" in ln)


def test_select_hscroll_leaves_the_detail_line_unshifted() -> None:
    """←→ slide only the title of the highlighted row. Its detail line never scrolls."""
    from meshterm.ui.tui.select import Choice, SelectScreen

    long_title = "row-one-" + "x" * 60 + "-tail"
    screen = SelectScreen("pick", [Choice(long_title, 1, detail="weakest -6.0 dB")], hscroll=True)
    screen.handle("right")
    lines = _row_plains(screen, 40)
    assert "weakest -6.0 dB" in lines[1]


# --- the fast path's frame source ----------------------------------------------------


def _framed_session():
    """A session with enough of an app behind it to compose a frame at 53x26."""
    import types

    from prompt_toolkit.data_structures import Size

    session = TuiSession()
    session._app = types.SimpleNamespace(
        renderer=types.SimpleNamespace(_last_screen=None),
        output=types.SimpleNamespace(get_size=lambda: Size(rows=26, columns=53)),
        invalidate=lambda: None,
    )
    return session


def test_a_dialog_frame_is_composed_here_not_handed_to_prompt_toolkit() -> None:
    """A float once sent the whole frame to the renderer of prompt_toolkit.

    This caused a full rebuild of the grid, and a diff, on each key press. It happened in
    each confirm, picker, and viewer in the app. The placement of the float is
    reproducible (a float with no anchor and no size is centred). Thus the frame source
    returns the finished picture with the box merged in. The diff of the rows applies to
    dialogs, as it does to the other screens.
    """
    session = _framed_session()
    session.push(ScrollScreen(Text("the list beneath"), title="Nodes", floating=False))
    session.push(ButtonDialog("Remove this contact?", [("Cancel", 0), ("Remove", 1)]))

    text = session._plain_frame()
    assert text is not None, "a dialog must not fall back to prompt_toolkit"
    rows = text.split("\n")
    assert len(rows) == 26
    body = "\n".join(rows)
    assert "Remove this contact?" in _plain(body)
    assert "the list beneath" in _plain(body), "the backdrop must show around the box"


def test_a_bare_base_paints_no_chrome_at_all() -> None:
    """The session draws a bare base through compose_bare, with no bars, no lane, and no box."""
    screen = ScrollScreen(Text("CODE", justify="center"), title="Share x", floating=False)
    screen.bare = True
    session = _framed_session()
    session.push(screen)
    text = session._plain_frame()
    assert text is not None
    rows = text.split("\n")
    assert len(rows) == 26
    plain = _plain(text)
    assert "CODE" in plain
    for chrome in ("MeshTerm", "Share x", "Esc", "F1", "─", "│"):
        assert chrome not in plain, f"a bare frame drew {chrome!r}"


def test_the_busy_overlay_is_still_prompt_toolkits_to_place() -> None:
    """This is the one float that MeshTerm does not place.

    It is a window that has the size of its content, and the float container measures it.
    """
    from meshterm.ui.tui.overlay import BusyOverlay

    session = _framed_session()
    assert session._plain_frame() is None  # An empty stack has no background either.
    overlay = BusyOverlay(title="Starting up")
    overlay.started_at -= overlay.hold + overlay.fade  # This is past the hold, so it shows.
    session._overlay = overlay
    assert session._overlay_visible()
    assert session._plain_frame() is None


def test_the_rows_a_dialog_does_not_reach_come_back_unchanged() -> None:
    """The rows that a dialog does not cover do not change, so the diff skips them.

    This is the reason to composite the frame.
    """
    session = _framed_session()
    session.push(
        ScrollScreen(Text("\n".join(f"line {i}" for i in range(40))), title="Nodes", floating=False)
    )
    first = session._plain_frame().split("\n")
    session.push(ButtonDialog("Sure?", [("Cancel", 0), ("Yes", 1)]))
    second = session._plain_frame().split("\n")

    unchanged = sum(1 for a, b in zip(first, second, strict=True) if a == b)
    assert unchanged >= 10, "the box should only rewrite the rows it covers"


# --- the body is drawn one viewport at a time ----------------------------------------


def test_a_long_list_only_rasterizes_the_rows_the_viewport_shows() -> None:
    """A list of 141 contacts that is 150 rows tall shows only twenty of them.

    The render of the other rows was a waste, on each key press and on the tick that comes
    one time each second. The line count must stay exact. The scroll clamp, the ``↑↓ more``
    markers, and the offsets of the sticky headers use it.
    """
    from meshterm.ui.tui.screen import LazyLines

    drawn: list[int] = []

    def title(i: int):
        def build():
            drawn.append(i)
            return f"contact {i:03d}"

        return build

    screen = SelectScreen("Contacts", [Choice(title(i), i) for i in range(150)])
    lines = screen.render_body(53)

    assert isinstance(lines, LazyLines)
    assert len(lines) == 150  # The count is exact, and it costs nothing.
    assert drawn == [], "no row is rasterized until something reads it"

    window = lines[0:20]
    assert len(window) == 20
    assert sorted(drawn) == list(range(20))
    assert "contact 000" in _plain(window[0])

    lines[5]  # A second read uses the memo and never renders again.
    assert sorted(drawn) == list(range(20))


def test_a_lazy_body_slices_frames_and_scrolls_exactly_as_a_list_did() -> None:
    """The frame must not see the substitution: it has the same height and the same clip flags.

    If MeshTerm defers the drawing of a row, it must never defer the count of the rows.
    The scroll clamp, the ``↑↓ more`` subtitle, and the pinned heading use that count.
    If the render did not build the tail, each of them would be wrong.
    """
    items = [section_heading("Group")]
    items += [Choice(f"row {i}", i) for i in range(60)]
    screen = SelectScreen("Long", items, footer_hint="Esc back")
    screen.floating = False

    out = frame.compose_base(Text("hdr"), screen, "Esc back", 53, 26)
    assert len(out.split("\n")) == 26
    plain = _plain(out)
    assert "row 0" in plain and "↓ more" in plain
    assert screen._scroll_total == 61  # The screen still measures the whole body.

    for _ in range(45):  # Walk the highlight past the fold.
        screen.handle("down")
    plain = _plain(frame.compose_base(Text("hdr"), screen, "Esc back", 53, 26))
    assert "row 45" in plain, "the highlight must still be kept in view"
    assert "row 0 " not in plain, "the top of the list should have scrolled away"
    assert "── Group ──" in plain, "the section heading pins once it scrolls off"


def test_an_off_screen_rows_live_title_is_left_alone() -> None:
    """MeshTerm resolves a callable title of a row only while the row is on the screen."""
    calls: list[str] = []
    items = [
        Choice(lambda: (calls.append("top"), "top row")[1], 0),
        *[Choice(f"filler {i}", i) for i in range(1, 40)],
        Choice(lambda: (calls.append("bottom"), "bottom row")[1], 40),
    ]
    screen = SelectScreen("Live", items)
    screen.floating = False
    lines = screen.render_body(53)
    lines[0:10]
    assert calls == ["top"]


def _button_dialog_hints() -> list[tuple[str, int, int | None, str]]:
    """Each literal ``footer_hint=`` on a button dialog, as (file, line, buttons, hint).

    ``buttons`` is the count when the call passes a list literal. Otherwise it is ``None``.
    A computed row can have any number of buttons. For example, the quit confirm has a
    third button on a bonded device. Thus the test applies the rule for many buttons to it.
    """
    import ast
    from pathlib import Path

    found: list[tuple[str, int, int | None, str]] = []
    root = Path(__file__).resolve().parent.parent / "meshterm"
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in ("button_dialog", "ButtonDialog"):
                continue
            hint = next(
                (
                    k.value.value
                    for k in node.keywords
                    if k.arg == "footer_hint" and isinstance(k.value, ast.Constant)
                ),
                None,
            )
            if hint is None:
                continue  # This is the default hint, which is the generic hint.
            row = node.args[1] if len(node.args) > 1 else None
            count = len(row.elts) if isinstance(row, ast.List) else None
            found.append((path.name, node.lineno, count, hint))
    return found


def test_a_button_dialogs_hint_never_names_the_verb_of_one_button() -> None:
    """Enter commits the highlighted button, so a row of buttons can say only ``select``.

    The quit confirm once had the hint ``Enter quit · Esc cancel``. This was true only
    while the Quit button was highlighted. ←→ move the highlight, and on a bonded device a
    third button is between the two (JP, 2026-08-31). The same hint also did not name the
    key that changes what Enter does. A list from which the user selects a row can name
    its committing verb (``Enter adopt path``), because each row commits in the same way.
    A row of buttons cannot do this.

    A dialog that has one button is the exception for which the rule is made. A lone
    acknowledgement has nothing to choose between. Thus it names its verb (``Enter OK``),
    and it does not advertise ←→, which would move nothing.
    """
    for where, line, buttons, hint in _button_dialog_hints():
        if buttons == 1:
            assert "←→" not in hint, f"{where}:{line} offers ←→ over a single button"
            continue
        assert "Enter select" in hint, (
            f"{where}:{line} promises {hint!r} — Enter commits whichever button is "
            "highlighted, so the atom stays 'Enter select'"
        )
        assert "←→" in hint, (
            f"{where}:{line} hides ←→, the key that moves the highlight Enter commits"
        )


# --- hiding devices on the splash --------------------------------------------


def _hide_store(tmp_path):  # noqa: ANN001, ANN202
    """A device registry in a scratch file."""
    from meshterm.core.device_store import DeviceStore

    return DeviceStore(tmp_path / "devices.json")


def test_hidden_devices_survive_the_session_that_hid_them(tmp_path) -> None:  # noqa: ANN001
    """MeshTerm writes the hidden state to the registry file, so a new store still has it."""
    store = _hide_store(tmp_path)
    assert store.hidden_ids() == set()

    store.hide("usb-0001")
    store.hide("usb-0002")
    store.hide("usb-0001")  # A second call has the same result.
    assert _hide_store(tmp_path).hidden_ids() == {"usb-0001", "usb-0002"}

    assert _hide_store(tmp_path).show_all() == 2
    assert _hide_store(tmp_path).hidden_ids() == set()
    assert _hide_store(tmp_path).show_all() == 0  # Nothing is left to show again.


def test_hiding_leaves_the_registry_and_its_default_alone(tmp_path) -> None:  # noqa: ANN001
    """A hidden device is still remembered, still the default, and the user can reach it by name.

    To hide a device is a choice about the list on one screen. Each code path that finds a
    device without the splash (``--port``, a profile, the reconnect) must not see the
    difference.
    """
    from meshterm.core.discovery import serial_device

    store = _hide_store(tmp_path)
    device = serial_device("COM7", name="A Radio")
    store.remember(device, node_name="Wardriver")
    store.hide(device.stable_id)

    fresh = _hide_store(tmp_path)
    assert fresh.hidden_ids() == {device.stable_id}
    assert fresh.is_known(device)
    remembered = fresh.load()
    assert remembered is not None and remembered.node_name == "Wardriver"


def test_connecting_to_a_hidden_device_shows_it_again(tmp_path) -> None:  # noqa: ANN001
    """If the user confirms a device, this shows that the device belongs on the list."""
    from meshterm.core.discovery import serial_device

    store = _hide_store(tmp_path)
    device = serial_device("COM7", name="A Radio")
    store.hide(device.stable_id)
    store.remember(device, node_name="Wardriver")
    assert store.hidden_ids() == set()


async def test_the_splash_hides_the_highlighted_device_and_redraws(tmp_path) -> None:  # noqa: ANN001
    """The h key removes the row and remembers the device. The new list is the feedback."""
    from meshterm.core.discovery import serial_device
    from meshterm.ui.device_picker import prompt_device
    from meshterm.ui.tui import Choice, KeyRequest

    probe = serial_device("COM3", name="A Debug Probe")
    radio = serial_device("COM7", name="A Radio")
    store = _hide_store(tmp_path)
    drawn: list[list] = []

    class _Ui:
        """Presses h on the probe, then selects the device that is left."""

        def __init__(self) -> None:
            self.round = 0

        async def select_startup(self, title, items, **kw):  # noqa: ANN001, ANN003, ANN201
            drawn.append([it for it in items if isinstance(it, Choice)])
            self.round += 1
            if self.round == 1:
                return KeyRequest(kw["keys"]["h"], probe)
            return radio

        async def busy_startup(self, message, coro, **kw):  # noqa: ANN001, ANN003, ANN201
            return await coro

    async def verify(device, pin):  # noqa: ANN001, ANN202
        return {"name": "Wardriver"}

    chosen = await prompt_device(_Ui(), [probe, radio], store, verify)
    assert chosen is radio
    assert store.hidden_ids() == {probe.stable_id}
    # The first pass offered both devices. The second pass offered only the device that is left.
    assert probe in [row.value for row in drawn[0]]
    assert probe not in [row.value for row in drawn[1]]


def test_the_splash_says_so_when_it_is_empty_only_because_of_hiding() -> None:
    """The splash says why it is empty, if the only reason is the hidden devices.

    "Nothing detected" would be false, and with no rows there is no footer atom either.
    """
    from meshterm.core.discovery import serial_device
    from meshterm.ui.device_picker import _build_items

    lines = [str(it.title) for it in _build_items([], None, {}, hidden=2)]
    assert any("2 devices hidden" in line and "⇧H" in line for line in lines)

    detected = [str(it.title) for it in _build_items([], None, {}, hidden=0)]
    assert any("no companion devices detected" in line for line in detected)

    # If the list still has a device, the footer names ⇧H on each row. Then the line would
    # use a row of the box to say it two times.
    radio = serial_device("COM7", name="A Radio")
    rows = [str(it.title) for it in _build_items([radio], None, {}, hidden=2)]
    assert not any("hidden" in line for line in rows)


def test_the_splash_action_rows_share_one_icon_column() -> None:
    """The words of "Add a network device" and "Quit" start in the same cell, on both platforms.

    The test measures the cell. The rows were once the literal strings ``"  🌐 Add…"`` and
    ``"  🚪 Quit"``. They lined up only because the two emoji have the same width. They also
    kept their icons on the PicoCalc, where each other command row removes its icon lane.
    """
    from rich.cells import cell_len

    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
    from meshterm.ui.device_picker import _ADD_TCP, _QUIT, _action_rows
    from meshterm.ui.tui import Choice

    def starts() -> dict:
        rows = {it.value: it.title.plain for it in _action_rows() if isinstance(it, Choice)}
        words = {_ADD_TCP: "Add a network device…", _QUIT: "Quit"}
        return {
            value: cell_len(rows[value][: rows[value].index(word)]) for value, word in words.items()
        }

    assert len(set(starts().values())) == 1
    set_platform(PICOCALC_LYRA)
    try:
        assert set(starts().values()) == {2}, "the icons go, and no padding stays behind"
    finally:
        set_platform(REGULAR)


def test_hiding_a_device_leaves_the_highlight_where_the_row_was() -> None:
    """The highlight goes to the next device down, or to the device above at the end.

    Thus a run of adapters clears in one run.
    """
    from meshterm.core.discovery import serial_device
    from meshterm.ui.device_picker import _after_hiding

    first, middle, last = (serial_device(f"COM{n}", name=f"D{n}") for n in (1, 2, 3))
    listed = [first, middle, last]

    assert _after_hiding(listed, first) is middle
    assert _after_hiding(listed, middle) is last
    # At the end there is no place further down, so the highlight steps up. It does not roll
    # round to the top, because nothing in the app rolls over.
    assert _after_hiding(listed, last) is middle
    assert _after_hiding([first], first) is None  # A list that is now empty has no row to select.


def test_the_splash_names_a_shortcut_only_where_it_would_act() -> None:
    """The hint names a shortcut only where it acts. This is the rule of Del remove.

    The h key is named on a device row only. ⇧H is named only while a device is hidden.
    """
    from meshterm.core.discovery import serial_device
    from meshterm.ui.device_picker import _ADD_TCP, _QUIT, _shortcut_hint

    radio = serial_device("COM7", name="A Radio")

    nothing_hidden = _shortcut_hint(0)
    # The hint does not offer a way back, because no device is hidden.
    assert nothing_hidden(radio) == "h hide"
    assert nothing_hidden(_ADD_TCP) == ""  # There is nothing to hide on the action rows.
    assert nothing_hidden(_QUIT) == ""
    assert nothing_hidden(None) == ""  # An empty list has no highlight.

    with_hidden = _shortcut_hint(2)
    assert with_hidden(radio) == "h hide · ⇧H show all"
    # ⇧H acts on the whole screen, so the hint shows it on each row that has the highlight.
    assert with_hidden(_QUIT) == "⇧H show all"


def test_a_hint_too_long_for_its_box_drops_atoms_rather_than_its_tail() -> None:
    """A hint that is too long for its box drops atoms. It does not drop its tail.

    A cut at the edge would remove Esc, and Esc is the one atom that must stay.
    """
    from meshterm.ui.tui.frame import fit_hint

    full = "↑↓ move · ←→ scroll · Enter select · h hide · ⇧H show all · Esc quit"
    assert fit_hint(full, 100) == full  # There is room for everything, so nothing changes.

    # The function drops atoms from the right, in front of Esc. It never drops Esc.
    assert fit_hint(full, 56) == "↑↓ move · ←→ scroll · Enter select · h hide · Esc quit"
    assert fit_hint(full, 45) == "↑↓ move · ←→ scroll · Enter select · Esc quit"
    assert fit_hint(full, 20).endswith("Esc quit")

    # The exception: the caller names the atoms that it can spare first, in the order in
    # which it can spare them. The move atom already names the scroll keys. Of the two hide
    # keys, the key to keep is the way back.
    spared = fit_hint(full, 56, shed_first=("←→ scroll", "h hide"))
    assert spared == "↑↓ move · Enter select · h hide · ⇧H show all · Esc quit"
    assert (
        fit_hint(full, 50, shed_first=("←→ scroll", "h hide"))
        == "↑↓ move · Enter select · ⇧H show all · Esc quit"
    )


def test_a_panning_list_slides_its_header_and_rows_as_one() -> None:
    """←→ pan a table. The header and each row move by one shift, and the shift stays.

    The shift stays when the highlight moves. The scroll for one row slides the
    highlighted row alone, and it drops its shift on ↑↓. This is correct for a list of
    long lines that are independent. It is wrong for a table. The lanes of a table have
    no meaning when they slide out from under their labels. A row that is not part of the
    table (an action row under it) stays where it is.
    """
    from meshterm.ui.tui import Choice, SelectScreen, Separator

    # A header has the same layout as a row: its first two cells are the pointer column.
    header = Separator("  " + "NAME".ljust(10) + "-" * 30 + " WHERE", pans=True)
    rows = [
        Choice("alpha".ljust(10) + "x" * 30 + " far-a", "a", pans=True),
        Choice("beta".ljust(10) + "y" * 30 + " far-b", "b", pans=True),
    ]
    quit_row = Choice("Quit", "q")
    screen = SelectScreen("t", [header, *rows, Separator(" "), quit_row], filterable=False)
    width = 30

    def lines() -> list[str]:
        return _plain(screen.render_body(width)).split("\n")

    first = lines()
    assert "←→ scroll" in screen.footer_hint  # The table overflows, so the hint names ←→.
    screen.handle("right")
    screen.handle("right")
    panned = lines()
    assert panned[0] != first[0] and panned[1] != first[1] and panned[2] != first[2]
    # Each line of the table moved by the same amount. The run of dashes in the header
    # starts in the same column as the runs of x and y under it.
    starts = [line.index(ch) for line, ch in zip(panned[:3], "-xy", strict=True)]
    assert len(set(starts)) == 1, panned
    assert panned[-1] == first[-1], "the Quit row is not part of the table"

    screen.handle("down")
    assert lines()[0] == panned[0], "moving the highlight keeps the pan"

    for _ in range(20):
        screen.handle("right")
    end = lines()
    # The pan clamps at the tail of the widest line, with each label over its lane.
    assert end[0].rstrip().endswith("WHERE") and end[1].rstrip().endswith("far-a")
    assert end[0].index("WHERE") == end[1].index("far-a") == end[2].index("far-b")
    assert all(cell_len(line) <= width for line in end)


def test_the_splash_pans_its_table_on_every_platform() -> None:
    """The header and the device rows of the splash pan together, and its action rows stay."""
    from meshterm.core.device_store import RememberedDevice
    from meshterm.core.discovery import serial_device
    from meshterm.platforms import CARDPUTER_ZERO, PICOCALC_LYRA, REGULAR, set_platform
    from meshterm.ui.device_picker import _build_items
    from meshterm.ui.tui import Choice, SelectScreen, Separator

    radio = serial_device("COM7", name="Wardriver")
    registry = {
        radio.stable_id: RememberedDevice(
            stable_id=radio.stable_id,
            port="COM7",
            label="COM7",
            last_connected="2026-09-01T00:00:00+00:00",
            node_name="Wardriver",
            hardware_model="Seeed Wio Tracker 1110 Development Kit",
        )
    }
    try:
        for platform in (REGULAR, PICOCALC_LYRA, CARDPUTER_ZERO):
            set_platform(platform)
            items = _build_items([radio], registry[radio.stable_id], registry)
            table = [it for it in items if getattr(it, "pans", False)]
            assert isinstance(table[0], Separator) and table[0].heading
            assert [it.value for it in table[1:]] == [radio]
            assert not any(
                it.pans for it in items if isinstance(it, Choice) and it.value is not radio
            ), "Add a network device and Quit are not table rows"

            width = platform.readable_cols - platform.dialog_margin - 4
            screen = SelectScreen("Select a companion device", items, filterable=False)
            before = _plain(screen.render_body(width)).split("\n")
            for _ in range(20):
                screen.handle("right")
            after = _plain(screen.render_body(width)).split("\n")
            assert "Wardriver" in before[1] and "Wardriver" not in after[1], platform.name
            # After a pan to the end, the address is under its label.
            assert after[0].index("PORT") == after[1].index("COM7"), platform.name
    finally:
        set_platform(REGULAR)


def test_the_splash_hint_stays_inside_the_box_it_is_drawn_in() -> None:
    """The splash that has no chrome keeps its hint in its own border, on both platforms.

    The width of the border is the width of the terminal minus the gutter over which the
    box floats. It is not the full readable width. On the PicoCalc it is 47 cells. Thus
    each atom here must be conditional, and this is more than tidiness.
    """
    from rich.cells import cell_len

    from meshterm.core.discovery import serial_device
    from meshterm.platforms import PICOCALC_LYRA, REGULAR
    from meshterm.ui.device_picker import _shortcut_hint
    from meshterm.ui.tui.select import splice_hint

    base = "↑↓ move · Enter select · Esc bye"  # This is the splash's own farewell.
    radio = serial_device("COM7", name="A Radio")
    common = splice_hint(base, _shortcut_hint(0)(radio))
    for platform in (REGULAR, PICOCALC_LYRA):
        budget = platform.readable_cols - platform.dialog_margin - 2
        assert cell_len(common) <= budget, (platform.name, common)


def test_a_shortcut_is_only_honoured_where_a_letter_is_free() -> None:
    """A list that filters uses its letters for the query, so it declares no shortcuts.

    If it did, the user could not type a device named "Homestead" on a list that claimed h.
    """
    from meshterm.ui.tui import Choice, KeyRequest
    from meshterm.ui.tui.select import SelectScreen

    token = object()
    fixed = SelectScreen("fixed", [Choice("a row", 1)], filterable=False, keys={"h": token})
    fixed.future = None
    resolved: list = []
    fixed.resolve = lambda value: resolved.append(value)  # type: ignore[method-assign]
    fixed.handle("text", "h")
    assert resolved == [KeyRequest(token, 1)]

    filtering = SelectScreen("filtering", [Choice("Homestead", 1)], keys={"h": token})
    filtering.resolve = lambda value: resolved.append(value)  # type: ignore[method-assign]
    filtering.handle("text", "h")
    assert len(resolved) == 1  # The list resolved nothing new.
    assert filtering._filter == "h"  # The letter went to the query, where it must go.


# -- the splash keeps looking ---------------------------------------------------------


def _drive_picker(tmp_path, monkeypatch, *, serial_rounds, ble_rounds=None, hide=None):
    """Run the rescan of the picker against a scripted sequence of scans.

    The fake UI takes the place of the open splash. It gets the ``live`` callable and runs
    it until the script ends, and it records each paint. The test replaces discovery. It
    does not use a mock at the port level, because the test examines the behaviour of the
    rescan (when it paints, and when it stays quiet) and not pyserial.
    """
    import asyncio

    from meshterm.core.device_store import DeviceStore
    from meshterm.ui import device_picker

    monkeypatch.setattr(device_picker, "_POLL_S", 0.001)
    monkeypatch.setattr(device_picker, "_BLE_EVERY_S", 0.0025)
    monkeypatch.setattr(device_picker, "_BLE_WINDOW_S", 0.0)

    serial = list(serial_rounds)
    ble = list(ble_rounds or [[]])
    calls = {"serial": 0, "ble": 0}

    def _serial():
        calls["serial"] += 1
        return list(serial[min(calls["serial"] - 1, len(serial) - 1)])

    async def _ble(_timeout):
        calls["ble"] += 1
        return list(ble[min(calls["ble"] - 1, len(ble) - 1)])

    monkeypatch.setattr(device_picker, "discover_devices", _serial)
    monkeypatch.setattr(device_picker, "discover_ble_devices", _ble)

    store = DeviceStore(tmp_path / "devices.json")
    for stable in hide or []:
        store.hide(stable)

    redraws: list = []
    # The number of scans to run: each scripted round, and enough more scans that the
    # paint of the last round has happened (a round paints before the next scan starts).
    # Also, the Bluetooth duty cycle (one listen for each few polls) must have room to show.
    scans = len(serial) + 5

    class _Ui:
        async def select_startup(  # noqa: ANN001, ANN201, ANN003
            self, title, items, *, default=None, banner=None, footnote=None, live=None, **_kw
        ):
            task = asyncio.ensure_future(live(redraws.append))
            # Wait for the scans, and not for a span of time. A fixed time of 50 ms gave a few
            # rounds on a quiet machine. Under a full run it sometimes gave only one round,
            # because Windows rounds each sleep of 1 ms up to its timer tick of approximately
            # 15 ms.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 10.0
            while calls["serial"] < scans and loop.time() < deadline:
                await asyncio.sleep(0.001)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return None

    async def _never(_device):
        raise AssertionError("verify should not run when selection is skipped")

    asyncio.run(device_picker.prompt_device(_Ui(), list(serial[0]), store, _never))
    assert calls["serial"] >= scans, f"the rescan ran {calls['serial']} of {scans} scans in 10 s"
    return redraws, calls


def _labels(items) -> list[str]:
    return [
        (it.title.plain if hasattr(it.title, "plain") else str(it.title))
        for it in items
        if isinstance(it, Choice)
    ]


def test_splash_redraws_when_a_device_is_plugged_in(tmp_path, monkeypatch) -> None:
    """A radio that the user attaches after the splash opened appears. MeshTerm need not restart."""
    from meshterm.core.discovery import DiscoveredDevice

    first = DiscoveredDevice(port="COM5", product="Wio SX1262", vid=0x2886)
    later = DiscoveredDevice(port="COM9", product="RAK4631", vid=0x239A)

    redraws, _ = _drive_picker(tmp_path, monkeypatch, serial_rounds=[[first], [first, later]])

    assert redraws, "the splash never redrew after the device appeared"
    assert any("COM9" in label for label in _labels(redraws[-1]))


def test_splash_stays_quiet_when_nothing_changed(tmp_path, monkeypatch) -> None:
    """A poll that finds the same ports paints nothing.

    This half is important for a user who is in the middle of a move down the list with
    the arrows. A paint that the user did not ask for, on a list that did not change, makes
    the screen move under the hands of the user for no reason.
    """
    from meshterm.core.discovery import DiscoveredDevice

    same = [DiscoveredDevice(port="COM5", product="Wio SX1262", vid=0x2886)]
    redraws, calls = _drive_picker(tmp_path, monkeypatch, serial_rounds=[same])

    assert calls["serial"] > 1, "the rescan never ran"
    assert redraws == []


def test_splash_notices_a_device_going_away(tmp_path, monkeypatch) -> None:
    """An unplug removes the row. This is the same question in the other direction."""
    from meshterm.core.discovery import DiscoveredDevice

    one = DiscoveredDevice(port="COM5", product="Wio SX1262", vid=0x2886)
    redraws, _ = _drive_picker(tmp_path, monkeypatch, serial_rounds=[[one], []])

    assert redraws, "the splash never redrew after the device went away"
    assert not any("COM5" in label for label in _labels(redraws[-1]))


def test_a_rescan_does_not_unhide_what_the_reader_hid(tmp_path, monkeypatch) -> None:
    """A refresh is not ⇧H. A device that `h` hid stays hidden when MeshTerm rebuilds the list."""
    from meshterm.core.discovery import DiscoveredDevice

    kept = DiscoveredDevice(port="COM5", product="Wio SX1262", vid=0x2886)
    buried = DiscoveredDevice(port="COM7", product="FT232R USB UART", vid=0x0403)
    later = DiscoveredDevice(port="COM9", product="RAK4631", vid=0x239A)

    redraws, _ = _drive_picker(
        tmp_path,
        monkeypatch,
        serial_rounds=[[kept, buried], [kept, buried, later]],
        hide=[buried.stable_id],
    )

    assert redraws, "the splash never redrew"
    labels = _labels(redraws[-1])
    assert any("COM9" in label for label in labels)
    assert not any("COM7" in label for label in labels)


def test_bluetooth_is_not_scanned_on_every_poll(tmp_path, monkeypatch) -> None:
    """Serial is cheap and MeshTerm polls it. Bluetooth is a listen, and it gets a duty cycle.

    Each BLE scan keeps the radio in the listen state. A user can leave this screen open
    on a handheld that uses a battery, while the user goes to find a cable. Thus the BLE
    scan must not be in the poll loop.
    """
    from meshterm.core.discovery import DiscoveredDevice

    same = [DiscoveredDevice(port="COM5", product="Wio SX1262", vid=0x2886)]
    _, calls = _drive_picker(tmp_path, monkeypatch, serial_rounds=[same])

    assert calls["serial"] > calls["ble"], (
        f"bluetooth scanned {calls['ble']} time(s) against {calls['serial']} serial poll(s)"
    )


def test_the_picker_only_passes_arguments_the_splash_surface_accepts(tmp_path) -> None:
    """Each keyword that `prompt_device` sends must exist on `Ui.select_startup`.

    A test double hides this type of bug. `prompt_device` calls the surface, and the
    surface delegates to the session. If a developer adds an argument to the session only,
    the startup path crashes and nothing else does. This is because each fake Ui in this
    file takes unknown keywords with `**kwargs`, and it does not fail. Thus the fake here
    binds the arguments that it gets against the real signature. A keyword that the surface
    does not have raises an error in the same place as in a running MeshTerm.
    """
    import inspect

    from meshterm.core.device_store import DeviceStore
    from meshterm.ui.device_picker import prompt_device
    from meshterm.ui.surface import Ui

    signature = inspect.signature(Ui.select_startup)

    class _Ui:
        async def select_startup(self, title, items, **kw):  # noqa: ANN001, ANN003, ANN201
            # This raises a TypeError for an unknown keyword.
            signature.bind(self, title, items, **kw)
            return None

    async def _never(_device):
        raise AssertionError("verify should not run when selection is skipped")

    store = DeviceStore(tmp_path / "devices.json")
    asyncio.run(prompt_device(_Ui(), [], store, _never))
