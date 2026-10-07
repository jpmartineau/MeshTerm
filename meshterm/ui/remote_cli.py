# SPDX-License-Identifier: Apache-2.0
"""The remote command line: talk to the CLI of a repeater over the mesh, like readline.

This is a full-screen terminal onto a remote node that the user logged in to (it is part
of the repeater-admin feature). The user types a command, watches it go, and reads the
reply in a transcript that grows. Each command is one mesh transmission. Thus the screen
sends exactly what you commit, one command at a time. While a command is in flight, the
prompt is parked until the reply arrives or the wait times out. Repeaters answer tersely,
and sometimes they do not answer.

The input line behaves like a shell:

* **↑/↓** recall the command history of the node. It is stored for each node (refer to
  :class:`~meshterm.core.remote_store.RemoteStore`), so a command from last week is one
  key press away in the next session.
* **Tab** completes the current text against the known repeater commands. These are the
  fixed verbs, each ``get`` and ``set`` spelling in the settings catalog, and each key that
  *this node* taught us here on an earlier visit. The best match shows as muted ghost text
  after the cursor.
* **Enter** sends. The transcript keeps each exchange of the session, with the newest at
  the bottom, and the viewport follows the prompt as the transcript grows.

The screen renders the state and routes the keys. The owning flow gives the screen
``send``, which owns the radio, the history store, and the reply plumbing. The flow gives
replies back through :meth:`reply` and :meth:`failed`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from rich.console import Group
from rich.text import Text

from ..core.remote_config import known_commands
from .tui.prompt import LineEditor
from .tui.render import render_lines
from .tui.screen import Screen
from .tui.spinner import Spinner

#: The depth of the history of the transcript, in exchanges. The body scrolls, and this
#: limit caps the memory.
_LOG_CAP = 400


class RemoteCliScreen(Screen):
    """A screen like a shell, onto the CLI of one remote node. The owner does the radio work."""

    floating = False
    #: The line that stays visible is the prompt, not a highlight. ↑↓ recall the history as
    #: they do in a shell, and the transcript scrolls only with the page keys. Thus this
    #: screen has no edge scroll.
    edge_scrolls = False

    @property
    def picocalc_lyra_lane(self):
        """The pager over the transcript, with no jumps behind it.

        Home and End never reach the transcript here. They go to the editor of the compose
        line, where they move the text cursor to the start or the end of the typed text
        (refer to :meth:`handle`). This is the normal behaviour of a command line, and both
        keys are on the keyboard. Thus the Shift companions stay blank. They do not promise
        a jump that the transcript does not make.
        """
        from .tui.fkeys import FPair, default_lane

        overflows = self.content_overflows
        lane = list(default_lane(nav=overflows))
        lane[3] = FPair("Page ↓", "pagedown", enabled=overflows)
        lane[4] = FPair("Page ↑", "pageup", enabled=overflows)
        return lane

    def __init__(
        self,
        *,
        node_label: str,
        history: list[str],
        send: Callable[[str], None],
        session: object,
        extra_keys: Iterable[str] = (),
    ) -> None:
        """Create the command line over the stored history of a node.

        Args:
            node_label: The display name of the remote node (for the title and the prompt).
            history: The earlier commands of the node, oldest first (↑ recalls them).
            send: Commits one command. The owner guards it, and it does nothing while a
                command is in flight.
            session: The running TUI session (for paints).
            extra_keys: The settings that *this node* has and the catalog does not have,
                found here on an earlier visit. They complete like each other key. The
                prompt is where MeshTerm learned them, so it is where they must be one Tab
                away.
        """
        super().__init__()
        self.title = f"Command line — {node_label}"
        self._node_label = node_label
        self._send = send
        self._session = session
        self._editor = LineEditor()
        self._log: list[Text] = []
        self._history = list(history)
        self._recall: int | None = None  # the index in the history while ↑/↓ browse it
        self._draft = ""  # the text typed before the recall started. ↓ past the end restores it
        self._completions = sorted(
            {
                *known_commands(),
                *(f"get {key}" for key in extra_keys),
                *(f"set {key} " for key in extra_keys),
            }
        )
        self.busy = False
        self._spinner = Spinner()
        self._prompt_line = 0  # the body index of the input line (pinned so that it is visible)

    # --- owner feed ------------------------------------------------------------------

    def sent(self, command: str) -> None:
        """Echo a command that MeshTerm just sent into the transcript, and park the prompt."""
        row = Text("❯ ", style="brand")
        row.append(command)
        self._append(row)
        self._history.append(command)
        self._recall = None
        self.busy = True
        self._spinner.reset()
        self._session.invalidate()

    def reply(self, text: str) -> None:
        """Add the reply of the node (it can have more than one line) and free the prompt."""
        for line in text.splitlines() or [""]:
            self._append(Text(f"  {line}"))
        self.busy = False
        self._session.invalidate()

    def failed(self, note: str, *, error: bool = False) -> None:
        """Add a note about a timeout or an error, and free the prompt."""
        self._append(Text(f"  {note}", style="err" if error else "muted"))
        self.busy = False
        self._session.invalidate()

    def tick(self) -> None:
        """Advance the spinner while a command is in flight.

        The animation timer of the owner calls this.
        """
        self._spinner.tick()

    def _append(self, row: Text) -> None:
        row.no_wrap = False
        self._log.append(row)
        del self._log[:-_LOG_CAP]

    # --- input -----------------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The footer keys. They change when a command is in flight."""
        if self.busy:
            return "waiting for the reply… · PgUp/PgDn scroll · Esc back"
        return "↑↓ history · PgUp/PgDn scroll · Enter send · Tab complete · Esc back"

    def handle(self, action: str, data: str = "") -> None:
        """Edit the buffer, recall history, complete, send, scroll, or dismiss."""
        if action == "enter":
            command = self._editor.text.strip()
            if command and not self.busy:
                self._editor = LineEditor()
                self._send(command)
        elif action == "up":
            self._recall_step(-1)
        elif action == "down":
            self._recall_step(1)
        elif action == "tab":
            completion = self._completion()
            if completion is not None:
                self._editor = LineEditor(completion)
        elif action == "pageup":
            self.scroll_pages(-1)
        elif action in ("pagedown",):
            self.scroll_pages(1)
        elif action == "escape":
            self.resolve(None)
        elif self._editor.edit(action, data):
            self._recall = None  # typing ends the history walk. The buffer is a draft.
        self._session.invalidate()

    def _recall_step(self, direction: int) -> None:
        """Walk the history with ↑/↓, and keep the draft that was not sent safe at the new end."""
        if not self._history:
            return
        if self._recall is None:
            if direction > 0:
                return  # nothing is newer than the draft
            self._draft = self._editor.text
            self._recall = len(self._history) - 1
        else:
            self._recall += direction
        if self._recall >= len(self._history):
            self._recall = None
            self._editor = LineEditor(self._draft)
            return
        self._recall = max(0, self._recall)
        self._editor = LineEditor(self._history[self._recall])

    def _completion(self) -> str | None:
        """The first known command that extends the current text, or ``None``."""
        prefix = self._editor.text.lstrip()
        if not prefix:
            return None
        return next((c for c in self._completions if c.startswith(prefix) and c != prefix), None)

    # --- rendering ---------------------------------------------------------------------

    def render_body(self, width: int) -> list[str]:
        """Render the intro, the transcript, and the prompt with its ghost completion."""
        intro = Text(
            f"Talking to {self._node_label} over the mesh — every command is one transmission.",
            style="muted",
        )
        lines = render_lines(Group(intro, Text(), *self._log), width)
        self._prompt_line = len(lines)
        lines.extend(render_lines(self._prompt(), width))
        self._scroll_total = max(1, len(lines))
        return lines

    def cursor_line(self) -> int | None:
        """Pin the prompt so that it is visible. The transcript thus follows itself as it grows."""
        return self._prompt_line

    def _prompt(self) -> Text:
        """The input line: the spinner while MeshTerm waits, or the editor and its ghost text."""
        if self.busy:
            line = self._spinner.text()
            line.append("  waiting for the reply…", style="muted")
            return line
        line = Text("❯ ", style="brand")
        line.append_text(self._editor.render())
        completion = self._completion()
        if completion is not None:
            line.append(completion[len(self._editor.text.lstrip()) :], style="faint")
        return line
