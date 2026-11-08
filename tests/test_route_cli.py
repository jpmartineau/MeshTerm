# SPDX-License-Identifier: Apache-2.0
"""Tests for ``meshterm route``: the device routes, on both faces of the command line.

Each test runs the real Typer app against the simulator, in a config directory of its own.
The simulator forgets what it was told at the end of each run, but the kept routes are in
the outbound store on disk. Thus a test that needs a route to persist between two runs
keeps it, and reads it back with the next run.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from meshterm.core import exitcodes
from meshterm.core.connection import MockDevice

ALICE = "d4e5f6a7" + "0" * 56


@pytest.fixture()
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201 - a closure
    """Run the real CLI against the simulator, with the whole config directory in tmp_path."""
    from meshterm.cli import app

    monkeypatch.setenv("MESHTERM_HOME", str(tmp_path / "home"))
    runner = CliRunner()

    def invoke(*args: str):  # noqa: ANN202
        return runner.invoke(app, ["--mock", "--db", str(tmp_path / "test.db"), *args])

    return invoke


@pytest.fixture()
def with_site(run) -> None:  # noqa: ANN001
    """Make a site and make it active, so that a route has a record to be kept in."""
    assert run("outbound", "set", "Home", "3d63c6429436").exit_code == exitcodes.OK


@pytest.fixture()
def flooding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the route of each simulated contact away, so that the radio floods to all."""
    original = MockDevice.get_contacts

    async def bare(self):  # noqa: ANN001, ANN202
        return [replace(c, route_hops=None) for c in await original(self)]

    monkeypatch.setattr(MockDevice, "get_contacts", bare)


def _document(result) -> object:  # noqa: ANN001
    """The one JSON document on stdout."""
    (line,) = result.stdout.splitlines()
    return json.loads(line)


def _facts(result) -> dict[str, str]:  # noqa: ANN001
    """The plain ``route show`` block as a dictionary (the first word is the key)."""
    return dict(line.split(None, 1) for line in result.stdout.splitlines() if " " in line)


# -- list and show -----------------------------------------------------------------------


def test_list_has_the_headings_a_script_looks_for(run) -> None:  # noqa: ANN001
    """The headings are the contract of the plain face. The route is the last column."""
    result = run("route", "list")
    assert result.exit_code == exitcodes.OK, result.output
    header, *rows = result.stdout.splitlines()
    assert header.split() == ["NAME", "TYPE", "HASH", "PATH", "ORIGIN", "KEPT", "ROUTE"]
    assert len(rows) == 5
    assert run("route").stdout == result.stdout, "no subcommand means list"


def test_list_in_json_is_an_array_of_typed_rows(run) -> None:  # noqa: ANN001
    """A flood is ``null``, no relay is ``""``, and a route draws both ends."""
    rows = _document(run("--json", "route", "list"))
    by_name = {row["node"]["name"]: row for row in rows}
    assert by_name["Alice"]["path"] == "a1b2c3d4"
    assert by_name["Yagi-Repeater"]["path"] == ""
    assert by_name["Alice"]["kept"] is None
    alice = by_name["Alice"]["route"]
    assert [hop["self"] for hop in alice] == [True, False, False]
    assert alice[-1]["key"] == ALICE


def test_a_neighbour_is_direct_in_plain_and_empty_in_json(run) -> None:  # noqa: ANN001
    """The empty spec is a real answer. The plain face has a word for it."""
    plain = run("route", "list").stdout
    line = next(row for row in plain.splitlines() if row.startswith("Yagi-Repeater"))
    assert "direct" in line.split()


def test_list_with_nothing_is_nothing_to_report(run, flooding) -> None:  # noqa: ANN001
    """No route and nothing kept: exit status 5, with the empty document on stdout."""
    plain = run("route", "list")
    assert plain.exit_code == exitcodes.NO_RESULT
    machine = run("--json", "route", "list")
    assert machine.exit_code == exitcodes.NO_RESULT
    assert _document(machine) == []


def test_show_states_the_route_and_where_it_comes_from(run) -> None:  # noqa: ANN001
    """The facts block has the node, the spec, the origin, and the drawn route."""
    result = run("route", "show", "Alice")
    assert result.exit_code == exitcodes.OK, result.output
    facts = _facts(result)
    assert facts["node"] == "Alice"
    assert facts["path"] == "a1b2c3d4"
    assert facts["origin"] == "learned"
    assert facts["kept"] == "-"
    assert "→" in facts["route"]
    assert "★" not in facts["route"]
    document = _document(run("--json", "route", "show", "Alice"))
    assert document["origin"] == "learned"
    assert document["kept_by"] is None
    assert document["skipped"] is None
    assert document["first_hop_heard_at"] is None


def test_show_takes_the_start_of_a_key(run) -> None:  # noqa: ANN001
    """A contact is a name or the first digits of its key."""
    document = _document(run("--json", "route", "show", "d4e5"))
    assert document["node"]["name"] == "Alice"


def test_an_unknown_contact_is_a_usage_error(run) -> None:  # noqa: ANN001
    """MeshTerm decides this before it sends anything, so it is never a device failure."""
    for args in (("show", "Nobody"), ("forget", "Nobody", "--yes"), ("discover", "Nobody")):
        result = run("route", *args)
        assert result.exit_code == exitcodes.USAGE, args
        assert result.stdout == ""
        assert "Nobody" in result.stderr


# -- set and forget ----------------------------------------------------------------------


def test_set_writes_a_route_and_shows_it(run) -> None:  # noqa: ANN001
    """The answer is the route as it is afterwards. The ✓ line is on stderr."""
    result = run("route", "set", "Alice", "--path", "b2")
    assert result.exit_code == exitcodes.OK, result.output
    facts = _facts(result)
    assert facts["path"] == "b2"
    assert facts["origin"] == "set"
    assert "set to b2" in result.stderr
    assert "set to" not in result.stdout


def test_set_with_an_empty_path_means_no_relay(run) -> None:  # noqa: ANN001
    """``--path ""`` is a neighbour. It is ``""`` in JSON, and exactly that comes back in."""
    document = _document(run("--json", "route", "set", "Alice", "--path", ""))
    assert document["path"] == ""
    assert document["origin"] == "set"
    assert [hop["self"] for hop in document["route"]] == [True, False]
    again = _document(run("--json", "route", "set", "Alice", "--path", document["path"]))
    assert again["path"] == ""


def test_a_path_in_the_output_goes_back_into_set(run) -> None:  # noqa: ANN001
    """The spec of ``show`` is the spec that ``set`` takes, with nothing added."""
    first = _document(run("--json", "route", "set", "Alice", "--path", "b2,a1"))
    assert first["path"] == "b2,a1"
    again = _document(run("--json", "route", "set", "Alice", "--path", first["path"]))
    assert again["path"] == first["path"]


@pytest.mark.parametrize("spec", ["zz", "a1,a1", "a", ",,x"], ids=["not-hex", "twice", "odd", "x"])
def test_a_bad_spec_is_a_usage_error(run, spec) -> None:  # noqa: ANN001
    """A spec that is not hops in hex is refused before the radio is touched."""
    result = run("route", "set", "Alice", "--path", spec)
    assert result.exit_code == exitcodes.USAGE
    assert result.stdout == ""
    assert "is not a route" in result.stderr


def test_a_route_that_the_radio_cannot_hold_is_a_usage_error(run) -> None:  # noqa: ANN001
    """65 hops of one byte do not fit in 64 bytes. Nothing was written, so it is not exit 4."""
    spec = ",".join(f"{i:02x}" for i in range(65))
    result = run("route", "set", "Alice", "--path", spec)
    assert result.exit_code == exitcodes.USAGE
    assert result.stdout == ""


def test_set_needs_a_path(run) -> None:  # noqa: ANN001
    """The route is the point of the command."""
    assert run("route", "set", "Alice").exit_code == exitcodes.USAGE


def test_forget_without_yes_is_a_silent_usage_error(run) -> None:  # noqa: ANN001
    """It clears what the radio holds, so it asks for the usual confirmation."""
    result = run("route", "forget", "Alice")
    assert result.exit_code == exitcodes.USAGE
    assert result.stdout == ""
    assert "--yes" in result.stderr


def test_forget_makes_the_next_message_flood(run) -> None:  # noqa: ANN001
    """After a forget, there is no route: ``null`` in JSON, and the origin is ``flood``."""
    result = run("--json", "route", "forget", "Alice", "--yes")
    assert result.exit_code == exitcodes.OK, result.output
    document = _document(result)
    assert document["path"] is None
    assert document["route"] is None
    assert document["origin"] == "flood"
    plain = _facts(run("route", "forget", "Alice", "--yes"))
    assert plain["path"] == "-" and plain["origin"] == "flood"


# -- keep and unkeep ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ("keep", "Alice", "--path", "a1"),
        ("keep", "Alice"),
        ("set", "Alice", "--path", "a1", "--keep"),
    ],
    ids=["keep-path", "keep-radio-route", "set-keep"],
)
def test_keep_without_a_site_fails_and_names_the_way_out(run, tmp_path, args) -> None:  # noqa: ANN001
    """No site and no fixed device: exit 1, a sentence on stderr, nothing on stdout."""
    result = run("route", *args)
    assert result.exit_code == exitcodes.FAILURE
    assert result.stdout == ""
    assert "meshterm outbound use" in result.stderr
    machine = run("--json", "route", *args)
    assert machine.exit_code == exitcodes.FAILURE
    assert machine.stdout == ""
    stored = tmp_path / "home" / "outbound.json"
    assert not stored.exists() or "d4e5f6a7" not in stored.read_text(encoding="utf-8")


def test_keep_remembers_the_route_for_the_active_site(run, with_site) -> None:  # noqa: ANN001
    """The route is kept for Home, and the next run shows it."""
    result = run("route", "keep", "Alice", "--path", "a1")
    assert result.exit_code == exitcodes.OK, result.output
    assert "Home" in result.stderr
    document = _document(run("--json", "route", "show", "Alice"))
    assert document["kept"] == "a1"
    assert document["kept_by"] == {"source": "site", "name": "Home"}
    assert document["kept_in_radio"] is True
    assert document["skipped"] is False
    assert document["origin"] == "kept"
    plain = _facts(run("route", "show", "Alice"))
    assert plain["kept_by"] == "site Home"


def test_keep_with_no_path_keeps_the_route_the_radio_holds(run, with_site) -> None:  # noqa: ANN001
    """The simulator holds ``a1b2c3d4`` for Alice, and that route is kept as it stands."""
    assert run("route", "keep", "Alice").exit_code == exitcodes.OK
    assert _document(run("--json", "route", "show", "Alice"))["kept"] == "a1b2c3d4"


def test_keep_with_no_path_and_no_route_is_a_usage_error(run, with_site, flooding) -> None:  # noqa: ANN001
    """There is nothing to keep, so the user must give a path."""
    result = run("route", "keep", "Alice")
    assert result.exit_code == exitcodes.USAGE
    assert result.stdout == ""
    assert "--path" in result.stderr


def test_set_with_keep_writes_and_keeps(run, with_site) -> None:  # noqa: ANN001
    """One command does both: the radio gets the route and the site keeps it."""
    result = run("--json", "route", "set", "Observer-Bot", "--path", "b2", "--keep")
    assert result.exit_code == exitcodes.OK, result.output
    document = _document(result)
    assert document["path"] == "b2" and document["kept"] == "b2"
    listed = _document(run("--json", "route", "list"))
    kept = {row["node"]["name"]: row["kept"] for row in listed}
    assert kept["Observer-Bot"] == "b2" and kept["Alice"] is None


def test_a_kept_route_with_no_relay_stays_empty(run, with_site) -> None:  # noqa: ANN001
    """A kept neighbour route is ``""`` and not an absent route."""
    assert run("route", "keep", "Alice", "--path", "").exit_code == exitcodes.OK
    document = _document(run("--json", "route", "show", "Alice"))
    assert document["kept"] == ""
    assert _facts(run("route", "show", "Alice"))["kept"] == "direct"


def test_a_kept_route_that_is_not_on_the_radio_is_listed(run, with_site, flooding) -> None:  # noqa: ANN001
    """A contact with a kept route is in the list when the radio holds no route to it."""
    assert run("route", "keep", "Alice", "--path", "a1").exit_code == exitcodes.OK
    rows = _document(run("--json", "route", "list"))
    assert [row["node"]["name"] for row in rows] == ["Alice"]
    assert rows[0]["path"] is None and rows[0]["kept"] == "a1"
    assert rows[0]["origin"] == "flood"


def test_forget_leaves_the_kept_route_kept(run, with_site) -> None:  # noqa: ANN001
    """The user forgets what the radio holds, and not the choice."""
    run("route", "keep", "Alice", "--path", "a1")
    result = run("--json", "route", "forget", "Alice", "--yes")
    document = _document(result)
    assert document["path"] is None and document["kept"] == "a1"
    assert "stays kept" in result.stderr


def test_unkeep_drops_the_kept_route(run, with_site) -> None:  # noqa: ANN001
    """The route stays in the radio. Only the memory is gone."""
    run("route", "keep", "Alice", "--path", "a1")
    result = run("--json", "route", "unkeep", "Alice")
    assert result.exit_code == exitcodes.OK, result.output
    assert _document(result)["kept"] is None
    assert _document(run("--json", "route", "show", "Alice"))["kept"] is None


def test_unkeep_of_nothing_is_nothing_to_report(run, with_site) -> None:  # noqa: ANN001
    """No route was kept: exit status 5, and the document is still printed."""
    plain = run("route", "unkeep", "Alice")
    assert plain.exit_code == exitcodes.NO_RESULT
    machine = run("--json", "route", "unkeep", "Alice")
    assert machine.exit_code == exitcodes.NO_RESULT
    assert _document(machine)["kept"] is None


# -- discover ----------------------------------------------------------------------------


def test_discover_shows_the_route_each_way(run) -> None:  # noqa: ANN001
    """The answer has both specs and both drawn routes. The back route ends at our node."""
    result = run("route", "discover", "Alice")
    assert result.exit_code == exitcodes.OK, result.output
    facts = _facts(result)
    assert facts["answered"] == "yes" and facts["used"] == "no"
    assert facts["out_path"] and facts["in_path"]
    assert facts["out_route"].startswith("MockCompanion")
    assert facts["in_route"].startswith("Alice")
    document = _document(run("--json", "route", "discover", "Alice"))
    assert document["answered"] is True and document["used"] is False
    assert document["in_route"][0]["name"] == "Alice"
    assert document["in_route"][-1]["self"] is True


def test_discover_does_not_change_the_route_unless_asked(run) -> None:  # noqa: ANN001
    """Without ``--use``, no route is written. With it, the outward route is set."""
    plain = run("--json", "route", "discover", "Alice")
    assert "set to" not in plain.stderr
    used = run("--json", "route", "discover", "Alice", "--use")
    assert used.exit_code == exitcodes.OK, used.output
    assert _document(used)["used"] is True
    assert "outward route" in used.stderr


def test_discover_without_an_answer_is_nothing_to_report(
    run,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The discovery went out and nothing came back: exit status 5, nulls in the document."""

    async def silent(self, node):  # noqa: ANN001, ANN202
        return None

    monkeypatch.setattr(MockDevice, "discover_route", silent)
    plain = run("route", "discover", "Alice", "--use")
    assert plain.exit_code == exitcodes.NO_RESULT
    machine = run("--json", "route", "discover", "Alice", "--use")
    assert machine.exit_code == exitcodes.NO_RESULT
    document = _document(machine)
    assert document["answered"] is False and document["used"] is False
    assert document["out_path"] is None and document["out_route"] is None


# -- sync --------------------------------------------------------------------------------


def test_sync_with_nothing_to_do_says_so(run) -> None:  # noqa: ANN001
    """Every count is zero, and the document has every key."""
    result = run("--json", "route", "sync")
    assert result.exit_code == exitcodes.OK, result.output
    assert _document(result) == {
        "site": None,
        "saved": [],
        "restored": 0,
        "kept_written": 0,
        "kept_skipped": [],
        "failed": [],
    }
    assert "in line" in result.stderr


def test_sync_writes_a_kept_route_the_radio_lost(run, with_site) -> None:  # noqa: ANN001
    """The simulator starts each run with its own routes. A kept route that differs is written."""
    assert run("route", "keep", "Alice", "--path", "b2").exit_code == exitcodes.OK
    result = run("--json", "route", "sync")
    assert result.exit_code == exitcodes.OK, result.output
    document = _document(result)
    assert document["site"] == "Home" and document["kept_written"] == 1
    assert "Wrote back 1 route you keep" in result.stderr
    assert _facts(run("route", "sync"))["site"] == "Home"


# -- the tool itself ---------------------------------------------------------------------


def test_the_tool_has_no_menu_row() -> None:
    """The menu works on a route from the node page of the contact."""
    from meshterm.tools import get_tool

    tool = get_tool("route")
    assert tool is not None and tool.menu_visible is False
