# SPDX-License-Identifier: Apache-2.0
"""Pin the columns of a written row to the grid of the terminal, so that no glyph can shift it.

:mod:`~meshterm.ui.tui.emoji_width` answers the question "how many cells does this glyph
use?". This module answers the question that decides if a row is aligned: **in which column
does the next glyph start?**

A row goes to the terminal as one sequence of characters. The terminal moves its own cursor
forward by the width that it thinks each glyph has. If that width is different from our width
by one cell, all the text after that glyph is drawn one column out of position. The lanes to
its right shift, the values under a heading are no longer under it, and the right border of
the panel moves in, one cell short of the edge of the frame. Thus one emoji in the name of one
node makes its row the only row in the list that is not aligned.

**A measurement cannot remove that difference**, and it is important to say this clearly. The
font decides if it draws a given emoji in one cell or in two cells. The code point does not
decide it. No table is correct on all terminals. Also, no escape sequence tells the program
the width that the renderer used. On a split terminal, the component that tracks the cursor
and the component that draws the glyph are different programs, and they do not agree with
each other (a cursor probe reads the PTY, not the component that draws). The app once kept a
list of exceptions, and added to it one confirmed glyph at a time. That method aligned the
rows that a person had already examined. But each name that arrives over the radio stayed as
broken as before, because a stranger wrote it, with any characters that they wanted to type.
For this reason, a contact list is the worst case in the app: its content is the only thing
on the screen that nobody can put on a list before it arrives.

Thus this module does not ask the terminal to agree. After each glyph whose drawn width is not
certain, the row has an **absolute column address**. This address is ``CSI n G``, and it names
the one-based column that the app measured for that glyph. Thus our answer to the question
"where am I?" replaces the answer of the terminal before the next glyph is drawn. The cursor
gets a new pin to the grid of the app at each point where a difference could start. Thus a
glyph with an incorrect measurement can move only itself:

* If the glyph is drawn **narrower** than the cells that we reserved, it leaves a blank cell
  after it. Before the glyph is drawn, its reserved cells are erased in its own colours
  (``CSI n X``). Thus that cell is blank, with the correct background. It never shows a
  character from the last frame, and it never makes a hole in the fill of a chip.
* If the glyph is drawn **wider**, the content of the next column is written over the part
  of the glyph that extends past its cells.

In both cases, the damage is one cell, only cosmetic, and in the lane of the glyph. A shift
damaged a full row. The second case does not occur in practice:
:mod:`~meshterm.ui.tui.emoji_width` reserves two cells for each glyph that a terminal may draw
as an emoji, and two cells is the maximum for one glyph. The pin keeps the lanes after a glyph
in position, at any width that the terminal gives to the glyph. The reservation also keeps the
text next to the glyph aligned from row to row, because a pin can only go to the column where
the glyph was measured.

**What is certain.** A pin after a glyph that did not need it does no damage, because it
addresses the column that the cursor is already in. But it has a cost: five bytes on the
wire, and a walk through the clusters in Python. MeshTerm draws some non-ASCII glyphs in
thousands: a map is a canvas of braille, each frame has box drawing, and a path ribbon is half
blocks and chevrons. Thus :data:`_TEXT_GLYPHS` lists the ranges that this app draws in large
quantities and for which the two authorities already agree on a one-cell text width.
:func:`snap_row` pins after all other glyphs. A row that has nothing outside that list is
returned unchanged, and that is almost each row of almost each screen. Thus the cost of the
walk occurs only where an emoji is.

If an entry in that list is wrong, the cost is the same cost that the app had before: the row
of that one glyph. Thus the list is a claim about performance, and correct alignment never
depends on it. This is the purpose of the inversion. The exception sets that this module
replaced had to be complete to keep the app aligned. This list must only be cheap.

**Two writers, two forms of the same pin.** A frame goes to the terminal in one of two ways,
and each way knows a different thing about the position of its cursor:

* :mod:`~meshterm.ui.tui.fastrender` writes a plain full-screen frame itself. It writes each
  changed row as one sequence from column 0. It knows the absolute position of each column,
  thus :func:`snap_row` writes the pin as an absolute address, ``CSI n G``.
* All that prompt_toolkit lays out (a floating dialog, and the backdrop that it draws again)
  goes out through its differential renderer. That renderer never knows the absolute
  position of a column. It moves a cursor of its own with relative moves, and it adds the
  measured width of each character when it writes the character. Thus :class:`PinnedOutput`
  writes the pin relative to the glyph itself: save the cursor, draw the glyph, restore the
  cursor, and then move forward by exactly the width that the renderer will add. After each
  glyph, the cursor of the terminal then goes to the position that the arithmetic of the
  renderer gives. Thus a relative move that comes from that arithmetic is a correct move.
  Nothing is tracked, thus nothing can go out of step with the count that the renderer
  keeps.

A platform that never draws a glyph outside its own font has nothing to pin, and it pins
nothing. The PicoCalc folds each emoji away before the emoji gets to the console. Each glyph
that the PicoCalc draws is a glyph that :mod:`~meshterm.ui.fontset` checked on the handheld.

To turn the pins off, set ``MESHTERM_COLUMN_SNAP=0``. This value is about the geometry of the
terminal, and MeshTerm reads it one time, when it builds the session. It is not a choice about
the behaviour of the app.
"""

from __future__ import annotations

import os
import re
from collections import OrderedDict
from typing import Any

from prompt_toolkit.utils import get_cwidth
from rich.cells import cell_len

from ...platforms import get_platform
from .emoji_width import clusters

#: The glyph ranges that MeshTerm draws in large quantities, and for which the two width
#: authorities already agree on a width of one cell. Thus a row that has only these glyphs
#: gets no pin. Each entry is an inclusive ``(first, last)`` pair. A single glyph gives its
#: own value two times.
#:
#: These are text glyphs: they are outside Emoji_Presentation, and they have no variation
#: selector. The most difficult lesson of the emoji-width module is about this class.
#: ``U+1F6E3`` and ``U+1F578`` measure one cell for the two authorities, because the font
#: draws them as one-cell text. When the app forced one of them to two cells, the border of
#: its row moved in. All the other non-ASCII glyphs are outside this list and get a pin: each
#: pictograph, each flag, each joined sequence, each fullwidth character, and each glyph that
#: nobody has examined.
_TEXT_GLYPHS: tuple[tuple[str, str], ...] = (
    ("\x00", "\x7f"),  # ASCII: each letter, digit, and escape byte in a composed row
    (" ", "ſ"),  # Latin-1 + Latin Extended-A: an accented name, °, ·, ±
    ("‐", "‧"),  # General Punctuation: `– — ‘ ’ “ ” • …`
    ("←", "↓"),  # ← ↑ → ↓: the arrow atoms at the start of each footer hint
    ("─", "╿"),  # Box Drawing: each panel border and each rule
    ("▀", "▟"),  # Block Elements: the half blocks of the path line, and the splash art
    ("■", "■"),  # ■ room server
    ("▲", "▲"),  # ▲ repeater
    ("▸", "▸"),  # ▸ the marker of a row
    ("◉", "◉"),  # ◉ sensor
    ("○", "●"),  # from ○ unknown to ● node, and the rings between them
    ("★", "★"),  # ★ you
    ("✓", "✓"),  # ✓ ok
    ("✗", "✗"),  # ✗ err
    ("❯", "❯"),  # ❯ the glyph that points to the highlight
    ("⠀", "⣿"),  # Braille: each chart, and all of the map canvas
    ("", ""),  # the powerline chevrons that make the seams of a path ribbon
)

#: Matches the first glyph in a row that is not in :data:`_TEXT_GLYPHS`. This test decides if a
#: walk through the row is necessary. It is a negated class of the ranges above. Thus the test
#: is one scan of the row at C speed, instead of a Python loop over its cells.
_UNPINNED = re.compile("[^" + "".join(f"{low}-{high}" for low, high in _TEXT_GLYPHS) + "]")

#: One escape sequence: a CSI, an OSC string, or a bare two-character escape. A composed row
#: has only CSI sequences (the SGR colour runs of the theme). The pattern matches an escape
#: sequence so that the walk can copy it without a move to the next column. A style change
#: uses no cell. If the walk counts it, each glyph after it gets a pin to the wrong column.
_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")

#: The pinned rows, with the composed row as the ``dict`` key. When a list scrolls, the same
#: few rows are written again many times: the row of the highlight, and the row that the
#: highlight left. Thus the walk occurs one time for each different row, instead of one time
#: for each paint.
_CACHE: OrderedDict[str, str] = OrderedDict()

#: The number of rows that the cache holds. After that number, the cache removes the least
#: recently used row.
_CACHE_MAX = 1024

#: DECSC and DECRC: save the cursor, and go back to it. :class:`PinnedOutput` puts these two
#: sequences before and after a glyph. Thus the step after the glyph is measured from the
#: start of the glyph, instead of from the position where the width table of the terminal put
#: the cursor. The two sequences also save and restore the current colour. This does no damage
#: here, because nothing changes the colour between the two sequences.
_SAVE = "\x1b7"
_RESTORE = "\x1b8"


def _erase(width: int) -> str:
    """ECH: blank ``width`` cells at the cursor, in the current colours, with no cursor move.

    MeshTerm writes this sequence before a glyph that has more than one reserved cell. Thus
    this frame draws each reserved cell, whatever the font does with the glyph. Each cell gets
    the background that a chip or a highlighted row gave it, instead of the last content of
    the terminal at that position.
    """
    return f"\x1b[{width}X"


def enabled() -> bool:
    """Whether the app pins the written glyphs to the columns that it measured for them.

    The pins are on wherever the platform draws emoji. Set ``MESHTERM_COLUMN_SNAP=0`` to turn
    them off for one run. A terminal that handles the pins incorrectly must have this way out.
    This value also shows how the alignment looks without the pins. The pins are off on a
    platform that draws no emoji, because there each glyph is in the platform's own font, and
    that font is already checked. The value is read when MeshTerm builds the session, the same
    as the values of its neighbours.
    """
    return os.environ.get("MESHTERM_COLUMN_SNAP") != "0" and get_platform().emoji


def _pinned(cluster: str) -> bool:
    """Whether ``cluster`` must have an absolute column address after it.

    True for each glyph when the app is not sure that the terminal draws it at the measured
    width:

    - each glyph with more than one code point: a flag, a joined sequence, or an emoji with a
      variation selector or a skin tone. :func:`~meshterm.ui.tui.emoji_width.clusters`
      collects these forms.
    - each lone code point outside :data:`_TEXT_GLYPHS`.
    """
    return len(cluster) > 1 or bool(_UNPINNED.match(cluster))


def snap_row(row: str) -> str:
    """``row`` with an absolute column address after each glyph that can have a wrong width.

    The function assumes that the row starts in **column 0**.
    :mod:`~meshterm.ui.tui.fastrender` makes sure of this: it addresses the row, then writes
    all of it.

    A row that has only :data:`_TEXT_GLYPHS` is returned as it is, with no change: one scan,
    no walk, and no allocation. The reason is that almost each row that the app draws is such
    a row.

    Args:
        row: One composed terminal row. It is text with the escape sequences of the theme in
            it, and it is already cut to the width of the terminal.

    Returns:
        The row, with ``CSI n G`` at each position where the cursor must get a new pin.
    """
    if _UNPINNED.search(row) is None:
        return row
    hit = _CACHE.get(row)
    if hit is not None:
        _CACHE.move_to_end(row)
        return hit
    out = _snapped(row)
    _CACHE[row] = out
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return out


def _snapped(row: str) -> str:
    """Walk ``row`` glyph by glyph, track the column, and pin where :func:`_pinned` says.

    The address is written **lazily**: the walk holds it until a glyph that it can draw
    follows it. Thus a row that ends with an emoji (often a chat line) has no cost for an
    address that nothing uses. Also, a style change between two glyphs stays where the
    theme put it.
    """
    parts: list[str] = []
    column = 0
    owed = -1  # the column that a held address names, or -1 when no address is owed
    position = 0
    for escape in _ESCAPE.finditer(row):
        column, owed = _pin_text(row[position : escape.start()], parts, column, owed)
        parts.append(escape.group())
        position = escape.end()
    column, owed = _pin_text(row[position:], parts, column, owed)
    return "".join(parts)


def _pin_text(text: str, parts: list[str], column: int, owed: int) -> tuple[int, int]:
    """Append one part of ``text`` that has no escape sequence to ``parts``, with pins.

    The pins are added glyph by glyph.

    Args:
        text: The part to append. It has no escape sequence in it.
        parts: The row that the walk builds. This function appends to it in place.
        column: The column in which this part starts.
        owed: The column that a held address names, or ``-1`` for none.

    Returns:
        The column in which the part ends, and the address that it still owes at its end.
    """
    for cluster in clusters(text):
        if owed >= 0:
            parts.append(f"\x1b[{owed + 1}G")
            owed = -1
        width = cell_len(cluster)
        pinned = _pinned(cluster)
        if pinned and width > 1:
            parts.append(_erase(width))
        parts.append(cluster)
        column += width
        if pinned:
            owed = column
    return column, owed


def _lone_pinned_glyph(data: str) -> bool:
    """Whether ``data`` is exactly one glyph, and a glyph that :func:`_pinned` says to pin.

    The renderer of prompt_toolkit writes the content of one screen cell at a time: a
    character, or a full sequence that it got as one item (refer to
    :class:`~meshterm.ui.tui.emoji_width.ClusterTextControl`). Thus that shape is the only
    shape that gets a pin. Longer data is not a cell, and it goes through with no change.
    """
    if len(data) == 1:
        return bool(_UNPINNED.match(data))
    for cluster in clusters(data):
        return cluster == data
    return False


class PinnedOutput:
    """A prompt_toolkit ``Output`` proxy that pins each uncertain glyph that it must write.

    The renderer of prompt_toolkit writes a screen cell with ``write``. Then it adds the
    measured width of that cell to a cursor that it keeps for itself. Each later move on the
    row is a relative move from that cursor. If the terminal draws the glyph at a different
    width, the two cursors separate, and each move after that glyph goes one column out of
    position. This proxy closes the gap at the moment when the gap opens. It writes each glyph
    that :func:`_pinned` cannot guarantee between a save and a restore of the cursor. Then it
    adds a forward step of exactly the width that the renderer will add. Thus the cursor of
    the terminal is in step again before the next move of the renderer.

    All the other calls go to the wrapped output with no change: ``write_raw``, the cursor
    moves, the size, and the attributes. ``write`` also goes through with no change when its
    data is not a lone uncertain glyph, and that is almost each call that it gets.
    """

    def __init__(self, inner: Any) -> None:
        """Wrap ``inner``, the concrete prompt_toolkit output for the real terminal."""
        self._inner = inner

    def write(self, data: str) -> None:
        """Write ``data``. If it is a glyph that must get a pin, pin the cursor after it."""
        inner = self._inner
        if data.isascii() or not _lone_pinned_glyph(data):
            inner.write(data)
            return
        width = get_cwidth(data)
        # The renderer thinks that the cells after a wide glyph are part of the glyph, and it
        # never writes them. Thus this code erases them first. Then a glyph that is drawn
        # narrower does not leave the character of the last frame in the cell that it did not
        # cover.
        inner.write_raw(_SAVE + _erase(width) if width > 1 else _SAVE)
        inner.write(data)
        # The terminal reads a forward step of zero as a step of one. Thus, after a glyph that
        # has a width of zero (a stranded joiner), the cursor only goes back to its start.
        inner.write_raw(f"{_RESTORE}\x1b[{width}C" if width else _RESTORE)

    def __getattr__(self, name: str) -> Any:
        """Forward each other attribute and method directly to the wrapped output."""
        return getattr(self._inner, name)


__all__ = ["PinnedOutput", "enabled", "snap_row"]
