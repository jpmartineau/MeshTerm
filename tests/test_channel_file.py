# SPDX-License-Identifier: Apache-2.0
"""Tests for the channel file: the export, the import, and the plan that an import makes.

The file logic and the plan need no hardware. The export and the import run against the
:class:`MockDevice` simulator, through the CLI tool and through the channel manager. Two
contexts with two configuration directories stand for two computers, or for the old
device and the new device.
"""

from __future__ import annotations

import io
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core.admin_store import AdminStore
from meshterm.core.channel_file import (
    ChannelEntry,
    ChannelFileError,
    apply_preferences,
    plan_import,
    read_channel_file,
    write_channel_file,
)
from meshterm.core.channel_probe import ChannelSlot, read_channel_slots
from meshterm.core.channels import DEFAULT_PUBLIC_SECRET, channel_identity, derive_secret
from meshterm.core.config import Settings
from meshterm.core.device_store import DeviceStore
from meshterm.core.mute_store import MuteStore
from meshterm.core.region_store import RegionStore
from meshterm.persistence.repository import Repository
from meshterm.tools.channels import ChannelsTool
from meshterm.ui.channels import (
    _EXPORT_FILE,
    _IMPORT_FILE,
    _LiveStats,
    _menu_items,
    manage_channels,
)

_FAMILY = bytes(range(16))
_PUBLIC = ChannelEntry("Public", DEFAULT_PUBLIC_SECRET)
_BOTS = ChannelEntry("#bots")
_FAMILY_ENTRY = ChannelEntry("Family", _FAMILY, scope="yul", muted=True)


def _slot(idx: int, name: str, secret: bytes | None = None) -> ChannelSlot:
    """A channel as the device reports it. A ``#`` channel comes back with its derived key."""
    return ChannelSlot(idx=idx, name=name, secret=secret or derive_secret(name))


# -- the file -------------------------------------------------------------------------


def test_a_channel_file_round_trips_names_keys_scopes_and_mutes(tmp_path: Path) -> None:
    """The file gives back each channel, in order, with its key, its scope, and its mute."""
    entries = [_PUBLIC, _BOTS, _FAMILY_ENTRY]
    path = write_channel_file(tmp_path / "channels.toml", entries)

    assert read_channel_file(path) == entries
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# MeshTerm channels.")  # a user who opens it finds what it is
    assert text.count("secret =") == 2  # the # channel has none: the device derives its key


def test_a_config_backup_is_also_a_channel_file(tmp_path: Path) -> None:
    """The ``[[channels]]`` table of ``config backup`` reads as a channel file.

    The backup writes an index and a secret for each channel. The reader does not use the
    index, and it removes the secret of a ``#`` channel, which the device derives.
    """
    from meshterm.core.config_io import backup_config

    channels = [
        {"channel_idx": 0, "channel_name": "#bots", "channel_secret": derive_secret("#bots")},
        {"channel_idx": 1, "channel_name": "Family", "channel_secret": _FAMILY},
    ]
    path = backup_config(tmp_path / "backup.toml", {}, {}, channels)

    entries = read_channel_file(path)
    assert [(e.name, e.secret) for e in entries] == [("#bots", None), ("Family", _FAMILY)]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("", "has no [[channels]] table"),
        ("[[channels]]\nsecret = 'aa'\n", "channel 1 has no name"),
        ("[[channels]]\nname = 'Family'\n", "channel 1 (Family): a private channel needs"),
        ("[[channels]]\nname = 'Family'\nsecret = 'abc'\n", "channel 1 (Family):"),
        ("[[channels]]\nname = '#bots'\nscope = '*'\n", "channel 1 (#bots):"),
        ("[[channels]]\nname = '#bots'\nmuted = 'yes'\n", "muted must be true or false"),
        ("name = = 1\n", "is not a TOML file"),
    ],
    ids=["empty", "no-name", "no-secret", "bad-secret", "bad-scope", "bad-mute", "not-toml"],
)
def test_a_bad_channel_file_names_what_is_wrong(tmp_path: Path, body: str, message: str) -> None:
    """A file that MeshTerm cannot use gives one error, which names the entry."""
    path = tmp_path / "channels.toml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ChannelFileError, match=None) as caught:
        read_channel_file(path)
    assert message in str(caught.value)


def test_a_missing_file_is_a_channel_file_error(tmp_path: Path) -> None:
    """A path with no file is an error that says so, not an OSError from deep inside."""
    with pytest.raises(ChannelFileError, match="no such file"):
        read_channel_file(tmp_path / "absent.toml")


def test_a_second_copy_of_a_channel_is_read_one_time(tmp_path: Path) -> None:
    """The same channel two times in a file is one channel, at its first position."""
    path = write_channel_file(tmp_path / "c.toml", [_BOTS, _PUBLIC, _BOTS])
    assert read_channel_file(path) == [_BOTS, _PUBLIC]


# -- the plan -------------------------------------------------------------------------


def test_a_new_device_gets_the_channels_of_the_file_in_order() -> None:
    """A new device ships with only Public. The import adds the other channels after it."""
    plan = plan_import(
        [_PUBLIC, _BOTS, _FAMILY_ENTRY], [_slot(0, "Public", DEFAULT_PUBLIC_SECRET)], 8
    )

    assert plan.writes == ((1, "#bots", None), (2, "Family", _FAMILY))
    assert plan.layout == ((0, "Public", "keep"), (1, "#bots", "add"), (2, "Family", "add"))
    assert [slot for slot, _ in plan.added] == [1, 2]
    assert plan.present == (_PUBLIC,) and plan.moved == 0


def test_an_import_puts_the_file_first_and_removes_nothing() -> None:
    """The channels of the file come first, the other channels of the device after them.

    The device has gaps (slots 0, 3, and 5). The channels move down to close them, and the
    plan clears slot 5, whose channel moved down. Each channel that the device had is still
    on it, once.
    """
    device = [_slot(0, "#test"), _slot(3, "Public", DEFAULT_PUBLIC_SECRET), _slot(5, "#bots")]
    plan = plan_import([_PUBLIC, _BOTS, _FAMILY_ENTRY], device, 8)

    assert plan.layout == (
        (0, "Public", "move"),
        (1, "#bots", "move"),
        (2, "Family", "add"),
        (3, "#test", "move"),
    )
    assert (5, "", None) in plan.writes  # the second copy of #bots goes
    after = {idx: name for idx, name, _ in plan.writes}
    assert sorted(n for n in after.values() if n) == ["#bots", "#test", "Family", "Public"]


def test_a_full_device_skips_the_last_channels_of_the_file() -> None:
    """When the free slots run out, the plan keeps the first new channels and skips the rest."""
    device = [_slot(0, "#a"), _slot(1, "#b")]
    plan = plan_import([ChannelEntry("#c"), ChannelEntry("#d"), ChannelEntry("#e")], device, 3)

    assert [entry.name for _, entry in plan.added] == ["#c"]
    assert [entry.name for entry in plan.skipped] == ["#d", "#e"]


def test_a_device_that_matches_the_file_needs_no_write() -> None:
    """Nothing to add and nothing to move is a plan with no write."""
    device = [_slot(0, "Public", DEFAULT_PUBLIC_SECRET), _slot(1, "#bots")]
    plan = plan_import([_PUBLIC, _BOTS], device, 8)
    assert not plan.changes_device
    assert plan.layout == ((0, "Public", "keep"), (1, "#bots", "keep"))


def test_the_file_sets_scopes_and_mutes_and_never_clears_them(tmp_path: Path) -> None:
    """An import adds a scope and a mute. A channel with none in the file keeps its own."""
    scopes = RegionStore(tmp_path / "regions.json")
    mutes = MuteStore(tmp_path / "mutes.json")
    bots_id = channel_identity("#bots", derive_secret("#bots"))
    scopes.set_channel_scope(bots_id, "yow")

    changed = apply_preferences([_BOTS, _FAMILY_ENTRY], scopes, mutes)

    family_id = channel_identity("Family", _FAMILY)
    assert changed == 2
    assert scopes.channel_scope(family_id) == "yul" and mutes.is_muted(family_id)
    assert scopes.channel_scope(bots_id) == "yow"  # the file gives #bots no scope: it stays


# -- the CLI and the channel manager --------------------------------------------------


def _ctx(config_dir: Path) -> AppContext:
    """A context on the mock device, with its own configuration directory and stores."""
    settings = Settings(config_dir=config_dir, db_path=config_dir / "chan.db")
    config_dir.mkdir(parents=True, exist_ok=True)
    return AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(config_dir / "devices.json"),
        admin_store=AdminStore(config_dir / "admin.json"),
        mock=True,
    )


async def _old_device(ctx: AppContext) -> None:
    """Give the device three channels, a scope on Family, and a mute on #bots."""
    device = await ctx.device()
    await device.set_channel(0, "Public", DEFAULT_PUBLIC_SECRET)
    await device.set_channel(1, "#bots", None)
    await device.set_channel(2, "Family", _FAMILY)
    ctx.region_store.set_channel_scope(channel_identity("Family", _FAMILY), "yul")
    ctx.mute_store.set_muted(channel_identity("#bots", derive_secret("#bots")), True)


async def test_cli_export_then_import_sets_up_a_new_device(tmp_path: Path) -> None:
    """``export`` on the old device and ``import --file`` on the new one give the same channels.

    The new context has its own stores, as another computer does. Thus the scope and the
    mute that it ends with came from the file.
    """
    tool = ChannelsTool()
    old, new = _ctx(tmp_path / "old"), _ctx(tmp_path / "new")
    try:
        await _old_device(old)
        path = tmp_path / "channels.toml"
        result = await tool.run(old, {"cli_action": "export", "path": path})
        assert result.summary == {"channels": 3} and path.exists()

        await (await new.device()).set_channel(0, "Public", DEFAULT_PUBLIC_SECRET)  # as shipped
        params = {"cli_action": "import_file", "file": path, "dry_run": False}
        result = await tool.run(new, params)

        assert result.summary == {"added": 2, "moved": 0, "skipped": 0, "dry_run": False}
        slots = await read_channel_slots(await new.device())
        assert [s.name for s in slots] == ["Public", "#bots", "Family"]
        assert slots[2].secret == _FAMILY
        assert new.region_store.channel_scope(slots[2].identity) == "yul"
        assert new.mute_store.is_muted(slots[1].identity)
    finally:
        old.repo.close()
        new.repo.close()


async def test_cli_import_dry_run_writes_nothing(tmp_path: Path) -> None:
    """``--dry-run`` gives the plan and leaves the device and the stores as they are."""
    tool = ChannelsTool()
    ctx = _ctx(tmp_path / "home")
    try:
        path = write_channel_file(tmp_path / "c.toml", [_BOTS, _FAMILY_ENTRY])
        params = {"cli_action": "import_file", "file": path, "dry_run": True}
        result = await tool.run(ctx, params)

        assert result.summary == {"added": 2, "moved": 0, "skipped": 0, "dry_run": True}
        assert await read_channel_slots(await ctx.device()) == []
        assert not ctx.mute_store.is_muted(_FAMILY_ENTRY.identity)
    finally:
        ctx.repo.close()


async def test_cli_export_of_a_device_with_no_channels_writes_no_file(tmp_path: Path) -> None:
    """Nothing to export is exit 5, and no file that holds nothing."""
    from meshterm.core import exitcodes

    ctx = _ctx(tmp_path / "home")
    try:
        path = tmp_path / "c.toml"
        result = await ChannelsTool().run(ctx, {"cli_action": "export", "path": path})
        assert result.exit_code == exitcodes.NO_RESULT and not path.exists()
    finally:
        ctx.repo.close()


async def test_cli_import_of_a_bad_file_is_a_usage_error(tmp_path: Path) -> None:
    """A file that is not valid is a bad argument (exit 2). Nothing is sent to the device."""
    import typer

    ctx = _ctx(tmp_path / "home")
    try:
        path = tmp_path / "c.toml"
        path.write_text("[[channels]]\nname = 'Family'\n", encoding="utf-8")
        params = {"cli_action": "import_file", "file": path, "dry_run": False}
        with pytest.raises(typer.BadParameter, match="needs its secret"):
            await ChannelsTool().run(ctx, params)
    finally:
        ctx.repo.close()


class _ScriptedUi:
    """A UI that replays scripted answers, to drive the channel manager with no screen.

    It records the prompt of each dialog and each line that the manager says, so that a
    test can read what the user saw.
    """

    def __init__(self, selects: list, paths: list, dialogs: list) -> None:
        self._selects, self._paths, self._dialogs = list(selects), list(paths), list(dialogs)
        self.prompts: list[str] = []
        self.notes: list[str] = []

    def show(self, *renderables) -> None:  # noqa: ANN002
        pass

    @asynccontextmanager
    async def busy_dialog(self, message: str = "", *, title: str = ""):  # noqa: ANN201
        yield SimpleNamespace(message=message)

    def note(self, markup: str) -> None:
        self.notes.append(markup)

    def ack(self, markup: str) -> None:
        pass

    async def select(self, title: str, items: list, *, default=None):  # noqa: ANN001, ANN201
        return self._selects.pop(0)

    async def path(self, title: str, *, prompt: str = "", default: str = "") -> str | None:
        return self._paths.pop(0)

    async def dialog(self, prompt, buttons, **_kw):  # noqa: ANN001, ANN003, ANN201
        self.prompts.append(getattr(prompt, "plain", str(prompt)))
        return self._dialogs.pop(0)


async def test_the_manager_exports_and_imports_through_its_rows(tmp_path: Path) -> None:
    """The two rows of "Export and import" move the channels to another device.

    The import shows its plan first, and the plan says that nothing is removed.
    """
    old, new = _ctx(tmp_path / "old"), _ctx(tmp_path / "new")
    path = tmp_path / "channels.toml"
    try:
        await _old_device(old)
        old.ui = _ScriptedUi(selects=[_EXPORT_FILE, None], paths=[str(path)], dialogs=[])
        await manage_channels(old)
        assert [e.name for e in read_channel_file(path)] == ["Public", "#bots", "Family"]
        assert "keys of 1 private channel" in old.ui.notes[-1]

        new.ui = ui = _ScriptedUi(
            selects=[_IMPORT_FILE, None], paths=[str(path)], dialogs=["import"]
        )
        await manage_channels(new)

        slots = await read_channel_slots(await new.device())
        assert [s.name for s in slots] == ["Public", "#bots", "Family"]
        assert "into slot 2" in ui.prompts[0] and "Nothing is removed" in ui.prompts[0]
        assert new.region_store.channel_scope(slots[2].identity) == "yul"
    finally:
        old.repo.close()
        new.repo.close()


async def test_export_is_offered_only_when_there_is_a_channel(tmp_path: Path) -> None:
    """With no channel, there is nothing to export. Import is always offered."""
    ctx = _ctx(tmp_path / "home")
    try:

        def values(slots: list) -> list:
            _, items = _menu_items(ctx, slots, 8, _LiveStats(ctx))
            return [getattr(item, "value", None) for item in items]

        assert _EXPORT_FILE not in values([]) and _IMPORT_FILE in values([])
        assert _EXPORT_FILE in values([_slot(0, "#bots")])
    finally:
        ctx.repo.close()
