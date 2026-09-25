# SPDX-License-Identifier: Apache-2.0
"""The ``channels`` tool: create, join, and manage mesh channels.

Interactively it opens a full-screen channel manager (see :mod:`meshterm.ui.channels`) — a
first-class, phone-app-style experience for creating private channels, adding public ``#``
channels, joining with a key, importing a scanned ``meshcore://`` link, and sharing any
channel as a QR code. On the CLI it exposes ``list``, ``add``, ``join``, ``import``,
``share``, ``clear`` and ``scope`` subcommands for scripted use.

Channels are also *listed* by the ``chat`` tool for picking a conversation; this tool owns
everything to do with configuring the slots themselves.
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
            ctx: Shared application context.
            params: A ``cli_action`` with its arguments (CLI), or empty for the menu.

        Returns:
            A :class:`ToolResult` recording what happened (the menu path shows nothing).
        """
        action = params.get("cli_action")
        if action is not None:
            return await self._run_cli(ctx, action, params)

        from ..ui.channels import manage_channels

        # No message: the manager acknowledged its work on the way out, which meant telling
        # the reader that changes they had just watched land were applied. The count still
        # goes to the run log through ``summary``, which is the record of what an invocation
        # did rather than anything shown.
        return ToolResult(summary={"changes": await manage_channels(ctx)})

    # -- CLI --------------------------------------------------------------------

    async def _run_cli(self, ctx: AppContext, action: str, params: dict[str, Any]) -> ToolResult:
        """Dispatch a scripted CLI action."""
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
        """Add a public, private-random, or explicitly-keyed channel on a slot."""
        from ..ui.channels import write_channel

        device = await ctx.device()
        idx = int(params["index"])
        name = str(params["name"])
        if name.startswith("#"):  # public: firmware derives the key from the name
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
        """Join a channel from a name and an explicit key."""
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
        """Print a channel's share link, with its QR code in the menu.

        The one command that names a channel's key deliberately, so it is the one place
        the secret and the share URL leave the app at all — every other channel document
        carries the identity and stops there.
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
        """Clear a channel slot, removing the channel from the device.

        The scriptable face of the channel manager's *Clear this slot*: an empty name
        makes the firmware read the slot as unused. The ``--yes`` gate lives on the CLI
        command (a private channel's key is lost with the slot unless it's saved
        elsewhere), mirroring the interactive flow's danger confirmation.
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
        await write_channel(ctx, device, idx, "", None)  # empty name => the slot reads as unused
        return ToolResult(
            summary={"index": idx, "cleared": True},
            report=(_cleared(idx, True),),
        )

    async def _cli_scope(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Read, set or clear the region a channel's messages are sent under.

        The scriptable face of the detail page's *Send scope…*. The scope is MeshTerm's to
        keep (the firmware has none per channel; the send sets the companion's session
        scope around each message), so setting one writes nothing to the radio — the slot
        is read only to learn which channel is in it, since the scope is keyed by the
        channel's identity and follows it to any slot.

        With no region, the plain face prints the scope alone, the way ``config get`` prints
        one value — and a channel with none prints ``-`` and exits 0: "no scope, so the
        device default" is an answer about a channel that is there, not an empty result. A
        set or a clear prints nothing, and the document always carries the channel and its
        scope (``null`` for none). Only an empty slot, with no channel to answer about,
        exits 5.
        """
        from ..core.channel_probe import read_channel_slots

        device = await ctx.device()
        idx = int(params["index"])
        slot = next((s for s in await read_channel_slots(device) if s.idx == idx), None)
        region = params.get("region")
        if slot is None:
            if region is not None or params.get("clear"):
                # A write names a channel that isn't there: a bad argument, not an empty
                # answer — exit 5 would tell a script the scope was set and read back empty.
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
        """Draw a channel's share link as a QR code — in the menu, and only there.

        The QR is for a phone pointed at the screen, and there it takes the whole screen
        (:func:`~meshterm.ui.qr.share_screen`). Piped into a file it is a block of block
        characters wrapped around the one thing that is actually the answer, so a scripted
        run states the link and nothing else.
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
                # A usage error, in the parser's own words and under its own status — not a
                # red line printed onto whatever is reading this command's output.
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
    """The configured slots, one record each.

    The one listing whose record *is* a shared shape rather than carrying one: a row that
    nested its only field under a ``channel`` key would make ``jq '.[].channel.name'``
    out of a listing whose every column is already the channel.

    ``SCOPE`` is the region the channel's messages are sent under (``-`` and ``null`` for
    the device default), read from the region store the send path reads.
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
    """What ``channels scope`` read or set: the channel and the region it sends into.

    Args:
        idx: The slot index.
        name: The channel name, or ``None`` for a slot that turned out to be empty.
        secret: The 16-byte key, or ``None`` for an empty slot.
        scope: The channel's scope now, or ``None`` for the device default.
        changed: Whether this run set or cleared it — then the plain face is silent, the
            way every write's is; a read prints the scope alone.

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
    # A channel with no scope answers `-` plain (and `null` in the document): "the device
    # default" is an answer about a channel that is there, where the bare form's usual
    # silence for an absent value would leave only the exit status to say it. An empty
    # slot has no channel to answer about, so it keeps that silence (and its exit 5).
    value = Rendered(scope, scope or script.NONE) if channel is not None else None
    return Facts(
        key="channel",
        fields=(fields.channel("channel"), fields.rendered("scope", "scope")),
        values={"channel": channel, "scope": value},
        shape=SILENT if changed else BARE,
        bare="scope",
    )


def _written(idx: int, name: str | None, secret: bytes | None, *, shown: bool = True) -> Facts:
    """What a slot now holds, after ``add``, ``join``, ``import`` or ``share``.

    All four write or read the same thing and all four know the key they wrote, so all
    four state the same shape. The plain face prints the share URL alone where the caller
    asked to be given something to pass on (``add``, ``share``) and nothing at all where
    it did not (``join``, ``import``): those two were handed the key, and reading it back
    to them is not an answer.

    Args:
        idx: The slot index.
        name: The channel name, or ``None`` for a slot that turned out to be empty.
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
    """What ``channels clear`` did — nothing plain, since the status already said it."""
    from ..ui import fields
    from ..ui.report import SILENT, Facts

    return Facts(
        key="cleared",
        fields=(fields.integer("slot", "slot"), fields.flag("cleared", "cleared")),
        values={"slot": idx, "cleared": cleared},
        shape=SILENT,
    )
