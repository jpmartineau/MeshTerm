# SPDX-License-Identifier: Apache-2.0
"""The ``map`` tool: plot the located nodes of the mesh on an OpenStreetMap terminal map.

The tool collects the nodes that share their location in adverts from the passive-monitor
history (:meth:`~meshterm.persistence.repository.Repository.heard_nodes`). It adds our node
when its position is known. Then it draws all the nodes over a real street basemap,
rendered as Unicode braille (streets, rivers, place names). This is the same terminal-map
idea as ``mapscii``.

Menu only: the tool opens a full-screen map that the user can pan and zoom (refer to
:mod:`meshterm.ui.map_screen`). It registers no CLI subcommand, because a map is a picture,
and the scripted CLI gives facts (refer to :meth:`MapTool.register_cli`). Repeaters have
priority over ordinary nodes: they have a different marker, drawn on top. The basemap is a
best effort. With no network (and no cached tiles), the tool plots the nodes on a blank
grid, and it still works offline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..services.markers import gather_markers
from .base import Tool, ToolResult, register


def _fraction(ctx: AppContext, params: dict[str, Any]) -> float:
    """The fraction of the mesh that a new map frames: the flag if given, else the preference.

    ``--fraction`` is resolved here, not as the default of the Typer option, because the
    options are declared when the CLI is registered. That occurs before a context exists,
    and thus before MeshTerm reads the preferences file.

    Args:
        ctx: The shared application context.
        params: The parameters of this run.

    Returns:
        The fraction to frame.
    """
    fraction = params.get("fraction")
    return ctx.preferences.map_view_fraction if fraction is None else float(fraction)


if TYPE_CHECKING:
    from ..core.models import MapMarker


@register
class MapTool(Tool):
    """Show the nodes of the mesh that share a location on a braille OpenStreetMap map."""

    name = "map"
    title = "Map"
    icon = "🌍"
    help = "Mesh nodes on a pannable street map"
    category = "Explore"
    order = 10

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Collect the located nodes and open the interactive map.

        The tool is for the menu only (refer to :meth:`register_cli`), so this method has
        one path.

        Args:
            ctx: The shared application context.
            params: An optional ``fraction``: the part of the mesh to frame when the map
                opens.

        Returns:
            A :class:`ToolResult` with the number of plotted nodes.
        """
        from ..ui.map_screen import open_map

        markers = await self._gather(ctx)
        if not markers:
            ctx.ui.note(
                "[muted]no contacts or heard nodes have shared a location yet — a node "
                "appears here once it advertises coordinates[/muted]"
            )
            return ToolResult(summary={"located": 0})

        await open_map(ctx, markers, fraction=_fraction(ctx, params))

        repeaters = sum(1 for m in markers if m.is_repeater and not m.is_self)
        self_located = any(m.is_self for m in markers)
        return ToolResult(
            summary={
                "located": len(markers),
                "repeaters": repeaters,
                "nodes": len(markers) - repeaters - (1 if self_located else 0),
                "self_located": self_located,
            },
        )

    # -- the markers ------------------------------------------------------------

    async def _gather(self, ctx: AppContext) -> list[MapMarker]:
        """Collect each located node to plot (refer to :func:`gather_markers`)."""
        return await gather_markers(ctx)

    def register_cli(self, app: typer.Typer) -> None:
        """Register no CLI command, because a map is a picture, and pictures are menu only.

        Each other feature has a scripted face, because its answer is a set of facts that
        a script can use. The answer of a map is a drawing. Its braille cells get their
        meaning from their position on a grid, and colour tells its nodes apart. The
        scripted CLI does not have these (refer to :mod:`meshterm.ui.script`). A one-shot
        render on the CLI is a block of glyphs that no program can parse. If the render
        has no colour, as the rest of the CLI, a person cannot read it either.

        A script can still get the located nodes: ``meshterm contacts`` lists each of
        them, with their coordinates.

        Args:
            app: The Typer application (not changed).
        """
