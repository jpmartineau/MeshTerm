# SPDX-License-Identifier: Apache-2.0
"""A repeater's region table: the dump parser, the commands, the editor, the node page.

The parser is pinned against dumps spelled the way ``RegionMap::printChildRegions`` spells
them — including the ones the 160-byte reply buffer cuts mid-name — and against the
simulator (:mod:`meshterm.core.region_sim`), which transcribes the firmware's CLI. Every
editor action is checked for the one command it sends, and the table it leaves behind is
checked against what the repeater itself would dump next.
"""

from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.text import Text

from meshterm.core.connection import DeviceCommandError, MockDevice
from meshterm.core.models import Contact
from meshterm.core.region_admin import (
    REPLY_CAP_BYTES,
    RegionTable,
    allow_command,
    default_command,
    deny_command,
    dump_may_be_cut,
    home_command,
    list_may_be_incomplete,
    parse_default_reply,
    parse_name_list,
    parse_region_dump,
    put_command,
    region_refused,
    remove_command,
    saves_table,
    validate_new_name,
)
from meshterm.core.region_sim import SimulatedRegionMap
from meshterm.core.region_store import RegionStore
from meshterm.core.regions import RegionNameError
from meshterm.core.remote_config import known_commands
from meshterm.ui import region_editor
from meshterm.ui.region_editor import RegionMenu, RegionPick, region_items

NODE = Contact(name="Yagi-Repeater", public_key="a1" * 32, key_prefix="a1b2c3d4", node_type=2)
BIG = Contact(name="Local-Repeater", public_key="b2" * 32, key_prefix="b2c3d4e5", node_type=2)

#: A small table, dumped exactly as the firmware prints it.
SMALL_DUMP = "*^ F\n lakeside F\n  lakeside-north F\n  lakeside-south\n harbour F\n"

_TREE = [
    ("lakeside", "*", True),
    ("lakeside-north", "lakeside", True),
    ("lakeside-south", "lakeside", False),
    ("harbour", "*", True),
]


def _rows(table: RegionTable) -> list[tuple]:
    return [(r.name, r.parent, r.depth, r.flood, r.home, r.placed) for r in table.rows]


# --- the dump --------------------------------------------------------------------------


def test_a_dump_reads_as_a_tree_with_its_flags() -> None:
    """Indentation is the parent, ``F`` is flood allowed, ``^`` is home."""
    table = parse_region_dump(SMALL_DUMP)
    assert _rows(table) == [
        ("*", None, 0, True, True, True),
        ("lakeside", "*", 1, True, False, True),
        ("lakeside-north", "lakeside", 2, True, False, True),
        ("lakeside-south", "lakeside", 2, False, False, True),
        ("harbour", "*", 1, True, False, True),
    ]
    assert not table.cut and table.complete
    assert table.home == "*"
    assert [r.name for r in table.children("lakeside")] == ["lakeside-north", "lakeside-south"]
    assert table.carried() == ["*", "lakeside", "lakeside-north", "harbour"]


def test_a_home_region_and_a_denied_wildcard() -> None:
    """A region marked ``^`` is home; a wildcard without ``F`` relays no unscoped floods."""
    table = parse_region_dump("*\n yul^ F\n  yul-east\n")
    assert table.home == "yul"
    assert table.wildcard is not None and not table.wildcard.flood
    assert table.carried() == ["yul"]


def test_the_dump_survives_a_transport_that_stripped_its_last_newline() -> None:
    """A short reply without its trailing newline is still whole — it never reached the cap."""
    assert _rows(parse_region_dump(SMALL_DUMP.rstrip("\n"))) == _rows(parse_region_dump(SMALL_DUMP))
    assert not parse_region_dump(SMALL_DUMP.rstrip("\n")).cut


def test_a_dump_at_the_cap_is_cut_and_its_half_line_dropped() -> None:
    """The buffer stops mid-name; that name must not become a region with half a name.

    The last line here reads ``riverside-upper`` with no ``F`` — as a region it would be a
    denied one called by a prefix of its name. It is dropped and the table says where the
    tree stopped.
    """
    sim = SimulatedRegionMap(
        [
            *_TREE,
            ("harbour-east", "harbour", True),
            ("harbour-west", "harbour", True),
            ("old-town", "*", False),
            ("old-town-market", "old-town", True),
            ("riverside", "*", True),
            ("riverside-upper", "riverside", True),
            ("hilltop", "*", True),
        ]
    )
    dump = sim.command("region")
    assert len(dump.encode()) == REPLY_CAP_BYTES - 1 and not dump.endswith("\n")
    table = parse_region_dump(dump)
    assert table.cut and not table.complete
    assert table.cut_after == "riverside"
    assert table.get("riverside-upper") is None and table.get("hilltop") is None


def test_a_cut_line_that_ended_on_its_newline_is_kept() -> None:
    """Near the cap but ending on a newline: the last line is whole, a next one may be lost."""
    dump = SMALL_DUMP
    while len(dump.encode()) < REPLY_CAP_BYTES - 4:
        dump += f" r{len(dump):03d} F\n"
    assert dump_may_be_cut(dump) and dump.endswith("\n")
    table = parse_region_dump(dump)
    assert table.cut
    assert table.rows[-1].name == dump.rstrip("\n").rsplit("\n", 1)[-1].split()[0]


def test_a_cut_inside_a_multibyte_name_still_counts_as_cut() -> None:
    """A UTF-8 name split by the buffer decodes a few bytes short — still read as cut."""
    dumps = (
        SimulatedRegionMap([(f"{i:02d}" + "é" * k, "*", True) for i in range(20)]).command("region")
        for k in range(1, 12)
    )
    # Slide the name length until the cap lands inside an "é" and the decode loses a byte.
    dump = next(d for d in dumps if len(d.encode()) < REPLY_CAP_BYTES - 1)
    table = parse_region_dump(dump)
    assert table.cut and not dump.endswith("\n")
    assert len({len(r.name) for r in table.regions()}) == 1  # no half-name among them


def test_a_line_that_breaks_the_grammar_ends_the_tree_there() -> None:
    """A dump that stops making sense stops being the dump: no guessing, marked cut."""
    table = parse_region_dump("*^ F\n lakeside F\n   skipped-a-level F\n harbour F\n")
    assert [r.name for r in table.rows] == ["*", "lakeside"]
    assert table.cut and table.cut_after == "lakeside"


def test_an_empty_reply_is_an_empty_table() -> None:
    """No reply, no rows — and nothing to call cut."""
    assert parse_region_dump("").rows == ()
    assert parse_region_dump(None).rows == ()


# --- the flat lists and the default ----------------------------------------------------


def test_the_flat_lists_recover_what_the_cut_hid() -> None:
    """Names past the cut join unplaced, flooded as their list says; the tree keeps its own."""
    table = replace(parse_region_dump("*^ F\n lakeside F\n"), cut=True, cut_after="lakeside")
    assert not table.complete
    table = table.with_lists("*,lakeside,lakeside-north,hilltop", "old-town")
    assert _rows(table)[2:] == [
        ("lakeside-north", None, 1, True, False, False),
        ("hilltop", None, 1, True, False, False),
        ("old-town", None, 1, False, False, False),
    ]
    assert table.lists_read and not table.lists_partial and table.complete
    assert table.carried() == ["*", "lakeside", "lakeside-north", "hilltop"]


def test_a_list_near_its_cap_may_have_skipped_a_name() -> None:
    """The firmware skips a name that won't fit and carries on — so a long list is flagged."""
    names = [f"region-number-{i:02d}" for i in range(12)]
    sim = SimulatedRegionMap([(n, "*", True) for n in names])
    listed = sim.command("region list allowed")
    assert parse_name_list(listed) != ["*", *names]  # some were skipped
    assert list_may_be_incomplete(listed)
    assert not list_may_be_incomplete("*,lakeside,harbour")
    assert parse_name_list("-none-") == []


def test_the_default_scope_reply() -> None:
    """``region default`` names the region, ``<null>``, or is not understood."""
    assert parse_default_reply(" default scope is harbour") == "harbour"
    assert parse_default_reply(" default scope is now harbour") == "harbour"
    assert parse_default_reply(" default scope is <null>") == ""
    assert parse_default_reply("Err - ??") is None  # pre-1.15 has no such query
    assert parse_default_reply(None) is None
    table = parse_region_dump(SMALL_DUMP)
    assert not table.with_default("Err - ??").default_known
    known = table.with_default(" default scope is harbour")
    assert known.default_known and known.default == "harbour"


def test_refusals_are_the_region_verbs_own_words() -> None:
    """``Err - …`` is a refusal; a reply naming a region is not."""
    for reply in ("Err - not empty", "Err - unknown region", "Err - ??", "Unknown command"):
        assert region_refused(reply)
    for reply in ("OK", "OK - (flood allowed)", " home is now harbour", "lakeside,harbour"):
        assert not region_refused(reply)
    assert region_refused(None)


# --- the commands ----------------------------------------------------------------------


def test_every_edit_is_one_command_spelled_once() -> None:
    """The exact text each editor action sends."""
    assert allow_command("lakeside") == "region allowf lakeside"
    assert deny_command("*") == "region denyf *"
    assert home_command("harbour") == "region home harbour"
    assert default_command("harbour") == "region default harbour"
    assert default_command(None) == "region default <null>"
    assert put_command("bay") == "region put bay"
    assert put_command("bay", "*") == "region put bay"
    assert put_command("bay", "harbour") == "region put bay harbour"
    assert remove_command("bay") == "region remove bay"


def test_saving_is_region_save_or_a_default_that_saved_itself() -> None:
    """Only a save, or a 1.15+ default change, clears the unsaved count."""
    assert saves_table("region save", "OK")
    assert not saves_table("region save", "Err - save failed")
    assert saves_table("region default harbour", " default scope is now harbour")
    assert not saves_table("region allowf harbour", "OK")


def test_a_new_name_follows_the_firmware_and_never_moves_a_region() -> None:
    """A put of a taken name would move it; punctuation the firmware refuses is refused first."""
    assert validate_new_name(" bay-2 ") == "bay-2"
    with pytest.raises(RegionNameError, match="already"):
        validate_new_name("harbour", ["*", "harbour"])  # region put would move it
    with pytest.raises(RegionNameError, match="letters, digits"):
        validate_new_name("old.town")
    with pytest.raises(RegionNameError):
        validate_new_name("$secret")
    with pytest.raises(RegionNameError):
        validate_new_name("*")


def test_the_command_line_completes_the_list_and_def_verbs_but_not_load() -> None:
    """``region load`` reads lines until a blank one — no remote command line can send that."""
    commands = known_commands()
    assert {"region list allowed", "region list denied", "region def "} <= set(commands)
    assert not any(c.startswith("region load") for c in commands)


# --- the edits fold into the table the way the firmware applies them -------------------


def test_each_accepted_edit_leaves_the_table_the_repeater_would_dump() -> None:
    """Random edits against the simulator: the folded table equals a fresh dump, every time.

    This is the claim the editor rests on — it redraws from a reply instead of re-reading —
    so it is checked against the firmware transcription over many sequences rather than
    one example of each verb.
    """
    rng = random.Random(7)
    pool = ["lakeside", "lakeside-north", "lakeside-south", "harbour", "bay", "cove", "*"]
    for _ in range(60):
        sim = SimulatedRegionMap(_TREE)
        table = parse_region_dump(sim.command("region")).with_default(sim.command("region default"))
        for _ in range(8):
            name = rng.choice(pool)
            known = [r.name for r in table.rows]
            verb = rng.choice(["allow", "deny", "home", "default", "put", "remove"])
            if verb == "allow":
                command = allow_command(name)
            elif verb == "deny":
                command = deny_command(name)
            elif verb == "home":
                command = home_command(name)
            elif verb == "default":
                if name == "*":
                    continue
                command = default_command(name if rng.random() < 0.8 else None)
            elif verb == "put":
                if name in known or name == "*":
                    continue  # the editor refuses these (a put would move the region)
                command = put_command(name, rng.choice([n for n in known]))
            else:
                if name == "*" or name not in known:
                    continue
                command = remove_command(name)
            reply = sim.command(command)
            table = table.after(command, reply)
            fresh = parse_region_dump(sim.command("region")).with_default(
                sim.command("region default")
            )
            assert _rows(table) == _rows(fresh), (command, reply)
            assert table.default == fresh.default, command


def test_a_refused_edit_changes_nothing() -> None:
    """A refusal leaves the very same table."""
    table = parse_region_dump(SMALL_DUMP)
    assert table.after("region remove lakeside", "Err - not empty") is table
    assert table.after("region allowf nowhere", "Err - unknown region") is table


def test_a_put_on_old_firmware_is_denied_until_allowed() -> None:
    """Before 1.15 a new region was created denied; the reply says which firmware answered."""
    table = parse_region_dump(SMALL_DUMP)
    assert not table.after("region put bay", "OK").get("bay").flood
    assert table.after("region put bay", "OK - (flood allowed)").get("bay").flood


# --- the simulator keeps the firmware's awkward edges ---------------------------------


def test_the_simulator_behaves_like_the_firmware() -> None:
    """Prefix matching, a put that moves, a remove that refuses, and a reboot that forgets."""
    sim = SimulatedRegionMap(_TREE)
    assert sim.command("region remove lakeside") == "Err - not empty"
    assert sim.command("region allowf lakeside-s") == "OK"  # a prefix is enough on the radio
    assert sim.command("region put harbour lakeside") == "OK - (flood allowed)"  # moves it
    assert " harbour" not in sim.command("region").split("\n")  # no longer at the top
    assert sim.command("region list denied") == "-none-"
    sim.reboot()  # nothing was saved
    assert sim.command("region") == SMALL_DUMP
    sim.command("region denyf harbour")
    assert sim.command("region save") == "OK"
    sim.reboot()
    assert "harbour\n" in sim.command("region")
    assert sim.command("region frobnicate") == "Err - ??"


async def test_the_mock_repeater_answers_the_region_cli_and_the_request_alike() -> None:
    """The anonymous request reads the same table the CLI edits."""
    device = MockDevice()
    await device.connect()
    assert await device.admin_login(NODE, "admin")
    assert await device.send_remote_command(NODE, "region") == SMALL_DUMP
    assert await device.request_regions(NODE) == ["*", "lakeside", "lakeside-north", "harbour"]
    await device.send_remote_command(NODE, "region denyf *")
    assert await device.request_regions(NODE) == ["lakeside", "lakeside-north", "harbour"]
    await device.send_remote_command(NODE, "reboot")  # unsaved, so it comes back
    assert (await device.request_regions(NODE))[0] == "*"
    await device.disconnect()


# --- the region store remembers the whole answer ----------------------------------------


def test_the_store_keeps_whether_a_repeater_relays_unscoped_floods(tmp_path: Path) -> None:
    """The ``*`` names no region, so it is kept beside the regions — and survives a reload."""
    store = RegionStore(tmp_path / "regions.json")
    assert store.answer_of("a1b2c3d4e5f6") is None
    store.learn_carried("a1b2c3d4e5f6aa", ["*", "lakeside"])
    answer = store.answer_of("a1b2c3d4e5f6")
    assert answer is not None and answer.unscoped
    assert store.carried_by("a1b2c3d4e5f6") == ["lakeside"]

    again = RegionStore(tmp_path / "regions.json")  # read back from disk
    assert again.answer_of("a1b2c3d4e5f6").unscoped
    again.learn_carried("a1b2c3d4e5f6", [])
    assert not again.answer_of("a1b2c3d4e5f6").unscoped
    assert again.carried_by("a1b2c3d4e5f6") == []


# --- the editor ------------------------------------------------------------------------


class _ScriptedUi:
    """A UI surface that answers each question from a script and records what was asked."""

    def __init__(self, *answers) -> None:  # noqa: ANN002
        self.answers = list(answers)
        self.asked: list[tuple[str, str]] = []
        self.messages: list[str] = []
        self.session = SimpleNamespace(message_dialog=self._message, invalidate=lambda: None)

    def _next(self, kind: str, title: str):  # noqa: ANN202
        self.asked.append((kind, title))
        return self.answers.pop(0) if self.answers else None

    async def select(self, title, items, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.last_items = items
        self.last_prompt = kwargs.get("prompt", "")
        return self._next("select", title)

    async def text(self, title, **kwargs):  # noqa: ANN001, ANN003, ANN201
        answer = self._next("text", title)
        check = kwargs.get("validate")
        if answer is not None and check is not None:
            assert check(answer) is True, check(answer)
        return answer

    async def dialog(self, prompt, buttons, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.last_dialog = kwargs
        return self._next("dialog", kwargs.get("title", ""))

    @asynccontextmanager
    async def busy_overlay(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        yield

    async def _message(self, body, *, title: str = "") -> None:  # noqa: ANN001
        self.messages.append(body.plain if isinstance(body, Text) else str(body))


@pytest.fixture()
async def region_device(monkeypatch):
    """The simulator, logged in to both repeaters, recording every command, unpaced."""
    real_sleep = asyncio.sleep

    async def no_wait(_delay, result=None):
        return await real_sleep(0, result)

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    device = MockDevice()
    await device.connect()
    assert await device.admin_login(NODE, "admin")
    assert await device.admin_login(BIG, "admin")
    device.sent = []
    original = device.send_remote_command

    async def recording(node, command, *, timeout=8.0):
        device.sent.append(command)
        return await original(node, command, timeout=timeout)

    monkeypatch.setattr(device, "send_remote_command", recording)
    yield device
    await device.disconnect()


def _ctx(tmp_path: Path, *answers) -> SimpleNamespace:  # noqa: ANN002
    return SimpleNamespace(
        ui=_ScriptedUi(*answers),
        region_store=RegionStore(tmp_path / "regions.json"),
        preferences=SimpleNamespace(trace_cooldown_s=0),
    )


async def _table(device) -> RegionTable:  # noqa: ANN001
    dump = await device.send_remote_command(NODE, "region")
    default = await device.send_remote_command(NODE, "region default")
    device.sent.clear()
    return parse_region_dump(dump).with_default(default)


@pytest.mark.parametrize(
    "region,pick,command",
    [
        ("lakeside", "deny", "region denyf lakeside"),
        ("lakeside-south", "allow", "region allowf lakeside-south"),
        ("harbour", "home", "region home harbour"),
        ("harbour", "default", "region default harbour"),
    ],
)
async def test_each_region_action_sends_its_one_command(
    tmp_path, region_device, region, pick, command
) -> None:
    """One pick, one command; the folded table matches the repeater's next dump."""
    ctx = _ctx(tmp_path, pick)
    table = await _table(region_device)
    table, unsaved = await region_editor._region_actions(ctx, region_device, NODE, table, 0, region)
    assert region_device.sent == [command]
    assert unsaved == (0 if pick == "default" else 1)  # a default saves the table itself
    fresh = parse_region_dump(await region_device.send_remote_command(NODE, "region"))
    assert _rows(table) == _rows(fresh)


async def test_denying_unscoped_floods_asks_first(tmp_path, region_device) -> None:
    """Every plain flood would stop at this repeater: Cancel sends nothing at all."""
    ctx = _ctx(tmp_path, "deny", None)
    table = await _table(region_device)
    await region_editor._region_actions(ctx, region_device, NODE, table, 0, "*")
    assert region_device.sent == []
    assert ctx.ui.last_dialog.get("danger")

    ctx = _ctx(tmp_path, "deny", "deny")
    table, unsaved = await region_editor._region_actions(ctx, region_device, NODE, table, 0, "*")
    assert region_device.sent == ["region denyf *"] and unsaved == 1
    assert not table.wildcard.flood


async def test_the_wildcard_clears_a_home_elsewhere(tmp_path, region_device) -> None:
    """Home on the wildcard means no home region: ``region home *``."""
    await region_device.send_remote_command(NODE, "region home harbour")
    table = await _table(region_device)
    ctx = _ctx(tmp_path, "home")
    table, _ = await region_editor._region_actions(ctx, region_device, NODE, table, 0, "*")
    assert region_device.sent == ["region home *"] and table.home == "*"


async def test_clearing_the_default_sends_null(tmp_path, region_device) -> None:
    """Clearing the default saves the table too, so nothing is left unsaved."""
    await region_device.send_remote_command(NODE, "region default harbour")
    table = await _table(region_device)
    ctx = _ctx(tmp_path, "undefault")
    table, unsaved = await region_editor._region_actions(
        ctx, region_device, NODE, table, 3, "harbour"
    )
    assert region_device.sent == ["region default <null>"]
    assert table.default is None and unsaved == 0


async def test_adding_under_a_region_puts_it_there(tmp_path, region_device) -> None:
    """Asked from a row, only the name is asked; the parent is the row."""
    ctx = _ctx(tmp_path, "add", "harbour-bay")
    table = await _table(region_device)
    table, unsaved = await region_editor._region_actions(
        ctx, region_device, NODE, table, 0, "harbour"
    )
    assert region_device.sent == ["region put harbour-bay harbour"]
    assert table.get("harbour-bay").parent == "harbour" and table.get("harbour-bay").flood
    assert unsaved == 1


async def test_adding_from_the_actions_asks_where_then_the_name(tmp_path, region_device) -> None:
    """Asked from Actions, the parent comes first, then the name."""
    ctx = _ctx(tmp_path, "*", "cove")
    table = await _table(region_device)
    table, _ = await region_editor._add_region(ctx, region_device, NODE, table, 0, None)
    assert [kind for kind, _ in ctx.ui.asked] == ["select", "text"]
    assert region_device.sent == ["region put cove"]


async def test_esc_on_the_name_steps_back_to_where(tmp_path, region_device) -> None:
    """The add flow is a stack: Esc on the name re-asks the parent, then Esc abandons."""
    ctx = _ctx(tmp_path, "harbour", None, None)
    table = await _table(region_device)
    await region_editor._add_region(ctx, region_device, NODE, table, 0, None)
    assert [kind for kind, _ in ctx.ui.asked] == ["select", "text", "select"]
    assert region_device.sent == []


async def test_removing_asks_in_red_and_sends_remove(tmp_path, region_device) -> None:
    """A single-record delete: a red Cancel/Remove confirm, then one command."""
    ctx = _ctx(tmp_path, "remove", "remove")
    table = await _table(region_device)
    table, unsaved = await region_editor._region_actions(
        ctx, region_device, NODE, table, 0, "lakeside-south"
    )
    assert ctx.ui.last_dialog.get("destructive")
    assert region_device.sent == ["region remove lakeside-south"]
    assert table.get("lakeside-south") is None and unsaved == 1


async def test_a_region_with_sub_regions_is_not_removed(tmp_path, region_device) -> None:
    """The firmware refuses it, so the page never sends it and says why."""
    ctx = _ctx(tmp_path, "blocked")
    table = await _table(region_device)
    await region_editor._region_actions(ctx, region_device, NODE, table, 0, "lakeside")
    assert region_device.sent == []
    assert "lakeside-north" in ctx.ui.messages[0]
    # …and Del on it asks nothing and sends nothing either.
    ctx = _ctx(tmp_path)
    await region_editor._remove(ctx, region_device, NODE, table, 0, "lakeside")
    assert region_device.sent == [] and ctx.ui.asked == []


async def test_saving_clears_the_unsaved_count(tmp_path, region_device) -> None:
    """``region save`` is the commit."""
    ctx = _ctx(tmp_path)
    table = await _table(region_device)
    _, unsaved = await region_editor._edit(ctx, region_device, NODE, table, 3, "region save")
    assert region_device.sent == ["region save"] and unsaved == 0


async def test_a_refused_edit_says_so_and_changes_nothing(tmp_path, region_device) -> None:
    """The repeater's own words are shown, and the table stays as it was."""
    ctx = _ctx(tmp_path)
    table = await _table(region_device)
    after, unsaved = await region_editor._edit(
        ctx, region_device, NODE, table, 0, "region remove lakeside"
    )
    assert after is table and unsaved == 0
    assert "Err - not empty" in ctx.ui.messages[0]


async def test_an_accepted_edit_teaches_the_store(tmp_path, region_device) -> None:
    """What the table says it relays is what the node page will show."""
    ctx = _ctx(tmp_path)
    table = await _table(region_device)
    await region_editor._edit(ctx, region_device, NODE, table, 0, "region allowf lakeside-south")
    node_id = region_editor.node_id_of(NODE)
    assert "lakeside-south" in ctx.region_store.carried_by(node_id)
    assert ctx.region_store.answer_of(node_id).unscoped


async def test_reading_a_table_that_fits_asks_two_questions(tui_ctx, region_device) -> None:
    """The dump and the default — no list reads when nothing was cut."""
    table = await region_editor.read_table(tui_ctx, region_device, NODE)
    assert region_device.sent == ["region", "region default"]
    assert table is not None and table.complete and table.default_known


async def test_reading_a_cut_table_recovers_the_rest_from_the_lists(tui_ctx, region_device) -> None:
    """Cut at the cap: the lists are asked, the rest joins unplaced, the store learns all."""
    table = await region_editor.read_table(tui_ctx, region_device, BIG)
    assert region_device.sent == [
        "region",
        "region default",
        "region list allowed",
        "region list denied",
    ]
    assert table.cut and table.lists_read and table.complete
    assert not table.get("hilltop").placed and table.get("hilltop").flood
    assert not table.get("riverside-lower").flood
    carried = tui_ctx.region_store.carried_by(region_editor.node_id_of(BIG))
    assert "hilltop" in carried and "riverside-lower" not in carried


# --- the page ----------------------------------------------------------------------------


def _plain_rows(items: list) -> list[str]:
    out = []
    for item in items:
        text = item.text(72)
        out.append(text.plain if isinstance(text, Text) else str(text))
    return out


def test_the_page_draws_the_tree_and_says_where_it_was_cut() -> None:
    """A cut table says where, and that ^R reads again when the lists never came."""
    sim = SimulatedRegionMap([(f"region-number-{i:02d}", "*", i % 2 == 0) for i in range(10)])
    table = parse_region_dump(sim.command("region"))
    title, items = region_items(BIG, table, 0)
    rows = _plain_rows(items)
    assert title == "Regions — Local-Repeater"
    assert any("⚠ the reply ran out at 160 bytes after" in r for r in rows)
    assert any("^R reads again" in r for r in rows)  # the lists never came
    assert not any("Save" in r for r in rows)  # nothing unsaved, no save pair


def test_unsaved_edits_put_the_save_pair_on_the_page() -> None:
    """The Apply/discard pair, worded for a table that is live but not saved."""
    table = parse_region_dump(SMALL_DUMP)
    title, items = region_items(NODE, table, 2)
    rows = _plain_rows(items)
    assert title.endswith("· 2 unsaved")
    assert rows[-2:] == ["✓ Save 2 changes to the repeater", "✗ Back — a reboot undoes them"]


def test_only_a_childless_region_is_deletable() -> None:
    """Del lights only where ``region remove`` would succeed."""
    table = parse_region_dump(SMALL_DUMP)
    _, items = region_items(NODE, table, 0)
    deletable = {
        item.value.name
        for item in items
        if isinstance(getattr(item, "value", None), RegionPick) and item.deletable
    }
    assert deletable == {"lakeside-north", "lakeside-south", "harbour"}


async def test_ctrl_r_reads_again_and_f3_is_read_over_remove() -> None:
    """A full-screen page; ``^R`` re-reads; the Shift half of F3 lights only on a removable row."""
    table = parse_region_dump(SMALL_DUMP)
    title, items = region_items(NODE, table, 0)
    menu = RegionMenu(title, items)
    menu.future = asyncio.get_running_loop().create_future()
    assert not menu.floating
    lane = menu.fkey_lane
    assert (lane[2].label, lane[2].opp_label) == ("Read", "Remove")
    assert not lane[2].opp_enabled  # the wildcard row is highlighted first
    assert "Del remove" not in menu.footer_hint
    menu.handle("down")
    menu.handle("down")  # lakeside-north
    assert menu.fkey_lane[2].opp_enabled
    assert "Del remove" in menu.footer_hint
    menu.handle("retry")
    assert menu.future.result() == region_editor._READ


async def _step_until(predicate, *, limit: int = 2000):  # noqa: ANN001, ANN202
    """Yield to the event loop until ``predicate()`` is truthy; return its value."""
    value = predicate()
    for _ in range(limit):
        if value:
            return value
        await asyncio.sleep(0)
        value = predicate()
    return value


async def test_a_visit_is_one_page_that_asks_before_leaving_edits_unsaved(
    tui_ctx, region_device
) -> None:
    """The page stays pushed for the visit; Esc with an edit live but unsaved asks first.

    Keep editing leaves the page where it was; Leave then ends the visit with the stack
    exactly as it was found.
    """
    from meshterm.ui.tui.prompt import ButtonDialog

    session = tui_ctx.ui.session
    task = asyncio.ensure_future(region_editor.open_region_editor(tui_ctx, region_device, NODE))
    try:
        page = await _step_until(lambda: isinstance(session.top, RegionMenu) and session.top)
        assert page is not None, "the region page never opened"
        assert region_device.sent == ["region", "region default"]
        assert page.title == "Regions — Yagi-Repeater"

        region_device.sent.clear()
        page.resolve(region_editor._SAVE)  # nothing is unsaved, but a save is still a save
        await _step_until(lambda: region_device.sent)
        assert region_device.sent == ["region save"]

        # An edit through the row's own popup: deny lakeside.
        await _step_until(lambda: session.top is page)
        page.resolve(RegionPick("lakeside"))
        popup = await _step_until(
            lambda: session.top if session.top is not page and session.top else None
        )
        popup.resolve("deny")
        await _step_until(lambda: "· 1 unsaved" in page.title)
        assert region_device.sent[-1] == "region denyf lakeside"

        page.handle("escape")
        confirm = await _step_until(
            lambda: session.top if isinstance(session.top, ButtonDialog) else None
        )
        confirm.resolve("keep")
        await _step_until(lambda: session.top is page)
        assert not task.done()

        page.handle("escape")
        confirm = await _step_until(
            lambda: session.top if isinstance(session.top, ButtonDialog) else None
        )
        confirm.resolve("leave")
        await task
    finally:
        if not task.done():
            task.cancel()
    assert session._stack == []


# --- the node page -------------------------------------------------------------------


def test_the_node_page_row_says_each_state_in_its_own_words(tmp_path: Path) -> None:
    """Answered, not asked, no answer — never one read as another."""
    from meshterm.ui.node_detail_screen import regions_value

    store = RegionStore(tmp_path / "regions.json")
    node_id = "a1a1a1a1a1a1"
    assert regions_value(store, node_id, routed=True).plain == "not asked yet"
    assert "neighbour" in regions_value(store, node_id, routed=False).plain
    assert regions_value(store, node_id, routed=True, failed=True).plain.startswith("no answer")
    store.learn_carried(node_id, ["*", "lakeside", "harbour"])
    shown = regions_value(store, node_id, routed=True).plain
    assert shown.startswith("lakeside, harbour  ·  unscoped too  ·  answered now")
    store.learn_carried(node_id, ["*"])
    assert regions_value(store, node_id, routed=True).plain.startswith("unscoped floods only")
    store.learn_carried(node_id, [])
    assert regions_value(store, node_id, routed=True).plain.startswith("none")
    stale = regions_value(store, node_id, routed=True, failed=True).plain
    assert stale.endswith("no answer now")


async def test_asking_from_the_node_page_learns_or_says_why_not(tmp_path: Path) -> None:
    """One request: learned on an answer, explained on silence, the old answer kept."""
    from meshterm.ui import node_detail_screen

    device = MockDevice()
    await device.connect()
    ctx = _ctx(tmp_path)

    async def the_device():  # noqa: ANN202
        return device

    ctx.device = the_device
    node_id = NODE.public_key[:12]
    failed = await node_detail_screen._ask_regions(ctx, NODE, node_id, NODE.name, routed=True)
    assert not failed
    assert ctx.region_store.carried_by(node_id) == ["lakeside", "lakeside-north", "harbour"]

    device._unreachable.add(NODE.name)
    failed = await node_detail_screen._ask_regions(ctx, NODE, node_id, NODE.name, routed=False)
    assert failed and "only a neighbour answers" in ctx.ui.messages[0]
    assert ctx.region_store.carried_by(node_id)  # the earlier answer is kept
    await device.disconnect()


async def test_an_unreachable_repeater_raises_rather_than_answering_empty() -> None:
    """Silence is not "carries nothing": the request raises, and nothing is learned."""
    device = MockDevice()
    await device.connect()
    device._unreachable.add(NODE.name)
    with pytest.raises(DeviceCommandError):
        await device.request_regions(NODE)
    await device.disconnect()


# --- shared fixture -------------------------------------------------------------------


@pytest.fixture()
def tui_ctx(tmp_path: Path):  # noqa: ANN201
    """A mock-backed context wired to a headless TUI session (as the admin tests use)."""
    import io

    from rich.console import Console

    from meshterm.context import AppContext
    from meshterm.core.admin_store import AdminStore
    from meshterm.core.config import Settings
    from meshterm.core.device_store import DeviceStore
    from meshterm.persistence.repository import Repository
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.tui.session import TuiSession

    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "regions.db")
    ctx = AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    ctx.ui = TuiUi(TuiSession())
    yield ctx
    ctx.repo.close()


def test_the_wildcard_row_says_when_no_home_region_is_set() -> None:
    """``*^`` is the firmware parking its home mark on the wildcard: no home region set."""
    from meshterm.ui.region_editor import _row_note

    unset = parse_region_dump("*^ F\n lakeside F\n")
    assert _row_note(unset.rows[0], unset) == "unscoped · no home"
    homed = parse_region_dump("* F\n lakeside^ F\n")
    assert _row_note(homed.rows[0], homed) == "unscoped floods"
    assert _row_note(homed.rows[1], homed) == "home"
