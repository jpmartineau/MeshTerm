# SPDX-License-Identifier: Apache-2.0
"""The busy skeleton: a card with a title that floats between screens while a load runs.

A :class:`BusyOverlay` is the model behind :meth:`~meshterm.ui.tui.session.TuiSession.
busy_overlay`. It is a small *skeleton card* that stands in for a screen whose data
MeshTerm still reads from the device. It has a title, and the one-cell working
:class:`~meshterm.ui.tui.spinner.Spinner` next to a caption. The card is not part of the
screen stack, as a :class:`~meshterm.ui.tui.screen.Screen` is. The session draws it as the
top-most float. Thus it floats over the gap between screens that a slow device operation
opens (Bluetooth most of all). The caller shows the card while the read runs, and removes
it when that read returns.

The card does not appear suddenly. It stays fully black for a short *hold*, then it fades
in from black during the next fraction of a second (refer to :attr:`brightness`). An
operation that finishes during the hold shows nothing. During a longer operation, the card
comes up slowly out of the black. Thus the card never flashes and never appears suddenly.

The chip of the spinner is the whole animation, and it tells all that the card must tell:
the app is working. The card once also had an LED bar with a scanning light
(JP, 2026-08-29): eighteen cells of red that moved across the bar. That bar pulled the eye
more strongly than the words did, on a screen that the user only passes through.
"""

from __future__ import annotations

import time

from rich.cells import cell_len
from rich.text import Text

from .render import render_to_ansi
from .spinner import Spinner

#: The colour of the title line (the theme's ``title.accent``, in hex so that the fade can
#: dim it).
_TITLE_COLOR = "bold #a5b4fc"

#: The colour of the working chip (the theme's ``accent``). The chip has the same one-cell
#: spinner as the other dialogs.
_CHIP_COLOR = "bold #818cf8"

#: The colour of the caption (the theme's ``muted``, in hex so that the fade can dim it).
_CAPTION_COLOR = "#94a3b8"


class BusyOverlay:
    """A skeleton card (a title, the working chip, and a caption): the top-most float.

    Attributes:
        spinner: The animated one-cell :class:`~meshterm.ui.tui.spinner.Spinner` next to
            the caption. It is the same working chip that other places use inline.
        title: An optional heading for the screen whose data is still read (empty for a
            bare card).
        message: An optional caption next to the chip (for example "reading from …").
        started_at: The monotonic time at which the card was created. The hold and the
            fade count from this time.
        hold: The time in seconds for which the card stays fully black before the fade
            starts.
        fade: The duration of the fade-in in seconds, after the hold.
    """

    def __init__(
        self,
        message: str = "",
        *,
        title: str = "",
        hold: float = 0.2,
        fade: float = 0.4,
    ) -> None:
        """Create a busy skeleton card.

        Args:
            message: A short caption next to the working chip (empty for a bare card).
            title: An optional heading that names the screen whose data is still read.
            hold: The time in seconds for which the card stays fully black before it fades
                in. Thus an operation that finishes in this time shows nothing.
            fade: The time in seconds in which the card then becomes brighter, from black
                to full colour.
        """
        self.spinner = Spinner()
        self.title = title
        self.message = message
        self.started_at = time.monotonic()
        self.hold = max(0.0, hold)
        self.fade = max(0.0, fade)

    @property
    def brightness(self) -> float:
        """The current fade level in ``[0, 1]``: 0 during the :attr:`hold`, then a soft rise to 1.

        The level stays exactly 0 for the first :attr:`hold` seconds (nothing paints). Then a
        smoothstep curve over :attr:`fade` gives a gentle glow up out of the black, instead of
        a linear ramp.
        """
        elapsed = time.monotonic() - self.started_at - self.hold
        if elapsed <= 0:
            return 0.0
        if self.fade <= 0:
            return 1.0
        t = min(1.0, elapsed / self.fade)
        return t * t * (3 - 2 * t)  # smoothstep

    def tick(self) -> None:
        """Move the working chip forward one animation step (the only animation of the card)."""
        self.spinner.tick()

    def restart(self) -> None:
        """Start the intro again: set the fade ramp and the chip back to their start.

        This method is called each time that the card becomes visible: at its first
        appearance, and each time that it comes back after a prompt covered it. Thus the black
        hold and the fade-in play again from the start each time that the card shows. If this
        method is not called, the card comes back at full brightness in the middle of an
        operation.
        """
        self.started_at = time.monotonic()
        self.spinner.reset()

    def render(self) -> str:
        """Render the skeleton card as a centred ANSI block, at the current fade level.

        The title line, and the line with the chip and the caption, are padded to the same
        width. Then they are joined into one :class:`~rich.text.Text` of more than one line.
        Thus the float of the session, which has the size of its content, centres a clean
        rectangle over the gap between screens.

        Returns:
            An ANSI string with the rows of the card.
        """
        level = self.brightness
        lines: list[Text] = []
        if self.title:
            lines.append(Text(self.title, style=dim_color(_TITLE_COLOR, level)))
        status = self.spinner.text(dim_color(_CHIP_COLOR, level))
        if self.message:
            status.append("  ")
            status.append(self.message, style=dim_color(_CAPTION_COLOR, level))
        lines.append(status)
        width = max((cell_len(line.plain) for line in lines), default=0)
        block = Text("\n").join(_center_text(line, width) for line in lines)
        return render_to_ansi(block, width)


def _center_text(text: Text, width: int) -> Text:
    """Return ``text`` with spaces added so that it is centred in ``width`` cells."""
    pad = max(0, width - cell_len(text.plain))
    left = pad // 2
    out = Text(" " * left)
    out.append_text(text)
    out.append(" " * (pad - left))
    return out


def dim_color(style: str, factor: float) -> str:
    """Scale a Rich hex-colour style toward black by ``factor`` (0 = black, 1 = unchanged).

    Only the ``#rrggbb`` token is scaled. The attribute words (for example ``bold``) stay as
    they are. A style without a hex colour is returned with no change. The style is also
    returned with no change when ``factor >= 1``.

    Args:
        style: A Rich style such as ``"#38bdf8"`` or ``"bold #ecfeff"``.
        factor: The brightness multiplier, clamped to ``[0, 1]``.

    Returns:
        The style with its colour dimmed toward black.
    """
    if factor >= 1.0:
        return style
    factor = max(0.0, factor)
    parts = []
    for token in style.split():
        if token.startswith("#") and len(token) == 7:
            r = round(int(token[1:3], 16) * factor)
            g = round(int(token[3:5], 16) * factor)
            b = round(int(token[5:7], 16) * factor)
            parts.append(f"#{r:02x}{g:02x}{b:02x}")
        else:
            parts.append(token)
    return " ".join(parts)
