# SPDX-License-Identifier: Apache-2.0
"""The ``repeater-admin`` tool: set up remote repeaters and room servers over the mesh.

In the menu, the tool opens the repeater-admin flow (refer to
:mod:`meshterm.ui.repeater_admin`):

1. The user selects a node. The nodes with stored credentials are first in the list.
2. The user logs in with a remembered password, or with a password that a prompt asks
   for.
3. The user goes to an editor similar to Device config, which uses the text CLI of the
   node. The editor includes the settings that only a repeater has, which the local
   editor never had (TX delay, Direct TX delay, airtime factor, advert intervals). It also
   has one-shot actions (advert, clock sync, password, reboot) and a remote command line
   similar to readline.

On the CLI, the tool stays a one-shot command for scripts. ``repeater-admin <node>
<command…>`` logs in (with the remembered password, or with ``--password``) and prints the
reply of the node. Each run does one transmission, which is the rule of the trace tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core import exitcodes
from ..core.connection import DeviceCommandError
from ..core.models import NODE_TYPE_LABELS, Contact, LoginResult
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ui.report import Facts


@register
class RepeaterAdminTool(Tool):
    """Configure a remote repeater or room server: settings, actions, and its raw CLI."""

    name = "repeater-admin"
    title = "Repeater admin"
    icon = "🗼"
    help = "Run a remote repeater — settings and actions"
    category = "Other nodes"
    order = 10  # the remote equivalent of the local config tools

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Run the interactive flow. It has no parameters to collect.

        The flow shows all its content itself (the same pattern as ``config``). Thus the
        return value ``None`` tells the menu that the run is complete.

        Args:
            ctx: The shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.repeater_admin import open_repeater_admin

        await open_repeater_admin(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Send one CLI command to a remote node and print its reply (scripted path).

        Args:
            ctx: The shared application context.
            params: ``node`` (the contact name), ``command`` (the CLI text), and an
                optional ``password`` that replaces the remembered password.

        Returns:
            A :class:`ToolResult` with the reply of the node (or with no reply).
        """
        device = await ctx.device()
        contacts = await device.get_contacts()
        name = str(params["node"])
        node = next((c for c in contacts if c.name == name), None)
        # In both cases, the argument is wrong, and nothing went out on the air. Thus they
        # are usage errors (2), not device failures (4). A device failure tells a caller
        # that a retry can help. `tx-optimize` already gave the same answer for the same
        # condition.
        if node is None:
            raise typer.BadParameter(f"unknown contact: {name!r}")

        password = params.get("password") or ctx.admin_store.get(node)
        if not password:
            raise typer.BadParameter(
                f"no admin password for {name!r}; pass --password or run the "
                "interactive flow once to store it."
            )
        outcome = await device.admin_login(node, str(password))
        ctx.admin_store.record(node, str(password), outcome)
        if outcome is LoginResult.REFUSED:
            raise DeviceCommandError(
                f"admin login to {name!r} failed (wrong password?). The saved password was cleared."
            )
        if not outcome:
            raise DeviceCommandError(
                f"{name!r} did not answer the admin login — it may be out of reach, "
                "asleep, or busy. Any saved password was kept; try again when it answers."
            )

        command = str(params["command"])
        ctx.remote_store.append_history(node, command)
        reply = await device.send_remote_command(node, command, timeout=10.0)
        return ToolResult(
            report=(_answered(node, command, reply),),
            summary={"node": name, "command": command, "replied": reply is not None},
            # No reply is not a failure, because the command possibly arrived. But there is
            # nothing to report, and a script that waits for output must know which result
            # it got.
            exit_code=exitcodes.OK if reply is not None else exitcodes.NO_RESULT,
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``repeater-admin`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _repeater_admin(
            node: str = typer.Argument(..., help="The remote contact's name"),
            command: list[str] = typer.Argument(..., help="The CLI command to send"),
            password: str | None = typer.Option(
                None, "--password", help="Admin password (else remembered)"
            ),
        ) -> None:
            tool_params: dict[str, Any] = {"node": node, "command": " ".join(command)}
            if password is not None:
                tool_params["password"] = password
            run_tool_command(self, tool_params)


def _answered(node: Contact, command: str, reply: str | None) -> Facts:
    """The reply of a remote node.

    ``reply`` is the text of the node, complete and exact. The plain face prints it bare,
    as text and never as markup. Thus a square bracket in a reply stays a square bracket.
    The document holds it raw, with more than one line if the node sent more than one
    line.

    On purpose, it is **not parsed**. MeshTerm does not know the CLI grammar of the remote
    node. If the document parses it, the document invents a structure, and a consumer
    then depends on that structure.
    """
    from ..ui import fields
    from ..ui.fields import NodeRef
    from ..ui.report import BARE, Facts

    return Facts(
        key="remote",
        fields=(
            fields.node("node", lanes=()),
            fields.hidden("command"),
            fields.word("reply", "reply"),
        ),
        values={
            "node": NodeRef(
                name=node.name,
                key=(node.public_key or "").lower() or None,
                hash=(node.key_prefix or "").lower() or None,
                type=NODE_TYPE_LABELS.get(node.node_type),
            ),
            "command": command,
            "reply": reply,
        },
        shape=BARE,
        bare="reply",
    )
