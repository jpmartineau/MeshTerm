# SPDX-License-Identifier: Apache-2.0
"""The ``route`` tool: the device routes that the companion holds to its contacts.

The companion holds one *device route* for each contact: the relays from our node to the
contact (refer to :mod:`meshterm.services.device_routes`). No route means that the radio
floods the next message. An empty route means no relay: the contact is a neighbour. The
radio replaces the route each time that it learns a new one, and it never forgets a route
that stopped to work. This tool lets the user see, change, and keep these routes.

This tool has only a command-line face. In the menu, the same actions are on the node page
of a contact. Thus the menu has no row for this tool.

On the CLI:

* ``route`` (or ``route list``): each contact that has a device route or a kept route.
  The exit status is ``5`` when there is none.
* ``route show CONTACT``: the route of one contact, how the radio got it, and where the
  user keeps it.
* ``route set CONTACT --path SPEC [--keep]``: write a route to the radio. ``--path ""``
  means no relay. ``--keep`` also keeps the route.
* ``route forget CONTACT --yes``: clear the route, so that the next message floods. A kept
  route stays kept.
* ``route keep CONTACT [--path SPEC]``: keep a route. With no ``--path``, MeshTerm keeps the
  route that the radio holds now.
* ``route unkeep CONTACT``: stop keeping the route. The exit status is ``5`` when the
  route was not kept.
* ``route discover CONTACT [--use]``: send one path discovery (one transmission) and show
  the route in each direction. ``--use`` writes the outward route to the radio. The exit
  status is ``5`` when no answer comes.
* ``route sync``: make the routes of the radio follow the active site and the kept routes,
  as MeshTerm does at each connection.

A contact is given by its name or by the first digits of its key. A ``SPEC`` is a list of
hops in hex, separated by commas (``a1,3d``), the same as ``--path`` of a trace. A bad
spec is a usage error (``2``). ``keep`` fails (``1``) when no site is active and the
device is not fixed, because no record can hold the route.

Each action that changes a route prints the route as it is afterwards, in the shape of
``show``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core import exitcodes
from ..core.models import NODE_TYPE_LABELS, Contact
from ..core.outbound_store import NoRouteOwnerError, RouteOwner, RoutePath, clean_route
from ..services.device_routes import RouteStatus, report_notice, silent_before_now
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from datetime import datetime

    from ..services.topology import MeshTopology
    from ..ui.fields import NodeRef
    from ..ui.report import Block, Column

#: The plain word for a route with no relay: the contact is a neighbour.
DIRECT = "direct"

#: The origin of a route that is not there, in both faces.
FLOOD = "flood"


@register
class RouteTool(Tool):
    """Show, change, and keep the device routes of the contacts (command line only)."""

    name = "route"
    title = "Device routes"
    icon = "🧭"
    help = "The routes that the radio holds to your contacts"
    category = "Explore"
    order = 36
    menu_visible = False  # the menu works on a route from the node page of the contact

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Do one CLI operation on the device routes.

        Args:
            ctx: The shared application context.
            params: ``op``: one of ``list``, ``show``, ``set``, ``forget``, ``keep``,
                ``unkeep``, ``discover``, and ``sync``. The operations on one contact also
                take ``contact``. ``set`` takes ``path`` and ``keep``. ``keep`` takes an
                optional ``path``. ``discover`` takes ``use``.

        Returns:
            A :class:`ToolResult` with the report of the operation.

        Raises:
            typer.BadParameter: If the contact is not known, or if a spec is not valid.
            DeviceConfigError: If ``keep`` has no site or fixed device to keep the route.
        """
        op = params.get("op", "list")
        if op == "sync":
            return await _sync(ctx)
        scene = await _Scene.load(ctx)
        if op == "list":
            return _list(ctx, scene)
        contact = _resolve(scene.contacts, str(params["contact"]))
        if op == "show":
            status = scene.status(ctx, contact)
            return ToolResult(
                summary={"contact": contact.name, "origin": status.origin or FLOOD},
                report=(_show_facts(scene, contact, status),),
            )
        if op == "set":
            return await _set(ctx, scene, contact, str(params["path"]), bool(params.get("keep")))
        if op == "forget":
            return await _forget(ctx, scene, contact)
        if op == "keep":
            return await _keep_op(ctx, scene, contact, params.get("path"))
        if op == "unkeep":
            return await _unkeep(ctx, scene, contact)
        if op == "discover":
            return await _discover(ctx, scene, contact, bool(params.get("use")))
        raise typer.BadParameter(f"unknown operation {op!r}")  # pragma: no cover - the CLI sets it

    # -- CLI --------------------------------------------------------------------

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``route`` subcommand group.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        group = typer.Typer(help=self.help, no_args_is_help=False, rich_markup_mode=None)

        @group.callback(invoke_without_command=True)
        def _root(ctx: typer.Context) -> None:
            """List the device routes when no subcommand is given."""
            if ctx.invoked_subcommand is None:
                run_tool_command(self, {"op": "list"})

        @group.command("list", help="List the contacts that have a device route or a kept route")
        def _list_cmd() -> None:
            run_tool_command(self, {"op": "list"})

        @group.command("show", help="Show the route to one contact and where it is kept")
        def _show_cmd(
            contact: str = typer.Argument(..., help="The contact name, or the start of its key"),
        ) -> None:
            run_tool_command(self, {"op": "show", "contact": contact})

        @group.command("set", help="Write a device route to the radio")
        def _set_cmd(
            contact: str = typer.Argument(..., help="The contact name, or the start of its key"),
            path: str = typer.Option(
                ..., "--path", help='The relays in hex, like a1,3d ("" for no relay)'
            ),
            keep: bool = typer.Option(False, "--keep", help="Also keep the route"),
        ) -> None:
            run_tool_command(self, {"op": "set", "contact": contact, "path": path, "keep": keep})

        @group.command("forget", help="Clear the route, so that the next message floods")
        def _forget_cmd(
            contact: str = typer.Argument(..., help="The contact name, or the start of its key"),
            yes: bool = typer.Option(False, "--yes", help="Confirm this destructive action"),
        ) -> None:
            if not yes:
                raise typer.BadParameter(
                    "this clears the route that the radio holds, and the next message "
                    "floods. Run it again with --yes to confirm"
                )
            run_tool_command(self, {"op": "forget", "contact": contact})

        @group.command("keep", help="Keep a route, so that MeshTerm writes it back")
        def _keep_cmd(
            contact: str = typer.Argument(..., help="The contact name, or the start of its key"),
            path: str | None = typer.Option(
                None, "--path", help="The relays in hex (default: the route the radio holds)"
            ),
        ) -> None:
            run_tool_command(self, {"op": "keep", "contact": contact, "path": path})

        @group.command("unkeep", help="Stop keeping the route to a contact")
        def _unkeep_cmd(
            contact: str = typer.Argument(..., help="The contact name, or the start of its key"),
        ) -> None:
            run_tool_command(self, {"op": "unkeep", "contact": contact})

        @group.command("discover", help="Send one path discovery and show the route each way")
        def _discover_cmd(
            contact: str = typer.Argument(..., help="The contact name, or the start of its key"),
            use: bool = typer.Option(False, "--use", help="Write the outward route to the radio"),
        ) -> None:
            run_tool_command(self, {"op": "discover", "contact": contact, "use": use})

        @group.command("sync", help="Make the routes follow the active site and the kept routes")
        def _sync_cmd() -> None:
            run_tool_command(self, {"op": "sync"})

        app.add_typer(group, name=self.name)


# -- what a run knows --------------------------------------------------------------------


def _key(contact: Contact) -> str:
    """The full public key of a contact in lowercase hex, or ``""``."""
    return (contact.public_key or "").lower().removeprefix("0x")


@dataclass(slots=True)
class _Scene:
    """What one run needs to describe a route: the contacts, our node, and the evidence.

    Attributes:
        contacts: The contacts of the companion.
        device_key: The public key of the companion, or ``""``.
        device_name: The name of the companion, or ``""``.
        size: The bytes of each hash in the radio, or ``None`` when the radio did not say.
        topo: The evidence graph, to find a first hop that is silent.
        silent_before: The time before which a first hop counts as silent.
    """

    contacts: list[Contact]
    device_key: str
    device_name: str
    size: int | None
    topo: MeshTopology
    silent_before: datetime | None

    @classmethod
    async def load(cls, ctx: AppContext) -> _Scene:
        """Read the contacts and the identity of the companion, and build the evidence graph."""
        from ..services.outbound import device_identity, session_topology

        identity = await device_identity(ctx)
        contacts = list(await ctx.devstate.contacts())
        try:
            mode = await ctx.devstate.path_hash_mode()
        except Exception:  # noqa: BLE001 - an optional read. Without it, a hash has one byte.
            mode = None
        size = (mode + 1) if isinstance(mode, int) and 0 <= mode <= 3 else None
        return cls(
            contacts=contacts,
            device_key=identity.key,
            device_name=identity.name,
            size=size,
            topo=session_topology(ctx, identity.key, contacts),
            silent_before=silent_before_now(ctx),
        )

    @property
    def hash_digits(self) -> int:
        """The hex digits of a hash in a drawn route."""
        return (self.size or 1) * 2

    def status(self, ctx: AppContext, contact: Contact) -> RouteStatus:
        """The route status of one contact, with the first hop test."""
        return ctx.routes.status(
            contact,
            device_key=self.device_key,
            topo=self.topo,
            silent_before=self.silent_before,
        )

    def ref(self, contact: Contact) -> NodeRef:
        """The identity of a contact, with its hash at the width of the radio."""
        from ..ui.fields import NodeRef

        key = _key(contact)
        return NodeRef(
            name=contact.name or None,
            key=key or None,
            hash=(key or contact.key_prefix.lower())[: self.hash_digits] or None,
            type=NODE_TYPE_LABELS.get(contact.node_type),
        )

    def self_ref(self) -> NodeRef:
        """The identity of our node."""
        from ..ui.fields import NodeRef

        return NodeRef(
            name=self.device_name or None,
            key=self.device_key or None,
            hash=self.device_key[: self.hash_digits] or None,
            is_self=True,
        )

    def hop(self, hop: str) -> NodeRef:
        """The identity of one relay. The name is known only when one contact matches."""
        from ..ui.fields import NodeRef

        found = [
            c for c in self.contacts if (_key(c) or c.key_prefix.lower()).startswith(hop.lower())
        ]
        if len(found) != 1:
            return NodeRef(hash=hop)
        return NodeRef(
            name=found[0].name or None,
            key=_key(found[0]) or None,
            hash=hop,
            type=NODE_TYPE_LABELS.get(found[0].node_type),
        )

    def route(
        self, contact: Contact, hops: tuple[str, ...] | None, *, outward: bool = True
    ) -> list[NodeRef] | None:
        """The route drawn from end to end, or ``None`` for no route.

        Args:
            contact: The contact at the far end.
            hops: The relays, in the order of travel.
            outward: ``True`` for a route from our node to the contact, ``False`` for the
                route that comes back.
        """
        if hops is None:
            return None
        relays = [self.hop(hop) for hop in hops]
        ends = (self.self_ref(), self.ref(contact))
        return [ends[0], *relays, ends[1]] if outward else [ends[1], *relays, ends[0]]

    def fit(self, contact: Contact, hops: RoutePath) -> RoutePath:
        """Check a route against the hash width of the radio, before anything is written.

        Raises:
            typer.BadParameter: If a hop is shorter than the hash width, or if the radio
                cannot hold so many hops. Nothing is transmitted in this case, thus it is a
                usage error and not a device failure.
        """
        if self.size is None:
            return hops
        from ..core.connection import DeviceCommandError, route_at_width

        try:
            return route_at_width(hops, self.size, name=contact.name)
        except DeviceCommandError as exc:
            raise typer.BadParameter(str(exc)) from exc


def _resolve(contacts: list[Contact], needle: str) -> Contact:
    """The contact that a name or a key prefix gives (the resolver of ``chat``)."""
    from .chat import _resolve_contact

    return _resolve_contact(contacts, needle)


def _spec_of(hops: tuple[str, ...] | None) -> str | None:
    """A route as a spec: ``None`` for no route, ``""`` for no relay."""
    return None if hops is None else ",".join(hops)


def _parse_route(text: str) -> RoutePath:
    """The route that the user typed. A bad one is a usage error."""
    route = clean_route(text)
    if route is None:
        raise typer.BadParameter(
            f"{text!r} is not a route: give hops in hex, separated by commas (a1,3d), "
            'each node only one time, or "" for no relay'
        )
    return route


def _say(text: str) -> str:
    """A name or a spec for a message, safe in Rich markup."""
    from rich.markup import escape

    return escape(text)


# -- the operations ----------------------------------------------------------------------


def _list(ctx: AppContext, scene: _Scene) -> ToolResult:
    """Each contact that has a device route or a kept route."""
    rows = []
    for contact in sorted(scene.contacts, key=lambda c: (c.name.casefold(), _key(c))):
        status = scene.status(ctx, contact)
        if status.hops is None and status.kept is None:
            continue
        rows.append(_row(scene, contact, status))
    return ToolResult(
        summary={"routes": len(rows)},
        report=(_listing(rows),),
        exit_code=exitcodes.OK if rows else exitcodes.NO_RESULT,
    )


async def _set(
    ctx: AppContext, scene: _Scene, contact: Contact, text: str, keep: bool
) -> ToolResult:
    """Write a route to the radio, and keep it when the user asks."""
    route = scene.fit(contact, _parse_route(text))
    if keep:
        _require_key(contact)
        await _keep_route(ctx, contact, route)
        written = route
    else:
        written = await ctx.routes.set(contact, route)
    now = replace(contact, route_hops=written)
    status = scene.status(ctx, now)
    spec = ",".join(written) or DIRECT
    note = " and kept" if keep else ""
    return ToolResult(
        summary={"contact": contact.name, "path": ",".join(written), "kept": keep},
        message=f"[ok]✓[/ok] route to [brand]{_say(contact.name)}[/brand] set to {spec}{note}",
        report=(_show_facts(scene, now, status),),
    )


async def _forget(ctx: AppContext, scene: _Scene, contact: Contact) -> ToolResult:
    """Clear the route, so that the next message to the contact floods."""
    await ctx.routes.forget(contact)
    now = replace(contact, route_hops=None)
    status = scene.status(ctx, now)
    message = f"[ok]✓[/ok] the next message to [brand]{_say(contact.name)}[/brand] floods"
    if status.kept is not None:
        message += ". The kept route stays kept, so run [bold]route unkeep[/bold] to drop it"
    return ToolResult(
        summary={"contact": contact.name},
        message=message,
        report=(_show_facts(scene, now, status),),
    )


async def _keep_op(ctx: AppContext, scene: _Scene, contact: Contact, text: object) -> ToolResult:
    """Keep the given route, or the route that the radio holds now."""
    _require_key(contact)
    if text is None:
        held = scene.status(ctx, contact).hops
        if held is None:
            raise typer.BadParameter(
                f"the radio has no route to {contact.name!r} to keep: give one with --path"
            )
        route: RoutePath = tuple(held)
    else:
        route = scene.fit(contact, _parse_route(str(text)))
    owner = await _keep_route(ctx, contact, route)
    now = replace(contact, route_hops=route)
    status = scene.status(ctx, now)
    where = f"{owner.source} [brand]{_say(owner.name)}[/brand]"
    return ToolResult(
        summary={"contact": contact.name, "path": ",".join(route), "owner": owner.name},
        message=f"[ok]✓[/ok] keeping the route to [brand]{_say(contact.name)}[/brand] for {where}",
        report=(_show_facts(scene, now, status),),
    )


async def _unkeep(ctx: AppContext, scene: _Scene, contact: Contact) -> ToolResult:
    """Stop keeping the route to a contact. The radio keeps the route that it holds."""
    done = await ctx.routes.unkeep(contact)
    status = scene.status(ctx, contact)
    return ToolResult(
        summary={"contact": contact.name, "unkept": done},
        message=(
            f"[ok]✓[/ok] no longer keeping the route to [brand]{_say(contact.name)}[/brand]"
            if done
            else f"no route to {_say(contact.name)} was kept"
        ),
        report=(_show_facts(scene, contact, status),),
        exit_code=exitcodes.OK if done else exitcodes.NO_RESULT,
    )


async def _discover(ctx: AppContext, scene: _Scene, contact: Contact, use: bool) -> ToolResult:
    """Send one path discovery, and write the outward route when ``use`` asks."""
    found = await ctx.routes.discover(contact)
    if found is None:
        return ToolResult(
            summary={"contact": contact.name, "answered": False},
            message=f"no answer from {_say(contact.name)}",
            report=(_discovery_facts(scene, contact, None, used=False),),
            exit_code=exitcodes.NO_RESULT,
        )
    used = False
    if use:
        await ctx.routes.set(contact, tuple(found.out_hops))
        used = True
    return ToolResult(
        summary={
            "contact": contact.name,
            "answered": True,
            "out": ",".join(found.out_hops),
            "in": ",".join(found.in_hops),
            "used": used,
        },
        message=(
            f"[ok]✓[/ok] route to [brand]{_say(contact.name)}[/brand] set to the outward route"
            if used
            else None
        ),
        report=(_discovery_facts(scene, contact, found, used=used),),
    )


async def _sync(ctx: AppContext) -> ToolResult:
    """Make the routes of the radio follow the active site and the kept routes."""
    report = await ctx.routes.reconcile()
    notice = report_notice(report)
    return ToolResult(
        summary={
            "restored": report.restored,
            "kept_written": report.kept_written,
            "saved": sum(report.saved.values()),
        },
        message=f"[ok]✓[/ok] {_say(notice)}" if notice else "[ok]✓[/ok] the routes are in line",
        report=(_sync_facts(report),),
    )


def _require_key(contact: Contact) -> None:
    """A kept route belongs to the full key of the contact, so the contact must have one."""
    if not _key(contact):
        raise typer.BadParameter(
            f"{contact.name!r} has no full key yet, so MeshTerm cannot keep a route to it"
        )


async def _keep_route(ctx: AppContext, contact: Contact, route: RoutePath) -> RouteOwner:
    """Keep a route and write it. Without a site or a fixed device, this fails (exit 1)."""
    from ..core.device_config import DeviceConfigError

    try:
        return await ctx.routes.keep(contact, route)
    except NoRouteOwnerError as exc:
        raise DeviceConfigError(
            "keep needs a site: make one active with `meshterm outbound use <site>`, "
            "or make this device a fixed device with `meshterm outbound fix`"
        ) from exc


# -- the report shapes -------------------------------------------------------------------


def _spec_cell(spec: str | None) -> str:
    """The plain cell of a spec: the spec itself, :data:`DIRECT` for no relay, or absent."""
    from ..ui import script

    if spec is None:
        return script.NONE
    return spec or DIRECT


def _spec(key: str, header: str) -> Column:
    """A route as a spec. ``""`` is a real answer (no relay) and ``None`` is no route.

    The document keeps both exactly, so a program can give a spec back to ``--path``. The
    plain face writes the word :data:`DIRECT` for the empty spec, because an empty cell
    reads as a missing value.
    """
    from ..ui.report import Column, Lane

    return Column(key=key, lanes=(Lane(header=header, render=_spec_cell),))


def _owner(key: str, header: str) -> Column:
    """The record that keeps a route: ``site Home`` or ``device Roof``."""
    from ..ui import script
    from ..ui.report import Column, Lane

    def cell(owner: RouteOwner | None) -> str:
        return script.NONE if owner is None else f"{owner.source} {script.name(owner.name)}"

    return Column(
        key=key,
        lanes=(Lane(header=header, render=cell),),
        json=lambda o: None if o is None else {"source": o.source, "name": o.name},
    )


def _names(key: str, header: str) -> Column:
    """A list of contact names: joined with commas, ``none`` when empty, an array in JSON."""
    from ..ui import script
    from ..ui.report import Column, Lane

    return Column(
        key=key,
        lanes=(
            Lane(header=header, render=lambda v: ", ".join(script.name(n) for n in v) or "none"),
        ),
        json=list,
    )


def _saved(key: str, header: str) -> Column:
    """The routes that a sync saved for other sites: ``Foulab 2``, or an array of objects."""
    from ..ui import script
    from ..ui.report import Column, Lane

    return Column(
        key=key,
        lanes=(
            Lane(
                header=header,
                render=lambda v: ", ".join(f"{script.name(s)} {n}" for s, n in v.items()) or "none",
            ),
        ),
        json=lambda v: [{"site": site, "routes": count} for site, count in v.items()],
    )


def _row(scene: _Scene, contact: Contact, status: RouteStatus) -> dict[str, Any]:
    """The typed values of one contact for the listing."""
    return {
        "node": scene.ref(contact),
        "path": _spec_of(status.hops),
        "origin": status.origin or FLOOD,
        "kept": _spec_of(status.kept),
        "route": scene.route(contact, status.hops),
    }


def _listing(rows: list[dict[str, Any]]) -> Block:
    """The contacts that have a route: the node, the spec, the origin, the kept spec, the route."""
    from ..ui import fields
    from ..ui.report import Listing

    return Listing(
        key="routes",
        columns=(
            fields.node("node", lanes=(("name", "NAME"), ("type", "TYPE"), ("hash", "HASH"))),
            _spec("path", "PATH"),
            fields.word("origin", "ORIGIN"),
            _spec("kept", "KEPT"),
            fields.route("route", "ROUTE"),
        ),
        rows=rows,
    )


def _show_facts(scene: _Scene, contact: Contact, status: RouteStatus) -> Block:
    """All that MeshTerm knows about the route to one contact, as one set of facts."""
    from ..ui import fields
    from ..ui.report import Facts

    kept = status.kept is not None
    values = {
        **_row(scene, contact, status),
        "kept_by": status.owner if kept else None,
        "kept_in_radio": status.kept_in_radio if kept else None,
        "skipped": status.skipped_since is not None if kept else None,
        "first_hop_heard_at": status.skipped_since,
    }
    return Facts(
        key="route",
        fields=(
            fields.node(
                "node",
                lanes=(("name", "node"), ("type", "type"), ("hash", "hash"), ("key", "key")),
            ),
            _spec("path", "path"),
            fields.word("origin", "origin"),
            _spec("kept", "kept"),
            _owner("kept_by", "kept_by"),
            fields.flag("kept_in_radio", "kept_in_radio"),
            fields.flag("skipped", "skipped"),
            fields.when("first_hop_heard_at", "first_hop_heard"),
            fields.route("route", "route"),
        ),
        values=values,
    )


def _discovery_facts(scene: _Scene, contact: Contact, found: Any, *, used: bool) -> Block:
    """The answer of a path discovery: the route each way, and whether MeshTerm used it."""
    from ..ui import fields
    from ..ui.report import Facts

    out = None if found is None else tuple(found.out_hops)
    back = None if found is None else tuple(found.in_hops)
    return Facts(
        key="discovery",
        fields=(
            fields.node("node", lanes=(("name", "node"), ("hash", "hash"))),
            fields.flag("answered", "answered"),
            _spec("out_path", "out_path"),
            _spec("in_path", "in_path"),
            fields.flag("used", "used"),
            fields.route("out_route", "out_route"),
            fields.route("in_route", "in_route"),
        ),
        values={
            "node": scene.ref(contact),
            "answered": found is not None,
            "out_path": _spec_of(out),
            "in_path": _spec_of(back),
            "used": used,
            "out_route": scene.route(contact, out),
            "in_route": scene.route(contact, back, outward=False),
        },
    )


def _sync_facts(report: Any) -> Block:
    """What a sync did, in counts and in names."""
    from ..ui import fields
    from ..ui.report import Facts

    return Facts(
        key="sync",
        fields=(
            fields.word("site", "site"),
            _saved("saved", "saved"),
            fields.integer("restored", "restored"),
            fields.integer("kept_written", "kept_written"),
            _names("kept_skipped", "kept_skipped"),
            _names("failed", "failed"),
        ),
        values={
            "site": report.site or None,
            "saved": dict(report.saved),
            "restored": report.restored,
            "kept_written": report.kept_written,
            "kept_skipped": list(report.kept_skipped),
            "failed": list(report.failed),
        },
    )
