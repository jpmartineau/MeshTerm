# SPDX-License-Identifier: Apache-2.0
"""Walk records: how MeshTerm measures and scores a completed trace for the trophy case.

A *walk* is one trace whose route starts and ends at our node: we transmit the first hop,
and the transmission of the last hop arrives back at us. Each trace is a walk:

* **Trace path** composes the full circuit by hand, and it must only end at a node that
  we can hear. Thus its route *is* the walk.
* **Trace target** walks the symmetric boomerang ``us → out… → target → out reversed…
  → us``, which also starts and ends at us.

Thus each successful trace can be scored. The trophy case keeps the record holders in
seven disciplines: the longest distance, the farthest node, the longest single link, the
most nodes (with and without revisits), the weakest link that still carried the walk, and
the widest enclosed loop.

This module only measures. :func:`walk_from_trace` changes a reply into a spec, a
canonical route, and its :class:`WalkStats`. :func:`walk_scores` scores these stats
against all the disciplines at the same time (a walk that was found during the trace of
one thing still counts in each discipline where it places). The UI owns the device and the
database. No code in this module transmits or stores.

One rule of eligibility applies to each board: a record walk must be a *trail*. A trail
is a walk that never crosses the same link two times in the same direction (refer to
:func:`first_repeated_edge`). If a walk can repeat a link, it can add a scoring subpath
again for a free score. Thus :func:`walk_scores` disqualifies such a walk completely. A
walk can cross a link again in the *other* direction: a Trace target boomerang crosses
each link again backwards by design, and radio links are truly different in each
direction.

Records are kept for each ``(category, width_bytes)``, because the hash width of each hop
limits two things: the maximum length of a walk (the transmitted path field is
:data:`MAX_PATH_BYTES`), and the probability of a collision. A 1-byte board and a 4-byte
board are different games.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise

from ..core.geo import haversine_km
from ..core.models import Hop, TraceResult

#: The transmitted path field has a fixed size of 64 bytes (refer to the contact-route
#: parsing in :mod:`~meshterm.core.connection`). Thus a walk can carry a maximum of this
#: number of bytes of hop hashes. This limit is the strict reason why records are kept
#: for each hash width.
MAX_PATH_BYTES = 64


def max_hops(width_bytes: int) -> int:
    """The maximum number of hops in a spec at ``width_bytes`` for each hop (64-byte field)."""
    return MAX_PATH_BYTES // max(1, width_bytes)


def first_repeated_edge(nodes: Sequence[str]) -> tuple[str, str] | None:
    """The first link that a walk crosses two times in the same direction, or ``None``.

    This is the no-cheat rule of the trophy case. The arbiter and each screen that warns
    about the rule share this function. A record walk must be a *trail*. In graph theory,
    a trail is a walk with no repeated edge. Here, the edge is the directed edge
    ``a → b``. Thus ``a → b … b → a`` is still a trail, but ``a → b … a → b`` is not. It
    is not necessary to give the implicit endpoints of the walk at our node: the arcs that
    touch us cannot repeat, except when we relay through ourselves in the middle of the
    walk.

    Args:
        nodes: The walked hops in order: spec tokens or canonical ids. The comparison
            ignores case. Blank entries are ignored.

    Returns:
        The ``(a, b)`` pair of the first link that was walked two times (as given, in
        lower case), or ``None`` when the walk is a trail.
    """
    walked = [n.strip().lower() for n in nodes if n and n.strip()]
    seen: set[tuple[str, str]] = set()
    for pair in pairwise(walked):
        if pair in seen:
            return pair
        seen.add(pair)
    return None


# --------------------------------------------------------------------------- scoring


@dataclass(slots=True)
class WalkStats:
    """All the values by which a finished walk is scored and shown.

    Attributes:
        node_ids: The canonical ids of the walked hops, in walk order (without us,
            because the walk starts and ends at our node implicitly).
        hop_count: The number of hops in the transmitted spec (= the relay
            transmissions on the mesh).
        distinct_nodes: The number of different nodes that the walk visited.
        repeats: True if the walk visited a node more than one time.
        min_snr: The weakest reading of a hop on the walk (dB), when measured.
        rtt_ms: The round trip of the walk, when measured.
        km_travelled: The sum of the great-circle km over the circuit us → hops… → us.
            A segment that touches a node with no position adds 0. Thus this value is a
            lower bound.
        km_complete: True if each segment had positions (then ``km_travelled`` is
            exact).
        far_km: The distance (km) from us to the farthest hop, when both ends have a
            position. ``None`` when there is no position for our node or for any hop.
        leg_km: The longest single link of the circuit (km): the transmission of one
            hop, from end to end, when both of its ends have a position. ``None`` when
            no segment had both. This is a different game from ``far_km``: a distant
            node that is reached over a chain of short hops scores far, not long.
        leg_link: The ids of the two ends of that link, in walk order, with ``None``
            for our node (a link that touches us is the first or the last segment).
            ``None`` when ``leg_km`` is ``None``.
        area_km2: The unsigned area in the circuit of the points with a position, in
            walk order (km², shoelace over a local plane). ``None`` below three points
            with a position. A walk that crosses itself scores its net algebraic area.
    """

    node_ids: tuple[str, ...]
    hop_count: int
    distinct_nodes: int
    repeats: bool
    min_snr: float | None
    rtt_ms: float | None
    km_travelled: float
    km_complete: bool
    far_km: float | None
    area_km2: float | None
    leg_km: float | None = None
    leg_link: tuple[str | None, str | None] | None = None

    def as_dict(self) -> dict:
        """The stats as a JSON-serializable dict (the ``stats_json`` column)."""
        return {
            "hop_count": self.hop_count,
            "distinct_nodes": self.distinct_nodes,
            "repeats": self.repeats,
            "min_snr": self.min_snr,
            "rtt_ms": self.rtt_ms,
            "km_travelled": round(self.km_travelled, 3),
            "km_complete": self.km_complete,
            "far_km": round(self.far_km, 3) if self.far_km is not None else None,
            "area_km2": round(self.area_km2, 3) if self.area_km2 is not None else None,
            "leg_km": round(self.leg_km, 3) if self.leg_km is not None else None,
            # A list, not a tuple, because this value goes through JSON and back. Our end
            # stays null, so that the code that reads it draws the app-wide ★ instead of a
            # name.
            "leg_link": list(self.leg_link) if self.leg_link is not None else None,
        }


@dataclass(frozen=True, slots=True)
class Category:
    """One discipline of the trophy case: what makes a walk a record.

    Attributes:
        id: A stable identifier (the database key). The title can change, but the id is
            never renamed.
        title: The name on the screen, in sentence case ("Most nodes").
        icon: A single glyph for menu rows.
        description: A one-line description of the game, shown under the section
            heading on the records screen (word-wrapped when necessary).
        unit: A short unit suffix for scores ("km", "nodes", "dB", "km²").
        ascending: ``True`` when *lower* scores win (Weakest link looks for the weakest
            link that still carried a walk back home).
        needs_positions: True if the score must have node positions. Thus the UI can
            say why the category has no scores, and does not score nothing silently.
        score: Changes the stats of a walk into its score. ``None`` when the walk
            cannot compete in this category (no positions, or a repeat in No revisits).
    """

    id: str
    title: str
    icon: str
    description: str
    unit: str
    score: Callable[[WalkStats], float | None]
    ascending: bool = False
    needs_positions: bool = False

    def format_score(self, value: float) -> str:
        """Render a score in the unit of the category (``12.4 km``, ``7 nodes``)."""
        if self.unit == "nodes":
            count = int(value)
            return f"{count} node{'s' if count != 1 else ''}"
        if self.unit == "dB":
            return f"{value:+.1f} dB"
        return f"{value:.1f} {self.unit}"


def _score_long_haul(stats: WalkStats) -> float | None:
    return stats.km_travelled if stats.km_travelled > 0 else None


def _score_far_point(stats: WalkStats) -> float | None:
    return stats.far_km


def _score_long_leg(stats: WalkStats) -> float | None:
    return stats.leg_km if stats.leg_km else None


def _score_grand_tour(stats: WalkStats) -> float | None:
    return float(stats.distinct_nodes) if stats.distinct_nodes else None


def _score_clean_trail(stats: WalkStats) -> float | None:
    if stats.repeats or not stats.distinct_nodes:
        return None
    return float(stats.distinct_nodes)


def _score_thin_thread(stats: WalkStats) -> float | None:
    return stats.min_snr


def _score_big_loop(stats: WalkStats) -> float | None:
    if stats.area_km2 is None or stats.area_km2 <= 0:
        return None
    return stats.area_km2


#: The seven disciplines. The ids are stable database keys and must never change. The
#: titles and the descriptions are only for the screen, and they are plain and descriptive.
CATEGORIES: tuple[Category, ...] = (
    Category(
        id="long_haul",
        title="Longest distance",
        icon="🛣",
        unit="km",
        description="travel the greatest distance and come home",
        score=_score_long_haul,
        needs_positions=True,
    ),
    Category(
        id="far_point",
        title="Farthest node",
        icon="🎯",
        unit="km",
        description="reach the node furthest from here",
        score=_score_far_point,
        needs_positions=True,
    ),
    Category(
        id="long_leg",
        title="Longest leg",
        icon="🏹",
        unit="km",
        description="cross the greatest distance in a single hop",
        score=_score_long_leg,
        needs_positions=True,
    ),
    Category(
        id="grand_tour",
        title="Most nodes",
        icon="🧳",
        unit="nodes",
        description="visit the most nodes, passing through some twice is fine",
        score=_score_grand_tour,
    ),
    Category(
        id="clean_trail",
        title="No revisits",
        icon="👣",
        unit="nodes",
        description="visit the most nodes without passing through any twice",
        score=_score_clean_trail,
    ),
    Category(
        id="thin_thread",
        title="Weakest link",
        icon="🕸",
        unit="dB",
        description="come home over the weakest link that still carries",
        score=_score_thin_thread,
        ascending=True,
    ),
    Category(
        id="big_loop",
        title="Biggest loop",
        icon="🔆",
        unit="km²",
        description="enclose the largest area inside the walk",
        score=_score_big_loop,
        needs_positions=True,
    ),
)

CATEGORY_BY_ID: dict[str, Category] = {c.id: c for c in CATEGORIES}


def local_xy(origin: tuple[float, float], point: tuple[float, float]) -> tuple[float, float]:
    """Project a lat/lon onto a local plane around ``origin``, in km.

    This is an equirectangular approximation. It is exact enough for areas of the size of
    a mesh (some tens of km), and it keeps the shoelace area in true km².
    """
    lat0, lon0 = origin
    lat, lon = point
    x = (lon - lon0) * 111.320 * math.cos(math.radians(lat0))
    y = (lat - lat0) * 110.574
    return x, y


def compute_walk_stats(
    node_ids: Sequence[str],
    hops: Sequence[Hop],
    *,
    rtt_ms: float | None,
    positions: dict[str, tuple[float, float]],
    self_pos: tuple[float, float] | None,
) -> WalkStats:
    """Measure one successful walk for all the categories at the same time.

    Args:
        node_ids: The canonical ids of the walked hops, in walk order (without us).
        hops: The reading of each hop in the trace reply. The last hop has no hash, and
            it is our device. It gives its SNR as the other hops do, because it is the
            reading on the link back home.
        rtt_ms: The measured round trip of the walk.
        positions: The known node positions, indexed by canonical id.
        self_pos: The position of our node, or ``None`` when the device shares no
            position.

    Returns:
        The :class:`WalkStats` of the walk.
    """
    ids = tuple(node_ids)
    snrs = [h.snr for h in hops if h.snr is not None]
    # The walked circuit for the geometry: us, each hop in order, then us again.
    points: list[tuple[float, float] | None] = [self_pos]
    points.extend(positions.get(node) for node in ids)
    points.append(self_pos)

    # The segments of the circuit carry their endpoints with their positions. Thus the
    # longest segment can name its link. ``None`` at either end is our node, where the
    # walk starts and ends.
    ends: list[str | None] = [None, *ids, None]

    km = 0.0
    complete = True
    leg_km: float | None = None
    leg_link: tuple[str | None, str | None] | None = None
    for i, (a, b) in enumerate(pairwise(points)):
        if a is None or b is None:
            complete = False
            continue
        span = haversine_km(a[0], a[1], b[0], b[1])
        km += span
        if leg_km is None or span > leg_km:
            leg_km, leg_link = span, (ends[i], ends[i + 1])

    far: float | None = None
    if self_pos is not None:
        dists = [
            haversine_km(self_pos[0], self_pos[1], p[0], p[1])
            for p in points[1:-1]
            if p is not None
        ]
        far = max(dists) if dists else None

    area: float | None = None
    if self_pos is not None:
        placed = [p for p in points[:-1] if p is not None]  # the circuit closes itself
        if len(placed) >= 3:
            xy = [local_xy(self_pos, p) for p in placed]
            twice = sum(
                xy[i][0] * xy[(i + 1) % len(xy)][1] - xy[(i + 1) % len(xy)][0] * xy[i][1]
                for i in range(len(xy))
            )
            area = abs(twice) / 2.0

    return WalkStats(
        node_ids=ids,
        hop_count=len(ids),
        distinct_nodes=len(set(ids)),
        repeats=len(set(ids)) < len(ids),
        min_snr=min(snrs) if snrs else None,
        rtt_ms=rtt_ms,
        km_travelled=km,
        km_complete=complete,
        far_km=far,
        area_km2=area,
        leg_km=leg_km,
        leg_link=leg_link,
    )


def walk_scores(stats: WalkStats) -> dict[str, float]:
    """Score a walk against each category (the rule of all the boards at the same time).

    A walk that repeats a directed link is not a trail (refer to
    :func:`first_repeated_edge`). It is disqualified from all the boards at the same time.
    Here, the arbiter applies the no-cheat rule, thus no caller must remember it.

    Returns:
        ``category id → score`` for each category in which the walk can compete. Empty
        for a disqualified walk.
    """
    if first_repeated_edge(stats.node_ids) is not None:
        return {}
    out: dict[str, float] = {}
    for category in CATEGORIES:
        value = category.score(stats)
        if value is not None:
            out[category.id] = value
    return out


def walk_from_trace(
    result: TraceResult,
    *,
    canonical: Callable[[str], str | None],
    positions: dict[str, tuple[float, float]],
    self_pos: tuple[float, float] | None,
) -> tuple[str, tuple[str, ...], WalkStats] | None:
    """Get the walk that a successful trace made: its spec, its route, and its stats.

    Each trace is a walk (it starts and ends at us, refer to the module docstring). Thus a
    successful reply can be scored directly from its hops. The addressed hops of the reply
    are the relays that it went through, in order. The last hop has no hash: it is our
    device, at the end of the walk. The relays become two things:

    * the spec for a new walk (their hashes, joined: exactly what *Trace this path*
      forces to walk it again), and
    * after canonicalization, the route over which the geometry is measured.

    Args:
        result: The trace reply to measure (it must have succeeded).
        canonical: Changes a hop hash into its canonical node id (the resolver of the
            topology). Thus the positions align, and new walks resolve. Unknown hops keep
            their hash.
        positions: The known node positions, indexed by canonical id.
        self_pos: The position of our node, or ``None`` when the device shares no
            position.

    Returns:
        ``(spec, route, stats)``: the comma-separated hex spec, the canonical route
        aligned with it, and the :class:`WalkStats` of the walk. ``None`` when the trace
        failed, or carried no addressable relay to score.
    """
    if not result.success:
        return None
    hashes = tuple(h.node.lower() for h in result.hops if h.node)
    if not hashes:
        return None
    route = tuple(canonical(h) or h for h in hashes)
    stats = compute_walk_stats(
        route,
        result.hops,
        rtt_ms=result.round_trip_ms,
        positions=positions,
        self_pos=self_pos,
    )
    return ",".join(hashes), route, stats
