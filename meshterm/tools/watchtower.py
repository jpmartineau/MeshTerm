# SPDX-License-Identifier: Apache-2.0
"""The ``watchtower`` tool: a passive sentinel for the nodes that are important to you.

The tool opens the Watchtower screen (refer to :mod:`meshterm.ui.watchtower_screen`): the
alert log with acknowledge and clear, the watchlist, and the rules for each node. The
rules run in the background for the whole session (refer to
:mod:`meshterm.services.watchtower`):

- A silence alarm when MeshTerm no longer hears a starred node.
- An SNR-sag warning when the receptions of the node become weaker.
- A recovery note when the node comes back.
- A notice for the whole mesh when a node appears that MeshTerm never heard before.

The header shows the alerts that are not acknowledged as the ``▲ n`` badge. Thus this
screen does not have to be open for the watch to work.

Menu only: the sentinel is always a live service for the whole session. Scripted runs can
read the same history through ``monitor`` and ``nodes``.
"""

from __future__ import annotations

from typing import Any

import typer

from ..context import AppContext
from .base import Tool, ToolResult, register


@register
class WatchtowerTool(Tool):
    """Watch starred nodes: silence alarms, SNR sag, and new nodes that are heard."""

    name = "watchtower"
    title = "Watchtower"
    icon = "🚨"
    help = "Alerts on watched nodes — silence, SNR sag"
    category = "Watch"
    order = 30  # after the live screens: the alerts of the section, which are always on

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Run the Watchtower screen. It has no parameters to collect.

        The screen shows all its content itself, and it returns when the user leaves it.
        Thus the return value ``None`` tells the menu that the run is complete (the same
        pattern as the ``dashboard`` tool).

        Args:
            ctx: The shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.watchtower_screen import open_watchtower

        await open_watchtower(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Nothing runs here: the Watchtower is in :meth:`prompt_params`.

        Args:
            ctx: The shared application context.
            params: Not used.

        Returns:
            An empty :class:`ToolResult` (only a scripted call comes here).
        """
        return ToolResult(summary={})

    def register_cli(self, app: typer.Typer) -> None:
        """Register no CLI command, because the Watchtower is a live feature for the menu only.

        Args:
            app: The Typer application (not changed).
        """
