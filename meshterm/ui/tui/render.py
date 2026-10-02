# SPDX-License-Identifier: Apache-2.0
"""The single Rich-to-prompt_toolkit bridge.

The TUI keeps rendering in Rich (all existing tables, panels, and text widgets are reused
verbatim) and lets prompt_toolkit own the terminal, input, and resize events. This module
is the seam between the two: it renders any Rich renderable to an ANSI string at a chosen
width, which prompt_toolkit then draws via :class:`prompt_toolkit.formatted_text.ANSI`.

Because rendering is redone at the *current* width on every repaint, content reflows
automatically when the terminal is resized.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from io import StringIO

from rich.cells import cell_len
from rich.console import Console, RenderableType
from rich.text import Span, Text

from ...platforms import Platform, on_platform
from ..pathline import PATH_INK
from ..theme import active_theme, fold_text, is_identity_style

#: Cache one headless render console per width. Consoles are cheap but repaint happens on
#: every keystroke and on the live-monitor timer, so caching avoids needless churn. Keyed
#: by width because a console's width is fixed at construction; emptied whenever the
#: platform switches, since the theme and colour system are baked in at construction.
_CONSOLES: dict[int, Console] = {}

#: The colour depth the rasterizer emits at — bound per platform. ``"standard"`` on the
#: 16-slot console: the theme's ``color(N)`` styles pass through as exact slot indices,
#: and any stray truecolor (a node hue, a map feature) is downsampled by Rich to the
#: nearest conventional slot — which the palette deliberately keeps hue-aligned (see
#: ``theme._VT_SLOTS``), so even strays land in their own colour family.
_COLOR_SYSTEM = "truecolor"


#: Memoized ANSI per (content, width) for the *describable* renderables — a ``str`` or a
#: :class:`Text`, whose rendering is a pure function of their plain text, spans, and flags.
#: The TUI recomposes the whole frame on every keystroke and on the idle tick, and the
#: list-family screens rasterize row by row through :func:`render_to_ansi`; between two
#: paints almost every row is byte-identical, so this cache turns the per-row console
#: render (~0.15 ms each) into a dict hit. Tables, panels and groups have no cheap content
#: key and skip the cache (their callers memoize at their own level where it matters).
_ANSI_CACHE: OrderedDict[tuple, str] = OrderedDict()

#: Entries the ANSI cache holds before evicting least-recently-used ones. Sized for a few
#: screenfuls of distinct rows (a busy list, its filtered variants, a dialog over it) at
#: well under ~2 MB; small enough that even the PicoCalc carries it without noticing.
_ANSI_CACHE_MAX = 4096

#: Generation stamp folded into every cache key. A platform switch swaps the theme and
#: colour system baked into the render consoles, so it bumps the generation instead of
#: trusting eviction — a stale entry can then never be *read*, even mid-eviction.
_CACHE_GEN = 0


@on_platform
def _bind(platform: Platform) -> None:
    """Re-bind the rasterizer's colour depth and drop stale consoles on a switch."""
    global _COLOR_SYSTEM, _CACHE_GEN
    _COLOR_SYSTEM = "truecolor" if platform.truecolor else "standard"
    _CONSOLES.clear()
    _ANSI_CACHE.clear()
    _CACHE_GEN += 1


def _cache_key(renderable: RenderableType, width: int, no_wrap: bool) -> tuple | None:
    """A content key for a cacheable renderable, or ``None`` when it has no cheap one.

    A ``str`` *is* its own content (markup included). A :class:`Text` renders as a pure
    function of its plain text, span list, base style, and wrap flags — everything the
    key captures (span styles via ``str``, which :class:`~rich.style.Style` memoizes).
    Anything else — a table, a group, a panel — would need a deep walk to describe, so it
    reports ``None`` and renders uncached.
    """
    if isinstance(renderable, str):
        return ("s", renderable, width, no_wrap, _CACHE_GEN)
    if isinstance(renderable, Text):
        return (
            "t",
            renderable.plain,
            tuple((s.start, s.end, str(s.style)) for s in renderable.spans),
            str(renderable.style),
            renderable.no_wrap,
            renderable.overflow,
            renderable.justify,
            renderable.tab_size,
            width,
            no_wrap,
            _CACHE_GEN,
        )
    return None


def _console(width: int) -> Console:
    """Return a themed, headless console that emits ANSI at the given width.

    Args:
        width: Target render width in columns.

    Returns:
        A cached :class:`rich.console.Console` writing to an internal buffer.
    """
    console = _CONSOLES.get(width)
    if console is None:
        console = Console(
            theme=active_theme(),
            width=width,
            file=StringIO(),
            force_terminal=True,
            color_system=_COLOR_SYSTEM,
            # This console is a rasterizer, not terminal output: its ANSI is re-parsed
            # by prompt_toolkit, and the TUI's hues are semantics (node identity,
            # recency heat), not decoration. So a NO_COLOR environment must not strip
            # them here — the scripted CLI's real console (theme.build_console) is the
            # one that honours NO_COLOR.
            no_color=False,
            highlight=False,
            soft_wrap=False,
        )
        _CONSOLES[width] = console
    return console


def _whiten_identities(renderable: RenderableType) -> RenderableType:
    """On the cursor row, a node's name is drawn in the highlight's white, not its hue.

    A node's name is always coloured by its key, so a column of them reads as a column of
    identities — but the row the ``❯`` points at is answering a different question, and a
    keyed hue on it competes with the very highlight that says "this one". So the row that
    declares itself the cursor (its *base* style is ``cursor``, which is how every list and
    every screen drawing its own rows marks it) has its identity spans folded to that same
    white, and the row reads as one thing.

    Only :func:`~meshterm.ui.theme.is_identity_style` hues fold. Everything else on the row
    is untouched — the heard-age heat, an SNR reading, a red badge, a path line's chip
    fills, the grey of a node no key could place — because none of those are the identity
    the highlight is standing in for.

    **A path line is spared**, wherever it sits on the row. There the hue is not decoration
    on a name: it is what tells one hop from the next, and what a route graph drawn above
    the row is cross-referenced by — its labels are a marker and one byte of hash, and the
    colour is what says *which node*. So a picked route kept its hops' colours in chip form
    (the fills were never in this vocabulary) and lost them in arrow form, which is every
    path line on the PicoCalc and on any terminal without the powerline glyphs (JP,
    2026-08-30). The widget stamps its own extent with
    :data:`~meshterm.ui.pathline.PATH_INK` and the stamp arrives here as a span over the
    hops (``Text.append_text`` keeps a child's base style as one), so the route is spared
    by name rather than by each screen remembering to ask.

    Done here because this is where a row *is* a row: one boundary every screen's rows
    already leave through, rather than a rule each of them has to remember. Callers hand us
    read-only :class:`Text` (the ANSI cache keys on their content), so the rewrite is on a
    copy.

    Args:
        renderable: Whatever is about to be rendered.

    Returns:
        The renderable, or a cursor row's copy with its name hues turned white.
    """
    if not isinstance(renderable, Text) or str(renderable.style) != "cursor":
        return renderable
    spans = renderable.spans
    if not any(is_identity_style(str(span.style)) for span in spans):
        return renderable
    routes = [(span.start, span.end) for span in spans if str(span.style) == PATH_INK]
    out = renderable.copy()
    out.spans = [
        Span(span.start, span.end, "cursor")
        if is_identity_style(str(span.style)) and not _within(span, routes)
        else span
        for span in spans
    ]
    return out


def _within(span: Span, runs: list[tuple[int, int]]) -> bool:
    """Does ``span`` lie inside one of ``runs`` — a hop inside a marked path line?"""
    return any(start <= span.start and span.end <= end for start, end in runs)


def render_to_ansi(renderable: RenderableType, width: int, *, no_wrap: bool = False) -> str:
    """Render a Rich renderable to an ANSI string at ``width`` columns.

    Args:
        renderable: Any Rich renderable (table, panel, text, group, markup string).
        width: Target width in columns; content wraps/pads to it.
        no_wrap: When ``True``, keep the content on a single line — show what fits and
            crop the overflow (with an ellipsis) instead of wrapping onto more rows.

    Returns:
        The rendered output as an ANSI-escaped string, without a trailing newline.
    """
    width = max(1, width)
    key = _cache_key(renderable, width, no_wrap)
    if key is not None:
        cached = _ANSI_CACHE.get(key)
        if cached is not None:
            _ANSI_CACHE.move_to_end(key)
            return cached
    renderable = _whiten_identities(renderable)
    console = _console(width)
    with console.capture() as capture:
        if no_wrap:
            console.print(renderable, end="", no_wrap=True, overflow="ellipsis", crop=True)
        else:
            console.print(renderable, end="")
    # THE render boundary: every visible string in the TUI leaves through here, so this
    # one call is what makes the PicoCalc glyph contract hold app-wide (identity on the
    # regular platform). Widths were measured on the pre-fold text; the fold preserves
    # cell counts (wide emoji become glyph + pad), so the layout above survives it.
    out = fold_text(capture.get())
    if key is not None:
        _ANSI_CACHE[key] = out
        if len(_ANSI_CACHE) > _ANSI_CACHE_MAX:
            _ANSI_CACHE.popitem(last=False)
    return out


def render_lines(renderable: RenderableType, width: int, *, no_wrap: bool = False) -> list[str]:
    """Render a Rich renderable to a list of ANSI lines at ``width`` columns.

    Each returned line is an independently styled ANSI string, so the caller can slice a
    vertical viewport (for scrolling) without splitting escape sequences.

    Args:
        renderable: Any Rich renderable.
        width: Target width in columns.
        no_wrap: When ``True``, keep the content to a single cropped line (see
            :func:`render_to_ansi`).

    Returns:
        The rendered lines, newline-free. A trailing empty line (from the final newline)
        is dropped so line counts match visible rows.
    """
    ansi = render_to_ansi(renderable, width, no_wrap=no_wrap)
    lines = ansi.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def query_line(query: str, width: int) -> str:
    """The live find/filter query echoed as one body line: ``/hub``, warn-styled.

    THE way a find-as-you-type screen shows what has been typed. Every such screen draws
    this same line above whatever the query narrows — the select list above its rows, the
    path composer above its suggestions, the map above its canvas, the walk above its link
    list — so the text you are typing is always in the same shape, in the same place, in
    the one colour nothing else in a body uses.

    The map and the walk *also* echo the query in their footer hint, with its editing keys
    beside it. That line is not drawn on a platform whose footer is the F-key lane (see
    :attr:`~meshterm.platforms.Platform.footer_fkeys`), which is exactly why those two
    call this: without it, the query would be invisible on the device while you typed it.
    They gate the call on that flag rather than drawing the line twice on the desktop.

    Args:
        query: The current query (callers only call this when it is non-empty).
        width: Inner content width in columns.

    Returns:
        One rendered ANSI line.
    """
    return render_to_ansi(query_text(query), width, no_wrap=True)


def query_text(query: str) -> Text:
    """The same echo as a styled run, for a surface that lays the row out itself.

    :func:`query_line` is the whole line and the usual call; this is the run inside it,
    wanted where something else shares the row. The map is the one such caller: its credit
    is pinned to the row's right end (see :mod:`meshterm.ui.attribution`), so the query has
    to arrive croppable rather than already rendered to the full width. Both go through
    this one constructor, so the shape and the ``warn`` colour cannot drift apart.

    Args:
        query: The current query (callers only call this when it is non-empty).

    Returns:
        The echo, ``warn`` and ``no_wrap``.
    """
    return Text(f"/{query}", style="warn", no_wrap=True)


def right_aligned_tail(body: Text, tail: Text, width: int) -> Text:
    """Lay ``body`` out with ``tail`` pinned to the right edge of its last line.

    ``body`` wraps at ``width`` as usual; ``tail`` — a short status such as the chat
    compose bar's byte counter — is right-aligned on ``body``'s final wrapped line when
    it still fits there (after at least one blank cell), otherwise on a new line of its
    own, also right-aligned. So the tail reads as a steady gauge in the corner rather
    than trailing the cursor and wrapping along with the text.

    Args:
        body: The wrappable leading content (e.g. the input line, cursor block included).
        tail: The short run to pin to the right edge.
        width: Total render width in columns.

    Returns:
        A single :class:`Text` with embedded newlines, ready for :func:`render_lines`.
    """
    width = max(1, width)
    console = _console(width)
    lines = list(body.wrap(console, width)) or [Text("")]
    combined = Text()
    for line in lines[:-1]:
        combined.append_text(line)
        combined.append("\n")
    last = lines[-1]
    gap = width - last.cell_len - tail.cell_len
    if gap >= 1:
        combined.append_text(last)
        combined.append(" " * gap)
    else:
        combined.append_text(last)
        combined.append("\n")
        combined.append(" " * max(0, width - tail.cell_len))
    combined.append_text(tail)
    return combined


def crop_cells(text: Text, start: int, width: int) -> Text:
    """Cut a single-line styled Text to the cell window ``[start, start + width)``.

    The horizontal-scroll primitive: a row wider than its lane is shown ``width``
    cells at a time, shifted ``start`` cells in, styling preserved. Boundaries are
    measured in display cells, so a double-width glyph straddling either edge is
    dropped (replaced by a pad space on the left) rather than half-shown.

    Args:
        text: The styled line to crop (treated as a single line).
        start: Cells to skip from the left (clamped at 0).
        width: Cells the window spans.

    Returns:
        A new :class:`Text` at most ``width`` cells wide, ``no_wrap`` set so the
        caller can drop it straight into a row.
    """
    start = max(0, start)
    plain = text.plain
    index, pad = len(plain), 0
    if start == 0:
        index = 0
    else:
        acc = 0
        for i, ch in enumerate(plain):
            w = cell_len(ch)
            if acc + w > start:
                # This character crosses the cut: keep it when it starts exactly at
                # the boundary, else drop it and pad for its protruding half.
                index, pad = (i, 0) if acc == start else (i + 1, acc + w - start)
                break
            acc += w
    out = Text(" " * pad, no_wrap=True)
    out.append_text(text[index:])
    out.truncate(max(0, width))
    return out


def render_hanging(
    prefix: Text,
    body: Text,
    width: int,
    *,
    indent: int,
    whole: Sequence[tuple[int, int]] = (),
    gutter: int = 0,
) -> list[str]:
    """Render ``prefix + body`` at ``width``, wrapping the body with a hanging indent.

    The first visual line carries ``prefix`` followed by the body; every wrapped
    continuation line is padded by ``indent`` columns so it aligns under the body rather
    than falling back to column zero. Used by the chat transcript so a wrapped message
    lines up with its own first line instead of with the timestamp gutter.

    ``whole`` names runs of the body that must not be cut where the screen can hold them —
    a URL, which a terminal can only open, and a reader only copy, while it sits on one
    line. A run that fits the hanging lane needs nothing: the wrap already moves a word
    to the next line rather than splitting it. One that outruns the lane but not the
    screen steps out of the block onto a line of its own, starting at ``gutter`` (or as
    far right of it as still fits); the body resumes under its indent after it, unless
    what follows is short enough to finish the run's own line. Only a run wider than the
    whole screen is folded, as before, since no line could hold it.

    Args:
        prefix: The leading run (e.g. a pointer + timestamp) shown once, on the first line.
        body: The wrappable message text; styling (mentions, glyphs) is preserved.
        width: Total render width in columns.
        indent: Columns to indent continuation lines by (typically ``prefix.cell_len``).
        whole: ``(start, end)`` offsets into ``body.plain`` of runs to keep on one line.
        gutter: The column a run too wide for the lane starts at, when it can.

    Returns:
        The rendered lines, newline-free.
    """
    width = max(1, width)
    console = _console(width)
    avail = max(1, width - indent)
    plain = body.plain
    # The runs that need the step out: wider than the lane, no wider than the screen.
    out = [
        (start, end) for start, end in sorted(whole) if avail < cell_len(plain[start:end]) <= width
    ]
    rows: list[tuple[int, Text]] = []  # (left pad, line); the first row's pad is the prefix

    def hang(start: int, end: int) -> None:
        """Wrap ``body[start:end]`` under the indent, trimming the seams beside a run."""
        if out:
            while start < end and plain[start].isspace() and start:
                start += 1
            while end > start and plain[end - 1].isspace():
                end -= 1
        lines = list(body[start:end].wrap(console, avail)) if end > start else []
        if not rows and not lines:
            lines = [Text("")]  # the prefix's line, even when the body opens on a run
        rows.extend((indent, line) for line in lines)

    pos = 0
    for i, (start, end) in enumerate(out):
        if start < pos:
            continue
        hang(pos, start)
        length = cell_len(plain[start:end])
        pad = gutter if length + gutter <= width else width - length
        line = body[start:end]
        # What follows up to the next run joins this line when it fits whole: a delivery
        # mark or an SNR left alone on a line of its own reads as a stray.
        upto = out[i + 1][0] if i + 1 < len(out) else len(plain)
        tail = plain[end:upto].rstrip()
        if tail and "\n" not in tail and pad + length + cell_len(tail) <= width:
            line.append_text(body[end : end + len(tail)])
            end = upto
        rows.append((pad, line))
        pos = end
    if pos < len(plain) or not rows:
        hang(pos, len(plain))

    combined = Text()
    for i, (pad, line) in enumerate(rows):
        if i:
            combined.append("\n")
        combined.append_text(prefix if i == 0 else Text(" " * pad))
        combined.append_text(line)
    return render_lines(combined, width)
