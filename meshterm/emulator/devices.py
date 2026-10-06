# SPDX-License-Identifier: Apache-2.0
"""The handhelds the emulator can stand in for, and what about each it reproduces.

A device here is its *display and keyboard* as MeshTerm meets them — the platform MeshTerm
runs as on it, the panel's size in pixels, how a console draws on that panel, and which
desktop keys stand in for its lane keys — never its processor or its operating system
(see :mod:`meshterm.emulator`). Each is named by its platform's id, so ``meshterm emulate
picocalc-lyra`` and ``--platform picocalc-lyra`` say the same device.
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
    """One handheld's display and keyboard, as the emulator reproduces them.

    Attributes:
        platform: The platform MeshTerm runs as on the device; its grid is the panel's.
        name: The device as people call it, for the window's title.
        panel: The panel's size in pixels, ``(width, height)``.
        console: Whether a Linux console draws the device's screen — the grid from the
            panel's top-left, bold as bright, the palette's slot 7 on slot 0 as the default
            colours, and sixteen colours out — rather than MeshTerm in truecolour.
        lane_keys: The keys under the panel that drive the F-key lane, as ``(label,
            centre)`` pairs in panel pixels, one per slot of the :attr:`deck`, left to
            right — drawn under the window's panel so a chip that drifts off its key
            shows, and pressed there with the mouse. Empty for a device whose lane keys
            sit elsewhere.
        shifted: The desktop's Shift+F-key as the key the device's keyboard sends instead.
            The PicoCalc's keyboard sends F6–F10 for Shift+F1–F5; the Cardputer Zero's sends
            a shifted F4–F8, which needs no translating.
        hold_to_quit: Whether holding Esc quits, as the Cardputer Zero's launcher has it
            (:mod:`~meshterm.services.hold_to_quit`): a tap is Esc on its release, a hold
            raises the quit box. The PicoCalc's console has no such rule, and its Esc is
            typed the moment it goes down.
    """

    platform: Platform
    name: str
    panel: tuple[int, int]
    console: bool = False
    lane_keys: tuple[tuple[str, int], ...] = ()
    shifted: dict[str, str] = field(default_factory=dict)
    hold_to_quit: bool = False

    @property
    def id(self) -> str:
        """The device's id: its platform's name (``picocalc-lyra``)."""
        return self.platform.name

    @property
    def color_depth(self) -> ColorDepth:
        """What prompt_toolkit may send: sixteen colours to a console, truecolour otherwise."""
        return ColorDepth.DEPTH_4_BIT if self.console else ColorDepth.TRUE_COLOR

    @property
    def deck(self) -> LaneDeck:
        """The F-key lane the device's platform deals (:mod:`~meshterm.ui.tui.fkeys`)."""
        from ..ui.tui.fkeys import DECKS

        return DECKS[self.platform.lane_deck]

    def translate(self, key: Key) -> Key:
        """The key as the device's keyboard would send it (see :attr:`shifted`)."""
        if key.shift and key.name in self.shifted:
            return Key(name=self.shifted[key.name], ctrl=key.ctrl, alt=key.alt)
        return key


#: M5Stack's Cardputer Zero: a 320×170 panel MeshTerm draws itself, its lane on Fn+4…8 —
#: the five keys right under the panel, at the positions M5's drawing gives them — and
#: Esc held for three seconds its launcher's way out of any app.
CARDPUTER_ZERO_DEVICE = EmulatedDevice(
    platform=CARDPUTER_ZERO,
    name="Cardputer Zero",
    panel=(320, 170),
    lane_keys=(("4", 48), ("5", 104), ("6", 160), ("7", 216), ("8", 272)),
    hold_to_quit=True,
)

#: ClockworkPi's PicoCalc with a Luckfox Lyra core: a 320×320 panel the Linux console draws
#: in 6×12 cells, its lane on F1–F5 with Shift sending F6–F10.
PICOCALC_LYRA_DEVICE = EmulatedDevice(
    platform=PICOCALC_LYRA,
    name="PicoCalc (Luckfox Lyra)",
    panel=(320, 320),
    console=True,
    shifted={f"f{n}": f"f{n + 5}" for n in range(1, 6)},
)

#: Every device the emulator offers, by id.
DEVICES: dict[str, EmulatedDevice] = {
    device.id: device for device in (CARDPUTER_ZERO_DEVICE, PICOCALC_LYRA_DEVICE)
}


def device(name: str) -> EmulatedDevice:
    """The device called ``name``, or a ``ValueError`` naming the ones there are."""
    try:
        return DEVICES[name.strip().lower()]
    except KeyError:
        raise ValueError(
            f"no emulated device {name!r} - choose from: {', '.join(sorted(DEVICES))}"
        ) from None
