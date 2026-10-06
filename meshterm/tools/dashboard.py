# SPDX-License-Identifier: Apache-2.0
"""The ``dashboard`` tool: the live overview of the mesh, at the top of the Watch section.

The tool opens the full-screen dashboard (refer to :mod:`meshterm.ui.dashboard_screen`).
It shows the activity chart of all packets for two hours with its pulse line, the traffic
counts of the session for each packet class, and the RF health of the window next to the
live numbers of the radio itself. (The stream of each packet is the Live feed tool,
directly below.) The dashboard is first in the Watch section, because it answers the
first question of the section (what occurs on the mesh now?) before the user goes to a
specific tool.

Menu only: the dashboard is always live (it uses the event hub and paints again each
second). Thus there is no scripted one-shot command to register. The ``monitor`` and
``contacts`` subcommands are the script commands to examine the mesh.
"""

from __future__ import annotations

from typing import Any

import typer

from ..context import AppContext
from .base import Tool, ToolResult, register


@register
class DashboardTool(Tool):
    """Watch all that occurs on the mesh: activity, traffic, RF health."""

    name = "dashboard"
    title = "Dashboard"
    icon = "📊"
    help = "The mesh live — activity, traffic, RF health"
    category = "Watch"
    order = 10  # the overview of the section, above the tools that it summarizes

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Run the live dashboard. It has no parameters to collect.

        The screen shows all its content itself, and it returns when the user leaves it.
        Thus the return value ``None`` tells the menu that the run is complete (the same
        pattern as the ``advert`` tool).

        Args:
            ctx: The shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.dashboard_screen import open_dashboard

        await open_dashboard(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Nothing runs here: the dashboard is in :meth:`prompt_params`.

        Args:
            ctx: The shared application context.
            params: Not used.

        Returns:
            An empty :class:`ToolResult` (only a scripted call comes here).
        """
        return ToolResult(summary={})

    def register_cli(self, app: typer.Typer) -> None:
        """Register no CLI command, because the dashboard is a live screen for the menu only.

        Args:
            app: The Typer application (not changed).
        """
