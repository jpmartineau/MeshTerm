# SPDX-License-Identifier: Apache-2.0
"""The ``walk`` tool: the observed shape of the mesh, explored one node at a time.

The tool opens the full-screen Mesh walk (refer to :mod:`meshterm.ui.walk_screen`). It is
a browser in which the user walks over the evidence graph: trace walks, firmware routes,
heard relay chains, and repeater neighbour tables. One node has the focus. Its
neighbourhood draws as SNR-coloured braille edges on a small clean canvas, and its links
show below it as rows with quality bars and evidence. Enter walks the graph, ⌫ goes back
along the breadcrumb trail, and typed text finds any node (also the islands). The
geographic map tells where the mesh is. The walk tells how the nodes of the mesh connect.

Menu only: with the walk, the user examines stored evidence interactively (and the walk
transmits nothing). Thus there is no scripted one-shot command to register. The commands
of the ``trace`` family already print path evidence in scripted runs.
"""

from __future__ import annotations

from typing import Any

import typer

from ..context import AppContext
from .base import Tool, ToolResult, register


@register
class WalkTool(Tool):
    """See how the nodes of the mesh connect: the observed link graph, which the user walks."""

    name = "walk"
    title = "Mesh walk"
    # A single-codepoint emoji (as all the other tool icons are), not a VS16 sequence. On a
    # terminal that renders VS16 narrow, the emoji-width calibration removes VS16. Thus it can
    # measure a 🕸️ one cell short of how it paints and drift this row's help text right of
    # the column to which the other rows align. The wireframe globe shows the logical
    # topology. It is the equivalent of the 🌍 of the geographic map.
    icon = "🌐"
    help = "Walk the observed topology — links and SNR"
    category = "Explore"
    order = 20  # next to Map: the logical shape next to the geographic shape

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Run the walk. It has no parameters to collect.

        The screen shows all its content itself, and it returns when the user leaves it.
        Thus the return value ``None`` tells the menu that the run is complete (the same
        pattern as the ``dashboard`` tool).

        Args:
            ctx: The shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.walk_screen import open_walk

        await open_walk(ctx)
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Nothing runs here: the walk is in :meth:`prompt_params`.

        Args:
            ctx: The shared application context.
            params: Not used.

        Returns:
            An empty :class:`ToolResult` (only a scripted call comes here).
        """
        return ToolResult(summary={})

    def register_cli(self, app: typer.Typer) -> None:
        """Register no CLI command, because the walk is an interactive screen for the menu only.

        Args:
            app: The Typer application (not changed).
        """
