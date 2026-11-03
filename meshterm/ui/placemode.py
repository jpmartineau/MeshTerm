# SPDX-License-Identifier: Apache-2.0
"""Place mode: a path line that names each hop by its place, and not by its node name.

Many node names say nothing about where a node is (``VE2XYZ``, ``RAK-1W``). On a path
screen, ^N changes the path lines between the node names (the default) and the places of
the nodes (refer to :mod:`meshterm.core.places`). Each screen keeps its own choice, and
opens with node names.

The change is applied in one place, to a finished :class:`~meshterm.ui.pathline.PathLine`
(:func:`placed`). Thus no builder of a path line has to know about places. A hop keeps its
key, so it keeps its colour: only its label changes. A chip shows the fine part of the place
(``Villeray``). A hop with a name but no known place shows its name in parentheses
(``(RAK-1W)``), so that the user can see that it is not a place. A hop with no name keeps
its hash, and our own node keeps its ``★``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from .pathline import PathHop, PathLine

if TYPE_CHECKING:
    from ..services.places import PlaceService

#: The action of the ^N chord (refer to ``session._CTRL_LETTER_CHORDS``).
ACTION = "places"

#: Gives the 12-hex node id for the hash of a hop, or ``None``.
NodeOf = Callable[[str], "str | None"]


def places_of_session() -> PlaceService | None:
    """The places of the session (``ctx.places``), for a screen that has no context."""
    from ..services.places import current

    return current()


def node_of(resolve: object) -> NodeOf | None:
    """The node-id half of a resolver that :func:`make_node_resolver` built, or ``None``."""
    return getattr(resolve, "node_of", None)


class PlaceToggle:
    """The ^N choice of one path screen: node names (the default) or places."""

    def __init__(self) -> None:
        """Start with node names."""
        self.on = False

    def flip(self) -> None:
        """Change between node names and places."""
        self.on = not self.on

    @property
    def hint(self) -> str:
        """The hint atom: what ^N does now."""
        return "^N names" if self.on else "^N places"

    @property
    def chip(self) -> str:
        """The label of the lane chip: what the key does now."""
        return "Names" if self.on else "Places"


def _hop(hop: PathHop, places: PlaceService, find: NodeOf | None) -> PathHop:
    """One hop in place mode."""
    if hop.you or hop.gap or hop.cursor or not hop.key or not hop.label:
        return hop
    key = hop.key.lower().removeprefix("0x")
    if hop.label.lower().removeprefix("0x") == key:
        return hop  # a hop with no name keeps its hash
    node = key[:12] if len(key) >= 12 else (find(hop.key) if find is not None else None)
    place = places.get(node) if node else None
    label = place.short if place is not None and place.short else f"({hop.label})"
    return replace(hop, label=label)


def placed_hops(
    hops: list[PathHop], places: PlaceService | None, find: NodeOf | None = None
) -> list[PathHop]:
    """The hops with each node name changed to its place (refer to the module docstring)."""
    if places is None:
        return hops
    return [_hop(hop, places, find) for hop in hops]


def placed(line: PathLine, places: PlaceService | None, find: NodeOf | None = None) -> PathLine:
    """The same path line, with each node name changed to its place.

    Args:
        line: The finished path line.
        places: The places of the nodes (``ctx.places``), or ``None`` to keep the line.
        find: Gives the node id for a short hash key (:func:`node_of` of the resolver that
            named the hops). A hop whose key is a full key prefix needs no help.
    """
    if places is None:
        return line
    return line.with_hops(placed_hops(line.hops, places, find))
