# SPDX-License-Identifier: Apache-2.0
"""Input screens: text, confirm, and autocomplete — the ``questionary`` replacements.

These reimplement the small slice of ``questionary`` the app uses (a validated line editor,
a yes/no toggle, and a free-text field with suggestions) as uniform TUI screens, so every
prompt shares the framework's look, layering, resize, and Esc-to-cancel behavior.
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

#: A validator returns ``True`` when the input is acceptable, or an error message to show.
Validator = Callable[[str], "bool | str"]

#: MeshCore's LoRa payload caps a direct message at 150 UTF-8 bytes and an (unscoped)
#: channel broadcast at 130; past the cap the companion silently drops the packet. Every
#: compose bar that feeds one of those sends — the chat input, the courier's message entry —
#: counts bytes against these and blocks over the limit rather than losing the packet.
DM_BYTE_LIMIT = 150
CHANNEL_BYTE_LIMIT = 130

#: Byte-counter thresholds (bytes *remaining*) at which its colour escalates, plus the two
#: mid-band hues. The theme's ``warn``/``err`` sit too close together (an amber that reads
#: orange, then red), so the counter names a truer yellow and orange directly to keep the
#: green→yellow→orange→red fuel gauge visibly stepped.
_BYTES_TIGHT, _BYTES_LOW = 20, 10
_BYTES_YELLOW = "bold #fde047"
_BYTES_ORANGE = "bold #ff9500"


def byte_style(remaining: int) -> str:
    """Map bytes remaining to the counter's escalating colour (green→yellow→orange→red)."""
    if remaining <= 0:
        return "err"  # at or over the limit — the send is blocked
    if remaining <= _BYTES_LOW:
        return _BYTES_ORANGE
    if remaining <= _BYTES_TIGHT:
        return _BYTES_YELLOW
    return "ok"


def byte_counter(used: int, limit: int) -> Text:
    """The inline ``used/limit`` budget; only ``used`` is coloured by how much is left.

    Green with room to spare, yellow within :data:`_BYTES_TIGHT` bytes, orange within
    :data:`_BYTES_LOW`, and red once the limit is met or exceeded — so the number reads as a
    fuel gauge while the ``/limit`` suffix stays muted (it never changes). The shared gauge
    the chat compose bar and the courier's message field both draw.
    """
    counter = Text()
    counter.append(str(used), style=byte_style(limit - used))
    counter.append(f"/{limit}", style="muted")
    return counter


def _center(content: Text, width: int) -> Text:
    """Center ``content`` in ``width`` with plain padding on both sides.

    Used instead of ``Text(justify="center")`` for lines that hold a reverse-video button
    chip: Rich strips a styled span's *trailing* whitespace when it sits directly against
    justify padding, which would shave the right edge off the chip (``"  Quit"`` instead of
    ``"  Quit  "``) and leave the label hugging the left of the fill. Padding manually keeps
    a real segment after the chip, so its trailing cells survive and the fill stays symmetric.
    """
    pad = max(0, width - content.cell_len)
    left = pad // 2
    line = Text(" " * left)
    line.append_text(content)
    line.append(" " * (pad - left))
    return line


class LineEditor:
    """A minimal single-line text editor (insert, delete, and cursor movement).

    Shared by :class:`TextScreen` and :class:`AutocompleteScreen`. Tracks the text and the
    cursor position; rendering shows the cursor as a reverse-video cell.
    """

    def __init__(self, initial: str = "", *, max_length: int | None = None) -> None:
        """Start the editor with ``initial`` text and the cursor at its end.

        Args:
            initial: The starting buffer contents.
            max_length: Optional hard cap on the number of characters; further insertions are
                dropped (or a paste truncated to fit). ``None`` leaves the length unbounded.
        """
        self.text = initial
        self.cursor = len(initial)
        self._max_length = max_length

    def edit(self, action: str, data: str = "") -> bool:
        """Apply an editing action, returning ``True`` if it changed the buffer/cursor.

        Args:
            action: The normalized key action.
            data: The character to insert when ``action`` is ``text`` — or the whole run
                pasted when ``action`` is ``paste``.

        Returns:
            ``True`` if the action was an editing action handled here.
        """
        if action == "paste":
            # A paste arrives as one multi-character run. A single-line field can't hold
            # newlines or control characters, so fold them to spaces, then insert the run
            # exactly like typed text (the max-length trim below still applies).
            data = "".join(ch if ch.isprintable() else " " for ch in data)
            if not data:
                return False
            action = "text"
        if action == "text" and data.isprintable():
            if self._max_length is not None:
                room = self._max_length - len(self.text)
                if room <= 0:
                    return False  # at capacity — swallow the key without changing the buffer
                data = data[:room]  # a multi-char paste fills only the remaining slots
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
        """Index of the start of the word at/left of ``pos`` (the previous word if already there).

        Skips any whitespace immediately left of the cursor, then the run of word characters, so
        from mid-word it lands on that word's first character and from a word start it steps back
        to the previous word — the usual Ctrl+Left behavior of a text editor.
        """
        i = pos
        while i > 0 and self.text[i - 1].isspace():
            i -= 1
        while i > 0 and not self.text[i - 1].isspace():
            i -= 1
        return i

    def _word_right(self, pos: int) -> int:
        """Index of the start of the next word after ``pos`` (or the line end if none remains).

        Skips the current run of word characters, then the whitespace after it, landing on the
        first character of the following word — the usual Ctrl+Right behavior.
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
            overflow_at: Character index at which the text spills past a byte budget; that
                character and everything after it are shown in the error style so the user
                can see exactly what to trim. ``None`` (the default) styles the line plainly.
            slots: When set, render exactly this many fixed positions (a masked PIN field):
                typed positions show a bullet, still-blank ones a muted centre dot. Takes
                precedence over ``mask``/``overflow_at``.
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

        Used for PIN entry — each of ``slots`` positions shows a bullet (``•``) once typed and
        a muted centre dot (``·``) while still blank, spaced apart so the field reads as a row
        of PIN boxes rather than a growing line. The cursor position is drawn reverse-video.
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
    """A dialog with no F-key lane, because it has nothing a lane could carry.

    Every prompt in this module is answered with Enter, Esc, the arrows, and typing —
    keys that sit right under the reader's hands on both platforms. None of them scroll,
    so the shared pager (which the base :class:`~meshterm.ui.tui.screen.Screen` hands out)
    would name two keys that do nothing here. The lane's standing rule is to leave a slot
    **empty** rather than dim when the action is not a thing on this screen *at all* —
    dim is for a thing that just isn't available this paint — so these dialogs draw the
    bare key numbers (see :mod:`~meshterm.ui.tui.fkeys`).

    Every one of them is also :attr:`~meshterm.ui.tui.screen.Screen.modal`: a prompt is an
    unanswered question, so the pop-all key (^W) declines to unwind past it and the reader
    answers or Escs out first.
    """

    modal = True

    @property
    def fkey_lane(self):
        """No slots: nothing on a prompt pages, jumps, or needs promoting off a chord."""
        from .fkeys import EMPTY_LANE

        return EMPTY_LANE


class TextScreen(_KeylessDialog):
    """A validated single-line text prompt. Resolves with the string, or CANCEL on Esc."""

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
            title: Short heading shown in the dialog's border.
            prompt: The question/instruction shown inside the box, above the field. Keep
                the ``title`` short and put the detail here, so the popup reads like the
                button dialogs (a prompt above its controls) rather than a lone field.
            default: Prefilled text.
            validate: Optional validator run on Enter; a returned string is shown as an
                error and blocks submission.
            help_text: Optional muted hint shown under the field.
            password: When ``True``, mask the entered text.
            byte_limit: When set, the field carries the shared UTF-8 byte gauge
                (:func:`byte_counter`) pinned bottom-right and blocks submission once the
                text exceeds it — for a field that feeds a size-capped packet (a direct
                message, a courier entry). Left ``None`` for an unbounded field.
            footer_hint: Footer key hint.
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
        """Natural outer width so the frame sizes the box to its content (see ButtonDialog).

        The widest of the prompt, title, hint, footer, and the field's current text, plus a
        comfortable field minimum — so a short prompt is a tidy box, not a banner stretched
        across the terminal. The compositor still caps this to the width available.
        """
        # A byte gauge rides the field's right edge, so the field lane needs room for the
        # text, its cursor, and the counter ("150/150" ≈ 8 cells) without them colliding.
        field_slack = 12 if self._byte_limit is not None else 4
        inner = max(
            cell_len(self._prompt),
            cell_len(self.title),
            cell_len(self._help),
            cell_len(self.footer_hint),
            cell_len(self._editor.text) + field_slack,
            36,  # a comfortable minimum so a short field isn't a cramped sliver
        )
        return inner + 8  # panel padding + border, plus horizontal breathing room

    def _used_bytes(self) -> int:
        """UTF-8 byte length of the field — what counts against ``byte_limit``."""
        return len(self._editor.text.encode("utf-8"))

    def render_body(self, width: int) -> list[str]:
        """Render the optional prompt, the field (+ byte gauge), help, and any error."""
        parts: list[RenderableType] = []
        if self._prompt:
            parts.append(Text(self._prompt))
            parts.append(Text(""))
        field = self._editor.render(mask=self._password)
        if self._byte_limit is not None:
            # The gauge pins to the field's right edge — a steady fuel gauge in the corner,
            # exactly as the chat compose bar draws it (both via ``byte_counter``).
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
    """A startup popup that collects a Bluetooth pairing PIN, re-asking on a rejected code.

    Shown by the device picker when a chosen companion answers the scan but refuses the GATT
    connection until it is bonded. It presents a masked field centered in its own bordered box
    under the wordmark — the same chromeless-splash presentation as the picker and its spinner
    — so it reads as one more step of the startup flow rather than a context switch.

    It only *collects* a PIN; verifying it means actually opening the BLE connection, which the
    picker does. So a rejected code isn't detected here — the picker catches the authentication
    failure and re-opens this dialog with ``error`` set, which is why the field starts empty each
    time. Resolves with the entered PIN, or :data:`CANCEL` on Esc (the user gave up — the picker
    returns them to the device list).
    """

    footer_hint = "Enter connect · Esc cancel"

    #: MeshCore pairing PINs are a fixed six digits, so the field is capped at six characters
    #: and drawn as six slots (typed digits as bullets, blanks as centre dots).
    PIN_LENGTH = 6

    def __init__(self, device_name: str, *, error: str = "", help_text: str = "") -> None:
        """Build the PIN dialog.

        Args:
            device_name: The companion's display name, woven into the prompt.
            error: A message shown in the error style — set by the picker on a re-ask after a
                rejected PIN; empty on the first ask.
            help_text: A muted hint under the field (e.g. where to read the code).
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
            if not pin:  # an empty PIN can't be right — nudge rather than pointlessly retry
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
        # The ButtonDialog hint verbatim: a yes/no prompt is a button row like any other,
        # and the row is what differs, not the keys. The y/n accelerators stay unwritten —
        # the Yes/No labels are their own hint, and naming them cost the Esc verb its
        # cells inside a PicoCalc-width box.
        footer_hint: str = "←→ choose · Enter select · Esc cancel",
    ) -> None:
        """Build a confirm prompt.

        Args:
            title: The yes/no question.
            default: The initially highlighted answer.
            footer_hint: Footer key hint.
        """
        super().__init__()
        self.title = title
        self.footer_hint = footer_hint
        self._value = default

    @property
    def dialog_width(self) -> int:
        """Natural outer width so the box hugs the question rather than stretching wide."""
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
    """A centered dialog: a prompt above a row of side-by-side buttons.

    A reusable choose-one prompt. The buttons sit in a row; ←/→ (or Tab) move the highlight,
    Enter commits the highlighted button, and Esc cancels. Optional single-key shortcuts
    commit a button instantly (e.g. ``y``/``n``, self-hinted by ``Yes``/``No`` labels). The
    highlight and border colours are parametrised so a caller can theme it (a destructive
    action in red, say). Resolves with the chosen button's value, or CANCEL on Esc.
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
            prompt: The question shown above the buttons. A plain string is styled with
                ``prompt_style``; a pre-styled (possibly multi-line) :class:`Text` is
                rendered as-is, so a caller can float richly-marked output — e.g. the
                message dialog's "[ok]✓[/ok] …" outcome lines — without losing colour.
            buttons: ``(label, value)`` pairs laid out left to right.
            title: Optional dialog heading.
            default: Index of the initially highlighted button.
            keys: Optional map of a lowercase shortcut character to the value it commits
                immediately (bypassing the highlight), e.g. ``{"y": True, "n": False}``.
            footer_hint: Footer key hint (hints Enter and Esc, as the other dialogs do).
            prompt_style: Rich style for the question (e.g. ``"warn"`` for a cautionary
                action); empty for the default foreground.
            button_style: Rich style for the highlighted button.
            button_idle_style: Rich style for the un-highlighted buttons.
            border_style: Rich style for the dialog border (read by the frame compositor).
            lane: An F-key lane for the one question that has a chip worth offering — the
                quit confirm's, whose F3 is the chip that asked it, pressed again (see
                ``menu._confirm_quit``). ``None`` keeps a prompt's empty lane.
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
        """The prompt as one styled :class:`Text` per line.

        A plain-string prompt becomes a single line in ``prompt_style``; a :class:`Text`
        prompt keeps its own spans and is split on newlines so each line can be centered
        independently (centering the block as a whole would skew every line to the
        longest one's margin).
        """
        if isinstance(self._prompt, Text):
            return list(self._prompt.split("\n")) or [Text("")]
        return [Text(self._prompt, style=self._prompt_style)]

    @property
    def fkey_lane(self):
        """The caller's lane where one was given, else a prompt's empty one."""
        return self._lane if self._lane is not None else super().fkey_lane

    @property
    def _button_row_width(self) -> int:
        """Display width of the button row (``  Label  `` cells joined by 4-space gaps)."""
        cells = sum(cell_len(label) + 4 for label, _ in self._buttons)
        gaps = 4 * max(0, len(self._buttons) - 1)
        return cells + gaps

    @property
    def dialog_width(self) -> int:
        """Natural outer width so the frame sizes the box to its content, not the terminal.

        The widest of the prompt lines, button row, title, and footer hint, plus a
        comfortable margin and the border — so a short confirm reads as a tidy box rather
        than a banner stretched across the screen. The compositor still caps this to the
        terminal.
        """
        inner = max(
            max(line.cell_len for line in self._prompt_lines()),
            self._button_row_width,
            cell_len(self.title),
            cell_len(self.footer_hint),
        )
        return inner + 12  # panel padding + border, plus horizontal breathing room

    def render_body(self, width: int) -> list[str]:
        """Render the prompt line(s) centered above a centered row of buttons."""
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
            # ←→ clamp, like every other cursor the app steers with a directional pair —
            # a highlight that leaps end to end is the one move that reads as the screen
            # changing rather than as one step.
            step = -1 if action == "left" else 1
            self._index = max(0, min(len(self._buttons) - 1, self._index + step))
        elif action == "tab" and self._buttons:
            # Tab is the exception the rule allows: a forward-only key with no reverse of
            # its own, so it cycles or it dead-ends on the last chip.
            self._index = (self._index + 1) % len(self._buttons)
        elif action == "text" and data.lower() in self._keys:
            self.resolve(self._keys[data.lower()])
        elif action == "enter" and self._buttons:
            self.resolve(self._buttons[self._index][1])
        elif action == "escape":
            super().handle("escape")


class CountdownDialog(_KeylessDialog):
    """A wait you can watch, and back out of: the seconds left, over one Cancel chip.

    Shown while a cooldown holds a transmission back (see
    :func:`meshterm.ui.cooldown.wait_for_cooldown`). The point is that a wait long enough
    to notice should say *why* it is waiting and leave a way out — a frozen screen says
    neither, and on a radio app it reads as a lost connection rather than as courtesy.

    It resolves itself: the caller ticks it down with :meth:`set_remaining` and the dialog
    resolves ``True`` the moment the clock reaches zero. Esc — or Enter on its Cancel chip
    — resolves CANCEL instead, and the caller abandons whatever was waiting.

    One chip, not two: there is nothing to choose between, only something to abandon, so
    it takes the lone-button hint shape (no ``←→``) with Cancel as its verb.
    """

    def __init__(self, action: str, remaining: float, *, reason: str = "") -> None:
        """Build a countdown over one Cancel chip.

        Args:
            action: What is waiting, named as the thing that will happen ("Flood advert").
            remaining: Seconds left when the dialog opens.
            reason: One muted line under the clock saying why the wait exists; empty for
                no line at all.
        """
        super().__init__()
        self.title = action
        # Both keys do the one thing this dialog offers, so they share an atom rather
        # than each claiming "cancel" in turn (the hint line's shared-key rule).
        self.footer_hint = "Enter/Esc cancel"
        self.border_style = "warn"
        self._remaining = max(0.0, remaining)
        self._reason = reason

    def set_remaining(self, remaining: float) -> None:
        """Update the clock, resolving the dialog once it reaches zero.

        Resolving twice is harmless — :meth:`~meshterm.ui.tui.screen.Screen.resolve` drops
        a second result — so the tick never has to know whether Esc beat it to it.
        """
        self._remaining = max(0.0, remaining)
        if self._remaining <= 0:
            self.resolve(True)

    @property
    def dialog_width(self) -> int:
        """Natural outer width, sized for the widest line it will ever draw.

        Measured against the *opening* clock rather than the current one, so the box does
        not shrink a cell as the seconds tick from 10 to 9 — a dialog that resizes while
        you read it is harder to read than a slightly wide one.
        """
        inner = max(
            cell_len(self.title),
            cell_len(self.footer_hint),
            cell_len(self._clock_text(self._remaining)),
            cell_len(self._reason),
            len("  Cancel  "),
        )
        return inner + 12  # panel padding + border, plus breathing room (see ButtonDialog)

    @staticmethod
    def _clock_text(remaining: float) -> str:
        """The clock line: whole seconds, counted the way a person would say them."""
        seconds = max(0, int(remaining + 0.999))  # 0.2s left still reads as "1s"
        return f"Ready in {seconds}s"

    def render_body(self, width: int) -> list[str]:
        """Draw the clock and its reason centered above a centered Cancel chip.

        Centered line by line, the way :class:`ButtonDialog` and :class:`ProgressDialog`
        draw theirs: a chip is a chip wherever it appears, and one shoved against the left
        border read as an afterthought beside its siblings' centered rows.
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
    """A destructive-action gate: the user must type a confirmation word to proceed.

    A centered, error-themed dialog for the actions a stray Enter must never be able to
    trigger (factory reset, identity overwrite). It shows the warning, then a field the
    user has to type the exact confirmation word into; Enter commits only when the field
    matches (anything else shows a nudge and keeps the dialog up), and Esc always backs
    out. Matching is case-insensitive so a forgotten CapsLock doesn't read as hesitation —
    the friction is having to *type the word*, not its case. Resolves ``True`` on a match,
    or :data:`~meshterm.ui.tui.screen.CANCEL` on Esc (never ``False``).
    """

    border_style = "err"
    footer_hint = "Enter confirm · Esc cancel"

    def __init__(self, warning: str, word: str, *, title: str = "Are you sure?") -> None:
        """Build the typed-confirmation dialog.

        Args:
            warning: The consequence, spelled out (e.g. "This erases ALL data …").
            word: The word the user must type to confirm (e.g. ``"RESET"``).
            title: Dialog heading.
        """
        super().__init__()
        self.title = title
        self._warning = warning
        self._word = word
        self._editor = LineEditor("")
        self._error = ""

    @property
    def dialog_width(self) -> int:
        """Natural outer width so the frame sizes the box to its content (see ButtonDialog)."""
        inner = max(
            cell_len(self._warning),
            cell_len(self.title),
            cell_len(self.footer_hint),
            cell_len(self._ask),
        )
        return min(inner, 72) + 12  # panel padding + border, plus breathing room

    @property
    def _ask(self) -> str:
        """The instruction line naming the word to type."""
        return f"Type {self._word} to confirm:"

    def render_body(self, width: int) -> list[str]:
        """Render the warning, the instruction, the typed field, and any mismatch nudge."""
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
    """A centered dialog shown when the companion link drops mid-session.

    An animated spinner and message sit above a single ``Abort`` button. Unlike the other
    dialogs there is nothing to *choose*: the app watches for the device to return and
    dismisses this screen itself (resolving its future) the moment the reconnect succeeds, so
    the user's only action is to give up and abort. Only Enter resolves ``"quit"``; Esc is
    deliberately inert, so an idle habit of pressing Esc can't drop the session while a replug
    may be a second away. The button uses the shared ``selected`` highlight style (the theme's
    cyan) — the same fill as the quit-confirmation dialog — so every popup dialog reads alike.
    The animation is the reusable :class:`~meshterm.ui.tui.spinner.Spinner`, advanced by
    :meth:`tick` from the session's animation timer.
    """

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
            message: The line shown beside the spinner (e.g. "Waiting for your device…").
            title: Dialog heading.
            footer_hint: Footer key hint (only Abort is offered).
            prompt_style: Rich style for the message (``"warn"`` for the cautionary tone).
            border_style: Rich style for the dialog border (read by the frame compositor).
        """
        super().__init__()
        self.title = title
        self.footer_hint = footer_hint
        self.border_style = border_style
        self._message = message
        self._prompt_style = prompt_style
        self._spinner = Spinner()

    @property
    def dialog_width(self) -> int:
        """Natural outer width so the frame sizes the box to its content (see ButtonDialog)."""
        inner = max(
            cell_len(self._message),
            cell_len(self.title),
            cell_len(self.footer_hint),
            len("  Abort  "),
        )
        return inner + 12  # panel padding + border, plus horizontal breathing room

    def set_message(self, message: str) -> None:
        """Replace the line shown beside the spinner (e.g. to note a stalled retry)."""
        self._message = message

    def tick(self) -> None:
        """Advance the spinner to its next frame (driven by the session's animation timer)."""
        self._spinner.tick()

    def render_body(self, width: int) -> list[str]:
        """Render the spinner and message centered above a single Abort button."""
        line = self._spinner.text()
        line.append("  ")
        line.append(self._message, style=self._prompt_style)
        row = Text()
        row.append("  Abort  ", style="selected")
        return render_lines(Group(_center(line, width), Text(""), _center(row, width)), width)

    def handle(self, action: str, data: str = "") -> None:
        """Abort only on Enter; ignore everything else, Esc included (the app auto-dismisses)."""
        if action == "enter":
            self.resolve("quit")


class AutocompleteScreen(_KeylessDialog):
    """A free-text field with a live suggestion list (``questionary.autocomplete``).

    The user may type any value; matching suggestions appear below and can be highlighted
    with the arrows and accepted into the field with Tab. Enter commits the typed text
    (after validation). Resolves with the string, or CANCEL on Esc.
    """

    #: Cap the suggestion list so it never dominates the dialog.
    MAX_SUGGESTIONS = 8

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
            title: Short heading shown in the dialog's border.
            choices: Suggestion strings to match against.
            prompt: The instruction shown inside the box, above the field.
            default: Prefilled text.
            validate: Optional validator run on Enter.
            footer_hint: Footer key hint.
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

    @property
    def dialog_width(self) -> int:
        """Natural outer width so the box fits its prompt/suggestions, not the whole screen."""
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
        """Return suggestions matching the current text, capped for display."""
        needle = self._editor.text.lower()
        if not needle:
            matches = list(self._choices)
        else:
            matches = [c for c in self._choices if needle in c.lower()]
        return matches[: self.MAX_SUGGESTIONS]

    def render_body(self, width: int) -> list[str]:
        """Render the optional prompt, the field, the matching suggestions, and any error."""
        parts: list[RenderableType] = []
        if self._prompt:
            parts.append(Text(self._prompt))
            parts.append(Text(""))
        parts.append(self._editor.render())
        suggestions = self._suggestions()
        self._sugg = max(0, min(self._sugg, len(suggestions) - 1)) if suggestions else 0
        for i, sug in enumerate(suggestions):
            is_sel = i == self._sugg
            row = Text(("❯ " if is_sel else "  ") + sug, style="cursor" if is_sel else "muted")
            row.truncate(width)
            parts.append(row)
        if self._error:
            parts.append(Text(self._error, style="err"))
        return render_lines(Group(*parts), width)

    def handle(self, action: str, data: str = "") -> None:
        """Edit the field, move/accept suggestions, submit on Enter, or cancel on Esc."""
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
