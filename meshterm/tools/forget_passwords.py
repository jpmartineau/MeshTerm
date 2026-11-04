# SPDX-License-Identifier: Apache-2.0
"""The ``forget-admin-passwords`` tool: forget all the admin passwords that MeshTerm stores.

MeshTerm stores the admin password of each node that accepted a login (refer to
:mod:`meshterm.core.admin_store`), so that the next login asks nothing. A user who changed
the passwords of their nodes, or who gives the machine to another person, must be able to
remove all of them in one step (GitHub issue 28). Del in the node picker forgets one
password (refer to :mod:`meshterm.ui.admin_picker`). This tool forgets all of them,
including the password of a node that the device does not know any more.

In the menu, the row is ``Forget passwords`` in the Managed nodes section. It is a
``popup`` tool: the red typed confirm floats over the menu, and so does the result. The
row says "passwords" and not "admin passwords", because the heading of its section already
says whose passwords they are, and the shorter title keeps the column of the titles at its
width. On the CLI, the command name says "admin", because a room has passwords too, and a
command line has no section heading.

On the CLI, ``forget-admin-passwords --yes`` forgets all the passwords and lists the nodes
that it forgot. Without ``--yes`` it is a usage error, as each command that deletes data
is. The exit status is ``5`` when the store held no password.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core import exitcodes
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.admin_store import AdminCredential
    from ..ui.report import Listing

#: A full public key in hex. The store keys a password by the key prefix or by the name
#: when a contact has no full key, and neither of these goes into the ``key`` of a node.
_FULL_KEY = re.compile(r"[0-9a-f]{64}")


@register
class ForgetPasswordsTool(Tool):
    """Forget all the stored admin passwords of remote nodes."""

    name = "forget-admin-passwords"
    title = "Forget passwords"
    icon = "🗑"
    help = "Clear every saved admin password"
    category = "Managed nodes"
    order = 30  # after the two tools that log in: it undoes what they stored
    popup = True  # dialog-sized: float over the menu, and do not replace it

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Ask for the typed confirm.

        Refer to :func:`~meshterm.ui.admin_picker.confirm_forget_all`.

        Args:
            ctx: The shared application context.

        Returns:
            ``{"yes": True}`` when the user typed the word, or ``None`` when the user backed
            out or the store held no password.
        """
        from ..ui.admin_picker import confirm_forget_all

        if not await confirm_forget_all(ctx, title=self.title):
            return None
        return {"yes": True}

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Forget all the stored admin passwords.

        Args:
            ctx: The shared application context.
            params: ``yes``: the confirm. The menu and the CLI both ask for it before they
                call this method.

        Returns:
            A :class:`ToolResult` with the number of passwords that MeshTerm forgot, and
            ``NO_RESULT`` when the store held none.
        """
        from ..ui.surface import TuiUi

        forgotten = ctx.admin_store.forget_all()
        count = len(forgotten)
        plural = "" if count == 1 else "s"
        return ToolResult(
            summary={"forgotten": count},
            message=f"[ok]✓[/ok] forgot [brand]{count}[/brand] admin password{plural}"
            if count
            else None,
            report=None if isinstance(ctx.ui, TuiUi) else (_forgotten(forgotten),),
            exit_code=exitcodes.OK if count else exitcodes.NO_RESULT,
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``forget-admin-passwords`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _forget(
            yes: bool = typer.Option(False, "--yes", help="Confirm this destructive action"),
        ) -> None:
            if not yes:
                raise typer.BadParameter(
                    "this forgets every saved admin password; each next login needs it "
                    "again. Re-run with --yes to confirm."
                )
            run_tool_command(self, {"yes": True})


def _forgotten(credentials: list[AdminCredential]) -> Listing:
    """The nodes whose passwords MeshTerm forgot, one row for each node.

    The store keeps the name of a node from its last good login, and the time of that
    login. It keeps no other fact about the node, so the row has no type and no hash.
    """
    from ..ui import fields
    from ..ui.report import Listing

    return Listing(
        key="forgotten",
        columns=(
            fields.node(lanes=(("name", "NAME"), ("key", "KEY"))),
            fields.when("last_login_at", "LAST_LOGIN"),
        ),
        rows=[
            {
                "node": fields.NodeRef(
                    name=c.label or None, key=c.key if _FULL_KEY.fullmatch(c.key) else None
                ),
                "last_login_at": _instant(c.last_used),
            }
            for c in credentials
        ],
        order=("NAME", "LAST_LOGIN", "KEY"),
    )


def _instant(text: str) -> datetime | None:
    """The stored ISO-8601 time of a login, or ``None`` if the store has none it can read."""
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None
