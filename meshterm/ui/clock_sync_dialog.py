# SPDX-License-Identifier: Apache-2.0
"""The dialog of the bulk clock sync: one line for each managed node, as the run goes.

The bulk ``Sync their clocks…`` of the Clock drift page sets each managed repeater and room
server in turn (refer to :func:`~meshterm.ui.repeater_admin.sync_clocks`). This dialog lists
all of them from the start, so the user sees what the run will do and how far it is (JP,
2026-11-03):

* ``○`` waiting, muted, for a node that the run did not reach yet.
* The spinner, with what the node waits for now: the transmit cooldown (``next in 4s``),
  the login, or the command.
* ``✓`` with the short reply of the node, or a red ``✗`` with the reason.

While the run works, Esc (or Enter) stops it after the node in progress, and each node that
it did not reach says ``not done``. The user is never trapped behind a run of a minute.
When the run is done, the ``OK`` chip closes the dialog.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from rich.cells import cell_len
from rich.text import Text

from ..core.models import Contact
from .theme import name_style
from .tui.render import render_hanging, render_to_ansi
from .tui.screen import Screen
from .tui.spinner import Spinner

#: The states of one line.
WAITING, ACTIVE, OK, FAILED, NOT_DONE = "waiting", "active", "ok", "failed", "not_done"


@dataclass(slots=True)
class _Line:
    """The state of one node in the dialog."""

    node: Contact
    state: str = WAITING
    note: str = ""


class ClockSyncDialog(Screen):
    """A floating dialog with one line for each node of a bulk clock sync.

    The owner changes the lines with :meth:`update`, and calls :meth:`finish` when the run
    ends. The dialog resolves (with ``None``) only when the user closes it after that.
    """

    floating = True
    #: The run owns the keyboard: ^W must not unwind the stack under it.
    modal = True

    def __init__(
        self,
        nodes: Sequence[Contact],
        *,
        spinner: Spinner,
        invalidate: Callable[[], None] = lambda: None,
    ) -> None:
        """Build the dialog with each node waiting.

        Args:
            nodes: The nodes of the run, in the order of the run.
            spinner: The spinner of the line in progress (the owner animates it).
            invalidate: Asks the session for a paint, after each change.
        """
        super().__init__()
        self.title = "Sync clocks"
        self._lines = [_Line(node) for node in nodes]
        self._spinner = spinner
        self._invalidate = invalidate
        #: Called when the user stops the run. The owner connects it to its task.
        self.on_stop: Callable[[], None] = lambda: None
        self.done = False

    @property
    def picocalc_lyra_lane(self):  # noqa: ANN201
        """No lane: Enter and Esc are the only keys, and the hint names them."""
        from .tui.fkeys import EMPTY_LANE

        return EMPTY_LANE

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """``Esc stop`` while the run works, then the acknowledgement of a lone button."""
        return "Enter OK · Esc close" if self.done else "Esc stop"

    @property
    def dialog_width(self) -> int:
        """The natural width: the widest line, the title, and the hint."""
        widths = [cell_len(self.title), cell_len("Enter OK · Esc close")]
        widths += [
            4 + cell_len(line.node.name) + 4 + max(cell_len(line.note), 22) for line in self._lines
        ]
        return max(widths) + 8

    def update(self, index: int, state: str, note: str = "") -> None:
        """Change the state of one line, and paint.

        Args:
            index: The position of the node in the run.
            state: :data:`ACTIVE`, :data:`OK`, or :data:`FAILED`.
            note: What the node waits for, its short reply, or the reason of the failure.
        """
        line = self._lines[index]
        line.state, line.note = state, note
        self._invalidate()

    def finish(self) -> None:
        """End the run: each line that the run did not complete says ``not done``."""
        for line in self._lines:
            if line.state in (WAITING, ACTIVE):
                line.state, line.note = NOT_DONE, "not done"
        self.done = True
        self._invalidate()

    def _line_parts(self, line: _Line) -> tuple[Text, Text]:
        """One node as its mark (the prefix) and its name with its note (the body)."""
        mark = Text()
        if line.state == ACTIVE:
            mark.append_text(self._spinner.text())
        elif line.state == OK:
            mark.append("✓", style="ok")
        elif line.state == FAILED:
            mark.append("✗", style="err")
        else:
            mark.append("○", style="muted")
        mark.append(" ")
        body = Text()
        key = line.node.public_key or line.node.key_prefix
        body.append(line.node.name, style=name_style(line.node.name, key))
        note = line.note or ("waiting" if line.state == WAITING else "")
        if note:
            style = "err" if line.state == FAILED else "muted"
            body.append(f"  ·  {note}", style=style)
        return mark, body

    def render_body(self, width: int) -> list[str]:
        """The lines of the nodes, then the chip of the one button: Stop, then OK."""
        from .tui.prompt import _center

        lines: list[str] = []
        for line in self._lines:
            # A long reason wraps under the name, never at column 0, so that it stays easy
            # to read on the 53 columns of a handheld.
            mark, body = self._line_parts(line)
            lines.extend(render_hanging(mark, body, width, indent=mark.cell_len))
        lines.append("")
        button = Text("  OK  " if self.done else "  Stop  ", style="selected")
        lines.append(render_to_ansi(_center(button, width), width))
        return lines

    def handle(self, action: str, data: str = "") -> None:
        """Enter, Esc, or Space: stop the run while it works, or close the dialog after."""
        if action not in ("enter", "escape", "space"):
            return
        if self.done:
            self.resolve(None)
        else:
            self.on_stop()
