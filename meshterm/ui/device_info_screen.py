# SPDX-License-Identifier: Apache-2.0
"""The Device info page: all that the companion reports, with its pairing PIN concealed.

The page itself belongs to the ``info`` tool. It is a live status panel over the full
configuration table (refer to :mod:`meshterm.tools.info`). This module has the one thing
that the page does *in addition to* scrolling. It hides the BLE pairing PIN behind a row of
mask bullets until the user asks for it.

This is the page that hides the PIN, and not the editor, for a reason. This page is the
dump of the whole device. A user opens it to read the radio out to another person, to take
screenshots for a forum post, or to leave it open while a colleague looks over the
shoulder of the user. The pairing PIN is the one value on it that lets another phone
connect to the radio. The editor, one menu away, still lists the PIN in plain text. A user
goes there to *change* it, one row at a time. The letters of the editor already belong to
find-as-you-type, so no key is left to reveal the PIN.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.console import RenderableType

from .tui.screen import ScrollScreen

#: The key that uncovers a concealed value, and the name that the footer gives it. There
#: is one chord for the concept, on each page that conceals something. This page could use
#: a bare letter, because the body is read-only and no letter is in use. But the Device
#: config editor cannot use one, because its letters are find-as-you-type. If a secret came
#: out with two different keys, depending on the page, the user would have one more thing
#: to learn with no benefit. A chord also must be *intended*: a PIN never appears because a
#: hand touched a letter.
REVEAL_KEY = "^S"


class DeviceInfoScreen(ScrollScreen):
    """The Device info result screen, with a key press that uncovers the pairing PIN.

    All the rest is :class:`~meshterm.ui.tui.screen.ScrollScreen`: the body scrolls, Esc
    closes, and the pager is on F4/F5. The reveal is a *toggle* on one key and one chip.
    When the user presses it again, the bullets come back. A user who uncovered a secret to
    read it out wants to hide it again without leaving the page.

    The screen builds the body again through the ``build`` function of the caller. It does
    not change the body in place. Thus this screen does not need to know what the page is
    made of. MeshTerm keeps both states after it builds them. The two states differ by six
    cells, and nothing reflows. Thus the scroll position stays where the user left it, also
    after a key press.
    """

    def __init__(
        self,
        session,  # noqa: ANN001 - TuiSession, imported lazily to avoid a cycle
        build: Callable[[bool], RenderableType],
        *,
        title: str = "",
        conceals: bool = True,
    ) -> None:
        """Put the page in its screen.

        Args:
            session: The running TUI session (it paints again when the user toggles the
                PIN).
            build: Renders the page body for ``reveal_pin``. MeshTerm calls it one time for
                each state.
            title: The heading for the screen.
            conceals: Whether there is a PIN to conceal at all. ``False`` is for a device
                that never reported one. The page then has nothing to reveal, so neither
                the footer nor the lane offers a key that does nothing.
        """
        super().__init__(build(False), title=title)
        self._session = session
        self._build = build
        self._conceals = conceals
        self._revealed = False
        self._bodies: dict[bool, RenderableType] = {False: self._renderable}

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """Scrolling, then the reveal, then Esc, in the order of the standard.

        The reveal atom names what the key press does *now* (``show`` or ``hide``). It does
        not name what the screen shows. Thus it never repeats the bullets that are already on
        the screen.
        """
        atoms = []
        if self.content_overflows:
            atoms.append("↑↓ PgUp/PgDn scroll")
        if self._conceals:
            atoms.append(f"{REVEAL_KEY} {'hide' if self._revealed else 'show'} PIN")
        atoms.append("Esc close" if self.floating else "Esc back")
        return " · ".join(atoms)

    @property
    def picocalc_lyra_lane(self):
        """The shared pager, and the reveal on F3, which is this screen's own verb slot.

        The chip is the only place where the user of a PicoCalc can learn that the key
        exists. It names the action and not its object: ``Reveal`` while the PIN is masked,
        ``Hide`` when it is visible. Both fit in the six cells of a chip. A device with no
        PIN leaves the slot *empty* and not dim, because the action does not exist on this
        page.
        """
        from .tui.fkeys import FPair, default_lane

        lane = list(default_lane(nav=self.content_overflows))
        if self._conceals:
            lane[2] = FPair("Hide" if self._revealed else "Reveal", "reveal")
        return lane

    def handle(self, action: str, data: str = "") -> None:
        """Toggle the PIN, or scroll or dismiss, as each result screen does.

        The chord and the chip of the lane arrive as the same action. Thus the chip is not a
        second implementation of the key (``^S`` is bound one time, in the chord table of the
        session).
        """
        if action == "reveal":
            if not self._conceals:
                return
            self._revealed = not self._revealed
            body = self._bodies.get(self._revealed)
            if body is None:
                body = self._bodies[self._revealed] = self._build(self._revealed)
            self.replace_content(body)
            self._session.invalidate()
            return
        super().handle(action, data)
