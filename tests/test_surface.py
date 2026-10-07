# SPDX-License-Identifier: Apache-2.0
"""Tests for the way that the TUI output surface shows a result: as a screen or as a dialog.

:meth:`TuiUi.present` changes a short result that has only text ("✓ flood advertisement
sent") to an OK dialog in the centre. It does this instead of a full screen that scrolls.
The tests cover these parts:

- The collapse gate (:func:`_collapse_to_message`).
- The routing between the two presentations.
- The mapping from severity to border that the message dialog applies
  (:func:`_message_border`).
"""

from __future__ import annotations

import asyncio
from typing import Any

from rich.table import Table
from rich.text import Text

from meshterm.ui.surface import _DIALOG_MAX_WRAPPED, TuiUi, _collapse_to_message
from meshterm.ui.tui.session import TuiSession, _message_border

# -- the collapse gate ------------------------------------------------------------


def test_collapse_accepts_a_short_note_and_keeps_its_styling() -> None:
    """A success note of one line qualifies, and its markup spans stay."""
    note = Text.from_markup("[ok]✓[/ok] flood advertisement sent")
    message = _collapse_to_message([note])
    assert message is not None
    assert message.plain == "✓ flood advertisement sent"
    assert any(str(span.style) == "ok" for span in message.spans)


def test_collapse_joins_a_few_notes_into_one_message() -> None:
    """Two or three separate notes collapse into one dialog message of more than one line."""
    notes = [Text("✓ device clock set"), Text("● wrote backup.toml")]
    message = _collapse_to_message(notes)
    assert message is not None
    assert message.plain == "✓ device clock set\n● wrote backup.toml"


def test_collapse_rejects_tall_output() -> None:
    """If there are more lines than a dialog can hold, the result goes to the result screen."""
    assert _collapse_to_message([Text(f"line {i}") for i in range(4)]) is None


def test_collapse_wraps_a_wide_line_instead_of_rejecting_it() -> None:
    """A wide line wraps, and the collapse does not reject it.

    A long outcome of one line stays a dialog. The box wraps the line, and does not pass it
    on. Thus a failure, for example "✗ Chat failed: could not read contacts …", is an
    acknowledgement that the user closes. It is not a full-frame screen for reading.
    """
    sentence = "could not read contacts from the radio. " * 3
    message = _collapse_to_message([Text(sentence)])
    assert message is not None
    lines = message.plain.splitlines()
    assert len(lines) > 1  # the text was reflowed
    assert max(len(line) for line in lines) <= 54  # the wrap width of the regular platform
    assert " ".join(line.strip() for line in lines) == sentence.strip()


def test_collapse_rejects_a_line_long_enough_to_be_a_page() -> None:
    """A line that is long enough to be a page is rejected.

    The wrap has a limit. Above the limit, the output is text to read, and the result
    screen takes it.
    """
    assert _collapse_to_message([Text("word " * 120)]) is None
    assert _DIALOG_MAX_WRAPPED == 8


def test_collapse_rejects_non_text_renderables() -> None:
    """A table or a panel in the buffer sends the whole result to the result screen."""
    assert _collapse_to_message([Text("note"), Table()]) is None
    assert _collapse_to_message([]) is None


# -- present() routing --------------------------------------------------------------


class _RecordingSession:
    """A substitute for :class:`TuiSession`. It stores which presentation the code used."""

    def __init__(self) -> None:
        self.dialogs: list[tuple[Text, str]] = []
        self.scrolls: list[tuple[Any, str]] = []

    async def message_dialog(self, message: Text, *, title: str = "") -> None:
        self.dialogs.append((message, title))

    async def scroll(self, body: Any, *, title: str = "", footer_hint: str = "") -> None:
        self.scrolls.append((body, title))


async def test_present_floats_a_short_note_as_a_dialog() -> None:
    """A single outcome note floats as an OK dialog, and not as a full result screen."""
    session = _RecordingSession()
    ui = TuiUi(session)  # type: ignore[arg-type]
    ui.note("[ok]✓[/ok] zero-hop advertisement sent")
    await ui.present(title="Advert")
    assert session.scrolls == []
    ((message, title),) = session.dialogs
    assert message.plain == "✓ zero-hop advertisement sent"
    assert title == "Advert"


async def test_present_keeps_big_output_in_the_result_window() -> None:
    """Tables, and any output that is too large, still open the result screen that scrolls."""
    session = _RecordingSession()
    ui = TuiUi(session)  # type: ignore[arg-type]
    ui.note("heading")
    ui.show(Table(title="nodes"))
    await ui.present(title="Nodes")
    assert session.dialogs == []
    assert [title for _, title in session.scrolls] == ["Nodes"]


async def test_present_clears_the_buffer_either_way() -> None:
    """After a presentation, the buffer is clear.

    Thus a second present has nothing to show (no double dialog).
    """
    session = _RecordingSession()
    ui = TuiUi(session)  # type: ignore[arg-type]
    ui.note("✓ done")
    await ui.present(title="Once")
    await ui.present(title="Twice")
    assert len(session.dialogs) == 1 and session.scrolls == []


# -- the dialog from end to end through a real session ------------------------------------


async def test_message_dialog_floats_over_a_blank_backdrop_end_to_end() -> None:
    """The message dialog floats over a blank backdrop, from end to end.

    On an empty stack, the dialog pushes a base under itself, then pops both. This is the
    path of a tool from the main menu. The menu is popped while a tool runs. Without the
    backdrop, the single dialog would be drawn as the base (full-frame). When a menu has
    declared itself the root, the menu is the base. A session that never reached a menu,
    such as this bare session, gets a blank frame. The test runs a real session with piped
    keys. It proves that Enter (OK) closes the dialog and the stack unwinds.
    """
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as inp:
        session = TuiSession(input=inp, output=DummyOutput())

        async def main() -> None:
            inp.send_text("\r")  # Enter selects the OK button
            await session.message_dialog(
                Text.from_markup("[ok]✓[/ok] flood advertisement sent"), title="Advert"
            )
            assert session._stack == []  # the dialog and its backdrop are both popped

        await asyncio.wait_for(session.run(main()), timeout=5)


async def test_confirm_floats_over_an_existing_popup_end_to_end() -> None:
    """A confirm floats over an existing dialog, from end to end.

    The confirm opens over a floating detail dialog. It renders and resolves. The render
    loop really runs the float pool with many layers (base, detail, and confirm). In this
    case, the old compositor that supported only one float stretched the detail dialog to
    full-frame.
    """
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from meshterm.ui.tui.screen import ScrollScreen
    from meshterm.ui.tui.select import Choice, SelectScreen

    with create_pipe_input() as inp:
        session = TuiSession(input=inp, output=DummyOutput())

        async def main() -> None:
            base = ScrollScreen(Text("channels"), title="Channels")  # a full-frame background
            detail = SelectScreen("Ops", [Choice("Clear", "clr")])  # a floating dialog
            session.push(base)
            session.push(detail)
            # Two layers float over the one background while the confirm is open.
            assert session._base_screen() is base
            inp.send_text("\r")  # Enter selects the default of the confirm (Delete)
            confirmed = await session.button_dialog(
                "Clear Ops?", [("Cancel", 0), ("Clear", 1)], default=1, border_style="err"
            )
            assert confirmed == 1
            assert session._float_layers() == [detail]  # the confirm is gone, and the detail floats
            session.pop(detail)
            session.pop(base)

        await asyncio.wait_for(session.run(main()), timeout=5)


# -- the border tone of the message dialog -----------------------------------------------


def test_message_border_echoes_the_strongest_tone() -> None:
    """The message border uses the strongest tone.

    Err is stronger than warn. Warn is stronger than the neutral accent. Ok stays neutral.
    """
    err = Text.from_markup("[err]✗ Trace failed:[/err] timeout")
    warn = Text.from_markup("[warn]device rebooting[/warn]")
    ok = Text.from_markup("[ok]✓[/ok] private key imported")
    both = Text.from_markup("[warn]careful[/warn] [err]broken[/err]")
    assert _message_border(err) == "err"
    assert _message_border(warn) == "warn"
    assert _message_border(ok) == "accent"
    assert _message_border(both) == "err"
    assert _message_border("plain string") == "accent"


# -- the caution tiers of the button dialog ----------------------------------------------


class _ButtonRecordingSession:
    """Stores the styling kwargs that :meth:`TuiUi.dialog` gives to the button dialog."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def button_dialog(self, prompt: str, buttons: list, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return buttons[-1][1]


async def _dialog_styles(**kw: Any) -> dict[str, Any]:
    session = _ButtonRecordingSession()
    ui = TuiUi(session)  # type: ignore[arg-type]
    await ui.dialog("Delete this record?", [("Cancel", False), ("Delete", True)], **kw)
    return session.kwargs


async def test_dialog_destructive_tier_borders_red() -> None:
    """The destructive tier of a dialog has a red border.

    A dialog for data loss is drawn in the reserved error red, for the prompt and the
    border.
    """
    styles = await _dialog_styles(destructive=True)
    assert styles["border_style"] == "err"
    assert styles["prompt_style"] == "err"


async def test_dialog_danger_tier_stays_amber() -> None:
    """The danger tier of a dialog stays amber.

    A dialog that is only disruptive keeps the amber caution tone. It never has the red of
    a deletion.
    """
    styles = await _dialog_styles(danger=True)
    assert styles["border_style"] == "warn"
    assert styles["prompt_style"] == "warn"


async def test_dialog_plain_is_neutral_and_destructive_outranks_danger() -> None:
    """A dialog with no flag is neutral. Destructive wins over danger when both are set.

    With no flag, the frame has the neutral accent.
    """
    plain = await _dialog_styles()
    assert plain["border_style"] == "accent" and plain["prompt_style"] == ""
    both = await _dialog_styles(danger=True, destructive=True)
    assert both["border_style"] == "err" and both["prompt_style"] == "err"
