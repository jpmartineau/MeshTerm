# SPDX-License-Identifier: Apache-2.0
"""No log ever holds a password or a secret: the log file, the run log, or the remote history.

JP, 2026-10-18: "NEVER log passwords or secrets!" The rule is a property of the places that
keep records, and not a habit of each tool. Thus the most important test here does not
look at the rules one by one. It runs each command that takes a secret, with a planted
value, through the real CLI at the DEBUG log level, and then searches the log file and the
``runs`` table for each planted value. A new command that takes a secret is covered when
it is added to that list. The other tests hold each rule and each shape on its own, so
that a failure says which rule broke.
"""

from __future__ import annotations

import io
import json
import logging
import sqlite3
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from meshterm.core.device_config import DeviceConfigError, get_spec, parse_value
from meshterm.core.models import Contact, Conversation
from meshterm.core.remote_store import RemoteStore
from meshterm.core.secrets import (
    SECRET_TEXT,
    Secret,
    hide,
    loggable,
    names_secret,
    remember,
    without_secret,
)
from meshterm.persistence import db
from meshterm.persistence.logging import configure_logging, get_logger, log_path
from meshterm.persistence.repository import Repository

#: Planted values. Each one is distinct, so a failure names the path that leaked it.
_PASSWORD = "plantedpw-7c1e"
_HEX = "5ec2e7a1" * 4


# --- names ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["password", "device_pin", "guest.password", "bridge.secret", "prv.key", "ble_pin"]
)
def test_a_name_with_a_secret_word_names_a_secret(name: str) -> None:
    """A secret word anywhere in the name, between any separators, makes the name secret."""
    assert names_secret(name)


@pytest.mark.parametrize("name", ["pinned", "spin", "public_key", "key_prefix", "name", "to"])
def test_a_name_that_only_contains_the_letters_is_not_secret(name: str) -> None:
    """A word must be whole: ``pinned`` is not ``pin``, and a public key is not a secret."""
    assert not names_secret(name)


# --- structures -----------------------------------------------------------------------


def test_each_shape_of_a_secret_in_the_parameters_is_hidden() -> None:
    """A key, a record, an operation, a dataclass field, a wrapper, and bytes: each is hidden."""
    params = {
        "room": "Lakeside",
        "password": _PASSWORD,
        "ops": [("set", "device_pin", 246813), ("import_key", Secret(_HEX))],
        "changes": [{"key": "device_pin", "previous": 135792, "value": 246813}],
        "set_channel": (5, "#lakeside", bytes.fromhex(_HEX)),
        "conversation": Conversation(
            label="#lakeside", is_channel=True, secret=bytes.fromhex(_HEX)
        ),
    }
    shown = json.dumps(loggable(params), default=str)
    for value in (_PASSWORD, _HEX, "246813", "135792", "xc2"):
        assert value not in shown
    assert "Lakeside" in shown  # what is not a secret stays
    assert "Conversation(label='#lakeside'" in shown
    assert "secret=<secret>" in shown


def test_a_list_of_setting_names_is_not_an_operation() -> None:
    """Only ``(verb, name, value)`` hides its value. A list of names has no value in it."""
    names = ["name", "device_pin", "radio_freq"]
    assert loggable({"settings": names}) == {"settings": names}
    assert loggable(("get", "device_pin")) == ("get", "device_pin")


def test_an_absent_secret_stays_absent() -> None:
    """A password that was not given is a fact that hides nothing. It stays ``None``."""
    assert loggable({"password": None, "secret": ""}) == {"password": None, "secret": ""}
    assert "secret=None" in str(loggable(Conversation(label="Bob", is_channel=False)))


# --- text -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("password hunter2-new", f"password {SECRET_TEXT}"),
        ("set guest.password hello there", f"set guest.password {SECRET_TEXT}"),
        ("set radio 869.5,250,11,5", "set radio 869.5,250,11,5"),
        (
            f"meshcore://channel/add?name=Lakeside&secret={_HEX}",
            f"meshcore://channel/add?name=Lakeside&secret={SECRET_TEXT}",
        ),
        (
            "Conversation(secret=b'\\x8b3\\x87', contact=None)",
            "Conversation(secret=<secret>, contact=None)",
        ),
        ("{'password': 'abcd1234'}", "{'password': <secret>}"),
        ('{"pin": "998877"}', '{"pin": <secret>}'),
        ("Conversation(secret=None)", "Conversation(secret=None)"),
        ("device_pin: must be 0 (no PIN) or six digits", None),
        ("BLE ProvidePin pairing for DD:F3:76:8B:0B:0B: status=19 ok=False", None),
    ],
)
def test_a_text_hides_each_secret_that_its_form_shows(text: str, expected: str | None) -> None:
    """A remote command, a URL, a ``repr``, and JSON all show which value is the secret."""
    assert hide(text) == (text if expected is None else expected)


def test_a_known_secret_is_hidden_in_any_text() -> None:
    """A registered secret is hidden where nothing names it: in an error, or inside a hex frame."""
    remember(_PASSWORD)
    remember(bytes.fromhex(_HEX))
    assert hide(f"login refused: {_PASSWORD} is wrong") == f"login refused: {SECRET_TEXT} is wrong"
    frame = "Unknown stats type: 66, data: 1842" + _HEX.upper() + "00ff"
    assert _HEX.upper() not in hide(frame)


def test_a_short_or_common_value_is_not_registered() -> None:
    """A value of three characters, or a secret word, would hide ordinary text everywhere."""
    remember("abc")
    remember("password")
    assert hide("abc password") == "abc password"


def test_a_registered_pin_does_not_hide_part_of_a_longer_number() -> None:
    """A PIN is found as a whole word only, so it cannot cut a timestamp or a count."""
    remember(246813)
    assert hide("pin 246813 and 12468139") == f"pin {SECRET_TEXT} and 12468139"


# --- the remote command line ----------------------------------------------------------


def test_the_remote_history_keeps_the_command_without_its_secret(tmp_path: Path) -> None:
    """↑ recalls ``password ``, ready to type, and the file never holds the password."""
    store = RemoteStore(tmp_path / "remote.json")
    node = Contact(name="Hilltop", public_key="3d" * 32)
    store.append_history(node, f"password {_PASSWORD}")
    store.append_history(node, f"set guest.password {_PASSWORD}")
    store.append_history(node, "set name Hilltop")
    assert store.history(node) == ["password ", "set guest.password ", "set name Hilltop"]
    assert _PASSWORD not in (tmp_path / "remote.json").read_text(encoding="utf-8")
    assert without_secret("ver") == "ver"


# --- the device settings --------------------------------------------------------------


def test_a_refused_pin_is_not_repeated_in_the_error() -> None:
    """The error of a secret setting states the rule. Each other setting still shows the value."""
    with pytest.raises(DeviceConfigError) as refused:
        parse_value(get_spec("device_pin"), "24681x")
    assert "24681x" not in str(refused.value)
    with pytest.raises(DeviceConfigError) as short:
        parse_value(get_spec("device_pin"), "24681")
    assert "24681" not in str(short.value)
    with pytest.raises(DeviceConfigError) as other:
        parse_value(get_spec("radio_sf"), "13")
    assert "13" in str(other.value)


# --- the places that keep records -----------------------------------------------------


def test_neither_log_handler_writes_a_secret(tmp_path: Path) -> None:
    """The file, its traceback, and the console all get the hidden form of a record."""
    console = Console(file=io.StringIO(), width=200, record=True, color_system=None)
    configure_logging(console, tmp_path, file_level=logging.DEBUG, level=logging.DEBUG)
    log = get_logger("core.connection")
    log.debug("run 1 start: params=%s", {"password": _PASSWORD})
    try:
        raise ValueError(f"login with {_PASSWORD} refused")
    except ValueError:
        log.exception("login failed")
    for handler in get_logger().handlers:
        handler.flush()
    written = log_path(tmp_path).read_text(encoding="utf-8")
    assert _PASSWORD not in written
    assert SECRET_TEXT in written
    assert _PASSWORD not in console.export_text()


def test_the_run_log_stores_no_secret(tmp_path: Path) -> None:
    """The arguments and the summary of a run go into the ``runs`` table hidden."""
    repo = Repository(tmp_path / "test.db")
    run = repo.start_run("rooms", {"cli_action": "join", "password": _PASSWORD})
    repo.finish_run(run, "error", {"error": f"refused {_PASSWORD}"})
    repo.close()
    conn = sqlite3.connect(tmp_path / "test.db")
    args, summary = conn.execute("SELECT args_json, summary_json FROM runs").fetchone()
    conn.close()
    assert _PASSWORD not in args + summary
    assert SECRET_TEXT in args


def test_the_migration_hides_the_secrets_that_older_versions_stored(tmp_path: Path) -> None:
    """A database at version 19 loses each stored secret, one time, when it opens."""
    path = tmp_path / "old.db"
    db.connect(path).close()
    conn = sqlite3.connect(path)
    conversation = "Conversation(label='#x', is_channel=True, secret=b'\\x5e\\xc2', contact=None)"
    leaked = json.dumps({"conversation": conversation})
    conn.execute(
        "INSERT INTO runs (tool, args_json, summary_json, status, started_at) "
        "VALUES ('chat', ?, ?, 'ok', '2026-10-01T00:00:00')",
        (leaked, json.dumps({"password": _PASSWORD})),
    )
    conn.execute("UPDATE schema_meta SET value = '19' WHERE key = 'version'")
    conn.commit()
    conn.close()

    db.connect(path).close()
    conn = sqlite3.connect(path)
    args, summary = conn.execute("SELECT args_json, summary_json FROM runs").fetchone()
    conn.close()
    assert "\\\\x5e" not in args and "secret=<secret>" in args
    assert _PASSWORD not in summary


# --- the whole program ----------------------------------------------------------------

#: Each command that takes a secret, with a planted value in it, and the planted values.
#: The commands run against the simulator. Some of them fail (a room that the simulator
#: does not have), and that is fine: a failure is logged too, and with the parameters.
_COMMANDS: list[tuple[list[str], tuple[str, ...]]] = [
    (["repeater-admin", "Yagi-Repeater", "ver", "--password", "plantedpw-a1"], ("plantedpw-a1",)),
    (["repeater-admin", "Yagi-Repeater", "password", "plantedpw-a2"], ("plantedpw-a2",)),
    (
        ["repeater-admin", "Yagi-Repeater", "set", "guest.password", "plantedpw-a3"],
        ("plantedpw-a3",),
    ),
    (["rooms", "join", "Lakeside BBS", "--password", "plantedpw-r1"], ("plantedpw-r1",)),
    (
        ["tx-optimize", "--path", "a1,b2", "--min", "20", "--max", "20", "--samples", "1"]
        + ["--no-apply", "--password", "plantedpw-t1"],
        ("plantedpw-t1",),
    ),
    (["channels", "add", "5", "Planted", "--secret", "a1" * 16], ("a1" * 16,)),
    (["channels", "join", "6", "Planted2", "b2" * 16], ("b2" * 16,)),
    (
        ["channels", "import", "7", "meshcore://channel/add?name=P3&secret=" + "c3" * 16],
        ("c3" * 16,),
    ),
    (["config", "channel", "5", "Planted4", "--secret", "d4" * 16], ("d4" * 16,)),
    (["config", "set", "device_pin", "246813"], ("246813",)),
    (["config", "set", "device_pin", "97531"], ("97531",)),
]


def test_no_command_writes_a_planted_secret_to_any_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each command with a secret, at the DEBUG level: no planted value in the log or the runs.

    This is the test that holds the promise. The rules above can each be correct and a
    path between them can still leak, as the parameters of a chat run did. Thus this test
    asks the two places that keep records, and not the rules.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("MESHTERM_HOME", str(home))
    runner = CliRunner()
    assert runner.invoke(app_(), ["preferences", "set", "log_level", "DEBUG"]).exit_code == 0
    planted: list[str] = []
    for args, values in _COMMANDS:
        runner.invoke(app_(), ["--mock", *args])
        planted.extend(values)

    for handler in get_logger().handlers:
        handler.flush()
    log = log_path(home).read_text(encoding="utf-8")
    assert log.count("start: tool=") >= len(_COMMANDS), "the commands did not run"
    conn = sqlite3.connect(home / "meshterm-mock.db")
    runs = "\n".join(
        f"{args} {summary}"
        for args, summary in conn.execute("SELECT args_json, summary_json FROM runs")
    )
    conn.close()
    for value in planted:
        assert value not in log, f"{value} is in the log"
        assert value not in runs, f"{value} is in the runs table"


def app_():
    """The CLI application, imported late so that the environment of the test applies."""
    from meshterm.cli import app

    return app
