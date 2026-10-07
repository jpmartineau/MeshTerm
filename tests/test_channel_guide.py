# SPDX-License-Identifier: Apache-2.0
"""Tests for the channel guide: the candidate names, the match, the screen, and the CLI.

Each packet here is built as the firmware builds it (PyCryptodome, as the decryption tests
do), so that the match of the guide, which uses the ``hmac`` module, is checked against the
same MAC that the firmware writes. The channel names are invented.
"""

from __future__ import annotations

import io
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from meshterm.context import AppContext
from meshterm.core import exitcodes
from meshterm.core.admin_store import AdminStore
from meshterm.core.channel_guide import (
    GUIDE_BUCKETS,
    GUIDE_WINDOW,
    MIN_MATCHES,
    build_guide,
    mention_names,
)
from meshterm.core.channel_probe import read_channel_slots
from meshterm.core.channels import (
    DEFAULT_PUBLIC_SECRET,
    channel_hash,
    derive_secret,
    identify_channel,
)
from meshterm.core.config import Settings
from meshterm.core.device_store import DeviceStore
from meshterm.core.models import Observation
from meshterm.persistence.repository import Repository
from meshterm.tools.channels import ChannelsTool
from meshterm.ui import channel_guide as guide_screen

_NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def _packet(name: str, text: str, *, key: bytes | None = None) -> tuple[str, str, str]:
    """One channel packet as the firmware builds it: ``(chan_hash, cipher_mac, crypted)``."""
    from Crypto.Cipher import AES
    from Crypto.Hash import HMAC, SHA256

    key = key or derive_secret(name)
    plain = (1_700_000_000).to_bytes(4, "little") + b"\x00" + text.encode("utf-8")
    plain += b"\x00" * (-len(plain) % 16)
    crypted = AES.new(key, AES.MODE_ECB).encrypt(plain)
    mac = HMAC.new(key, digestmod=SHA256)
    mac.update(crypted)
    return channel_hash(key), mac.digest()[:2].hex(), crypted.hex()


def _row(name: str, text: str, *, hours: float = 1, key: bytes | None = None) -> tuple:
    """A row as :meth:`Repository.channel_packets` gives it, heard ``hours`` before now."""
    when = (_NOW - timedelta(hours=hours)).isoformat()
    return (*_packet(name, text, key=key), when, when)


# -- the names --------------------------------------------------------------------------


def test_mentions_give_each_name_as_written_and_in_lowercase() -> None:
    """A ``#name`` gives two candidates, because the key comes from the exact bytes."""
    names = mention_names(["join #Harbour or #ops-2, not C# or ##x", None, "net #10"])
    assert names == {"#Harbour", "#harbour", "#ops-2", "#10"}


def test_the_match_of_the_guide_is_the_mac_that_the_firmware_writes() -> None:
    """The ``hmac`` check names the same channel as :func:`identify_channel` does."""
    packet = _packet("#harbour", "hello")
    assert identify_channel(*packet, [("#harbour", derive_secret("#harbour"))]) is not None
    rows = [(*packet, None, None), (*_packet("#harbour", "again"), None, None)]
    guide = build_guide(rows, [], {"#harbour"}, now=_NOW)
    assert [c.name for c in guide.channels] == ["#harbour"]


# -- the guide --------------------------------------------------------------------------


def test_a_guessed_name_needs_two_messages() -> None:
    """One match of a guessed name is not proof, so the message stays unnamed."""
    assert MIN_MATCHES == 2
    rows = [_row("#harbour", "one"), _row("#ops", "one"), _row("#ops", "two")]
    guide = build_guide(rows, [], {"#harbour", "#ops"}, now=_NOW)
    assert [c.name for c in guide.channels] == ["#ops"]
    assert guide.unnamed == 1


def test_a_channel_with_a_known_key_needs_one_message() -> None:
    """A channel of the device is not a guess. Public is always known."""
    rows = [_row("Public", "hi", key=DEFAULT_PUBLIC_SECRET), _row("#harbour", "hi")]
    guide = build_guide(rows, [("#harbour", derive_secret("#harbour"))], set(), now=_NOW)
    assert sorted(c.name for c in guide.channels) == ["#harbour", "Public"]
    assert guide.unnamed == 0


def test_a_private_channel_names_its_packets_but_is_not_in_the_guide() -> None:
    """The guide lists public channels. A private channel of the device is not unnamed."""
    family = bytes(range(16))
    rows = [_row("Family", "hi", key=family), _row("#elsewhere", "hi")]
    guide = build_guide(rows, [("Family", family)], set(), now=_NOW)
    assert guide.channels == ()
    assert guide.unnamed == 1  # only the message on the channel that no key matched


def test_the_guide_counts_and_charts_each_channel() -> None:
    """The counts, the times, and the chart of a channel, the most recent channel first."""
    rows = [
        _row("#ops", "a", hours=1),
        _row("#ops", "b", hours=7),
        _row("#ops", "c", hours=24 * 30),  # outside the chart, still counted
        _row("#harbour", "a", hours=0.5),
        _row("#harbour", "b", hours=2),
    ]
    guide = build_guide(rows, [], {"#ops", "#harbour"}, now=_NOW)
    harbour, ops = guide.channels
    assert (harbour.name, ops.name) == ("#harbour", "#ops")  # the most recent first
    assert ops.messages == 3
    assert ops.last_heard == _NOW - timedelta(hours=1)
    assert ops.first_heard == _NOW - timedelta(days=30)
    span = GUIDE_WINDOW / GUIDE_BUCKETS  # six hours
    assert len(ops.histogram) == GUIDE_BUCKETS
    assert ops.histogram[0] == 1 and ops.histogram[int(timedelta(hours=7) / span)] == 1
    assert sum(ops.histogram) == 2


def test_the_repository_gives_one_row_for_each_message(tmp_path: Path) -> None:
    """Copies that repeaters relayed are one message, heard first and last at two times."""
    repo = Repository(tmp_path / "chan.db")
    try:
        run = repo.start_run("monitor", {}, None)
        chan_hash, mac, crypted = _packet("#harbour", "hi")
        raw = {"payload_typename": "GRP_TXT", "chan_hash": chan_hash}
        raw |= {"cipher_mac": mac, "crypted": crypted}
        for _ in range(3):
            repo.record_observation(run, Observation(node="src", kind="packet", raw=raw))
        rows = repo.channel_packets()
        assert len(rows) == 1 and rows[0][:3] == (chan_hash, mac, crypted)
    finally:
        repo.close()


# -- the screen and the CLI -------------------------------------------------------------


def _ctx(config_dir: Path) -> AppContext:
    """A context on the mock device, with its own configuration directory and database."""
    config_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(config_dir=config_dir, db_path=config_dir / "chan.db")
    return AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(config_dir / "devices.json"),
        admin_store=AdminStore(config_dir / "admin.json"),
        mock=True,
    )


def _hear(ctx: AppContext, name: str, *texts: str) -> None:
    """Store one packet for each text, and one message that mentions the channel."""
    run = ctx.repo.start_run("monitor", {}, None)
    for text in texts:
        chan_hash, mac, crypted = _packet(name, text)
        raw = {"payload_typename": "GRP_TXT", "chan_hash": chan_hash}
        raw |= {"cipher_mac": mac, "crypted": crypted}
        ctx.repo.record_observation(run, Observation(node="src", kind="packet", raw=raw))
    from meshterm.core.models import ChatMessage

    ctx.repo.record_chat_message(ChatMessage(text=f"Alice: try {name}", is_channel=True))


class _ScriptedUi:
    """A UI that replays the selects, to drive the guide with no screen."""

    def __init__(self, selects: list) -> None:
        self._selects = list(selects)
        self.titles: list[str] = []

    @asynccontextmanager
    async def busy_dialog(self, message: str = "", *, title: str = ""):  # noqa: ANN201
        yield SimpleNamespace(message=message)

    def note(self, markup: str) -> None:
        pass

    async def select(self, title: str, items: list, *, default=None):  # noqa: ANN001, ANN201
        self.titles.append(title)
        return self._selects.pop(0)


async def test_enter_on_a_heard_channel_adds_it_to_the_device(tmp_path: Path) -> None:
    """The guide adds the highlighted channel to the next free slot, with its derived key."""
    ctx = _ctx(tmp_path / "home")
    guide_screen._cache = None
    try:
        _hear(ctx, "#harbour", "one", "two")
        target = derive_secret("#harbour")
        identity = next(
            c.identity for c in (await guide_screen.read_guide(ctx, [])).channels if c.key == target
        )
        ctx.ui = _ScriptedUi(selects=[identity, None])
        added = await guide_screen.channel_guide(ctx, await ctx.device(), 8)

        assert added == 1
        slots = await read_channel_slots(await ctx.device())
        assert [s.name for s in slots] == ["#harbour"] and slots[0].identity == identity
    finally:
        ctx.repo.close()


async def test_cli_guide_lists_the_heard_channels(tmp_path: Path) -> None:
    """``channels guide`` gives one record for each public channel heard."""
    ctx = _ctx(tmp_path / "home")
    guide_screen._cache = None
    try:
        _hear(ctx, "#harbour", "one", "two")
        result = await ChannelsTool().run(ctx, {"cli_action": "guide"})
        assert result.summary == {"channels": 1, "unnamed": 0}
        (listing,) = result.report
        assert [row["name"] for row in listing.rows] == ["#harbour"]
        assert listing.rows[0]["messages"] == 2 and listing.rows[0]["on_device"] is False
    finally:
        ctx.repo.close()


async def test_cli_guide_with_nothing_heard_is_exit_5(tmp_path: Path) -> None:
    """No public channel heard is nothing to report."""
    ctx = _ctx(tmp_path / "home")
    guide_screen._cache = None
    try:
        result = await ChannelsTool().run(ctx, {"cli_action": "guide"})
        assert result.exit_code == exitcodes.NO_RESULT
    finally:
        ctx.repo.close()
