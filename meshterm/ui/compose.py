# SPDX-License-Identifier: Apache-2.0
"""The compose line of a chat: a line editor that shows each mention as a chip.

The reply flow puts an ``@[Name]`` token in the compose line at the cursor (refer to
:meth:`~meshterm.ui.chat.ChatScreen._begin_reply`), and the user can also type one. When
the ``]`` completes a token, the line shows it as the chip of the node that it names: the
same chip as in the transcript (refer to :func:`~meshterm.ui.widgets.mention_chip`). After
that, the chip is one atom. The cursor moves over it in one step, and Backspace or Delete
removes all of the mention. The text of the message does not change. The line sends the
tokens as they are, and the byte gauge counts them.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace

from rich.text import Text

from .pathline import PathHop
from .tui.prompt import LineEditor
from .widgets import DESTINATION, hop_chip

#: A complete mention in the compose line. It is :data:`~meshterm.core.channels.MENTION`
#: with one more rule: the name has no ``[``. Thus an ``@[`` that the user types in front
#: of a chip does not join the chip into one long token, and the chip stays as it is.
ATOM = re.compile(r"@\[([^\[\]]{1,20})\]")

#: The actions that move the cursor to the left. A cursor that one of them puts inside a
#: chip goes to the start of the chip. Each other action puts it at the end.
_BACKWARD = frozenset({"left", "ctrl_left"})


class MentionEditor(LineEditor):
    """The line editor of a chat. Each complete ``@[Name]`` token in it is one atom.

    The cursor is never inside an atom. ← and → go over an atom in one step, and a word
    jump (^← and ^→) that stops inside one goes to its start or to its end. Backspace at
    the end of an atom, and Delete at its start, remove all of it. A typed ``]`` that
    completes a token makes it an atom immediately, with the cursor after it.
    """

    def atoms(self) -> list[tuple[int, int, str]]:
        """The atoms of the line, from left to right, as ``(start, end, name)``."""
        return [(m.start(), m.end(), m.group(1)) for m in ATOM.finditer(self.text)]

    def insert(self, run: str) -> None:
        """Insert ``run`` at the cursor, as typed text, and put the cursor after it."""
        self.edit("text", run)

    def edit(self, action: str, data: str = "") -> bool:
        """Apply an edit action, with each atom as one character. Return ``True`` if it acted.

        Args:
            action: The normalized key action.
            data: The typed character, or the pasted run.

        Returns:
            ``True`` if the action was an edit action that this method handled.
        """
        atoms = self.atoms()
        if action == "backspace":
            for start, end, _name in atoms:
                if end == self.cursor:
                    return self._remove(start, end)
        elif action == "delete":
            for start, end, _name in atoms:
                if start == self.cursor:
                    return self._remove(start, end)
        elif action == "left":
            for start, end, _name in atoms:
                if end == self.cursor:
                    self.cursor = start
                    return True
        elif action == "right":
            for start, end, _name in atoms:
                if start == self.cursor:
                    self.cursor = end
                    return True
        if not super().edit(action, data):
            return False
        # The edit can complete a token around the cursor (a typed ``]``, or an ``@`` in
        # front of ``[Name]``), and a word jump can stop at a space inside a name. Thus the
        # cursor goes out of the atom, in the direction of the action.
        for start, end, _name in self.atoms():
            if start < self.cursor < end:
                self.cursor = start if action in _BACKWARD else end
        return True

    def _remove(self, start: int, end: int) -> bool:
        """Remove the atom at ``[start, end)`` and put the cursor where it was."""
        self.text = self.text[:start] + self.text[end:]
        self.cursor = start
        return True

    def render_line(
        self, hop_of: Callable[[str], PathHop], *, overflow_at: int | None = None
    ) -> Text:
        """Render the line, with each atom as the chip of the node that it names.

        The text has the ``compose`` white, and the ``›`` has the ``cursor`` white. When the
        cursor is at the start of an atom, the chip is the white chip of an insertion slot
        (refer to :attr:`~meshterm.ui.pathline.PathHop.cursor`). It is the cursor block of
        the line, as wide as the atom: Delete removes it all. A chip that goes past the byte
        limit is red, the same as each character past the limit.

        Args:
            hop_of: Makes the hop of a mention from its name (refer to
                :func:`~meshterm.ui.widgets.mention_hop`).
            overflow_at: The index of the first character past the byte limit, or ``None``.

        Returns:
            The line as one :class:`Text`. Each chip in it has the
            :data:`~meshterm.ui.pathline.PATH_INK` span, so that a wrap can keep it whole.
        """
        # The style goes on the ``›`` alone. The base style of a Text applies to all of the
        # line, and the typed text took the colour of the ``›`` when it was there.
        text = Text()
        text.append("› ", style="cursor")
        pos = 0
        for start, end, name in self.atoms():
            self._append_run(text, self.text, pos, start, overflow_at, "compose")
            hop = hop_of(name)
            if self.cursor == start:
                hop = replace(hop, cursor=True, white_ink=False)
            elif overflow_at is not None and end > overflow_at:
                hop = replace(hop, style="err", white_ink=False)
            text.append_text(hop_chip(hop, role=DESTINATION))
            pos = end
        self._append_run(text, self.text, pos, len(self.text), overflow_at, "compose")
        if self.cursor >= len(self.text):  # the cursor after the last character
            text.append(" ", style="reverse")
        return text
