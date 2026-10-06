# SPDX-License-Identifier: Apache-2.0
"""The only bridge from Rich to prompt_toolkit.

The TUI keeps its rendering in Rich (all the existing tables, panels, and text widgets are
used again exactly as they are). It lets prompt_toolkit own the terminal, the input, and
the resize events. This module is the seam between the two. It renders any Rich
renderable to an ANSI string at a chosen width. Then prompt_toolkit draws that string
through :class:`prompt_toolkit.formatted_text.ANSI`.

Each paint renders again at the current width. Thus the content wraps to the new width
automatically when the terminal changes size.
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

#: A cache of one headless render console for each width. Consoles are cheap, but a paint
#: occurs at each key press and at each tick of the live-monitor timer. Thus the cache
#: prevents work that is not necessary. The cache key is the width, because the width of a
#: console is fixed at construction. The cache is cleared each time that the platform
#: changes, because the theme and the colour system are fixed at construction.
_CONSOLES: dict[int, Console] = {}

#: The colour depth that the rasterizer writes at, bound for each platform. On the
#: 16-slot console it is ``"standard"``. The ``color(N)`` styles of the theme go through
#: as exact slot indices. Rich downsamples any stray truecolour (a node hue, a map
#: feature) to the nearest standard slot. The palette keeps each slot aligned with its hue
#: on purpose (refer to ``theme._VT_SLOTS``), so even a stray colour lands in its own
#: colour family.
_COLOR_SYSTEM = "truecolor"


#: A cache of the ANSI for each (content, width), for the renderables that can be
#: described: a ``str`` or a :class:`Text`, whose rendering is a pure function of their
#: plain text, spans, and flags. The TUI composes the full frame again at each key press
#: and at the idle tick. The screens of the list family rasterize row by row through
#: :func:`render_to_ansi`. Between two paints, almost every row is byte-identical. Thus
#: this cache changes the console render of each row (approximately 0.15 ms each) into a
#: dict hit. Tables, panels, and groups have no cheap content key and skip the cache.
#: (Their callers cache at their own level where it is important.)
_ANSI_CACHE: OrderedDict[tuple, str] = OrderedDict()

#: The number of entries that the ANSI cache holds before it evicts the least recently
#: used entries. The size is for a few screens of different rows (a busy list, its
#: filtered variants, a dialog over it), at much less than 2 MB. It is small enough that
#: even the PicoCalc holds it with no problem.
_ANSI_CACHE_MAX = 4096

#: The generation stamp in each cache key. A platform switch changes the theme and the
#: colour system that are fixed in the render consoles. Thus the switch increments the
#: generation, and does not trust eviction. Then a stale entry can never be read, even in
#: the middle of an eviction.
_CACHE_GEN = 0


@on_platform
def _bind(platform: Platform) -> None:
    """Bind the colour depth of the rasterizer again, and remove stale consoles, on a switch."""
    global _COLOR_SYSTEM, _CACHE_GEN
    _COLOR_SYSTEM = "truecolor" if platform.truecolor else "standard"
    _CONSOLES.clear()
    _ANSI_CACHE.clear()
    _CACHE_GEN += 1


def _cache_key(renderable: RenderableType, width: int, no_wrap: bool) -> tuple | None:
    """A content key for a renderable that can be cached, or ``None`` if it has no cheap key.

    A ``str`` is its own content (markup included). A :class:`Text` renders as a pure
    function of its plain text, span list, base style, and wrap flags, and the key holds
    all of these (the span styles through ``str``, which :class:`~rich.style.Style`
    caches). To describe any other renderable (a table, a group, a panel), a deep walk is
    necessary. Thus it reports ``None`` and renders without the cache.
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
    """Return a themed, headless console that writes ANSI at the given width.

    Args:
        width: The target render width, in cells.

    Returns:
        A cached :class:`rich.console.Console` that writes to an internal buffer.
    """
    console = _CONSOLES.get(width)
    if console is None:
        console = Console(
            theme=active_theme(),
            width=width,
            file=StringIO(),
            force_terminal=True,
            color_system=_COLOR_SYSTEM,
            # This console is a rasterizer, not terminal output. prompt_toolkit parses its
            # ANSI again, and the hues of the TUI are semantics (node identity, recency
            # heat), not decoration. Thus a NO_COLOR environment must not remove them
            # here. The real console of the scripted CLI (theme.build_console) is the
            # console that obeys NO_COLOR.
            no_color=False,
            highlight=False,
            soft_wrap=False,
        )
        _CONSOLES[width] = console
    return console


def _whiten_identities(renderable: RenderableType) -> RenderableType:
    """On the highlight, the name of a node is drawn in the highlight's white, not its hue.

    The name of a node is always coloured by its key, so a column of names reads as a
    column of identities. But the row that ``❯`` points to answers a different question,
    and a keyed hue on it competes with the highlight that says "this one". Thus the row
    that declares itself the highlight has its identity spans folded to that same white,
    and the row reads as one thing. (Its base style is ``cursor``. That is how each list,
    and each screen that draws its own rows, marks the highlight.)

    Only :func:`~meshterm.ui.theme.is_identity_style` hues fold. Nothing else on the row
    changes: the heat of the heard age, an SNR reading, a red badge, the chip fills of a
    path line, the grey of a node that no key could place. None of these is the identity
    that the highlight stands in for.

    **A path line keeps its hues**, at any position on the row. There the hue is not
    decoration on a name. It tells one hop from the next, and a route graph drawn above
    the row is cross-referenced by it. The labels of that graph are a marker and one byte
    of hash, and the colour is what says which node. Earlier, a picked route kept the
    colours of its hops in chip form (the fills were never in this vocabulary), but lost
    them in arrow form (JP, 2026-08-30). Arrow form is each path line on the PicoCalc, and
    on each terminal without the powerline glyphs.

    The widget stamps its own extent with :data:`~meshterm.ui.pathline.PATH_INK`. The
    stamp arrives here as a span over the hops (``Text.append_text`` keeps the base style
    of a child as one span). Thus the fold skips the route by its name, and no screen has
    to remember to ask.

    This is done here because here a row is a row: one boundary that the rows of each
    screen already go through, instead of a rule that each screen has to remember. Callers
    give read-only :class:`Text` (the ANSI cache uses their content as its key), so the
    change is done on a copy.

    Args:
        renderable: The renderable that is about to be rendered.

    Returns:
        The renderable, or a copy of a highlight row with its name hues changed to white.
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
    """Whether ``span`` is inside one of ``runs``: a hop inside a marked path line."""
    return any(start <= span.start and span.end <= end for start, end in runs)


def render_to_ansi(renderable: RenderableType, width: int, *, no_wrap: bool = False) -> str:
    """Render a Rich renderable to an ANSI string at ``width`` cells.

    Args:
        renderable: Any Rich renderable (table, panel, text, group, markup string).
        width: The target width in cells. The content wraps or pads to it.
        no_wrap: When ``True``, keep the content on a single line: show what fits, and
            crop the overflow (with an ellipsis). Do not wrap it onto more lines.

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
    # The only render boundary: each visible string in the TUI goes out through here. Thus
    # this one call makes the PicoCalc glyph contract true in all of the app (on the
    # regular platform, the fold changes nothing). The widths were measured on the text
    # before the fold. The fold keeps the cell counts (a wide emoji becomes a glyph and a
    # pad), so the layout above stays correct after it.
    out = fold_text(capture.get())
    if key is not None:
        _ANSI_CACHE[key] = out
        if len(_ANSI_CACHE) > _ANSI_CACHE_MAX:
            _ANSI_CACHE.popitem(last=False)
    return out


def render_lines(renderable: RenderableType, width: int, *, no_wrap: bool = False) -> list[str]:
    """Render a Rich renderable to a list of ANSI lines at ``width`` cells.

    Each line that is returned is an ANSI string with its own styles. Thus the caller can
    slice a vertical range of lines (to scroll), and no escape sequence is split.

    Args:
        renderable: Any Rich renderable.
        width: The target width in cells.
        no_wrap: When ``True``, keep the content to a single cropped line (refer to
            :func:`render_to_ansi`).

    Returns:
        The rendered lines, with no newlines. A trailing empty line (from the last
        newline) is removed, so that the line count matches the visible lines.
    """
    ansi = render_to_ansi(renderable, width, no_wrap=no_wrap)
    lines = ansi.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def query_line(query: str, width: int) -> str:
    """The live find or filter query, shown as one body line: ``/hub``, in the warn style.

    The only way that a find-as-you-type screen shows what the user typed. Each such
    screen draws this same line above the content that the query narrows: the select list
    above its rows, the path composer above its suggestions, the map above its canvas, and
    the walk above its link list. Thus the text that the user types always has the same
    shape, in the same place, in the one colour that nothing else in a body uses.

    The map and the walk also show the query in their footer hint, with the keys that edit
    it next to it. That line is not drawn on a platform whose footer is the F-key lane
    (refer to :attr:`~meshterm.platforms.Platform.footer_fkeys`). That is why those two
    screens call this function: without it, the query is not visible on the handheld
    while the user types it. They call it only when that flag is set, so that the desktop
    does not draw the line two times.

    Args:
        query: The current query (callers call this function only when the query is not
            empty).
        width: The inner content width, in cells.

    Returns:
        One rendered ANSI line.
    """
    return render_to_ansi(query_text(query), width, no_wrap=True)


def query_text(query: str) -> Text:
    """The same echo as a styled run, for a surface that does the layout of the row itself.

    :func:`query_line` is the full line and the usual call. This function is the run in
    that line, for a place where something else shares the row. The map is the only such
    caller. Its credit is pinned to the right end of the row (refer to
    :mod:`meshterm.ui.attribution`). Thus the query must arrive in a form that can be
    cropped, not already rendered to the full width. Both functions use this one
    constructor, so the shape and the ``warn`` colour cannot become different.

    Args:
        query: The current query (callers call this function only when the query is not
            empty).

    Returns:
        The echo, ``warn`` and ``no_wrap``.
    """
    return Text(f"/{query}", style="warn", no_wrap=True)


def right_aligned_tail(body: Text, tail: Text, width: int) -> Text:
    """Lay out ``body`` with ``tail`` pinned to the right edge of its last line.

    ``body`` wraps at ``width`` as usual. ``tail`` is a short status, for example the byte
    counter of the chat compose bar. It is right-aligned on the last wrapped line of
    ``body`` when it still fits there (after at least one blank cell). If not, it goes on
    a new line of its own, also right-aligned. Thus the tail reads as a steady gauge in
    the corner, and does not follow the cursor and wrap with the text.

    Args:
        body: The leading content that can wrap (for example, the input line, with the
            cursor block).
        tail: The short run to pin to the right edge.
        width: The total render width, in cells.

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
    """Cut a single-line styled Text to the cell range ``[start, start + width)``.

    The primitive for horizontal scroll: a row that is wider than its lane shows ``width``
    cells at a time, moved ``start`` cells in, with its styles kept. The boundaries are
    measured in cells. Thus a double-width glyph that crosses either edge is removed (and
    replaced by a pad space on the left), and is not shown as a half.

    Args:
        text: The styled line to crop (treated as a single line).
        start: The cells to skip from the left (clamped at 0).
        width: The cells that the range spans.

    Returns:
        A new :class:`Text` that is at most ``width`` cells wide, with ``no_wrap`` set so
        that the caller can put it directly into a row.
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
                # This character crosses the cut. Keep it when it starts exactly at the
                # boundary. If not, remove it, and pad for the half that sticks out.
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
    """Render ``prefix + body`` at ``width``, and wrap the body with a hanging indent.

    The first visual line has ``prefix``, then the body. Each wrapped continuation line is
    padded by ``indent`` cells, so that it aligns under the body and does not go back to
    column zero. The chat transcript uses this function, so that a wrapped message aligns
    with its own first line and not with the timestamp gutter.

    ``whole`` names runs of the body that must not be cut where the screen can hold them.
    A URL is such a run: a terminal can open it, and a user can copy it, only while it is
    on one line. For a run that fits the hanging lane, nothing is necessary, because the
    wrap already moves a word to the next line and does not split it. A run that is wider than the
    lane, but not wider than the screen, steps out of the block onto a line of its own. It
    starts at ``gutter``, or as far to the right of the gutter as still fits. After it,
    the body continues under its indent, unless the text that follows is short enough to
    finish the line of the run. Only a run wider than the full screen is folded, as
    before, because no line can hold it.

    Args:
        prefix: The leading run (for example, a pointer and a timestamp), shown one time,
            on the first line.
        body: The message text that can wrap. Its styles (mentions, glyphs) are kept.
        width: The total render width, in cells.
        indent: The cells by which to indent continuation lines (usually
            ``prefix.cell_len``).
        whole: ``(start, end)`` offsets into ``body.plain`` of the runs to keep on one
            line.
        gutter: The column at which a run that is too wide for the lane starts, when it
            can.

    Returns:
        The rendered lines, with no newlines.
    """
    width = max(1, width)
    console = _console(width)
    avail = max(1, width - indent)
    plain = body.plain
    # The runs that must step out: wider than the lane, but not wider than the screen.
    out = [
        (start, end) for start, end in sorted(whole) if avail < cell_len(plain[start:end]) <= width
    ]
    rows: list[tuple[int, Text]] = []  # (left pad, line). The pad of the first row is the prefix.

    def hang(start: int, end: int) -> None:
        """Wrap ``body[start:end]`` under the indent, and trim the seams next to a run."""
        if out:
            while start < end and plain[start].isspace() and start:
                start += 1
            while end > start and plain[end - 1].isspace():
                end -= 1
        lines = list(body[start:end].wrap(console, avail)) if end > start else []
        if not rows and not lines:
            lines = [Text("")]  # the line of the prefix, also when the body starts with a run
        rows.extend((indent, line) for line in lines)

    pos = 0
    for i, (start, end) in enumerate(out):
        if start < pos:
            continue
        hang(pos, start)
        length = cell_len(plain[start:end])
        pad = gutter if length + gutter <= width else width - length
        line = body[start:end]
        # The text up to the next run joins this line when all of it fits. A delivery mark
        # or an SNR alone on a line of its own reads as a stray.
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
