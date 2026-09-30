# SPDX-License-Identifier: Apache-2.0
"""The Device info page: everything the companion reports, its pairing PIN concealed.

The page itself is the ``info`` tool's — a live status panel over the full configuration
table (see :mod:`meshterm.tools.info`). What lives here is the one thing that page does
*beyond* scrolling: it holds the BLE pairing PIN back behind a row of mask bullets until
the reader asks for it.

Why this page and not the editor: this is the whole-device dump, the screen someone opens
to read their radio out to somebody else, screenshots for a forum post, or leaves up while
a colleague looks over their shoulder — and the pairing PIN is the one value on it that
lets another phone onto the radio. The editor one menu over still lists it plainly: you go
there to *change* it, a row at a time, and its letters already belong to find-as-you-type
so there is no key left to reveal with.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.console import RenderableType

from .tui.screen import ScrollScreen

#: The key that uncovers a concealed value, and the name the footer gives it. One chord for
#: the concept on every page that conceals something: this one could have taken a bare
#: letter (a read-only body has none spoken for), but the Device config editor cannot — its
#: letters are find-as-you-type — and a secret that comes out from under two different keys
#: depending on which page you are standing on is a second thing to learn for nothing. A
#: chord also has to be *meant*: no PIN ever appears because a hand brushed a letter.
REVEAL_KEY = "^S"


class DeviceInfoScreen(ScrollScreen):
    """The Device info result window, plus a keypress that uncovers the pairing PIN.

    Everything else is :class:`~meshterm.ui.tui.screen.ScrollScreen`: the body scrolls,
    Esc closes, the pager rides F4/F5. The reveal is a *toggle* on one key and one chip —
    press it again and the bullets come back — because a reader who uncovered a secret to
    read it out wants to put it away without leaving the page.

    The body is rebuilt through the caller's ``build`` rather than patched in place, so
    this screen never has to know what the page is made of. Both states are kept once
    built: the two differ by six cells and nothing reflows, so the scroll position stays
    exactly where the reader left it across a press.
    """

    def __init__(
        self,
        session,  # noqa: ANN001 - TuiSession, imported lazily to avoid a cycle
        build: Callable[[bool], RenderableType],
        *,
        title: str = "",
        conceals: bool = True,
    ) -> None:
        """Wrap the page in its screen.

        Args:
            session: The running TUI session (repainted when the PIN is toggled).
            build: Renders the page body for ``reveal_pin``; called once per state.
            title: Heading for the window.
            conceals: Whether there is a PIN to conceal at all. ``False`` — a device that
                never reported one — leaves the page with nothing to reveal, so neither
                the footer nor the lane offers a key that would do nothing.
        """
        super().__init__(build(False), title=title)
        self._session = session
        self._build = build
        self._conceals = conceals
        self._revealed = False
        self._bodies: dict[bool, RenderableType] = {False: self._renderable}

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """Scrolling, then the reveal, then Esc — the standard's order.

        The reveal atom names what the press does *now* (``show``/``hide``), not what the
        screen is showing, so it is never a restatement of the bullets already on screen.
        """
        atoms = []
        if self.content_overflows:
            atoms.append("↑↓ PgUp/PgDn scroll")
        if self._conceals:
            atoms.append(f"{REVEAL_KEY} {'hide' if self._revealed else 'show'} PIN")
        atoms.append("Esc close" if self.floating else "Esc back")
        return " · ".join(atoms)

    @property
    def picocalc_lane(self):
        """The shared pager, plus the reveal on F3 — this screen's own verb slot.

        The chip is the only place the PicoCalc can learn the key exists, and it names the
        action rather than its object: ``Reveal`` while the PIN is masked, ``Hide`` once it
        is out, both inside the six cells a chip has. A device with no PIN leaves the slot
        *empty* rather than dim — the action isn't a thing on this page at all.
        """
        from .tui.fkeys import FPair, default_lane

        lane = list(default_lane(nav=self.content_overflows))
        if self._conceals:
            lane[2] = FPair("Hide" if self._revealed else "Reveal", "reveal")
        return lane

    def handle(self, action: str, data: str = "") -> None:
        """Toggle the PIN, or scroll/dismiss as any result window does.

        The chord and the lane's chip arrive as the same action, so the chip is not a second
        implementation of the key (``^S`` is bound once, in the session's chord table).
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
