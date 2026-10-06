# SPDX-License-Identifier: Apache-2.0
"""Write an already composed frame directly to the terminal, one changed row at a time.

MeshTerm composes each frame itself. :func:`~meshterm.ui.tui.frame.compose_base` returns a
finished ANSI string, with exactly one line for each terminal row. The string is already
cut to the viewport, and already folded to the glyph set of the platform.
The renderer of prompt_toolkit then takes that string and does a full round trip with it:

* it parses the ANSI back into style and text fragments,
* it writes each of the approximately 1400 cells into a new grid of ``Char`` objects,
* it compares that grid with the grid of the previous frame, and
* it writes ANSI again for the cells that changed.

That round trip is the largest cost of a key press on the PicoCalc. MeshTerm pays all of
it on each paint: a measured 70-125 ms, when the frame changed by one row, by all rows, or
not at all. (The header tick every 2 s composes an identical frame again, and still pays
the full cost.) We already know the frame as rows of text. Thus the same pixels are
possible with less work: compare the rows of this frame with the rows of the last frame,
and write again only the rows that are different. That takes approximately 0.1 ms, and the
write is also smaller.

The bypass is narrow on purpose. It runs only when the frame is a plain full-screen
composition: one base screen, with no floating dialog and no busy overlay. The float
containers of prompt_toolkit lay out the dialog and the overlay, not MeshTerm. If we
composite them here, we must implement that placement again. All other frames go
directly to the stock renderer, which stays the authority. :meth:`FastRenderer.render`
makes the common case faster. It is never a second implementation of the whole layout
engine.

Three details make the row diff safe, not only fast:

* **A row is written again whole, from column 0.** When the cursor steps down to a row,
  the terminal goes back to a true column 0, whatever occurred on the row above. Thus a
  glyph that the terminal draws at a width that we did not measure can never move the rows
  below it. :meth:`~meshterm.ui.tui.session.TuiSession._emit` relies on the same
  guarantee. Here the guarantee comes at no cost, without a scrub pass.
* **Each cell of that row is pinned to the grid that the app measured**
  (:func:`~meshterm.ui.tui.colsnap.snap_row`). This pin keeps the drift from having an
  effect in the row too. This is the only place in the app where the bytes of a row are
  still ours on their way to the terminal. Thus it is the only place where that correction
  is possible.
* **Each row is cleared before it is drawn, never after.** Thus no style comes in from the
  row above, a row that became shorter leaves no tail, and a row that fills the terminal
  exactly keeps its last cell.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from prompt_toolkit.renderer import Renderer

from . import colsnap


def _as_is(row: str) -> str:
    """The write path with no pin: a row goes out exactly as it was composed."""
    return row


def enabled() -> bool:
    """Whether the direct row writer is active.

    The writer is on by default. It makes each navigation key press 2-3x faster. Its output
    was checked against the stock renderer with a read back of the console, and the bytes
    are identical. If a terminal does not agree with the writer, the ``fast_render``
    preference turns it off. ``MESHTERM_FASTRENDER=0``/``=1`` overrides that preference
    for one run, without an edit to anything.

    The value is read when the session is built. Thus a change on the Preferences page has
    an effect at the next start of the app. The description of that preference promises
    this behaviour.
    """
    from ...core.preferences import current as current_preferences

    override = os.environ.get("MESHTERM_FASTRENDER")
    if override is not None:
        return override != "0"
    return bool(current_preferences().fast_render)


class FastRenderer(Renderer):
    """A :class:`~prompt_toolkit.renderer.Renderer` that takes a short path for simple frames.

    Args:
        frame_source: A function that returns the composed full-screen frame as one ANSI
            string (rows joined by newlines). It returns ``None`` when this paint is not a
            plain base frame and must go through the layout of prompt_toolkit instead.
    """

    def __init__(self, *args, frame_source: Callable[[], str | None], **kwargs) -> None:
        """Wrap the stock renderer, with no remembered frame at the start.

        There is nothing to compare with, thus the first paint is always a full paint. The
        class docstring above describes the ``frame_source`` argument.
        """
        super().__init__(*args, **kwargs)
        self._frame_source = frame_source
        #: How a row is pinned to the column grid of the app on its way out. It is resolved
        #: one time here, because the switch that turns the pin off is a property of the
        #: terminal of this session, not of one paint. When the pin is off, this is the
        #: identity function, so the write path never branches.
        self._pin: Callable[[str], str] = colsnap.snap_row if colsnap.enabled() else _as_is
        self._prev_rows: list[str] | None = None
        self._prev_size: tuple[int, int] | None = None
        #: The paints that this class does itself, and the paints that it gives to the stock
        #: renderer. The bench reads these counts.
        self.fast_paints = 0
        self.slow_paints = 0

    def reset(self, _scroll: bool = False, leave_alternate_screen: bool = True) -> None:
        """Forget the last frame, and also the render state of prompt_toolkit."""
        self._prev_rows = None
        super().reset(_scroll=_scroll, leave_alternate_screen=leave_alternate_screen)

    def erase(self, leave_alternate_screen: bool = True) -> None:
        """The terminal is cleared under our record of it, so the next paint must be full."""
        self._prev_rows = None
        super().erase(leave_alternate_screen=leave_alternate_screen)

    def render(self, app, layout, is_done: bool = False) -> None:  # noqa: ANN001
        """Paint the frame. Use the row-diff path when this frame is a plain frame."""
        text = None if is_done else self._frame_source()
        if text is None:
            # A dialog, the busy overlay, or the closing paint: prompt_toolkit lays these
            # out, so it must also own the diff. Its grid is the only record of what is on
            # the terminal, and prompt_toolkit will build that grid again from the start in
            # any case.
            self._prev_rows = None
            self.slow_paints += 1
            super().render(app, layout, is_done)
            return

        output = self.output
        pending = False  # whether the terminal setup below queued anything to flush
        if self.full_screen and not self._in_alternate_screen:
            self._in_alternate_screen = True
            output.enter_alternate_screen()
            pending = True
        if not self._bracketed_paste_enabled:
            output.enable_bracketed_paste()
            self._bracketed_paste_enabled = True
            pending = True
        if not self._cursor_key_mode_reset:
            output.reset_cursor_key_mode()
            self._cursor_key_mode_reset = True
            pending = True

        size = output.get_size()
        dims = (size.rows, size.columns)
        rows = text.split("\n")[: size.rows]

        # A resize makes both records of the terminal wrong: ours and that of prompt_toolkit.
        full = self._prev_rows is None or self._prev_size != dims
        if not full and not pending and rows == self._prev_rows:
            # The app paints on a timer to keep the pulse of the header in motion, and most
            # of those frames are identical. To write nothing is not only cheaper than to
            # write the rows again. It also keeps the damage region of the display empty.
            # Thus the display never flushes, and the SPI bus stays quiet.
            self._last_size = size
            self.fast_paints += 1
            return
        if full:
            self._last_screen = None
            output.hide_cursor()
            output.reset_attributes()
            output.erase_screen()
            output.cursor_goto(0, 0)

        write = output.write_raw
        prev = self._prev_rows
        last = -2
        for i, row in enumerate(rows):
            if not full and i < len(prev) and prev[i] == row:
                continue
            # A step down one row with CRLF sets the terminal back to a true column 0,
            # whatever occurred on the row above. It also costs three bytes less than a
            # cursor address. Only a real jump uses a cursor address.
            write("\r\n" if i == last + 1 else f"\x1b[{i + 1};1H")
            # Erase before the row is drawn, never after. A row that fills the terminal
            # exactly leaves the cursor in the last cell, with a wrap pending. An erase to
            # the end there removes the character that was just written. (The last cell of
            # the row went missing on the console of the PicoCalc, which is 53 cells wide.)
            # An erase first also removes the tail of a row that became shorter, which is
            # the purpose of the erase.
            write("\x1b[0m\x1b[K")
            # Pinned, not only written: the terminal draws an emoji in the name of a node at
            # the width that this font gives it. But the lanes after the emoji stay on the
            # grid of the app in all cases.
            write(self._pin(row))
            last = i
        # Move the cursor out of the text. The app hides the cursor, but a terminal can
        # ignore that. Such a terminal must not leave the cursor to blink in the middle of
        # a row.
        write(f"\x1b[{len(rows)};1H")
        output.flush()

        self._prev_rows = rows
        self._prev_size = dims
        self._last_size = size
        # The grid of prompt_toolkit no longer describes the terminal. If a later paint
        # goes back to that grid, it must draw everything again, instead of a diff against
        # an old record.
        self._last_screen = None
        self.fast_paints += 1
