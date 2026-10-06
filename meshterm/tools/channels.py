# SPDX-License-Identifier: Apache-2.0
"""The ``channels`` tool: create, join, and manage mesh channels.

In the menu, the tool opens a full-screen channel manager (refer to
:mod:`meshterm.ui.channels`). The manager is a complete feature, similar to a phone app.
With it, the user can create a private channel, add a public ``#`` channel, join a channel
with a key, import a scanned ``meshcore://`` link, and share any channel as a QR code. On
the CLI, the tool gives the ``list``, ``add``, ``join``, ``import``, ``share``, ``clear``,
and ``scope`` subcommands for use in scripts.

The ``chat`` tool also lists the channels, so that the user can select a conversation. But
this tool owns all the configuration of the slots.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core import exitcodes
from ..core.channels import (
    channel_hash,
    derive_secret,
    is_public_channel,
    normalize_secret,
    parse_share_url,
    random_secret,
    share_url,
)
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ui.report import Facts, Listing


@register
class ChannelsTool(Tool):
    """Create, join, share, and manage the device's mesh channels."""

    name = "channels"
    title = "Channels"
    icon = "📻"
    help = "Create, join, and share mesh channels"
    category = "Message"
    order = 20

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Open the channel manager (menu) or run a scripted action (CLI).

        Args:
            ctx: The shared application context.
            params: A ``cli_action`` and its arguments (CLI), or empty for the menu.

        Returns:
            A :class:`ToolResult` that holds what occurred. The menu path shows nothing.
        """
        action = params.get("cli_action")
        if action is not None:
            return await self._run_cli(ctx, action, params)

        from ..ui.channels import manage_channels

        # No message. The manager once acknowledged its work when the user left it. That
        # told the user that the changes were applied, but the user saw them occur already.
        # The count still goes to the run log through ``summary``. ``summary`` is the
        # record of what a run did, not a thing that the screen shows.
        return ToolResult(summary={"changes": await manage_channels(ctx)})

    # -- CLI --------------------------------------------------------------------

    async def _run_cli(self, ctx: AppContext, action: str, params: dict[str, Any]) -> ToolResult:
        """Run the handler for a scripted CLI action."""
        if action == "add":
            return await self._cli_add(ctx, params)
        if action == "join":
            return await self._cli_join(ctx, params)
        if action == "import":
            return await self._cli_import(ctx, params)
        if action == "share":
            return await self._cli_share(ctx, params)
        if action == "clear":
            return await self._cli_clear(ctx, params)
        if action == "scope":
            return await self._cli_scope(ctx, params)
        return await self._cli_list(ctx)

    async def _cli_list(self, ctx: AppContext) -> ToolResult:
        """List the configured channel slots."""
        from ..core.channel_probe import read_channel_slots

        device = await ctx.device()
        slots = await read_channel_slots(device)
        return ToolResult(
            summary={"channels": len(slots)},
            report=(_channel_listing(slots, ctx.region_store),),
            exit_code=exitcodes.OK if slots else exitcodes.NO_RESULT,
        )

    async def _cli_add(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Add a channel on a slot: public, private with a random key, or with a given key."""
        from ..ui.channels import write_channel

        device = await ctx.device()
        idx = int(params["index"])
        name = str(params["name"])
        if name.startswith("#"):  # public: the firmware derives the key from the name
            await write_channel(ctx, device, idx, name, None)
            secret = derive_secret(name)
        else:
            secret = normalize_secret(params["secret"]) if params.get("secret") else random_secret()
            await write_channel(ctx, device, idx, name, secret)
        await self._show_qr(ctx, name, share_url(name, secret))
        return ToolResult(
            summary={"index": idx, "name": name},
            report=(_written(idx, name, secret),),
        )

    async def _cli_join(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Join a channel with a name and a given key."""
        from ..ui.channels import write_channel

        device = await ctx.device()
        idx = int(params["index"])
        name = str(params["name"])
        secret = normalize_secret(str(params["secret"]))
        await write_channel(ctx, device, idx, name, secret)
        return ToolResult(
            summary={"index": idx, "name": name},
            report=(_written(idx, name, secret, shown=False),),
        )

    async def _cli_import(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Import a channel from a ``meshcore://channel/add`` link."""
        parsed = parse_share_url(str(params["url"]))
        if parsed is None:
            raise typer.BadParameter("not a valid meshcore:// channel link")
        name, secret = parsed
        from ..ui.channels import write_channel

        device = await ctx.device()
        idx = int(params["index"])
        await write_channel(ctx, device, idx, name, secret)
        return ToolResult(
            summary={"index": idx, "name": name},
            report=(_written(idx, name, secret, shown=False),),
        )

    async def _cli_share(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Print the share link of a channel. In the menu, also show its QR code.

        This is the only command that shows the key of a channel on purpose. Thus it is
        the only place where the secret and the share URL go out of the app. Each other
        channel document holds the identity of the channel and nothing more.
        """
        from ..core.channel_probe import read_channel_slots

        device = await ctx.device()
        idx = int(params["index"])
        slot = next((s for s in await read_channel_slots(device) if s.idx == idx), None)
        if slot is None:
            return ToolResult(
                summary={"index": idx, "shared": False},
                report=(_written(idx, None, None, shown=False),),
                exit_code=exitcodes.NO_RESULT,
            )
        await self._show_qr(ctx, slot.name, share_url(slot.name, slot.secret))
        return ToolResult(
            summary={"index": idx, "shared": True},
            report=(_written(idx, slot.name, slot.secret),),
        )

    async def _cli_clear(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Clear a channel slot. This removes the channel from the device.

        This is the script form of "Clear this slot" in the channel manager. An empty
        name makes the firmware read the slot as unused. The ``--yes`` gate is on the CLI
        command, because the key of a private channel is lost with the slot if it is not
        saved in a different place. The gate is the same as the danger confirmation of
        the interactive flow.
        """
        from ..core.channel_probe import read_channel_slots
        from ..ui.channels import write_channel

        device = await ctx.device()
        idx = int(params["index"])
        slot = next((s for s in await read_channel_slots(device) if s.idx == idx), None)
        if slot is None:
            return ToolResult(
                summary={"index": idx, "cleared": False},
                report=(_cleared(idx, False),),
                exit_code=exitcodes.NO_RESULT,
            )
        await write_channel(ctx, device, idx, "", None)  # an empty name marks the slot as unused
        return ToolResult(
            summary={"index": idx, "cleared": True},
            report=(_cleared(idx, True),),
        )

    async def _cli_scope(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Read, set, or clear the scope of a channel: the region its messages are sent in.

        This is the script form of "Send scope…" on the detail page. MeshTerm keeps the
        scope, because the firmware has no scope for each channel. When MeshTerm sends a
        message, it sets the session scope of the companion around that message. Thus,
        when this command sets a scope, it writes nothing to the device. It reads the slot
        only to find which channel is in it, because the scope is keyed by the identity of
        the channel. The scope follows the channel to any slot.

        With no region, the plain face prints only the scope, as ``config get`` prints one
        value. A channel with no scope prints ``-`` and exits 0, because "no scope, so the
        device default" is an answer about a channel that exists. It is not an empty
        result. A set or a clear prints nothing. The document always holds the channel and
        its scope (``null`` for no scope). Only an empty slot exits 5, because it has no
        channel to answer about.
        """
        from ..core.channel_probe import read_channel_slots

        device = await ctx.device()
        idx = int(params["index"])
        slot = next((s for s in await read_channel_slots(device) if s.idx == idx), None)
        region = params.get("region")
        if slot is None:
            if region is not None or params.get("clear"):
                # A write names a channel that does not exist. That is a bad argument, not an
                # empty answer. Exit 5 tells a script that the scope was set and then read
                # back as empty, which is false.
                import typer

                raise typer.BadParameter(f"slot {idx} is empty; there is no channel to scope")
            return ToolResult(
                summary={"index": idx, "scope": None},
                report=(_scoped(idx, None, None, None, changed=False),),
                exit_code=exitcodes.NO_RESULT,
            )
        store = ctx.region_store
        changed = bool(params.get("clear")) or region is not None
        if params.get("clear"):
            store.set_channel_scope(slot.identity, None)
        elif region is not None:
            store.set_channel_scope(slot.identity, region)
        scope = store.channel_scope(slot.identity)
        return ToolResult(
            summary={"index": idx, "scope": scope},
            report=(_scoped(idx, slot.name, slot.secret, scope, changed=changed),),
        )

    @staticmethod
    async def _show_qr(ctx: AppContext, name: str, url: str) -> None:
        """Draw the share link of a channel as a QR code, only in the menu.

        The QR code is for a phone that points at the screen. There, the code fills the
        whole screen (:func:`~meshterm.ui.qr.share_screen`). In a file (through a pipe),
        the code is only a mass of block characters around the link, and the link is the
        real answer. Thus a scripted run prints the link and nothing else.
        """
        from ..ui.qr import share_screen
        from ..ui.surface import TuiUi

        if isinstance(ctx.ui, TuiUi):
            await share_screen(ctx, name=name, url=url)

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``channels`` subcommand group.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        channels_app = typer.Typer(help=self.help, no_args_is_help=True, rich_markup_mode=None)

        @channels_app.command("list", help="List the configured channel slots")
        def _list_cmd() -> None:
            run_tool_command(self, {"cli_action": "list"})

        @channels_app.command("add", help="Add a channel (# name = public; else private)")
        def _add_cmd(
            index: int = typer.Argument(..., help="Channel slot index"),
            name: str = typer.Argument(..., help="Channel name (leading # = public)"),
            secret: str | None = typer.Option(
                None, "--secret", help="32-hex-char key (private only; random if omitted)"
            ),
        ) -> None:
            run_tool_command(
                self, {"cli_action": "add", "index": index, "name": name, "secret": secret}
            )

        @channels_app.command("join", help="Join a channel with its name and key")
        def _join_cmd(
            index: int = typer.Argument(..., help="Channel slot index"),
            name: str = typer.Argument(..., help="Channel name"),
            secret: str = typer.Argument(..., help="32-hex-char (16-byte) key"),
        ) -> None:
            run_tool_command(
                self, {"cli_action": "join", "index": index, "name": name, "secret": secret}
            )

        @channels_app.command("import", help="Import a meshcore:// channel link")
        def _import_cmd(
            index: int = typer.Argument(..., help="Channel slot index"),
            url: str = typer.Argument(..., help="A meshcore://channel/add link"),
        ) -> None:
            run_tool_command(self, {"cli_action": "import", "index": index, "url": url})

        @channels_app.command("share", help="Print a channel's share link and QR code")
        def _share_cmd(
            index: int = typer.Argument(..., help="Channel slot index"),
        ) -> None:
            run_tool_command(self, {"cli_action": "share", "index": index})

        @channels_app.command("clear", help="Clear a channel slot (removes it from the device)")
        def _clear_cmd(
            index: int = typer.Argument(..., help="Channel slot index"),
            yes: bool = typer.Option(
                False, "--yes", help="Confirm removing the channel from this slot"
            ),
        ) -> None:
            if not yes:
                # A usage error, in the words of the parser and with its exit status. It is
                # not a red line printed into the program that reads the output of this
                # command.
                raise typer.BadParameter(
                    "clearing a slot removes the channel (a private channel's key is lost "
                    "unless you have it saved). Re-run with --yes to confirm."
                )
            run_tool_command(self, {"cli_action": "clear", "index": index})

        @channels_app.command("scope", help="Show or set the region a channel sends into")
        def _scope_cmd(
            index: int = typer.Argument(..., help="Channel slot index"),
            region: str | None = typer.Argument(
                None, help="Region to send this channel's messages into (omit to show it)"
            ),
            clear: bool = typer.Option(
                False, "--clear", help="Drop the channel's scope (send under the default)"
            ),
        ) -> None:
            from ..core.regions import RegionNameError, validate

            if clear and region is not None:
                raise typer.BadParameter("Pass a region or --clear, not both.")
            if region is not None:
                try:
                    region = validate(region)
                except RegionNameError as exc:
                    raise typer.BadParameter(str(exc)) from exc
            run_tool_command(
                self, {"cli_action": "scope", "index": index, "region": region, "clear": clear}
            )

        app.add_typer(channels_app, name=self.name)


def _channel_listing(slots: list, store: object | None = None) -> Listing:
    """The configured slots, one record for each slot.

    This is the only listing whose record is a shared shape itself, instead of a record
    that holds one. Each column of this listing is already the channel. If each row put
    its only field under a ``channel`` key, a query must be ``jq '.[].channel.name'``
    for no reason.

    ``SCOPE`` is the region that the messages of the channel are sent in (``-`` and
    ``null`` for the device default). The value comes from the region store, which the
    send path also reads.
    """
    from ..ui import fields
    from ..ui.report import Listing

    def scope_of(slot) -> str | None:  # noqa: ANN001 - a ChannelSlot
        return store.channel_scope(slot.identity) if store is not None else None

    return Listing(
        key="channels",
        columns=(
            fields.integer("slot", "SLOT"),
            fields.name("name", "NAME"),
            fields.word("type", "TYPE"),
            fields.hexid("hash", "HASH"),
            fields.name("scope", "SCOPE"),
        ),
        rows=[
            {
                "slot": slot.idx,
                "name": slot.name,
                "type": "public" if slot.is_public else "private",
                "hash": slot.hash,
                "scope": scope_of(slot),
            }
            for slot in slots
        ],
    )


def _scoped(
    idx: int, name: str | None, secret: bytes | None, scope: str | None, *, changed: bool
) -> Facts:
    """What ``channels scope`` read or set: the channel and the region that it sends into.

    Args:
        idx: The slot index.
        name: The channel name, or ``None`` if the slot is empty.
        secret: The 16-byte key, or ``None`` for an empty slot.
        scope: The current scope of the channel, or ``None`` for the device default.
        changed: Whether this run set or cleared the scope. If it did, the plain face
            prints nothing, as for each write. A read prints only the scope.

    Returns:
        The facts block.
    """
    from ..ui import fields, script
    from ..ui.fields import ChannelRef, Rendered
    from ..ui.report import BARE, SILENT, Facts

    channel = None
    if name is not None and secret is not None:
        public = is_public_channel(name, secret)
        channel = ChannelRef(slot=idx, name=name, public=public, hash=channel_hash(secret))
    # A channel with no scope prints `-` on the plain face (and `null` in the document).
    # "The device default" is an answer about a channel that exists. Usually the bare form
    # prints nothing for an absent value, and then only the exit status can give this
    # answer. An empty slot has no channel to answer about, so it keeps that silence (and
    # its exit 5).
    value = Rendered(scope, scope or script.NONE) if channel is not None else None
    return Facts(
        key="channel",
        fields=(fields.channel("channel"), fields.rendered("scope", "scope")),
        values={"channel": channel, "scope": value},
        shape=SILENT if changed else BARE,
        bare="scope",
    )


def _written(idx: int, name: str | None, secret: bytes | None, *, shown: bool = True) -> Facts:
    """What a slot holds now, after ``add``, ``join``, ``import``, or ``share``.

    All four commands write or read the same thing, and all four know the key that they
    wrote. Thus all four give the same shape. The plain face prints only the share URL
    when the caller asked for something to give to other persons (``add``, ``share``). It
    prints nothing when the caller did not ask for it (``join``, ``import``). The caller
    gave the key to these two commands, and to read the key back to the caller is not an
    answer.

    Args:
        idx: The slot index.
        name: The channel name, or ``None`` if the slot is empty.
        secret: The 16-byte key, or ``None`` for an empty slot.
        shown: Whether the plain face prints the URL.

    Returns:
        The facts block.
    """
    from ..ui import fields
    from ..ui.fields import ChannelRef
    from ..ui.report import BARE, SILENT, Facts

    channel = None
    if name is not None and secret is not None:
        public = is_public_channel(name, secret)
        channel = ChannelRef(slot=idx, name=name, public=public, hash=channel_hash(secret))
    return Facts(
        key="channel",
        fields=(
            fields.channel("channel"),
            fields.hexid("secret", "secret"),
            fields.word("url", "url"),
        ),
        values={
            "channel": channel,
            "secret": secret.hex() if secret else None,
            "url": share_url(name, secret) if name is not None and secret is not None else None,
        },
        shape=BARE if shown else SILENT,
        bare="url",
    )


def _cleared(idx: int, cleared: bool) -> Facts:
    """What ``channels clear`` did. The plain face prints nothing, because the status said it."""
    from ..ui import fields
    from ..ui.report import SILENT, Facts

    return Facts(
        key="cleared",
        fields=(fields.integer("slot", "slot"), fields.flag("cleared", "cleared")),
        values={"slot": idx, "cleared": cleared},
        shape=SILENT,
    )
