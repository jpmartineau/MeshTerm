# SPDX-License-Identifier: Apache-2.0
"""End-to-end tests for what the CLI prints and returns.

:mod:`tests.test_script_output` covers the vocabulary. :mod:`tests.test_report` covers the
seam. This file covers the commands that use both. The tests run the real Typer app against
the simulator. They check what a caller depends on:

- no escape sequences,
- no wrapped records,
- a header that the records line up under,
- a documented exit status.

Under ``--json``, they also check that the document parses, and that a rendering flag does
not change the exit status.

Each command here is read-only or uses the simulator, so nothing transmits.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meshterm.core import exitcodes

#: Any ANSI escape sequence. Nothing on the CLI's stdout may match this.
_ANSI = re.compile(r"\x1b\[")

#: A Rich console-markup tag. The scripted console does not interpret markup. A node name is
#: remote data, and `[bold]` in a name is a name, not a style. Thus a tag that MeshTerm
#: writes is also not consumed, and it goes into the pipe of the caller.
_MARKUP = re.compile(r"\[/?(?:ok|err|warn|muted|accent|brand|bold|dim|reverse)\b[^\]]*\]")

#: The shape of a drawn route: one or more hops. Each hop is a name with its hash in
#: parentheses (or only one of the two, if the stored history has only one). The arrow with
#: spaces joins the hops. This is the whole grammar. Because of the arrow, a name that has a
#: comma does not need quotes to stay one hop.
_HOP = r"[^\n→]+?"
_ROUTE_LINE = re.compile(f"{_HOP}(?: → {_HOP})*")

#: The shape of a path spec: lowercase hex joined by commas, exactly what ``--path`` takes.
#: It is the only line on each face that round-trips, so no other text can be in it.
_PATH_SPEC = re.compile(r"[0-9a-f]+(?:,[0-9a-f]+)*")

#: The commands whose output is a listing or a key/value block, and the fields that a caller
#: must find in each. Each command runs against only the mock companion or the database.
_LISTINGS: list[tuple[str, list[str], list[str]]] = [
    # (command, arguments, headings or keys the output must carry)
    ("contacts", [], ["NAME", "TYPE", "HEARD", "PKTS", "HASH", "LOCATION", "KEY"]),
    ("info", [], ["name", "public_key", "role"]),
    ("config", ["show"], ["name", "radio_freq", "tx_power"]),
    ("preferences", ["show"], ["PREFERENCE", "VALUE", "DEFAULT", "DESCRIPTION"]),
    ("chat", ["list"], ["CONVERSATION", "KIND", "UNREAD", "LAST", "LAST_TEXT"]),
    ("platform", [], ["platform", "icons"]),
]


def _leaf_commands() -> list[tuple[str, ...]]:
    """All the registered subcommand paths, found by a walk through the real Typer app.

    The code finds the paths and does not use a list, because a list that a person keeps
    covers only the commands that were changed on the day that the person wrote it. A command
    that is added later is not in the list. Two rule violations shipped in commands that
    were not in the list: a rule of 16384 cells in ``about`` and markup tags in
    ``config export-key``.
    """
    from typer.main import get_command

    from meshterm.cli import app

    def walk(command, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:  # noqa: ANN001
        found: list[tuple[str, ...]] = []
        for name, sub in sorted(getattr(command, "commands", {}).items()):
            here = (*prefix, name)
            found.extend(walk(sub, here) if getattr(sub, "commands", None) else [here])
        return found

    return walk(get_command(app))


#: Commands that own the terminal or run until the user interrupts them. They have no
#: one-shot output.
_NOT_ONE_SHOT = {("monitor",), ("chat", "listen"), ("tx-optimize",), ("emulate",)}

#: The one command whose output is its colour, and the command says this
#: (:mod:`meshterm.ui.specimen`).
_KEEPS_ITS_COLOUR = {("specimen",)}

#: Extra arguments that the derived sweep passes to a leaf. The dictionary is empty because
#: ``devices`` now knows that ``--mock`` means no scan. Before, ``devices`` listed the
#: serial ports and also switched on the Bluetooth adapter of this machine three times in
#: each run of the suite, and the sweep passed ``--no-ble`` to prevent this. The dictionary
#: stays as a hook, in case another leaf uses hardware.
_SWEEP_ARGS: dict[tuple[str, ...], tuple[str, ...]] = {}


def _sweep(leaf: tuple[str, ...]) -> tuple[str, ...]:
    """The leaf as the sweep runs it: its own path, and a flag that prevents hardware use."""
    return (*leaf, *_SWEEP_ARGS.get(leaf, ()))


@pytest.fixture()
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201 - a closure
    """Run the real CLI against the simulator, in a config directory of its own.

    ``--db`` moves only the history. The contact cache, the outbox, the channel cache, the
    stored admin passwords, and the device profiles stay in the config directory. Thus a
    suite that set only ``--db`` read the files in the own ``~/.meshterm`` of the developer.
    For this reason, a contact named ``[/]Bob``, which a person added by hand on one
    machine, could make this suite crash on that machine and pass on all other machines.
    ``$MESHTERM_HOME`` moves the whole directory, and a test must use it.
    """
    from meshterm.cli import app

    monkeypatch.setenv("MESHTERM_HOME", str(tmp_path / "home"))
    runner = CliRunner()

    def invoke(*args: str):  # noqa: ANN202
        return runner.invoke(app, ["--mock", "--db", str(tmp_path / "test.db"), *args])

    return invoke


#: Commands that refuse a machine-readable face, and report this as a usage error.
_REFUSES_JSON = {("specimen",), ("emulate",)}


# -- the four rules -------------------------------------------------------------------


@pytest.mark.parametrize("command,args,fields", _LISTINGS, ids=[c for c, _, _ in _LISTINGS])
def test_a_command_prints_no_escape_sequence(run, command, args, fields) -> None:  # noqa: ANN001
    """Nothing has colour, because stdout is more often a pipe than a terminal."""
    result = run(command, *args)
    assert result.exit_code == exitcodes.OK, result.output
    assert not _ANSI.search(result.stdout)


@pytest.mark.parametrize("command,args,fields", _LISTINGS, ids=[c for c, _, _ in _LISTINGS])
def test_a_command_carries_the_fields_a_caller_looks_for(run, command, args, fields) -> None:  # noqa: ANN001
    """The headings and keys are the contract. A changed name breaks a script with no warning."""
    result = run(command, *args)
    for field in fields:
        assert field in result.stdout, f"{command} lost {field}"


@pytest.mark.parametrize("command,args,fields", _LISTINGS, ids=[c for c, _, _ in _LISTINGS])
def test_a_command_draws_no_frame_and_no_rule(run, command, args, fields) -> None:  # noqa: ANN001
    """There are no borders, boxes, or header rules. The command that is typed is the title."""
    result = run(command, *args)
    assert not set(result.stdout) & set("─│┌┐└┘├┤━┃")


@pytest.mark.parametrize("command,args,fields", _LISTINGS, ids=[c for c, _, _ in _LISTINGS])
def test_a_command_leaves_no_trailing_whitespace(run, command, args, fields) -> None:  # noqa: ANN001
    """The column padding must not become invisible spaces at the end of each record."""
    for line in result_lines(run(command, *args)):
        assert line == line.rstrip(), repr(line)


def result_lines(result) -> list[str]:  # noqa: ANN001
    """The stdout of the command as lines, without the blank line at the end.

    ``result.output`` is a third buffer, and both streams copy into it. Thus a check of a
    stdout rule against it passes when the field appeared only on stderr. The check also
    fails for the wrong reason when anything writes a log.
    """
    return result.stdout.splitlines()


@pytest.mark.parametrize(
    "leaf",
    [c for c in _leaf_commands() if c not in _NOT_ONE_SHOT | _KEEPS_ITS_COLOUR],
    ids=lambda leaf: "-".join(leaf),
)
def test_every_registered_command_obeys_the_rules(run, leaf) -> None:  # noqa: ANN001
    """The rules apply to the whole CLI, not to the six commands that a person listed by hand.

    A command that needs arguments fails with a usage error when it runs with none. This
    is harmless, and it still proves that the command wrote nothing wrong. A command that
    can answer with no arguments runs for real. Both violations that shipped were in such
    commands: the ``---`` in ``about``, drawn as a rule as wide as a console of 16384 cells,
    and the ``[muted]`` tags around the text that ``config export-key`` must print.
    """
    out = run(*_sweep(leaf)).stdout
    assert not _ANSI.search(out), f"{' '.join(leaf)} put an escape sequence on stdout"
    assert not set(out) & set("─│┌┐└┘├┤━┃╭╮╰╯"), f"{' '.join(leaf)} drew a frame or a rule"
    assert not _MARKUP.search(out), f"{' '.join(leaf)} printed a console markup tag"
    for line in out.splitlines():
        assert line == line.rstrip(), f"{' '.join(leaf)} padded a line: {line!r}"


@pytest.mark.parametrize(
    "leaf",
    [c for c in _leaf_commands() if c not in _NOT_ONE_SHOT | _KEEPS_ITS_COLOUR],
    ids=lambda leaf: "-".join(leaf),
)
def test_every_registered_command_answers_json_that_parses(run, leaf) -> None:  # noqa: ANN001
    """``--json`` is a rendering, so it belongs to the CLI and not to a list of tools.

    It reached two commands out of twenty and stopped, because each command that wanted it
    had to state its whole answer again above the rendering. The same sweep that checks the
    plain rules now runs each command a second time and reads the result. **Each line on
    stdout is one complete document**, for a success and for an empty result. A command that
    fails writes nothing on stdout.
    """
    result = run("--json", *_sweep(leaf))
    for line in result.stdout.splitlines():
        json.loads(line)  # raises, with the wrong line, if other text was written
    if result.exit_code in (exitcodes.OK, exitcodes.NO_RESULT):
        assert result.stdout.strip(), f"{' '.join(leaf)} exited {result.exit_code} silently"


@pytest.mark.parametrize(
    "leaf",
    [c for c in _leaf_commands() if c not in _NOT_ONE_SHOT | _KEEPS_ITS_COLOUR],
    ids=lambda leaf: "-".join(leaf),
)
def test_the_two_faces_never_disagree_about_the_exit_status(run, leaf) -> None:  # noqa: ANN001
    """``--json`` changes the rendering, never the report.

    The exit status is the report. A caller branches on it, so a flag about how the answer
    looks must not change it. This check stops the two faces from drifting apart, as they
    did before. ``devices --json`` exited with ``0`` where the plain path exited with ``5``.
    Thus a script could not find the difference between an empty scan and a full scan, even
    when it asked the same question two times.
    """
    assert run("--json", *_sweep(leaf)).exit_code == run(*_sweep(leaf)).exit_code, " ".join(leaf)


@pytest.mark.parametrize("leaf", sorted(_REFUSES_JSON), ids=lambda leaf: "-".join(leaf))
def test_a_command_whose_output_is_the_colour_refuses_json_loudly(run, leaf) -> None:  # noqa: ANN001
    """The output of ``specimen`` is the colour, so a JSON document of it has no meaning.

    A clear refusal is the correct answer. The project gives the same answer for the map,
    the dashboard, and the live feed, but these have no subcommand that can refuse. The
    refusal is a usage error, so stdout stays empty. The sentence goes to the same place as
    the sentence of each other failure.
    """
    result = run("--json", *_sweep(leaf))
    assert result.exit_code == exitcodes.USAGE
    assert result.stdout == ""
    assert "machine-readable" in result.stderr


def test_the_menu_has_no_document_and_says_so(run) -> None:  # noqa: ANN001
    """``meshterm --json`` with no subcommand must not start a full-screen session.

    An interactive session cannot print a document. If the session started, the program
    that waits to parse the document waits for ever.
    """
    result = run("--json")
    assert result.exit_code == exitcodes.USAGE
    assert result.stdout == ""


def test_no_leaf_command_declares_a_short_option_the_globals_already_claim() -> None:
    """A short option of a leaf that a global option also uses is shadowed and cannot be used.

    ``_globals_first`` moves each token that matches a group option to a place before the
    subcommand, where the user typed it. Thus ``trace -t Alice -p a1,d4`` gave ``a1,d4`` to
    ``--profile`` and failed with "no device profile named 'a1,d4'". Three commands
    documented ``-p`` as the short form of ``--path``, and on all three it was not usable.
    The test examines the real app and does not use a list, because the next collision will
    be with a different letter.
    """
    from typer.main import get_command

    from meshterm.cli import app

    root = get_command(app)
    globals_ = {
        opt
        for param in root.params
        for opt in getattr(param, "opts", ())
        if opt.startswith("-") and not opt.startswith("--")
    }
    assert globals_, "the root group declares no short options — has the callback moved?"

    def walk(command, prefix: tuple[str, ...] = ()) -> None:  # noqa: ANN001
        for name, sub in sorted(getattr(command, "commands", {}).items()):
            here = (*prefix, name)
            if getattr(sub, "commands", None):
                walk(sub, here)
                continue
            for param in sub.params:
                clash = globals_ & {o for o in getattr(param, "opts", ()) if o.startswith("-")}
                assert not clash, f"{' '.join(here)} declares {sorted(clash)}, a global's own"

    walk(root)


def test_version_answers_without_touching_a_device(run) -> None:  # noqa: ANN001
    """``--version`` is a question about the program, not about a run.

    The option is eager, so it answers before MeshTerm opens a database or looks for a
    radio. The answer is bare: one field for a script to read. To know the version is a
    success, so the exit status is ``0``.
    """
    from meshterm import __version__

    result = run("--version")
    assert result.exit_code == exitcodes.OK
    assert result.stdout.strip() == f"meshterm {__version__}"


def test_the_sweep_actually_covers_the_whole_command_surface() -> None:
    """A derived list is a guarantee only while it finds the commands.

    If the walk breaks (for example, a Typer upgrade moves ``commands``), each parametrised
    case becomes zero cases with no warning. Then the sweep passes and covers nothing.
    """
    leaves = _leaf_commands()
    assert len(leaves) > 40, f"only found {len(leaves)} commands: {leaves}"
    for expected in [("about",), ("config", "export-key"), ("contacts",), ("trace",)]:
        assert expected in leaves, expected


def test_a_global_option_may_be_typed_after_the_subcommand(run) -> None:  # noqa: ANN001
    """``meshterm contacts --json`` is what a person types, so it must work.

    Click binds an option to the command that declares it. Thus a global option that the
    callback declares is a parse error when it comes one word later. For an output format,
    this order is wrong: in each tool that has an output format, ``-o json`` goes after the
    verb. The failure was a usage error with no hint that the user only needed to move the
    flag.
    """
    after = run("contacts", "--json")
    before = run("--json", "contacts")
    assert after.exit_code == exitcodes.OK, after.output
    assert after.stdout == before.stdout


def test_a_global_option_taking_a_value_moves_with_its_value(run) -> None:  # noqa: ANN001
    """If ``--db`` moves to the front, the path must move with it. The path must not stay behind."""
    assert run("contacts", "--sort", "name", "--json").exit_code == exitcodes.OK


def test_lifting_a_global_never_steals_a_subcommands_own_help(run) -> None:  # noqa: ANN001
    """``contacts --help`` must stay the help of ``contacts``, not the help of the app.

    ``--help`` is on each command. A rule that moves "the options of the group" to the front
    also moves it, and then the answer is for a different question.
    """
    contacts_help = run("contacts", "--help").stdout
    assert "contacts [OPTIONS]" in contacts_help
    assert "--sort" in contacts_help  # an option of contacts, not in the help of the app
    app_help = run("--help").stdout
    assert "COMMAND [ARGS]" in app_help
    assert "--sort" not in app_help


def test_a_time_column_is_headed_by_its_fact_not_by_its_format(run) -> None:  # noqa: ANN001
    """``--absolute`` changes the values under the heading, so the heading must stay the same.

    A column with the heading ``AGE`` and the value ``2026-09-08T04:18:25-04:00`` has a wrong
    heading. Each other time column names the fact (``HEARD``, ``RECORDED``), and this name is
    correct for each form that the run asks for.
    """
    run("chat", "send", "--to", "Alice", "hello")
    relative = result_lines(run("chat", "history", "--to", "Alice"))[0].split()
    absolute = result_lines(run("chat", "history", "--to", "Alice", "--absolute"))[0].split()
    assert relative == absolute
    assert "AGE" not in relative


# -- listings -------------------------------------------------------------------------


def test_contacts_names_every_node_bare_and_ages_every_time(run) -> None:  # noqa: ANN001
    """The two rules that changed direction, checked on the listing for which they changed.

    A name is bare, because the alignment now makes it one field. ``HEARD`` is an age,
    because the user opens this listing to ask "was it heard recently?". An ISO instant
    makes the user do arithmetic to answer. ``--absolute`` shows the instants again, for a
    user who wants them.
    """
    header, *records = result_lines(run("contacts"))
    assert header.split() == ["NAME", "TYPE", "HEARD", "PKTS", "HASH", "LOCATION", "KEY"]
    assert records, "the simulator always has contacts"
    # Read the lane at the position of its heading. Do not count words, because a value can
    # have a space (the `Lakeside BBS` of the simulator is a `room server`). The alignment,
    # not the whitespace, separates the fields.
    heard = header.index("HEARD")
    for line in records:
        assert not line.startswith('"')
        age = line[heard:].split()[0]
        assert age in ("now", "never") or age[-1] in "mhdw"

    # `--absolute` asks for a different form of time, not for one fact less. Thus `never`
    # stays: a node that was never heard has no instant to print.
    header, *absolute = result_lines(run("--absolute", "contacts"))
    heard = header.index("HEARD")
    for line in absolute:
        stamp = line[heard:].split()[0]
        assert stamp in ("-", "never") or stamp[:4].isdigit()


def test_contacts_never_elides_a_key(run) -> None:  # noqa: ANN001
    """A caller cannot give a truncated key back to ``--to`` or ``--path``."""
    for line in result_lines(run("contacts"))[1:]:
        assert "…" not in line and not line.endswith("...")


def test_a_config_line_can_be_typed_back_into_config_set(run) -> None:  # noqa: ANN001
    """``show`` names each setting by the key that ``get``/``set`` take, and prints its value."""
    values = dict(line.split(None, 1) for line in result_lines(run("config", "show")) if line)
    assert values["name"] == "MockCompanion"
    # An enum prints its number, not the label for the user: `parse_value` takes the number.
    assert values["adv_loc_policy"].isdigit()
    assert run("config", "get", "name").output.strip() == "MockCompanion"


def test_every_setting_show_prints_is_one_set_takes_back(run) -> None:  # noqa: ANN001
    """The round-trip claim is checked on each setting, not only on the two easy settings.

    A user can write ``config show > f`` and then give ``f`` back as input. This is the
    normal use of a dump, and a failure gives no warning. An empty string that was printed as
    ``""`` was parsed back as the two quote characters. The next dump looked the same, so
    nothing showed that the setting was replaced. A refusal is acceptable here, because a
    user cannot set a value that the firmware did not report. A value that parses to
    something else is not acceptable.
    """
    from meshterm.core import device_config as dc
    from meshterm.tools.config import _script_value

    probes = {
        "str": ["", "Yagi-Repeater", "a name with spaces"],
        "bool": [True, False],
        "int": [0, 1, 12],
        "float": [0.0, 3.5],
    }
    checked = 0
    for _category, specs in dc.settings_by_category():
        for spec in specs:
            if spec.value_type == "enum" and spec.choices:
                values: list = list(spec.choices)
            else:
                values = probes.get(spec.value_type, [])
            for value in values:
                printed = _script_value(spec, value)
                try:
                    parsed = dc.parse_value(spec, printed, {})
                except dc.DeviceConfigError:
                    continue  # a clear refusal is correct. A wrong value is not correct.
                checked += 1
                assert parsed == value, f"{spec.key}: show printed {printed!r} -> {parsed!r}"
    assert checked > 40, f"only {checked} round-trips exercised"


def test_an_unreported_pin_is_not_masked_as_though_one_were_set(run) -> None:  # noqa: ANN001
    """Bullets say "there is a secret here". An absent value must not make this claim.

    ``conceal`` already does not mask the absence token of the menu, for this reason. But
    the token of the scripted face is ``-``, and ``conceal`` did not know it. Thus a radio
    that never reported a PIN printed six bullets in a dump, and the user (or the bug report
    where the user pasted the dump) read this as a PIN lock.
    """
    from meshterm.ui import script
    from meshterm.ui.config_editor import MASK_MARK, conceal

    # The rule at the boundary where it broke: a real value is masked and an absent value is
    # not. Each surface says which glyph it uses for an absent value.
    assert conceal("1234", absent=script.NONE) == MASK_MARK * 6
    assert conceal(script.NONE, absent=script.NONE) == script.NONE
    assert conceal("?") == "?"

    # The two surfaces must not disagree about the same radio: `show` prints bullets only
    # where `get` has a value to conceal.
    values = dict(
        line.split(None, 1) for line in result_lines(run("config", "show")) if " " in line
    )
    got = run("config", "get", "device_pin").output.strip()
    assert (MASK_MARK in values["device_pin"]) == (got != script.NONE), (
        f"show says {values['device_pin']!r} while get says {got!r}"
    )


def test_config_get_prints_the_bare_value(run) -> None:  # noqa: ANN001
    """The caller named the key. If the output repeats the key, the caller must remove it."""
    assert run("config", "get", "tx_power").output.strip() == "20"


def test_preferences_get_prints_a_value_its_own_set_would_take(run) -> None:  # noqa: ANN001
    """The page shows ``5 s``, which has a unit that ``preferences set`` does not accept."""
    assert run("preferences", "get", "trace_cooldown_s").output.strip() == "5"


# -- path lines -----------------------------------------------------------------------


def test_a_trace_draws_its_route_with_arrows_and_keeps_its_path_in_commas(run) -> None:  # noqa: ANN001
    """This is the most important of the changes of direction, because of the project lexicon.

    A path is a spec that the user writes and can paste into ``--path``. Thus it stays
    separated by commas, and it round-trips. A route is what a walk did. It is not a spec,
    and a route with commas promised a round trip that it does not have. Our node has a
    name like each other hop. The ``★`` of the menu says "you already know who this is".
    This is true for the user, and it is not true for a person who opens the file later.
    """
    result = run("trace-path", "--path", "a1,d4,a1")
    assert result.exit_code == exitcodes.OK, result.output
    facts = dict(line.split(None, 1) for line in result_lines(result) if line and " " in line)
    assert facts["success"] == "yes"

    route = facts["route"]
    assert "★" not in route
    assert "→" in route
    assert _ROUTE_LINE.fullmatch(route), route

    assert _PATH_SPEC.fullmatch(facts["path"]), facts["path"]
    assert "→" not in facts["path"]


def test_a_trace_reports_its_per_hop_readings_under_a_header(run) -> None:  # noqa: ANN001
    """The second block has one record for each hop. The hash joins it to the route line."""
    lines = result_lines(run("trace-path", "--path", "a1,d4,a1"))
    header = next(line for line in lines if line.startswith("HOP"))
    assert header.split() == ["HOP", "FROM", "TO", "SNR_DB"]


# -- exit statuses --------------------------------------------------------------------


def test_an_empty_result_is_its_own_status_and_prints_nothing(run) -> None:  # noqa: ANN001
    """The simulator has no channel slots: the command worked and found nothing.

    ``grep`` uses the same status for "found nothing" and "worked". This is the only reason
    that :data:`~meshterm.core.exitcodes.NO_RESULT` exists.
    """
    result = run("channels", "list")
    assert result.exit_code == exitcodes.NO_RESULT
    assert result.output.strip() == ""


def test_a_bad_flag_is_a_usage_error(run) -> None:  # noqa: ANN001
    """This status comes from Click. The table names it, so the table is complete."""
    assert run("contacts", "--no-such-flag").exit_code == exitcodes.USAGE


def test_a_bad_setting_key_is_a_plain_failure(run) -> None:  # noqa: ANN001
    """A retry does not help. The caller must change the request."""
    result = run("config", "get", "nosuchkey")
    assert result.exit_code == exitcodes.FAILURE


def test_a_failure_says_so_on_stderr_and_never_on_stdout(run) -> None:  # noqa: ANN001
    """The message is ``program: what went wrong``. A caller that parses stdout need not filter."""
    result = run("config", "get", "nosuchkey")
    assert result.stdout == ""
    assert "meshterm: unknown setting" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("config", "reboot"),
        ("config", "factory-reset"),
        ("config", "import-key", "00" * 32),
        ("channels", "clear", "1"),
        ("preferences", "reset"),
    ],
    ids=["reboot", "factory-reset", "import-key", "channels-clear", "preferences-reset"],
)
def test_a_destructive_command_without_yes_is_a_silent_usage_error(run, args) -> None:  # noqa: ANN001
    """A missing confirmation is a bad argument, and the message goes where errors go.

    These commands printed a red refusal on stdout and exited with 1. The colour and the
    sentence were not the answer of the command, but they were in the text that the program
    that read the answer received.
    """
    result = run(*args)
    assert result.exit_code == exitcodes.USAGE
    assert result.stdout == ""
    assert "--yes" in result.stderr


@pytest.mark.parametrize(
    "args,flag",
    [
        (("contacts", "--sort", "bogus"), "--sort"),
        (("records", "--category", "long_hual"), "--category"),
        (("records", "--width", "3"), "--width"),
        (("records", "--width", "0"), "--width"),
        (("records", "--width", "-1"), "--width"),
        (("records", "--json", "--width", "3"), "--width"),
    ],
    ids=[
        "contacts-sort",
        "records-category",
        "records-width-3",
        "records-width-0",
        "records-width-negative",
        "records-width-3-json",
    ],
)
def test_a_value_outside_a_closed_set_is_refused_rather_than_ignored(run, args, flag) -> None:  # noqa: ANN001
    """A typo in an option with a closed set of values was answered as a different question.

    ``--sort bogus`` sorted by the default with no warning. ``--category long_hual`` selected
    all the disciplines with no warning, and the exit status was ``5``, the code for a real
    empty result. A caller that branched on ``5`` could not find the difference between
    "there are no records" and "you misspelled the discipline". This is the worst answer
    that a closed set can give.
    """
    result = run(*args)
    assert result.exit_code == exitcodes.USAGE
    assert result.stdout == ""
    assert flag in result.stderr


def test_a_profile_that_names_nothing_is_refused_before_anything_transmits(run) -> None:  # noqa: ANN001
    """``--profile`` says which radio to use. If it cannot be kept, no default replaces it.

    An unknown name went to the normal discovery. Thus a scheduled ``--profile yagi config
    advert`` with a typo transmitted from the companion that was attached, whichever it was.
    This is the one failure where a silent error puts a packet on the air.
    """
    result = run("--profile", "nosuchprofile", "info")
    assert result.exit_code == exitcodes.USAGE
    assert result.stdout == ""
    assert "nosuchprofile" in result.stderr


def test_a_port_that_will_not_open_is_no_device_not_a_device_failure(run) -> None:  # noqa: ANN001
    """The two device statuses answer different questions, and this status gave the wrong answer.

    ``3`` means that nothing was transmitted, and the user must look at what is plugged in.
    ``4`` means that MeshTerm reached the radio and the operation failed, so a retry is
    reasonable. A ``--port`` that could not open gave ``4``, with the message "the connection
    to the device was lost". There was never a connection. The ``case $?`` recipe in the
    manual branches on this difference.
    """
    from typer.testing import CliRunner

    from meshterm.cli import app

    result = CliRunner().invoke(app, ["--port", "NOSUCHPORT99", "info"])
    assert result.exit_code == exitcodes.NO_DEVICE
    assert "could not open" in result.stderr
    assert "lost" not in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("repeater-admin", "Nobody", "get", "name"),
        ("regions", "Nobody"),
        ("regions", "Alice"),
        ("courier", "queue", "Nobody", "hi"),
        ("courier", "queue", "Alice", "hi", "--at", "25:99"),
        ("tx-optimize", "--path", "Yagi-Repeater", "--samples", "1"),
    ],
    ids=[
        "unknown-admin-node",
        "unknown-regions-node",
        "regions-of-a-companion",
        "unknown-courier-contact",
        "bad-at-time",
        "tx-optimize-one-hop-path",
    ],
)
def test_a_bad_argument_is_never_reported_as_a_device_failure(run, args) -> None:  # noqa: ANN001
    """``4`` tells a caller that the radio answered and that a retry is worth a try.

    In each of these cases, the argument is wrong, and MeshTerm decides this before it
    transmits anything. If a caller retries on ``4``, it retries for ever and transmits
    nothing. ``tx-optimize`` already gave the same answer for a missing password. These
    commands did not yet do the same, and this includes the one-hop ``--path`` of
    ``tx-optimize``, which gave ``4`` for a path that never left the machine.
    """
    result = run(*args)
    assert result.exit_code == exitcodes.USAGE, result.output
    assert result.stdout == ""


def test_a_node_that_answers_no_is_still_a_device_failure(run) -> None:  # noqa: ANN001
    """This is the other side of the line: MeshTerm reached the radio, so a retry is a real option.

    A refused admin login is not a bad argument, because the password can have changed. The
    change of the status for the arguments above must not change the status of this case.
    """
    result = run("repeater-admin", "Yagi-Repeater", "get", "name", "--password", "wrongpw")
    assert result.exit_code == exitcodes.DEVICE, result.output


def test_a_repeaters_regions_on_both_faces(run) -> None:  # noqa: ANN001
    """The output has the node that answered, if it relays unscoped floods, and the regions.

    The output never has the ``*``.
    """
    plain = run("regions", "Yagi-Repeater")
    assert plain.exit_code == exitcodes.OK, plain.output
    assert result_lines(plain) == [
        "node      Yagi-Repeater",
        "unscoped  yes",
        "regions   lakeside, lakeside-north, harbour",
    ]
    document = json.loads(run("--json", "regions", "Yagi-Repeater").stdout)
    assert document["node"]["name"] == "Yagi-Repeater"
    assert document["unscoped"] is True
    assert document["regions"] == ["lakeside", "lakeside-north", "harbour"]


def test_a_repeater_that_names_nothing_is_nothing_to_report(run, monkeypatch) -> None:  # noqa: ANN001
    """The repeater answered, so this is not a failure. It relays no floods: exit status 5."""
    from meshterm.core.connection import MockDevice

    async def nothing(self, node):  # noqa: ANN001, ANN202
        return []

    monkeypatch.setattr(MockDevice, "request_regions", nothing)
    result = run("regions", "Yagi-Repeater")
    assert result.exit_code == exitcodes.NO_RESULT
    document = json.loads(run("--json", "regions", "Yagi-Repeater").stdout)
    assert document["unscoped"] is False and document["regions"] == []


def test_a_repeater_that_never_answers_is_a_device_failure(run, monkeypatch) -> None:  # noqa: ANN001
    """MeshTerm sent the request and nothing came back. A retry is a real option: exit status 4."""
    from meshterm.core.connection import DeviceCommandError, MockDevice

    async def silent(self, node):  # noqa: ANN001, ANN202
        raise DeviceCommandError(f"{node.name!r} did not answer the regions request.")

    monkeypatch.setattr(MockDevice, "request_regions", silent)
    result = run("regions", "Yagi-Repeater")
    assert result.exit_code == exitcodes.DEVICE
    assert result.stdout == ""
    assert "did not answer" in result.stderr


def test_an_empty_result_writes_nothing_to_stdout_at_all(run) -> None:  # noqa: ANN001
    """There is no "no channels configured" line. The status says it, so the pipe stays clean."""
    result = run("channels", "list")
    assert result.exit_code == exitcodes.NO_RESULT
    assert result.stdout == ""


def test_mock_and_an_explicit_radio_are_a_bad_command_line(run) -> None:  # noqa: ANN001
    """One flag says "pretend", the other names one exact radio, and both cannot be true.

    ``--mock`` won with no warning. Thus a scheduled ``--port COM7 --mock info`` reported on
    a simulator, and the report looked as if it came from the radio. Two claims that
    disagree about which device to use are a bad command line. They are not a question of
    precedence.
    """
    from typer.testing import CliRunner

    from meshterm.cli import app

    for flag, value in (("--port", "COM7"), ("--ble", "AA:BB"), ("--tcp", "host:1")):
        result = CliRunner().invoke(app, ["--mock", flag, value, "info"])
        assert result.exit_code == exitcodes.USAGE, flag
        assert result.stdout == ""
        assert flag in result.stderr


def test_an_acknowledgement_reaches_the_person_without_reaching_the_pipe(run) -> None:  # noqa: ANN001
    """The CLI dropped ``ack``, and the reason covered only half of the case.

    An acknowledgement is not the answer. A caller that redirects stdout must get only the
    answer, but stdout is not the only stream. stderr follows this rule, and it also gives a
    person who waits for a ``config restore`` of five minutes the ✓ for each setting. Before,
    the person could not see these marks.
    """
    result = run("config", "set", "tx_power", "20")
    assert result.exit_code == exitcodes.OK
    assert result.stdout == ""
    assert "tx_power" in result.stderr


def test_a_closing_message_goes_to_stderr_too(run) -> None:  # noqa: ANN001
    """A closing count is for a person, not a record for a parser.

    The count repeats what the listing above showed and what ``$?`` says, so it must not go
    into a redirect. A person wants it after a command that scrolled past on the terminal.
    """
    result = run("records")
    assert result.exit_code == exitcodes.NO_RESULT
    assert result.stdout == ""
    assert "records stored" in result.stderr


@pytest.mark.parametrize("width", ["1", "2", "4"])
def test_every_real_hash_width_is_accepted(run, width) -> None:  # noqa: ANN001
    """The refusal of the widths that do not exist must not refuse the three that exist."""
    result = run("records", "--width", width)
    assert result.exit_code == exitcodes.NO_RESULT, result.stderr
    assert "records stored" in result.stderr


def test_the_simulator_is_listed_without_empty_brackets(run) -> None:  # noqa: ANN001
    """``--mock devices`` named the simulator ``… (nothing transmits) ()``: a port it lacks."""
    result = run("devices")
    assert result.exit_code == exitcodes.OK, result.stderr
    assert "the built-in simulator (nothing transmits)" in result.stdout
    assert "()" not in result.stdout


#: The text of a source docstring that goes into ``--help`` when Typer uses the docstring:
#: a Sphinx role, the double backticks of reST, or a path into the package.
_SOURCE_NOTES = re.compile(r":(func|mod|class|meth|attr|data):`|``|meshterm/[\w/]+\.py")


@pytest.mark.parametrize("leaf", _leaf_commands(), ids=lambda leaf: "-".join(leaf))
def test_every_command_help_is_written_for_the_terminal(run, leaf) -> None:  # noqa: ANN001
    """A help page is for a person at a prompt who asks what a command does.

    ``platform`` and ``specimen`` printed their whole docstrings. These are notes for a
    person who reads ``cli.py``, with Sphinx roles and source paths. The same markup also
    cut their one-line summaries short in ``meshterm --help``.
    """
    result = run(*leaf, "--help")
    assert result.exit_code == exitcodes.OK, result.output
    assert not _SOURCE_NOTES.search(result.output), result.output


def test_the_command_list_has_no_source_notes(run) -> None:  # noqa: ANN001
    """The one-line summaries in ``meshterm --help`` come from the same help text."""
    result = run("--help")
    assert not _SOURCE_NOTES.search(result.output), result.output
    for line in result.output.splitlines():
        if line.split()[:1] in (["platform"], ["specimen"]):
            assert not line.rstrip().endswith("..."), f"summary cut short: {line!r}"


# -- the simulator's own history ------------------------------------------------------


def test_mock_records_into_a_database_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A simulated radio gets a simulated history, so a demo run does not change the real mesh data.

    The simulator is a fake radio, not a fake MeshTerm. Before, the adverts of the simulator
    were written to ``meshterm.db`` like each other message from a companion. Then its
    invented contacts were in the mesh walk, the dashboard, and the map of the real mesh. The
    run still stores its history, because the tools of a ``--mock`` session read it to see
    what they heard. But the history goes to a different file.
    """
    from meshterm.cli import app

    home = tmp_path / "home"
    monkeypatch.setenv("MESHTERM_HOME", str(home))

    result = CliRunner().invoke(app, ["--mock", "info"])
    assert result.exit_code == exitcodes.OK, result.output
    assert (home / "meshterm-mock.db").exists()
    assert not (home / "meshterm.db").exists()


def test_an_explicit_db_still_wins_under_mock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--db`` names one exact database, and the simulator does not override it.

    Unlike ``--port``, ``--db`` does not contradict ``--mock``. The place where a run stores
    its history and the radio that it uses are different questions. Thus this is a rule of
    precedence and not a usage error. The ``run`` fixture of this suite also depends on it.
    """
    from meshterm.cli import app

    home = tmp_path / "home"
    monkeypatch.setenv("MESHTERM_HOME", str(home))
    named = tmp_path / "named.db"

    result = CliRunner().invoke(app, ["--mock", "--db", str(named), "info"])
    assert result.exit_code == exitcodes.OK, result.output
    assert named.exists()
    assert not (home / "meshterm-mock.db").exists()
    assert not (home / "meshterm.db").exists()


# -- devices under --mock -------------------------------------------------------------


def test_mock_devices_scans_nothing_and_lists_the_simulator(
    run,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--mock`` promises no real hardware, so the list has the simulator and there is no scan.

    The test replaces both discovery entry points with functions that fail with a clear
    error, because the tool must not call either one. The one row that the tool lists names
    ``--mock`` as its target, because this flag selects this device. ``--port`` and ``--ble``
    do the same for the other devices.
    """
    from meshterm.tools import devices as tool

    def never(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("a --mock session scanned for real hardware")

    monkeypatch.setattr(tool, "discover_devices", never)
    monkeypatch.setattr(tool, "discover_all", never)

    result = run("devices")
    assert result.exit_code == exitcodes.OK
    assert "--mock" in result.stdout and "mock" in result.stdout

    machine = run("--json", "devices")
    assert machine.exit_code == exitcodes.OK
    (row,) = json.loads(machine.stdout)
    assert row["target"] == "--mock" and row["transport"] == "mock"


# -- the map --------------------------------------------------------------------------


def test_the_map_has_no_cli_command_at_all(run) -> None:  # noqa: ANN001
    """A picture has no scripted face. The nodes that have a position stay in ``contacts``."""
    assert run("map").exit_code == exitcodes.USAGE


# -- help -----------------------------------------------------------------------------


def test_help_is_plain_and_names_every_exit_status(run) -> None:  # noqa: ANN001
    """The help from Click has no boxed panels and no colour. The status table is under it."""
    result = run("--help")
    assert not _ANSI.search(result.output)
    assert not set(result.output) & set("─│╭╮╰╯")
    for code in exitcodes.MEANINGS:
        assert f"{code} " in result.output
