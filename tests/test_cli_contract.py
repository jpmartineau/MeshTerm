# SPDX-License-Identifier: Apache-2.0
"""End-to-end tests for what the CLI actually prints and returns.

:mod:`tests.test_script_output` covers the vocabulary and :mod:`tests.test_report` the
seam; this covers the commands built out of both — the real Typer app, driven against the
simulator, asserting the things a caller depends on: no escape sequences, no wrapped
records, a header its records line up under, a documented exit status — and, under
``--json``, a document that parses and a status that did not change because of a rendering
flag.

Every command here is read-only or simulator-backed, so nothing transmits.
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

#: A Rich console-markup tag. The scripted console no longer *interprets* markup — a node
#: name is remote data and `[bold]` in one is a name, not a style — which means a tag
#: MeshTerm writes itself is no longer consumed either, and lands in the caller's pipe.
_MARKUP = re.compile(r"\[/?(?:ok|err|warn|muted|accent|brand|bold|dim|reverse)\b[^\]]*\]")

#: The shape of a drawn route: one or more hops, each a bare name with its hash in
#: parentheses (or one of the two halves alone, where history knows only one), joined by
#: the spaced arrow. This is the whole grammar, and the arrow is what makes it one — a
#: name holding a comma no longer needs quoting to stay one hop.
_HOP = r"[^\n→]+?"
_ROUTE_LINE = re.compile(f"{_HOP}(?: → {_HOP})*")

#: The shape of a path *spec*: comma-joined lowercase hex, exactly what ``--path`` takes
#: back. The one line on either face that round-trips, so nothing may creep into it.
_PATH_SPEC = re.compile(r"[0-9a-f]+(?:,[0-9a-f]+)*")

#: The commands whose output is a listing or a key/value block, and the fields a caller
#: should find in each. Every one runs against the mock companion or the database alone.
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
    """Every registered subcommand path, walked out of the real Typer app.

    Derived rather than listed: a hand-kept roster covers whatever was reworked the day it
    was written, and a command added afterwards inherits none of it. Two rule violations
    shipped inside commands the roster did not name — a 16384-cell rule in ``about`` and
    markup tags in ``config export-key``.
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


#: Commands that own the terminal or run until interrupted: they have no one-shot output.
_NOT_ONE_SHOT = {("monitor",), ("chat", "listen"), ("tx-optimize",)}

#: The one command whose output *is* its colour, and says so (:mod:`meshterm.ui.specimen`).
_KEEPS_ITS_COLOUR = {("specimen",)}

#: Extra arguments the derived sweep passes a leaf. Empty since ``devices`` learned that
#: ``--mock`` means no scan: it used to enumerate serial ports *and* switch this machine's
#: Bluetooth radio on three times per suite run, and the sweep passed ``--no-ble`` to
#: spare it. Kept as the hook it is, in case another leaf ever reaches for hardware.
_SWEEP_ARGS: dict[tuple[str, ...], tuple[str, ...]] = {}


def _sweep(leaf: tuple[str, ...]) -> tuple[str, ...]:
    """The leaf as the sweep invokes it: its own path, plus any hardware-sparing flag."""
    return (*leaf, *_SWEEP_ARGS.get(leaf, ()))


@pytest.fixture()
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201 - a closure
    """Invoke the real CLI against the simulator, in a config directory of its own.

    ``--db`` moves the history and *nothing else*: the contact cache, the outbox, the
    channel cache, the stored admin passwords and any device profiles all live in the
    config directory regardless. So a suite that set only ``--db`` read whatever was in
    the developer's own ``~/.meshterm`` — which is how a contact named ``[/]Bob``, planted
    by hand on one machine, could crash this suite on that machine and pass everywhere
    else. ``$MESHTERM_HOME`` is the override that moves the whole directory, and it is
    the one a test wants.
    """
    from meshterm.cli import app

    monkeypatch.setenv("MESHTERM_HOME", str(tmp_path / "home"))
    runner = CliRunner()

    def invoke(*args: str):  # noqa: ANN202
        return runner.invoke(app, ["--mock", "--db", str(tmp_path / "test.db"), *args])

    return invoke


#: Commands that refuse a machine-readable face outright, and say so as a usage error.
_REFUSES_JSON = {("specimen",)}


# -- the four rules -------------------------------------------------------------------


@pytest.mark.parametrize("command,args,fields", _LISTINGS, ids=[c for c, _, _ in _LISTINGS])
def test_a_command_prints_no_escape_sequence(run, command, args, fields) -> None:  # noqa: ANN001
    """Nothing is coloured: stdout is a pipe more often than a terminal."""
    result = run(command, *args)
    assert result.exit_code == exitcodes.OK, result.output
    assert not _ANSI.search(result.stdout)


@pytest.mark.parametrize("command,args,fields", _LISTINGS, ids=[c for c, _, _ in _LISTINGS])
def test_a_command_carries_the_fields_a_caller_looks_for(run, command, args, fields) -> None:  # noqa: ANN001
    """The headings and keys are the contract; renaming one silently breaks a script."""
    result = run(command, *args)
    for field in fields:
        assert field in result.stdout, f"{command} lost {field}"


@pytest.mark.parametrize("command,args,fields", _LISTINGS, ids=[c for c, _, _ in _LISTINGS])
def test_a_command_draws_no_frame_and_no_rule(run, command, args, fields) -> None:  # noqa: ANN001
    """No borders, no boxes, no header rules — the command typed is the title."""
    result = run(command, *args)
    assert not set(result.stdout) & set("─│┌┐└┘├┤━┃")


@pytest.mark.parametrize("command,args,fields", _LISTINGS, ids=[c for c, _, _ in _LISTINGS])
def test_a_command_leaves_no_trailing_whitespace(run, command, args, fields) -> None:  # noqa: ANN001
    """Column padding must not become invisible spaces at the end of every record."""
    for line in result_lines(run(command, *args)):
        assert line == line.rstrip(), repr(line)


def result_lines(result) -> list[str]:  # noqa: ANN001
    """The command's stdout as lines, with the trailing blank dropped.

    ``result.output`` is a third buffer that *both* streams copy into, so a rule about
    stdout asserted against it passes when the field it wants only ever appeared on
    stderr — and fails for the wrong reason the moment anything logs.
    """
    return result.stdout.splitlines()


@pytest.mark.parametrize(
    "leaf",
    [c for c in _leaf_commands() if c not in _NOT_ONE_SHOT | _KEEPS_ITS_COLOUR],
    ids=lambda leaf: "-".join(leaf),
)
def test_every_registered_command_obeys_the_rules(run, leaf) -> None:  # noqa: ANN001
    """The rules belong to the CLI, not to the six commands somebody listed by hand.

    Run bare, a command that needs arguments fails harmlessly as a usage error and still
    proves it wrote nothing awkward on the way out; a command that can answer without
    arguments is exercised for real. That is where both shipped violations were — the
    ``---`` in ``about`` drawn as a rule the width of a 16384-cell console, and the
    ``[muted]`` tags around what ``config export-key`` exists to emit.
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
    """``--json`` is a rendering, so it belongs to the CLI rather than to a list of tools.

    It reached two commands out of twenty and stopped, because each one that wanted it had
    to restate its whole answer above the rendering. The same sweep that keeps the plain
    rules honest now runs every command a second time and reads what comes back: **every
    line on stdout is one complete document**, on success and on empty alike, and a
    command that fails writes nothing there at all.
    """
    result = run("--json", *_sweep(leaf))
    for line in result.stdout.splitlines():
        json.loads(line)  # raises, with the offending line, if anything else got out
    if result.exit_code in (exitcodes.OK, exitcodes.NO_RESULT):
        assert result.stdout.strip(), f"{' '.join(leaf)} exited {result.exit_code} silently"


@pytest.mark.parametrize(
    "leaf",
    [c for c in _leaf_commands() if c not in _NOT_ONE_SHOT | _KEEPS_ITS_COLOUR],
    ids=lambda leaf: "-".join(leaf),
)
def test_the_two_faces_never_disagree_about_the_exit_status(run, leaf) -> None:  # noqa: ANN001
    """``--json`` changes the rendering, never the report.

    The status is the report — it is what a caller branches on — and a flag about *how the
    answer looks* has no business moving it. This is the cross-check that stops the two
    faces drifting the way they did before: ``devices --json`` used to exit ``0`` where the
    plain path said ``5``, so a script could not tell an empty scan from a full one by
    asking the same question twice.
    """
    assert run("--json", *_sweep(leaf)).exit_code == run(*_sweep(leaf)).exit_code, " ".join(leaf)


@pytest.mark.parametrize("leaf", sorted(_REFUSES_JSON), ids=lambda leaf: "-".join(leaf))
def test_a_command_whose_output_is_the_colour_refuses_json_loudly(run, leaf) -> None:  # noqa: ANN001
    """``specimen``'s output *is* the colour; a document of it would be an empty gesture.

    A loud refusal is the honest answer, and it is the same one this project already gives
    for the map, the dashboard and the live feed — which simply have no subcommand to
    refuse from. It is a usage error, so stdout stays empty and the sentence goes where
    every other failure's does.
    """
    result = run("--json", *_sweep(leaf))
    assert result.exit_code == exitcodes.USAGE
    assert result.stdout == ""
    assert "machine-readable" in result.stderr


def test_the_menu_has_no_document_and_says_so(run) -> None:  # noqa: ANN001
    """``meshterm --json`` with no subcommand would launch a full-screen session.

    There is no document an interactive session can emit, and starting one would hang
    whatever was waiting to parse it.
    """
    result = run("--json")
    assert result.exit_code == exitcodes.USAGE
    assert result.stdout == ""


def test_no_leaf_command_declares_a_short_option_the_globals_already_claim() -> None:
    """A leaf short form that a global also spells could never be reached, only shadowed.

    ``_globals_first`` lifts any token matching a group option ahead of the subcommand
    wherever it was typed, so ``trace -t Alice -p a1,d4`` handed ``a1,d4`` to ``--profile``
    and failed with "no device profile named 'a1,d4'". Three commands documented ``-p`` as
    the short form of ``--path``, and on all three it was unusable. Checked against the real
    app rather than a list, because the next collision will be some other letter.
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
    """``--version`` is the one question that is about the program, not about a run.

    Eager, so it answers before a database is opened or a radio looked for, and bare — one
    field for a script to read back. Knowing the version is a success, so it exits ``0``.
    """
    from meshterm import __version__

    result = run("--version")
    assert result.exit_code == exitcodes.OK
    assert result.stdout.strip() == f"meshterm {__version__}"


def test_the_sweep_actually_covers_the_whole_command_surface() -> None:
    """A derived list is only a guarantee while it is still finding the commands.

    If the walk breaks — a Typer upgrade moving ``commands``, say — every parametrised
    case silently becomes zero cases and the sweep passes by covering nothing.
    """
    leaves = _leaf_commands()
    assert len(leaves) > 40, f"only found {len(leaves)} commands: {leaves}"
    for expected in [("about",), ("config", "export-key"), ("contacts",), ("trace",)]:
        assert expected in leaves, expected


def test_a_global_option_may_be_typed_after_the_subcommand(run) -> None:  # noqa: ANN001
    """``meshterm contacts --json`` is what a person types, so it has to be what works.

    Click binds an option to whatever command declares it, so a global declared on the
    callback is a parse error one word later. For an output format that is the wrong way
    round — ``-o json`` goes after the verb in every tool that has one — and the failure
    was a bare usage error with no hint that the flag simply needed moving.
    """
    after = run("contacts", "--json")
    before = run("--json", "contacts")
    assert after.exit_code == exitcodes.OK, after.output
    assert after.stdout == before.stdout


def test_a_global_option_taking_a_value_moves_with_its_value(run) -> None:  # noqa: ANN001
    """Lifting ``--db`` to the front must take the path with it, not orphan it."""
    assert run("contacts", "--sort", "name", "--json").exit_code == exitcodes.OK


def test_lifting_a_global_never_steals_a_subcommands_own_help(run) -> None:  # noqa: ANN001
    """``contacts --help`` must stay the *contacts* help, not silently become the app's.

    ``--help`` is on every command, so a rule that lifts "the group's options" would take
    it too and answer a different question than the one asked.
    """
    contacts_help = run("contacts", "--help").stdout
    assert "contacts [OPTIONS]" in contacts_help
    assert "--sort" in contacts_help  # a contacts option, absent from the app's own help
    app_help = run("--help").stdout
    assert "COMMAND [ARGS]" in app_help
    assert "--sort" not in app_help


def test_a_time_column_is_headed_by_its_fact_not_by_its_format(run) -> None:  # noqa: ANN001
    """``--absolute`` changes what is under the heading, so the heading must survive it.

    A column headed ``AGE`` holding ``2026-09-08T04:18:25-04:00`` is a heading that lies —
    and every other time column already names the fact (``HEARD``, ``RECORDED``), which
    reads correctly whichever form the run asked for.
    """
    run("chat", "send", "--to", "Alice", "hello")
    relative = result_lines(run("chat", "history", "--to", "Alice"))[0].split()
    absolute = result_lines(run("chat", "history", "--to", "Alice", "--absolute"))[0].split()
    assert relative == absolute
    assert "AGE" not in relative


# -- listings -------------------------------------------------------------------------


def test_contacts_names_every_node_bare_and_ages_every_time(run) -> None:  # noqa: ANN001
    """The two rules that reversed, checked on the listing they were reversed for.

    A name is bare — the alignment is what makes it one field now — and ``HEARD`` is an
    age, because "heard recently?" is the question this listing is opened to ask and an ISO
    instant makes the reader do arithmetic to answer it. ``--absolute`` puts the instants
    back, for anyone who wants them.
    """
    header, *records = result_lines(run("contacts"))
    assert header.split() == ["NAME", "TYPE", "HEARD", "PKTS", "HASH", "LOCATION", "KEY"]
    assert records, "the simulator always has contacts"
    for line in records:
        assert not line.startswith('"')
        age = line.split()[2]
        assert age in ("now", "never") or age[-1] in "mhdw"

    # `--absolute` asks for a different *form* of time, not for one fewer fact, so
    # `never` survives it: a node that has never been heard has no instant to print.
    absolute = result_lines(run("--absolute", "contacts"))[1:]
    for line in absolute:
        stamp = line.split()[2]
        assert stamp in ("-", "never") or stamp[:4].isdigit()


def test_contacts_never_elides_a_key(run) -> None:  # noqa: ANN001
    """A truncated key is not something a caller can hand back to ``--to`` or ``--path``."""
    for line in result_lines(run("contacts"))[1:]:
        assert "…" not in line and not line.endswith("...")


def test_a_config_line_can_be_typed_back_into_config_set(run) -> None:  # noqa: ANN001
    """``show`` names each setting by the key ``get``/``set`` take, and prints its value."""
    values = dict(line.split(None, 1) for line in result_lines(run("config", "show")) if line)
    assert values["name"] == "MockCompanion"
    # An enum prints its number, not the reader's label: `parse_value` takes the number.
    assert values["adv_loc_policy"].isdigit()
    assert run("config", "get", "name").output.strip() == "MockCompanion"


def test_every_setting_show_prints_is_one_set_takes_back(run) -> None:  # noqa: ANN001
    """The round-trip claim, checked on every setting rather than on the two easy ones.

    ``config show > f`` and feeding ``f`` back in is the obvious thing to do with a dump,
    and the failure mode is silent: an empty string printed as ``""`` used to parse back
    as *the two quote characters*, and the next dump looked identical, so nothing ever
    said the setting had been replaced. A refusal is fine here — you cannot set a value
    the firmware never reported — but a value that parses to something else is not.
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
                    continue  # a loud refusal is honest; a wrong value is not
                checked += 1
                assert parsed == value, f"{spec.key}: show printed {printed!r} -> {parsed!r}"
    assert checked > 40, f"only {checked} round-trips exercised"


def test_an_unreported_pin_is_not_masked_as_though_one_were_set(run) -> None:  # noqa: ANN001
    """Bullets say "there is a secret here". An absence must not borrow that claim.

    ``conceal`` already declines to mask the *menu's* absence token for exactly this
    reason, but the scripted token is ``-``, which it had never been shown — so a radio
    that had never reported a PIN dumped six bullets, and the reader (or the bug report
    they pasted it into) read that as PIN-locked.
    """
    from meshterm.ui import script
    from meshterm.ui.config_editor import MASK_MARK, conceal

    # The rule at the boundary where it broke: a real value is masked, an absence is not,
    # and each surface says which glyph it writes an absence with.
    assert conceal("1234", absent=script.NONE) == MASK_MARK * 6
    assert conceal(script.NONE, absent=script.NONE) == script.NONE
    assert conceal("?") == "?"

    # And the two surfaces must never disagree about the same radio: `show` wears bullets
    # only where `get` has something to conceal.
    values = dict(
        line.split(None, 1) for line in result_lines(run("config", "show")) if " " in line
    )
    got = run("config", "get", "device_pin").output.strip()
    assert (MASK_MARK in values["device_pin"]) == (got != script.NONE), (
        f"show says {values['device_pin']!r} while get says {got!r}"
    )


def test_config_get_prints_the_bare_value(run) -> None:  # noqa: ANN001
    """The caller named the key; repeating it back is one more thing to strip off."""
    assert run("config", "get", "tx_power").output.strip() == "20"


def test_preferences_get_prints_a_value_its_own_set_would_take(run) -> None:  # noqa: ANN001
    """The page's ``5 s`` carries a unit that ``preferences set`` will not accept back."""
    assert run("preferences", "get", "trace_cooldown_s").output.strip() == "5"


# -- path lines -----------------------------------------------------------------------


def test_a_trace_draws_its_route_with_arrows_and_keeps_its_path_in_commas(run) -> None:  # noqa: ANN001
    """The sharpest of the reversals, and the reason is the project's own lexicon.

    A *path* is a spec you compose and can paste back into ``--path``, so it stays
    comma-separated and round-trippable. A *route* is what a walk actually did; it is not a
    spec, and rendering it with commas promised a round trip it does not have. Our own node
    is still named like any other hop — the menu's ``★`` says "you already know who this
    is", which is true of the reader and false of whoever opens the file later.
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
    """The second block is a record per hop, joined to the route line by hash."""
    lines = result_lines(run("trace-path", "--path", "a1,d4,a1"))
    header = next(line for line in lines if line.startswith("HOP"))
    assert header.split() == ["HOP", "FROM", "TO", "SNR_DB"]


# -- exit statuses --------------------------------------------------------------------


def test_an_empty_result_is_its_own_status_and_prints_nothing(run) -> None:  # noqa: ANN001
    """The simulator configures no channel slots: the command worked and found nothing.

    ``grep`` conflates "found nothing" with "worked"; this is the whole reason
    :data:`~meshterm.core.exitcodes.NO_RESULT` exists.
    """
    result = run("channels", "list")
    assert result.exit_code == exitcodes.NO_RESULT
    assert result.output.strip() == ""


def test_a_bad_flag_is_a_usage_error(run) -> None:  # noqa: ANN001
    """Click's own status, named in the table so it is complete."""
    assert run("contacts", "--no-such-flag").exit_code == exitcodes.USAGE


def test_a_bad_setting_key_is_a_plain_failure(run) -> None:  # noqa: ANN001
    """Retrying will not help: the caller has to change what it asked for."""
    result = run("config", "get", "nosuchkey")
    assert result.exit_code == exitcodes.FAILURE


def test_a_failure_says_so_on_stderr_and_never_on_stdout(run) -> None:  # noqa: ANN001
    """``program: what went wrong`` — so a caller parsing stdout never has to filter it."""
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
    """A missing confirmation *is* a bad argument, and it says so where errors go.

    These used to print a red refusal on stdout and exit 1 — a colour and a sentence that
    is not the command's answer, in whatever was reading the command's answer.
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
    ],
    ids=["contacts-sort", "records-category"],
)
def test_a_value_outside_a_closed_set_is_refused_rather_than_ignored(run, args, flag) -> None:  # noqa: ANN001
    """A typo in a closed-set option used to be answered with a different question.

    ``--sort bogus`` quietly sorted by the default, and ``--category long_hual`` quietly
    selected *every* discipline — which then came back as exit ``5``, the code that means
    a real empty result. A caller branching on ``5`` could not tell "there are no records"
    from "you misspelled the discipline", which is the worst answer a closed set can give.
    """
    result = run(*args)
    assert result.exit_code == exitcodes.USAGE
    assert result.stdout == ""
    assert flag in result.stderr


def test_a_profile_that_names_nothing_is_refused_before_anything_transmits(run) -> None:  # noqa: ANN001
    """``--profile`` is a claim about *which* radio, and an unkeepable one is not a default.

    An unknown name used to fall through to ordinary discovery, so a scheduled
    ``--profile yagi config advert`` with a typo transmitted from whichever companion
    happened to be attached — the one failure mode where being quietly wrong puts a packet
    on the air.
    """
    result = run("--profile", "nosuchprofile", "info")
    assert result.exit_code == exitcodes.USAGE
    assert result.stdout == ""
    assert "nosuchprofile" in result.stderr


def test_a_port_that_will_not_open_is_no_device_not_a_device_failure(run) -> None:  # noqa: ANN001
    """The two device statuses answer different questions, and this one answered wrong.

    ``3`` says nothing was transmitted — look at what is plugged in. ``4`` says the radio
    was reached and the operation failed, so a retry is reasonable. An unopenable
    ``--port`` reported ``4``, with the message "the connection to the device was lost"
    describing a connection there had never been. The manual's own ``case $?`` recipe
    branches on exactly this split.
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
        ("courier", "queue", "Nobody", "hi"),
        ("courier", "queue", "Alice", "hi", "--at", "25:99"),
        ("tx-optimize", "--path", "Yagi-Repeater", "--samples", "1"),
    ],
    ids=[
        "unknown-admin-node",
        "unknown-courier-contact",
        "bad-at-time",
        "tx-optimize-one-hop-path",
    ],
)
def test_a_bad_argument_is_never_reported_as_a_device_failure(run, args) -> None:  # noqa: ANN001
    """``4`` promises a caller that the radio answered and a retry is worth trying.

    Each of these is the *argument* being wrong, decided before a word goes over the air —
    so a caller that retried on ``4`` would retransmit nothing, forever. ``tx-optimize``
    already answered the identical question the identical way for a missing password;
    these are the ones that had not caught up — including its own one-hop ``--path``, which
    reported ``4`` for a path that never left the machine.
    """
    result = run(*args)
    assert result.exit_code == exitcodes.USAGE, result.output
    assert result.stdout == ""


def test_a_node_that_answers_no_is_still_a_device_failure(run) -> None:  # noqa: ANN001
    """The other side of the line: the radio was reached, so a retry is a real option.

    A refused admin login is not a bad argument — the password may simply have changed —
    and reclassifying the arguments above must not drag this one along with them.
    """
    result = run("repeater-admin", "Yagi-Repeater", "get", "name", "--password", "wrongpw")
    assert result.exit_code == exitcodes.DEVICE, result.output


def test_an_empty_result_writes_nothing_to_stdout_at_all(run) -> None:  # noqa: ANN001
    """Not a "no channels configured" line: the status carries it, so the pipe stays clean."""
    result = run("channels", "list")
    assert result.exit_code == exitcodes.NO_RESULT
    assert result.stdout == ""


def test_mock_and_an_explicit_radio_are_a_bad_command_line(run) -> None:  # noqa: ANN001
    """One flag says "pretend", the other names one exact radio, and both cannot be true.

    ``--mock`` used to win silently, so a scheduled ``--port COM7 --mock info`` reported on
    a simulator while reading as though it had reached the radio. Two contradictory claims
    about which device to talk to is a bad command line, not a precedence question.
    """
    from typer.testing import CliRunner

    from meshterm.cli import app

    for flag, value in (("--port", "COM7"), ("--ble", "AA:BB"), ("--tcp", "host:1")):
        result = CliRunner().invoke(app, ["--mock", flag, value, "info"])
        assert result.exit_code == exitcodes.USAGE, flag
        assert result.stdout == ""
        assert flag in result.stderr


def test_an_acknowledgement_reaches_the_person_without_reaching_the_pipe(run) -> None:  # noqa: ANN001
    """``ack`` used to be dropped on the CLI, and the reason only covered half the case.

    An acknowledgement is not the answer, and a caller redirecting stdout must catch only
    the answer — but stdout is not the only stream. stderr honours that rule exactly while
    giving a person watching a five-minute ``config restore`` the setting-by-setting ✓ they
    had no way to see.
    """
    result = run("config", "set", "tx_power", "20")
    assert result.exit_code == exitcodes.OK
    assert result.stdout == ""
    assert "tx_power" in result.stderr


def test_a_closing_message_goes_to_stderr_too(run) -> None:  # noqa: ANN001
    """A closing count is for a person, not a record for a parser.

    It restates what the listing above already showed and what ``$?`` already says, so it
    has no business in a redirect — and it is exactly what someone wants after a command
    that scrolled past them.
    """
    result = run("records")
    assert result.exit_code == exitcodes.NO_RESULT
    assert result.stdout == ""
    assert "records stored" in result.stderr


# -- the simulator's own history ------------------------------------------------------


def test_mock_records_into_a_database_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pretend radio gets a pretend history, so a demo run leaves the real mesh alone.

    The simulator is a fake radio, not a fake MeshTerm: what it adverts used to be written
    to ``meshterm.db`` like anything a companion said, and its invented contacts then sat
    in the mesh walk, the dashboard and the map of the mesh actually being run. The run
    still records — the history is how a ``--mock`` session's own tools read back what it
    heard — just not there.
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
    """``--db`` names one exact database, which is a claim the simulator does not override.

    Unlike ``--port``, it is not a contradictory one: where a run records and which radio
    it talks to are different questions, so this is a precedence rule and not a usage
    error. It is also what the suite's own ``run`` fixture depends on.
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
    """``--mock`` promises no real hardware, so the inventory is the simulator and no scan.

    Both discovery entry points are replaced with ones that fail loudly: the tool must not
    reach either. The one row it lists names ``--mock`` as its target, because that flag is
    how this device is selected — the same rule as ``--port``/``--ble`` for the others.
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
    """A picture has no scripted face; the located nodes stay in ``contacts``."""
    assert run("map").exit_code == exitcodes.USAGE


# -- help -----------------------------------------------------------------------------


def test_help_is_plain_and_names_every_exit_status(run) -> None:  # noqa: ANN001
    """Click's own help: no boxed panels, no colour, and the status table under it."""
    result = run("--help")
    assert not _ANSI.search(result.output)
    assert not set(result.output) & set("─│╭╮╰╯")
    for code in exitcodes.MEANINGS:
        assert f"{code} " in result.output

def test_every_command_help_is_plain_english(run) -> None:  # noqa: ANN001
    """No Sphinx or source-path markup in any ``--help`` page.

    ``platform`` and ``specimen`` used to dump their developer notes (backticks,
    ``:func:`` / ``:mod:`` roles) into the terminal. Every command's help should
    read like the others: a short plain description.
    """
    from meshterm.cli import app

    markup = re.compile(r":(?:func|mod|class|meth|attr|data|exc|obj):`")
    source_ticks = re.compile(r"``[^`]+``")
    pages = [("--help", run("--help"))]
    for cmd in app.registered_commands:
        name = cmd.name or cmd.callback.__name__
        pages.append((f"{name} --help", run(name, "--help")))
    for label, result in pages:
        assert result.exit_code == 0, label
        text = result.stdout
        assert not markup.search(text), f"{label} still has Sphinx roles:\n{text}"
        assert not source_ticks.search(text), f"{label} still has double-backtick markup:\n{text}"
        assert "meshterm/platforms.py" not in text
        assert "meshterm.ui.termfont" not in text
        assert "meshterm.ui.specimen" not in text
