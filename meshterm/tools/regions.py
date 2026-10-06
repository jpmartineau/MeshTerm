# SPDX-License-Identifier: Apache-2.0
"""The ``regions`` command: ask a repeater for which regions it relays floods.

``meshterm regions <repeater>`` sends the anonymous regions request (firmware 1.12 or
later, no login) and prints the answer. The answer gives the regions for which the
repeater relays scoped floods, and it tells if the repeater also relays unscoped floods.
Each run sends one request, which is one transmission. The repeater limits the rate of
this question, and nothing here tries it again.

This tool has only a command-line face. In the menu, the same question is on the node page
of a repeater ("Ask which regions it carries"), next to the last answer of that repeater.
Thus the menu has no row for this tool.

In both cases, MeshTerm adds the answer to the region store, exactly as the node page does.
Thus MeshTerm can trace a scoped flood that it hears later back to a region that this
repeater named.

The exit status, by the rules of the CLI:

- ``0`` if the repeater answered with any value (only unscoped floods is also an answer).
- ``5`` if it answered and named nothing (it relays no floods).
- ``2`` for a contact that does not exist, or that is known not to be a repeater (nothing
  was sent).
- ``4`` if it never answered. A repeater that is out of direct reach does this, because it
  answers the request only when the request comes from a neighbour or over a known route.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core import exitcodes
from ..core.models import NODE_TYPE_LABELS, NODE_TYPE_REPEATER, Contact
from ..core.regions import WILDCARD
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ui.report import Facts


@register
class RegionsTool(Tool):
    """Ask one repeater which regions it carries (command line only)."""

    name = "regions"
    title = "Regions"
    icon = "🔖"
    help = "Ask a repeater which regions it relays floods for"
    category = "Other nodes"
    order = 20
    menu_visible = False  # the menu asks from the repeater's node page

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Send the regions request to one repeater and report its answer.

        Args:
            ctx: The shared application context.
            params: ``node``: the contact name of the repeater.

        Returns:
            The answer as a report. ``NO_RESULT`` if the repeater named nothing.

        Raises:
            typer.BadParameter: For an unknown contact, or for a contact that is known not
                to be a repeater. The method finds both before it sends anything.
            DeviceCommandError: If the repeater never answered.
        """
        device = await ctx.device()
        contacts = await device.get_contacts()
        name = str(params["node"])
        node = next((c for c in contacts if c.name == name), None)
        if node is None:
            raise typer.BadParameter(f"unknown contact: {name!r}")
        if node.node_type is not None and node.node_type != NODE_TYPE_REPEATER:
            role = NODE_TYPE_LABELS.get(node.node_type, "not a repeater")
            raise typer.BadParameter(
                f"{name!r} is a {role}; only a repeater answers the regions request"
            )
        names = await device.request_regions(node)
        node_id = (node.public_key or node.key_prefix or "").lower()[:12]
        if node_id and ctx.region_store is not None:
            ctx.region_store.learn_carried(node_id, names)
        regions = [n for n in names if n != WILDCARD]
        unscoped = WILDCARD in names
        return ToolResult(
            report=(_answer(node, unscoped, regions),),
            summary={"node": name, "unscoped": unscoped, "regions": regions},
            exit_code=exitcodes.OK if names else exitcodes.NO_RESULT,
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``regions`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _regions(
            node: str = typer.Argument(..., help="The repeater's contact name"),
        ) -> None:
            run_tool_command(self, {"node": node})


def _answer(node: Contact, unscoped: bool, regions: list[str]) -> Facts:
    """The answer of the repeater, given one time for both faces.

    Three facts: which node answered, if it relays unscoped floods, and the regions for
    which it relays scoped floods. That list does not include the wildcard, because the
    wildcard names no region (refer to :func:`~meshterm.ui.fields.regions`).
    """
    from ..ui import fields
    from ..ui.fields import NodeRef
    from ..ui.report import Facts

    return Facts(
        key="regions",
        fields=(
            fields.node("node", lanes=(("name", "node"),)),
            fields.flag("unscoped", "unscoped"),
            fields.regions("regions", "regions"),
        ),
        values={
            "node": NodeRef(
                name=node.name,
                key=(node.public_key or "").lower() or None,
                hash=(node.key_prefix or "").lower() or None,
                type=NODE_TYPE_LABELS.get(node.node_type),
            ),
            "unscoped": unscoped,
            "regions": regions,
        },
    )
