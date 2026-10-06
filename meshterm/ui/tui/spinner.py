# SPDX-License-Identifier: Apache-2.0
"""A reusable text spinner: one animated glyph for a "working" indicator.

A small animation helper that does not depend on a screen. It holds a cycle of glyphs, one
for each animation step, and a current position. A caller moves it forward with
:meth:`tick` (usually from an animation timer). Then the caller reads the current
animation step as a bare glyph, or as a styled Rich :class:`~rich.text.Text`. The busy
splash uses it. But it is independent of all screens on purpose, so that each "working"
indicator in the toolkit can use the same animation.
"""

from __future__ import annotations

from rich.text import Text

from ...platforms import get_platform


def spinner_interval() -> float:
    """The seconds that a "working" animation waits between animation steps, on this platform.

    This function is the only source of the spin rate of the app. Each animated wait sleeps
    for this value, not for its own constant. Thus the rate is one decision of the
    platform, not five copies that become different over time. The function is called at
    each tick, and its value is not read into a module constant. A constant keeps the
    platform that was active at import time, and that is always the default platform
    (``set_platform`` runs later, in the CLI callback: refer to :mod:`meshterm.platforms`).

    Returns:
        The :attr:`~meshterm.platforms.Platform.spinner_tick_s` of the active platform.
    """
    return get_platform().spinner_tick_s


class Spinner:
    """An animated spinner that cycles through a set of glyphs, one for each animation step.

    Attributes:
        style: The Rich style that :meth:`text` applies to the current glyph.
    """

    #: The default animation steps, in Braille dots: a smooth rotation in one cell.
    BRAILLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    #: A plain ASCII cycle for terminals or fonts that have no Braille glyphs.
    LINE = "|/-\\"

    def __init__(self, frames: str | None = None, *, style: str = "accent") -> None:
        """Create a spinner at its first animation step.

        Args:
            frames: The glyphs to cycle through, one for each animation step. The default
                is the cycle of the active platform: :data:`BRAILLE` where effects are on,
                and :data:`LINE` (four animation steps) where effects are off. The default
                is resolved here, at construction. Thus a spinner that is built after
                ``set_platform`` gets the correct cycle, and never decides again while it
                spins.
            style: The Rich style that :meth:`text` applies to the current glyph.
        """
        if frames is None:
            frames = self.BRAILLE if get_platform().effects else self.LINE
        if not frames:
            raise ValueError("a spinner needs at least one frame")
        self._frames = frames
        self.style = style
        self._index = 0

    @property
    def frame(self) -> str:
        """The glyph of the current animation step."""
        return self._frames[self._index]

    @property
    def frames(self) -> str:
        """The glyphs of the full cycle of animation steps, in order."""
        return self._frames

    def tick(self) -> None:
        """Go to the next animation step. After the end of the cycle, go back to the start."""
        self._index = (self._index + 1) % len(self._frames)

    def reset(self) -> None:
        """Put the spinner back at its first animation step."""
        self._index = 0

    def text(self, style: str | None = None) -> Text:
        """Return the current glyph as a styled Rich :class:`~rich.text.Text`.

        Args:
            style: A style to use instead of the spinner's own :attr:`style`.

        Returns:
            The current glyph, styled.
        """
        return Text(self.frame, style=self.style if style is None else style)
