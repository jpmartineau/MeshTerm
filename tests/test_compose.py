# SPDX-License-Identifier: Apache-2.0
"""Tests for the compose line of a chat: each complete mention is one atom, drawn as a chip."""

from __future__ import annotations

from rich.style import Style
from rich.text import Text

from meshterm.ui.compose import MentionEditor
from meshterm.ui.pathline import PATH_INK, POWERLINE_SEP, PathHop
from meshterm.ui.widgets import mention_hop


def _hop(name: str) -> PathHop:
    """The hop of a mention with no key: the keyless grey."""
    return mention_hop(name, None)


def _style(text: Text, run: str) -> Style:
    """The parsed style of the span over ``run`` (not the stamp of the path line)."""
    return Style.parse(
        next(
            str(s.style)
            for s in text.spans
            if text.plain[s.start : s.end] == run and str(s.style) != PATH_INK
        )
    )


def test_backspace_after_a_mention_removes_all_of_it() -> None:
    """One Backspace removes the whole token, and the cursor goes where the token was."""
    editor = MentionEditor("hi @[Bob]")
    assert editor.edit("backspace")
    assert (editor.text, editor.cursor) == ("hi ", 3)
    editor.edit("backspace")  # an ordinary character: one at a time, as before
    assert editor.text == "hi"


def test_delete_before_a_mention_removes_all_of_it() -> None:
    """Delete at the start of a token removes the whole token."""
    editor = MentionEditor("@[Bob] hi")
    editor.edit("home")
    editor.edit("delete")
    assert (editor.text, editor.cursor) == (" hi", 0)


def test_left_and_right_go_over_a_mention_in_one_step() -> None:
    """The cursor is never inside a token."""
    editor = MentionEditor("a @[Bob] b")
    editor.edit("left")  # before "b"
    editor.edit("left")  # before the space
    assert editor.cursor == 8
    editor.edit("left")  # over the token
    assert editor.cursor == 2
    editor.edit("right")  # over it again
    assert editor.cursor == 8


def test_a_word_jump_that_stops_inside_a_mention_goes_out_of_it() -> None:
    """A name can have a space. ^← goes to the start of the token, ^→ to its end."""
    editor = MentionEditor("@[Bob Smith] hi")
    editor.edit("end")
    editor.edit("ctrl_left")  # to "hi"
    editor.edit("ctrl_left")  # into "Smith]", then out to the start
    assert editor.cursor == 0
    editor.edit("ctrl_right")  # to the space in the name, then out to the end
    assert editor.cursor == len("@[Bob Smith]")


def test_a_typed_bracket_completes_a_mention_with_the_cursor_after_it() -> None:
    """The ``]`` makes the token an atom, and the cursor is at its end."""
    editor = MentionEditor()
    for ch in "hey @[Al":
        editor.edit("text", ch)
    assert editor.atoms() == []  # not complete yet: each character is its own
    editor.edit("text", "]")
    assert editor.atoms() == [(4, 9, "Al")] and editor.cursor == 9


def test_an_at_typed_in_front_of_a_bracketed_name_puts_the_cursor_after_it() -> None:
    """An edit that completes a token around the cursor moves the cursor out of the token."""
    editor = MentionEditor("[Bob]")
    editor.edit("home")
    editor.edit("text", "@")
    assert editor.atoms() == [(0, 6, "Bob")] and editor.cursor == 6


def test_an_open_token_typed_in_front_of_a_mention_does_not_join_it() -> None:
    """``@[`` before a chip leaves the chip as it is, and the cursor stays where it was."""
    editor = MentionEditor("@[Bob]")
    editor.edit("home")
    editor.edit("text", "@")
    editor.edit("text", "[")
    assert editor.atoms() == [(2, 8, "Bob")] and editor.cursor == 2


def test_insert_puts_text_at_the_cursor() -> None:
    """A mention goes in where the cursor is, not at the start of the line."""
    editor = MentionEditor("hi  there")
    editor.cursor = 3
    editor.insert("@[Bob]")
    assert editor.text == "hi @[Bob] there" and editor.cursor == 9


def test_the_line_draws_each_mention_as_a_chip(powerline) -> None:
    """The token shows as the chip of the node, with no ``@`` and no brackets."""
    powerline(True)
    editor = MentionEditor("hi @[Bob] ok")
    line = editor.render_line(_hop)
    assert "@[" not in line.plain and "@" not in line.plain
    assert f"{POWERLINE_SEP} Bob " in line.plain
    assert line.plain.startswith("› hi ")
    assert any(str(s.style) == PATH_INK for s in line.spans)  # the wrap keeps it whole


def test_the_prompt_and_the_text_are_white(powerline) -> None:
    """The ``›`` has the ``cursor`` white, and the typed text has the ``compose`` white."""
    powerline(True)
    line = MentionEditor("hi").render_line(_hop)
    styles = {line.plain[s.start : s.end]: str(s.style) for s in line.spans}
    assert styles["› "] == "cursor"
    assert styles["h"] == "compose" and styles["i"] == "compose"


def test_the_chip_under_the_cursor_is_the_white_slot(powerline) -> None:
    """With the cursor at the start of a token, the chip is the white chip of a slot."""
    powerline(True)
    editor = MentionEditor("@[Bob]")
    editor.edit("home")
    style = _style(editor.render_line(_hop), "Bob")
    assert style.bgcolor.name == "#ffffff"


def test_a_chip_past_the_byte_limit_is_red(powerline) -> None:
    """A token that goes past the limit has the error colour, the same as each character."""
    powerline(True)
    editor = MentionEditor("hi @[Bob]")
    editor.edit("home")  # the cursor away from the chip
    normal = _style(editor.render_line(_hop), "Bob")
    over = _style(editor.render_line(_hop, overflow_at=5), "Bob")
    assert over.color != normal.color and over.bgcolor != normal.bgcolor
