# SPDX-License-Identifier: Apache-2.0
"""The ``config`` tool: each device setting, and the operations on the device itself.

In the menu, it opens the Device config page (refer to :mod:`meshterm.ui.config_editor`).
There, the settings are staged and applied in place, and the clock, backup and restore,
the identity key, reboot, and factory reset are actions below them. On the CLI, it has
generic key and value subcommands, and also backup and restore, channels, custom
variables, and a set of destructive operations that must have ``--yes``. All of these go
through :func:`apply_ops`. The Apply and the actions of the page, :meth:`ConfigTool.run`
for the scripted commands, and the sends of the standalone ``advert`` tool all run
through the one executor.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core.channels import CHANNEL_SLOT_PROBE_CAP
from ..core.config_io import backup_config, plan_restore, read_backup
from ..core.connection import Device, DeviceCommandError
from ..core.device_config import (
    SettingSpec,
    build_snapshot,
    format_value,
    get_spec,
    parse_value,
    settings_by_category,
)
from ..services.clock_sync import set_clock
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ui.report import Block, Facts, Listing, Report


@register
class ConfigTool(Tool):
    """Show and edit all the settings of the connected device."""

    name = "config"
    title = "Device config"
    icon = "🔧"
    help = "Settings and actions — radio, backup, reboot, …"
    category = "This node"
    order = 20

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Run the Device config page. There are never parameters to collect.

        The page itself applies the staged values and runs its actions, and logs a ``runs``
        row for each Apply (the same pattern as ``repeater-admin``). Thus, when the user
        leaves, :meth:`run` has nothing more to do.

        Args:
            ctx: The shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.config_editor import edit_config

        await edit_config(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Do a list of setting operations on the device.

        Args:
            ctx: The shared application context.
            params: ``ops``: a list of operation tuples (refer to the module docstring).

        Returns:
            A :class:`ToolResult` with a summary of how many values changed.
        """
        device = await ctx.device()
        ops: list[tuple] = list(params.get("ops") or [])
        snapshot = await build_snapshot(device)
        learn_default_scope(ctx, snapshot)
        changes, artifacts, report = await apply_ops(ctx, device, snapshot, ops)

        plural = "" if changes == 1 else "s"
        message = f"[ok]✓[/ok] applied [brand]{changes}[/brand] change{plural}" if changes else None
        return ToolResult(
            summary={"changes": changes},
            message=message,
            artifacts=artifacts,
            report=report or None,
        )

    # -- CLI --------------------------------------------------------------------

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``config`` subcommand group.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        config_app = typer.Typer(help=self.help, no_args_is_help=False, rich_markup_mode=None)

        @config_app.callback(invoke_without_command=True)
        def _root(ctx: typer.Context) -> None:
            """Show all the current settings when no subcommand is given."""
            if ctx.invoked_subcommand is None:
                run_tool_command(self, {"ops": [("show",)]})

        @config_app.command("show", help="Show all current settings")
        def _show_cmd() -> None:
            run_tool_command(self, {"ops": [("show",)]})

        @config_app.command("get", help="Print one setting's value")
        def _get_cmd(key: str = typer.Argument(..., help="Setting key")) -> None:
            run_tool_command(self, {"ops": [("get", key)]})

        @config_app.command("set", help="Set one setting to a value")
        def _set_cmd(
            key: str = typer.Argument(..., help="Setting key"),
            value: str = typer.Argument(..., help="New value"),
        ) -> None:
            run_tool_command(self, {"ops": [("set", key, value)]})

        @config_app.command("backup", help="Write all settings to a TOML file")
        def _backup_cmd(path: Path = typer.Argument(..., help="Destination file")) -> None:
            run_tool_command(self, {"ops": [("backup", path)]})

        @config_app.command("restore", help="Apply settings from a TOML backup")
        def _restore_cmd(
            path: Path = typer.Argument(..., help="Backup file"),
            dry_run: bool = typer.Option(False, "--dry-run", help="Preview without applying"),
        ) -> None:
            run_tool_command(self, {"ops": [("restore", path, dry_run)]})

        @config_app.command("custom", help="Set an experimental custom variable")
        def _custom_cmd(
            key: str = typer.Argument(..., help="Variable name"),
            value: str = typer.Argument(..., help="Variable value"),
        ) -> None:
            run_tool_command(self, {"ops": [("set_custom", key, value)]})

        @config_app.command("channel", help="Configure a channel slot")
        def _channel_cmd(
            index: int = typer.Argument(..., help="Channel slot index"),
            name: str = typer.Argument(..., help="Channel name (# derives the secret)"),
            secret: str | None = typer.Option(None, "--secret", help="16-byte hex secret"),
        ) -> None:
            secret_bytes = bytes.fromhex(secret) if secret else None
            run_tool_command(self, {"ops": [("set_channel", index, name, secret_bytes)]})

        @config_app.command("advert", help="Broadcast an advertisement (zero-hop by default)")
        def _advert_cmd(
            flood: bool = typer.Option(False, "--flood", help="Flood across the mesh"),
        ) -> None:
            run_tool_command(self, {"ops": [("advert", flood)]})

        @config_app.command("share", help="Show this node's contact card as a QR code / URI")
        def _share_cmd() -> None:
            run_tool_command(self, {"ops": [("share",)]})

        @config_app.command("export-key", help="Export the private key (sensitive)")
        def _export_key_cmd(
            out: Path | None = typer.Option(None, "--out", help="Write to file instead of stdout"),
        ) -> None:
            ops = [("export_key", out)] if out else [("export_key",)]
            run_tool_command(self, {"ops": ops})

        @config_app.command("import-key", help="Import a private key (overwrites identity)")
        def _import_key_cmd(
            key_hex: str = typer.Argument(..., help="Private key as hex"),
            yes: bool = typer.Option(False, "--yes", help="Confirm this destructive action"),
        ) -> None:
            _require_yes(yes, "import-key overwrites the device identity")
            run_tool_command(self, {"ops": [("import_key", key_hex)]})

        @config_app.command("sync-clock", help="Set the device clock from this computer")
        def _sync_clock_cmd() -> None:
            run_tool_command(self, {"ops": [("sync_clock",)]})

        @config_app.command("reboot", help="Reboot the device")
        def _reboot_cmd(
            yes: bool = typer.Option(False, "--yes", help="Confirm reboot"),
        ) -> None:
            _require_yes(yes, "reboot restarts the device")
            run_tool_command(self, {"ops": [("reboot",)]})

        @config_app.command("factory-reset", help="Erase all data and reset to defaults")
        def _factory_reset_cmd(
            yes: bool = typer.Option(False, "--yes", help="Confirm this destructive action"),
        ) -> None:
            _require_yes(yes, "factory-reset erases ALL data on the device")
            run_tool_command(self, {"ops": [("factory_reset",)]})

        app.add_typer(config_app, name=self.name)


# -- op execution (the tool and the interactive editor share it) --------------


async def apply_ops(
    ctx: AppContext, device: Device, snapshot: dict, ops: list[tuple]
) -> tuple[int, list[str], Report]:
    """Do a list of setting operation tuples on ``device``.

    This is the only executor for all the setting operations. :meth:`ConfigTool.run` (the
    scripted commands) uses it, and the Device config page uses it too: for its Apply, one
    staged value at a time, and for its actions (reboot, backup and restore, key
    management, factory reset). The actions run immediately, and are not staged.

    Args:
        ctx: The shared application context (for console output).
        device: The connected device on which to do the operations.
        snapshot: The current device snapshot. It is changed in place when settings
            change, so that coupled commands are made again from the current values.
        ops: The operation tuples (refer to the module docstring).

    Returns:
        A ``(changes, artifacts, report)`` triple: the number of applied value changes,
        the paths of the files that were made, and the blocks that state what occurred
        (empty for the menu, which shows its own acknowledgements).
    """
    changes = 0
    artifacts: list[str] = []
    report: list[Any] = []
    applied: list[dict[str, Any]] = []
    for op in ops:
        kind = op[0]
        if kind == "show":
            block = await _show(ctx, device, snapshot)
            if block is not None:
                report.append(block)
        elif kind == "get":
            # Only the bare value. The caller named the key, so if the output repeats it,
            # `$(meshterm config get name)` must remove one more thing. `sysctl -n` and
            # `git config --get` print the same shape. The document keeps the key, the type,
            # and the label of an enum, and the bare form has space for none of them.
            spec = get_spec(op[1])
            report.append(_one_setting(spec, spec.getter(snapshot)))
        elif kind == "set":
            change = await _apply_setting(ctx, device, op[1], op[2], snapshot)
            if change is not None:
                applied.append(change)
                changes += 1
        elif kind == "set_custom":
            previous = (await device.get_custom_vars()).get(op[1])
            await device.set_custom_var(op[1], op[2])
            ctx.ui.ack(f"[ok]✓[/ok] custom [brand]{op[1]}[/brand] = {op[2]}")
            applied.append({"key": f"custom.{op[1]}", "previous": previous, "value": op[2]})
            changes += 1
        elif kind == "set_channel":
            await device.set_channel(op[1], op[2], op[3])
            ctx.ui.ack(f"[ok]✓[/ok] channel {op[1]} = [brand]{op[2]}[/brand]")
            changes += 1
            report.append(_channel_written(op[1], op[2], op[3]))
        elif kind == "backup":
            written, counts = await _backup(device, snapshot, op[1])
            artifacts.append(str(written))
            report.append(_backed_up(written, counts))
        elif kind == "restore":
            done, blocks = await _restore(ctx, device, snapshot, op[1], op[2])
            changes += done
            report.extend(blocks)
        elif kind == "advert":
            flood = len(op) > 1 and bool(op[1])
            await device.send_advert(flood)
            # A flood advert, however it was requested, starts the week of this device again.
            # The weekly advert goes out only a full week after the last one (refer to
            # AdvertScheduler). A zero-hop advert gets only to the neighbours, and resets
            # nothing.
            public_key = str(snapshot.get("public_key") or "")
            if flood and public_key:
                ctx.advert_store.mark_flood(public_key)
            kind_label = "flood" if flood else "zero-hop"
            ctx.ui.ack(f"[ok]✓[/ok] {kind_label} advertisement sent")
            report.append(_acted("advert", sent=True, flood=flood))
        elif kind == "share":
            report.append(await _share_contact(ctx, snapshot))
        elif kind == "sync_clock":
            # The same step that the on-connect service runs in the background
            # (:mod:`meshterm.services.clock_sync`). Here, it is acknowledged and reported.
            done = await set_clock(device)
            if done.written:
                ctx.ui.ack(f"[ok]✓[/ok] device clock set to [brand]{done.stamp}[/brand]")
            else:
                ctx.ui.ack(
                    f"[ok]✓[/ok] device clock already in sync "
                    f"([muted]{done.drift_s:+d} s, ahead of this computer's[/muted])"
                )
            changes += int(done.written)
            report.append(
                _acted(
                    "clock",
                    changes=int(done.written),
                    set_at=done.set_at,
                    # The clock error *before* the set. That is the fact to log, and when
                    # this command succeeds, it removes that fact.
                    drift_s=done.drift_s,
                )
            )
        elif kind == "reboot":
            await device.reboot()
            ctx.ui.ack("[warn]device rebooting[/warn]")
            report.append(_acted("reboot", rebooted=True))
            if ctx.active_transport is not None:
                # When this runs from the command line of the menu, it gives control to the
                # reconnect dialog, as the reboot of the config editor does. A scripted run
                # has no watcher to wake.
                ctx.announce_reboot()
        elif kind == "export_key":
            report.append(await _export_key(ctx, device, op[1] if len(op) > 1 else None, artifacts))
        elif kind == "import_key":
            await device.import_private_key(op[1])
            ctx.ui.ack("[ok]✓[/ok] private key imported")
            changes += 1
            report.append(_acted("imported", changes=1, imported=True))
        elif kind == "factory_reset":
            await device.factory_reset()
            ctx.ui.ack("[err]device factory-reset[/err]")
            report.append(_acted("factory_reset", factory_reset=True))
        else:  # pragma: no cover - the call sites that make ops prevent this
            raise DeviceCommandError(f"unknown config operation: {kind}")
    if applied:
        report.append(_applied(applied))
    # Remove the facts in the session cache that these ops can have changed. Thus the next
    # screen reads the true values again, not an old copy (refer to
    # meshterm.services.device_state.DeviceState). A reboot or a reconnect resets the full
    # cache itself. These lines are for the edits in place.
    kinds = {op[0] for op in ops}
    _WHOLESALE = {"restore", "import_key", "factory_reset"}
    if kinds & _WHOLESALE:
        ctx.devstate.reset()
    else:
        if "set" in kinds:
            ctx.devstate.invalidate_config()  # the self-info fields and the path hash mode
        if "set_channel" in kinds:
            ctx.devstate.invalidate_channels()
    return changes, artifacts, tuple(report)


async def _apply_setting(
    ctx: AppContext, device: Device, key: str, raw: Any, snapshot: dict
) -> dict[str, Any]:
    """Parse, apply, and store one setting, and keep ``snapshot`` consistent.

    The local snapshot gets the new value. Thus a later coupled change in the same batch
    (for example, another radio field) is made again from the current values.

    Returns:
        The change, as ``{"key", "previous", "value"}``. The *previous* value is read from
        the snapshot before it is overwritten. A caller that manages the settings of a
        machine wants to know what changed, and the old value is gone when this function
        returns.
    """
    spec = get_spec(key)
    value = parse_value(spec, raw, snapshot)
    previous = spec.getter(snapshot)
    await spec.apply(device, value, snapshot)
    snapshot[key] = value
    # Keep what we set, keyed by the public key of the device. Thus a device that forgets
    # (a radio bridge with no firmware) can get its settings back at the next connect (refer
    # to meshterm.core.settings_store). Only the values that MeshTerm changed are kept.
    if ctx.settings_store is not None:
        ctx.settings_store.remember(str(snapshot.get("public_key") or ""), key, value)
    if key == "flood_scope":
        learn_default_scope(ctx, snapshot)
    ctx.ui.ack(f"[ok]✓[/ok] [brand]{key}[/brand] = {format_value(spec, value)}")
    return {"key": key, "previous": previous, "value": value}


def learn_default_scope(ctx: AppContext, snapshot: dict) -> None:
    """Keep the default flood scope that a settings read or a settings write got.

    Two parts of MeshTerm use it. The region store learns the name (source ``default``).
    It is the region that the plain floods of our node have, so it is the first name
    against which any of them is resolved. The session cache (``devstate``) also keeps
    it, because a channel with no scope of its own stores its messages as sent under the
    default. Thus that store does not have to do a round trip.

    A snapshot whose read of the default failed (firmware before 1.15) has no
    ``flood_scope`` key, and gives nothing to learn.

    Args:
        ctx: The shared application context.
        snapshot: A :func:`~meshterm.core.device_config.build_snapshot` result, or a
            snapshot that an apply has changed.
    """
    if "flood_scope" not in snapshot:
        return
    from ..core.regions import normalize

    name = normalize(str(snapshot.get("flood_scope") or ""))
    devstate = getattr(ctx, "devstate", None)
    if devstate is not None:
        devstate.note_default_scope(name)
    store = getattr(ctx, "region_store", None)
    if store is not None and name:
        store.learn(name, "default")


async def _show(ctx: AppContext, device: Device, snapshot: dict) -> Listing | None:
    """Print each current setting, and the custom variables, if there are any.

    The menu shows the table with notes. A scripted run gets one ``key value`` line for
    each setting, with the key exactly as ``config get`` and ``config set`` name it. Thus
    a line from ``show`` can be typed back in directly. The labels and descriptions in the
    table help a user who selects a setting, but this user already selected.

    The pairing PIN stays hidden here, as it is in the table. This is the dump of the full
    device: a person redirects it into a file and pastes it into a bug report. The PIN is
    the one value in it that lets the phone of another person connect to the radio.
    ``config get device_pin`` names it on purpose.
    """
    from ..ui import fields, script
    from ..ui.config_editor import PIN_KEY, conceal, config_table
    from ..ui.report import Listing
    from ..ui.surface import TuiUi

    custom = await device.get_custom_vars()
    if isinstance(ctx.ui, TuiUi):
        ctx.ui.show(config_table(snapshot, custom))
        return None

    rows: list[dict[str, Any]] = []
    for _category, specs in settings_by_category():
        for spec in specs:
            value = spec.getter(snapshot)
            printed = _script_value(spec, value)
            redacted = spec.key == PIN_KEY and printed != script.NONE
            masked = conceal(printed, absent=script.NONE) if spec.key == PIN_KEY else printed
            rows.append(
                {
                    "key": spec.key,
                    # A masked PIN is *withheld*, and that is not a value. The document says
                    # so with `redacted` and writes `null`. It does not send six bullets
                    # that a consumer must recognize as a mask.
                    "value": fields.Rendered(None if redacted else value, masked),
                    "type": spec.value_type,
                    "label": (spec.choices or {}).get(value) if spec.value_type == "enum" else None,
                    "redacted": redacted,
                }
            )
    # Custom variables are experimental firmware fields with no spec. Thus they have their
    # own namespace, and are not mixed in. A user can tell which lines `config set` accepts.
    rows.extend(
        {
            "key": f"custom.{key}",
            "value": fields.Rendered(value, value),
            "type": "str",
            "label": None,
            "redacted": False,
        }
        for key, value in sorted(custom.items())
    )
    return Listing(
        key="settings",
        columns=_setting_columns(),
        rows=rows,
        # An array for a parser, and a `sysctl -a` block for a person. A header line above
        # two columns whose keys *are* the answer adds nothing.
        headed=False,
    )


def _script_value(spec: SettingSpec, value: Any) -> str:
    """The value of a setting, in the form that ``config set`` accepts back.

    :func:`~meshterm.core.device_config.format_value` writes for a user who selects a
    setting. An enum shows as ``0 (off)``, a string with no value shows as ``(not set)``,
    and a value that the device did not report shows as ``?``. None of those three can go
    in and come back out unchanged. :func:`~meshterm.core.device_config.parse_value` must
    have a bare integer for an enum, and takes the words ``(not set)`` as the literal value
    of a string. Thus the scripted dump prints what can be typed back: the number of the
    enum, the empty string as ``""``, and :data:`~meshterm.ui.script.NONE` for a value
    that the firmware never reported. That last one is an absence, not a value, and it is
    the one line that must not go back to ``config set``.

    Args:
        spec: The setting of the value.
        value: Its current value, or ``None`` when the device did not report it.

    Returns:
        The scripted rendering.
    """
    from ..ui import script

    if value is None:
        return script.NONE
    if spec.value_type == "bool":
        return "true" if value else "false"
    if spec.value_type == "str" and value == "":
        return '""'
    return str(value)


async def _share_contact(ctx: AppContext, snapshot: dict) -> Facts:
    """State the contact card of this node, with the QR code drawn on the full frame in the menu.

    The QR code is for a phone that points at the screen, and there it fills the screen
    (:func:`~meshterm.ui.qr.share_screen`). In a redirected file, it is a block of block
    characters around the one thing that is the answer. Thus a scripted run states only
    the link. There is also no machine face for the code, and there never will be one,
    because it is a second rendering of ``url``.
    """
    from ..ui import fields
    from ..ui.config_editor import contact_share_url
    from ..ui.qr import share_screen
    from ..ui.report import BARE, Facts
    from ..ui.surface import TuiUi

    public_key = str(snapshot.get("public_key") or "")
    if not public_key:
        raise DeviceCommandError("the device did not report a public key — nothing to share")
    name = str(snapshot.get("name") or "this node")
    adv_type = int(snapshot.get("adv_type") or 1)
    url = contact_share_url(name, public_key, adv_type)
    if isinstance(ctx.ui, TuiUi):
        await share_screen(ctx, name=name, url=url)
    return Facts(
        key="share",
        fields=(
            fields.word("url", "url"),
            fields.word("name", "name"),
            fields.hexid("public_key", "public_key"),
            fields.integer("type", "type"),
        ),
        values={"url": url, "name": name, "public_key": public_key.lower(), "type": adv_type},
        shape=BARE,
        bare="url",
    )


async def _backup(device: Device, snapshot: dict, path: Path) -> tuple[Path, dict[str, int]]:
    """Write a TOML backup of the current settings.

    Returns:
        The path of the written file, and how much went into it, counted in the same way
        as the file counts. A setting that the firmware never reported is not in the
        backup, and is not in the count.
    """
    from ..core.device_config import DEVICE_SETTINGS

    custom = await device.get_custom_vars()
    channels = await _read_channels(device)
    written = backup_config(Path(path), snapshot, custom, channels)
    counts = {
        "settings": sum(1 for spec in DEVICE_SETTINGS if spec.getter(snapshot) is not None),
        "channels": len(channels),
        "custom": len(custom),
    }
    return written, counts


async def _restore(
    ctx: AppContext, device: Device, snapshot: dict, path: Path, dry_run: bool
) -> tuple[int, list[Block]]:
    """Apply (or preview) a backup file over the current settings.

    On a dry run, the plan is the answer. On a real run, it is a record. Thus the same
    listing is for the two runs. It is drawn for a person only when nothing changed,
    because a run that *did* change settings already said so on stderr, one setting at a
    time.

    Returns:
        The number of applied changes (always ``0`` for a dry run), and the blocks that
        state what was planned or done.
    """
    from ..ui import fields
    from ..ui.report import SILENT, Facts, Listing

    backup = read_backup(Path(path))
    custom = await device.get_custom_vars()
    ops = plan_restore(backup, snapshot, custom)

    def plan(applied: list[tuple], *, drawn: bool) -> list[Block]:
        listing = Listing(
            key="operations",
            columns=(
                fields.word("operation", "OPERATION"),
                fields.word("target", "TARGET"),
                fields.word("value", "VALUE"),
            ),
            rows=[
                {
                    "operation": op[0],
                    "target": str(op[1]),
                    "value": str(op[2]) if len(op) > 2 else None,
                }
                for op in applied
            ],
            plain_only=not drawn,
        )
        facts = Facts(
            key="restore",
            fields=(fields.flag("dry_run", "dry_run"), fields.integer("changes", "changes")),
            values={"dry_run": dry_run, "changes": 0 if dry_run else len(applied)},
            shape=SILENT,
        )
        return [facts, listing]

    if not ops:
        ctx.ui.ack("[muted]restore: device already matches the backup.[/muted]")
        return 0, plan([], drawn=False)

    if dry_run:
        ctx.ui.ack("[muted]dry run — nothing was changed.[/muted]")
        return 0, plan(ops, drawn=True)

    changes = 0
    for op in ops:
        if op[0] == "set":
            await _apply_setting(ctx, device, op[1], op[2], snapshot)
            changes += 1
        elif op[0] == "set_custom":
            await device.set_custom_var(op[1], op[2])
            changes += 1
        elif op[0] == "set_channel":
            await device.set_channel(op[1], op[2], op[3])
            changes += 1
    return changes, plan(ops, drawn=False)


async def _export_key(
    ctx: AppContext, device: Device, out: Path | None, artifacts: list[str]
) -> Facts:
    """Export the private key: to a file if ``out`` is given, or else as the answer itself.

    In the two cases, the plain face prints **one bare line**: the key, or the path of the
    file that holds it. The reason: ``config export-key > key.hex`` must hold the key and
    nothing else, and a caller that asked for a file wants its name. The warning that
    comes with each of them must be next to it on the screen, not in the file. Thus it
    goes to stderr with all the other acknowledgements.

    This document holds a private key when the caller asked for one. That is the purpose
    of the command, the same as on the plain face, and this function adds no more gate.
    """
    from ..ui import fields
    from ..ui.report import BARE, Facts

    key_hex = await device.export_private_key()
    written: Path | None = None
    if out is not None:
        written = Path(out)
        written.parent.mkdir(parents=True, exist_ok=True)
        written.write_text(key_hex, encoding="utf-8")
        artifacts.append(str(written))
        ctx.ui.ack("[warn]private key written — keep this file secret.[/warn]")
    else:
        ctx.ui.ack("[warn]private key (keep secret):[/warn]")
    return Facts(
        key="key",
        fields=(fields.hexid("private_key", "private_key"), fields.word("path", "path")),
        values={
            "private_key": None if written else key_hex,
            "path": str(written) if written else None,
        },
        shape=BARE,
        bare="path" if written else "private_key",
    )


async def _read_channels(device: Device) -> list[dict]:
    """Probe the channel slots, and return the configured slots."""
    channels: list[dict] = []
    for idx in range(CHANNEL_SLOT_PROBE_CAP):
        try:
            ch = await device.get_channel(idx)
        except Exception:  # noqa: BLE001 - the firmware may not support channel reads
            break
        if ch:
            channels.append(ch)
    return channels


def _require_yes(yes: bool, what: str) -> None:
    """Stop a destructive CLI command, unless the user gave ``--yes``.

    This function raises a :class:`typer.BadParameter`, and does not print and exit. A
    missing confirmation *is* a usage error. Thus it must go to stderr, in the words of the
    parser, and with the status of the parser (refer to :mod:`meshterm.core.exitcodes`).
    Before, the function printed it in red on stdout. That put a colour, and a sentence
    that is not the answer of the command, into the program that read the answer.

    Args:
        yes: Whether the user gave ``--yes``.
        what: A description of the result, for a person.

    Raises:
        typer.BadParameter: If the user did not confirm.
    """
    if not yes:
        raise typer.BadParameter(f"{what}. Re-run with --yes to confirm.")


def _setting_columns() -> tuple:
    """The columns of one setting row, shared by ``config show`` and ``config get``."""
    from ..ui import fields

    return (
        fields.word("key", "key"),
        fields.rendered("value", "value"),
        fields.hidden("type"),
        fields.hidden("label"),
        fields.hidden("redacted"),
    )


def _one_setting(spec: SettingSpec, value: Any) -> Facts:
    """One setting: the bare value on the plain face, and the full object in the document.

    ``config get device_pin`` is the one place where the caller names the PIN on purpose.
    Thus it is **not** redacted here. The caller asked for that key by name, and that is a
    different act from a dump of all the settings into a file.
    """
    from ..ui import fields
    from ..ui.report import BARE, Facts

    return Facts(
        key="setting",
        fields=_setting_columns(),
        values={
            "key": spec.key,
            "value": fields.Rendered(value, _script_value(spec, value)),
            "type": spec.value_type,
            "label": (spec.choices or {}).get(value) if spec.value_type == "enum" else None,
            "redacted": False,
        },
        shape=BARE,
        bare="value",
    )


def _applied(applied: list[dict[str, Any]]) -> Facts:
    """What a ``set`` changed: nothing on the plain face, and a record for a program that logs it.

    The exit status is the plain answer, and the acknowledgement on stderr tells the user
    that all is well. A script cannot read either of them later to find *what changed*.
    """
    from ..ui import fields
    from ..ui.report import SILENT, Column, Facts

    return Facts(
        key="applied",
        fields=(fields.integer("changes", "changes"), Column(key="applied")),
        values={"changes": len(applied), "applied": applied},
        shape=SILENT,
    )


def _channel_written(idx: int, name: str, secret: bytes | None) -> Facts:
    """What ``config channel`` wrote into a slot."""
    from ..core.channels import channel_hash, derive_secret, is_public_channel
    from ..ui import fields
    from ..ui.fields import ChannelRef
    from ..ui.report import SILENT, Facts

    key = secret or derive_secret(name)
    return Facts(
        key="channel",
        fields=(fields.integer("changes", "changes"), fields.channel("channel", lanes=())),
        values={
            "changes": 1,
            "channel": ChannelRef(
                slot=idx,
                name=name,
                public=is_public_channel(name, key),
                hash=channel_hash(key),
            ),
        },
        shape=SILENT,
    )


def _backed_up(written: Path, counts: dict[str, int]) -> Facts:
    """What ``config backup`` wrote, and how much.

    The plain face prints only the path. The caller keeps the path, and the program that
    ran the command reads it next.
    """
    from ..ui import fields
    from ..ui.report import BARE, Facts

    return Facts(
        key="backup",
        fields=(
            fields.word("path", "path"),
            fields.integer("settings", "settings"),
            fields.integer("channels", "channels"),
            fields.integer("custom", "custom"),
        ),
        values={"path": str(written), **counts},
        shape=BARE,
        bare="path",
    )


def _acted(key: str, **facts: Any) -> Facts:
    """An action, stated as data: nothing on the plain face, and a document for a record.

    Each of these actions prints nothing on the plain face now, and must continue to print
    nothing. ``config advert`` says all that it has to say in its exit status. But "prints
    nothing" is an answer on which a person can act, and a program cannot. Thus the
    machine face gets the fact.
    """
    from ..ui import fields
    from ..ui.report import SILENT, Column, Facts

    def column(name: str, value: Any) -> Column:
        if isinstance(value, bool):
            return fields.flag(name, name)
        if isinstance(value, int) or value is None:
            return fields.integer(name, name)
        return Column(key=name)

    return Facts(
        key=key,
        fields=tuple(column(name, value) for name, value in facts.items()),
        values=dict(facts),
        shape=SILENT,
    )
