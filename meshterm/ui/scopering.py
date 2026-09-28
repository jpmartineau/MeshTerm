# SPDX-License-Identifier: Apache-2.0
"""The scope ring: the views a screen of recorded traffic can narrow to, and their words.

A flood is either unscoped or scoped into a region, and a screen that counts floods can
show all of them or only one scope's. The dashboard and the Time Machine's mesh page both
cycle those views on ``s`` (F2/F3 on the PicoCalc), so the ring's order, the title atom a
view is named by, and the chip that names the next one are defined once, here.

A view is a :data:`ScopeKey` — ``("unscoped",)``, ``("region", name)`` or ``("unknown",)``
— and ``None`` is the unnarrowed view, every packet heard. A direct frame has no scope, so
it belongs to no view but that one.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..core.regions import Scope
from .menus import fit_cells

#: A scope view: ``("unscoped",)``, ``("region", name)`` or ``("unknown",)``.
ScopeKey = tuple[str, ...]


def scope_key(scope: Scope | None) -> ScopeKey | None:
    """The view a frame counts in, or ``None`` for one with no scope to state.

    A direct frame has none, and neither does one stored before its route type was kept;
    both are counted only in the unnarrowed view. Every scoped frame no known region name
    reproduces shares one ``unknown`` view: its code changes with each packet, so it
    cannot tell two unnamed regions apart.
    """
    if scope is None:
        return None
    if not scope.scoped:
        return ("unscoped",)
    if scope.region:
        return ("region", scope.region)
    return ("unknown",)


def _ring_order(key: ScopeKey) -> tuple[int, str]:
    """Where a view sits in the cycle: unscoped, the regions A–Z, then unknown.

    Alphabetical rather than busiest-first so the ring holds still while the counts move:
    a cycle whose order shifted under the reader's thumb would skip or repeat a view.
    """
    if key[0] == "unscoped":
        return (0, "")
    if key[0] == "region":
        return (1, key[1].casefold())
    return (2, "")


def ring(heard: Iterable[ScopeKey], current: ScopeKey | None) -> list[ScopeKey | None]:
    """Every view on offer: all, then each scope heard, in cycle order.

    The view on screen stays in the ring even once nothing in it is left to count (its
    last frame aged out, or a narrower window holds none), so the cycle never loses the
    reader's place; it simply leaves on the next press.
    """
    keys = set(heard)
    if current is not None:
        keys.add(current)
    return [None, *sorted(keys, key=_ring_order)]


def step(views: list[ScopeKey | None], current: ScopeKey | None) -> ScopeKey | None:
    """The view after ``current``.

    The cycle wraps: ``s`` is forward-only, with no reverse key of its own, so a ring
    that stopped at its end would strand the reader there.
    """
    return views[(views.index(current) + 1) % len(views)]


def scope_atom(key: ScopeKey, *, bare: bool = False) -> str:
    """A view's title atom: ``scope yul``, ``unscoped`` or ``unknown scope``.

    ``bare`` drops the ``scope`` lead-in before a region's name, for a title too narrow
    to spend the word on (the lexicon's region is shown bare anyway).
    """
    if key[0] == "region":
        return key[1] if bare else f"scope {key[1]}"
    return "unscoped" if key[0] == "unscoped" else "unknown scope"


def scope_chip(key: ScopeKey | None) -> str:
    """The F-lane chip naming the view a press goes *to*, within the lane's 6 cells."""
    word = "all" if key is None else key[-1]
    return "▸ " + fit_cells(word, 4).rstrip()
