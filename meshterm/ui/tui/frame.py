# SPDX-License-Identifier: Apache-2.0
"""Composition helpers: turn a screen + terminal size into a bounded, framed ANSI view.

The session owns input and the screen stack; this module owns *layout* — slicing a screen's
body to a scroll viewport, wrapping it in a titled panel, and assembling the persistent
header and footer so the whole view fits the terminal exactly (never overflowing it).
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

#: How many times :func:`_visible_slice` re-settles the scroll against the rows a screen
#: pins above it. Each pin costs a content row, so gaining one can push the highlighted row
#: back out of view and call for another nudge; two or three passes always reach a fixed
#: point at the pin counts screens actually use (a column header and a section heading).
_PIN_SETTLE_PASSES = 3

#: Rows the splash may shear off the *top* of the wordmark when the terminal is too short
#: to hold the whole block. Both marks are drawn as a globe standing over the lettering, and
#: the globe is decoration: on a short screen the box below it — the device list, the PIN
#: prompt — is what the user came for. Cropping from the top keeps the lettering and its
#: copyright sitting exactly where they were, so the mark thins rather than moves. The art's
#: own height bounds this, so a mark drawn without a globe is never sheared into its letters.
_BANNER_CROP_ROWS = 9


def _window_start(
    scroll: int, total: int, viewport: int, pinned: int, cursor: int | None = None
) -> int:
    """The body line the visible window starts at, given ``pinned`` reserved top rows.

    Normally the scroll offset itself. Scrolled fully to the bottom, the reserved rows must
    not cost the *last* body lines (a chat's input row would vanish exactly when the
    transcript fills the screen), so the window slides down by as many rows as are pinned —
    the lines it drops are at the top, right under the pins, where they are stale.

    Except the ``cursor``'s, which is never stale: the slide stops at the highlighted line.
    A reader walking ↑ back up a list that is still scrolled to its bottom reaches the
    window's top line with the scroll yet to move, and a slide past that line hid the very
    row they had just stepped onto. Where the two cannot share the window the highlight
    keeps its row and the last line waits below the ``↓`` marker.
    """
    cap = max(1, viewport - pinned)
    if scroll >= total - viewport and total > cap:
        start = min(scroll + pinned, total - cap)
        return start if cursor is None else max(scroll, min(start, cursor))
    return scroll


def _visible_slice(screen: Screen, lines: list[str], viewport: int) -> tuple[list[str], bool, bool]:
    """Clamp the screen's scroll and return the visible lines plus clip flags.

    Keeps the screen's cursor line in view (for select/text screens) and clamps the scroll
    offset to the content, then pads the slice to exactly ``viewport`` rows so the panel
    always fills its allotted height. Any sticky rows the screen offers (a grouped list's
    section heading that has scrolled off, over a table's column header — see
    :meth:`Screen.sticky_rows`) are pinned to the top rows: each reserves one row, so the
    cursor is kept within the remaining ``viewport - pinned`` and the bottom clamp is relaxed
    by as many so the final content row can still reach the last visible line.

    Args:
        screen: The screen being rendered (its ``scroll`` is adjusted in place).
        lines: The screen's full, unclipped body lines.
        viewport: The visible body height in rows.

    Returns:
        A tuple of (padded visible lines, more-above, more-below).
    """
    total = len(lines)
    # Record the body height and viewport so the screen's shared scroll helpers can page by a
    # screenful and clamp to the content (see :meth:`Screen.note_metrics`).
    screen.note_metrics(total, viewport)
    cursor = screen.cursor_line()
    # Edge scroll: a page scrolled past its highlight leaves it where it is, off screen if
    # need be, until a key brings it back (see Screen.edge_scroll) — so only a page that
    # isn't edge-scrolled has its highlight followed.
    follow = None if screen.edge_scrolled else cursor

    scroll = screen.scroll
    if follow is not None:
        if follow < scroll:
            scroll = follow
        elif follow >= scroll + viewport:
            scroll = follow - viewport + 1
    scroll = max(0, min(scroll, max(0, total - viewport)))

    # Each pinned row takes a top row, leaving that many fewer for content: nudge the scroll
    # down if the cursor would fall in the reserved rows, then re-ask — moving can change
    # which heading governs (crossing a section boundary pins another, or none), and a pin
    # gained that way reserves one more row, which can call for a further nudge. Settles
    # within _PIN_SETTLE_PASSES, since a screen pins at most a column header and a heading.
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
            # The window slid down off the scroll offset, so the pins must describe the row
            # it now *starts* at — otherwise a section heading among the dropped lines would
            # simply vanish instead of being pinned. Re-ask, then re-settle the start against
            # however many rows that reserves (never unpinning: the slide depends on it).
            pinned = screen.sticky_rows(start) or pinned
            start = _window_start(scroll, total, viewport, len(pinned), follow)
        cap = max(1, viewport - len(pinned))
        screen.note_cursor_shown(cursor is None or start <= cursor < start + cap)
        visible = pinned + lines[start : start + cap]
        more_below = start + cap < total
        visible = visible + [""] * (viewport - len(visible))
        return visible, True, more_below

    screen.note_cursor_shown(cursor is None or scroll <= cursor < scroll + viewport)
    visible = lines[scroll : scroll + viewport]
    more_above = scroll > 0
    more_below = scroll + viewport < total
    visible = visible + [""] * (viewport - len(visible))
    return visible, more_above, more_below


def _breathing_room(body_len: int, budget: int) -> int:
    """Rows of blank vertical padding a content-sized box should draw inside its borders.

    Every popup box (:func:`compose_dialog` floats and the :func:`compose_startup` splash
    boxes alike) aerates its layout with a blank row above and below the body whenever that
    doesn't cost visible content; on a terminal too short for both, the content wins and the
    box sits flush. Full-screen panels (:func:`compose_base`) deliberately skip this — they
    hold dense content and would only waste rows.

    Args:
        body_len: The body's full height in rows.
        budget: Rows the box may spend between its borders — on body and padding alike.

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
    """Wrap already-sliced body lines in a titled, scroll-aware panel.

    The bottom rule carries up to two things, and they share one subtitle rather than
    fighting over the line. The clip arrows sit centred as they always have. A
    ``caption`` — a screen's :attr:`~meshterm.ui.tui.screen.Screen.bottom_caption`, which
    today means the map's OpenStreetMap credit — is **right-justified**, so it lands as
    ``──── © OpenStreetMap ─╯``: one rule cell before the corner, the mirror of the way
    the title sits in the top rule, and not one cell of the body given up for it. Where
    both are wanted the caption joins the arrows in the same run and the run moves right,
    because the arrows are the half a reader is looking *for* and the caption is the half
    that must simply be present — dropping either would be the frame deciding something
    neither screen asked it to.

    Args:
        title: The screen's heading (empty for none).
        visible: The viewport's ANSI lines, already sliced and padded to height.
        more_above: Whether content continues above the slice.
        more_below: Whether content continues below the slice.
        border: Border style name (``"accent"`` for the focused screen).
        caption: A short muted credit for the bottom rule's right end (empty for none).
        flush: Drop the one-column padding inside each side border, for a body that fills
            the frame (see :attr:`~meshterm.ui.tui.screen.Screen.flush`).

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
    """Columns the bordered base frame takes from the body's width.

    Its two side borders, plus the padding column inside each unless the screen is
    :attr:`~meshterm.ui.tui.screen.Screen.flush`. One definition, because the session sizes
    a screen against this frame before the frame is drawn (``base_body_size``).
    """
    return 2 if flush else 4


#: The last framed base composition: ``(content key, rendered lines)``. Re-parsing the
#: sliced body (``Text.from_ansi``) and re-rendering it through the Panel is the priciest
#: part of a repaint (~8 ms on a full frame), and the idle tick recomposes an unchanged
#: screen every second — only the header above it moves. One slot suffices: there is only
#: ever one base screen per paint, and any content change (a keystroke, a scroll, new
#: rows) simply misses and re-renders.
_BASE_BOX_CACHE: tuple[tuple, list[str]] | None = None


#: The screen's own way out, lifted off the end of its footer hint. The grammar is
#: mandated — "navigation keys, then action keys, **Esc last**" — so the last atom is the
#: Esc clause wherever a screen has one, and its verb is the true one for that surface
#: (``back`` on a screen, ``keep`` in a value picker). The main menu is the one screen
#: whose way out is not Esc, which is inert there: its hint ends on ``^Q quit?`` instead,
#: until a typed filter puts ``Esc clear`` after it.
_WAY_OUT_ATOM = re.compile(r"(?:^|·\s*)(Esc\s+\S+|\^[A-Z]\s+quit\??)\s*$")

#: Rule cells the title keeps on each side before the bar gives the Esc hint back. Below
#: this the title is being crowded, which is the one thing the hint must not do.
_MIN_RULE = 2


def fitted_title(screen: Screen, room: int) -> str:
    """The heading to draw in ``room`` cells: the full title, or its short form where it won't fit.

    A title that states figures (``Contacts · 198 known + 123 archived``) would otherwise be
    cut by the rule it sits in, and the cut takes the end — the last figure. A screen that
    declares a :attr:`~meshterm.ui.tui.screen.Screen.short_title` gets that instead, the same
    atoms said tighter; one with none keeps its title and the frame's usual clipping.

    Args:
        screen: The screen whose heading is drawn.
        room: Cells the heading may take, not counting the space either side of it.
    """
    if screen.short_title and cell_len(screen.title) > room:
        return screen.short_title
    return screen.title


def _way_out(hint: str) -> str:
    """The way-out atom a screen's footer hint ends on (``Esc …``, ``^Q quit?``), or ``""``."""
    found = _WAY_OUT_ATOM.search(hint or "")
    return found.group(1) if found else ""


def _title_bar(
    screen: Screen,
    cols: int,
    more_above: bool,
    more_below: bool,
    status: Text | None = None,
) -> Text:
    """The borderless frame's one-row title bar: clip arrows, a centered title, the way out.

    The Panel border's whole vocabulary — where you are (title) and whether the body
    continues (the ``↑↓ more`` subtitle) — compressed into a single row so the body wins
    back three rows and four columns on the PicoCalc. Shape:
    ``↑↓ ──── Title ─────── Esc back``, echoing
    :func:`~meshterm.ui.menus.section_heading`'s heading language.

    **The clip arrows lead the row, and both are always drawn** (JP, 2026-08-31). A pair
    that appeared and vanished — and, half-shown, left a blank cell standing in for the
    arrow that wasn't there — made the reader compare the row against a memory of itself
    to learn something the row could simply state. So the pair is fixed furniture at the
    left edge, where the eye starts, and **colour** carries the reading: an arrow whose
    direction has more takes the border's own ``accent``, so it reads as part of the
    frame; one with nothing that way drops to ``muted``. That is the same live/dim
    language the F-key lane draws one row below (a dim chip keeps its label and loses its
    fill), and the two are the platform's only two indicators.

    The title is centered in the rule between the arrows and the tail, exactly as Rich's
    ``Panel`` centers its own ``title`` — the desktop's bordered frame this one stands in
    for. The rule itself takes ``border_style`` (``"accent"``) directly, the same bold
    weight a real border draws in, rather than :func:`~meshterm.ui.theme.hint_style`'s
    muted variant, which is for auxiliary text riding *alongside* a border (the Esc atom
    below, a footer hint on the other platform).

    The bar's tail carries **the way out** (JP, 2026-08-30). This platform has no footer
    hint line — the F-key lane stands where it would be, and the lane advertises only what
    its five slots do — so nothing on the screen said that Esc leaves, which is the one key
    every screen answers to and the reason no screen spends a row on a *Back* item. The
    verb is the screen's own (:func:`_way_out` lifts the atom off its footer hint, so a
    value picker says ``keep``, and the main menu, where Esc is inert, says ``^Q quit?``);
    a screen whose hint names no way out gets nothing. Compact by construction: where a
    long title would be crowded, the atom drops to its bare key and then out
    altogether — the title is what the reader came for, and the arrows are two cells
    nothing else can spend.

    Where the platform has **no header row** (the Cardputer, JP 2026-10-03), the header's
    atoms arrive as ``status`` and are pinned to the bar's right end, after the way out:
    ``↑↓ ── Title ──── Esc back ● 3 ⣷ 87%``. That is the top-right corner the badges and
    battery always had, one row up, and every screen gets the header's row back. The status
    gives way to nothing — an unread count and a dying battery are what it exists to say —
    so the way out sheds first, as it does for a long title, and a title too long even then
    is cut short on an ellipsis rather than pushing the status off the end of the row.

    Args:
        screen: The screen whose heading the bar carries.
        cols: Bar width in cells.
        more_above: Whether the body continues above the viewport.
        more_below: Whether the body continues below it.
        status: The header's atoms, for a platform without a header row; ``None`` (or
            empty: nothing unread, no pack) draws the bar exactly as it is with a header.
    """
    border = "accent"
    head_span = 3  # the arrow pair, plus the space parting it from the rule
    status = status if status is not None and status.cell_len else None
    stat_span = status.cell_len + 1 if status else 0  # the space in front of the status
    # The short form is chosen against the bar with no Esc tail at all: the title is what
    # the reader came for, so the tail gives way to it before it gives way to the tail.
    title = fitted_title(screen, cols - head_span - stat_span - 2 - 2 * _MIN_RULE)
    label_w = cell_len(title) + 2 if title else 0  # a space either side
    atom = _way_out(screen.footer_hint)
    for esc in (atom, atom.split()[0] if atom else "", ""):
        tail_span = cell_len(esc) + 1 if esc else 0  # the space in front of the tail
        if not esc or cols - head_span - tail_span - stat_span - label_w >= 2 * _MIN_RULE:
            break
    rule_span = max(0, cols - head_span - tail_span - stat_span)
    heading = screen.bar_title(title, title_style(border)) if title else Text()
    if status and label_w > rule_span - 2:
        # A rule cell either side, and whatever is left for the title; the status stays.
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
    """The header's rows above the frame: its one line, or none where it has no row.

    The header is a single status line, cropped to one row so a narrow terminal never wraps
    it onto a second (which would push the panel down and misreport its height). A platform
    without a header row (:attr:`~meshterm.platforms.Platform.header_row`) draws none here —
    the title bar carries the header's atoms instead (see :func:`_title_bar`) — and this is
    the one place that says so, for :func:`compose_base` and for the session sizing a body
    against it alike.
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
    """Compose the full-screen ANSI view: header, the base screen's panel, and a footer.

    Args:
        header: The persistent header (banner + live status) — a row of its own, or, on a
            platform without a header row, the title bar's right end (see
            :func:`header_lines`).
        base: The screen filling the background (the deepest non-floating layer).
        footer_hint: The active screen's key hint, shown at the very bottom.
        cols: Terminal width.
        rows: Terminal height.
        footer_lane: Builds the footer row (the PicoCalc F-key lane) that replaces the
            hint string when the platform runs fixed F-key hints. Called *after* the body
            renders, since a lane dims its slots from the screen's scroll metrics — which
            this paint has only just recorded (see :func:`~meshterm.ui.tui.fkeys.default_lane`).

    Returns:
        An ANSI string of exactly ``rows`` lines, each within ``cols`` columns.
    """
    global _BASE_BOX_CACHE
    platform = get_platform()
    head = header_lines(header, cols)
    header_h = len(head)

    def footer_row() -> RenderableType:
        """The bottom line: the platform's F-key lane, else the muted hint string."""
        if footer_lane is not None:
            return footer_lane()
        return Text.from_markup(f"[muted]{footer_hint}[/muted]")

    if platform.frame_border:
        viewport = max(1, rows - header_h - 1 - 2)  # minus footer(1) and panel border(2)
        # Record the viewport *before* the body renders, so a screen that windows a
        # list inside itself (see :class:`~meshterm.ui.tui.screen.ListWindow`) can size
        # its chrome to the frame it is about to be sliced into.
        base.note_viewport(viewport)
        body_lines = base.render_body(cols - panel_inset(base.flush))
        visible, more_above, more_below = _visible_slice(base, body_lines, viewport)
        # The panel wrap is a pure function of what's between its borders: memoize it so
        # the repaints that change nothing below the header (the 1 Hz tick) skip the
        # ANSI re-parse and Panel re-render. A dynamic footer (the F-key lane, which can
        # flip with the physical Shift key alone) has no place in the key, so it renders
        # uncached — that combination doesn't arise: the lane belongs to the borderless
        # platform below.
        # The caption belongs in the key like the title does: the map's credit collapses on
        # a keystroke that need not change a single body line, and a memo without it would
        # keep serving the rule the reader has already grown out of.
        caption = base.bottom_caption
        # Rich sets the padded title into the rule's inner ``cols - 4`` cells.
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
        # Borderless chrome: a one-row title bar instead of the Panel's border and
        # padding — the body wins the full terminal width and one extra row.
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
    # Guarantee we never exceed the terminal height (pt would otherwise clip unpredictably).
    if len(lines) > rows:
        lines = lines[:rows]
    else:
        lines += [""] * (rows - len(lines))
    return "\n".join(lines)


def _ansi_width(line: str) -> int:
    """Return the display width of an ANSI line, ignoring its trailing padding."""
    return cell_len(Text.from_ansi(line).plain.rstrip())


def _center(lines: list[str], cols: int) -> list[str]:
    """Left-pad each ANSI line so the block is horizontally centered within ``cols``."""
    out: list[str] = []
    for line in lines:
        pad = max(0, (cols - cell_len(Text.from_ansi(line).plain)) // 2)
        out.append(" " * pad + line)
    return out


def _banner_lines(banner: Sequence[str], cols: int) -> list[str]:
    """Center the wordmark rows (already-coloured ANSI) as one left-aligned block.

    Each row is padded to the block's widest display width first, so the shared left margin
    keeps the art internally aligned rather than centering every row on its own axis.

    The rows are art, not renderables, so they never pass through
    :func:`~meshterm.ui.tui.render.render_to_ansi` — the fold is applied here instead, and
    the wordmark's truecolour lands on the palette slots the art was drawn against rather
    than on whatever the console's own downsample picks.
    """
    if not banner:
        return []
    widths = [cell_len(Text.from_ansi(row).plain) for row in banner]
    width = max(widths)
    padded = [fold_text(row) + " " * (width - w) for row, w in zip(banner, widths, strict=True)]
    return _center(padded, cols)


#: The wordmark as the splash last drew it: ``(banner, cols)`` and what that came to. One
#: slot, because there is one splash and one terminal size at a time.
_BANNER_CACHE: tuple[tuple, tuple[list[str], int]] | None = None


def _fitted_banner(banner: Sequence[str], cols: int) -> tuple[list[str], int]:
    """The wordmark drawn at ``cols`` — fitted, folded and centred — and the art's width.

    Fitting the mark is the dearest thing the splash draws: a mark wider than the terminal
    sends :func:`~meshterm.ui.logo.load_logo` back to the disk for every size until one
    fits, each one parsed and measured, and then every row is decoded again to centre it.
    None of that moves from one paint to the next, and the splash paints on every keystroke
    and every idle tick. Done fresh each time it was nine-tenths of a splash frame at
    53×26, which on the PicoCalc put a visible wait behind every ↑↓ on the device list.

    Args:
        banner: The wordmark rows the screen names (usually the full-size mark).
        cols: Terminal width.

    Returns:
        The centred rows to draw — shared with the memo, so never mutate them — and the
        display width of the mark actually chosen, for hanging the footnote off its edge.
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
    """Drop ``·`` atoms from a footer hint, right to left, until it fits ``width``.

    The chromeless splash has one row of border for its hint and a box no wider than the
    terminal less its gutter — 47 cells on the PicoCalc — while the hint itself grows as the
    highlight moves onto a row with more keys to offer. Cutting the line at the edge takes
    the *end* of it, and the end is ``Esc``: the one atom that must survive, being the only
    way out. So atoms are dropped instead, from the right and never the last one, which
    spends the cells on the keys nearest the front of the sentence — the ones that move and
    commit — and gives up the optional ones a screen advertises elsewhere.

    ``shed_first`` overrides that order for atoms whose key the reader would find anyway. A
    scroll atom is the example: ←→ are the keys already named by the leading move atom, so
    trying them costs nothing and teaches the rest, while a bare-letter shortcut is
    unguessable and has nowhere else to be advertised on a chromeless splash. Given the
    choice between those two, the sentence keeps the letter.

    Args:
        hint: The composed hint sentence.
        width: Cells available.
        shed_first: Atoms to drop before any others, in the order given.

    Returns:
        The hint, or as much of its head plus its final atom as fits (ellipsized only if
        even those cannot).
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
        del atoms[-2]  # the last atom is Esc's; drop what sits in front of it
    fitted = " · ".join(atoms)
    if cell_len(fitted) <= width:
        return fitted
    return crop_cells(Text(fitted), 0, max(1, width - 1)).plain + "…"


def compose_startup(screen: Screen, cols: int, rows: int) -> str:
    """Compose a chromeless splash: a centered wordmark above a content-sized panel.

    Unlike :func:`compose_base`, this draws no header or footer status bars and does not
    stretch the panel across the terminal — the box is sized to its own content (the device
    list) and the whole block is centered on screen. Used for the startup device picker.

    Args:
        screen: The chromeless base screen (its ``banner`` supplies the wordmark).
        cols: Terminal width.
        rows: Terminal height.

    Returns:
        An ANSI string of exactly ``rows`` lines, each within ``cols`` columns.
    """
    # A screen names its wordmark; the width to draw it at is only known here, so this is
    # where the size is chosen — a narrow terminal gets a narrow mark rather than a torn one.
    # ``logo_w`` is the chosen mark's own width, so the footnote below can hang off its right
    # edge (the wordmark is centered as one block, so every row shares one left margin).
    banner, logo_w = _fitted_banner(screen.banner or (), cols)
    banner_h = len(banner)
    gap = 1 if banner else 0  # the blank line under the banner
    logo_right = max(0, (cols - logo_w) // 2) + logo_w

    # Size the box to its widest real row (probe at a generous width, then measure), never
    # wider than the terminal and never narrower than the title/hint it must show.
    probe = max(10, min(cols - 6, 100))
    probe_lines = screen.render_body(probe)
    measured = max((_ansi_width(line) for line in probe_lines), default=10)
    # Size to the fullest the footer can get, not this frame's — a screen whose hint grows
    # as the highlight moves (a select's per-row "Del remove") reports that width here, so
    # the box is reserved for it up front and never widens mid-navigation.
    sizing_footer = getattr(screen, "sizing_footer_hint", screen.footer_hint)
    inner_w = max(measured, cell_len(screen.title), cell_len(sizing_footer))
    inner_w = max(10, min(inner_w, cols - 6))
    # The hint is fitted to the box rather than cut off by it (see :func:`fit_hint`).
    hint_text = fit_hint(
        screen.footer_hint, inner_w, shed_first=getattr(screen, "spare_hint_atoms", ())
    )

    # The probe render is the answer whenever it was already made at the width we settled
    # on — the common case, since a body narrower than the probe *is* what set inner_w.
    # The splash repaints on every tick while the device list sits there, and rendering
    # the same body twice was a measured third of that paint on the PicoCalc.
    body_lines = probe_lines if inner_w == probe else screen.render_body(inner_w)
    footnote_h = 1 if screen.footnote else 0  # the note sits directly under the logo

    # A terminal too short for the whole block gives rows back in a fixed order, cheapest
    # first: the blank line under the mark (breathing room, and nothing else), then the top
    # of the mark itself — the globe over the lettering, up to _BANNER_CROP_ROWS of it. The
    # lettering and its copyright are the part that says what this is, so they are the part
    # that never moves: the mark thins from above while the box below keeps its rows. Past
    # that the block is simply taller than the screen and the trailing clip still applies.
    short = banner_h + footnote_h + gap + len(body_lines) + 2 - rows
    if short > 0 and gap:
        gap = 0
        short -= 1
    if short > 0 and banner:
        banner = banner[min(short, _BANNER_CROP_ROWS, banner_h) :]
        banner_h = len(banner)

    # Pin the banner to a fixed vertical anchor that depends only on the terminal height and
    # the banner's own (constant) height, so the wordmark never moves as the box below it
    # swaps contents between splash states (device list → spinner → message). The box hangs
    # from just under the banner and only *it* grows or shrinks; the logo stays put. The
    # anchor sits at 2/5 of the terminal rather than the midline so the block reads centered
    # once the device list has populated (the box only ever grows downward); the brief small
    # states sit slightly high, the conventional optical placement for dialogs.
    top = max(0, rows * 2 // 5 - banner_h - gap)
    # Rows left for the box below the fixed banner block: the panel border is 2 rows.
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

    # A small muted line (e.g. a copyright notice) sits immediately under the logo, its right
    # edge hung off the logo's right edge so the two read as one signed block.
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

    No header, no footer, no title bar, no box, no wordmark — the frame
    :attr:`~meshterm.ui.tui.screen.Screen.bare` asks for. The body is drawn at the
    terminal's full width (the screen lays its own rows out across it) and sits a little
    above true centre when it is shorter than the frame, the optical placement everything
    else here anchors to. A body taller than the frame is windowed from the top and
    scrolls, so a code that does not fit is at least whole while the reader is at the top
    of it, with the URL under it a page down.

    Args:
        screen: The bare base screen.
        cols: Terminal width.
        rows: Terminal height.

    Returns:
        An ANSI string of exactly ``rows`` lines, each within ``cols`` columns.
    """
    # The height is known here and nowhere else, and a bare screen may size its body
    # to it (the share screen fits its code to the rows), so it is noted *before* the
    # body is asked for rather than after, the way the framed composers record it.
    screen.note_viewport(rows)
    body_lines = screen.render_body(cols)
    while body_lines and not _ansi_width(body_lines[-1]):
        body_lines.pop()  # a trailing blank row would push the block off its anchor
    visible, _more_above, _more_below = _visible_slice(screen, body_lines, rows)
    top = max(0, (rows - len(body_lines)) * 2 // 5)
    lines = [""] * top + visible[: rows - top]
    return "\n".join(lines)


def _dialog_layout(screen: Screen, cols: int, rows: int) -> tuple[int, int, int, list[str]]:
    """Size a floating dialog: ``(outer_width, vpad, viewport, body_lines)``."""
    # Most dialogs stretch to a generous cap; a screen may instead request a natural width
    # (a short confirm sized to its content), still bounded to the terminal. A grow-only
    # screen's natural width ratchets like its height, so the box never narrows either.
    # What the cap leaves behind is the backdrop gutter the box floats over, and how many
    # columns that is worth is the platform's call (see Platform.dialog_margin).
    cap = min(cols - get_platform().dialog_margin, 100)
    natural = getattr(screen, "dialog_width", None)
    max_w = cap if natural is None else max(24, min(cap, screen.ratchet_width(natural)))
    # Rows the box may spend between its borders — on body lines and breathing room alike:
    # the frame less the two borders and the rows it leaves to the frame around it (the
    # header, the footer, and on a tall enough frame a row of air each side — see
    # Platform.dialog_row_margin).
    budget = max(3, rows - 2 - get_platform().dialog_row_margin)
    # Record the budget as the provisional viewport before the body renders, so a
    # dialog that windows a list inside itself (the path composer) can size to what
    # it may spend; the slice records the real, body-sized viewport afterwards.
    screen.note_viewport(budget)
    body_lines = screen.render_body(max_w - 4)
    # A grow-only screen (the packet viewer paging between packets) sizes to the tallest
    # body it has shown, not this one, so a shorter body keeps the larger box instead of
    # re-centring smaller — the frame blank-pads the slack. The breathing room is taken
    # from that ratcheted height, so the box stays put as the body shrinks below it.
    body_h = screen.ratchet_viewport(max(1, len(body_lines)))
    vpad = _breathing_room(body_h, budget)
    viewport = min(budget - 2 * vpad, body_h)
    return max_w, vpad, viewport, body_lines


#: Memoized dialog compositions, keyed by everything the box is a function of. Dialogs
#: stack (a confirm over a picker over a menu), and each layer recomposes on every
#: repaint of the frame beneath it, so a single slot would thrash — a handful covers the
#: deepest realistic stack, LRU-evicted as dialogs change.
_DIALOG_CACHE: OrderedDict[tuple, str] = OrderedDict()

#: Dialog compositions the memo keeps.
_DIALOG_CACHE_MAX = 12


def _dialog_hint(screen: Screen) -> str:
    """The hint a floating dialog's own border carries — often none at all.

    A dialog is drawn over a frame that already has a footer row, and *what that row is*
    decides whether the box needs to speak at all:

    - Where the footer is the **hint line** (the desktop), it draws the top screen's hint
      — this dialog's — in full, on the row every screen's keys have always been read
      from. A subtitle repeating it was the same sentence twice on one frame, once in the
      border and once at the bottom of the terminal (JP, 2026-08-31), so the border says
      nothing and the box leaves the reader's eye on the one place hints live.
    - Where the footer is the **F-key lane** (the PicoCalc), there is no hint line at all
      and the lane speaks only for its five chips, so the border is the only thing that
      can name Enter, Esc or the arrows — it keeps the hint. What the lane *does* say it
      says already, one row below: :func:`~meshterm.ui.tui.fkeys.strip_lane_atoms` lifts
      out the atoms whose every key is a chip on that row.

    Resolved on each paint rather than folded into a screen's hint once, because both
    halves move: a screen rewrites its hint as its content changes (the packet viewer
    drops to a bare ``Esc close`` on a single packet) and its lane is read fresh every
    frame.

    The chromeless startup splash is not this case and never passes through here — it has
    no footer row of any kind, so :func:`compose_startup` keeps drawing the hint in its
    own panel's subtitle on both platforms.
    """
    if not get_platform().footer_fkeys:
        return ""
    return fkeys.strip_lane_atoms(screen.footer_hint, screen.fkey_lane)


def _dialog_subtitle(hint: str, more_above: bool, more_below: bool, border: str) -> str | None:
    """A floating box's bottom-border legend: the clip arrows, the hint, or both.

    With no hint to carry (the desktop — see :func:`_dialog_hint`) the arrows keep the
    base frame's own wording, ``↑↓ more``, exactly as :func:`_panel_box` labels them: a
    border that has fallen silent still says the one thing every border here says, in the
    vocabulary the frame around it uses.
    """
    arrow = ""
    if more_above or more_below:
        arrow = ("↑" if more_above else " ") + ("↓" if more_below else " ")
    if not hint:
        return f"[{hint_style(border)}]{arrow} more[/]" if arrow else None
    legend = f"{arrow} · {hint}" if arrow else hint
    return f"[{hint_style(border)}]{legend}[/]"


def compose_dialog(screen: Screen, cols: int, rows: int) -> str:
    """Compose a centered dialog panel for a floating screen, bounded to the terminal.

    Args:
        screen: The floating (top) screen.
        cols: Terminal width.
        rows: Terminal height.

    Returns:
        An ANSI string sized to the dialog's content (never larger than the terminal).
    """
    max_w, vpad, viewport, body_lines = _dialog_layout(screen, cols, rows)
    visible, more_above, more_below = _visible_slice(screen, body_lines, viewport)
    border = getattr(screen, "border_style", "accent")
    # The resolved hint, not the screen's own: it is what the border actually draws, and
    # it moves with the lane below as well as with the screen's hint (see _dialog_hint).
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


#: Memoized composited rows, keyed by everything the merge is a function of: the base row
#: under the box, the box's own line, and where it sits. A dialog repaints with almost every
#: row identical to the last paint (only the highlight moved, or nothing did but the header's
#: pulse), and each unchanged row then costs a dict hit instead of two ANSI parses, two cell
#: crops and a render.
_OVERLAY_CACHE: OrderedDict[tuple, str] = OrderedDict()

#: Composited rows the memo holds — a few dialogs' worth of rows.
_OVERLAY_CACHE_MAX = 256


def _overlay_row(base_row: str, box_line: str, left: int, cols: int) -> str:
    """Lay one box line over one base row at cell ``left``, keeping both sides' styling."""
    key = (base_row, box_line, left, cols)
    cached = _OVERLAY_CACHE.get(key)
    if cached is not None:
        _OVERLAY_CACHE.move_to_end(key)
        return cached
    under = Text.from_ansi(base_row)
    over = Text.from_ansi(box_line)
    merged = crop_cells(under, 0, left)
    # A base row shorter than the box's left edge (a blank line under a wide dialog) still
    # has to hold the box out at its true column, so pad the gap rather than closing it.
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
    """Lay a dialog box over the full-screen rows beneath it, centred, and return the result.

    The floating layers are normally placed by prompt_toolkit's ``FloatContainer``, which is
    why a frame carrying one used to fall through to prompt_toolkit's renderer whole — and
    pay its full grid rebuild and diff (70-125 ms on the PicoCalc) on every keystroke in
    every dialog. There is nothing to that placement we cannot do here: an unanchored float
    with no size of its own is centred on the frame, horizontally at its widest line and
    vertically at its line count, clipped to the terminal. Doing it ourselves keeps dialogs
    on the row-diff path with the rest of the app.

    Args:
        base_rows: The frame beneath, one ANSI string per terminal row.
        box: The composed dialog, its rows joined by newlines (see :func:`compose_dialog`).
        cols: Terminal width.
        rows: Terminal height.

    A box that lands on the borderless frame's **title bar** takes the whole row: the
    cells either side of it are blanked instead of letting the bar show through. Every
    other row's gutter is the backdrop the box floats over, and reads as one; a bar cut by a
    box is fragments of chrome — the base's clip arrows, the last letters of its way out,
    and, where the bar carries the header's atoms (the Cardputer, whose tall dialogs draw
    over the bar), the tail of a battery gauge, where a stray ``7%`` reads as a reading.

    Returns:
        A new list of rows with the box merged in. Rows the box doesn't reach are passed
        through untouched — the same strings, so the row diff skips them for free.
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


#: The row the borderless frame's title bar sits on — under the header's one line, or
#: the top row where the platform has no header row — or ``None`` on a bordered frame,
#: whose top rule is the panel's own and has no fragments to leave (bound in :func:`_bind`).
_BAR_ROW: int | None = None


@on_platform
def _bind(platform: Platform) -> None:
    """Drop the composition memos on a platform switch — their output bakes the theme in.

    Also binds where the title bar sits (:data:`_BAR_ROW`), which only the platform decides.
    Registered at module bottom so the immediate first run (see
    :func:`~meshterm.platforms.on_platform`) finds every cache already defined.
    """
    global _BASE_BOX_CACHE, _BANNER_CACHE, _BAR_ROW
    _BAR_ROW = None if platform.frame_border else int(platform.header_row)
    _BASE_BOX_CACHE = None
    _BANNER_CACHE = None
    _DIALOG_CACHE.clear()
    _OVERLAY_CACHE.clear()
