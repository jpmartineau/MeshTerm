# SPDX-License-Identifier: Apache-2.0
"""Composition helpers: change a screen and a terminal size into a bounded ANSI frame.

The session owns the input and the screen stack. This module owns the layout. It slices the
body of a screen to its viewport, wraps it in a panel with a title, and assembles the
persistent header and footer. Thus the whole frame fits the terminal exactly, and never goes
past it.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Callable, Sequence

from rich.cells import cell_len
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from ...platforms import Platform, get_platform, on_platform
from ..logo import load_logo, logo_width
from ..theme import fold_text, hint_style, title_style
from . import fkeys
from .render import crop_cells, render_lines, render_to_ansi
from .screen import Screen

#: How many times :func:`_visible_slice` settles the scroll again against the rows that a
#: screen pins above it. Each pin costs a content row. Thus one more pin can push the
#: highlighted row out of the viewport again, and make another small move necessary.
#: Two or three passes always get to a fixed point with the pin counts that screens use (a
#: column header and a section heading).
_PIN_SETTLE_PASSES = 3

#: The rows that the splash can cut off the top of the wordmark when the terminal is too
#: short for the whole block. Both marks show a globe above the lettering, and the globe is
#: decoration. On a short terminal, the box below it (the device list, the PIN prompt) is
#: what the user came for. A crop from the top keeps the lettering and its copyright exactly
#: where they were, so the mark becomes thinner and does not move. The height of the art
#: itself limits this crop, so a mark with no globe is never cut into its letters.
_BANNER_CROP_ROWS = 9


def _window_start(
    scroll: int, total: int, viewport: int, pinned: int, cursor: int | None = None
) -> int:
    """The body line at which the viewport starts, with ``pinned`` reserved top rows.

    Usually this is the scroll offset itself. When the page is scrolled fully to the
    bottom, the reserved rows must not cost the last body lines. (If they do, the input row
    of a chat disappears exactly when the transcript fills the screen.) Thus the viewport
    slides down by the number of pinned rows. The lines that it removes are at the top,
    directly under the pins, where they are stale.

    The exception is the line of the ``cursor``, which is never stale: the slide stops at
    the highlighted line. A user who walks ↑ back up a list that is still scrolled to its
    bottom gets to the top line of the viewport before the scroll moves. A slide past that
    line hid the row that the user stepped onto a moment before. When the two cannot share the
    viewport, the highlight keeps its row, and the last line waits below the ``↓`` marker.
    """
    cap = max(1, viewport - pinned)
    if scroll >= total - viewport and total > cap:
        start = min(scroll + pinned, total - cap)
        return start if cursor is None else max(scroll, min(start, cursor))
    return scroll


def _cursor_side(cursor: int | None, start: int, rows: int) -> int:
    """Where a viewport of ``rows`` lines from ``start`` puts ``cursor``: 0 in, -1 above, +1 below.

    If there is no highlight, the result is 0 (in the viewport): there is nothing to bring
    back.
    """
    if cursor is None or start <= cursor < start + rows:
        return 0
    return -1 if cursor < start else 1


def _visible_slice(screen: Screen, lines: list[str], viewport: int) -> tuple[list[str], bool, bool]:
    """Clamp the scroll of the screen, and return the visible lines and the clip flags.

    The function keeps the line of the highlight visible (for select and text screens), and
    clamps the scroll offset to the content. Then it pads the slice to exactly ``viewport``
    rows, so that the panel always fills its height. The screen can offer sticky rows (refer
    to :meth:`Screen.sticky_rows`): for example, the section heading of a grouped list that
    scrolled off, above the column header of a table. These rows are pinned to the top rows,
    and each one reserves one row. Thus the highlight is kept in the remaining
    ``viewport - pinned`` rows, and the bottom clamp is relaxed by the same number, so that
    the last content row can still get to the last visible line.

    Args:
        screen: The screen to render (its ``scroll`` is changed in place).
        lines: All the body lines of the screen, not clipped.
        viewport: The height of the visible body in rows.

    Returns:
        A tuple of (padded visible lines, more-above, more-below).
    """
    total = len(lines)
    # Record the body height and the viewport, so that the shared scroll helpers of the
    # screen can page by one screen height and clamp to the content (refer to
    # :meth:`Screen.note_metrics`).
    screen.note_metrics(total, viewport)
    cursor = screen.cursor_line()
    # Edge scroll: first, find the result of the arrow keys pressed since the last paint. An
    # arrow key that did not move the highlight scrolls the page instead
    # (Screen.note_highlight). A page scrolled past its highlight leaves the highlight where
    # it is, off the screen if necessary, until a key brings it back (refer to
    # Screen.edge_scroll). Thus the frame follows the highlight only on a page that is not
    # edge-scrolled.
    screen.note_highlight(cursor, lines)
    follow = None if screen.edge_scrolled else cursor

    scroll = screen.scroll
    if follow is not None:
        if follow < scroll:
            scroll = follow
        elif follow >= scroll + viewport:
            scroll = follow - viewport + 1
    scroll = max(0, min(scroll, max(0, total - viewport)))

    # Each pinned row takes a top row, and the content gets that many fewer rows. Move the
    # scroll down a little if the highlight falls in the reserved rows, then ask again. A move
    # can change which heading applies (when the move crosses a section boundary, it pins a
    # different heading, or none). A pin that the move adds reserves one more row, which can
    # make another small move necessary. The loop settles within _PIN_SETTLE_PASSES, because
    # a screen pins at most a column header and a heading.
    pinned = screen.sticky_rows(scroll) if scroll > 0 else []
    for _ in range(_PIN_SETTLE_PASSES):
        cap = max(1, viewport - len(pinned))
        if not pinned or follow is None or follow < scroll + cap:
            break
        scroll = min(follow - cap + 1, max(0, total - cap))
        pinned = screen.sticky_rows(scroll) if scroll > 0 else []

    screen.scroll = scroll

    if pinned:
        start = _window_start(scroll, total, viewport, len(pinned), follow)
        if start != scroll:
            # The viewport slid down from the scroll offset, so the pins must describe the row
            # at which it now starts. If not, a section heading in the removed lines
            # disappears, and is not pinned. Ask again, then settle the start again against
            # the number of rows that this reserves (never remove the pins: the slide
            # depends on them).
            pinned = screen.sticky_rows(start) or pinned
            start = _window_start(scroll, total, viewport, len(pinned), follow)
        cap = max(1, viewport - len(pinned))
        screen.note_cursor_side(_cursor_side(cursor, start, cap))
        visible = pinned + lines[start : start + cap]
        more_below = start + cap < total
        visible = visible + [""] * (viewport - len(visible))
        return visible, True, more_below

    screen.note_cursor_side(_cursor_side(cursor, scroll, viewport))
    visible = lines[scroll : scroll + viewport]
    more_above = scroll > 0
    more_below = scroll + viewport < total
    visible = visible + [""] * (viewport - len(visible))
    return visible, more_above, more_below


def _breathing_room(body_len: int, budget: int) -> int:
    """The rows of blank vertical padding that a box of content size draws in its borders.

    Each dialog box (the :func:`compose_dialog` floats, and also the :func:`compose_startup`
    splash boxes) adds a blank row above and below the body, when this costs no visible
    content. On a terminal that is too short for both, the content has priority and the box
    has no padding. Full-screen panels (:func:`compose_base`) do not use this padding, on
    purpose. They hold dense content, and the padding only uses rows to no purpose.

    Args:
        body_len: The full height of the body in rows.
        budget: The rows that the box can use between its borders, for the body and for
            the padding.

    Returns:
        The vertical padding (1 or 0) to pass to the panel.
    """
    return 1 if body_len + 2 <= budget else 0


def _panel_box(
    title: str,
    visible: list[str],
    more_above: bool,
    more_below: bool,
    border: str,
    caption: str = "",
    flush: bool = False,
) -> Panel:
    """Wrap body lines that are already sliced in a panel with a title and scroll arrows.

    The bottom rule can carry two things, and they share one subtitle instead of a fight for
    the line. The clip arrows are centred, as always. A ``caption`` is **right-justified**.
    (The caption is the :attr:`~meshterm.ui.tui.screen.Screen.bottom_caption` of a screen.
    Today, that is only the OpenStreetMap credit of the map.) Thus it shows as
    ``──── © OpenStreetMap ─╯``: one rule cell before the corner, the mirror of the title in
    the top rule, and the body gives up no cell for it. When both are necessary, the caption
    joins the arrows in the same run, and the run moves right. The user looks for the
    arrows, and the caption must only be present. If the frame removes one of them, the
    frame decides something that no screen asked for.

    Args:
        title: The screen's heading (empty for none).
        visible: The ANSI lines of the viewport, already sliced and padded to height.
        more_above: Whether content continues above the slice.
        more_below: Whether content continues below the slice.
        border: The name of the border style (``"accent"`` for the focused screen).
        caption: A short muted credit for the right end of the bottom rule (empty for none).
        flush: Remove the one-cell padding inside each side border, for a body that fills
            the frame (refer to :attr:`~meshterm.ui.tui.screen.Screen.flush`).

    Returns:
        A Rich :class:`Panel` of exactly ``len(visible) + 2`` rows.
    """
    body = Text.from_ansi("\n".join(visible))
    atoms = []
    if more_above or more_below:
        atoms.append(("↑" if more_above else " ") + ("↓" if more_below else " ") + " more")
    if caption:
        atoms.append(caption)
    subtitle = f"[{hint_style(border)}]{' · '.join(atoms)}[/]" if atoms else None
    return Panel(
        body,
        title=f"[{title_style(border)}]{title}[/]" if title else None,
        subtitle=subtitle,
        subtitle_align="right" if caption else "center",
        border_style=border,
        padding=(0, 0 if flush else 1),
    )


def panel_inset(flush: bool) -> int:
    """The cells that the base frame with a border takes from the width of the body.

    These are its two side borders, plus the padding cell inside each border, unless the
    screen is :attr:`~meshterm.ui.tui.screen.Screen.flush`. There is one definition, because
    the session sizes a screen against this frame before the frame is drawn
    (``base_body_size``).
    """
    return 2 if flush else 4


#: The last base composition with a border: ``(content key, rendered lines)``. To parse
#: the sliced body again (``Text.from_ansi``) and render it again through the Panel is the
#: most costly part of a paint (approximately 8 ms on a full frame). The idle tick composes
#: an unchanged screen again each second, and only the header above it moves. One slot is
#: sufficient: there is only one base screen for each paint. Each change of content (a key
#: press, a scroll, new rows) misses the cache and renders again.
_BASE_BOX_CACHE: tuple[tuple, list[str]] | None = None


#: The way out of the screen, taken from the end of its footer hint. The grammar is a rule:
#: "navigation keys, then action keys, **Esc last**". Thus the last atom is the Esc clause
#: when a screen has one, and its verb is the correct verb for that surface (``back`` on a
#: screen, ``keep`` in a value picker). The main menu is the one screen whose way out is not
#: Esc, because Esc does nothing there. Its hint ends on ``^Q quit?`` instead, until a typed
#: filter puts ``Esc clear`` after it.
_WAY_OUT_ATOM = re.compile(r"(?:^|·\s*)(Esc\s+\S+|\^[A-Z]\s+quit\??)\s*$")

#: The rule cells that the title keeps on each side before the bar removes the Esc hint.
#: With fewer cells, the hint crowds the title, and that is the one thing that the hint must
#: not do.
_MIN_RULE = 2


def fitted_title(screen: Screen, room: int) -> str:
    """The heading for ``room`` cells: the title, or its short form if the title is too long.

    If not, the rule cuts a title that states figures (``Contacts · 198 known + 123
    archived``), and the cut removes the end: the last figure. A screen that declares a
    :attr:`~meshterm.ui.tui.screen.Screen.short_title` gets that short title instead, with
    the same atoms in fewer cells. A screen with no short title keeps its title and the usual
    clipping of the frame.

    Args:
        screen: The screen whose heading is drawn.
        room: The cells that the heading can take, without the space on each side of it.
    """
    if screen.short_title and cell_len(screen.title) > room:
        return screen.short_title
    return screen.title


def _way_out(hint: str) -> str:
    """The way-out atom at the end of a footer hint (``Esc …``, ``^Q quit?``), or ``""``."""
    found = _WAY_OUT_ATOM.search(hint or "")
    return found.group(1) if found else ""


def _title_bar(
    screen: Screen,
    cols: int,
    more_above: bool,
    more_below: bool,
    status: Text | None = None,
) -> Text:
    """The one-row title bar of a borderless frame: clip arrows, a centred title, the way out.

    The bar puts the whole vocabulary of the Panel border in a single row: where you are
    (the title), and if the body continues (the ``↑↓ more`` subtitle). Thus the body gets
    back three rows and four cells of width on the PicoCalc. The shape is
    ``↑↓ ──── Title ─────── Esc back``, which repeats the heading language of
    :func:`~meshterm.ui.menus.section_heading`.

    **The clip arrows are at the start of the row, and the bar always draws both** (JP,
    2026-08-31). A pair that appeared and disappeared made the user compare the row with a
    memory of it, to learn something that the row can state directly. (When only one arrow
    showed, a blank cell stood in for the other.) Thus the pair is fixed at the left edge,
    where the eye starts, and **colour** gives the information. An arrow whose direction
    has more content takes the ``accent`` of the border, so it looks like part of the
    frame. An arrow with nothing in its direction becomes ``muted``. This is the same
    live/dim language that the F-key lane draws one row below (a dim chip keeps its label
    and loses its fill). The two are the only indicators of the platform.

    The title is centred in the rule between the arrows and the tail, exactly as the Rich
    ``Panel`` centres its own ``title``. (That panel is the frame with a border on the
    desktop, which this bar replaces.) The rule itself takes ``border_style``
    (``"accent"``) directly: the same bold weight that a real border has. It does not take
    the muted variant of :func:`~meshterm.ui.theme.hint_style`, which is for secondary text
    next to a border (the Esc atom below, a footer hint on the other platform).

    The tail of the bar carries **the way out** (JP, 2026-08-30). This platform has no
    footer hint line. The F-key lane is in its place, and the lane shows only what its five
    slots do. Thus nothing on the screen said that Esc leaves. But Esc is the one key that
    all screens answer to, and it is the reason why no screen uses a row for a *Back* item.
    The verb comes from the screen (:func:`_way_out` takes the atom from its footer hint).
    Thus a value picker says ``keep``, and the main menu, where Esc does nothing, says
    ``^Q quit?``. A screen whose hint names no way out gets nothing.

    The design keeps the bar compact. When a long title has too little space, the atom
    becomes only its key, then disappears. The title is what the user came for, and the
    arrows are two cells that nothing else can use.

    When the platform has **no header row** (the Cardputer, JP 2026-10-03), the atoms of
    the header arrive as ``status``. They are pinned to the right end of the bar, after the
    way out: ``↑↓ ── Title ──── Esc back ● 3 ⣷ 87%``. That is the top-right corner that the
    badges and the battery always had, one row higher, and each screen gets the header row
    back. The status gives way to nothing, because it exists to tell an unread count and a
    battery that is almost empty. Thus the way out goes first, as it does for a long title.
    If the title is still too long, it is cut with an ellipsis, and does not push the
    status off the end of the row.

    Args:
        screen: The screen whose heading the bar carries.
        cols: The width of the bar in cells.
        more_above: Whether the body continues above the viewport.
        more_below: Whether the body continues below it.
        status: The atoms of the header, for a platform without a header row. ``None`` (or
            empty: nothing unread, no pack) draws the bar exactly as it is with a header.
    """
    border = "accent"
    head_span = 3  # the arrow pair, plus the space between it and the rule
    status = status if status is not None and status.cell_len else None
    stat_span = status.cell_len + 1 if status else 0  # the space before the status
    # The short form is chosen against the bar with no Esc tail: the title is what the user
    # came for. Thus the tail gives way to the title before the title gives way to the tail.
    title = fitted_title(screen, cols - head_span - stat_span - 2 - 2 * _MIN_RULE)
    label_w = cell_len(title) + 2 if title else 0  # a space on each side
    atom = _way_out(screen.footer_hint)
    for esc in (atom, atom.split()[0] if atom else "", ""):
        tail_span = cell_len(esc) + 1 if esc else 0  # the space before the tail
        if not esc or cols - head_span - tail_span - stat_span - label_w >= 2 * _MIN_RULE:
            break
    rule_span = max(0, cols - head_span - tail_span - stat_span)
    heading = screen.bar_title(title, title_style(border)) if title else Text()
    if status and label_w > rule_span - 2:
        # One rule cell on each side, and the remaining cells for the title. The status stays.
        heading.truncate(max(1, rule_span - 4), overflow="ellipsis")
        label_w = heading.cell_len + 2

    bar = Text()
    bar.append("↑", style=border if more_above else "muted")
    bar.append("↓", style=border if more_below else "muted")
    bar.append(" ")
    if title:
        left = max(1, (rule_span - label_w) // 2)
        right = max(1, rule_span - label_w - left)
        bar.append("─" * left, style=border)
        bar.append(" ", style=None)
        bar.append_text(heading)
        bar.append(" ", style=None)
        bar.append("─" * right, style=border)
    else:
        bar.append("─" * rule_span, style=border)
    if esc:
        bar.append(" " + esc, style=hint_style(border))
    if status:
        bar.append(" ")
        bar.append_text(status)
    bar.truncate(cols)
    return bar


def header_lines(header: Text, cols: int) -> list[str]:
    """The header rows above the panel: its one line, or none where it has no row.

    The header is a single status line, cropped to one row, so that a narrow terminal never
    wraps it onto a second row. (A second row pushes the panel down and gives a wrong height
    for it.) A platform without a header row (:attr:`~meshterm.platforms.Platform.header_row`)
    draws no header here. The title bar carries the atoms of the header instead (refer to
    :func:`_title_bar`). This function is the one place that says so, for
    :func:`compose_base` and also for the session when it sizes a body against it.
    """
    if not get_platform().header_row:
        return []
    return render_lines(header, cols, no_wrap=True)


def compose_base(
    header: Text,
    base: Screen,
    footer_hint: str,
    cols: int,
    rows: int,
    footer_lane: Callable[[], RenderableType] | None = None,
) -> str:
    """Compose the full-screen ANSI frame: the header, the base screen's panel, and a footer.

    Args:
        header: The persistent header (banner and live status). It is a row of its own, or
            the right end of the title bar on a platform without a header row (refer to
            :func:`header_lines`).
        base: The screen that fills the background (the deepest layer that does not
            float).
        footer_hint: The key hint of the active screen, shown at the bottom.
        cols: Terminal width.
        rows: Terminal height.
        footer_lane: A function that builds the footer row (the PicoCalc F-key lane). That
            row replaces the hint string when the platform uses fixed F-key hints. It is
            called after the body renders, because a lane dims its slots from the scroll
            metrics of the screen. This paint records these metrics only a moment before
            (refer to :func:`~meshterm.ui.tui.fkeys.default_lane`).

    Returns:
        An ANSI string of exactly ``rows`` lines, each not wider than ``cols`` cells.
    """
    global _BASE_BOX_CACHE
    platform = get_platform()
    head = header_lines(header, cols)
    header_h = len(head)

    def footer_row() -> RenderableType:
        """The bottom line: the F-key lane of the platform, or else the muted hint string."""
        if footer_lane is not None:
            return footer_lane()
        return Text.from_markup(f"[muted]{footer_hint}[/muted]")

    if platform.frame_border:
        viewport = max(1, rows - header_h - 1 - 2)  # minus footer(1) and panel border(2)
        # Record the viewport before the body renders. Thus a screen with a list window
        # (refer to :class:`~meshterm.ui.tui.screen.ListWindow`) can size its chrome to the
        # frame into which it will be sliced.
        base.note_viewport(viewport)
        body_lines = base.render_body(cols - panel_inset(base.flush))
        visible, more_above, more_below = _visible_slice(base, body_lines, viewport)
        # The panel wrap is a pure function of what is between its borders. Thus cache it,
        # so that the paints that change nothing below the header (the 1 Hz tick) do not
        # parse the ANSI again and do not render the Panel again. A dynamic footer (the F-key
        # lane, which can change with only the physical Shift key) has no place in the key,
        # so it renders without the cache. But that combination does not occur: the lane
        # belongs to the borderless platform below.
        # The caption belongs in the key, as the title does. The credit of the map becomes
        # short on a key press that does not have to change a body line. If the cache does
        # not have the caption, it continues to return the old rule after that key press.
        caption = base.bottom_caption
        # Rich sets the padded title into the inner ``cols - 4`` cells of the rule.
        title = fitted_title(base, cols - 4 - 2)
        flush = base.flush
        key = (cols, rows, title, caption, flush, footer_hint, more_above, more_below, *visible)
        if footer_lane is None and _BASE_BOX_CACHE is not None and _BASE_BOX_CACHE[0] == key:
            body = _BASE_BOX_CACHE[1]
        else:
            panel = _panel_box(title, visible, more_above, more_below, "accent", caption, flush)
            body = render_lines(Group(panel, footer_row()), cols)
            if footer_lane is None:
                _BASE_BOX_CACHE = (key, body)
    else:
        # Chrome with no border: a one-row title bar instead of the border and padding of
        # the Panel. The body gets the full terminal width and one more row.
        viewport = max(1, rows - header_h - 1 - 1)  # minus footer(1) and title bar(1)
        base.note_viewport(viewport)
        body_lines = base.render_body(cols)
        visible, more_above, more_below = _visible_slice(base, body_lines, viewport)
        status = None if platform.header_row else header
        bar = _title_bar(base, cols, more_above, more_below, status)
        body = (
            render_lines(bar, cols, no_wrap=True)
            + visible
            + render_lines(footer_row(), cols, no_wrap=True)
        )
    lines = head + body
    # Make sure that the frame is never taller than the terminal. If it is taller, pt clips it
    # in a way that we cannot predict.
    if len(lines) > rows:
        lines = lines[:rows]
    else:
        lines += [""] * (rows - len(lines))
    return "\n".join(lines)


def _ansi_width(line: str) -> int:
    """Return the width in cells of an ANSI line, without its trailing padding."""
    return cell_len(Text.from_ansi(line).plain.rstrip())


def _center(lines: list[str], cols: int) -> list[str]:
    """Pad each ANSI line on the left, so that the block is centred horizontally in ``cols``."""
    out: list[str] = []
    for line in lines:
        pad = max(0, (cols - cell_len(Text.from_ansi(line).plain)) // 2)
        out.append(" " * pad + line)
    return out


def _banner_lines(banner: Sequence[str], cols: int) -> list[str]:
    """Centre the wordmark rows (ANSI that already has colour) as one left-aligned block.

    First, each row is padded to the widest width of the block. Thus the shared left margin
    keeps the art aligned in itself, and each row is not centred on its own axis.

    The rows are art, not renderables, so they never go through
    :func:`~meshterm.ui.tui.render.render_to_ansi`. Thus the fold is applied here instead.
    The truecolour of the wordmark then goes to the palette slots for which the art was
    drawn, not to the slots that the downsample of the console selects.
    """
    if not banner:
        return []
    widths = [cell_len(Text.from_ansi(row).plain) for row in banner]
    width = max(widths)
    padded = [fold_text(row) + " " * (width - w) for row, w in zip(banner, widths, strict=True)]
    return _center(padded, cols)


#: The wordmark as the splash last drew it: ``(banner, cols)`` and the result. One slot,
#: because there is one splash and one terminal size at a time.
_BANNER_CACHE: tuple[tuple, tuple[list[str], int]] | None = None


def _fitted_banner(banner: Sequence[str], cols: int) -> tuple[list[str], int]:
    """The wordmark drawn at ``cols`` (fitted, folded, and centred), and the width of the art.

    To fit the mark is the most costly thing that the splash draws. If a mark is wider than
    the terminal, :func:`~meshterm.ui.logo.load_logo` goes back to the disk for each size
    until one fits. It parses and measures each one, and then each row is decoded again to
    centre it. None of that changes from one paint to the next, and the splash paints on
    each key press and each idle tick. When this was done again each time, it took
    nine-tenths of a splash frame at 53×26. On the PicoCalc, that caused a visible wait
    after each ↑↓ on the device list.

    Args:
        banner: The wordmark rows that the screen names (usually the full-size mark).
        cols: Terminal width.

    Returns:
        The centred rows to draw, and the width in cells of the mark that was chosen, to
        align the footnote with its edge. The cache shares the rows, so never change them.
    """
    global _BANNER_CACHE
    key = (tuple(banner), cols)
    if _BANNER_CACHE is not None and _BANNER_CACHE[0] == key:
        return _BANNER_CACHE[1]
    raw = list(banner)
    if raw and logo_width(raw) > cols:
        raw = load_logo(cols)
    fitted = (_banner_lines(raw, cols), logo_width(raw))
    _BANNER_CACHE = (key, fitted)
    return fitted


def fit_hint(hint: str, width: int, *, shed_first: Sequence[str] = ()) -> str:
    """Remove ``·`` atoms from a footer hint, from right to left, until it fits ``width``.

    The chromeless splash has one border row for its hint, and a box that is not wider than
    the terminal minus its gutter (47 cells on the PicoCalc). But the hint grows when the
    highlight moves onto a row with more keys to offer. A cut of the line at the edge
    removes its end, and the end is ``Esc``: the one atom that must stay, because it is the
    only way out. Thus the function removes atoms instead, from the right, and never the
    last one. The cells then go to the keys nearest the front of the sentence (the keys that
    move and commit). The function gives up the optional atoms, which a screen shows in
    other places.

    ``shed_first`` overrides that order for atoms whose key the user finds without the hint.
    A scroll atom is the example. The leading move atom already names the ←→ keys, so a try
    of these keys costs nothing and shows the rest. But nobody can guess a bare-letter
    shortcut, and a chromeless splash has no other place to show it. Between those two, the
    sentence keeps the letter.

    Args:
        hint: The composed hint sentence.
        width: The cells that are available.
        shed_first: The atoms to remove before all others, in the given order.

    Returns:
        The hint, or as much of its start as fits, plus its last atom. If even those do not
        fit, the result is cut with an ellipsis.
    """
    if cell_len(hint) <= width:
        return hint
    atoms = hint.split(" · ")
    for spare in shed_first:
        if cell_len(" · ".join(atoms)) <= width:
            break
        if spare in atoms:
            atoms.remove(spare)
    while len(atoms) > 2 and cell_len(" · ".join(atoms)) > width:
        del atoms[-2]  # the last atom is the Esc atom: remove the atom before it
    fitted = " · ".join(atoms)
    if cell_len(fitted) <= width:
        return fitted
    return crop_cells(Text(fitted), 0, max(1, width - 1)).plain + "…"


def compose_startup(screen: Screen, cols: int, rows: int) -> str:
    """Compose a chromeless splash: a centred wordmark above a panel of content size.

    This function draws no header or footer status bars, and it does not stretch the panel
    across the terminal, as :func:`compose_base` does. The box has the size of its own
    content (the device list), and the whole block is centred on the terminal. The startup
    device picker uses it.

    Args:
        screen: The chromeless base screen (its ``banner`` gives the wordmark).
        cols: Terminal width.
        rows: Terminal height.

    Returns:
        An ANSI string of exactly ``rows`` lines, each not wider than ``cols`` cells.
    """
    # A screen names its wordmark. Only this function knows the width at which to draw it,
    # so the size is chosen here. A narrow terminal gets a narrow mark, not a torn one.
    # ``logo_w`` is the width of the chosen mark, so that the footnote below can align with
    # its right edge (the wordmark is centred as one block, so all rows share one left
    # margin).
    banner, logo_w = _fitted_banner(screen.banner or (), cols)
    banner_h = len(banner)
    gap = 1 if banner else 0  # the blank line under the banner
    logo_right = max(0, (cols - logo_w) // 2) + logo_w

    # Size the box to its widest real row (a trial render at a generous width, then a
    # measure). It is never wider than the terminal, and never narrower than the title or the
    # hint that it must show.
    probe = max(10, min(cols - 6, 100))
    probe_lines = screen.render_body(probe)
    measured = max((_ansi_width(line) for line in probe_lines), default=10)
    # Size to the widest footer possible, not to the footer of this frame. A screen whose
    # hint grows as the highlight moves (the "Del remove" of a select list on some rows)
    # gives that width here. Thus the box has space for it from the start, and never
    # becomes wider during navigation.
    sizing_footer = getattr(screen, "sizing_footer_hint", screen.footer_hint)
    inner_w = max(measured, cell_len(screen.title), cell_len(sizing_footer))
    inner_w = max(10, min(inner_w, cols - 6))
    # The hint is fitted to the box, instead of cut by it (refer to :func:`fit_hint`).
    hint_text = fit_hint(
        screen.footer_hint, inner_w, shed_first=getattr(screen, "spare_hint_atoms", ())
    )

    # The trial render is the answer when it was already made at the width that we chose.
    # That is the usual case, because a body narrower than the trial width is what set
    # inner_w. The splash paints on each tick while the device list is visible. A second
    # render of the same body was a measured third of that paint on the PicoCalc.
    body_lines = probe_lines if inner_w == probe else screen.render_body(inner_w)
    footnote_h = 1 if screen.footnote else 0  # the note is directly under the logo

    # A terminal too short for the whole block gives back rows in a fixed order, the least
    # costly first. First, the blank line under the mark (it is only space). Then the top of
    # the mark itself: the globe above the lettering, up to _BANNER_CROP_ROWS of it. The
    # lettering and its copyright tell what this is, so they never move. The mark becomes
    # thinner from above, and the box below keeps its rows. After that, the block is taller
    # than the terminal, and the trailing clip still applies.
    short = banner_h + footnote_h + gap + len(body_lines) + 2 - rows
    if short > 0 and gap:
        gap = 0
        short -= 1
    if short > 0 and banner:
        banner = banner[min(short, _BANNER_CROP_ROWS, banner_h) :]
        banner_h = len(banner)

    # Pin the banner to a fixed vertical anchor that depends only on the terminal height and
    # the (constant) height of the banner. Thus the wordmark never moves when the box below
    # it changes contents between splash states (device list → spinner → message). The box
    # hangs from directly under the banner, and only the box grows or shrinks. The logo
    # stays where it is. The anchor is at 2/5 of the terminal, not at the midline, so that
    # the block looks centred when the device list has its rows (the box only grows
    # downward). The short states, which are small, are a little high: the usual optical
    # placement for dialogs.
    top = max(0, rows * 2 // 5 - banner_h - gap)
    # The rows that remain for the box below the fixed banner block. The panel border is
    # 2 rows.
    below = rows - top - banner_h - footnote_h - gap
    budget = below - 2
    vpad = _breathing_room(len(body_lines), budget)
    viewport = max(1, min(len(body_lines), budget - 2 * vpad))
    visible, more_above, more_below = _visible_slice(screen, body_lines, viewport)

    body = Text.from_ansi("\n".join(visible))
    hint = hint_style("accent")
    subtitle = f"[{hint}]{hint_text}[/]"
    if more_above or more_below:
        arrow = ("↑" if more_above else " ") + ("↓" if more_below else " ")
        subtitle = f"[{hint}]{arrow} · {fit_hint(hint_text, inner_w - 5)}[/]"
    panel = Panel(
        body,
        title=f"[{title_style('accent')}]{screen.title}[/]" if screen.title else None,
        subtitle=subtitle,
        border_style="accent",
        padding=(vpad, 1),
        width=inner_w + 4,
    )
    panel_lines = _center(render_lines(panel, inner_w + 4), cols)

    # A small muted line (for example a copyright notice) is immediately under the logo. Its
    # right edge aligns with the right edge of the logo, so that the two look like one signed
    # block.
    footnote_lines: list[str] = []
    if screen.footnote:
        note = Text.from_markup(f"[muted]{screen.footnote}[/muted]")
        rendered = render_lines(note, cell_len(screen.footnote))
        pad = max(0, logo_right - cell_len(screen.footnote))
        footnote_lines = [" " * pad + line for line in rendered]

    block = banner + footnote_lines + ([""] * gap) + panel_lines
    lines = [""] * top + block
    if len(lines) > rows:
        lines = lines[:rows]
    else:
        lines += [""] * (rows - len(lines))
    return "\n".join(lines)


def compose_bare(screen: Screen, cols: int, rows: int) -> str:
    """Compose a bare frame: the screen's body on blank rows, and nothing else.

    There is no header, no footer, no title bar, no box, and no wordmark: this is the frame
    that :attr:`~meshterm.ui.tui.screen.Screen.bare` asks for. The body is drawn at the full
    width of the terminal (the screen lays out its own rows across it). When the body is
    shorter than the frame, it is a little above the true centre: the optical placement that
    all other parts of this module use. A body taller than the frame shows from its top in
    the viewport, and scrolls. Thus a QR code that does not fit is at least whole while the
    user is at its top, and the URL is under it, one page down.

    Args:
        screen: The bare base screen.
        cols: Terminal width.
        rows: Terminal height.

    Returns:
        An ANSI string of exactly ``rows`` lines, each not wider than ``cols`` cells.
    """
    # Only this function knows the height, and a bare screen can size its body to it (the
    # share screen fits its code to the rows). Thus the height is recorded before the body
    # is requested, not after, as the composers with a frame also do.
    screen.note_viewport(rows)
    body_lines = screen.render_body(cols)
    while body_lines and not _ansi_width(body_lines[-1]):
        body_lines.pop()  # a trailing blank row pushes the block off its anchor
    visible, _more_above, _more_below = _visible_slice(screen, body_lines, rows)
    top = max(0, (rows - len(body_lines)) * 2 // 5)
    lines = [""] * top + visible[: rows - top]
    return "\n".join(lines)


def _dialog_layout(screen: Screen, cols: int, rows: int) -> tuple[int, int, int, list[str]]:
    """Size a floating dialog: ``(outer_width, vpad, viewport, body_lines)``."""
    # Most dialogs stretch to a generous limit. A screen can request a natural width instead
    # (a short confirm sized to its content), still limited to the terminal. The natural
    # width of a grow-only screen ratchets like its height, so the box also never becomes
    # narrower. The cells that the limit leaves are the backdrop gutter over which the box
    # floats. The platform decides how many cells that is (refer to Platform.dialog_margin).
    cap = min(cols - get_platform().dialog_margin, 100)
    natural = getattr(screen, "dialog_width", None)
    max_w = cap if natural is None else max(24, min(cap, screen.ratchet_width(natural)))
    # The rows that the box can use between its borders, for body lines and for padding:
    # the frame minus the two borders and the rows that it leaves to the frame around it
    # (the header, the footer, and a blank row on each side when the frame is tall enough).
    # Refer to Platform.dialog_row_margin.
    budget = max(3, rows - 2 - get_platform().dialog_row_margin)
    # Record the budget as the temporary viewport before the body renders. Thus a dialog
    # with a list window (the path composer) can size to what it can use. After that, the
    # slice records the real viewport, sized to the body.
    screen.note_viewport(budget)
    body_lines = screen.render_body(max_w - 4)
    # A grow-only screen (the packet viewer, when it pages between packets) sizes to the
    # tallest body that it has shown, not to this body. Thus a shorter body keeps the larger
    # box, and the box does not become smaller and centred again. The frame pads the extra
    # rows with blanks. The padding comes from that ratcheted height, so the box stays where
    # it is when the body becomes shorter than it.
    body_h = screen.ratchet_viewport(max(1, len(body_lines)))
    vpad = _breathing_room(body_h, budget)
    viewport = min(budget - 2 * vpad, body_h)
    return max_w, vpad, viewport, body_lines


#: Cached dialog compositions, with a key of all that the box is a function of. Dialogs
#: stack (a confirm over a picker over a menu), and each layer composes again on each paint
#: of the frame below it. Thus one slot is not sufficient, because it thrashes. A small
#: number covers the deepest realistic stack. When dialogs change, the cache removes the
#: entry that was used least recently.
_DIALOG_CACHE: OrderedDict[tuple, str] = OrderedDict()

#: The number of dialog compositions that the cache keeps.
_DIALOG_CACHE_MAX = 12


def _dialog_hint(screen: Screen) -> str:
    """The hint that the border of a floating dialog carries. Often there is no hint.

    A dialog is drawn over a frame that already has a footer row. The type of that row
    decides if the box must show a hint:

    - When the footer is the **hint line** (the desktop), the footer draws the hint of the
      top screen (this dialog) in full. It is on the row where the user always reads the
      keys of each screen. A subtitle that repeated it put the same sentence two times in
      one frame: one time in the border, and one time at the bottom of the terminal
      (JP, 2026-08-31). Thus the border says nothing, and the user looks at the one place
      where the hints are.
    - When the footer is the **F-key lane** (the PicoCalc), there is no hint line, and the
      lane shows only its five chips. Thus the border is the only place that can name
      Enter, Esc, or the arrow keys, and it keeps the hint. The lane already shows its own
      atoms, one row below: :func:`~meshterm.ui.tui.fkeys.strip_lane_atoms` removes the
      atoms whose keys are all chips on that row.

    The hint is resolved on each paint, instead of one time in the hint of a screen,
    because both halves change. A screen writes its hint again when its content changes
    (the packet viewer changes to only ``Esc close`` on a single packet), and its lane is
    read again for each frame.

    The chromeless startup splash is not this case, and never comes here. It has no footer
    row of any type, so :func:`compose_startup` continues to draw the hint in the subtitle
    of its own panel on both platforms.
    """
    if not get_platform().footer_fkeys:
        return ""
    return fkeys.strip_lane_atoms(screen.footer_hint, screen.fkey_lane)


def _dialog_subtitle(hint: str, more_above: bool, more_below: bool, border: str) -> str | None:
    """The legend on the bottom border of a floating box: the clip arrows, the hint, or both.

    When there is no hint to carry (the desktop: refer to :func:`_dialog_hint`), the arrows
    keep the words of the base frame, ``↑↓ more``, exactly as :func:`_panel_box` labels
    them. A border with no hint still says the one thing that all borders here say, in the
    vocabulary of the frame around it.
    """
    arrow = ""
    if more_above or more_below:
        arrow = ("↑" if more_above else " ") + ("↓" if more_below else " ")
    if not hint:
        return f"[{hint_style(border)}]{arrow} more[/]" if arrow else None
    legend = f"{arrow} · {hint}" if arrow else hint
    return f"[{hint_style(border)}]{legend}[/]"


def compose_dialog(screen: Screen, cols: int, rows: int) -> str:
    """Compose a centred dialog panel for a floating screen, limited to the terminal.

    Args:
        screen: The floating (top) screen.
        cols: Terminal width.
        rows: Terminal height.

    Returns:
        An ANSI string sized to the content of the dialog (never larger than the terminal).
    """
    max_w, vpad, viewport, body_lines = _dialog_layout(screen, cols, rows)
    visible, more_above, more_below = _visible_slice(screen, body_lines, viewport)
    border = getattr(screen, "border_style", "accent")
    # The resolved hint, not the hint of the screen: it is what the border draws. It changes
    # with the lane below, and also with the hint of the screen (refer to _dialog_hint).
    hint = _dialog_hint(screen)
    key = (
        max_w,
        vpad,
        screen.title,
        hint,
        border,
        more_above,
        more_below,
        *visible,
    )
    cached = _DIALOG_CACHE.get(key)
    if cached is not None:
        _DIALOG_CACHE.move_to_end(key)
        return cached
    body = Text.from_ansi("\n".join(visible))
    subtitle = _dialog_subtitle(hint, more_above, more_below, border)
    panel = Panel(
        body,
        title=f"[{title_style(border)}]{screen.title}[/]" if screen.title else None,
        subtitle=subtitle,
        border_style=border,
        padding=(vpad, 1),
        width=max_w,
    )
    out = "\n".join(render_lines(panel, max_w))
    _DIALOG_CACHE[key] = out
    if len(_DIALOG_CACHE) > _DIALOG_CACHE_MAX:
        _DIALOG_CACHE.popitem(last=False)
    return out


#: Cached composited rows, with a key of all that the merge is a function of: the base row
#: under the box, the line of the box itself, and its position. When a dialog paints, almost
#: each row is identical to the last paint (only the highlight moved, or only the pulse of
#: the header). Each unchanged row then costs a dict hit, instead of two ANSI parses, two
#: cell crops, and a render.
_OVERLAY_CACHE: OrderedDict[tuple, str] = OrderedDict()

#: The number of composited rows that the cache holds: the rows of a few dialogs.
_OVERLAY_CACHE_MAX = 256


def _overlay_row(base_row: str, box_line: str, left: int, cols: int) -> str:
    """Put one box line over one base row at cell ``left``, and keep the styles of both."""
    key = (base_row, box_line, left, cols)
    cached = _OVERLAY_CACHE.get(key)
    if cached is not None:
        _OVERLAY_CACHE.move_to_end(key)
        return cached
    under = Text.from_ansi(base_row)
    over = Text.from_ansi(box_line)
    merged = crop_cells(under, 0, left)
    # A base row shorter than the left edge of the box (a blank line under a wide dialog)
    # must still hold the box at its true column. Thus pad the gap, and do not close it.
    merged.append(" " * max(0, left - merged.cell_len))
    merged.append_text(over)
    tail_at = left + over.cell_len
    merged.append_text(crop_cells(under, tail_at, max(0, cols - tail_at)))
    merged.no_wrap = True
    merged.overflow = "crop"
    merged.truncate(cols)
    out = render_to_ansi(merged, cols, no_wrap=True)
    _OVERLAY_CACHE[key] = out
    if len(_OVERLAY_CACHE) > _OVERLAY_CACHE_MAX:
        _OVERLAY_CACHE.popitem(last=False)
    return out


def composite_float(base_rows: list[str], box: str, cols: int, rows: int) -> list[str]:
    """Put a dialog box over the full-screen rows below it, centred, and return the result.

    Usually, the ``FloatContainer`` of prompt_toolkit places the floating layers. For that
    reason, a frame with a float once went whole to the renderer of prompt_toolkit. It then
    paid the full grid rebuild and diff (70-125 ms on the PicoCalc) on each key press in
    each dialog. But this function can do all of that placement. A float with no anchor and
    no size of its own is centred on the frame: horizontally by its widest line, and
    vertically by its line count, clipped to the terminal. When we do it here, dialogs stay
    on the row-diff path with the rest of the app.

    Args:
        base_rows: The frame below, one ANSI string for each terminal row.
        box: The composed dialog, its rows joined by newlines (refer to
            :func:`compose_dialog`).
        cols: Terminal width.
        rows: Terminal height.

    A box that lands on the **title bar** of a borderless frame takes the whole row. The
    cells on each side of it are blank, and the bar does not show through. On all other
    rows, the gutter is the backdrop below the floating box, and looks like a backdrop. But
    a bar that a box cuts is only fragments of chrome: the clip arrows of the base, the last
    letters of its way out, and the tail of a battery gauge. (The bar carries the atoms of
    the header on the Cardputer, whose tall dialogs draw over the bar.) There, a stray
    ``7%`` looks like a battery level.

    Returns:
        A new list of rows, with the box merged in. The rows that the box does not reach go
        through with no change. They are the same strings, so the row diff skips them at no
        cost.
    """
    lines = box.split("\n")
    width = max((cell_len(Text.from_ansi(line).plain) for line in lines), default=0)
    top = max(0, (rows - len(lines)) // 2)
    left = max(0, (cols - width) // 2)
    out = list(base_rows)
    for i, line in enumerate(lines):
        y = top + i
        if 0 <= y < len(out):
            under = "" if y == _BAR_ROW else out[y]
            out[y] = _overlay_row(under, line, left, cols)
    return out


#: The row of the title bar of a borderless frame: under the one line of the header, or the
#: top row when the platform has no header row. It is ``None`` on a frame with a border,
#: because its top rule belongs to the panel and leaves no fragments (bound in
#: :func:`_bind`).
_BAR_ROW: int | None = None


@on_platform
def _bind(platform: Platform) -> None:
    """Clear the composition caches on a platform switch: their output contains the theme.

    It also binds the row of the title bar (:data:`_BAR_ROW`), which only the platform
    decides. It is registered at the bottom of the module, so that the immediate first run
    (refer to :func:`~meshterm.platforms.on_platform`) finds each cache already defined.
    """
    global _BASE_BOX_CACHE, _BANNER_CACHE, _BAR_ROW
    _BAR_ROW = None if platform.frame_border else int(platform.header_row)
    _BASE_BOX_CACHE = None
    _BANNER_CACHE = None
    _DIALOG_CACHE.clear()
    _OVERLAY_CACHE.clear()
