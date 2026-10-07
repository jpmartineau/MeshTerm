# SPDX-License-Identifier: Apache-2.0
"""Tests for the plain CLI's output language (:mod:`meshterm.ui.script`).

A person at a prompt reads the plain face. ``--json`` is for the machine. Each rule that
makes the plain face easy to read can be tested: no colour, no wrapping, no frames, bare
names that cannot break a record, relative ages, arrows for a route and commas for a path,
one token for an absent value, and one documented exit status for each outcome. These
tests are the enforcement point for :mod:`meshterm.ui.script`, in the same way as
``test_gallery`` is for the screens, and :mod:`tests.test_report` is for the seam above
them.
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone

import pytest
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from meshterm.core import exitcodes
from meshterm.ui import script

#: Any ANSI escape sequence. No output of the scripted console can match this.
_ANSI = re.compile(r"\x1b\[")


def _rendered(*renderables: object) -> str:
    """Print ``renderables`` through the scripted console and return the output."""
    buffer = io.StringIO()
    console = script.console(buffer)
    for renderable in renderables:
        console.print(renderable)
    console.file.flush()
    return buffer.getvalue()


# -- names ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Yagi-Repeater", "Yagi-Repeater"),
        ("YUL Cartierville", "YUL Cartierville"),
        ("Node, Inc", "Node, Inc"),
        ('He said "hi"', 'He said "hi"'),
        ("back\\slash", "back\\\\slash"),
    ],
)
def test_a_name_is_bare_because_alignment_is_the_delimiter(value: str, expected: str) -> None:
    """The quotes are retired. A column that is aligned is already one field to the eye.

    The quotes were there so that a name with a space or a comma stayed one field for the
    code that split the line. That code reads JSON now. In plain text, the padding shows
    where a field ends. Thus the user gets back the six cells that the quotes used in each
    row, and a name shows as the name that the node broadcast.
    """
    assert script.name(value) == expected


def test_a_name_still_cannot_end_the_record_it_sits_in() -> None:
    """The escaping under the quoting stays. This was the half that was necessary.

    A node broadcasts its own name, so each name here is remote data. The alignment does not
    stop a newline, a tab, or an ESC in a name from ending the record too early. An ESC in
    a name is a live colour sequence that goes into the file of the caller. The rule "not a
    single escape sequence" exists to prevent this.
    """
    assert script.name("two\nlines") == "two\\nlines"
    assert script.name("tabbed\there") == "tabbed\\there"
    assert script.name("bell\a") == "bell\\x07"
    assert "\x1b" not in script.name("esc\x1b[31m")


@pytest.mark.parametrize(
    "body,expected",
    [
        ("line one\nline two", "line one\\nline two"),
        ("tabbed\there", "tabbed\\there"),
        ("carriage\rreturn", "carriage\\rreturn"),
        ("back\\slash", "back\\\\slash"),
        ('a "quoted" body', 'a "quoted" body'),
        ("plain", "plain"),
    ],
)
def test_a_message_body_can_never_end_its_own_record(body: str, expected: str) -> None:
    """``TEXT`` is the rest of the line, and "the rest of the line" must stay one line.

    A body with a newline ended its record too early. The remainder was indented under the
    other columns, and it looked like a second record with an empty ``TIME``. The body is
    also the one field that a stranger fills in, so this message is not hypothetical. The
    function does not change quotes in the body. Nothing wraps the body, so there is
    nothing to escape from.
    """
    assert script.text(body) == expected
    assert "\n" not in script.text(body)


@pytest.mark.parametrize("absent", [None, ""])
def test_a_node_that_never_gave_a_name_reads_as_absent_not_as_empty(absent: str | None) -> None:
    """``""`` says "called nothing". ``-`` says "never said". These are different facts.

    Seven of the ten places that printed a name made the first claim by accident. Thus the
    same node with no name showed as ``""`` in one listing and ``-`` in the next. One helper
    now gives the answer. The machine face needs the same distinction: an absent name is
    ``null``, an empty name is ``""``, and a field that makes both the same cannot say
    which one it meant.
    """
    assert script.name(absent) == script.NONE
    assert script.name("Alice") == "Alice"


# -- timestamps ----------------------------------------------------------------------


def test_stamp_is_absolute_local_iso_to_the_second() -> None:
    """A time is printed in a form that a script can sort and subtract."""
    when = datetime(2026, 9, 7, 22, 22, 41, tzinfo=timezone.utc)
    text = script.stamp(when)
    assert datetime.fromisoformat(text) == when
    assert text.endswith(when.astimezone().strftime("%z")[:3] + ":00")


def test_stamp_reads_an_unknown_offset_as_absent() -> None:
    """A naive datetime is a time with an offset that we do not know. This is not a fact.

    :func:`~meshterm.ui.widgets.age_seconds` uses the same rule for relative ages.
    """
    assert script.stamp(None) == script.NONE
    assert script.stamp(datetime(2026, 9, 7, 22, 22, 41)) == script.NONE


def test_a_time_is_an_age_unless_the_instant_is_itself_the_fact() -> None:
    """A time is an age by default. This rule changed, and this is the reason.

    A person at a prompt who reads ``2026-09-07T19:58:53-04:00`` does arithmetic to answer
    "recently?". This is the question that the person typed the command to ask. Thus an age
    is now the default. :func:`~meshterm.ui.script.stamp` stays for three places where the
    instant is the answer: the device clock, an appointment that ``--at`` sets, and the
    ``TIME`` column of a live capture itself (an age shows ``now`` in each row). It
    also stays for each column under ``--absolute``.
    """
    fresh = datetime.now(timezone.utc) - timedelta(seconds=5)
    assert script.age(fresh) == "now"
    assert script.stamp(fresh)[:4].isdigit()


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "now"),
        (59, "now"),
        (60, "1m"),
        (3599, "59m"),
        (3600, "1h"),
        (86400, "1d"),
        (604800, "1w"),
    ],
)
def test_the_age_ladder_steps_where_a_person_would_step(seconds: int, expected: str) -> None:
    """``now``, ``5m``, ``3h``, ``2d``, ``4w``: the ladder of the columns in the menu.

    The code calls :func:`~meshterm.ui.widgets.format_age` and does not derive the ages
    again. Thus the two faces of the same age cannot become different.
    """
    assert script.age(datetime.now(timezone.utc) - timedelta(seconds=seconds)) == expected


def test_an_age_reads_an_unknown_offset_as_absent_exactly_as_a_stamp_does() -> None:
    """A naive datetime is a time with an offset that we do not know. This is not a fact."""
    assert script.age(None) == script.NONE
    assert script.age(datetime(2026, 9, 7, 22, 22, 41)) == script.NONE


def test_never_is_a_value_and_the_absent_token_is_not() -> None:
    """A node that was never heard is a fact. A row with no such time is not a fact.

    If both cases show ``-``, then "we have not heard from it" and "this type of row has no
    heard time" look the same. The column exists to show this difference.
    """
    assert script.age(None, absent="never") == "never"
    assert script.age(None) == script.NONE


@pytest.mark.parametrize(
    "seconds,expected",
    [(42, "42s"), (360, "6m"), (125, "2m 5s"), (93784, "1d 2h"), (3600, "1h"), (-125, "2m 5s")],
)
def test_a_duration_reads_in_two_units_at_most(seconds: int, expected: str) -> None:
    """``93784`` is a number that nobody can keep in mind. ``1d 2h`` is the same fact.

    The duration has two adjacent units, the largest first. Thus the user sees the
    magnitude at a glance, and the precision is not more than the user can use. It is
    always a gloss beside the raw figure. The key says what the number counts, and a caller
    that reads the key must still find a number under it.
    """
    assert script.duration(seconds) == expected


def test_a_location_is_one_field_because_it_is_one_fact() -> None:
    """A coordinate pair is the text that goes into the search box of a map, without change.

    Two columns also give two ``-`` for each node that never shared a position. This is
    worse than one ``-``.
    """
    assert script.location(45.50190, -73.56740) == "45.50190,-73.56740"
    assert script.location(45.5, None) == script.NONE
    assert script.location(None, None) == script.NONE


# -- the absent token -----------------------------------------------------------------


def test_one_token_stands_for_every_kind_of_absence() -> None:
    """The user learns ``-`` one time, and never sees an em dash, an ``n/a``, or a blank.

    ``never`` is the one word that is not an absence. It is a value, and the age lane spells
    it (refer to the ladder tests above).
    """
    assert script.NONE == "-"
    assert script.number(None) == script.NONE
    assert script.route([]) == script.NONE
    assert script.spec([]) == script.NONE


def test_number_formats_with_its_spec_when_there_is_one() -> None:
    """The function formats a number that is present. Only an absent number becomes the token."""
    assert script.number(2.64, "+.1f") == "+2.6"
    assert script.number(-1.5, "+.1f") == "-1.5"
    assert script.number(7) == "7"


# -- routes and paths ------------------------------------------------------------------


def test_a_route_is_drawn_with_arrows_and_a_path_keeps_its_commas() -> None:
    """A route is walked and a path is typed, and the output shows the difference.

    Both were separated by commas in the past. A route looked like text that you can paste
    back into ``--path``, but you cannot, because the names are in it. Thus the comma
    promised a round trip that only the spec has. The arrow says "this is a picture", and
    it shows the direction in which the packet travelled.
    """
    assert (
        script.route([("Alice", "3d"), ("Yagi-Repeater", "f2")])
        == "Alice (3d) → Yagi-Repeater (f2)"
    )
    assert script.spec(["a1", "d4", "a1"]) == "a1,d4,a1"


def test_a_route_names_our_own_node_like_any_other_hop() -> None:
    """The route has no star. The reason is the person who reads the line.

    The menu draws our node as ``★``, because the user does not need to be told which node
    is theirs. A route line is often read from a file by somebody who was not at the prompt
    when the command ran. For that person, the star names nothing.
    """
    line = script.route([("MockCompanion", "00"), ("Alice", "3d"), ("MockCompanion", "00")])
    assert "★" not in line
    assert line.startswith("MockCompanion (00)")
    assert line.endswith("MockCompanion (00)")


def test_a_route_hop_never_shows_an_empty_pair_of_parentheses() -> None:
    """Half an identity prints as the half that is known: a name alone, or a hash alone.

    :class:`~meshterm.ui.pathline.PathLine` in the menu labels an unresolved hop in the same
    way. Both halves are optional because the history often knows only one half.
    """
    assert script.route([("Alice", None)]) == "Alice"
    assert script.route([(None, "3d")]) == "3d"
    assert script.route([(None, None)]) == script.NONE


def test_the_arrow_is_what_makes_the_quoting_unnecessary() -> None:
    """The claim moved. The quotes made a comma safe, and now nothing must do this.

    A comma in the name of a node cannot be read as a hop boundary now, because the hop
    boundary is not a comma.
    """
    line = script.route([("Node, Inc", "3d"), ("Bob", "f2")])
    assert line == "Node, Inc (3d) → Bob (f2)"
    assert line.count("→") == 1


def test_a_spec_is_the_one_line_on_either_face_that_round_trips() -> None:
    """``--path`` takes the spec back, so the spec has no name, no arrow, and no space."""
    assert script.spec(["a1b2", "d4e5", "a1b2"]) == "a1b2,d4e5,a1b2"
    assert " " not in script.spec(["a1", "d4"])


# -- live streams ----------------------------------------------------------------------


def test_a_stream_pins_its_lanes_because_it_cannot_measure_them() -> None:
    """A live capture cannot measure its columns, because it has not seen the records yet.

    :func:`~meshterm.ui.script.columns` sets the size of each lane to its widest value after
    all the records are in. ``monitor`` prints a row at the moment when a packet arrives,
    and the next row can be twice as wide. Thus these two streams were joined with a gutter
    and had no alignment. The quoting was the last thing that kept the alignment.
    """
    lanes = script.stream(("TIME", 19), ("NODE", 8), ("SNR_DB", 6), right=("SNR_DB",))
    assert lanes.header == "TIME                 NODE      SNR_DB"
    assert lanes.record("2026-09-08T02:25:45", "a1b2c3d4", "+7.0") == (
        "2026-09-08T02:25:45  a1b2c3d4    +7.0"
    )


def test_a_streamed_value_wider_than_its_lane_overruns_rather_than_lying() -> None:
    """One row of many is wider, and the stream does not elide anything.

    This is the reason that the one field with no limit goes last (the name of a node, or
    the body of a message). Then it pushes no other field.
    """
    lanes = script.stream(("NODE", 8), ("NAME", 6))
    assert lanes.record("a1", "A Very Long Node Name") == "a1        A Very Long Node Name"


# -- the console ----------------------------------------------------------------------


def test_the_scripted_console_emits_no_escape_sequence_at_all() -> None:
    """No colour, no bold, no dim: ``stdout`` is more often a pipe than a screen."""
    loud = Text.assemble(
        ("brand", "brand"), ("bold", "bold"), ("hex", "#ff8800"), ("reverse", "reverse")
    )
    assert not _ANSI.search(_rendered(loud))


@pytest.mark.parametrize(
    "name",
    ["[bold]Loud", "[/]Bob", "[red]a[/red]", ":fire:Hot", "[[weird]]", "100% [done]"],
)
def test_a_node_name_is_never_read_as_rich_markup(name: str) -> None:
    """A node broadcasts its own name, so each name printed here is remote data.

    Rich reads ``[...]`` as a style tag, unless the code tells it not to. This caused two
    different bugs. ``[bold]Loud`` printed as ``Loud``. This was a silent corruption, and
    the text was no longer the string that identifies the node to the person who reads the
    output. ``[/]Bob`` raised ``MarkupError`` and stopped the whole command. ``:fire:`` is
    the same hazard in another parser. These cases are not hypothetical: a name is the one
    field that a stranger controls.
    """
    table = script.columns("NAME", "TYPE")
    table.add_row(script.name(name), "node")
    record = _rendered(table).splitlines()[1]
    assert record.startswith(script.name(name)), record


def test_the_scripted_console_never_wraps_a_record_onto_a_second_line() -> None:
    """A record is a line. If it wraps, half of its fields are under the wrong headings."""
    table = script.columns("KEY", "VALUE")
    table.add_row("basemap_tilejson_url", "x" * 400)
    lines = _rendered(table).splitlines()
    assert len(lines) == 2  # the header and the one record
    assert lines[1].endswith("x" * 400)


def test_the_scripted_console_leaves_no_trailing_whitespace() -> None:
    """If the last column has padding to its width, each line has invisible spaces at the end."""
    table = script.columns("NAME", "TYPE")
    table.add_row(script.name("a-long-name"), "node")
    table.add_row(script.name("b"), "repeater")
    for line in _rendered(table).splitlines():
        assert line == line.rstrip(), repr(line)


def test_the_gutter_still_separates_columns_after_the_trimming() -> None:
    """The trimmer holds trailing whitespace, but it must not remove the padding between.

    Rich writes a rendered row one segment at a time and flushes after each segment. If a
    wrapper trims at the flush, it removes the spaces that make the columns into columns
    (refer to :class:`meshterm.ui.script._Trimmed`).
    """
    table = script.columns("NAME", "TYPE", "PKTS", right=("PKTS",))
    table.add_row(script.name("Alice"), "node", "12")
    header, record = _rendered(table).splitlines()
    assert header.split() == ["NAME", "TYPE", "PKTS"]
    assert record.split() == ["Alice", "node", "12"]
    # The text is written in full, not as `" " * script.GUTTER`. An assertion that reads
    # the constant that it checks becomes `" " in record` when the gutter becomes narrower.
    # The gutter exists to prevent this state.
    assert "Alice  node" in record


def test_columns_align_so_every_record_splits_the_same_way() -> None:
    """Each field is in the column of its heading, for any widths."""
    table = script.columns("NAME", "TYPE")
    table.add_row(script.name("Yagi-Repeater"), "repeater")
    table.add_row(script.name("Al"), "node")
    header, *records = _rendered(table).splitlines()
    starts = {line.index("re") if "repeater" in line else line.index("node") for line in records}
    assert starts == {header.index("TYPE")}  # each TYPE value starts under its heading


def test_a_numeric_column_really_right_aligns() -> None:
    """The user can compare a column of magnitudes by eye only if the digits are aligned.

    ``Text.wrap`` of Rich returns early when ``overflow="ignore"``. This early return is
    above the justify step, so ``overflow="ignore"`` removes ``justify="right"`` and gives
    no warning. Each ``right=`` in the CLI did nothing until the columns used ``"crop"``.
    ``"crop"`` refuses to elide in the same way, and it still aligns. A check of the
    position where a field starts cannot find this problem. Only a check of the end can.
    """
    table = script.columns("NAME", "PKTS", right=("PKTS",))
    table.add_row(script.name("Alice"), "1174")
    table.add_row(script.name("Bob"), "3")
    header, *records = _rendered(table).splitlines()
    assert len({len(line) for line in records}) == 1, records
    assert {len(line) for line in records} == {len(header)}


def test_no_column_elides_however_long_its_value_runs() -> None:
    """A caller cannot give a short key back to ``--to`` or ``--path``.

    The guard against elision and the guard against misalignment are the same setting.
    Thus a change to one guard changes the other guard without a warning. This test
    checks both.
    """
    table = script.columns("KEY", "PKTS", right=("PKTS",))
    table.add_row("d4" * 32, "7")
    out = _rendered(table)
    assert "…" not in out
    assert "d4" * 32 in out


def test_the_trimmer_keeps_the_gutter_when_the_stream_flushes_between_columns() -> None:
    """The legacy-Windows renderer of Rich writes a row one segment at a time and flushes each.

    ``LegacyWindowsTerm.write_text`` is ``write(text); flush()``. Thus on that path a flush
    occurs between two columns as often as at the end of a line. A wrapper that emptied its
    held run at the flush destroyed each gutter in real use. The tests in the process
    continued to pass, because they see one write and one flush for each ``print``. This is
    not hypothetical: the code shipped with this bug. This test drives the stream in the
    same way as that renderer.
    """
    buffer = io.StringIO()
    stream = script._Trimmed(buffer)
    for segment in ("Alice", "  ", "node", "  ", "12", "\n"):
        stream.write(segment)
        stream.flush()
    assert buffer.getvalue() == "Alice  node  12\n"


def test_the_trimmer_still_drops_the_run_that_ends_a_line() -> None:
    """The other half of the rule: the trimmer holds padding, and it must still remove it.

    The trimmer writes held whitespace when text follows it on the line. It removes the
    whitespace at the newline and at the end of the stream. Otherwise each record ends with
    the invisible padding of its last column.
    """
    buffer = io.StringIO()
    stream = script._Trimmed(buffer)
    for segment in ("row", "    ", "\n", "next", "   "):
        stream.write(segment)
        stream.flush()
    assert buffer.getvalue() == "row\nnext"


def test_a_value_wider_than_the_console_is_cut_rather_than_elided() -> None:
    """After :data:`~meshterm.ui.script.WIDTH` cells the value is cut. It must not become "…".

    The user cannot see the difference between an elision and a value that really ends
    there. A key that the user reads from a listing then goes back into ``--to`` with an
    error that is hard to see. A key of 64 hex digits never reaches the overflow path, so
    only a value that is wider than the limit tests the guard at that place.
    """
    table = script.columns("KEY")
    table.add_row("d" * (script.WIDTH + 500))
    assert "…" not in _rendered(table)


def test_an_error_stays_greppable_when_stderr_is_not_a_terminal() -> None:
    """An error stays easy to find with grep when stderr is not a terminal.

    A sentence that is broken across three lines is a sentence that nothing can find. Rich
    uses 80 cells when it cannot measure the destination. Thus ``2> errors.log`` wrapped
    each message, and ``grep 'not already connected'`` found nothing, because a newline was
    in the middle of the words. In a real terminal, the code does not change the width. A
    progress bar of 16384 cells is not a progress bar.
    """
    import sys

    buffer = io.StringIO()
    stderr, sys.stderr = sys.stderr, buffer
    try:
        console = script.stderr_console()
        console.print("meshterm: " + "the connection was not already connected " * 6)
    finally:
        sys.stderr = stderr
    assert len(buffer.getvalue().splitlines()) == 1


# -- framing --------------------------------------------------------------------------


def test_a_panel_gives_up_its_border_and_its_title() -> None:
    """No frames: the command that the user typed is the title."""
    out = _rendered(*script.flatten(Panel(Text("body"), title="Trace summary")))
    assert out.strip() == "body"


def test_a_table_gives_up_its_box_its_title_and_its_expansion() -> None:
    """If a shared table arrives with a box, the output has no frame."""
    from rich import box

    table = Table(title="Contacts", box=box.SIMPLE_HEAD, expand=True)
    table.add_column("NAME")
    table.add_row("Alice")
    out = _rendered(*script.flatten(table))
    assert "Contacts" not in out
    assert not any(ch in out for ch in "─│┌└")


def test_a_group_is_unframed_member_by_member() -> None:
    """The code removes the frames at any depth in a shared renderable."""
    nested = Group(Panel(Text("one"), title="First"), Panel(Text("two"), title="Second"))
    assert _rendered(*script.flatten(nested)).split() == ["one", "two"]


def test_no_rendered_line_is_a_box_drawing_rule() -> None:
    """No borders and no header rules. A listing is its header line and its records."""
    table = script.columns("NAME", "TYPE")
    table.add_row(script.name("Alice"), "node")
    assert "─" not in _rendered(table)


# -- key/value listings ---------------------------------------------------------------


def test_pairs_print_one_fact_per_line_with_no_header() -> None:
    """The form of ``sysctl -a``: a key, the gutter, and the rest of the line as its value."""
    out = _rendered(script.pairs([("name", "MockCompanion"), ("uptime_s", "93784")]))
    assert [line.split(None, 1) for line in out.splitlines()] == [
        ["name", "MockCompanion"],
        ["uptime_s", "93784"],
    ]


def test_a_pairs_value_is_unquoted_because_it_is_the_rest_of_the_line() -> None:
    """Quotes are for a name that shares a line with other fields. Here there are only two."""
    out = _rendered(script.pairs([("name", "YUL Cartierville")]))
    assert out.splitlines()[0].split(None, 1) == ["name", "YUL Cartierville"]


# -- exit statuses --------------------------------------------------------------------


def test_the_exit_statuses_are_the_numbers_a_caller_hard_codes() -> None:
    """``case $? in 3)`` uses the digits, so the digits are the contract.

    The test states the digits as literals on purpose. The assertion
    ``set(MEANINGS) == {OK, FAILURE, …}``, or ``len(set(d)) == len(d)``, cannot fail when
    someone changes the numbers of the constants. A dict has no duplicate keys, so if two
    statuses have the same number, the dict loses a row, and both sides of the comparison
    lose the same row together. When a change gave ``NO_DEVICE`` the number ``4``, all of
    these assertions stayed green, and status 3 was not in ``--help``.
    """
    assert (exitcodes.OK, exitcodes.FAILURE, exitcodes.USAGE) == (0, 1, 2)
    assert (exitcodes.NO_DEVICE, exitcodes.DEVICE, exitcodes.NO_RESULT) == (3, 4, 5)
    assert sorted(exitcodes.MEANINGS) == [0, 1, 2, 3, 4, 5]
    assert all(meaning for meaning in exitcodes.MEANINGS.values())


def test_the_help_epilog_names_every_status() -> None:
    """A caller must not need to find the table in a README."""
    from meshterm.cli import EXIT_STATUS_EPILOG

    for code, meaning in exitcodes.MEANINGS.items():
        assert f"{code} {meaning}" in EXIT_STATUS_EPILOG
