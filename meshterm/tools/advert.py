# SPDX-License-Identifier: Apache-2.0
"""The ``advert`` tool: announce this node to the mesh, directly from the main menu.

The tool opens the Send advert flow (refer to :func:`meshterm.ui.config_editor.send_advert`):
a zero-hop or flood advert, or the contact card of this node as a QR code to share. It is
a ``popup`` tool. The prompts and the result of the flow float as modal dialogs over the
main menu, which stays pushed (the pattern of the quit dialog). Thus this everyday action
stays a quick dialog in place, not a change of screen.

Menu only: the scripted equivalents are ``config advert`` and ``config share``, so this
module registers no duplicate subcommand.
"""

from __future__ import annotations

from typing import Any

import typer

from ..context import AppContext
from .base import Tool, ToolResult, register


@register
class SendAdvertTool(Tool):
    """Send a zero-hop or flood advert, or share this node's contact card."""

    name = "advert"
    title = "Send advert"
    icon = "📡"
    help = "Announce this node — zero-hop, flood, or QR"
    category = "This node"
    order = 40  # our node announces itself: a device action, not a screen
    popup = True  # dialog-sized: float over the menu, and do not replace it

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Run the interactive advert flow. It has no parameters to collect.

        The flow shows its own result (the note about the sent advert, or the contact
        card) before it returns. Thus, when the user leaves, :meth:`run` has nothing more
        to do. The return value ``None`` tells the menu that the run is complete (the
        same pattern as the Device config page).

        Args:
            ctx: The shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.config_editor import send_advert

        await send_advert(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Nothing runs here: the advert is sent in :meth:`prompt_params`.

        Args:
            ctx: The shared application context.
            params: Not used.

        Returns:
            An empty :class:`ToolResult` (only a scripted call comes here).
        """
        return ToolResult(summary={})

    def register_cli(self, app: typer.Typer) -> None:
        """Register no CLI command: ``config advert`` and ``config share`` are for scripts.

        Args:
            app: The Typer application (not changed).
        """
