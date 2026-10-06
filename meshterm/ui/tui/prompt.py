# SPDX-License-Identifier: Apache-2.0
"""Input screens: text, confirm, and autocomplete. They replace ``questionary``.

These screens implement again the small part of ``questionary`` that the app uses: a line
editor with validation, a yes/no toggle, and a free-text field with suggestions. They are
uniform TUI screens. Thus each prompt has the same look, layering, resize, and
Esc-to-cancel behaviour as the rest of the framework.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.console import Group, RenderableType
from rich.text import Text

from ..marks import MASK_MARK
from .render import render_lines, right_aligned_tail
from .screen import Screen
from .spinner import Spinner

if TYPE_CHECKING:
    from .fkeys import FPair

#: A validator returns ``True`` when the input is acceptable. Otherwise, it returns an error
#: message to show.
Validator = Callable[[str], "bool | str"]

#: The LoRa payload of MeshCore limits a direct message to 150 UTF-8 bytes, and an
#: (unscoped) channel broadcast to 130 bytes. Past the limit, the companion discards the
#: packet and gives no error. Each compose bar that feeds one of those sends (the chat input,
#: the message entry of the courier) counts bytes against these limits. Over the limit, it
#: blocks the send, instead of a lost packet.
DM_BYTE_LIMIT = 150
CHANNEL_BYTE_LIMIT = 130

#: The thresholds of the byte counter (in bytes that remain) at which its colour goes up one
#: step, and the two colours of the middle band. The theme's ``warn`` and ``err`` are too close
#: together (an amber that looks orange, then red). Thus the counter names a truer yellow and
#: orange directly, so that the green→yellow→orange→red fuel gauge has visible steps.
_BYTES_TIGHT, _BYTES_LOW = 20, 10
_BYTES_YELLOW = "bold #fde047"
_BYTES_ORANGE = "bold #ff9500"


def byte_style(remaining: int) -> str:
    """Map the bytes that remain to the colour of the counter (green→yellow→orange→red)."""
    if remaining <= 0:
        return "err"  # at or over the limit: the send is blocked
    if remaining <= _BYTES_LOW:
        return _BYTES_ORANGE
    if remaining <= _BYTES_TIGHT:
        return _BYTES_YELLOW
    return "ok"


def byte_counter(used: int, limit: int) -> Text:
    """The inline ``used/limit`` budget. Only ``used`` gets a colour, from how much is left.

    The colour is green when there is space to spare, yellow within :data:`_BYTES_TIGHT`
    bytes, orange within :data:`_BYTES_LOW`, and red when the text is at or over the limit.
    Thus the number is a fuel gauge, and the ``/limit`` suffix stays muted (it never
    changes). The chat compose bar and the message field of the courier both draw this
    shared gauge.
    """
    counter = Text()
    counter.append(str(used), style=byte_style(limit - used))
    counter.append(f"/{limit}", style="muted")
    return counter


def _center(content: Text, width: int) -> Text:
    """Centre ``content`` in ``width``, with plain padding on the two sides.

    Use this function instead of ``Text(justify="center")`` for lines that hold a
    reverse-video button chip. When a styled span is directly against justify padding, Rich
    removes the trailing whitespace of the span. That removes the right edge of the chip
    (``"  Quit"`` instead of ``"  Quit  "``), and the label then stays at the left of the
    fill. Padding added by hand keeps a real segment after the chip. Thus the trailing cells
    of the chip stay, and the fill stays symmetric.
    """
    pad = max(0, width - content.cell_len)
    left = pad // 2
    line = Text(" " * left)
    line.append_text(content)
    line.append(" " * (pad - left))
    return line


class LineEditor:
    """A small single-line text editor (insert, delete, and cursor movement).

    :class:`TextScreen` and :class:`AutocompleteScreen` share it. It tracks the text and the
    cursor position. The render shows the cursor as a reverse-video cell.
    """

    def __init__(self, initial: str = "", *, max_length: int | None = None) -> None:
        """Start the editor with ``initial`` text and the cursor at its end.

        Args:
            initial: The contents of the buffer at the start.
            max_length: An optional hard limit on the number of characters. More insertions
                are ignored (or a paste is truncated to fit). ``None`` leaves the length
                without a limit.
        """
        self.text = initial
        self.cursor = len(initial)
        self._max_length = max_length

    def edit(self, action: str, data: str = "") -> bool:
        """Apply an edit action. Return ``True`` if it changed the buffer or the cursor.

        Args:
            action: The normalized key action.
            data: The character to insert when ``action`` is ``text``, or the whole pasted
                run when ``action`` is ``paste``.

        Returns:
            ``True`` if the action was an edit action that this method handled.
        """
        if action == "paste":
            # A paste arrives as one multi-character run. A single-line field cannot hold
            # newlines or control characters, so fold them to spaces. Then insert the run
            # exactly as typed text (the max-length trim below still applies).
            data = "".join(ch if ch.isprintable() else " " for ch in data)
            if not data:
                return False
            action = "text"
        if action == "text" and data.isprintable():
            if self._max_length is not None:
                room = self._max_length - len(self.text)
                if room <= 0:
                    return False  # full: ignore the key, and do not change the buffer
                data = data[:room]  # a multi-character paste fills only the slots that remain
            self.text = self.text[: self.cursor] + data + self.text[self.cursor :]
            self.cursor += len(data)
        elif action == "backspace" and self.cursor > 0:
            self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
            self.cursor -= 1
        elif action == "delete" and self.cursor < len(self.text):
            self.text = self.text[: self.cursor] + self.text[self.cursor + 1 :]
        elif action == "left":
            self.cursor = max(0, self.cursor - 1)
        elif action == "right":
            self.cursor = min(len(self.text), self.cursor + 1)
        elif action == "home":
            self.cursor = 0
        elif action == "end":
            self.cursor = len(self.text)
        elif action == "ctrl_left":
            self.cursor = self._word_left(self.cursor)
        elif action == "ctrl_right":
            self.cursor = self._word_right(self.cursor)
        else:
            return False
        return True

    def _word_left(self, pos: int) -> int:
        """Index of the word start at or left of ``pos`` (the previous word if already there).

        It skips the whitespace immediately left of the cursor, then the run of word characters.
        Thus from the middle of a word, it lands on the first character of that word. From a
        word start, it steps back to the previous word. This is the usual Ctrl+Left behaviour
        of a text editor.
        """
        i = pos
        while i > 0 and self.text[i - 1].isspace():
            i -= 1
        while i > 0 and not self.text[i - 1].isspace():
            i -= 1
        return i

    def _word_right(self, pos: int) -> int:
        """Index of the start of the next word after ``pos`` (or the line end if none remains).

        It skips the current run of word characters, then the whitespace after it. Thus it
        lands on the first character of the next word. This is the usual Ctrl+Right behaviour.
        """
        n = len(self.text)
        i = pos
        while i < n and not self.text[i].isspace():
            i += 1
        while i < n and self.text[i].isspace():
            i += 1
        return i

    def render(
        self, mask: bool = False, *, overflow_at: int | None = None, slots: int | None = None
    ) -> Text:
        """Render the current line with a reverse-video cursor cell.

        Args:
            mask: When ``True``, replace each character with a bullet (password entry).
            overflow_at: The character index at which the text goes past a byte budget. That
                character and all characters after it show in the error style, so that the
                user can see exactly what to cut. ``None`` (the default) gives the line a
                plain style.
            slots: When set, render exactly this number of fixed positions (a masked PIN
                field). A typed position shows a bullet, and a blank position shows a muted
                centre dot. This argument has priority over ``mask`` and ``overflow_at``.
        """
        if slots is not None:
            return self._render_slots(slots)
        shown = MASK_MARK * len(self.text) if mask else self.text
        text = Text("› ", style="accent")
        for i, ch in enumerate(shown):
            over = overflow_at is not None and i >= overflow_at
            if i == self.cursor:
                text.append(ch, style="err.reverse" if over else "reverse")
            else:
                text.append(ch, style="err" if over else None)
        if self.cursor >= len(shown):  # cursor past the last character → trailing block
            text.append(" ", style="reverse")
        return text

    def _render_slots(self, slots: int) -> Text:
        """Render a fixed-width masked field: filled bullets and centred dots for blanks.

        The PIN entry uses this method. Each of the ``slots`` positions shows a bullet (``•``)
        after it is typed, and a muted centre dot (``·``) while it is blank. Spaces separate
        the positions, so that the field looks like a row of PIN boxes, not a line that grows.
        The cursor position is drawn in reverse video.
        """
        filled = len(self.text)
        text = Text("› ", style="accent")
        for i in range(slots):
            if i:
                text.append(" ")
            glyph = MASK_MARK if i < filled else "·"
            if i == self.cursor:
                text.append(glyph, style="reverse")  # the active slot
            elif i < filled:
                text.append(glyph)  # an entered digit
            else:
                text.append(glyph, style="muted")  # a blank still to fill
        return text


class _KeylessDialog(Screen):
    """A dialog with no F-key lane, because it has nothing that a lane can carry.

    The user answers each prompt in this module with Enter, Esc, the arrow keys, and typed
    text. These keyboard keys are directly under the hands of the user on both platforms.
    None of these prompts scroll. Thus the shared pager (which the base
    :class:`~meshterm.ui.tui.screen.Screen` gives by default) names two keys that do nothing
    here. The rule of the lane is: leave a slot **empty**, not dim, when the action
    does not exist on this screen. Dim is for an action that is not available for this paint
    only. Thus these dialogs draw only the key numbers (refer to :mod:`~meshterm.ui.tui.fkeys`).

    Each of these dialogs is also :attr:`~meshterm.ui.tui.screen.Screen.modal`. A prompt is a
    question with no answer yet, so the pop-all key (^W) does not unwind past it. The user
    answers it first, or leaves it with the Esc key.
    """

    modal = True

    @property
    def picocalc_lyra_lane(self):
        """No slots. On a prompt, nothing pages or jumps, and no chord must have a chip."""
        from .fkeys import EMPTY_LANE

        return EMPTY_LANE


class TextScreen(_KeylessDialog):
    """A validated single-line text prompt. It resolves with the string, or CANCEL on Esc."""

    def __init__(
        self,
        title: str,
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
        help_text: str = "",
        password: bool = False,
        byte_limit: int | None = None,
        footer_hint: str = "Enter accept · Esc cancel",
    ) -> None:
        """Build a text prompt.

        Args:
            title: A short heading in the border of the dialog.
            prompt: The question or instruction in the box, above the field. Keep the
                ``title`` short and put the detail here. Thus the dialog looks like the
                button dialogs (a prompt above its controls), not like a lone field.
            default: Prefilled text.
            validate: An optional validator that runs on Enter. If it returns a string, the
                string shows as an error and the submission is blocked.
            help_text: An optional muted hint under the field.
            password: When ``True``, mask the entered text.
            byte_limit: When set, the field has the shared UTF-8 byte gauge
                (:func:`byte_counter`) pinned at the bottom right. The field blocks the
                submission when the text is longer than the limit. Use it for a field that
                feeds a packet with a size limit (a direct message, a courier entry). Keep
                ``None`` for a field with no limit.
            footer_hint: The key hint in the footer.
        """
        super().__init__()
        self.title = title
        self.footer_hint = footer_hint
        self._prompt = prompt
        self._editor = LineEditor(default)
        self._validate = validate
        self._help = help_text
        self._password = password
        self._byte_limit = byte_limit
        self._error = ""

    @property
    def dialog_width(self) -> int:
        """Natural outer width: the frame sizes the box to its content (refer to ButtonDialog).

        The width is the widest of these: the prompt, the title, the hint, the footer, and the
        current text of the field, plus a comfortable minimum for the field. Thus a short
        prompt is a tidy box, not a banner across the terminal. The compositor still limits
        this width to the width that is available.
        """
        # A byte gauge sits at the right edge of the field. Thus the field lane must have space
        # for the text, its cursor, and the counter ("150/150" ≈ 8 cells), so that they do not
        # collide.
        field_slack = 12 if self._byte_limit is not None else 4
        inner = max(
            cell_len(self._prompt),
            cell_len(self.title),
            cell_len(self._help),
            cell_len(self.footer_hint),
            cell_len(self._editor.text) + field_slack,
            36,  # a comfortable minimum, so that a short field is not too narrow
        )
        return inner + 8  # panel padding + border, plus horizontal space to spare

    def _used_bytes(self) -> int:
        """The UTF-8 byte length of the field: the value that counts against ``byte_limit``."""
        return len(self._editor.text.encode("utf-8"))

    def render_body(self, width: int) -> list[str]:
        """Render the optional prompt, the field (and its byte gauge), the help, and an error."""
        parts: list[RenderableType] = []
        if self._prompt:
            parts.append(Text(self._prompt))
            parts.append(Text(""))
        field = self._editor.render(mask=self._password)
        if self._byte_limit is not None:
            # The gauge pins to the right edge of the field: a steady fuel gauge in the corner,
            # exactly as the chat compose bar draws it (both through ``byte_counter``).
            field = right_aligned_tail(
                field, byte_counter(self._used_bytes(), self._byte_limit), width
            )
        parts.append(field)
        if self._help:
            parts.append(Text(self._help, style="muted"))
        if self._error:
            parts.append(Text(self._error, style="err"))
        return render_lines(Group(*parts), width)

    def handle(self, action: str, data: str = "") -> None:
        """Edit the buffer, submit on Enter (if valid and within budget), or cancel on Esc."""
        if action == "enter":
            value = self._editor.text
            if self._byte_limit is not None:
                over = self._used_bytes() - self._byte_limit
                if over > 0:
                    plural = "s" if over != 1 else ""
                    self._error = f"Too long by {over} byte{plural} — trim to send."
                    return
            if self._validate is not None:
                result = self._validate(value)
                if result is not True:
                    self._error = str(result)
                    return
            self.resolve(value)
        elif action == "escape":
            super().handle("escape")
        else:
            if self._editor.edit(action, data):
                self._error = ""


class PinDialog(_KeylessDialog):
    """A startup dialog that gets a Bluetooth pairing PIN, and asks again after a rejected code.

    The device picker shows it when a selected companion answers the scan, but refuses the
    GATT connection until it is bonded. It shows a masked field, centred in its own box with a
    border, under the wordmark. This is the same chromeless-splash presentation as the picker
    and its spinner. Thus the dialog looks like one more step of the startup flow, not like a
    change of context.

    It only gets a PIN. To check the PIN, MeshTerm must open the BLE connection, and the
    picker does that. Thus this dialog does not find a rejected code. The picker catches the
    authentication failure and opens this dialog again with ``error`` set. For this reason,
    the field is empty each time. It resolves with the entered PIN, or with :data:`CANCEL` on
    Esc (the user stopped, and the picker returns the user to the device list).
    """

    footer_hint = "Enter connect · Esc cancel"

    #: A MeshCore pairing PIN has exactly six digits. Thus the field is limited to six
    #: characters, and drawn as six slots (typed digits as bullets, blanks as centre dots).
    PIN_LENGTH = 6

    def __init__(self, device_name: str, *, error: str = "", help_text: str = "") -> None:
        """Build the PIN dialog.

        Args:
            device_name: The display name of the companion, which the prompt includes.
            error: A message in the error style. The picker sets it when it asks again after
                a rejected PIN. It is empty on the first ask.
            help_text: A muted hint under the field (for example, where to read the code).
        """
        super().__init__()
        self.title = "Bluetooth PIN required"
        self._device = device_name
        self._error = error
        self._help = help_text
        self._editor = LineEditor("", max_length=self.PIN_LENGTH)

    def render_body(self, width: int) -> list[str]:
        """Render the prompt, the six-slot PIN field, any hint, and a rejected-PIN error."""
        prompt = Text()
        prompt.append(self._device, style="brand")
        prompt.append(" needs a pairing PIN to connect.")
        field = self._editor.render(slots=self.PIN_LENGTH)
        parts: list[RenderableType] = [prompt, Text(""), field]
        if self._help:
            parts.append(Text(self._help, style="muted"))
        if self._error:
            parts.append(Text(self._error, style="err"))
        return render_lines(Group(*parts), width)

    def handle(self, action: str, data: str = "") -> None:
        """Edit the field, submit a non-empty PIN on Enter, or cancel on Esc."""
        if action == "enter":
            pin = self._editor.text.strip()
            if not pin:  # an empty PIN cannot be correct: tell the user, not a useless retry
                self._error = "Enter the PIN, or press Esc to cancel."
                return
            self.resolve(pin)
        elif action == "escape":
            super().handle("escape")
        elif self._editor.edit(action, data):
            self._error = ""


class ConfirmScreen(_KeylessDialog):
    """A yes/no prompt. Resolves with a bool, or CANCEL on Esc."""

    def __init__(
        self,
        title: str,
        *,
        default: bool = True,
        # The ButtonDialog hint, word for word: a yes/no prompt is a button row like all
        # others. The row is what is different, not the keys. The hint does not name the y/n
        # accelerators. The Yes/No labels are their own hint, and when the hint named them,
        # the Esc verb lost its cells in a box of PicoCalc width.
        footer_hint: str = "←→ choose · Enter select · Esc cancel",
    ) -> None:
        """Build a confirm prompt.

        Args:
            title: The yes/no question.
            default: The answer that has the highlight at the start.
            footer_hint: The key hint in the footer.
        """
        super().__init__()
        self.title = title
        self.footer_hint = footer_hint
        self._value = default

    @property
    def dialog_width(self) -> int:
        """Natural outer width, so that the box fits the question and does not become wide."""
        inner = max(
            cell_len(self.title),
            cell_len(self.footer_hint),
            len("  Yes      No  "),
        )
        return inner + 8

    def render_body(self, width: int) -> list[str]:
        """Render the Yes / No options with the current choice highlighted."""
        text = Text()
        text.append("  Yes  ", style="selected" if self._value else "muted")
        text.append("   ")
        text.append("  No  ", style="muted" if self._value else "selected")
        return render_lines(text, width)

    def handle(self, action: str, data: str = "") -> None:
        """Toggle the choice, submit on Enter, or cancel on Esc."""
        if action in ("left", "right", "tab"):
            self._value = not self._value
        elif action == "text" and data.lower() in ("y", "n"):
            self._value = data.lower() == "y"
        elif action == "enter":
            self.resolve(self._value)
        elif action == "escape":
            super().handle("escape")


class ButtonDialog(_KeylessDialog):
    """A centred dialog: a prompt above a row of buttons, side by side.

    A prompt that can be used again, to select one item. The buttons are in a row. ←/→ (or
    Tab) move the highlight, Enter commits the highlighted button, and Esc cancels. Optional
    single-key shortcuts commit a button immediately (for example ``y``/``n``, which the
    ``Yes``/``No`` labels hint themselves). The highlight and border colours are parameters,
    so that a caller can theme the dialog (for example, a destructive action in red). It
    resolves with the value of the selected button, or CANCEL on Esc.
    """

    def __init__(
        self,
        prompt: str | Text,
        buttons: list[tuple[str, object]],
        *,
        title: str = "",
        default: int = 0,
        keys: dict[str, object] | None = None,
        footer_hint: str = "←→ choose · Enter select · Esc cancel",
        prompt_style: str = "",
        button_style: str = "selected",
        button_idle_style: str = "muted",
        border_style: str = "accent",
        lane: Sequence[FPair | None] | None = None,
    ) -> None:
        """Build a button dialog.

        Args:
            prompt: The question above the buttons. A plain string gets the style
                ``prompt_style``. A :class:`Text` that already has its styles (possibly on
                more than one line) is rendered as it is. Thus a caller can float output with
                rich marks, and the colour is not lost. An example is the outcome lines
                "[ok]✓[/ok] …" of the message dialog.
            buttons: ``(label, value)`` pairs laid out left to right.
            title: An optional heading for the dialog.
            default: The index of the button that has the highlight at the start.
            keys: An optional map from a lowercase shortcut character to the value that it
                commits immediately (without the highlight), for example
                ``{"y": True, "n": False}``.
            footer_hint: The key hint in the footer (it names Enter and Esc, as the other
                dialogs do).
            prompt_style: The Rich style for the question (for example ``"warn"`` for an
                action with a risk). Empty for the default foreground.
            button_style: The Rich style for the highlighted button.
            button_idle_style: The Rich style for the buttons without the highlight.
            border_style: The Rich style for the border of the dialog (the frame
                compositor reads it).
            lane: An F-key lane for the one question that has a chip to offer: the quit
                confirm. Its F3 is the chip that asked the question, pressed again (refer to
                ``menu._confirm_quit``). ``None`` keeps the empty lane of a prompt.
        """
        super().__init__()
        self._lane = tuple(lane) if lane is not None else None
        self.title = title
        self.footer_hint = footer_hint
        self.border_style = border_style
        self._prompt = prompt
        self._buttons = buttons
        self._index = default if 0 <= default < len(buttons) else 0
        self._keys = {k.lower(): v for k, v in (keys or {}).items()}
        self._prompt_style = prompt_style
        self._button_style = button_style
        self._button_idle_style = button_idle_style

    def _prompt_lines(self) -> list[Text]:
        """The prompt as one styled :class:`Text` for each line.

        A plain-string prompt becomes a single line in ``prompt_style``. A :class:`Text`
        prompt keeps its own spans, and is split on newlines, so that each line can be
        centred alone. (If the block is centred as a whole, each line moves to the margin of
        the longest line.)
        """
        if isinstance(self._prompt, Text):
            return list(self._prompt.split("\n")) or [Text("")]
        return [Text(self._prompt, style=self._prompt_style)]

    @property
    def picocalc_lyra_lane(self):
        """The lane of the caller when the caller gave one, or else the empty lane of a prompt.

        This is the definition for one deck, as on all screens. The Cardputer deck follows it.
        """
        return self._lane if self._lane is not None else super().picocalc_lyra_lane

    @property
    def _button_row_width(self) -> int:
        """The width in cells of the button row (``  Label  `` chips joined by 4-space gaps)."""
        cells = sum(cell_len(label) + 4 for label, _ in self._buttons)
        gaps = 4 * max(0, len(self._buttons) - 1)
        return cells + gaps

    @property
    def dialog_width(self) -> int:
        """Natural outer width: the frame sizes the box to its content, not to the terminal.

        The width is the widest of these: the prompt lines, the button row, the title, and
        the footer hint. Then a comfortable margin and the border are added. Thus a short
        confirm is a tidy box, not a banner across the terminal. The compositor still limits
        this width to the terminal.
        """
        inner = max(
            max(line.cell_len for line in self._prompt_lines()),
            self._button_row_width,
            cell_len(self.title),
            cell_len(self.footer_hint),
        )
        return inner + 12  # panel padding + border, plus horizontal space to spare

    def render_body(self, width: int) -> list[str]:
        """Render the prompt lines, centred above a centred row of buttons."""
        row = Text()
        for i, (label, _value) in enumerate(self._buttons):
            if i:
                row.append("    ")
            style = self._button_style if i == self._index else self._button_idle_style
            row.append(f"  {label}  ", style=style)
        parts = [_center(line, width) for line in self._prompt_lines()]
        return render_lines(Group(*parts, Text(""), _center(row, width)), width)

    def handle(self, action: str, data: str = "") -> None:
        """Move the highlight, commit on Enter or a shortcut key, or cancel on Esc."""
        if action in ("left", "right") and self._buttons:
            # ←→ clamp, like all other highlights that the app moves with a directional pair.
            # A highlight that jumps from one end to the other is the one move that looks like
            # a change of the screen, instead of one step.
            step = -1 if action == "left" else 1
            self._index = max(0, min(len(self._buttons) - 1, self._index + step))
        elif action == "tab" and self._buttons:
            # Tab is the exception that the rule permits: a forward-only key with no reverse
            # key of its own. Thus it cycles, or else it stops on the last chip with no way on.
            self._index = (self._index + 1) % len(self._buttons)
        elif action == "text" and data.lower() in self._keys:
            self.resolve(self._keys[data.lower()])
        elif action == "enter" and self._buttons:
            self.resolve(self._buttons[self._index][1])
        elif action == "escape":
            super().handle("escape")


class CountdownDialog(_KeylessDialog):
    """A wait that you can watch and leave: the seconds that remain, above one Cancel chip.

    It shows while a cooldown holds back a transmission (refer to
    :func:`meshterm.ui.cooldown.wait_for_cooldown`). A wait that is long enough to notice
    must tell why it waits, and must give a way out. A frozen screen tells neither. On a
    radio app, a frozen screen looks like a lost connection, not like courtesy.

    The dialog resolves itself. The caller counts it down with :meth:`set_remaining`, and
    the dialog resolves ``True`` when the clock gets to zero. Esc (or Enter on its Cancel
    chip) resolves CANCEL instead, and the caller abandons the thing that waited.

    One chip, not two: there is nothing to select between, only something to abandon. Thus
    the dialog uses the lone-button hint shape (no ``←→``), with Cancel as its verb.
    """

    def __init__(self, action: str, remaining: float, *, reason: str = "") -> None:
        """Build a countdown over one Cancel chip.

        Args:
            action: The thing that waits, named as the thing that will occur
                ("Flood advert").
            remaining: The seconds that remain when the dialog opens.
            reason: One muted line under the clock that tells why the wait is necessary.
                Empty for no line.
        """
        super().__init__()
        self.title = action
        # Both keys do the one thing that this dialog offers. Thus they share an atom,
        # instead of one atom each that says "cancel" (the shared-key rule of the hint line).
        self.footer_hint = "Enter/Esc cancel"
        self.border_style = "warn"
        self._remaining = max(0.0, remaining)
        self._reason = reason

    def set_remaining(self, remaining: float) -> None:
        """Set the clock, and resolve the dialog when the clock gets to zero.

        To resolve two times causes no problem: :meth:`~meshterm.ui.tui.screen.Screen.resolve`
        ignores a second result. Thus the tick never has to know if Esc came first.
        """
        self._remaining = max(0.0, remaining)
        if self._remaining <= 0:
            self.resolve(True)

    @property
    def dialog_width(self) -> int:
        """Natural outer width, sized for the widest line that the dialog will ever draw.

        The width is measured against the clock at the opening, not the current clock. Thus
        the box does not become one cell narrower when the seconds go from 10 to 9. A dialog
        that changes size while you read it is harder to read than a dialog that is a little
        wide.
        """
        inner = max(
            cell_len(self.title),
            cell_len(self.footer_hint),
            cell_len(self._clock_text(self._remaining)),
            cell_len(self._reason),
            len("  Cancel  "),
        )
        return inner + 12  # panel padding + border, plus space to spare (refer to ButtonDialog)

    @staticmethod
    def _clock_text(remaining: float) -> str:
        """The clock line: whole seconds, counted as a person says them."""
        seconds = max(0, int(remaining + 0.999))  # 0.2s left still shows as "1s"
        return f"Ready in {seconds}s"

    def render_body(self, width: int) -> list[str]:
        """Draw the clock and its reason, centred above a centred Cancel chip.

        Each line is centred alone, as :class:`ButtonDialog` and :class:`ProgressDialog` draw
        their lines. A chip is a chip in all places. A chip against the left border looked
        like a late addition, next to the centred rows of the other dialogs.
        """
        parts: list[Text] = [_center(Text(self._clock_text(self._remaining), style="warn"), width)]
        if self._reason:
            parts.append(_center(Text(self._reason, style="muted"), width))
        parts.append(Text(""))
        parts.append(_center(Text("  Cancel  ", style="selected"), width))
        return render_lines(Group(*parts), width)

    def handle(self, action: str, data: str = "") -> None:
        """Enter and Esc both abandon: the only chip on the dialog is Cancel."""
        if action in ("enter", "escape"):
            super().handle("escape")


class TypedConfirmDialog(_KeylessDialog):
    """A gate for a destructive action: the user must type a confirmation word to continue.

    A centred dialog with the error theme, for the actions that a stray Enter must never
    start (factory reset, identity overwrite). It shows the warning, then a field in which
    the user must type the exact confirmation word. Enter commits only when the field
    matches. All other text shows a short message and keeps the dialog open. Esc always
    leaves. The match ignores case, so that a forgotten CapsLock does not look like
    hesitation. The effort that the dialog asks for is to type the word, not its case. It
    resolves ``True`` on a match, or :data:`~meshterm.ui.tui.screen.CANCEL` on Esc (never
    ``False``).
    """

    border_style = "err"
    footer_hint = "Enter confirm · Esc cancel"

    def __init__(self, warning: str, word: str, *, title: str = "Are you sure?") -> None:
        """Build the typed-confirmation dialog.

        Args:
            warning: The result, in full words (for example "This erases ALL data …").
            word: The word that the user must type to confirm (for example ``"RESET"``).
            title: The heading of the dialog.
        """
        super().__init__()
        self.title = title
        self._warning = warning
        self._word = word
        self._editor = LineEditor("")
        self._error = ""

    @property
    def dialog_width(self) -> int:
        """Natural outer width: the frame sizes the box to its content (refer to ButtonDialog)."""
        inner = max(
            cell_len(self._warning),
            cell_len(self.title),
            cell_len(self.footer_hint),
            cell_len(self._ask),
        )
        return min(inner, 72) + 12  # panel padding + border, plus space to spare

    @property
    def _ask(self) -> str:
        """The instruction line that names the word to type."""
        return f"Type {self._word} to confirm:"

    def render_body(self, width: int) -> list[str]:
        """Render the warning, the instruction, the typed field, and a mismatch message."""
        parts: list[RenderableType] = [
            Text(self._warning, style="err"),
            Text(""),
            Text(self._ask, style="muted"),
            self._editor.render(),
        ]
        if self._error:
            parts.append(Text(self._error, style="err"))
        return render_lines(Group(*parts), width)

    def handle(self, action: str, data: str = "") -> None:
        """Edit the field, confirm on an exact match, or cancel on Esc."""
        if action == "enter":
            if self._editor.text.strip().lower() == self._word.lower():
                self.resolve(True)
            else:
                self._error = f"That doesn't match — type {self._word}, or press Esc to cancel."
            return
        if action == "escape":
            super().handle("escape")
            return
        if self._editor.edit(action, data):
            self._error = ""


class ReconnectDialog(_KeylessDialog):
    """A centred dialog that shows when the link to the companion goes down during a session.

    An animated spinner and a message are above a single ``Abort`` button. In this dialog,
    there is nothing to select, as there is in the other dialogs. The app watches for the
    device to come back, and closes this dialog itself (it resolves its future) when the
    reconnect is successful. Thus the only action of the user is to stop and abort.

    Only Enter resolves ``"quit"``. Esc does nothing, on purpose: a user who presses Esc
    from habit cannot lose the session while a replug may be only one second away. The
    button uses the shared ``selected`` chip style (a grey fill), the same fill as
    the quit-confirmation dialog. Thus all dialogs look the same. The animation is the
    reusable :class:`~meshterm.ui.tui.spinner.Spinner`, which :meth:`tick` moves forward
    from the animation timer of the session.

    When the device is back but the reconnect continues to fail, the reason shows under the
    spinner line (:meth:`set_detail`). For example, another program took the port while we
    waited, or a pairing became stale. Without the reason, a spinner alone waits on the
    problem forever.
    """

    #: The maximum width of a reason before it wraps: one or two sentences at a width that is
    #: easy to read, instead of a box as wide as the terminal that holds it on one line.
    DETAIL_MEASURE = 56

    def __init__(
        self,
        message: str,
        *,
        title: str = "Device disconnected",
        footer_hint: str = "Enter abort",
        prompt_style: str = "warn",
        border_style: str = "warn",
    ) -> None:
        """Build the reconnect dialog.

        Args:
            message: The line next to the spinner (for example "Waiting for your device…").
            title: The heading of the dialog.
            footer_hint: The key hint in the footer (only Abort is offered).
            prompt_style: The Rich style for the message (``"warn"`` for a tone of caution).
            border_style: The Rich style for the border of the dialog (the frame
                compositor reads it).
        """
        super().__init__()
        self.title = title
        self.footer_hint = footer_hint
        self.border_style = border_style
        self._message = message
        self._prompt_style = prompt_style
        self._spinner = Spinner()
        self._detail: Text | None = None

    @property
    def dialog_width(self) -> int:
        """Natural outer width: the frame sizes the box to its content (refer to ButtonDialog)."""
        detail = 0
        if self._detail is not None:
            widest = max(cell_len(line.plain) for line in self._detail.split("\n"))
            detail = min(widest, self.DETAIL_MEASURE)
        inner = max(
            cell_len(self._message),
            cell_len(self.title),
            cell_len(self.footer_hint),
            len("  Abort  "),
            detail,
        )
        return inner + 12  # panel padding + border, plus horizontal space to spare

    @property
    def message(self) -> str:
        """The line next to the spinner."""
        return self._message

    def set_message(self, message: str) -> None:
        """Replace the line next to the spinner (for example, to tell of a stalled retry)."""
        self._message = message

    def set_detail(self, detail: Text | None) -> None:
        """Show why the reconnect continues to fail, under the spinner line. ``None`` clears it."""
        self._detail = detail

    def tick(self) -> None:
        """Move the spinner to its next animation step (the session's animation timer calls it)."""
        self._spinner.tick()

    def render_body(self, width: int) -> list[str]:
        """Render the spinner and the message, then a reason, centred above an Abort button."""
        line = self._spinner.text()
        line.append("  ")
        line.append(self._message, style=self._prompt_style)
        row = Text()
        row.append("  Abort  ", style="selected")
        parts: list[Text] = [_center(line, width)]
        if self._detail is not None:
            # Wrapped and centred line by line. It holds no chip, so the centring of Rich
            # itself is safe here (refer to _center for the one case where it is not safe).
            detail = self._detail.copy()
            detail.justify = "center"
            parts += [Text(""), detail]
        parts += [Text(""), _center(row, width)]
        return render_lines(Group(*parts), width)

    def handle(self, action: str, data: str = "") -> None:
        """Abort only on Enter. Ignore all other keys, also Esc (the app closes the dialog)."""
        if action == "enter":
            self.resolve("quit")


class AutocompleteScreen(_KeylessDialog):
    """A free-text field with a live suggestion list (``questionary.autocomplete``).

    The user can type any value. The suggestions that match appear below the field. The
    user can move the highlight to one of them with the arrow keys, and accept it into the
    field with Tab. Enter commits the typed text (after validation). It resolves with the
    string, or CANCEL on Esc.
    """

    #: The limit of the suggestion list, so that the list never fills most of the dialog.
    MAX_SUGGESTIONS = 8
    #: Home and End move the caret of the field, so edge scroll does not take them.
    home_end_jumps = False

    def __init__(
        self,
        title: str,
        choices: list[str],
        *,
        prompt: str = "",
        default: str = "",
        validate: Validator | None = None,
        footer_hint: str = "↑↓ move · Tab complete · Enter accept · Esc cancel",
    ) -> None:
        """Build an autocomplete prompt.

        Args:
            title: A short heading in the border of the dialog.
            choices: The suggestion strings to match against.
            prompt: The instruction in the box, above the field.
            default: Prefilled text.
            validate: An optional validator that runs on Enter.
            footer_hint: The key hint in the footer.
        """
        super().__init__()
        self.title = title
        self.footer_hint = footer_hint
        self._prompt = prompt
        self._editor = LineEditor(default)
        self._choices = choices
        self._validate = validate
        self._error = ""
        self._sugg = 0
        self._cursor: int | None = None

    @property
    def dialog_width(self) -> int:
        """Natural outer width: the box fits its prompt and suggestions, not the whole terminal."""
        widths = [
            cell_len(self._prompt),
            cell_len(self.title),
            cell_len(self.footer_hint),
            cell_len(self._editor.text) + 4,
            36,
        ]
        widths.extend(cell_len(c) + 2 for c in self._choices[: self.MAX_SUGGESTIONS])
        return max(widths) + 8

    def _suggestions(self) -> list[str]:
        """Return the suggestions that match the current text, up to the maximum that shows."""
        needle = self._editor.text.lower()
        if not needle:
            matches = list(self._choices)
        else:
            matches = [c for c in self._choices if needle in c.lower()]
        return matches[: self.MAX_SUGGESTIONS]

    def render_body(self, width: int) -> list[str]:
        """Render the optional prompt, the field, the matching suggestions, and any error.

        The suggestions are drawn one row at a time, so that the body line of the
        highlighted suggestion is known (refer to :meth:`cursor_line`).
        """
        parts: list[RenderableType] = []
        if self._prompt:
            parts.append(Text(self._prompt))
            parts.append(Text(""))
        parts.append(self._editor.render())
        lines = render_lines(Group(*parts), width)
        suggestions = self._suggestions()
        self._sugg = max(0, min(self._sugg, len(suggestions) - 1)) if suggestions else 0
        self._cursor = None
        for i, sug in enumerate(suggestions):
            is_sel = i == self._sugg
            row = Text(("❯ " if is_sel else "  ") + sug, style="cursor" if is_sel else "muted")
            row.truncate(width)
            if is_sel:
                self._cursor = len(lines)
            lines.extend(render_lines(row, width))
        if self._error:
            lines.extend(render_lines(Text(self._error, style="err"), width))
        return lines

    def cursor_line(self) -> int | None:
        """The body line of the highlighted suggestion, kept visible in a box too short for all."""
        return self._cursor

    def handle(self, action: str, data: str = "") -> None:
        """Edit the field, move or accept suggestions, submit on Enter, or cancel on Esc."""
        suggestions = self._suggestions()
        if action == "up":
            self._sugg = max(0, self._sugg - 1) if suggestions else 0
        elif action == "down":
            self._sugg = min(len(suggestions) - 1, self._sugg + 1) if suggestions else 0
        elif action == "tab":
            if suggestions:
                self._editor = LineEditor(suggestions[self._sugg])
                self._error = ""
        elif action == "enter":
            value = self._editor.text
            if self._validate is not None:
                result = self._validate(value)
                if result is not True:
                    self._error = str(result)
                    return
            self.resolve(value)
        elif action == "escape":
            super().handle("escape")
        else:
            if self._editor.edit(action, data):
                self._error = ""
                self._sugg = 0
