# SPDX-License-Identifier: Apache-2.0
"""The scope ring: the scope views that a screen of stored traffic can narrow to, and their words.

A flood is unscoped or it is scoped to a region. A screen that counts floods can show all
of them or only the floods of one scope. The dashboard and the mesh page of the Time
Machine both cycle those scope views on ``s`` (F2/F3 on the PicoCalc). Thus the order of
the ring, the title atom that names a scope view, and the chip that names the next scope
view are defined one time, here.

A scope view is a :data:`ScopeKey`: ``("unscoped",)``, ``("region", name)``, or
``("unknown",)``. ``None`` is the scope view that is not narrowed: each packet heard. A
direct packet has no scope, so it belongs only to that scope view.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..core.regions import Scope
from .menus import fit_cells

#: A scope view: ``("unscoped",)``, ``("region", name)`` or ``("unknown",)``.
ScopeKey = tuple[str, ...]


def scope_key(scope: Scope | None) -> ScopeKey | None:
    """The scope view in which a packet counts, or ``None`` for a packet with no scope to state.

    A direct packet has no scope. A packet that MeshTerm stored before it kept the route
    type has no scope, too. MeshTerm counts both only in the scope view that is not
    narrowed. A scoped packet that no known region name reproduces goes in one ``unknown``
    scope view. Its code changes with each packet, so the code cannot show that two
    unnamed regions are different.
    """
    if scope is None:
        return None
    if not scope.scoped:
        return ("unscoped",)
    if scope.region:
        return ("region", scope.region)
    return ("unknown",)


def _ring_order(key: ScopeKey) -> tuple[int, str]:
    """The place of a scope view in the cycle: unscoped, the regions A to Z, then unknown.

    The order is alphabetical, not busiest first, so that the ring does not move while the
    counts change. If the order of the cycle changed under the thumb of the user, the cycle
    would skip a scope view or repeat one.
    """
    if key[0] == "unscoped":
        return (0, "")
    if key[0] == "region":
        return (1, key[1].casefold())
    return (2, "")


def ring(heard: Iterable[ScopeKey], current: ScopeKey | None) -> list[ScopeKey | None]:
    """All the scope views on offer: all packets, then each scope heard, in cycle order.

    The scope view on the screen stays in the ring when nothing is left in it to count (its
    last packet aged out, or a narrower time window has none). Thus the cycle does not
    lose the place of the user. The scope view leaves the ring on the next key press.
    """
    keys = set(heard)
    if current is not None:
        keys.add(current)
    return [None, *sorted(keys, key=_ring_order)]


def step(views: list[ScopeKey | None], current: ScopeKey | None) -> ScopeKey | None:
    """The scope view after ``current``.

    The cycle wraps. The ``s`` key goes only forward and has no reverse key. If the ring
    stopped at its end, the user could not leave the end.
    """
    return views[(views.index(current) + 1) % len(views)]


def scope_atom(key: ScopeKey, *, bare: bool = False) -> str:
    """The title atom of a scope view: ``scope yul``, ``unscoped``, or ``unknown scope``.

    ``bare`` removes the ``scope`` word before the name of a region. It is for a title
    that is too narrow for the word. (The lexicon already shows a region without a prefix.)
    """
    if key[0] == "region":
        return key[1] if bare else f"scope {key[1]}"
    return "unscoped" if key[0] == "unscoped" else "unknown scope"


def scope_chip(key: ScopeKey | None) -> str:
    """The F-key lane chip that names the scope view that a key press goes *to*.

    The chip has the word alone, in all 6 cells. It has no ``▸`` lead-in (JP, 2026-10-04):
    the lead-in took two of the six cells of the chip to say what the place of the chip on
    the lane already says, and it cut the name of a region to four cells.
    """
    word = "all" if key is None else key[-1]
    return fit_cells(word, 6).rstrip()
