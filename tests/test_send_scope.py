# SPDX-License-Identifier: Apache-2.0
"""Tests for the sending side of region scopes.

A channel's scope is a window on the companion's one *session* scope: set it, send, put it
back, with nothing else let through in between. These pin that ordering against the
simulator — including what happens when a step fails — then everything built on it: the
recorded scope, the chat's title and its unscoped resend, the paths dialog's line, the
channel page's picker, the CLI, and Device config teaching the default.

Everything runs against :class:`~meshterm.core.connection.MockDevice` and scratch files;
nothing transmits.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshterm.core.channel_probe import ChannelSlot
from meshterm.core.config import Settings
from meshterm.core.connection import (
    DeviceCommandError,
    FloodScopeError,
    MockDevice,
    firmware_version,
)
from meshterm.core.models import ChatMessage, Conversation
from meshterm.core.preferences import Preferences
from meshterm.core.region_store import RegionStore
from meshterm.core.regions import RegionNameError
from meshterm.core.transmit_lock import TransmitLock
from meshterm.persistence import db as db_module
from meshterm.persistence.repository import Repository
from meshterm.services.chat_service import ChatService
from meshterm.services.event_hub import EventHub
from meshterm.ui.chat import ChatScreen, _sent_scope_line
from meshterm.ui.tui import Choice, fkeys

# -- a device that writes down what it was asked, in order --------------------------------


class _TracingDevice(MockDevice):
    """The simulator, keeping a log of every scope change and send in the order made.

    ``refuse`` makes the named scope command fail the way old firmware does; ``version``
    is what the device query reports as its firmware build.
    """

    def __init__(self, *, refuse: set[str] | None = None, version: str = "mock") -> None:
        super().__init__()
        self.trail: list[tuple] = []
        self._refuse = refuse or set()
        self._version = version
        self.fail_restore = False

    async def set_flood_scope(self, region: str | None) -> None:
        key = region if region else None
        if key in self._refuse or (key is None and self.fail_restore):
            raise DeviceCommandError("ERR_CODE_UNSUPPORTED_CMD")
        await super().set_flood_scope(region)
        self.trail.append(("scope", key))

    async def send_channel_message(self, index: int, text: str) -> None:
        await super().send_channel_message(index, text)
        self.trail.append(("channel", text, self._send_scope))

    async def send_direct_message(self, contact, text):  # noqa: ANN001, ANN201
        ack = await super().send_direct_message(contact, text)
        self.trail.append(("dm", text, self._send_scope))
        return ack

    async def get_device_info(self) -> dict:
        return {**await super().get_device_info(), "ver": self._version}


# -- the transmit lock -----------------------------------------------------------------


async def test_transmit_lock_reenters_for_its_holder_and_blocks_everyone_else() -> None:
    """The scoped send re-enters its own lock; another task waits for the whole window."""
    lock = TransmitLock()
    order: list[str] = []

    async def other() -> None:
        async with lock.held():
            order.append("other")

    async with lock.held():
        async with lock.held():  # the same task: must not deadlock
            order.append("inner")
        task = asyncio.ensure_future(other())
        await asyncio.sleep(0.01)
        assert order == ["inner"]  # still waiting behind us
        order.append("outer-done")
    await task
    assert order == ["inner", "outer-done", "other"]
    assert not lock.locked


# -- the three-step window ---------------------------------------------------------------


async def test_scoped_send_sets_then_sends_then_restores() -> None:
    """Scope set → channel send (under it) → restored to the default, in that order."""
    device = _TracingDevice()
    await device.send_channel_in_scope(0, "hello", "lakeside")
    assert device.trail == [
        ("scope", "lakeside"),
        ("channel", "hello", "lakeside"),
        ("scope", None),
    ]
    assert device._send_scope == ""


async def test_plain_send_never_touches_the_session_scope() -> None:
    """No scope asked: the message goes out under the default and nothing is set."""
    device = _TracingDevice()
    await device.send_channel_in_scope(0, "hello", None)
    assert device.trail == [("channel", "hello", "")]


async def test_a_dm_cannot_interleave_into_the_window() -> None:
    """A DM started mid-window waits until the scope is restored, then goes out unscoped."""
    device = _TracingDevice()
    await device.connect()
    alice = next(c for c in await device.get_contacts() if c.name == "Alice")
    entered = asyncio.Event()
    release = asyncio.Event()
    original = device.send_channel_message

    async def slow_send(index: int, text: str) -> None:
        entered.set()
        await release.wait()
        await original(index, text)

    device.send_channel_message = slow_send  # type: ignore[method-assign]
    scoped = asyncio.ensure_future(device.send_channel_in_scope(0, "scoped", "lakeside"))
    await entered.wait()
    dm = asyncio.ensure_future(device.send_direct_message(alice, "dm"))
    await asyncio.sleep(0.01)
    assert not dm.done()  # held behind the window
    release.set()
    await asyncio.gather(scoped, dm)
    assert device.trail == [
        ("scope", "lakeside"),
        ("channel", "scoped", "lakeside"),
        ("scope", None),
        ("dm", "dm", ""),
    ]


async def test_a_refused_scope_sends_nothing() -> None:
    """Old firmware refuses the scope: FloodScopeError, and no message went out at all."""
    device = _TracingDevice(refuse={"lakeside"})
    with pytest.raises(FloodScopeError) as caught:
        await device.send_channel_in_scope(0, "hello", "lakeside")
    assert caught.value.scope == "lakeside"
    assert "1.10" in str(caught.value) and "Nothing was sent" in str(caught.value)
    assert device.trail == []


async def test_a_send_that_fails_still_restores_the_scope() -> None:
    """The channel send raising must not leave the session scope on the channel's region."""
    device = _TracingDevice()

    async def boom(index: int, text: str) -> None:
        raise DeviceCommandError("rejected")

    device.send_channel_message = boom  # type: ignore[method-assign]
    with pytest.raises(DeviceCommandError):
        await device.send_channel_in_scope(0, "hello", "lakeside")
    assert device.trail == [("scope", "lakeside"), ("scope", None)]


async def test_a_failed_restore_is_repaired_before_the_next_transmission() -> None:
    """A restore that failed is retried by the next send, before that send goes out."""
    device = _TracingDevice()
    device.fail_restore = True
    await device.send_channel_in_scope(0, "first", "lakeside")  # the message did go
    assert device._send_scope == "lakeside"
    device.fail_restore = False
    await device.send_advert()
    assert device.trail[-1] == ("scope", None)
    assert device._send_scope == ""


async def test_an_invalid_region_is_refused_before_the_radio_is_asked() -> None:
    """A name the firmware couldn't hold never reaches the device."""
    device = _TracingDevice()
    with pytest.raises(RegionNameError):
        await device.send_channel_in_scope(0, "hello", "two words")
    assert device.trail == []


# -- unscoped (*) and the firmware that can or can't do it ---------------------------------


def test_firmware_version_reads_the_leading_release() -> None:
    """``v1.15.0``, ``1.16.2-dev`` and ``1.9`` all compare; a non-version is None."""
    assert firmware_version({"ver": "v1.15.0"}) == (1, 15, 0)
    assert firmware_version({"ver": "1.16.2-dev"}) == (1, 16, 2)
    assert firmware_version({"ver": "1.9"}) == (1, 9, 0)
    assert firmware_version({"ver": "mock"}) is None
    assert firmware_version({}) is None


async def test_unscoped_uses_the_override_on_new_firmware() -> None:
    """1.16+: ``*`` is set as the session scope, the message goes, the scope is restored."""
    device = _TracingDevice(version="v1.16.0")
    await device.send_channel_in_scope(0, "hello", "*")
    assert device.trail == [("scope", "*"), ("channel", "hello", "*"), ("scope", None)]


async def test_unscoped_on_old_firmware_without_a_default_is_the_plain_send() -> None:
    """1.15 with no default scope: a plain flood already is unscoped, so it just goes."""
    device = _TracingDevice(version="v1.15.0")
    await device.send_channel_in_scope(0, "hello", "*")
    assert device.trail == [("channel", "hello", "")]


async def test_unscoped_on_old_firmware_over_a_default_is_refused() -> None:
    """1.15 with a default set can't override it: refused, and nothing was sent."""
    device = _TracingDevice(version="v1.15.0")
    await device.set_default_flood_scope("lakeside")
    with pytest.raises(FloodScopeError) as caught:
        await device.send_channel_in_scope(0, "hello", "*")
    assert "1.16" in str(caught.value) and "lakeside" in str(caught.value)
    assert device.trail == []


# -- the chat service: which scope, and what gets recorded ----------------------------------


class _Ctx:
    """The slice of AppContext the chat service reads, with a real region store."""

    def __init__(self, device: MockDevice, repo: Repository, tmp: Path) -> None:
        self._device = device
        self.repo = repo
        self.log = logging.getLogger("test.scope")
        self.profile_name = None
        self.settings = Settings()
        self.preferences = Preferences()
        self.events = EventHub(self)
        self.region_store = RegionStore(tmp / "regions.json")
        self.devstate = SimpleNamespace(default_scope=self._default_scope)
        self.default = ""

    async def _default_scope(self) -> str:
        return self.default

    async def device(self) -> MockDevice:
        await self._device.connect()
        return self._device


@pytest.fixture()
def repo(tmp_path: Path) -> Repository:
    """A Repository on a scratch database, closed when the test finishes."""
    r = Repository(tmp_path / "scope.db")
    yield r
    r.close()


async def test_a_channel_send_goes_under_its_scope_and_records_it(
    tmp_path: Path, repo: Repository
) -> None:
    """The channel's scope is looked up by identity, sent under, and kept with the message."""
    device = _TracingDevice()
    ctx = _Ctx(device, repo, tmp_path)
    chat = ChatService(ctx)
    channel_id = await chat.channel_id_for(0)
    ctx.region_store.set_channel_scope(channel_id, "lakeside")

    sent = await chat.send_channel(0, "hello", label="Public")
    assert sent.scope == "lakeside"
    assert ("channel", "hello", "lakeside") in device.trail
    stored = repo.recent_chat_messages(is_channel=True, channel_id=channel_id)
    assert stored[-1].scope == "lakeside" and stored[-1].row_id == sent.row_id


async def test_a_channel_without_a_scope_records_the_default_or_unscoped(
    tmp_path: Path, repo: Repository
) -> None:
    """No channel scope: recorded as the device default, or ``*`` when there is none."""
    ctx = _Ctx(_TracingDevice(), repo, tmp_path)
    chat = ChatService(ctx)
    assert (await chat.send_channel(0, "a")).scope == "*"
    ctx.default = "harbour"
    assert (await chat.send_channel(0, "b")).scope == "harbour"


async def test_the_one_shot_override_sends_unscoped(tmp_path: Path, repo: Repository) -> None:
    """``scope="*"`` overrides the channel's scope for one message and is recorded."""
    device = _TracingDevice()
    ctx = _Ctx(device, repo, tmp_path)
    chat = ChatService(ctx)
    ctx.region_store.set_channel_scope(await chat.channel_id_for(0), "lakeside")
    sent = await chat.send_channel(0, "again", scope="*")
    assert sent.scope == "*"
    assert ("channel", "again", "*") in device.trail


async def test_a_refused_scope_records_nothing(tmp_path: Path, repo: Repository) -> None:
    """A message the radio wouldn't send under its scope is not in the transcript either."""
    ctx = _Ctx(_TracingDevice(refuse={"lakeside"}), repo, tmp_path)
    chat = ChatService(ctx)
    channel_id = await chat.channel_id_for(0)
    ctx.region_store.set_channel_scope(channel_id, "lakeside")
    with pytest.raises(FloodScopeError):
        await chat.send_channel(0, "hello")
    assert repo.recent_chat_messages(is_channel=True, channel_id=channel_id) == []


# -- the schema --------------------------------------------------------------------------


def test_a_v16_database_gains_the_scope_column(tmp_path: Path) -> None:
    """Opening a database from before v17 adds ``messages.scope``, old rows NULL."""
    path = tmp_path / "old.db"
    conn = db_module.connect(path)
    conn.execute("ALTER TABLE messages DROP COLUMN scope")
    conn.execute(
        "INSERT INTO messages (outbound, is_channel, text, created_at) "
        "VALUES (1, 1, 'old', '2026-09-01T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    conn = db_module.connect(path)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
    assert "scope" in cols
    assert conn.execute("SELECT scope FROM messages").fetchone()["scope"] is None
    version = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    assert version["value"] == "17"
    conn.close()


# -- the chat screen -----------------------------------------------------------------------


class _Session:
    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.dialogs: list[str] = []

    def invalidate(self) -> None:
        pass

    def run_detached(self, work):  # noqa: ANN001, ANN201
        return asyncio.ensure_future(work)

    async def button_dialog(self, prompt, buttons, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.dialogs.append(kwargs.get("title", ""))
        return self.answer


def _channel_chat(session, messages, *, scope="lakeside", resend=None) -> ChatScreen:  # noqa: ANN001
    conv = Conversation(label="#ops", is_channel=True, channel_idx=1, channel_id="cid")
    return ChatScreen(
        conv,
        messages,
        send=None,
        names={},
        session=session,
        scope=scope,
        resend_unscoped=resend,
    )


def test_the_chat_title_carries_the_scope_as_a_status_atom() -> None:
    """``#ops · scope lakeside``; no atom at all when the channel has no scope."""
    assert _channel_chat(_Session(), []).title == "#ops · scope lakeside"
    assert _channel_chat(_Session(), [], scope=None).title == "#ops"


async def test_resend_unscoped_targets_only_a_newest_scoped_message() -> None:
    """^R offers the newest sent message when it went under a region, and nothing else."""

    async def resend(message: ChatMessage) -> ChatMessage:
        return ChatMessage(text=message.text, outbound=True, is_channel=True, scope="*")

    scoped = ChatMessage(text="hi", outbound=True, is_channel=True, scope="lakeside")
    screen = _channel_chat(_Session(), [scoped], resend=resend)
    assert screen._retry_target() is scoped
    assert "^R resend unscoped" in screen.footer_hint
    lane = screen.fkey_lane
    assert lane[2].opp_label == "Resend" and lane[2].opp_enabled

    screen.handle("retry")
    await asyncio.sleep(0.05)
    assert screen._messages[-1].scope == "*"
    assert screen._retry_target() is None  # the unscoped copy is now the newest
    assert "^R" not in screen.footer_hint
    assert "unscoped" in _plain_lines(screen)


async def test_a_declined_resend_sends_nothing() -> None:
    """Cancel on the amber confirm leaves the transcript as it was."""
    calls: list[str] = []

    async def resend(message: ChatMessage) -> ChatMessage:
        calls.append(message.text)
        return message

    scoped = ChatMessage(text="hi", outbound=True, is_channel=True, scope="lakeside")
    session = _Session(answer=False)
    screen = _channel_chat(session, [scoped], resend=resend)
    screen.handle("retry")
    await asyncio.sleep(0.05)
    assert session.dialogs == ["Resend unscoped"] and calls == []


def test_a_channel_with_nothing_to_resend_dims_the_chip() -> None:
    """An unscoped newest message leaves Resend on the lane, dim — present but unavailable."""
    unscoped = ChatMessage(text="hi", outbound=True, is_channel=True, scope="*")

    async def resend(message: ChatMessage) -> ChatMessage:  # pragma: no cover - never called
        return message

    screen = _channel_chat(_Session(), [unscoped], resend=resend)
    lane = screen.fkey_lane
    assert lane[2].opp_label == "Resend" and not lane[2].opp_enabled
    assert fkeys.lane_text(lane, shifted=True).plain


def _plain_lines(screen: ChatScreen) -> str:
    from tests.conftest import plain

    return plain(screen.render_body(72))


# -- the paths dialog's line ----------------------------------------------------------------


def test_the_paths_line_says_when_no_known_repeater_carries_the_scope(tmp_path: Path) -> None:
    """Scoped, nothing relayed, no carrier known: said plainly; a carrier changes the tail."""
    store = RegionStore(tmp_path / "regions.json")
    ctx = SimpleNamespace(region_store=store)
    sent = ChatMessage(text="hi", outbound=True, is_channel=True, scope="lakeside")

    line = _sent_scope_line(ctx, sent, relayed=False)
    assert line.plain == "sent under scope lakeside — no repeater known here carries it"

    store.learn("lakeside", "repeater", repeater="3d63c6429436")
    assert _sent_scope_line(ctx, sent, relayed=False).plain == (
        "sent under scope lakeside · 1 known repeater carries it"
    )
    unscoped = ChatMessage(text="hi", outbound=True, is_channel=True, scope="*")
    assert _sent_scope_line(ctx, unscoped, relayed=True).plain == "sent unscoped"
    inbound = ChatMessage(text="hi", is_channel=True)
    assert _sent_scope_line(ctx, inbound, relayed=True) is None


# -- the channel page's picker ---------------------------------------------------------------


class _PickerUi:
    """Replays scripted select/text answers; records the rows each select offered."""

    def __init__(self, selects: list, texts: list) -> None:
        self._selects = list(selects)
        self._texts = list(texts)
        self.offered: list[list] = []

    async def select(self, title, items, *, prompt="", default=None):  # noqa: ANN001, ANN201
        self.offered.append(items)
        return self._selects.pop(0)

    async def text(self, title, *, prompt="", default="", validate=None):  # noqa: ANN001, ANN201
        answer = self._texts.pop(0)
        if answer is not None and validate is not None:
            assert validate(answer) is True
        return answer


async def test_the_picker_sets_types_steps_back_and_clears(tmp_path: Path) -> None:
    """Pick a known region; type one (Esc on the field comes back to the list); clear."""
    from meshterm.ui.channels import _NO_SCOPE, _TYPE_REGION, _detail_summary, _pick_scope

    store = RegionStore(tmp_path / "regions.json")
    store.learn("harbour", "repeater", repeater="3d63c6429436")
    slot = ChannelSlot(idx=1, name="Ops", secret=bytes(range(16)))

    ui = _PickerUi(selects=["harbour"], texts=[])
    ctx = SimpleNamespace(region_store=store, ui=ui)
    assert await _pick_scope(ctx, slot) is True
    assert store.channel_scope(slot.identity) == "harbour"
    labels = [item.label.plain for item in ui.offered[0] if isinstance(item, Choice)]
    assert labels[1] == "harbour  · 1 repeater"

    ctx.ui = _PickerUi(selects=[_TYPE_REGION, _TYPE_REGION], texts=[None, "yul"])
    assert await _pick_scope(ctx, slot) is True
    assert store.channel_scope(slot.identity) == "yul"
    assert "typed" in store.get("yul").sources

    ctx.ui = _PickerUi(selects=[_NO_SCOPE], texts=[])
    assert await _pick_scope(ctx, slot) is True
    assert store.channel_scope(slot.identity) is None

    ctx.ui = _PickerUi(selects=[None], texts=[])
    assert await _pick_scope(ctx, slot) is False  # Esc keeps

    store.set_channel_scope(slot.identity, "yul")
    stats = SimpleNamespace(get=lambda identity: None)
    summary_ctx = SimpleNamespace(
        region_store=store,
        chat=SimpleNamespace(unread=lambda key: 0),
        mute_store=SimpleNamespace(is_muted=lambda identity: False),
    )
    assert "scope yul" in _detail_summary(summary_ctx, slot, stats)


# -- the CLI ------------------------------------------------------------------------------


@pytest.fixture()
def run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """The real Typer app against the simulator, in a config directory of its own."""
    from typer.testing import CliRunner

    from meshterm.cli import app

    monkeypatch.setenv("MESHTERM_HOME", str(tmp_path / "home"))
    runner = CliRunner()

    def invoke(*args: str):  # noqa: ANN202
        return runner.invoke(app, ["--mock", "--db", str(tmp_path / "test.db"), *args])

    return invoke


async def test_cli_channel_scope_reads_sets_and_clears(tmp_path: Path) -> None:
    """``channels scope`` reads (exit 5 for none), sets, clears — keyed by the channel."""
    from io import StringIO

    from rich.console import Console

    from meshterm.context import AppContext
    from meshterm.core.admin_store import AdminStore
    from meshterm.core.device_store import DeviceStore
    from meshterm.tools.channels import ChannelsTool

    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "chan.db")
    ctx = AppContext(
        console=Console(file=StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    tool = ChannelsTool()
    try:
        await tool.run(ctx, {"cli_action": "add", "index": 1, "name": "Ops", "secret": None})
        read = {"cli_action": "scope", "index": 1, "region": None, "clear": False}
        empty = await tool.run(ctx, read)
        assert empty.summary["scope"] is None and empty.exit_code == 5

        await tool.run(ctx, {**read, "region": "lakeside"})
        again = await tool.run(ctx, read)
        assert again.summary["scope"] == "lakeside" and again.exit_code == 0
        assert again.report[0].values["channel"].name == "Ops"

        listed = await tool.run(ctx, {"cli_action": "list"})
        assert [row["scope"] for row in listed.report[0].rows] == ["lakeside"]

        cleared = await tool.run(ctx, {**read, "clear": True})
        assert cleared.summary["scope"] is None and cleared.exit_code == 0
    finally:
        ctx.repo.close()


def test_cli_channel_scope_refuses_bad_arguments(run) -> None:  # noqa: ANN001
    """A bad name or a name with --clear is a usage error; an empty slot is exit 5 + a doc."""
    assert run("channels", "scope", "0", "two words").exit_code == 2
    assert run("channels", "scope", "0", "yul", "--clear").exit_code == 2
    result = run("--json", "channels", "scope", "0")
    assert result.exit_code == 5
    assert json.loads(result.stdout) == {"channel": None, "scope": None}


def test_cli_send_scope_is_a_channel_option_and_reports_its_scope(run) -> None:  # noqa: ANN001
    """``--scope *`` goes out unscoped and says so; on a DM or a bad name it's a usage error."""
    doc = json.loads(run("--json", "chat", "send", "hi", "--channel", "0", "--scope", "*").stdout)
    assert doc["scope"] == {"state": "unscoped", "region": None, "code": None}
    doc = json.loads(run("--json", "chat", "send", "hi", "--channel", "0", "--scope", "yul").stdout)
    assert doc["scope"]["region"] == "yul"
    assert run("chat", "send", "hi", "--to", "Alice", "--scope", "yul").exit_code == 2
    assert run("chat", "send", "hi", "--channel", "0", "--scope", "$x").exit_code == 2


# -- Device config teaches the default ---------------------------------------------------------


def test_a_config_read_learns_the_default_scope(tmp_path: Path) -> None:
    """A snapshot carrying the default scope teaches the store and seeds the cache."""
    from meshterm.tools.config import learn_default_scope

    noted: list = []
    store = RegionStore(tmp_path / "regions.json")
    ctx = SimpleNamespace(
        region_store=store, devstate=SimpleNamespace(note_default_scope=noted.append)
    )
    learn_default_scope(ctx, {"flood_scope": "#harbour"})
    assert store.get("harbour").sources == ("default",)
    assert noted == ["harbour"]
    learn_default_scope(ctx, {})  # the read failed: nothing learned, nothing noted
    assert noted == ["harbour"]
    learn_default_scope(ctx, {"flood_scope": ""})
    assert noted == ["harbour", ""]


async def test_a_channel_chat_sends_under_its_scope_end_to_end(tmp_path: Path) -> None:
    """Typing in a scoped channel's chat, through the real session, sends under the scope."""
    from io import StringIO

    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from rich.console import Console

    from meshterm.context import AppContext
    from meshterm.core.admin_store import AdminStore
    from meshterm.core.device_store import DeviceStore
    from meshterm.ui.chat import open_chat
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.tui.session import TuiSession

    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "e2e.db")
    ctx = AppContext(
        console=Console(file=StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    try:
        device = await ctx.device()
        await device.set_channel(0, "Public", bytes(16))
        channel_id = await ctx.chat.channel_id_for(0)
        ctx.region_store.set_channel_scope(channel_id, "lakeside")
        conv = Conversation(label="Public", is_channel=True, channel_idx=0, channel_id=channel_id)
        with create_pipe_input() as inp:
            session = TuiSession(input=inp, output=DummyOutput())
            ctx.ui = TuiUi(session)

            async def main() -> None:
                inp.send_text("hi\r")
                task = asyncio.ensure_future(open_chat(ctx, conv))
                await asyncio.sleep(0.3)
                inp.send_text("\x1b")
                await task

            await asyncio.wait_for(session.run(main()), timeout=5)

        stored = ctx.repo.recent_chat_messages(is_channel=True, channel_id=channel_id)
        assert [(m.text, m.scope) for m in stored] == [("hi", "lakeside")]
        assert device.sent_channel == [(0, "hi", "lakeside")]
        assert device._send_scope == ""  # restored after the send
    finally:
        await ctx.aclose()
