# SPDX-License-Identifier: Apache-2.0
"""The observed mesh topology: a link graph made from each path that we ever received.

MeshCore never tells us the shape of the network directly. But almost everything that we
receive carries a part of it:

* A successful trace is proof that the packet went through each link in its path (out
  and back, with an SNR reading at each hop).
* The firmware's ``out_path`` for each contact is a route made from received floods.
* When the packet logging of the companion is on, each overheard packet reports the relay
  chain on which it arrived.

We can ask for a fourth part. A repeater on which we have admin rights reports its own
neighbour table (the nodes that it hears directly, and at what SNR) when we query it over
the mesh. This is a second point of view. It shows links, and whole nodes, from which our
device never received anything. This module combines these parts into one undirected
graph of evidence. It answers the questions that the trace path composer asks of the
graph:

* Which nodes are neighbours: :meth:`MeshTopology.next_hops`, the suggestion list when
  the user composes a path hop by hop, with the strongest observed link first.
* How can we reach a target: :meth:`MeshTopology.scenarios`, ranked candidate outbound
  paths (the learned route of the device, the direct path, and the best alternatives that
  the evidence supports), ready for a trace or a probe.

Links are undirected on purpose. Radio paths work in both directions, and each reading is
evidence for the same physical link: an SNR measured at either end, or a route learned
from the opposite direction. Node identity is canonicalized against the contact list,
because the same node shows in different sources at different widths (a 1-byte trace hop
hash, a 12-hex key prefix in an observation, a full public key).

All of this module is pure data in, data out. The callers supply repository rows and
contacts, so a unit test can test the graph without a device or a database.
"""

from __future__ import annotations

import bisect
import heapq
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from itertools import chain, pairwise

from ..core.models import Contact, utcnow
from ..persistence.repository import NeighbourLink, PacketPath, TracedPath

#: The canonical id for our node in the graph (its 12-hex key prefix when known). By
#: construction, it is different from hop hashes, because it comes from the full public key.

#: The half-life of link evidence, in days. A reading that is one week old counts half as
#: much as a reading from now. Old links stay available as suggestions, but new
#: observations have the most effect on the ranking.
_EVIDENCE_HALF_LIFE_DAYS = 7.0

#: The SNR range that maps onto the link-quality factor. -15 dB (barely readable) gets the
#: floor, and +10 dB (excellent) gets the ceiling. This is the same span as the quality bar
#: on the trace screen.
_SNR_FLOOR_DB = -15.0
_SNR_CEIL_DB = 10.0
_QUALITY_MIN = 0.15
_QUALITY_MAX = 1.5

#: The cost for each hop, added when MeshTerm ranks candidate paths. Thus a longer path
#: that is only a little "stronger" does not win over a short path. Each extra hop is real
#: airtime and a real point of failure (and a trace goes through it two times: out and
#: back).
_HOP_PENALTY = 0.35

#: The limits for scenario generation: how many alternatives to offer, and how deep a
#: suggested route can go (the boomerang of the trace goes through each hop two times).
_MAX_SCENARIOS = 5
_MAX_SCENARIO_HOPS = 5

#: The test that an ambiguous short hash must pass before it merges into one of the nodes
#: that it can name (refer to :meth:`MeshTopology._corroborated_owner`). The first value is
#: how many discriminating neighbours (neighbours that link to exactly one candidate) must
#: vote for the winner. The second value is how far in front of the second candidate the
#: winner must be. Both values leave a contested stub as it is. Two independent neighbours
#: that agree, with no neighbour against them, are evidence. One neighbour is a
#: coincidence. A close second means that the hash probably combines the traffic of two
#: nodes.
_MIN_CORROBORATION = 2
_CORROBORATION_MARGIN = 3

#: The full width of a canonical node id, in hex digits (6 bytes of key). An id of this
#: width is never under-specified. Thus it cannot merge into another id, and it cannot
#: extend one. For this reason, the prefix settle walks only the ids below this width
#: (refer to :meth:`MeshTopology.coalesce_prefixes`).
_CANONICAL_WIDTH = 12

_HEX_DIGITS = frozenset("0123456789abcdef")

#: Tells apart "cached as None" and "not cached" in the identity caches.
_MISS = object()


def is_path_hash(value: str) -> bool:
    """Whether ``value`` can be a path hash: hex that is not empty, a whole number of bytes.

    This is the only test. All the code that must tell a hop id from a name or a graph
    sentinel uses it. The length must be even, because a hash is bytes, never a half byte.
    This rule also keeps out an endpoint marker of one character.
    """
    return bool(value) and len(value) % 2 == 0 and all(c in _HEX_DIGITS for c in value)


def render_forced_spec(hops: tuple[str, ...], target_hash: str, width_bytes: int) -> str:
    """Render a symmetric forced-path spec: the outbound hops, the target, the mirror back.

    The MeshCore trace protocol has no separate "return path" field. The full boomerang
    (out to the target, and back to our node) is one path that the repeaters walk in
    order. Thus the spec must give the return hops explicitly, not only the outbound
    hops. This function renders the symmetric round trip: the return leg is the outbound
    hops in reverse. This function adds them, so the caller does not have to give them. A
    route that the user composed, and that comes back on a different path, is rendered
    exactly as given by :func:`render_custom_spec` instead.

    Args:
        hops: Intermediate repeaters, in order from our node outward, without the two
            endpoints (empty means to trace the target directly, with no forced hops).
        target_hash: The hex hash of the target (any width ≥ 1 byte).
        width_bytes: The preferred path-hash width for each hop, in bytes (1, 2, 4, or
            8). :func:`collapse_width` makes it smaller when the known hash of a hop is
            narrower.

    Returns:
        A comma-separated hex spec, for example ``"3d,f2,3d"`` for one forced hop.
    """
    width = collapse_width(*hops, target_hash, ceiling=width_bytes)
    outbound = [h[: width * 2] for h in (*hops, target_hash)]
    outbound.extend(h[: width * 2] for h in reversed(hops))
    return ",".join(outbound)


def render_custom_spec(hops: tuple[str, ...], width_bytes: int) -> str:
    """Render a path spec that the user composed (asymmetric), exactly as given.

    This is the counterpart of :func:`render_forced_spec` for a route that the user gave
    in full: out to the target, and back home on the path that the user chose. This
    function adds nothing and mirrors nothing: the hops are the path. The trace protocol
    walks any sequence that ends in radio range of our node. Only the shared width rules
    apply.

    Args:
        hops: Each hop of the route in walk order, with the target, but without our
            node at the two ends (the reply comes to our node from the last hop).
        width_bytes: The preferred path-hash width for each hop, in bytes (1, 2, 4, or
            8). :func:`collapse_width` makes it smaller when the known hash of a hop is
            narrower.

    Returns:
        A comma-separated hex spec, for example ``"3d,f2,27"``, or ``""`` with no hops
        (a custom route with zero hops has no meaning).
    """
    if not hops:
        return ""
    width = collapse_width(*hops, ceiling=width_bytes)
    return ",".join(h[: width * 2] for h in hops)


def collapse_width(*hashes: str, ceiling: int) -> int:
    """The widest per-hop hash width that a spec can use and that each given hash can supply.

    Trace paths transmit all hops at one width: 1, 2, 4, or 8 bytes. Canonical node ids
    are usually 6 bytes. But an unidentified hop can be as short as the width at which it
    was traced before. Thus the spec must become as narrow as its narrowest hash.

    Args:
        *hashes: The hex hashes for one spec.
        ceiling: The width that the caller prefers (in bytes, itself 1, 2, 4, or 8).

    Returns:
        The largest of 1, 2, 4, and 8 that is ≤ ``ceiling`` and ≤ the byte length of each
        hash.
    """
    limit = min([ceiling, *(len(h) // 2 for h in hashes if h)] or [ceiling])
    return max(s for s in (1, 2, 4, 8) if s <= max(limit, 1))


@dataclass(slots=True)
class Link:
    """The evidence that MeshTerm collected for one undirected link between two nodes.

    Attributes:
        a: The canonical id of one endpoint (the lexically smaller one, so that the pair
            is a key).
        b: The canonical id of the other endpoint.
        samples: How many independent readings went through this link, from any source.
        snrs: The SNR readings (dB) for the link, in either direction.
        last_seen: The time when the link was most recently observed.
        sources: The evidence classes that observed it (``trace``, ``route``,
            ``packet``, ``neighbour``).
    """

    a: str
    b: str
    samples: int = 0
    snrs: list[float] = field(default_factory=list)
    last_seen: datetime | None = None
    sources: set[str] = field(default_factory=set)

    @property
    def median_snr(self) -> float | None:
        """The median SNR (dB) of the readings of the link, or ``None`` if there are none."""
        return statistics.median(self.snrs) if self.snrs else None

    def strength(self, now: datetime | None = None) -> float:
        """Score the observed reliability of the link, for the ranking (higher is stronger).

        Three factors multiply:

        * Evidence: the sample count on a log scale. Thus ten observations win over one,
          but a thousand do not hide all the others.
        * Recency: exponential decay with a
          :data:`one-week half-life <_EVIDENCE_HALF_LIFE_DAYS>`.
        * Quality: the median SNR, mapped linearly from -15 dB → :data:`_QUALITY_MIN` to
          +10 dB → more than 1.0. Links with no SNR reading (for example, links known only
          from a firmware route) get a neutral 1.0. They do not get a penalty for missing
          data.

        Args:
            now: The reference time for recency (the default is the current time).

        Returns:
            The strength score of the link, ``> 0``.
        """
        now = now or utcnow()
        evidence = 1.0 + math.log2(1 + self.samples)
        recency = 1.0
        if self.last_seen is not None and getattr(self.last_seen, "tzinfo", None):
            age_days = max(0.0, (now - self.last_seen).total_seconds() / 86400.0)
            recency = 0.5 ** (age_days / _EVIDENCE_HALF_LIFE_DAYS)
        snr = self.median_snr
        quality = 1.0
        if snr is not None:
            span = _SNR_CEIL_DB - _SNR_FLOOR_DB
            frac = (snr - _SNR_FLOOR_DB) / span
            reach = _QUALITY_MIN + frac * (_QUALITY_MAX - _QUALITY_MIN)
            quality = min(_QUALITY_MAX, max(_QUALITY_MIN, reach))
        return evidence * recency * quality


@dataclass(slots=True)
class HopSuggestion:
    """One suggested next hop, when the user composes a path.

    Attributes:
        node: The canonical id of the suggested node.
        link: The evidence for the link from the current end of the path to ``node``.
        strength: The strength of the link, calculated before (the sort key). Thus the UI
            can render a stable ranking, and does not calculate the score again at each
            paint.
    """

    node: str
    link: Link
    strength: float


@dataclass(slots=True)
class PathScenario:
    """One candidate outbound route to a target, ready for a trace.

    A scenario stores only the outbound leg. The return is always the symmetric mirror,
    so there is nothing to choose for it. But :meth:`spec` renders the full boomerang,
    because the trace protocol has no separate field for the return path: the full route
    out and back is one spec that the repeaters walk in order.

    Attributes:
        label: A short description for the user, of where the route came from.
        hops: The canonical ids of the intermediate repeaters, in order from our node
            outward, without the two endpoints (empty means to trace the target directly).
        source: The evidence class that proposed the route (``device``, ``direct``,
            ``observed``).
        score: The ranking score (higher first): the strength of the weakest link of the
            path, with a penalty for the hop count. Direct and device scenarios with no
            observed evidence score 0.
        weakest_snr: The lowest median SNR (dB) of a link on the route, when known. This
            is the bottleneck that the trace will probably measure.
        samples: The total of the readings on the links of the route (an approximate
            measure of confidence).
    """

    label: str
    hops: tuple[str, ...]
    source: str
    score: float
    weakest_snr: float | None = None
    samples: int = 0

    def spec(self, target_hash: str, width_bytes: int) -> str:
        """Render the scenario as a forced-path spec, with the return leg.

        Refer to :func:`render_forced_spec` for the rules of the width and the return leg.

        Args:
            target_hash: The hex hash of the target (any width ≥ 1 byte).
            width_bytes: The preferred path-hash width for each hop, in bytes (1, 2, 4,
                or 8).

        Returns:
            A comma-separated hex spec, for example ``"3d,f2,3d"``.
        """
        return render_forced_spec(self.hops, target_hash, width_bytes)


class MeshTopology:
    """The evidence graph of the observed mesh links, which answers queries for route suggestions.

    Build one with :func:`build_topology`. Instances are snapshots that cost little, and
    that are immutable in intent. When new evidence arrives, build a new one, and do not
    change the old one.
    """

    def __init__(self, self_id: str, contacts: list[Contact]) -> None:
        """Make an empty graph, with our node at its root.

        Args:
            self_id: The canonical id of our node (12-hex key prefix, in lower case).
            contacts: The known contacts. MeshTerm uses them to canonicalize hop hashes of
                any width to stable node ids, and to name nodes for display.
        """
        self.self_id = self_id.lower().removeprefix("0x")[:12]
        self._links: dict[tuple[str, str], Link] = {}
        self._now = utcnow()
        # The contact index for canonicalization: full public keys in lower case (or the
        # stored prefix, if there is no key), mapped to the 12-hex canonical id and the
        # display name.
        self._known: list[tuple[str, str, str]] = []  # (full_key, canonical, name)
        for c in contacts:
            key = (c.public_key or c.key_prefix or "").lower().removeprefix("0x")
            if key:
                self._known.append((key, key[:12], c.name))
        # ``_known`` never changes after construction. A build sends the same few dozen
        # distinct hashes through canonical() thousands of times (each hop of each stored
        # packet path). Thus both identity lookups cache their results for each instance.
        self._canonical_memo: dict[str, str | None] = {}
        self._name_memo: dict[str, str | None] = {}
        # Route search results for each (target, k), valid for one shape of the graph.
        # Each change to a link increments the version, so a cached run of Yen's algorithm
        # can never stay longer than the evidence that it ranked. Callers ask for the same
        # target two times when a screen opens (suggested() and scenarios() both run the
        # search). The search is the most costly part of an open that uses the topology.
        self._graph_version = 0
        self._routes_memo: dict[tuple[str, int, int], list[tuple[str, ...]]] = {}

    # --- identity ----------------------------------------------------------------

    def canonical(self, hop: str | None) -> str | None:
        """Map a hop hash of any width onto a stable node id.

        Trace hops arrive at the path-hash width of the command (1-8 bytes), observation
        ids (key prefixes) at 12 hex, and routes at the width of the region. All of them
        name the same nodes. A hash that is a prefix of exactly one known contact takes
        the 12-hex id of that contact. An ambiguous or unknown hash stays as it is. Thus
        MeshTerm keeps the evidence about nodes that it cannot identify (under the short
        id), and does not guess them onto the wrong node.

        Args:
            hop: The raw hex hash (any case, optional ``0x``), or ``None`` or empty.

        Returns:
            The canonical id, or ``None`` for a missing or non-hex hop (non-hex includes
            old simulator artefacts such as ``hop0``).
        """
        if not hop:
            return None
        cached = self._canonical_memo.get(hop, _MISS)
        if cached is not _MISS:
            return cached
        needle = hop.lower().removeprefix("0x")
        if not is_path_hash(needle):
            result: str | None = None
        else:
            matches = {
                canonical
                for key, canonical, _name in self._known
                if key.startswith(needle) or needle.startswith(key[:12])
            }
            result = next(iter(matches)) if len(matches) == 1 else needle[:12]
        self._canonical_memo[hop] = result
        return result

    def display_name(self, node: str) -> str | None:
        """The contact name for a canonical id, or ``None`` when it is not known."""
        cached = self._name_memo.get(node, _MISS)
        if cached is not _MISS:
            return cached
        result = next((name for _key, canonical, name in self._known if canonical == node), None)
        self._name_memo[node] = result
        return result

    # --- construction ------------------------------------------------------------

    def add_walk(
        self,
        nodes: list[str | None],
        *,
        snrs: list[float | None] | None = None,
        when: datetime | None = None,
        source: str,
    ) -> None:
        """Add one walked node sequence to the graph, as a set of link readings.

        Each pair of consecutive nodes becomes one reading on their undirected link.
        ``nodes`` holds canonical ids. ``None`` entries (hops that cannot be resolved)
        break the chain, so that no false adjacency is invented across them. The method
        skips self-loops (a hash repeated two times in sequence, for example a
        there-and-back path that the user forced).

        Args:
            nodes: The walked sequence, with the endpoints, in order.
            snrs: Optional SNR readings for each link, aligned with the links
                (``snrs[i]`` is for the ``nodes[i] → nodes[i+1]`` link, measured at
                arrival).
            when: When the walk was observed (this sets the recency of each touched link).
            source: The evidence class tag (``trace``, ``route``, ``packet``,
                ``neighbour``).
        """
        self._graph_version += 1  # link evidence changed, so cached route searches expire
        for i in range(len(nodes) - 1):
            a, b = nodes[i], nodes[i + 1]
            if not a or not b or a == b:
                continue
            key = (a, b) if a < b else (b, a)
            link = self._links.get(key)
            if link is None:
                link = self._links[key] = Link(a=key[0], b=key[1])
            link.samples += 1
            link.sources.add(source)
            snr = snrs[i] if snrs and i < len(snrs) else None
            if snr is not None:
                link.snrs.append(snr)
            if when is not None and (link.last_seen is None or when > link.last_seen):
                link.last_seen = when

    def coalesce_prefixes(self) -> None:
        """Merge each under-specified node id into the one longer id that it can be.

        The same physical node comes into the graph at different hash widths: a trace hop
        logged at 1 byte (``27``), a packet relay at 3 bytes (``27d439``), and a full
        6-byte contact id (``27d4396a2967``). :meth:`canonical` widens a hop to a contact
        id only when the prefix names exactly one contact. But the mesh has two nodes whose
        keys both start with ``27`` (``27d4…`` and ``27e4…``). Thus the bare ``27`` hop
        stays short, and stands next to the wide ``27d4396a2967`` as a false second node.
        If nothing corrects this, the node shows two times on each surface that the graph
        supplies. The mesh walk draws it two times, the route graph draws two lanes to one
        repeater, and the scenario ranker offers a redundant weaker route through the stub.

        This method closes the gap with the evidence that the graph has, not with the full
        contact list. It uses two passes, in the order of decreasing confidence:

        1. **By prefix only** (:meth:`_next_prefix_merge`). A short id merges into a longer
           node id in the graph when it is a strict prefix of **exactly one** of them. The
           method follows a prefix chain (``65`` → ``6532`` → ``6532eb``) to its longest
           end. Nothing is inferred: the short id can only mean that one node.
        2. **By corroboration** (:meth:`_next_corroborated_merge`). A short id that is the
           start of two different longer nodes (``c5`` → ``c5bc…`` and ``c5ba…``) is
           ambiguous by prefix. But the graph usually knows which node it is. The stub
           carries the links of the node that it really was, so its neighbourhood names
           the owner. Neighbours that can tell the candidates apart vote, and a decisive
           result merges the stub into the winner. Refer to that method for the test that a
           vote must pass.

        The passes alternate. The graph changes with each merge, so an ambiguity can
        resolve itself after a neighbouring stub merges. The certain pass always runs again
        before the inference pass gets another turn.

        A short id that is the start of no longer node (a node that we only heard with a
        narrow hash) keeps its width. It is one node, with only a short name. A short id
        whose vote stays contested also keeps its width. A merge moves the links of the
        short id into the links of the wide id. It adds the samples, combines the SNR
        readings and the sources, and keeps the newest observation. Thus the wide node gets
        each reading that the stub collected. Idempotent: a second call finds nothing more
        to merge.

        :func:`build_topology` calls this method one time at its end, so each consumer
        sees each node one time. It is safe for our node (its 12-hex id is never a
        candidate as a short prefix).

        **The method builds one neighbour table, and then keeps it through the merges.** It
        does not walk the full graph again at each turn. Before, three separate passes over
        each link ran for each merge. One found the node set, one built the neighbour table
        that the corroboration vote reads, and one found the links that name the id to
        merge. On the real mesh that was measured, that was approximately 1150 links walked
        three times, approximately eighty times: the largest remaining cost when the mesh
        walk opened. The same table answers all three questions. The effect of a merge on
        the table is known exactly where the merge occurs (refer to :meth:`_merge_node`).
        Thus the table is the live node set and adjacency of the graph for all the settle.
        Its keys are the node ids: a node is in the table exactly while it holds a link.
        """
        adjacency = self._adjacency()
        while True:
            # Only an under-specified id can merge, and a full-width id extends nothing.
            # Thus both passes walk only the short ids, the shortest first (a 65 → 6532 →
            # 6532eb chain collapses from the end inward over the next turns). A graph of
            # nodes that all have full names settles, and neither pass examines a link.
            #
            # Ties break lexicographically, and this makes a settle repeatable. An order by
            # length only leaves ids of equal width in the iteration order of the node set,
            # and CPython randomizes string hashing in each process. Thus two runs over the
            # same evidence could do the merges in different orders. Where a merge changes
            # what a later merge sees, the two runs could make graphs that are different.
            shorts = sorted(
                (node for node in adjacency if len(node) < _CANONICAL_WIDTH),
                key=lambda node: (len(node), node),
            )
            if not shorts:
                return
            ordered = sorted(adjacency)
            merge = self._next_prefix_merge(shorts, ordered) or self._next_corroborated_merge(
                shorts, ordered, adjacency
            )
            if merge is None:
                return
            self._merge_node(*merge, adjacency=adjacency)

    def _node_ids(self) -> set[str]:
        """Each node id that is now an endpoint of a link."""
        return set(chain.from_iterable(self._links))

    def _next_prefix_merge(self, shorts: list[str], ordered: list[str]) -> tuple[str, str] | None:
        """The next ``(short, long)`` pair to merge, or ``None`` when no pair remains.

        A short id (shorter than the full canonical width of 6 bytes) merges when the
        longer ids in the graph that extend it are all on one prefix chain. That is, the
        longest of them starts with each of the others. Thus the short id can only mean
        that one node.

        Args:
            shorts: The under-specified node ids, the shortest first. Thus a ``65`` →
                ``6532`` → ``6532eb`` chain collapses from the end inward over the next
                calls.
            ordered: Each node id, sorted lexicographically (refer to :meth:`_extensions`).
        """
        for short in shorts:
            exts = self._extensions(short, ordered)
            if not exts:
                continue
            longest = max(exts, key=len)
            if all(longest.startswith(ext) for ext in exts):  # one node, not two
                return short, longest
        return None

    @staticmethod
    def _extensions(short: str, ordered: list[str]) -> list[str]:
        """Each node id in ``ordered`` that strictly extends the under-specified ``short``.

        Empty for a full-width canonical id (12 hex is never under-specified), and for a
        short id that nothing in the graph extends.

        ``ordered`` must be the node ids sorted lexicographically. This makes the method
        fast: all the ids that have the same prefix sort into one contiguous run. Thus the
        extensions are a slice that a binary search finds, not a scan of the full graph.
        This is important, because the caller asks this question one time for each node,
        and the caller of that caller starts the full pass again after each merge. On a
        mesh with approximately 1100 links and a few hundred merges to settle, a linear
        scan here made approximately 24 million ``len`` calls, and the mesh walk took
        sixteen seconds to open.
        """
        if len(short) >= _CANONICAL_WIDTH:
            return []
        out: list[str] = []
        width = len(short)
        for i in range(bisect.bisect_left(ordered, short), len(ordered)):
            other = ordered[i]
            if not other.startswith(short):
                break  # after the run: no later id can have the prefix
            if len(other) > width:
                out.append(other)
        return out

    def _next_corroborated_merge(
        self, shorts: list[str], ordered: list[str], adjacency: dict[str, set[str]]
    ) -> tuple[str, str] | None:
        """The next ambiguous stub that the neighbourhood evidence resolves, or ``None``.

        :meth:`_next_prefix_merge` merges only what the hash alone settles. This method
        answers the case that the other method leaves: a short id such as ``bf`` that is
        the start of two real nodes (``bf61f2…`` and ``bfbeef…``). By prefix, that is a
        coin toss. But the stub is not an empty label. It carries the links of the node
        that it really was, so its neighbours name its owner. Each neighbour that can tell
        the candidates apart is a vote: a neighbour of the stub that links to exactly one
        candidate. Both candidates share the other neighbours, and two repeaters in the
        same city have many of them. These neighbours do not vote, so that the local
        density does not hide the signal.

        The test is high on purpose, because a wrong merge does what this full routine
        exists to prevent: it gives the evidence of one node to another node. The winner
        must get at least :data:`_MIN_CORROBORATION` discriminating votes, and must be
        :data:`_CORROBORATION_MARGIN`× in front of the second candidate. Thus a stub that
        is really contested stays exactly where it was: by itself, with a short name, but
        with no false claim. The neighbours of such a stub point to both candidates, that
        is, the stub really is the traffic of two nodes combined under one hash.

        The shortest ids come first, the same as in :meth:`_next_prefix_merge`. Only the
        maximal candidates compete. The inner links of a chain (``f0`` → ``f062eb`` →
        ``f062eb…``) are the same node, so they never split their own vote.

        Args:
            shorts: The under-specified node ids, the shortest first.
            ordered: Each node id, sorted lexicographically (refer to :meth:`_extensions`).
            adjacency: The live neighbour table of the graph, which the settle loop that
                calls this method carries (refer to :meth:`coalesce_prefixes`).
        """
        for short in shorts:
            exts = self._extensions(short, ordered)
            candidates = [
                ext
                for ext in exts
                if not any(other != ext and other.startswith(ext) for other in exts)
            ]
            if len(candidates) < 2:  # the certain pass settled (or ignored) this id
                continue
            owner = self._corroborated_owner(short, candidates, adjacency)
            if owner is not None:
                return short, owner
        return None

    def _adjacency(self) -> dict[str, set[str]]:
        """The graph as neighbour sets, for questions that are difficult to ask of link pairs."""
        adjacency: dict[str, set[str]] = {}
        for a, b in self._links:
            adjacency.setdefault(a, set()).add(b)
            adjacency.setdefault(b, set()).add(a)
        return adjacency

    @staticmethod
    def _corroborated_owner(
        short: str, candidates: list[str], adjacency: dict[str, set[str]]
    ) -> str | None:
        """The candidate that the discriminating neighbours of the stub elect, if any.

        A neighbour votes only when it separates the candidates: it is a neighbour of
        exactly one candidate. Thus shared neighbours count for nothing, and the ``short``
        and candidate ids never vote for their own case. ``None`` means that there is no
        decisive winner: too few votes, or a second candidate so close that the stub may
        be the traffic of both nodes under one hash.

        Args:
            short: The under-specified id to resolve.
            candidates: The distinct node ids that it can name (two or more).
            adjacency: The neighbour sets of the full graph (refer to :meth:`_adjacency`).

        Returns:
            The elected candidate, or ``None`` to leave the stub as it is.
        """
        field = set(candidates) | {short}
        votes = dict.fromkeys(candidates, 0)
        for neighbour in adjacency.get(short, ()) - field:
            electors = [c for c in candidates if neighbour in adjacency.get(c, ())]
            if len(electors) == 1:
                votes[electors[0]] += 1
        ranked = sorted(votes.items(), key=lambda pair: (-pair[1], pair[0]))
        (winner, top), (_runner, second) = ranked[0], ranked[1]
        if top < _MIN_CORROBORATION or top < _CORROBORATION_MARGIN * second:
            return None
        return winner

    def _merge_node(
        self, src: str, dst: str, *, adjacency: dict[str, set[str]] | None = None
    ) -> None:
        """Relabel each link of ``src`` to ``dst``, and combine the links that they share.

        The method finds the links through the neighbour table, not with a scan of the
        graph. A link is addressed by its two endpoints, so the neighbours of ``src`` name
        its links directly. That is a few dict lookups, not a walk over each link in the
        mesh, for each of the many merges of a settle.

        Args:
            src: The under-specified id to merge away.
            dst: The node id that it can only mean.
            adjacency: The live neighbour table of the caller, **updated in place** to
                agree with the changed graph. ``src`` leaves it, and ``dst`` gets the
                neighbours of ``src``. In one special case, ``dst`` itself also leaves:
                when each link of ``src`` went to ``dst``. Each of these links collapses to
                a self-loop and is discarded, so ``dst`` goes with them, unless it had a
                link of its own. No other node can lose its last link here: a relabelled
                link keeps its far end. Omit this argument for a single merge, and the
                method builds the table here (and discards it after).
        """
        if adjacency is None:
            adjacency = self._adjacency()
        self._graph_version += 1  # the graph changes shape, so cached searches expire
        for far in adjacency.pop(src, ()):
            link = self._links.pop((src, far) if src < far else (far, src), None)
            if link is None:
                continue
            adjacency[far].discard(src)
            if far == dst:  # a src→dst link (src is a prefix of dst) becomes a self-loop
                continue
            new_key = (dst, far) if dst < far else (far, dst)
            existing = self._links.get(new_key)
            if existing is None:
                self._links[new_key] = Link(
                    a=new_key[0],
                    b=new_key[1],
                    samples=link.samples,
                    snrs=list(link.snrs),
                    last_seen=link.last_seen,
                    sources=set(link.sources),
                )
            else:
                existing.samples += link.samples
                existing.snrs.extend(link.snrs)
                existing.sources |= link.sources
                if link.last_seen is not None and (
                    existing.last_seen is None or link.last_seen > existing.last_seen
                ):
                    existing.last_seen = link.last_seen
            adjacency.setdefault(dst, set()).add(far)
            adjacency[far].add(dst)
        if not adjacency.get(dst, ()):
            adjacency.pop(dst, None)  # all its links went to the stub that is now merged

    # --- queries -----------------------------------------------------------------

    @property
    def link_count(self) -> int:
        """How many distinct links the graph has evidence for."""
        return len(self._links)

    def links(self) -> list[Link]:
        """Each link that the graph has evidence for (in no specific order).

        This is the view of the full graph that the Mesh walk draws. For path queries,
        use :meth:`next_hops` or :meth:`scenarios` instead, because these rank and filter.
        """
        return list(self._links.values())

    def link(self, a: str, b: str) -> Link | None:
        """The evidence for the undirected link between ``a`` and ``b``, if there is any."""
        return self._links.get((a, b) if a < b else (b, a))

    def next_hops(self, tail: str, *, exclude: frozenset[str] = frozenset()) -> list[HopSuggestion]:
        """Suggest the nodes that ``tail`` can reach, with the strongest observed link first.

        This is the step-by-step feed of the composer. From the current end of the path,
        it gives each node that ``tail`` can hear, as the evidence says (links are
        bidirectional, so a path observed in either direction is accepted). It does not
        give the nodes that the path already uses.

        Args:
            tail: The canonical id at which the path ends now (our own id at the start).
            exclude: The canonical ids to omit (the nodes already in the path). A node
                used again is never useful, and repeaters discard duplicate hops in any
                case.

        Returns:
            Suggestions sorted by decreasing link strength.
        """
        out: list[HopSuggestion] = []
        for (a, b), link in self._links.items():
            other = b if a == tail else a if b == tail else None
            if other is None or other in exclude:
                continue
            out.append(HopSuggestion(node=other, link=link, strength=link.strength(self._now)))
        out.sort(key=lambda s: (-s.strength, s.node))
        return out

    def scenarios(
        self, target: str, *, device_route: tuple[str, ...] | None = None
    ) -> list[PathScenario]:
        """Rank the candidate outbound routes to ``target``.

        Three families, without duplicates, in this order of priority:

        1. **device**: the route that the firmware itself learned from received floods
           (the route that an auto-routed trace walks), when the caller supplies one.
        2. **direct**: no repeaters at all. MeshTerm always offers it as the baseline.
        3. **observed**: the strongest simple paths through the evidence graph (the score
           is the weakest link, with a penalty for each hop). This family gives the paths
           that the device never learned, but that the data supports.

        Each scenario stores only an outbound leg. The return is always those hops in
        mirror order, so there is nothing to rank there. But :meth:`PathScenario.spec`
        renders the full boomerang. The trace implicitly goes through the intermediate
        links two times.

        Args:
            target: The canonical id of the target.
            device_route: The learned route of the firmware (canonical intermediate
                hops), if the contact has one.

        Returns:
            Scenarios ranked with the best evidence first. The device route, when there
            is one, is always first, whatever its score, because it is what "auto" does.
        """
        seen: set[tuple[str, ...]] = set()
        out: list[PathScenario] = []

        def add(label: str, hops: tuple[str, ...], source: str) -> None:
            if hops in seen or len(hops) > _MAX_SCENARIO_HOPS or target in hops:
                return
            seen.add(hops)
            score, weakest, samples = self._score_route(hops, target)
            out.append(
                PathScenario(
                    label=label,
                    hops=hops,
                    source=source,
                    score=score,
                    weakest_snr=weakest,
                    samples=samples,
                )
            )

        if device_route is not None:
            add("device route", device_route, "device")
        add("direct", (), "direct")
        for hops in self._best_routes(target, k=_MAX_SCENARIOS):
            add("observed path", hops, "observed")

        # Rank the observed evidence, but keep the device route pinned at the top. It is
        # the reference point for the comparison of each alternative.
        head = [s for s in out if s.source == "device"]
        rest = sorted(
            (s for s in out if s.source != "device"),
            key=lambda s: (-s.score, len(s.hops)),
        )
        return (head + rest)[:_MAX_SCENARIOS]

    def suggested(self, target: str) -> PathScenario | None:
        """The one best outbound route to ``target`` that has evidence, if one exists.

        This is the answer from the data to "what is the best way to reach this node?".
        It is the **observed** scenario with the highest score: a route through repeaters
        that the recorder observed when they carried traffic (traces, overheard relay
        chains, neighbour tables that MeshTerm read). It is ranked by its weakest link,
        with a penalty for each hop, and the sample counts and median SNR of its links are
        behind the score. It ignores on purpose the device and direct families that
        :meth:`scenarios` also offers. The learned route of the firmware is what an
        auto-routed trace already walks (the caller prefers it when it has one). A direct
        path is the trivial fallback. Neither is a suggestion that the observations made.
        For a node that the evidence can only reach directly (or not at all), the method
        returns ``None``. Thus the caller keeps the default that it uses otherwise.

        Args:
            target: The canonical id of the target.

        Returns:
            The best observed :class:`PathScenario` (always with at least one intermediate
            hop, and a positive score from evidence), or ``None`` when the observations
            support nothing better than a direct route.
        """
        for scenario in self.scenarios(target):
            if scenario.source == "observed" and scenario.hops and scenario.score > 0:
                return scenario
        return None

    # --- internals -----------------------------------------------------------------

    def _score_route(self, hops: tuple[str, ...], target: str) -> tuple[float, float | None, int]:
        """Score one outbound route by its weakest observed link.

        A chain is only as reliable as its weakest link. Thus the score of the route is
        the minimum link strength along ``us → hops… → target``, reduced for each hop
        (refer to :data:`_HOP_PENALTY`). A route that has a link with no evidence scores
        0. MeshTerm can offer it, but ranks it below all the routes that it observed.

        Args:
            hops: The intermediate canonical ids (can be empty for a direct route).
            target: The canonical id of the destination.

        Returns:
            ``(score, weakest_median_snr, total_samples)``.
        """
        chain = [self.self_id, *hops, target]
        weakest_strength: float | None = None
        weakest_snr: float | None = None
        samples = 0
        for a, b in pairwise(chain):
            link = self.link(a, b)
            if link is None:
                return 0.0, None, samples
            strength = link.strength(self._now)
            samples += link.samples
            if weakest_strength is None or strength < weakest_strength:
                weakest_strength = strength
            snr = link.median_snr
            if snr is not None and (weakest_snr is None or snr < weakest_snr):
                weakest_snr = snr
        score = (weakest_strength or 0.0) / (1.0 + _HOP_PENALTY * len(hops))
        return score, weakest_snr, samples

    def _best_routes(self, target: str, *, k: int) -> list[tuple[str, ...]]:
        """Find up to ``k`` strong simple paths from our node to ``target``, lowest cost first.

        This is Yen's k-shortest-loopless-paths over the evidence graph. Each link costs
        ``1/strength + _HOP_PENALTY``, so a weak or indirect path costs more. The routes
        come back ranked by total cost, the lowest first (:meth:`scenarios` then scores
        this candidate set again by the weakest link). The method finds only simple
        (loopless) paths, because a node visited again is never useful. It also finds only
        routes with :data:`_MAX_SCENARIO_HOPS` intermediate hops or fewer. A longer chain
        is more airtime and more points of failure than is good for a trace (and the trace
        goes through it two times).

        Yen's algorithm uses plain Dijkstra. It does not enumerate simple paths with a
        best-first frontier, because that frontier is pathological on a real mesh graph. A
        well-heard core has low-cost links everywhere, so a search of partial paths spreads
        through all of the core. Each indirect prefix through the core costs less than the
        one weak link in front of a distant target. Thus the search explores millions of
        dead-end prefixes before it reaches the target (tens of seconds for a busy node).
        Yen's algorithm instead finds the single shortest path. Then it finds each
        next-shortest path when it routes around one edge of the previous path. Thus the
        work stays polynomial in the graph size, however dense the core is. The result is
        the same routes, in the same cost order (except for ties), in milliseconds.

        Args:
            target: The canonical id of the destination.
            k: The maximum number of routes to return.

        Returns:
            The intermediate-hop tuples of the routes that the method found (without the
            endpoints), the lowest total cost first. The result is cached for each graph
            version (refer to ``_routes_memo``). Callers must use the list as read-only,
            and they do (both consumers only iterate it).
        """
        memo_key = (target, k, self._graph_version)
        memoized = self._routes_memo.get(memo_key)
        if memoized is not None:
            return memoized
        neighbors: dict[str, list[tuple[str, float]]] = {}
        for (a, b), link in self._links.items():
            cost = 1.0 / max(link.strength(self._now), 1e-6) + _HOP_PENALTY
            neighbors.setdefault(a, []).append((b, cost))
            neighbors.setdefault(b, []).append((a, cost))

        def shortest(
            source: str, banned_nodes: set[str], banned_edges: set[tuple[str, str]]
        ) -> tuple[float, list[str]] | None:
            """Dijkstra ``source``→``target`` without the banned nodes and edges, or ``None``."""
            dist: dict[str, float] = {source: 0.0}
            prev: dict[str, str] = {}
            heap: list[tuple[float, str]] = [(0.0, source)]
            done: set[str] = set()
            while heap:
                d, u = heapq.heappop(heap)
                if u in done:
                    continue
                done.add(u)
                if u == target:
                    break
                for v, cost in neighbors.get(u, ()):
                    if v in banned_nodes or (u, v) in banned_edges:
                        continue
                    nd = d + cost
                    if nd < dist.get(v, math.inf):
                        dist[v] = nd
                        prev[v] = u
                        heapq.heappush(heap, (nd, v))
            if target not in dist:
                return None
            path = [target]
            while path[-1] != source:
                path.append(prev[path[-1]])
            path.reverse()
            return dist[target], path

        def path_cost(path: list[str]) -> float:
            total = 0.0
            for a, b in pairwise(path):
                total += min((c for v, c in neighbors.get(a, ()) if v == b), default=math.inf)
            return total

        first = shortest(self.self_id, set(), set())
        if first is None or len(first[1]) - 2 > _MAX_SCENARIO_HOPS:
            self._routes_memo[memo_key] = []
            return []
        accepted: list[list[str]] = [first[1]]
        # Spur candidates, in a min-heap by total cost. A counter breaks ties, so that the
        # heap never compares the path lists of candidates that have equal cost.
        candidates: list[tuple[float, int, list[str]]] = []
        seen: set[tuple[str, ...]] = {tuple(first[1])}
        counter = 0
        while len(accepted) < k:
            prev_path = accepted[-1]
            for i in range(len(prev_path) - 1):
                spur = prev_path[i]
                root = prev_path[: i + 1]
                # Ban the next edge that each accepted path with this same root took. Thus
                # the spur must take a really different route out of the spur node...
                banned_edges: set[tuple[str, str]] = set()
                for p in accepted:
                    if p[: i + 1] == root and len(p) > i + 1:
                        banned_edges.add((p[i], p[i + 1]))
                        banned_edges.add((p[i + 1], p[i]))
                # ...and ban the interior nodes of the root, so that the detour stays loopless.
                banned_nodes = set(root[:-1])
                spur_result = shortest(spur, banned_nodes, banned_edges)
                if spur_result is None:
                    continue
                full = root[:-1] + spur_result[1]
                if len(full) - 2 > _MAX_SCENARIO_HOPS:
                    continue
                key = tuple(full)
                if key in seen:
                    continue
                seen.add(key)
                counter += 1
                heapq.heappush(candidates, (path_cost(full), counter, full))
            if not candidates:
                break
            _, _, best_path = heapq.heappop(candidates)
            accepted.append(best_path)
        routes = [tuple(path[1:-1]) for path in accepted]
        self._routes_memo[memo_key] = routes
        return routes


def build_topology(
    *,
    self_id: str,
    contacts: list[Contact],
    trace_paths: list[TracedPath],
    packet_paths: list[PacketPath],
    neighbour_links: tuple[NeighbourLink, ...] | list[NeighbourLink] = (),
) -> MeshTopology:
    """Make the evidence graph from all that we received, or asked for.

    Four sources go into the graph. :meth:`MeshTopology.add_walk` walks each one:

    * **traces**: the hop sequence of each successful trace, with our node at the two
      ends. The boomerang leaves our node and comes back to it, and the last hop, which
      has no hash, is our node. The SNR of each hop is a reading on the link on which it
      arrived.
    * **contact routes**: the learned ``out_path`` of the firmware for each contact: a
      chain from our node through its repeaters to the contact. There is no SNR. But the
      route comes from real received floods, so it counts as one reading for each link
      (with the last-heard time of the contact). A learned direct route is a direct link
      to our node.
    * **packets**: packets in the RX log: the relay chain, with the originator first when
      known, and always with our node at the end (we heard the last relay). The measured
      SNR is for that last link only.
    * **neighbour reports**: links that a remote repeater reported about itself when we
      read its neighbour table: one two-node walk for each entry, with the SNR as the
      repeater measured it. This is the only evidence here that does not come from our
      own reception. It can add nodes that we never heard from another source.

    Args:
        self_id: The key or hash of our device (any width, canonicalized to 12 hex).
        contacts: The known contacts of the device (for canonicalization and route
            evidence).
        trace_paths: Stored successful trace walks (refer to ``Repository.trace_paths``).
        packet_paths: Stored packet paths from the RX log (refer to
            ``Repository.packet_paths``).
        neighbour_links: The current links that repeaters reported (refer to
            ``Repository.neighbour_links``). Empty when MeshTerm read no neighbour table.

    Returns:
        The populated :class:`MeshTopology`.
    """
    topo = MeshTopology(self_id, contacts)
    us = topo.self_id

    for traced in trace_paths:
        nodes: list[str | None] = [us]
        snrs: list[float | None] = []
        for hop, snr in traced.hops:
            # The last hop, which has no hash, is the reply that comes back to our node.
            nodes.append(topo.canonical(hop) if hop is not None else us)
            snrs.append(snr)
        topo.add_walk(nodes, snrs=snrs, when=traced.when, source="trace")

    for contact in contacts:
        if contact.route_hops is None:
            continue
        contact_id = topo.canonical(contact.public_key or contact.key_prefix)
        route = [topo.canonical(h) for h in contact.route_hops]
        topo.add_walk([us, *route, contact_id], when=contact.last_seen, source="route")

    for packet in packet_paths:
        nodes = []
        if packet.origin:
            nodes.append(topo.canonical(packet.origin))
        nodes.extend(topo.canonical(h) for h in packet.hops)
        nodes.append(us)
        if len(nodes) < 2:
            continue
        # Only the last link (last relay → us) has the measured reception SNR.
        snrs = [None] * (len(nodes) - 2) + [packet.snr]
        topo.add_walk(nodes, snrs=snrs, when=packet.when, source="packet")

    for reported in neighbour_links:
        topo.add_walk(
            [topo.canonical(reported.repeater), topo.canonical(reported.neighbour)],
            snrs=[reported.snr],
            when=reported.when,
            source="neighbour",
        )

    # All the sources are in the graph. Collapse each node that reached our node at two
    # hash widths (a bare trace hop next to its wide contact id), so that each node shows
    # one time everywhere.
    topo.coalesce_prefixes()
    return topo
