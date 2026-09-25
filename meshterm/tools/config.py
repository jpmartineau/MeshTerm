# SPDX-License-Identifier: Apache-2.0
"""The ``config`` tool: every device setting, and the operations on the box itself.

Interactively it opens the Device config page (see :mod:`meshterm.ui.config_editor`) —
settings staged and applied in place, with the clock, backup/restore, the identity key,
reboot and factory reset as actions below them. On the CLI it exposes generic key/value
subcommands plus backup/restore, channels, custom vars, and a ``--yes``-gated set of
destructive operations. Everything funnels through :func:`apply_ops`: the page's Apply and
its actions, :meth:`ConfigTool.run` for the scripted commands, and the standalone ``advert``
tool's sends all run through the one executor.
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
    """View and edit the connected device's full configuration."""

    name = "config"
    title = "Device config"
    icon = "🔧"
    help = "Settings and actions — radio, backup, reboot, …"
    category = "This node"
    order = 20

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Run the Device config page; there are never parameters to collect.

        The page applies what is staged and runs its actions itself, logging a ``runs`` row
        for each Apply (the same pattern as ``repeater-admin``), so by the time the reader
        backs out there is nothing left for :meth:`run` to do.

        Args:
            ctx: Shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.config_editor import edit_config

        await edit_config(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Execute a list of configuration operations against the device.

        Args:
            ctx: Shared application context.
            params: ``ops`` — a list of operation tuples (see module docstring).

        Returns:
            A :class:`ToolResult` summarizing how many values changed.
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
            """Show the configuration when no subcommand is given."""
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


# -- op execution (shared by the tool and the interactive editor) -------------


async def apply_ops(
    ctx: AppContext, device: Device, snapshot: dict, ops: list[tuple]
) -> tuple[int, list[str], Report]:
    """Execute a list of configuration operation tuples against ``device``.

    This is the single executor for every config operation, used by :meth:`ConfigTool.run`
    (the scripted commands) and by the Device config page — its Apply, one staged value at a
    time, and its actions (reboot, backup/restore, key management, factory reset), which run
    immediately rather than staging.

    Args:
        ctx: Shared application context (for console output).
        device: The connected device to act on.
        snapshot: The current device snapshot; updated in place as settings change so
            coupled commands rebuild from current values.
        ops: Operation tuples (see the module docstring).

    Returns:
        A ``(changes, artifacts, report)`` triple: the number of value changes applied,
        any file paths produced, and the blocks stating what happened (empty for the
        menu, which shows its own acknowledgements).
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
            # The bare value, nothing else: the caller named the key, so repeating it back
            # is one more thing for `$(meshterm config get name)` to strip off. The same
            # shape `sysctl -n` and `git config --get` print. The document keeps the key,
            # the type and an enum's label, none of which the bare form has room for.
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
            # A flood advert, however it was asked for, restarts this device's week: the
            # weekly one only ever goes out a full week after the last (see
            # AdvertScheduler). A zero-hop reaches only the neighbours, and resets nothing.
            public_key = str(snapshot.get("public_key") or "")
            if flood and public_key:
                ctx.advert_store.mark_flood(public_key)
            kind_label = "flood" if flood else "zero-hop"
            ctx.ui.ack(f"[ok]✓[/ok] {kind_label} advertisement sent")
            report.append(_acted("advert", sent=True, flood=flood))
        elif kind == "share":
            report.append(await _share_contact(ctx, snapshot))
        elif kind == "sync_clock":
            # The same step the on-connect service runs in the background
            # (:mod:`meshterm.services.clock_sync`); here it is acknowledged and reported.
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
                    # What the clock was off by *before* the set: the fact worth logging,
                    # and the one this command destroys by succeeding.
                    drift_s=done.drift_s,
                )
            )
        elif kind == "reboot":
            await device.reboot()
            ctx.ui.ack("[warn]device rebooting[/warn]")
            report.append(_acted("reboot", rebooted=True))
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
        else:  # pragma: no cover - guarded by the call sites that build ops
            raise DeviceCommandError(f"unknown config operation: {kind}")
    if applied:
        report.append(_applied(applied))
    # Drop any session-cached facts these ops may have changed, so the next screen re-reads
    # the truth rather than a stale copy (see meshterm.services.device_state.DeviceState). A
    # reboot/reconnect resets the whole cache on its own; these cover the in-place edits.
    kinds = {op[0] for op in ops}
    _WHOLESALE = {"restore", "import_key", "factory_reset"}
    if kinds & _WHOLESALE:
        ctx.devstate.reset()
    else:
        if "set" in kinds:
            ctx.devstate.invalidate_config()  # self-info fields + path-hash mode
        if "set_channel" in kinds:
            ctx.devstate.invalidate_channels()
    return changes, artifacts, tuple(report)


async def _apply_setting(
    ctx: AppContext, device: Device, key: str, raw: Any, snapshot: dict
) -> dict[str, Any]:
    """Parse, apply and record one setting; keep ``snapshot`` consistent.

    The local snapshot is updated with the new value so a later coupled change in the
    same batch (e.g. another radio field) is rebuilt from current values.

    Returns:
        The change, as ``{"key", "previous", "value"}``. The *previous* value is read off
        the snapshot before it is overwritten: a caller managing a machine's configuration
        wants to know what moved, and it is gone the moment this returns.
    """
    spec = get_spec(key)
    value = parse_value(spec, raw, snapshot)
    previous = spec.getter(snapshot)
    await spec.apply(device, value, snapshot)
    snapshot[key] = value
    # Remember what we set, keyed by the device's own public key, so a forgetful device (a
    # firmware-less radio bridge) can be offered its settings back on the next connect (see
    # meshterm.core.settings_store). Provenance-gated: only values changed through MeshTerm.
    if ctx.settings_store is not None:
        ctx.settings_store.remember(str(snapshot.get("public_key") or ""), key, value)
    if key == "flood_scope":
        learn_default_scope(ctx, snapshot)
    ctx.ui.ack(f"[ok]✓[/ok] [brand]{key}[/brand] = {format_value(spec, value)}")
    return {"key": key, "previous": previous, "value": value}


def learn_default_scope(ctx: AppContext, snapshot: dict) -> None:
    """Take in the default flood scope a config read or write has just seen.

    Two things want it. The region store learns the name (source ``default``): it is the
    region this station's own plain floods carry, so it is the first name any of them will
    be resolved against. And the session cache holds it (``devstate``), since a channel with
    no scope of its own records its messages as sent under the default, and this saves that
    record a round trip.

    A snapshot whose read of the default failed (firmware before 1.15) carries no
    ``flood_scope`` key at all, and teaches nothing.

    Args:
        ctx: Shared application context.
        snapshot: A :func:`~meshterm.core.device_config.build_snapshot` result, or one an
            apply has just updated.
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
    """Print every current setting, plus any custom variables.

    The menu shows the annotated table; a scripted run gets one ``key value`` line per
    setting, keyed exactly as ``config get`` and ``config set`` name them, so a line read
    out of ``show`` can be typed straight back in. The labels and descriptions the table
    carries are for a reader choosing a setting, and this reader has already chosen.

    The pairing PIN stays concealed here, as it is in the table: this is the whole-device
    dump, the thing that gets redirected into a file and pasted into a bug report, and the
    PIN is the one value on it that lets someone else's phone onto the radio.
    ``config get device_pin`` names it deliberately.
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
                    # A masked PIN is *withheld*, which is not a value: the document says
                    # so with `redacted` and writes `null`, rather than shipping six
                    # bullets a consumer would have to recognise as a mask.
                    "value": fields.Rendered(None if redacted else value, masked),
                    "type": spec.value_type,
                    "label": (spec.choices or {}).get(value) if spec.value_type == "enum" else None,
                    "redacted": redacted,
                }
            )
    # Custom variables are experimental firmware fields with no spec, so they are namespaced
    # rather than mixed in — a reader can tell which lines `config set` will take.
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
        # An array to a parser and a `sysctl -a` block to a reader: a header line over two
        # columns whose keys *are* the answer would be furniture.
        headed=False,
    )


def _script_value(spec: SettingSpec, value: Any) -> str:
    """A setting's value in the form ``config set`` accepts back.

    :func:`~meshterm.core.device_config.format_value` writes for a reader choosing a
    setting: an enum reads ``0 (off)``, an unset string reads ``(not set)``, an unreported
    one reads ``?``. None of those three survive a round trip —
    :func:`~meshterm.core.device_config.parse_value` wants a bare integer for an enum, and
    would take the words ``(not set)`` as the literal value of a string. So the scripted
    dump prints what can be typed back: the enum's number, the empty string as ``""``, and
    :data:`~meshterm.ui.script.NONE` for a value the firmware never reported (which is an
    absence, not a value, and is the one line ``config set`` should not be handed back).

    Args:
        spec: The setting the value belongs to.
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
    """State this node's contact card — with the QR code drawn in the menu, full-frame.

    The QR is for a phone pointed at the screen, and there it takes the whole screen
    (:func:`~meshterm.ui.qr.share_screen`). Redirected into a file it is a block of
    block characters around the one thing that is the answer, so a scripted run states the
    link by itself. There is no machine face for the code either, and never will be: it is
    a second rendering of ``url``.
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
    """Write a TOML backup of the current configuration.

    Returns:
        The path written and how much went into it, counted the way the file counts —
        a setting the firmware never reported is not in the backup and is not in the tally.
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
    """Apply (or preview) a backup file against the current configuration.

    The plan is the answer on a dry run and a record on a real one, so the same listing
    serves both — drawn for a reader only when nothing was changed, since a run that *did*
    change things already said so, setting by setting, on stderr.

    Returns:
        The number of changes applied (always ``0`` for a dry run), and the blocks stating
        what was planned or done.
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
    """Export the private key, to a file if ``out`` is given, else as the answer itself.

    Either way the plain face prints **one bare line** — the key, or the path it was
    written to — because ``config export-key > key.hex`` is expected to hold the key and
    nothing else, and a caller that asked for a file wants the name of it. The warning
    that comes with either belongs beside it on screen, not in the file, so it goes to
    stderr with every other acknowledgement.

    This document carries a private key when one was asked for. That is what the command
    is, exactly as on the plain face, and no extra gate is introduced here.
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
    """Probe channel slots and return the configured ones."""
    channels: list[dict] = []
    for idx in range(CHANNEL_SLOT_PROBE_CAP):
        try:
            ch = await device.get_channel(idx)
        except Exception:  # noqa: BLE001 - firmware may not support channel reads
            break
        if ch:
            channels.append(ch)
    return channels


def _require_yes(yes: bool, what: str) -> None:
    """Abort a destructive CLI command unless ``--yes`` was passed.

    Raised as a :class:`typer.BadParameter` rather than printed and exited: a missing
    confirmation *is* a usage error, so it belongs on stderr in the parser's own words and
    under the parser's own status (see :mod:`meshterm.core.exitcodes`). Printing it in red
    on stdout put a colour, and a sentence that is not the command's answer, into whatever
    was reading the command's answer.

    Args:
        yes: Whether the user passed ``--yes``.
        what: Human-readable description of the consequence.

    Raises:
        typer.BadParameter: If confirmation was not given.
    """
    if not yes:
        raise typer.BadParameter(f"{what}. Re-run with --yes to confirm.")


def _setting_columns() -> tuple:
    """One setting row's columns, shared by ``config show`` and ``config get``."""
    from ..ui import fields

    return (
        fields.word("key", "key"),
        fields.rendered("value", "value"),
        fields.hidden("type"),
        fields.hidden("label"),
        fields.hidden("redacted"),
    )


def _one_setting(spec: SettingSpec, value: Any) -> Facts:
    """One setting: the bare value plain, the whole object in the document.

    ``config get device_pin`` is the one place the PIN is named deliberately, so it is
    **not** redacted here — the caller asked for that key by name, which is a different
    act from dumping every setting into a file.
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
    """What a ``set`` changed — nothing plain, a record for whoever is logging it.

    The exit status is the plain answer and the acknowledgement on stderr is the reassurance;
    neither is something a script can read back later to find out *what moved*.
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
    """What ``config backup`` wrote, and how much of it.

    The plain face prints the path bare — it is what the caller keeps, and what the thing
    that ran the command reads next.
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
    """An action, stated as data: nothing plain, a document for whoever wanted a record.

    Every one of these prints nothing on the plain face today and should keep doing so —
    ``config advert`` says all it has to say in its exit status. But "prints nothing" is an
    answer a person can act on and a program cannot, so the machine face gets the fact.
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
