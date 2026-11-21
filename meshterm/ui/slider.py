# SPDX-License-Identifier: Apache-2.0
"""A horizontal slider: a dialog that sets a whole number in a short range, one step at a time.

The Preferences page opens it for the volume of the sounds (JP, 2026-11-20: "a slider
(horizontal) 1 at a time"). ←→ move the value one step, Home and End go to the ends, Enter
sets the value, and Esc keeps the old one. The ends clamp, as each cursor in the app does.

Each step calls ``on_change`` with the new value. The volume uses it to play the channel
message sound at the new volume, so that the user hears the value while choosing it. A
typed number is not necessary: a short range is faster with the arrows, and the bar shows
where the value is in the range.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.console import Group
from rich.text import Text

from .braillechart import meter
from .tui.fkeys import FPair
from .tui.render import render_lines
from .tui.screen import Screen

#: The width of the dialog, in cells. The bar takes what the value label does not.
_WIDTH = 44


class SliderScreen(Screen):
    """The slider dialog. It resolves with the value that Enter sets, or with ``CANCEL``."""

    floating = True
    footer_hint = "←→ change · Home/End ends · Enter set · Esc keep"

    def __init__(
        self,
        title: str,
        prompt: str,
        value: int,
        minimum: int,
        maximum: int,
        on_change: Callable[[int], None] | None = None,
        more: str = "More",
        less: str = "Less",
    ) -> None:
        """Make a slider.

        Args:
            title: The title of the dialog (the label of the preference).
            prompt: The line above the bar (the description of the preference).
            value: The value at the start. The slider clamps it to the range.
            minimum: The lowest value.
            maximum: The highest value.
            on_change: Called with the new value at each step.
            more: The F-key chip of a step up on a handheld (six cells at most).
            less: The F-key chip of a step down.
        """
        super().__init__()
        self.title = title
        self._prompt = prompt
        self._minimum = minimum
        self._maximum = maximum
        self.value = max(minimum, min(maximum, value))
        self._on_change = on_change
        self._more = more
        self._less = less

    @property
    def dialog_width(self) -> int:
        """A small box: the prompt and the bar."""
        return _WIDTH

    @property
    def picocalc_lyra_lane(self):
        """A step up on F1 and a step down on F2: a directional pair rises to its outer key."""
        return (
            FPair(self._more, "right", enabled=self.value < self._maximum),
            FPair(self._less, "left", enabled=self.value > self._minimum),
            None,
            None,
            None,
        )

    def render_body(self, width: int) -> list[str]:
        """The prompt, then the bar with the value at its right end.

        The bar is a whole number of cells for each step, so that each step lights whole
        cells and the bar never ends on a half cell. The value follows the bar.
        """
        label = f" {self.value:>{len(str(self._maximum))}}"
        steps = self._maximum - self._minimum + 1
        room = max(steps, width - len(label) - 1)
        cells = room - room % steps
        fraction = (self.value - self._minimum + 1) / steps
        bar = meter(fraction, cells, style="accent", slim=True, track="track")
        bar.append(label, style="bold")
        return render_lines(Group(Text(self._prompt, style="muted"), Text(""), bar), width)

    def handle(self, action: str, data: str = "") -> None:
        """Move one step, go to an end, set, or keep."""
        if action == "escape":
            super().handle(action, data)
            return
        if action == "enter":
            self.resolve(self.value)
            return
        steps = {"left": -1, "right": 1, "home": -self._maximum, "end": self._maximum}
        if action in steps:
            new = max(self._minimum, min(self._maximum, self.value + steps[action]))
            if new != self.value:
                self.value = new
                if self._on_change is not None:
                    self._on_change(new)
