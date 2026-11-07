# SPDX-License-Identifier: Apache-2.0
"""The ``outbound`` tool: the outbound paths of the sites of this computer and of fixed devices.

An outbound path is a path out of our node into the mesh, one or more hops long, that the
user trusts for the first hops of a route (refer to :mod:`meshterm.core.outbound_store`).
The route suggestions of the trace features start on one of them.

In the menu, the tool opens the Outbound paths page (:mod:`meshterm.ui.outbound_screen`),
in the Explore section, after Trace target and Trace path, which start their routes on
these paths. The paths belong to the computer, and the page works also with no device
connected.

On the CLI:

* ``outbound`` (or ``outbound show``): the paths that apply to the connected device now,
  and where they come from. The exit status is ``5`` when no list applies.
* ``outbound sites``: each site, whether it is active, and its paths.
* ``outbound use SITE`` and ``outbound use --none``: make a site active, or no site.
* ``outbound set SITE PATH…``: make a site, or replace its paths. A path is a spec, the
  same as ``--path`` of a trace (``3d63,f2c2``). When no site is active, the new site
  becomes active, the same as in the menu.
* ``outbound remove SITE --yes``: delete a site.
* ``outbound fix [PATH…]``: make the connected device a fixed device. With no path, it
  starts with the paths of the active site.
* ``outbound unfix --yes``: the connected device follows the active site again.

The commands that delete data need ``--yes``, as each such command in MeshTerm does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core import exitcodes
from ..core.outbound_store import OutboundPath, clean_path
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ui.report import Block


@register
class OutboundTool(Tool):
    """Show and change the outbound paths of the sites and of the fixed devices."""

    name = "outbound"
    title = "Outbound paths"
    icon = "🧭"
    help = "The ways out of your node, for each site and fixed device"
    category = "Explore"
    order = 34  # after the two trace tools, which start their routes on these paths

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Open the Outbound paths page. The page writes its own changes.

        Args:
            ctx: The shared application context.

        Returns:
            ``None``: nothing is left for :meth:`run` to do.
        """
        from ..ui.outbound_screen import manage_outbound

        await manage_outbound(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Do one CLI operation on the store of outbound paths.

        Args:
            ctx: The shared application context.
            params: ``op``: one of ``show``, ``sites``, ``use``, ``set``, ``remove``,
                ``fix``, and ``unfix``, with ``site`` and ``paths`` where the operation
                takes them.

        Returns:
            A :class:`ToolResult` with the report of the operation.

        Raises:
            typer.BadParameter: If a site name or a path is not valid.
        """
        op = params.get("op", "show")
        store = ctx.outbound_store
        if op == "show":
            return await _show(ctx)
        if op == "sites":
            sites = store.sites()
            active = store.active_site()
            return ToolResult(
                summary={"sites": len(sites)},
                report=(_sites_listing(sites, active.name if active else None),),
                exit_code=exitcodes.OK if sites else exitcodes.NO_RESULT,
            )
        if op == "use":
            name = params.get("site")
            if name is not None and store.site(name) is None:
                raise typer.BadParameter(
                    f"no site is named {name!r}; see `meshterm outbound sites`"
                )
            store.set_active(name)
            active = store.active_site()
            shown = active.name if active else None
            return _ack(
                {"active": shown},
                f"[ok]✓[/ok] active site: [brand]{shown}[/brand]"
                if shown
                else "[ok]✓[/ok] no site is active",
            )
        if op == "set":
            name = str(params.get("site") or "").strip()
            paths = _parse_paths(params.get("paths") or [])
            try:
                site = store.put_site(name, paths)
            except ValueError as exc:
                raise typer.BadParameter(str(exc)) from exc
            if store.active_site() is None:
                store.set_active(site.name)
            active = store.active_site()
            return _ack(
                {
                    "site": site.name,
                    "active": active is not None and active.name == site.name,
                    "paths": [",".join(path) for path in site.paths],
                },
                f"[ok]✓[/ok] site [brand]{site.name}[/brand]: {len(site.paths)} outbound path(s)",
            )
        if op == "remove":
            name = str(params.get("site") or "")
            if not store.remove_site(name):
                raise typer.BadParameter(
                    f"no site is named {name!r}; see `meshterm outbound sites`"
                )
            return _ack({"removed": name}, f"[ok]✓[/ok] deleted the site [brand]{name}[/brand]")
        if op == "fix":
            # A fixed device is the connected companion, so this needs the device: a read
            # that fails here is the "no device" exit, and not an empty identity.
            info = await ctx.devstate.self_info()
            key = str(info.get("public_key") or "").lower()
            name = str(info.get("name") or "")
            given = params.get("paths")
            if given:
                paths = _parse_paths(given)
            else:
                active = store.active_site()
                paths = list(active.paths) if active else []
            device = store.fix_device(key, name, paths)
            return _ack(
                {"device": name or None, "paths": [",".join(path) for path in device.paths]},
                f"[ok]✓[/ok] fixed [brand]{name or key[:12]}[/brand]: "
                f"{len(device.paths)} outbound path(s)",
            )
        if op == "unfix":
            info = await ctx.devstate.self_info()
            key = str(info.get("public_key") or "").lower()
            done = store.unfix_device(key)
            return ToolResult(
                summary={"unfixed": done},
                message="[ok]✓[/ok] the device follows the active site again" if done else None,
                report=(_facts({"unfixed": done}),),
                exit_code=exitcodes.OK if done else exitcodes.NO_RESULT,
            )
        raise typer.BadParameter(f"unknown operation {op!r}")  # pragma: no cover - the CLI sets it

    # -- CLI --------------------------------------------------------------------

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``outbound`` subcommand group.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        group = typer.Typer(help=self.help, no_args_is_help=False, rich_markup_mode=None)

        @group.callback(invoke_without_command=True)
        def _root(ctx: typer.Context) -> None:
            """Show the outbound paths that apply now when no subcommand is given."""
            if ctx.invoked_subcommand is None:
                run_tool_command(self, {"op": "show"})

        @group.command("show", help="Show the outbound paths that apply to the device now")
        def _show_cmd() -> None:
            run_tool_command(self, {"op": "show"})

        @group.command("sites", help="List the sites and their outbound paths")
        def _sites_cmd() -> None:
            run_tool_command(self, {"op": "sites"})

        @group.command("use", help="Make a site active (or no site, with --none)")
        def _use_cmd(
            site: str | None = typer.Argument(None, help="The site to make active"),
            none: bool = typer.Option(False, "--none", help="Make no site active"),
        ) -> None:
            if (site is None) == (not none):
                raise typer.BadParameter("give a site, or --none")
            run_tool_command(self, {"op": "use", "site": site})

        @group.command("set", help="Make a site, or replace its outbound paths")
        def _set_cmd(
            site: str = typer.Argument(..., help="The name of the site"),
            paths: list[str] = typer.Argument(
                None, help="Outbound paths in order, each a hop spec like 3d63,f2c2"
            ),
        ) -> None:
            run_tool_command(self, {"op": "set", "site": site, "paths": paths or []})

        @group.command("remove", help="Delete a site and its outbound paths")
        def _remove_cmd(
            site: str = typer.Argument(..., help="The site to delete"),
            yes: bool = typer.Option(False, "--yes", help="Confirm this destructive action"),
        ) -> None:
            if not yes:
                raise typer.BadParameter(
                    "this deletes the site and its outbound paths; re-run with --yes to confirm"
                )
            run_tool_command(self, {"op": "remove", "site": site})

        @group.command("fix", help="Give the connected device its own outbound paths")
        def _fix_cmd(
            paths: list[str] = typer.Argument(
                None, help="Outbound paths in order (default: those of the active site)"
            ),
        ) -> None:
            run_tool_command(self, {"op": "fix", "paths": paths or []})

        @group.command("unfix", help="Let the connected device follow the active site again")
        def _unfix_cmd(
            yes: bool = typer.Option(False, "--yes", help="Confirm this destructive action"),
        ) -> None:
            if not yes:
                raise typer.BadParameter(
                    "this forgets the device's own outbound paths; re-run with --yes to confirm"
                )
            run_tool_command(self, {"op": "unfix"})

        app.add_typer(group, name=self.name)


async def _show(ctx: AppContext) -> ToolResult:
    """The paths that apply to the connected device now, with the source of the list."""
    from ..services.outbound import current_choice, session_contacts

    choice = await current_choice(ctx)
    contacts = await session_contacts(ctx)
    blocks: list[Block] = [
        _facts({"source": choice.source or None, "name": choice.name or None}, shape="pairs"),
        _paths_listing(choice.paths, contacts),
    ]
    return ToolResult(
        summary={"source": choice.source, "paths": len(choice.paths)},
        report=tuple(blocks),
        exit_code=exitcodes.OK if choice.paths else exitcodes.NO_RESULT,
    )


def _parse_paths(specs: list[str]) -> list[OutboundPath]:
    """The paths that the user typed, each checked. A bad one is a usage error."""
    paths: list[OutboundPath] = []
    for spec in specs:
        path = clean_path(spec)
        if path is None:
            raise typer.BadParameter(
                f"{spec!r} is not an outbound path: give hops in hex, separated by commas "
                "(3d63,f2c2), each node only one time"
            )
        paths.append(path)
    return paths


def _ack(values: dict[str, Any], message: str) -> ToolResult:
    """A change that worked: its facts for the machine face, and a line on stderr."""
    return ToolResult(summary=dict(values), message=message, report=(_facts(values),))


def _facts(values: dict[str, Any], *, shape: str = "silent") -> Block:
    """One set of facts. Each value is a word, a flag, or a list of specs."""
    from ..ui import fields
    from ..ui.report import Column, Facts, Lane

    columns = []
    for key, value in values.items():
        if isinstance(value, bool):
            columns.append(fields.flag(key, key.upper()))
        elif isinstance(value, list):
            columns.append(
                Column(
                    key=key,
                    lanes=(Lane(header=key.upper(), render=lambda v: " ".join(v) or "-"),),
                    json=list,
                )
            )
        else:
            columns.append(fields.word(key, key.upper()))
    return Facts(key="outbound", fields=tuple(columns), values=values, shape=shape)


def _paths_listing(paths: tuple[OutboundPath, ...], contacts: list) -> Block:
    """The outbound paths in order: the rank, the hops with their names, and the spec."""
    from ..ui import fields
    from ..ui.report import Listing

    def ref(hop: str) -> fields.NodeRef:
        for contact in contacts:
            key = (contact.public_key or contact.key_prefix or "").lower()
            if key and key.startswith(hop):
                return fields.NodeRef(
                    name=contact.name or None, key=contact.public_key or None, hash=hop
                )
        return fields.NodeRef(hash=hop)

    return Listing(
        key="paths",
        columns=(
            fields.integer("rank", "RANK"),
            fields.spec("path", "PATH"),
            fields.route("hops", "HOPS"),
        ),
        rows=[
            {"rank": i + 1, "path": ",".join(path), "hops": [ref(hop) for hop in path]}
            for i, path in enumerate(paths)
        ],
    )


def _sites_listing(sites: list, active: str | None) -> Block:
    """Each site: its name, whether it is active, and its paths as specs."""
    from ..ui import fields
    from ..ui.report import Column, Lane, Listing

    return Listing(
        key="sites",
        columns=(
            fields.name("site", "SITE"),
            fields.flag("active", "ACTIVE"),
            Column(
                key="paths",
                lanes=(Lane(header="PATHS", render=lambda v: " ".join(v) or "-"),),
                json=list,
            ),
        ),
        rows=[
            {
                "site": site.name,
                "active": site.name == active,
                "paths": [",".join(path) for path in site.paths],
            }
            for site in sites
        ],
    )
