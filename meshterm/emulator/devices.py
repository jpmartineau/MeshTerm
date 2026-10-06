# SPDX-License-Identifier: Apache-2.0
"""The handhelds that the emulator can act as, and what the emulator copies of each.

A handheld here is its display and its keyboard, as MeshTerm meets them. That is the
platform that MeshTerm runs as on it, the size of the display in pixels, how a console
draws on that display, and which desktop keyboard keys act as its lane keys. It is never
its processor or its operating system (refer to :mod:`meshterm.emulator`). The id of each
handheld is the id of its platform. Thus ``meshterm emulate picocalc-lyra`` and
``--platform picocalc-lyra`` name the same handheld.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from prompt_toolkit.output.color_depth import ColorDepth

from ..platforms import CARDPUTER_ZERO, PICOCALC_LYRA, Platform
from .keys import Key

if TYPE_CHECKING:
    from ..ui.tui.fkeys import LaneDeck


@dataclass(frozen=True)
class EmulatedDevice:
    """The display and the keyboard of one handheld, as the emulator copies them.

    Attributes:
        platform: The platform that MeshTerm runs as on the handheld. Its grid is the grid
            of the display.
        name: The usual name of the handheld, for the title of the window.
        panel: The size of the display in pixels, ``(width, height)``.
        console: Whether a Linux console draws the screen of the handheld, instead of
            MeshTerm in truecolour. A console puts the grid at the top-left of the
            display, shows bold as bright, uses palette slot 7 on slot 0 as the default
            colours, and sends sixteen colours.
        lane_keys: The keyboard keys under the display that drive the F-key lane, as
            ``(label, centre)`` pairs in display pixels: one for each slot of the
            :attr:`deck`, from left to right. The window draws them under its display, so
            that a chip that moves away from its keyboard key is visible. The user can
            press them there with the mouse. Empty for a handheld whose lane keys are at
            a different place.
        shifted: The Shift+F-key of the desktop, mapped to the keyboard key that the
            keyboard of the handheld sends instead. The PicoCalc's keyboard sends F6–F10
            for Shift+F1–F5. The Cardputer Zero's keyboard sends a shifted F4–F8, for
            which no translation is necessary.
    """

    platform: Platform
    name: str
    panel: tuple[int, int]
    console: bool = False
    lane_keys: tuple[tuple[str, int], ...] = ()
    shifted: dict[str, str] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """The id of the handheld: the name of its platform (``picocalc-lyra``)."""
        return self.platform.name

    @property
    def color_depth(self) -> ColorDepth:
        """What prompt_toolkit can send: sixteen colours to a console, else truecolour."""
        return ColorDepth.DEPTH_4_BIT if self.console else ColorDepth.TRUE_COLOR

    @property
    def deck(self) -> LaneDeck:
        """The F-key lane deck of the platform of the handheld (:mod:`~meshterm.ui.tui.fkeys`)."""
        from ..ui.tui.fkeys import DECKS

        return DECKS[self.platform.lane_deck]

    def translate(self, key: Key) -> Key:
        """The keyboard key as the keyboard of the handheld sends it (refer to :attr:`shifted`)."""
        if key.shift and key.name in self.shifted:
            return Key(name=self.shifted[key.name], ctrl=key.ctrl, alt=key.alt)
        return key


#: M5Stack's Cardputer Zero: a 320×170 display that MeshTerm draws itself. Its lane is on
#: Fn+4…8, the five keyboard keys directly under the display, at the positions that the
#: drawing from M5 gives them.
CARDPUTER_ZERO_DEVICE = EmulatedDevice(
    platform=CARDPUTER_ZERO,
    name="Cardputer Zero",
    panel=(320, 170),
    lane_keys=(("4", 48), ("5", 104), ("6", 160), ("7", 216), ("8", 272)),
)

#: ClockworkPi's PicoCalc with a Luckfox Lyra core: a 320×320 display that the Linux
#: console draws in 6×12 cells. Its lane is on F1–F5, and Shift sends F6–F10.
PICOCALC_LYRA_DEVICE = EmulatedDevice(
    platform=PICOCALC_LYRA,
    name="PicoCalc (Luckfox Lyra)",
    panel=(320, 320),
    console=True,
    shifted={f"f{n}": f"f{n + 5}" for n in range(1, 6)},
)

#: All the handhelds that the emulator offers, by id.
DEVICES: dict[str, EmulatedDevice] = {
    device.id: device for device in (CARDPUTER_ZERO_DEVICE, PICOCALC_LYRA_DEVICE)
}


def device(name: str) -> EmulatedDevice:
    """The handheld called ``name``, or a ``ValueError`` that names the available ones."""
    try:
        return DEVICES[name.strip().lower()]
    except KeyError:
        raise ValueError(
            f"no emulated device {name!r} - choose from: {', '.join(sorted(DEVICES))}"
        ) from None
