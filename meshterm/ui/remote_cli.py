# SPDX-License-Identifier: Apache-2.0
"""The remote command line: talk to a repeater's CLI over the mesh, readline-style.

A full-screen terminal onto a logged-in remote node (part of the repeater-admin
feature): type a command, watch it fly, read the reply in a growing transcript. Every
command is one mesh transmission, so the screen sends exactly what you commit and one
at a time — a command in flight parks the prompt until the reply lands (or times out;
repeaters answer tersely and sometimes not at all).

The input line behaves like a shell:

* **↑/↓** recall the node's command history, which persists per node (see
  :class:`~meshterm.core.remote_store.RemoteStore`) so last week's incantation is one
  keystroke away next session.
* **Tab** completes the current text against the known repeater commands — the fixed
  verbs, every ``get``/``set`` spelling in the settings catalog, and any key *this node*
  taught us here on an earlier visit — and the best match previews muted, ghost-text
  style, after the cursor.
* **Enter** sends. The transcript keeps every exchange of the session, newest at the
  bottom, and the view follows the prompt as it grows.

The screen renders state and routes keys; the owning flow injects ``send`` (which owns
the radio, the history store, and the reply plumbing) and feeds replies back through
:meth:`reply` / :meth:`failed`.
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

#: The transcript's history depth in exchanges (the body scrolls; this caps memory).
_LOG_CAP = 400


class RemoteCliScreen(Screen):
    """A shell-like screen onto one remote node's CLI. The owner does the radio work."""

    floating = False

    @property
    def picocalc_lyra_lane(self):
        """The pager over the transcript, with no jumps behind it.

        Home and End never reach the transcript here: they fall through to the compose
        line's editor, where they move the text cursor to either end of what is being
        typed (see :meth:`handle`). That is the ordinary behaviour of a command line and
        both keys are on the keyboard, so the Shift companions stay blank rather than
        promising a jump the transcript would not make.
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
        """Create the command line over a node's persisted history.

        Args:
            node_label: The remote node's display name (title and prompt chrome).
            history: The node's prior commands, oldest first (recalled with ↑).
            send: Commits one command (owner-guarded; no-op while one is flying).
            session: The running TUI session (for repaints).
            extra_keys: Settings *this node* has that the catalog hasn't, found here on an
                earlier visit. They complete like any other key: the prompt is where they
                were learned, so it is where they should be one Tab away.
        """
        super().__init__()
        self.title = f"Command line — {node_label}"
        self._node_label = node_label
        self._send = send
        self._session = session
        self._editor = LineEditor()
        self._log: list[Text] = []
        self._history = list(history)
        self._recall: int | None = None  # index into history while ↑/↓ browse it
        self._draft = ""  # what was typed before recall began, restored on ↓ past the end
        self._completions = sorted(
            {
                *known_commands(),
                *(f"get {key}" for key in extra_keys),
                *(f"set {key} " for key in extra_keys),
            }
        )
        self.busy = False
        self._spinner = Spinner()
        self._prompt_line = 0  # body index of the input line (pinned into view)

    # --- owner feed ------------------------------------------------------------------

    def sent(self, command: str) -> None:
        """Echo a just-sent command into the transcript and park the prompt."""
        row = Text("❯ ", style="brand")
        row.append(command)
        self._append(row)
        self._history.append(command)
        self._recall = None
        self.busy = True
        self._spinner.reset()
        self._session.invalidate()

    def reply(self, text: str) -> None:
        """Land the node's reply (possibly multi-line) and free the prompt."""
        for line in text.splitlines() or [""]:
            self._append(Text(f"  {line}"))
        self.busy = False
        self._session.invalidate()

    def failed(self, note: str, *, error: bool = False) -> None:
        """Land a timeout/error note and free the prompt."""
        self._append(Text(f"  {note}", style="err" if error else "muted"))
        self.busy = False
        self._session.invalidate()

    def tick(self) -> None:
        """Advance the in-flight spinner (driven by the owner's animation timer)."""
        self._spinner.tick()

    def _append(self, row: Text) -> None:
        row.no_wrap = False
        self._log.append(row)
        del self._log[:-_LOG_CAP]

    # --- input -----------------------------------------------------------------------

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The footer keys, tracking whether a command is in flight."""
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
            self._recall = None  # typing leaves the history walk; the buffer is a draft
        self._session.invalidate()

    def _recall_step(self, direction: int) -> None:
        """Walk the history with ↑/↓, keeping the un-sent draft safe at the new end."""
        if not self._history:
            return
        if self._recall is None:
            if direction > 0:
                return  # nothing newer than the draft
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
        """The first known command extending the current text, or ``None``."""
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
        """Pin the prompt into view, so the transcript follows itself as it grows."""
        return self._prompt_line

    def _prompt(self) -> Text:
        """The input line: spinner while waiting, else the editor and its ghost text."""
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
