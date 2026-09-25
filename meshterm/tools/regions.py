# SPDX-License-Identifier: Apache-2.0
"""The ``regions`` command: ask a repeater which regions it relays floods for.

``meshterm regions <repeater>`` sends the anonymous regions request (firmware 1.12+, no
login) and prints the answer — the regions it relays scoped floods for, and whether it also
relays unscoped ones. One request, one transmission, per invocation: the repeater
rate-limits the question, and nothing here retries it.

It is a command-line face only. In the menu the same question lives on a repeater's node
page (*Ask which regions it carries*), beside the answer it last gave, so there is no menu
row for it here.

The answer is learned into the region store either way, exactly as the node page learns it,
so a scoped flood heard afterwards can be traced back to a region this repeater named.

Exit status, per the CLI's rules: ``0`` when it answered with anything (unscoped floods
alone are an answer), ``5`` when it answered and named nothing at all — it relays no floods
— ``2`` for a contact that does not exist or is known not to be a repeater (nothing was
sent), and ``4`` when it never answered, which is what a repeater out of direct reach does:
the request is only answered when it arrives from a neighbour or over a known route.
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
            ctx: Shared application context.
            params: ``node``, the repeater's contact name.

        Returns:
            The answer as a report; ``NO_RESULT`` when it named nothing.

        Raises:
            typer.BadParameter: For an unknown contact, or one known not to be a repeater —
                both decided before anything is sent.
            DeviceCommandError: When the repeater never answered.
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
    """The repeater's answer, stated once for both faces.

    Three facts: who answered, whether it relays unscoped floods, and the regions it relays
    scoped floods for — the wildcard kept out of that list, since it names no region (see
    :func:`~meshterm.ui.fields.regions`).
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
