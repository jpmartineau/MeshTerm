# SPDX-License-Identifier: Apache-2.0
"""Tests for repeater admin.

The settings catalog (checked against the firmware's own CLI), the per-node store, the
simulated remote CLI, the editor's read/apply round trips, and the command-line screen's
readline behavior.
"""

from __future__ import annotations

import asyncio
import io
from datetime import timedelta
from pathlib import Path

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core.admin_store import AdminStore
from meshterm.core.config import Settings
from meshterm.core.connection import MockDevice
from meshterm.core.device_store import DeviceStore
from meshterm.core.models import Contact, utcnow
from meshterm.core.remote_config import (
    DISCOVERED_HELP,
    REPEATER_SETTINGS,
    SettingCommand,
    composite_reads,
    discovered_setting,
    get_setting,
    known_commands,
    normalize_value,
    parse_reply_value,
    parse_setting_command,
    range_hint,
    read_plan,
    reply_is_error,
    validate_value,
    write_plan,
)
from meshterm.core.remote_store import HISTORY_CAP, CachedValue, RemoteStore
from meshterm.persistence.repository import Repository
from meshterm.ui import repeater_admin
from meshterm.ui.remote_cli import RemoteCliScreen
from meshterm.ui.repeater_admin import AdminMenu, ReadOne, open_repeater_admin
from meshterm.ui.surface import TuiUi
from meshterm.ui.trace_screen import TracingDialog
from meshterm.ui.tui.prompt import TextScreen
from meshterm.ui.tui.screen import CANCEL
from meshterm.ui.tui.select import SelectScreen
from meshterm.ui.tui.session import TuiSession

NODE = Contact(name="Yagi-Repeater", public_key="a1" * 32, key_prefix="a1b2c3d4")

#: Every key MeshCore's ``CommonCLI.cpp`` (``handleSetCmd``) lets an admin write over the
#: mesh — ``radio`` standing for its four fields. ``prv.key`` and ``freq`` are left out: the
#: firmware takes those from its serial console only. ``extra.sf`` is LR2021-only.
FIRMWARE_SET_KEYS = {
    "name",
    "owner.info",
    "lat",
    "lon",
    "radio",
    "tx",
    "radio.rxgain",
    "radio.fem.rxgain",
    "radio.fem.txgain",
    "cad",
    "int.thresh",
    "agc.reset.interval",
    "repeat",
    "txdelay",
    "direct.txdelay",
    "rxdelay",
    "dutycycle",
    "af",
    "loop.detect",
    "path.hash.mode",
    "multi.acks",
    "flood.max",
    "flood.max.unscoped",
    "flood.max.advert",
    "advert.interval",
    "flood.advert.interval",
    "guest.password",
    "allow.read.only",
    "adc.multiplier",
    "bridge.enabled",
    "bridge.source",
    "bridge.delay",
    "bridge.baud",
    "bridge.channel",
    "bridge.secret",
}

#: The settings ``handleCommand`` takes as top-level verbs rather than ``set`` keys.
FIRMWARE_VERB_SETTINGS = {"powersaving", "gps", "gps advert"}


class _FakeSession:
    def __init__(self) -> None:
        self.repaints = 0

    def invalidate(self) -> None:
        self.repaints += 1


# --- the catalog ----------------------------------------------------------------------


def test_catalog_covers_every_setting_the_firmware_takes_over_the_mesh() -> None:
    """Each writable firmware key has a row, and nothing is spelled that the firmware lacks.

    Regression: the catalog was written from convention and carried ``get bw``/``sf``/``cr``
    (keys no firmware answers — the radio is read as one ``get radio``) while missing
    ``path.hash.mode`` and twenty-odd others.
    """
    spelled = {s.composite or s.key for s in REPEATER_SETTINGS if s.writable and not s.verb}
    assert spelled == FIRMWARE_SET_KEYS
    assert {s.verb for s in REPEATER_SETTINGS if s.verb} == FIRMWARE_VERB_SETTINGS
    assert len({s.key for s in REPEATER_SETTINGS}) == len(REPEATER_SETTINGS)


def test_catalog_carries_the_repeater_only_knobs() -> None:
    """TX delay and Direct TX delay — the reason this feature exists — are first-class."""
    txdelay = get_setting("txdelay")
    direct = get_setting("direct.txdelay")
    assert txdelay is not None and txdelay.set_command("0.5") == "set txdelay 0.5"
    assert direct is not None and direct.get_command == "get direct.txdelay"
    assert any(s.key == "af" for s in REPEATER_SETTINGS)
    assert any(s.key == "advert.interval" for s in REPEATER_SETTINGS)


def test_delays_are_floats_rounded_to_one_place() -> None:
    """TX, Direct TX and RX delay read, validate and send as one-decimal floats.

    Regression: they were typed ``int``, so a firmware ``0.5`` showed as ``0`` and typing
    ``0.5`` back was refused as "not a whole number".
    """
    for key in ("txdelay", "direct.txdelay", "rxdelay"):
        spec = get_setting(key)
        assert spec.kind == "float" and spec.decimals == 1
        assert parse_reply_value(spec, "> 0.5") == "0.5"
        assert parse_reply_value(spec, "> 1.2345678") == "1.2"
        assert validate_value(spec, "0.5") is True
        assert normalize_value(spec, "0.3333") == "0.3"
    assert "≤ 2" in validate_value(get_setting("txdelay"), "2.5")
    assert validate_value(get_setting("rxdelay"), "12.5") is True  # RX delay runs to 20


def test_path_hash_mode_is_an_enum_the_firmware_accepts() -> None:
    """Path-hash mode is on the repeater too, offering exactly the modes ``set`` takes."""
    spec = get_setting("path.hash.mode")
    assert spec is not None and spec.kind == "enum"
    assert [o.value for o in spec.options] == ["0", "1", "2"]  # the firmware refuses 3
    assert parse_reply_value(spec, "> 1") == "1"
    assert spec.display("1") == "2-byte"
    assert validate_value(spec, "2") is True
    assert validate_value(spec, "3") != True  # noqa: E712 - a message, not False
    assert spec.set_command("2") == "set path.hash.mode 2"


def test_radio_fields_travel_as_one_command() -> None:
    """Frequency, bandwidth, SF and CR are one ``get radio`` and one ``set radio``."""
    plan = dict(read_plan(REPEATER_SETTINGS))
    assert [s.key for s in plan["get radio"]] == ["freq", "bw", "sf", "cr"]
    assert not any(command in plan for command in ("get freq", "get bw", "get sf", "get cr"))
    assert len(plan) == len(REPEATER_SETTINGS) - 3  # everything else is its own read

    reply = "> 910.525,62.5,7,5"
    fields = {key: parse_reply_value(get_setting(key), reply) for key in ("freq", "bw", "sf", "cr")}
    assert fields == {"freq": "910.525", "bw": "62.5", "sf": "7", "cr": "5"}

    (write,) = write_plan({"sf": "9"}, fields)
    assert write.command == "set radio 910.525,62.5,9,5"
    assert write.values == {**fields, "sf": "9"}

    (blind,) = write_plan({"sf": "9"}, {})  # siblings never read: nothing to restate
    assert blind.command == "" and set(blind.missing) == {"freq", "bw", "cr"}
    assert composite_reads({"sf": "9"}, {}) == ["get radio"]
    assert composite_reads({"sf": "9"}, fields) == []


def test_verb_shaped_and_oddly_spelled_settings() -> None:
    """Settings the firmware spells its own way read and write the way it spells them."""
    powersaving = get_setting("powersaving")
    assert powersaving.get_command == "powersaving"
    assert powersaving.set_command("on") == "powersaving on"
    assert parse_reply_value(powersaving, "on - Immediate effect") == "on"

    gps = get_setting("gps")
    assert parse_reply_value(gps, "on, deactivated, no fix, 0 sats") == "off"
    assert parse_reply_value(gps, "on, active, fix, 7 sats") == "on"
    assert get_setting("gps advert").set_command("share") == "gps advert share"

    assert get_setting("multi.acks").set_command("on") == "set multi.acks 1"  # an atoi
    assert parse_reply_value(get_setting("multi.acks"), "> 1") == "on"
    assert parse_reply_value(get_setting("bridge.source"), "> logRx") == "rx"
    assert parse_reply_value(get_setting("dutycycle"), "> 50.0%") == "50.0"


def test_advert_intervals_take_zero_as_off() -> None:
    """0 turns an advert interval off; anything else must sit in the firmware's range."""
    spec = get_setting("advert.interval")
    assert validate_value(spec, "0") is True
    assert validate_value(spec, "120") is True
    assert validate_value(spec, "30") == "Must be 0, or 60 – 240."
    assert range_hint(spec) == "Allowed: 0 (off), or 60 – 240  (min)"


def test_parse_reply_value_survives_terse_and_verbose_firmware() -> None:
    """Numbers are extracted from any phrasing; errors parse as unknown, not values."""
    tx = get_setting("tx")
    assert parse_reply_value(tx, "> 20") == "20"
    assert parse_reply_value(tx, "tx: 20") == "20"
    assert parse_reply_value(tx, "TX power = 20 dBm") == "20"
    assert parse_reply_value(tx, "ERR: unknown config: tx") is None
    assert parse_reply_value(tx, "??: tx") is None  # handleGetCmd's own spelling
    assert parse_reply_value(tx, None) is None

    name = get_setting("name")
    assert parse_reply_value(name, "> Yagi") == "Yagi"
    assert parse_reply_value(name, "name: Yagi") == "Yagi"
    # A value echo is a value, even one that reads like a refusal.
    assert parse_reply_value(name, "> Unknown-Hill") == "Unknown-Hill"
    assert not reply_is_error("> Unknown-Hill")
    assert parse_reply_value(get_setting("owner.info"), "> ") == ""  # blank is a value

    repeat = get_setting("repeat")
    assert parse_reply_value(repeat, "> on") == "on"
    assert parse_reply_value(repeat, "repeat is off") == "off"


def test_validate_value_enforces_kind_and_bounds() -> None:
    """Prompt validation speaks in the setting's own terms."""
    sf = get_setting("sf")
    assert validate_value(sf, "9") is True
    assert "≥ 5" in validate_value(sf, "3")
    assert "number" in validate_value(sf, "fast")
    assert validate_value(get_setting("repeat"), "maybe") == "Enter on or off."
    assert validate_value(get_setting("name"), "  ") == "Enter a value."


def test_known_commands_cover_catalog_and_verbs() -> None:
    """The CLI completions include every catalog spelling plus the fixed verbs."""
    commands = known_commands()
    assert "get txdelay" in commands
    assert "set direct.txdelay " in commands
    assert "get radio" in commands and "set radio " in commands
    assert "powersaving " in commands and "gps advert" in commands
    assert "get path.hash.mode" in commands
    assert "reboot" in commands and "ver" in commands
    assert "get bw" not in commands and "set freq " not in commands


# --- the store ------------------------------------------------------------------------


def test_remote_store_caches_settings_per_node(tmp_path: Path) -> None:
    """Values round-trip with their read stamps, keyed per node."""
    store = RemoteStore(tmp_path / "remote.json")
    store.remember_setting(NODE, "txdelay", "0.50")
    cached = store.settings(NODE)
    assert cached["txdelay"].value == "0.50"
    assert cached["txdelay"].read_at is not None
    other = Contact(name="Other", public_key="b2" * 32)
    assert store.settings(other) == {}


def test_remote_store_tells_unsupported_from_unread(tmp_path: Path) -> None:
    """A key the firmware refused is remembered as that; forgetting makes it unread again."""
    store = RemoteStore(tmp_path / "remote.json")
    store.remember_unsupported(NODE, "bridge.delay")
    store.remember_setting(NODE, "af", "1.00")
    cached = store.settings(NODE)
    assert cached["bridge.delay"].supported is False
    assert cached["af"].supported is True
    store.forget_setting(NODE, "af")
    assert "af" not in store.settings(NODE)


def test_remote_store_history_dedupes_and_caps(tmp_path: Path) -> None:
    """History appends in order, skips adjacent repeats, and stays capped."""
    store = RemoteStore(tmp_path / "remote.json")
    store.append_history(NODE, "get tx")
    store.append_history(NODE, "get tx")  # adjacent repeat — no stutter on recall
    store.append_history(NODE, "set tx 20")
    assert store.history(NODE) == ["get tx", "set tx 20"]
    for i in range(HISTORY_CAP + 20):
        store.append_history(NODE, f"cmd {i}")
    history = store.history(NODE)
    assert len(history) == HISTORY_CAP and history[-1] == f"cmd {HISTORY_CAP + 19}"


# --- the simulated remote CLI ----------------------------------------------------------


async def test_mock_remote_cli_requires_login() -> None:
    """A stranger's command reads as a timeout (None), exactly like hardware."""
    device = MockDevice()
    await device.connect()
    assert await device.send_remote_command(NODE, "get tx") is None
    await device.disconnect()


async def test_mock_remote_cli_answers_like_the_firmware() -> None:
    """The simulator keeps CommonCLI's spellings and reply shapes, refusals included."""
    device = MockDevice()
    await device.connect()
    assert await device.admin_login(NODE, "admin")
    assert await device.send_remote_command(NODE, "get txdelay") == "> 0.5"
    assert await device.send_remote_command(NODE, "set txdelay 1.25") == "OK"
    assert await device.send_remote_command(NODE, "get txdelay") == "> 1.25"
    assert await device.send_remote_command(NODE, "set tx 22") == "OK"
    assert await device.get_remote_tx_power(NODE) == 22  # one shared TX state
    assert await device.send_remote_command(NODE, "get tx") == "> 22"
    assert await device.send_remote_command(NODE, "get radio") == "> 910.525,62.5,7,5"
    refused = await device.send_remote_command(NODE, "set freq 915")
    assert refused.startswith("unknown config")  # serial console only
    reboot = await device.send_remote_command(NODE, "set radio 915,250,10,5")
    assert reboot == "OK - reboot to apply"
    assert await device.send_remote_command(NODE, "get radio") == "> 915,250,10,5"
    assert await device.send_remote_command(NODE, "get nonsense") == "??: nonsense"
    assert "simulator" in (await device.send_remote_command(NODE, "ver"))
    await device.disconnect()


# --- the login flow: the password floats over the node picker, never a blank frame ----


@pytest.fixture()
def tui_ctx(tmp_path: Path) -> AppContext:
    """A mock-backed context wired to a headless TUI session (no prompt_toolkit app)."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "admin.db")
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


async def _step_until(predicate, *, limit: int = 200):
    """Yield to the event loop until ``predicate()`` is truthy; return it (or its last value)."""
    value = predicate()
    for _ in range(limit):
        if value:
            return value
        await asyncio.sleep(0)
        value = predicate()
    return value


def test_picker_rows_hue_each_name_by_its_own_key(tui_ctx) -> None:
    """Picker rows lead with the type glyph in *its* colour and hue the name by the key.

    Regression: the row was built as ``Text(glyph, style=type_style)``, which made the
    node-type colour the whole row's *base* style — so every repeater's name came out the
    repeater violet and a section of them read as one node. The glyph now carries its type
    style as a span and the name takes :func:`~meshterm.ui.theme.name_style`, like every
    other list of nodes in the app.
    """
    from meshterm.ui.admin_picker import admin_picker_rows
    from meshterm.ui.theme import name_style
    from meshterm.ui.widgets import NODE_GLYPHS

    contacts = [
        Contact(name="Hub-North", public_key="a1" * 32, key_prefix="a1b2c3d4", node_type=2),
        Contact(name="Hub-South", public_key="d4" * 32, key_prefix="d4c3b2a1", node_type=2),
    ]
    rows, candidates = admin_picker_rows(tui_ctx, contacts)
    assert [c.name for c in candidates] == ["Hub-North", "Hub-South"]

    glyph, glyph_style = NODE_GLYPHS[2]
    titles = {str(row.value): row.title for row in rows if getattr(row, "value", None) is not None}
    hues = set()
    for contact in contacts:
        title = titles[contact.name]
        assert str(title.style) == ""  # no base style to paint the whole row one colour
        assert title.plain == f"{glyph} {contact.name}"
        assert any(s.style == glyph_style and s.start == 0 for s in title.spans)
        hue = name_style(contact.name, contact.public_key)
        at = title.plain.index(contact.name)
        assert any(s.style == hue and s.start <= at < s.end for s in title.spans)
        hues.add(hue)
    assert len(hues) == 2  # two repeaters, two identities — not one violet block


async def test_the_node_picker_is_a_popup_gone_before_the_login(tui_ctx) -> None:
    """The node pick is a floating question, answered and gone; a refused login re-asks it.

    The picker used to stay pushed as a hub under the password prompt and the admin page,
    so Esc from the page landed on the list it was picked from — two places where there
    is one. Now it draws as a box over a blank base (a popup that is the only frame on the
    stack would otherwise be painted full-frame), comes down on Enter, the password prompt
    floats on its own, and Esc out of the prompt asks the question again with the same node
    highlighted rather than dropping the reader on a list that never left.
    """
    ctx = tui_ctx
    session = ctx.ui.session
    pick_title = "Repeater admin — node to manage"

    def picker_up() -> SelectScreen | None:
        top = session.top
        return top if isinstance(top, SelectScreen) and top.title == pick_title else None

    task = asyncio.ensure_future(open_repeater_admin(ctx))
    try:
        picker = await _step_until(picker_up)
        assert picker is not None, "the node picker never opened"
        assert picker.floating and session._has_float(), "drawn as a box…"
        assert not session._base_screen().floating, "…over a blank base"

        # Walk to the repeater's row and commit it, the way a reader does.
        for _ in range(len(picker._items)):
            current = picker._current_choice()
            if current is not None and current.value == "Yagi-Repeater":
                break
            picker.handle("down")
        picker.handle("enter")
        prompt = await _step_until(
            lambda: session.top if isinstance(session.top, TextScreen) else None
        )
        assert prompt.floating and session._has_float(), "the password box floats"
        assert picker not in session._stack, "the pick is answered, so the popup is gone"

        prompt.resolve(CANCEL)  # Esc — abandon the login…
        again = await _step_until(picker_up)
        assert again is not picker, "…and the question is asked again, a fresh popup"
        current = again._current_choice()
        assert current is not None and current.value == "Yagi-Repeater", "same node highlighted"
        again.handle("escape")  # Esc on the question leaves the flow
        result = await task
    finally:
        if not task.done():
            task.cancel()

    assert result is None  # no node was ever administered
    assert session._stack == []


# --- reading and applying against the simulator ----------------------------------------


@pytest.fixture()
async def admin_device(monkeypatch):
    """A simulator logged in to ``NODE``, recording every command, with pacing waived."""
    real_sleep = asyncio.sleep

    async def no_wait(_delay, result=None):
        return await real_sleep(0, result)

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    device = MockDevice()
    await device.connect()
    assert await device.admin_login(NODE, "admin")
    device.sent = []
    original = device.send_remote_command

    async def recording(node, command, *, timeout=8.0):
        device.sent.append(command)
        return await original(node, command, timeout=timeout)

    monkeypatch.setattr(device, "send_remote_command", recording)
    yield device
    await device.disconnect()


async def test_read_settings_reads_every_setting_asking_each_question_once(
    tui_ctx, admin_device
) -> None:
    """Read settings leaves every row answered — a value, or the node saying it has none.

    The radio's four fields come from one ``get radio``; nothing is asked twice; a board
    without a front-end module or a bridge reads as unsupported rather than unread.
    """
    await repeater_admin.read_settings(tui_ctx, admin_device, NODE, REPEATER_SETTINGS)

    sent = admin_device.sent
    assert len(sent) == len(set(sent)) == len(read_plan(REPEATER_SETTINGS))
    cache = tui_ctx.remote_store.settings(NODE)
    assert set(cache) == {s.key for s in REPEATER_SETTINGS}
    assert cache["txdelay"].value == "0.5" and cache["direct.txdelay"].value == "0.2"
    assert (cache["freq"].value, cache["bw"].value, cache["sf"].value) == ("910.525", "62.5", "7")
    assert cache["path.hash.mode"].value == "0"
    assert cache["dutycycle"].value == "50.0"
    assert cache["gps"].value == "off" and cache["gps advert"].value == "none"
    assert cache["owner.info"].value == "" and cache["owner.info"].supported
    assert not cache["radio.fem.rxgain"].supported
    assert not cache["bridge.delay"].supported


async def test_read_one_setting_sends_one_read(tui_ctx, admin_device) -> None:
    """``^R``'s read asks for the highlighted setting alone — or its radio line."""
    await repeater_admin.read_settings(tui_ctx, admin_device, NODE, [get_setting("sf")])
    assert admin_device.sent == ["get radio"]
    assert tui_ctx.remote_store.settings(NODE)["cr"].value == "5"  # the same answer filled it


def test_a_reply_that_cannot_be_the_value_reads_n_a(tui_ctx) -> None:
    """A node answering a key it lacks with anything but its value reads ``n/a``, not ``?``.

    Regression: v1.15 matches ``get`` keys by prefix, so ``get radio.fem.rxgain`` came back
    as the ``get radio`` line. It parsed as nothing, and the row stayed ``?`` — never asked.
    """
    replies = {
        "radio.fem.rxgain": "> 910.525,62.5,7,5",  # a shorter sibling's branch answered
        "gps": "Can't find GPS",
        "bridge.delay": "??: bridge.delay",
    }
    for key, reply in replies.items():
        assert repeater_admin.remember_reply(tui_ctx, NODE, [get_setting(key)], reply) == 0
    cache = tui_ctx.remote_store.settings(NODE)
    for key in replies:
        assert repeater_admin._value_text(get_setting(key), cache, {}).plain == "n/a"


async def test_apply_reads_the_radio_first_then_sends_one_line(tui_ctx, admin_device) -> None:
    """A radio field staged unread restates its siblings from a fresh read, never a guess."""
    ctx = tui_ctx
    session = ctx.ui.session
    ctx.remote_store.remember_setting(NODE, "af", "1.00")
    pending = {"sf": "9", "txdelay": "1.5", "dutycycle": "40.0"}

    task = asyncio.ensure_future(repeater_admin._apply(ctx, admin_device, NODE, pending))
    summary = await _step_until(
        lambda: (
            session.top
            if session.top is not None and not isinstance(session.top, TracingDialog)
            else None
        ),
        limit=5000,
    )
    assert summary is not None, "the apply summary never opened"
    summary.resolve(None)
    applied = await task

    assert admin_device.sent == [
        "get radio",
        "set radio 910.525,62.5,9,5",
        "set txdelay 1.5",
        "set dutycycle 40.0",
    ]
    assert applied == 3 and pending == {}
    cache = ctx.remote_store.settings(NODE)
    assert cache["sf"].value == "9" and cache["txdelay"].value == "1.5"
    assert "af" not in cache  # the other spelling of the duty cycle is stale now


# --- the editor menu -----------------------------------------------------------------


def test_value_lane_shows_the_value_without_its_age() -> None:
    """The value lane is the value: no age stamp, and its own word for each absence."""
    cache = {
        "txdelay": CachedValue("0.5", utcnow() - timedelta(hours=3)),
        "bridge.delay": CachedValue("", utcnow(), supported=False),
        "guest.password": CachedValue("", utcnow()),
        "path.hash.mode": CachedValue("1", utcnow()),
    }
    lane = repeater_admin._value_text
    assert lane(get_setting("txdelay"), cache, {}).plain == "0.5"
    assert lane(get_setting("bridge.delay"), cache, {}).plain == "n/a"
    assert lane(get_setting("guest.password"), cache, {}).plain == "empty"
    assert lane(get_setting("rxdelay"), cache, {}).plain == "?"
    staged = lane(get_setting("path.hash.mode"), cache, {"path.hash.mode": "2"})
    assert staged.plain == "2-byte → 3-byte"


async def test_ctrl_r_reads_only_a_highlighted_setting() -> None:
    """``^R`` resolves with the highlighted setting, and is named only where it would act."""
    title, items = repeater_admin._menu_items(NODE, {}, {})
    menu = AdminMenu(title, items, footer_hint="↑↓ move · type to filter · Enter select · Esc back")
    loop = asyncio.get_running_loop()

    menu.future = loop.create_future()
    assert menu._current_choice().value == "name"
    assert menu.footer_hint.endswith("Enter select · ^R read · Esc back")
    assert menu.picocalc_lyra_lane[2].label == "Read" and menu.picocalc_lyra_lane[2].enabled
    menu.handle("retry")
    assert menu.future.result() == ReadOne("name")

    menu.future = loop.create_future()
    menu.handle("end")  # the last action row: nothing there to read
    assert "^R" not in menu.footer_hint
    assert not menu.picocalc_lyra_lane[2].enabled
    menu.handle("retry")
    assert not menu.future.done()


def test_admin_editor_is_a_full_screen_page() -> None:
    """The editor fills the frame like Device config; only its prompts float over it."""
    title, items = repeater_admin._menu_items(NODE, {}, {})
    assert AdminMenu(title, items).floating is False


def test_admin_editor_ends_long_rows_at_the_edge_like_device_config() -> None:
    """A long row is cut with the ellipsis and ←→ stay inert — the Device config handling.

    The Actions rows pin a head block, which alone used to turn ←→ scrolling on for the
    whole page: the footer grew a ``←→ scroll`` atom and a setting's label slid out of view.
    """
    title, items = repeater_admin._menu_items(NODE, {}, {})
    menu = AdminMenu(title, items, footer_hint="↑↓ move · type to filter · Enter select · Esc back")
    before = list(menu.render_body(40))  # narrow enough that the highlighted row overflows
    assert "←→" not in menu.footer_hint
    menu.handle("right")
    assert list(menu.render_body(40)) == before


def test_the_map_pick_row_heads_the_coordinates_it_sets() -> None:
    """``Pick location on map…`` sits directly above Latitude and Longitude, as on Device config."""
    from meshterm.ui.tui import Choice

    _title, items = repeater_admin._menu_items(NODE, {}, {})
    rows = [
        item.title.plain
        for item in items
        if isinstance(item, Choice) and hasattr(item.title, "plain")
    ]
    at = next(i for i, row in enumerate(rows) if row.startswith("Pick location on map…"))
    assert rows[at + 1].startswith("Latitude")
    assert rows[at + 2].startswith("Longitude")


async def test_the_map_pick_stages_both_coordinates_as_a_read_would_spell_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The map opens where the node says it is; a coordinate it already holds stays unstaged."""
    opened: list = []

    async def _fake_pick(_ctx, *, initial=None):  # noqa: ANN001
        opened.append(initial)
        return (45.51234567, -73.6)

    monkeypatch.setattr("meshterm.ui.map_screen.pick_location", _fake_pick)
    now = utcnow()
    cache = {"lat": CachedValue("45.5", now), "lon": CachedValue("-73.6", now)}
    pending: dict[str, str] = {}
    await repeater_admin._stage_location(None, cache, pending)
    assert opened == [(45.5, -73.6)]
    assert pending == {"lat": "45.512346"}  # the longitude is already where the node has it

    # A node that never reported a position opens the map on the mesh, and takes both.
    unread: dict[str, str] = {}
    await repeater_admin._stage_location(None, {}, unread)
    assert opened[-1] is None
    assert unread == {"lat": "45.512346", "lon": "-73.6"}


# --- the command-line screen -----------------------------------------------------------


def _cli(history=None, sent=None) -> RemoteCliScreen:
    return RemoteCliScreen(
        node_label="Yagi-Repeater",
        history=list(history or []),
        send=(sent.append if sent is not None else lambda c: None),
        session=_FakeSession(),
    )


def _type(screen: RemoteCliScreen, text: str) -> None:
    for ch in text:
        screen.handle("text", ch)


def test_cli_screen_sends_on_enter_and_parks_while_waiting() -> None:
    """Enter commits the buffer once; a command in flight blocks the next send."""
    sent: list[str] = []
    screen = _cli(sent=sent)
    _type(screen, "get tx")
    screen.handle("enter")
    assert sent == ["get tx"]
    screen.sent("get tx")  # the owner echoes and parks the prompt
    _type(screen, "ver")
    screen.handle("enter")
    assert sent == ["get tx"]  # parked: nothing new goes out until the reply lands
    screen.reply("20")
    screen.handle("enter")  # the typed-while-parked buffer is still there to send
    assert sent == ["get tx", "ver"]


def test_cli_screen_history_recall_keeps_the_draft() -> None:
    """↑ walks back through history; ↓ past the newest restores the unsent draft."""
    screen = _cli(history=["get tx", "set tx 20"])
    _type(screen, "dra")
    screen.handle("up")
    assert screen._editor.text == "set tx 20"
    screen.handle("up")
    assert screen._editor.text == "get tx"
    screen.handle("down")
    screen.handle("down")
    assert screen._editor.text == "dra"  # the draft came back


def test_cli_screen_tab_completes_known_commands() -> None:
    """Tab adopts the first known command extending the typed prefix."""
    screen = _cli()
    _type(screen, "get txd")
    screen.handle("tab")
    assert screen._editor.text == "get txdelay"


def test_cli_screen_transcript_shows_exchange() -> None:
    """The transcript keeps the echoed command and its reply, prompt at the bottom."""
    screen = _cli()
    screen.sent("get txdelay")
    screen.reply("> 5")
    body = "\n".join(screen.render_body(80))
    assert "get txdelay" in body and "> 5" in body
    assert screen.cursor_line() == screen._prompt_line  # the view follows the prompt


def test_cli_screen_timeout_note_frees_the_prompt() -> None:
    """A timeout lands as a muted note and the prompt accepts input again."""
    screen = _cli()
    screen.sent("advert")
    assert screen.busy
    screen.failed("no reply within 10 s")
    assert not screen.busy
    assert "no reply within 10 s" in "\n".join(screen.render_body(80))


# --- the editor menu's column header --------------------------------------------------


def test_admin_menu_pins_the_column_header_over_the_category() -> None:
    """Scrolled deep, the lane names stay overhead with the category heading under them."""
    import re

    from meshterm.ui.tui import frame

    ansi = re.compile(r"\x1b\[[0-9;]*m")
    title, items = repeater_admin._menu_items(NODE, {}, {})
    screen = SelectScreen(title, items)
    for _ in range(18):  # down past the first categories
        screen.handle("down")
    visible, above, _below = frame._visible_slice(screen, screen.render_body(72), 10)
    top = [ansi.sub("", row).strip() for row in visible[:2]]
    assert top[0].startswith("SETTING") and top[0].endswith("DESCRIPTION")
    assert top[1].startswith("──")  # the category the highlighted row sits in
    assert above is True


def test_admin_menu_header_abbreviates_rather_than_wrapping() -> None:
    """Too narrow for the whole line, the last label shortens — the header stays one row."""
    from rich.cells import cell_len

    _title, items = repeater_admin._menu_items(NODE, {}, {})
    header = items[0]
    assert header.pinned
    full = header.text(100)
    assert full.endswith("DESCRIPTION")
    narrow = header.text(cell_len(full) - 2)
    assert narrow.endswith("DESC") and "\n" not in narrow


def test_admin_actions_start_every_label_in_the_same_cell() -> None:
    """``↻`` and ``⌨`` are one cell among two-cell siblings; the icon column absorbs it.

    The Actions rows used to write ``icon + " "``, which started *Read settings* and
    *Command line…* a column left of *Send advert…* below them.
    """
    from rich.cells import cell_len

    from meshterm.ui.tui import Choice

    _title, items = repeater_admin._menu_items(NODE, {}, {})
    labels = ("Read settings", "Command line…", "Send advert…", "Sync clock…", "Reboot node…")
    starts = {}
    for item in items:
        if not isinstance(item, Choice) or not hasattr(item.title, "plain"):
            continue
        plain = item.title.plain
        for label in labels:
            if label in plain:
                starts[label] = cell_len(plain[: plain.index(label)])
    assert set(starts) == set(labels)
    assert len(set(starts.values())) == 1, f"a label starts a column early: {starts}"


# --- settings discovered on the command line -------------------------------------------


def test_only_get_and_set_are_read_as_settings() -> None:
    """The CLI's own grammar is the filter; every top-level verb falls out of it.

    Nothing is excluded by name: ``reboot``, ``advert`` and ``powersaving`` are simply not
    ``get <key>`` or ``set <key> <value>``, and ``setperm`` only starts with the letters.
    """
    assert parse_setting_command("get radio.rxps") == SettingCommand("get", "radio.rxps")
    assert parse_setting_command("  set reboot.interval 24  ") == SettingCommand(
        "set", "reboot.interval", "24"
    )
    assert parse_setting_command("set owner.info a b c") == SettingCommand(
        "set", "owner.info", "a b c"
    )
    for command in (
        "reboot",
        "advert",
        "clock sync",
        "powersaving on",
        "setperm ab12 rw",
        "start ota",
        "get",  # no key
        "set tx",  # no value
        "get ../etc",  # not a key's spelling
    ):
        assert parse_setting_command(command) is None, command


def test_a_write_the_node_accepts_is_proof_the_key_is_there(tui_ctx) -> None:
    """``set`` teaches a row: the firmware matches its keys exactly and refuses the rest.

    ``handleSetCmd`` compares ``"radio "`` *with* the trailing space, so a longer key can
    never land in a shorter one's branch, and an unknown one answers ``unknown config:``.
    """
    repeater_admin.learn_from_cli(tui_ctx, NODE, "set radio.rxps balanced", "OK")
    cached = tui_ctx.remote_store.settings(NODE)["radio.rxps"]
    assert (cached.value, cached.discovered) == ("balanced", True)

    repeater_admin.learn_from_cli(tui_ctx, NODE, "set nonesuch 1", "unknown config: nonesuch")
    assert "nonesuch" not in tui_ctx.remote_store.settings(NODE)


def test_a_read_answered_out_of_a_shorter_keys_branch_is_no_discovery(tui_ctx) -> None:
    """``get`` matches on a bare prefix, so a plausible reply can belong to another key.

    Stock firmware answers ``get radio.rxps`` from the ``get radio`` branch. With no typing
    to reject it — a discovered key has none — the guard is that the reply is exactly what
    the shadowing key last said.
    """
    for key, value in (("freq", "910.525"), ("bw", "62.5"), ("sf", "7"), ("cr", "5")):
        tui_ctx.remote_store.remember_setting(NODE, key, value)

    repeater_admin.learn_from_cli(tui_ctx, NODE, "get radio.rxps", "> 910.525,62.5,7,5")
    assert "radio.rxps" not in tui_ctx.remote_store.settings(NODE)

    repeater_admin.learn_from_cli(tui_ctx, NODE, "get radio.rxps", "> desired=off level=2")
    assert tui_ctx.remote_store.settings(NODE)["radio.rxps"].value == "desired=off level=2"


def test_a_read_nothing_shadows_is_taken_at_its_word(tui_ctx) -> None:
    """No catalog key prefixes ``reboot.interval``, so its reply can only be its own."""
    repeater_admin.learn_from_cli(tui_ctx, NODE, "get reboot.interval", "> 24")
    assert tui_ctx.remote_store.settings(NODE)["reboot.interval"].value == "24"

    repeater_admin.learn_from_cli(tui_ctx, NODE, "get nonesuch", "??: nonesuch")
    assert "nonesuch" not in tui_ctx.remote_store.settings(NODE)


def test_an_unread_shadow_leaves_the_question_unanswerable(tui_ctx) -> None:
    """A shadowed key whose shadow was never read is not discovered: we cannot tell."""
    repeater_admin.learn_from_cli(tui_ctx, NODE, "get radio.rxps", "> 910.525,62.5,7,5")
    assert "radio.rxps" not in tui_ctx.remote_store.settings(NODE)


def test_the_private_key_is_never_written_to_disk(tui_ctx, tmp_path: Path) -> None:
    """``get prv.key`` prints in the transcript and goes no further."""
    secret = "ab" * 32
    repeater_admin.learn_from_cli(tui_ctx, NODE, "get prv.key", "> " + secret)
    assert "prv.key" not in tui_ctx.remote_store.settings(NODE)
    written = [p for p in tmp_path.rglob("*.json") if secret in p.read_text(encoding="utf-8")]
    assert written == []


def test_a_catalog_key_read_on_the_command_line_refreshes_its_row(tui_ctx) -> None:
    """The cache is the page, so the command line fills it exactly as a read does.

    ``get tx`` used to leave the TX power row showing what it said last week, and
    ``get radio`` answered four rows that none of them heard about.
    """
    repeater_admin.learn_from_cli(tui_ctx, NODE, "get tx", "> 20")
    repeater_admin.learn_from_cli(tui_ctx, NODE, "get radio", "> 869.525,250,10,6")
    repeater_admin.learn_from_cli(tui_ctx, NODE, "set txdelay 1.5", "OK")
    cache = tui_ctx.remote_store.settings(NODE)
    assert cache["tx"].value == "20"
    assert (cache["freq"].value, cache["sf"].value) == ("869.525", "10")
    assert cache["txdelay"].value == "1.5"
    assert not any(cached.discovered for cached in cache.values())


def test_a_write_stales_the_other_spelling_from_the_command_line_too(tui_ctx) -> None:
    """``set dutycycle`` at the prompt drops the cached ``af``, as an Apply would."""
    tui_ctx.remote_store.remember_setting(NODE, "af", "1.00")
    repeater_admin.learn_from_cli(tui_ctx, NODE, "set dutycycle 40", "OK - 40.0%")
    cache = tui_ctx.remote_store.settings(NODE)
    assert cache["dutycycle"].value == "40.0" and "af" not in cache


def test_no_reply_can_remove_a_discovered_row(tui_ctx) -> None:
    """A read may only ever take a discovered row to ``n/a``; removing it is a decision.

    Its key is recorded nowhere but here, so a reply that misreads — a truncated line, a
    node answering mid-reboot, a frame correlated to the wrong command — must not be able
    to delete the row. A catalog row costs nothing when that happens, the catalog still
    naming the key; this one would cost the reader something only they can restore.
    """
    spec = discovered_setting("radio.rxps")
    tui_ctx.remote_store.remember_discovered(NODE, "radio.rxps", "off")

    repeater_admin.remember_reply(tui_ctx, NODE, [spec], "> balanced")
    cached = tui_ctx.remote_store.settings(NODE)["radio.rxps"]
    assert (cached.value, cached.discovered) == ("balanced", True)

    repeater_admin.remember_reply(tui_ctx, NODE, [spec], "??: radio.rxps")
    cache = tui_ctx.remote_store.settings(NODE)
    assert cache["radio.rxps"].discovered and not cache["radio.rxps"].supported
    assert repeater_admin._value_text(spec, cache, {}).plain == "n/a"
    assert "radio.rxps" in repeater_admin._extra_keys(cache), "still the reader's to forget"


def test_discovered_rows_draw_last_in_their_own_section_and_are_deletable() -> None:
    """A node's own settings sit under ``Extra`` — and only they answer Del."""
    from meshterm.ui.tui import Choice

    plain = {"txdelay": CachedValue("0.5", utcnow())}
    catalog_rows = [
        item
        for item in repeater_admin._menu_items(NODE, plain, {})[1]
        if isinstance(item, Choice) and item.deletable
    ]
    assert catalog_rows == [], "no catalog row may be forgotten"

    cache = dict(plain, **{"radio.rxps": CachedValue("balanced", utcnow(), discovered=True)})
    items = repeater_admin._menu_items(NODE, cache, {})[1]
    deletable = [i for i in items if isinstance(i, Choice) and i.deletable]
    assert [i.value for i in deletable] == ["radio.rxps"]

    rows = [i for i in items if isinstance(i, Choice) and isinstance(i.value, str)]
    settings_rows = [r for r in rows if repeater_admin.get_setting(r.value) or r.deletable]
    assert settings_rows[-1].value == "radio.rxps", "the node's own settings come last"
    assert DISCOVERED_HELP in settings_rows[-1].title.plain


def test_a_staged_discovered_value_is_written_with_its_own_set() -> None:
    """A key the catalog hasn't got is still written the one way anything can write it."""
    writes = write_plan({"radio.rxps": "balanced", "txdelay": "1.5"}, {})
    assert [w.command for w in writes] == ["set txdelay 1.5", "set radio.rxps balanced"]


def test_a_long_discovered_value_cannot_widen_the_whole_page() -> None:
    """The value lane is one width for every row, so a node's own setting may not set it.

    Some builds answer ``get radio.rxps`` with a whole diagnostic line. Unbounded, that one
    row would push every catalog setting's description right and off a 72-column screen —
    reshaping a page the reader opened to read the catalog.
    """
    from rich.cells import cell_len

    from meshterm.ui.tui import Choice

    def widest(cache) -> int:
        items = repeater_admin._menu_items(NODE, cache, {})[1]
        return max(
            cell_len(i.title.plain)
            for i in items
            if isinstance(i, Choice) and hasattr(i.title, "plain")
        )

    catalog = {
        "tx": CachedValue("20", utcnow()),
        "owner.info": CachedValue("Jean-Pierre, Montreal", utcnow()),
    }
    chatty = dict(
        catalog,
        **{
            "radio.rxps": CachedValue(
                "desired=off effective=off supported=yes level=0 preamble=16 rx=0 sleep=0",
                utcnow(),
                discovered=True,
            )
        },
    )
    assert widest(chatty) == widest(catalog)

    fitted = repeater_admin._extra_value("a value far too long for its lane", 14)
    assert cell_len(fitted) == 14
    assert repeater_admin._extra_value("  balanced\n", 14) == "balanced"
