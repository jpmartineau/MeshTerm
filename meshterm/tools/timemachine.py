# SPDX-License-Identifier: Apache-2.0
"""The ``timemachine`` tool: explore all that the recorder ever heard.

The tool opens the Time Machine (refer to :mod:`meshterm.ui.timemachine_screen`). The
user selects the whole mesh, or any node that MeshTerm ever heard, and reads its history
as braille charts: the reception volume, the median-SNR band, the rhythm by hour of the
day, the packets and nodes for each day, and the first arrivals. The time window is 24 h,
7 d, 30 d, or all the time, and the user can change it. The passive monitor writes this
history, and it started in the first session. This tool is where that history becomes
useful.

Menu only: the explorer is interactive (subjects, time windows, scroll). The ``monitor``
and ``nodes`` subcommands stay the scripted listings of the same data.
"""

from __future__ import annotations

from typing import Any

import typer

from ..context import AppContext
from .base import Tool, ToolResult, register


@register
class TimeMachineTool(Tool):
    """Read the stored history of the mesh: node timelines, daily volumes, arrivals."""

    name = "timemachine"
    title = "Time machine"
    icon = "⏳"
    help = "Recorded history — timelines, rhythms, arrivals"
    category = "Watch"
    order = 40  # the Dashboard above it, but for the past

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Run the explorer. It has no parameters to collect.

        The screen shows all its content itself, and it returns when the user leaves it.
        Thus the return value ``None`` tells the menu that the run is complete (the same
        pattern as the ``dashboard`` tool).

        Args:
            ctx: The shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.timemachine_screen import open_timemachine

        await open_timemachine(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Nothing runs here: the Time Machine is in :meth:`prompt_params`.

        Args:
            ctx: The shared application context.
            params: Not used.

        Returns:
            An empty :class:`ToolResult` (only a scripted call comes here).
        """
        return ToolResult(summary={})

    def register_cli(self, app: typer.Typer) -> None:
        """Register no CLI command, because the Time Machine is an interactive explorer.

        Args:
            app: The Typer application (not changed).
        """
