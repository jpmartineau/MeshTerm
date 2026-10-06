# SPDX-License-Identifier: Apache-2.0
"""The ``livefeed`` tool: each packet when it arrives, directly below the dashboard.

The tool opens the full-screen live feed (refer to :mod:`meshterm.ui.livefeed_screen`).
This is the streaming packet list that was the bottom panel of the dashboard before. Now
it has its own menu entry. It shows the newest packets first and all the classes, and
Enter opens any row in the shared packet viewer. It is directly below the dashboard,
because the two tools share one question. The dashboard summarizes what occurs on the
mesh, and the feed shows each packet behind those numbers.

Menu only: the feed is always live (it uses the event hub and paints again each second).
Thus there is no scripted one-shot command to register. The ``monitor`` subcommand is the
script command to watch packets.
"""

from __future__ import annotations

from typing import Any

import typer

from ..context import AppContext
from .base import Tool, ToolResult, register


@register
class LiveFeedTool(Tool):
    """Watch each packet as it arrives, newest first, and open any packet in the viewer."""

    name = "livefeed"
    title = "Live feed"
    icon = "📰"
    help = "Every packet as it arrives, newest first"
    category = "Watch"
    order = 20  # directly below the dashboard, which it came out of

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Run the live feed. It has no parameters to collect.

        The screen shows all its content itself, and it returns when the user leaves it.
        Thus the return value ``None`` tells the menu that the run is complete (the same
        pattern as the ``dashboard`` tool).

        Args:
            ctx: The shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.livefeed_screen import open_livefeed

        await open_livefeed(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Nothing runs here: the feed is in :meth:`prompt_params`.

        Args:
            ctx: The shared application context.
            params: Not used.

        Returns:
            An empty :class:`ToolResult` (only a scripted call comes here).
        """
        return ToolResult(summary={})

    def register_cli(self, app: typer.Typer) -> None:
        """Register no CLI command, because the feed is a live screen for the menu only.

        Args:
            app: The Typer application (not changed).
        """
