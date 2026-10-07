# SPDX-License-Identifier: Apache-2.0
"""The output surface: one small API that tools use for input and output, in either front end.

Tools do not use ``questionary`` or a raw console. They talk to ``ctx.ui``. Two
implementations are behind it:

* :class:`PlainUi` (the scripted CLI) prints at once and uses a Rich progress bar. Thus the
  behaviour of the CLI is exactly the same, byte for byte, as before the TUI existed.
* :class:`TuiUi` (the interactive menu) collects the output of a tool and presents it in a
  scrollable result screen of limited height. If the whole result is only a line or two
  of text ("✓ flood advertisement sent"), it presents the result in a small centred
  dialog with an OK button and not on a full result screen (refer to
  :func:`_collapse_to_message`). It sends each prompt and each progress display through
  the full-screen :class:`~meshterm.ui.tui.session.TuiSession`.

Only the ``prompt_params`` of a tool (menu only) can reach the interactive prompt methods,
so :class:`PlainUi` does not support them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.text import Text

from .tui.session import TuiSession

#: A validator returns ``True`` if the input is acceptable, or an error message to show.
Validator = Callable[[str], "bool | str"]

#: Buffered output of this number of lines or less can use the message dialog. This is the
#: OK dialog that :meth:`TuiUi.present` shows instead of a full result screen. The number
#: is small, because a dialog is an acknowledgement and not a place to read. The count is
#: the lines that the tool *emitted*, before any wrapping. One sentence is one outcome,
#: and its length does not change that.
_DIALOG_MAX_LINES = 3

#: ... and this number of lines or less after the text wraps to the width of the box. This
#: is what the box must draw. The two counts are different only for a long line, and this
#: is the case that needs a limit. An outcome of three lines stays in a dialog. A note that
#: is long enough to be a page of text opens the scrollable result screen, where it belongs.
_DIALOG_MAX_WRAPPED = 8

#: The number of cells that :class:`~meshterm.ui.tui.prompt.ButtonDialog` uses for the
#: chrome around its widest line: its own ``dialog_width`` margin (the padding of the
#: panel, the border, and some space). If you subtract it from the readable width of the
#: platform, you get the number of cells that a message can use.
_DIALOG_CHROME_CELLS = 12


def _dialog_wrap_cells() -> int:
    """The maximum width of a line in the message dialog, on the active platform.

    A dialog has the width of its widest line, and the compositor limits that width to the
    terminal. A line that is long enough to need the limit is clipped on a screen of the
    readable width. A long outcome, for example the sentence that explains why a read
    failed, can stay in a dialog only if the code wraps it to the width that fits there.
    If it does not, the outcome takes the whole frame.
    """
    from ..platforms import get_platform

    platform = get_platform()
    return max(24, platform.readable_cols - platform.dialog_margin - _DIALOG_CHROME_CELLS)


def _wrapped(lines: list[Text], width: int) -> list[Text]:
    """Wrap ``lines`` again to ``width`` cells, and keep each span that they have.

    A blank line wraps to nothing, but a blank line is content in an outcome block, so the
    function keeps it as it is. The wrap leaves the trailing space of the break on the line
    that it ended. If the line keeps that space, the dialog centres the line with the space.
    Thus the function trims each line.
    """
    from .tui.render import _console

    console = _console(width)
    out: list[Text] = []
    for line in lines:
        parts = list(line.wrap(console, width)) or [Text("")]
        for part in parts:
            part.rstrip()  # in place. Rich's rstrip returns nothing
        out.extend(parts)
    return out


class _NullBusy:
    """The substitute of the scripted CLI for a busy card: a caption that goes nowhere."""

    def __init__(self) -> None:
        """Start with an empty caption. The only contract is that a caller can assign to it."""
        self.message = ""


def _collapse_to_message(buffered: list[RenderableType]) -> Text | None:
    """Collapse a small buffered output of only text into one wrapped dialog message.

    This is the gate for the upgrade to a dialog in :meth:`TuiUi.present`. The output
    qualifies only if all the buffered items are plain note text (:class:`Text`). A table
    or a panel from ``show()`` makes the whole output not qualify. The total must be at
    most :data:`_DIALOG_MAX_LINES` emitted lines, and at most :data:`_DIALOG_MAX_WRAPPED`
    lines after the text is wrapped again to the width of the box. The function keeps the
    styling (the ``[ok]`` and ``[warn]`` markup on outcome notes) in the joined message.
    Thus the frame of the dialog can take its tone from the styling.

    The width causes a **wrap**, not a rejection. An outcome of one sentence is an outcome
    to acknowledge, and its length does not change that. The code once sent such an outcome
    to the scrollable result screen because it was 140 cells wide. Then the least interesting
    results took over the screen. The worst case was a tool that failed before it made any
    output.

    Args:
        buffered: The renderables that were collected since the last present.

    Returns:
        The lines joined into one :class:`Text`, or ``None`` if the output belongs in the
        scrollable result screen.
    """
    if not buffered or not all(isinstance(item, Text) for item in buffered):
        return None
    lines: list[Text] = []
    for item in buffered:
        lines.extend(item.split("\n") or [item])
    if len(lines) > _DIALOG_MAX_LINES:
        return None
    lines = _wrapped(lines, _dialog_wrap_cells())
    if len(lines) > _DIALOG_MAX_WRAPPED:
        return None
    return Text("\n").join(lines)


class Ui:
    """Abstract output surface. :class:`PlainUi` and :class:`TuiUi` are the two backends."""

    def show(self, *renderables: RenderableType) -> None:
        """Show one or more Rich renderables (tables, panels, text)."""
        raise NotImplementedError

    def note(self, markup: str) -> None:
        """Show a short line of text in Rich markup."""
        raise NotImplementedError

    def ack(self, markup: str) -> None:
        """Acknowledge that an action was done. In the menu only.

        The difference from :meth:`note` is the person for whom the line is. A note is
        *output*: the answer that the caller asked for. An acknowledgement tells the user
        that something occurred, for example "✓ device clock set" or "✓ channel 2 =
        #general". A person who looks at a screen needs it. A script does not need it,
        because the exit status already says it. For a script the line is only something to
        remove from the real output.

        Both faces show acknowledgements, but in different places. The menu prints them in
        its result screen. The CLI prints them on **stderr** (refer to
        :meth:`PlainUi.ack`). Thus the CLI keeps the promise that was the reason for the
        removal: ``meshterm contacts > f`` still catches only the answer. Also, the person
        at the prompt gets back the ✓ and the count that a rule for a redirect took away,
        and that person did not use a redirect.
        """
        raise NotImplementedError

    async def view(
        self, renderable: RenderableType, *, title: str = "", footer_hint: str = ""
    ) -> None:
        """Show a renderable at once on a screen that the user can close (CLI mode prints it)."""
        raise NotImplementedError

    async def present(self, *, title: str = "") -> None:
        """Flush all buffered output to the user. It does nothing if the output is immediate."""

    def discard(self) -> None:
        """Remove buffered output that was not shown. It does nothing if output is immediate."""

    def progress(self, title: str = "Working"):  # noqa: ANN201 - context manager, varies by backend
        """Return a progress context manager with ``add_task``, ``advance``, and ``update``."""
        raise NotImplementedError

    def busy_overlay(self, message: str = "", *, title: str = ""):  # noqa: ANN201 - async CM, varies by backend
        """Return an async context manager that floats a "working" skeleton while its block runs.

        In the interactive menu this shows the skeleton card at the top (refer to
        :meth:`~meshterm.ui.tui.session.TuiSession.busy_overlay`). It covers the delay of a
        Bluetooth operation. Without it, the screen would be blank. In the scripted CLI it
        does nothing, because there is no full-screen display to float over.
        """
        raise NotImplementedError

    def busy_dialog(self, message: str = "", *, title: str = ""):  # noqa: ANN201 - async CM, varies by backend
        """Return an async context manager that floats a **modal** busy card over a screen.

        It is the counterpart of :meth:`busy_overlay`. Use it for slow work that a *screen*
        starts, and use the overlay for work that occurs in the gap between screens. The
        overlay is not a screen. It draws only on an empty stack, and it takes no keys. Thus
        a hub that stays pushed while it works gets no card and no protection. The busy
        dialog is pushed and modal. Refer to
        :meth:`~meshterm.ui.tui.session.TuiSession.busy_dialog` for the result of that. In
        the scripted CLI it does nothing, the same as the overlay.
        """
        raise NotImplementedError

    async def select(
        self,
        title: str,
        items: list,
        *,
        prompt: str = "",
        default: Any = None,
        filterable: bool = True,
        delete_hint: str = "",
        floating: bool = False,
    ) -> Any:
        """Ask the user to select one item. Return its value, or ``None`` if the user cancels.

        ``prompt`` draws an instruction in the dialog, above the list. For a short, fixed
        list, pass ``filterable=False``. Then a key press by mistake cannot make the list
        narrower (and change its size). ``delete_hint`` (with rows marked as deletable)
        turns on the remove flow of the Delete key. ``floating`` keeps a lead-in question
        drawn as a box, also when there is nothing under it. Refer to
        :meth:`~meshterm.ui.tui.session.TuiSession.select`.
        """
        raise NotImplementedError

    async def select_startup(
        self,
        title: str,
        items: list,
        *,
        default: Any = None,
        banner: Any = None,
        footnote: str | None = None,
        footer_hint: str | None = None,
        keys: Mapping[str, Any] | None = None,
        key_hint: Callable[[Any], str] | None = None,
        live: Callable[[Callable[[list], None]], Awaitable[None]] | None = None,
        hscroll: bool | None = None,
    ) -> Any:
        """Select one item on a startup splash that has no chrome. Return ``None`` if skipped.

        ``live`` is work to run while the splash is up. It gets a ``redraw(items)`` function
        that replaces the rows under the user and paints again. The rescan of the device
        picker is an example. The code cancels the work when the splash resolves.

        ``keys`` declares the bare-key shortcuts that the splash answers. It resolves with a
        :class:`~meshterm.ui.tui.select.KeyRequest` (the hide and show-all pair of the
        picker). ``key_hint`` names them for each highlighted row, so the footer advertises
        a key only where the key acts. ``footer_hint`` replaces the base sentence.
        """
        raise NotImplementedError

    async def confirm_startup(
        self,
        prompt: str | Text,
        *,
        title: str = "",
        confirm_label: str = "Remove",
        banner: Any = None,
        footnote: str | None = None,
        backdrop_items: list | None = None,
        backdrop_default: Any = None,
    ) -> bool:
        """Confirm a destructive action on the startup splash.

        Return ``True`` only if the user commits.

        ``backdrop_items`` (the rows of the picker) floats the confirm over a device list
        that is drawn again. ``backdrop_default`` highlights the row that the action is for.
        """
        raise NotImplementedError

    async def notify_startup(
        self,
        renderable: RenderableType,
        *,
        title: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> None:
        """Show a message on a startup splash that has no chrome, until the user closes it."""
        raise NotImplementedError

    async def busy_startup(
        self,
        message: str,
        coro: Any,
        *,
        title: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> Any:
        """Await ``coro`` while the startup splash shows a spinner. Return its result."""
        raise NotImplementedError

    async def prompt_pin_startup(
        self,
        device_name: str,
        *,
        error: str = "",
        help_text: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> str | None:
        """Ask for the pairing PIN of a Bluetooth companion on the splash. ``None`` if cancelled."""
        raise NotImplementedError

    async def prompt_text_startup(
        self,
        title: str,
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
        help_text: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> str | None:
        """Ask for a line of text on a startup splash that has no chrome. ``None`` if cancelled."""
        raise NotImplementedError

    async def reorder(self, title: str, labels: list[str]) -> list[int]:
        """Let the user put the rows in a new order with the arrows.

        Returns:
            The new order of the row indices.
        """
        raise NotImplementedError

    async def text(
        self,
        title: str,
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
        help_text: str = "",
        password: bool = False,
        floating: bool = False,
    ) -> str | None:
        """Ask for a line of text. Return it, or ``None`` if the user cancels.

        Keep ``title`` short, because it is in the border of the dialog. Put the question or
        the instruction in ``prompt``, which is drawn above the field. Thus a text dialog
        looks like the button dialogs. ``floating`` makes the prompt draw as a centred
        dialog also when there is nothing under it (a modal in the middle of a flow, such as
        a remote-admin password). Without it, the prompt fills the frame. A backend that has
        no stack of screens ignores it.
        """
        raise NotImplementedError

    async def confirm(self, title: str, *, default: bool = True) -> bool | None:
        """Ask a yes/no question. Return the answer, or ``None`` if the user cancels."""
        raise NotImplementedError

    async def dialog(
        self,
        prompt: str | Text,
        buttons: list[tuple[str, Any]],
        *,
        title: str = "",
        default: int = 0,
        keys: dict[str, Any] | None = None,
        danger: bool = False,
        destructive: bool = False,
    ) -> Any:
        """Show a centred button dialog. Return the selected value, or ``None`` on Esc.

        This is the general dialog to select one option (a prompt above a row of buttons).
        There are two levels of caution, and they colour the prompt and the border.
        ``danger`` (amber) is for a disruptive choice, such as to discard edits or to
        reboot. ``destructive`` (the reserved error red) is for a loss of data that cannot
        be undone. Thus a delete confirm is red, the same as its typed-confirm sibling.
        The buttons follow the convention of platform dialogs. The safe way out is on the
        left, and the committing action is on the right. The committing action is also the
        correct ``default``. Thus Enter commits it, and Esc always backs out.

        The code draws a :class:`~rich.text.Text` prompt that has a style as it is, so the
        prompt can name a node in its own colours. The level then colours only the border,
        and the prose of the prompt has the colour of the level itself.
        """
        raise NotImplementedError

    async def typed_confirm(self, warning: str, word: str, *, title: str = "Are you sure?") -> bool:
        """Make the user type ``word`` before a destructive action. Return if it was typed."""
        raise NotImplementedError

    async def autocomplete(
        self,
        title: str,
        choices: list[str],
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
    ) -> str | None:
        """Ask for free text with suggestions. Return it, or ``None`` if the user cancels."""
        raise NotImplementedError

    async def path(self, title: str, *, prompt: str = "", default: str = "") -> str | None:
        """Ask for a filesystem path. Return it, or ``None`` if the user cancels."""
        raise NotImplementedError


class PlainUi(Ui):
    """The CLI backend. It prints directly to the scripted console and has no prompts.

    The backend does not support interactive prompts. The console that it prints on emits
    no colour, wraps nothing, and trims trailing whitespace
    (:func:`meshterm.ui.script.console`). Everything that a tool gives to it passes through
    :func:`~meshterm.ui.script.flatten` on the way out. Thus no border, no box, and no
    hoisted title reaches stdout. That fold is a safety net and not the design. The output
    that a script must read is *written* in the CLI's own vocabulary (refer to
    :mod:`meshterm.ui.script`).
    """

    def __init__(self, console: Console) -> None:
        """Connect the backend to a Rich console.

        Args:
            console: The console to which the output of a tool is printed.
        """
        self.console = console

    def show(self, *renderables: RenderableType) -> None:
        """Print each renderable at once, with no frame.

        Refer to :func:`~meshterm.ui.script.flatten`.
        """
        from . import script

        for renderable in renderables:
            for item in script.flatten(renderable):
                self.console.print(item)

    def note(self, markup: str) -> None:
        """Print a markup line to the console at once, as plain text.

        The parameter is markup. This is the contract of the shared ``Ui`` class, and more
        than forty callers write markup. But the scripted console does not interpret
        markup. A node broadcasts its own name, and Rich would read a ``[...]`` in a name as
        a style tag. Thus this method resolves the tags and the console does not, and the
        text that the tags wrapped is what reaches stdout. When the method printed the tags
        as they were, ``[muted]`` and ``[/muted]`` were around the private key. But
        ``config export-key > key.hex`` must have only the key as its content.

        Malformed markup is what a *name* that has a bracket looks like. It is not an error
        to report but a string to print, so the method prints it as it is.
        """
        from rich.errors import MarkupError
        from rich.markup import render

        try:
            self.console.print(render(markup).plain)
        except MarkupError:
            self.console.print(markup)

    def ack(self, markup: str) -> None:
        """Print the acknowledgement on stderr, where all output *about* the run goes.

        The CLI once removed these lines. The reason was correct, but only to a point. An
        acknowledgement is not the answer, and a caller that redirects stdout must catch
        only the answer. But stdout is not the only stream. If the CLI prints them on
        stderr, it follows that rule exactly. Also, a person who watches a ``config
        restore`` of five minutes gets the ✓ for each setting, which the person could not
        see before.

        This method resolves the markup and the console does not, as :meth:`note` explains.
        A *name* that has a square bracket is not an error to report but a string to print.
        Thus malformed markup is printed as it is.
        """
        from rich.errors import MarkupError
        from rich.markup import render

        from . import script

        try:
            line: Text | str = render(markup)
        except MarkupError:
            line = markup
        script.stderr_console().print(line, highlight=False)

    async def view(
        self, renderable: RenderableType, *, title: str = "", footer_hint: str = ""
    ) -> None:
        """Print the renderable at once, with no frame. CLI mode has no screens."""
        from . import script

        for item in script.flatten(renderable):
            self.console.print(item)

    def progress(self, title: str = "Working"):  # noqa: ANN201
        """Return a progress bar that is drawn on stderr, so it is never in piped output.

        A progress bar helps a person who watches a long command run. For a program that
        reads the output of the command, it is noise. stderr is the correct place for it.
        It is visible in a terminal, and it is not in ``meshterm contacts > contacts.txt``.
        Also, Rich draws nothing when stderr is not a terminal, so a run with all streams
        redirected is silent.
        """
        from . import script
        from .widgets import make_progress

        console = script.stderr_console()
        progress = make_progress(console)
        progress.disable = not console.is_terminal
        return progress

    @asynccontextmanager
    async def busy_overlay(self, message: str = "", *, title: str = "") -> AsyncIterator[None]:
        """Do nothing. The scripted CLI has no full-screen display to float a skeleton over."""
        yield

    @asynccontextmanager
    async def busy_dialog(self, message: str = "", *, title: str = "") -> AsyncIterator[_NullBusy]:
        """Do nothing. There is no screen to interrupt and no keyboard to take.

        It still yields an object with a caption that a caller can set. A caller that
        changes the title of the card in the middle of a batch must not have to ask which
        backend it runs on.
        """
        yield _NullBusy()

    def _no_prompt(self) -> RuntimeError:
        """Build the error that is raised if a prompt is reached on the non-interactive path."""
        return RuntimeError("interactive prompts are only available in the menu")

    async def select(
        self,
        title: str,
        items: list,
        *,
        prompt: str = "",
        default: Any = None,
        filterable: bool = True,
        delete_hint: str = "",
        floating: bool = False,
    ) -> Any:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()

    async def select_startup(
        self,
        title: str,
        items: list,
        *,
        default: Any = None,
        banner: Any = None,
        footnote: str | None = None,
        footer_hint: str | None = None,
        keys: Mapping[str, Any] | None = None,
        key_hint: Callable[[Any], str] | None = None,
        live: Callable[[Callable[[list], None]], Awaitable[None]] | None = None,
        hscroll: bool | None = None,
    ) -> Any:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()

    async def confirm_startup(
        self,
        prompt: str | Text,
        *,
        title: str = "",
        confirm_label: str = "Remove",
        banner: Any = None,
        footnote: str | None = None,
        backdrop_items: list | None = None,
        backdrop_default: Any = None,
    ) -> bool:
        """Not supported in scripted CLI mode. The picker splash is only for the menu."""
        raise self._no_prompt()

    async def notify_startup(
        self,
        renderable: RenderableType,
        *,
        title: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> None:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()

    async def busy_startup(
        self,
        message: str,
        coro: Any,
        *,
        title: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> Any:
        """There is no splash in scripted CLI mode. Await the task and return its result."""
        return await coro

    async def prompt_pin_startup(
        self,
        device_name: str,
        *,
        error: str = "",
        help_text: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> str | None:
        """Not supported in scripted CLI mode. The caller must give a PIN with no prompt.

        The scripted path cannot show a dialog. Thus a device that has a PIN gets the clear
        ``DeviceAuthenticationError`` message (the user passes ``--ble-pin``) and not a
        prompt.
        """
        raise self._no_prompt()

    async def prompt_text_startup(
        self,
        title: str,
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
        help_text: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> str | None:
        """Not supported in scripted CLI mode. A network endpoint comes from ``--tcp``."""
        raise self._no_prompt()

    async def reorder(self, title: str, labels: list[str]) -> list[int]:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()

    async def text(
        self,
        title: str,
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
        help_text: str = "",
        password: bool = False,
        floating: bool = False,
    ) -> str | None:
        """Ask on the terminal (line editor or getpass). Ask again until the input is valid.

        A few tools (for example, a remote-admin password) can correctly ask a question in a
        scripted run when the user did not give a flag. Thus this method works on the CLI.
        The method prints the in-body ``prompt`` (which the interactive dialogs use) one time
        as a lead-in line. ``floating`` is an extra for a full-screen dialog, and it has no
        meaning on the plain terminal. The method accepts it and ignores it.

        Returns:
            The string that the user entered, or ``None`` on EOF or an interrupt.
        """
        import getpass

        if prompt:
            self.console.print(prompt)
        head = f"{title} " if not default else f"{title} [{default}] "
        prompt = head
        while True:
            try:
                raw = getpass.getpass(prompt) if password else input(prompt)
            except (EOFError, KeyboardInterrupt):
                return None
            value = raw if raw != "" else default
            if validate is not None:
                result = validate(value)
                if result is not True:
                    self.console.print(f"[err]{result}[/err]")
                    continue
            return value

    async def confirm(self, title: str, *, default: bool = True) -> bool | None:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()

    async def dialog(
        self,
        prompt: str | Text,
        buttons: list[tuple[str, Any]],
        *,
        title: str = "",
        default: int = 0,
        keys: dict[str, Any] | None = None,
        danger: bool = False,
        destructive: bool = False,
    ) -> Any:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()

    async def typed_confirm(self, warning: str, word: str, *, title: str = "Are you sure?") -> bool:
        """Not supported in scripted CLI mode. A destructive CLI command needs ``--yes``."""
        raise self._no_prompt()

    async def autocomplete(
        self,
        title: str,
        choices: list[str],
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
    ) -> str | None:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()

    async def path(self, title: str, *, prompt: str = "", default: str = "") -> str | None:
        """Unsupported in scripted CLI mode."""
        raise self._no_prompt()


class TuiUi(Ui):
    """The menu backend. It buffers output for a result screen and sends prompts to the session."""

    def __init__(self, session: TuiSession) -> None:
        """Connect the backend to a running session.

        Args:
            session: The full-screen session that renders the prompts and the screens.
        """
        self.session = session
        self._buffer: list[RenderableType] = []

    # --- output --------------------------------------------------------------

    def show(self, *renderables: RenderableType) -> None:
        """Collect renderables for the next result screen."""
        self._buffer.extend(renderables)

    def note(self, markup: str) -> None:
        """Collect a markup line for the next result screen."""
        self._buffer.append(Text.from_markup(markup))

    def ack(self, markup: str) -> None:
        """Collect an acknowledgement. In the menu, it is the same as a note."""
        self.note(markup)

    async def present(self, *, title: str = "") -> None:
        """Show all output that was buffered since the last present, on a screen or in a dialog.

        A short outcome that has only text (a line or two of notes, for example "✓ flood
        advertisement sent") floats as a centred OK dialog over the current screen. Thus a
        result of one line never takes the whole frame. A bigger outcome, or an outcome that
        has a table or a panel, opens the scrollable result screen of limited height, as
        before. Refer to :func:`_collapse_to_message` for the exact gate. The method then
        clears the buffer. It does nothing if nothing was buffered (for example, a tool that
        made only a file and an empty message).

        Args:
            title: The heading for the result screen or the dialog.
        """
        if not self._buffer:
            return
        buffered = self._buffer
        self._buffer = []
        message = _collapse_to_message(buffered)
        if message is not None:
            await self.session.message_dialog(message, title=title)
            return
        body = buffered[0] if len(buffered) == 1 else Group(*buffered)
        await self.session.scroll(body, title=title)

    def discard(self) -> None:
        """Remove all buffered output and do not show it."""
        self._buffer = []

    async def view(
        self, renderable: RenderableType, *, title: str = "", footer_hint: str = ""
    ) -> None:
        """Show a renderable at once on a scroll screen that the user can close."""
        await self.session.scroll(renderable, title=title, footer_hint=footer_hint)

    def progress(self, title: str = "Working"):  # noqa: ANN201
        """Return a context manager of a progress dialog for the session."""
        return self.session.progress(title)

    def busy_overlay(self, message: str = "", *, title: str = ""):  # noqa: ANN201
        """Float the skeleton card at the top of the session while the wrapped block runs."""
        return self.session.busy_overlay(message, title=title)

    def busy_dialog(self, message: str = "", *, title: str = ""):  # noqa: ANN201
        """Push the modal busy card of the session over the current screen while the block runs."""
        return self.session.busy_dialog(message, title=title)

    # --- input ---------------------------------------------------------------

    async def select(
        self,
        title: str,
        items: list,
        *,
        prompt: str = "",
        default: Any = None,
        filterable: bool = True,
        delete_hint: str = "",
        floating: bool = False,
    ) -> Any:
        """Give the work to the select screen of the session."""
        return await self.session.select(
            title,
            items,
            prompt=prompt,
            default=default,
            filterable=filterable,
            delete_hint=delete_hint,
            floating=floating,
        )

    async def select_startup(
        self,
        title: str,
        items: list,
        *,
        default: Any = None,
        banner: Any = None,
        footnote: str | None = None,
        footer_hint: str | None = None,
        keys: Mapping[str, Any] | None = None,
        key_hint: Callable[[Any], str] | None = None,
        live: Callable[[Callable[[list], None]], Awaitable[None]] | None = None,
        hscroll: bool | None = None,
    ) -> Any:
        """Give the work to the startup select splash of the session, which has no chrome."""
        return await self.session.select_startup(
            title,
            items,
            default=default,
            banner=banner,
            footnote=footnote,
            keys=keys,
            key_hint=key_hint,
            live=live,
            hscroll=hscroll,
            **({} if footer_hint is None else {"footer_hint": footer_hint}),
        )

    async def confirm_startup(
        self,
        prompt: str | Text,
        *,
        title: str = "",
        confirm_label: str = "Remove",
        banner: Any = None,
        footnote: str | None = None,
        backdrop_items: list | None = None,
        backdrop_default: Any = None,
    ) -> bool:
        """Give the work to the startup confirm dialog of the session (floated over the picker)."""
        return await self.session.confirm_startup(
            prompt,
            title=title,
            confirm_label=confirm_label,
            banner=banner,
            footnote=footnote,
            backdrop_items=backdrop_items,
            backdrop_default=backdrop_default,
        )

    async def notify_startup(
        self,
        renderable: RenderableType,
        *,
        title: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> None:
        """Give the work to the startup message splash of the session, which has no chrome."""
        await self.session.notify_startup(renderable, title=title, banner=banner, footnote=footnote)

    async def busy_startup(
        self,
        message: str,
        coro: Any,
        *,
        title: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> Any:
        """Give the work to the startup splash of the session that has an animated spinner."""
        return await self.session.busy_startup(
            message, coro, title=title, banner=banner, footnote=footnote
        )

    async def prompt_pin_startup(
        self,
        device_name: str,
        *,
        error: str = "",
        help_text: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> str | None:
        """Give the work to the startup PIN dialog of the session."""
        return await self.session.prompt_pin_startup(
            device_name, error=error, help_text=help_text, banner=banner, footnote=footnote
        )

    async def prompt_text_startup(
        self,
        title: str,
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
        help_text: str = "",
        banner: Any = None,
        footnote: str | None = None,
    ) -> str | None:
        """Give the work to the startup text dialog of the session, which has no chrome."""
        return await self.session.prompt_text_startup(
            title,
            prompt=prompt,
            default=default,
            validate=validate,
            help_text=help_text,
            banner=banner,
            footnote=footnote,
        )

    async def reorder(self, title: str, labels: list[str]) -> list[int]:
        """Give the work to the reorder screen of the session."""
        return await self.session.reorder(title, labels)

    async def text(
        self,
        title: str,
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
        help_text: str = "",
        password: bool = False,
        byte_limit: int | None = None,
        floating: bool = False,
    ) -> str | None:
        """Give the work to the text screen of the session."""
        return await self.session.text(
            title,
            prompt=prompt,
            default=default,
            validate=validate,
            help_text=help_text,
            password=password,
            byte_limit=byte_limit,
            floating=floating,
        )

    async def confirm(self, title: str, *, default: bool = True) -> bool | None:
        """Give the work to the confirm screen of the session."""
        return await self.session.confirm(title, default=default)

    async def dialog(
        self,
        prompt: str | Text,
        buttons: list[tuple[str, Any]],
        *,
        title: str = "",
        default: int = 0,
        keys: dict[str, Any] | None = None,
        danger: bool = False,
        destructive: bool = False,
    ) -> Any:
        """Give the work to the button dialog of the session, at the requested caution level.

        ``danger`` gives the frame a colour of caution. ``destructive`` uses the reserved
        error red, and it is only for a loss of data that cannot be undone.
        """
        tier = "err" if destructive else "warn" if danger else ""
        return await self.session.button_dialog(
            prompt,
            buttons,
            title=title,
            default=default,
            keys=keys,
            prompt_style=tier,
            border_style=tier or "accent",
        )

    async def typed_confirm(self, warning: str, word: str, *, title: str = "Are you sure?") -> bool:
        """Give the work to the typed-confirmation dialog of the session."""
        return await self.session.typed_confirm(warning, word, title=title)

    async def autocomplete(
        self,
        title: str,
        choices: list[str],
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
    ) -> str | None:
        """Give the work to the autocomplete screen of the session."""
        return await self.session.autocomplete(
            title, choices, prompt=prompt, default=default, validate=validate
        )

    async def path(self, title: str, *, prompt: str = "", default: str = "") -> str | None:
        """Ask for a path as free text. The current value is already in the field."""
        return await self.session.text(
            title, prompt=prompt, default=default, help_text="filesystem path"
        )
