# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the shared route-graph widget.

The widget is the braille renderer for routes that diverge and converge.
"""

from __future__ import annotations

import re
from itertools import pairwise

import pytest

from meshterm.ui.pathgraph import (
    _GRAPH_PAD_DOTS,
    _LANE_PITCH_ROWS,
    _OCCURRENCE_SEP,
    DST_NODE,
    SRC_NODE,
    PathLayer,
    _assign_lanes,
    _balanced_x,
    _base_node,
    _bypass_vias,
    _coalesce_prefixes,
    _collapse,
    _layout_lanes,
    _route,
    _split_revisits,
    render_path_graph,
    revisited_hops,
)
from meshterm.ui.pathgraph import (
    _OFF_ROUTE as OFF_ROUTE,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

WHITE = (255, 255, 255)
GREEN = (74, 222, 128)
YELLOW = (250, 204, 21)
GREY = (120, 120, 120)


def _glyph(node: str) -> tuple[str, str]:
    # The marker colours are deliberately not in the palette. Thus a colour assertion about
    # edges can never match the escape sequence of a marker by accident.
    if node in (SRC_NODE, DST_NODE):
        return ("★", "#123456")
    return ("●", "#654321")


def _render(layers, label_of=None):  # noqa: ANN001
    return render_path_graph(
        layers,
        60,
        glyph_of=_glyph,
        label_of=label_of or (lambda node: "you" if node in (SRC_NODE, DST_NODE) else node[:2]),
        label_rgb_of=lambda _node: WHITE,
    )


def _sgr(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f"38;2;{r};{g};{b}m"


def test_no_layers_render_nothing() -> None:
    """If there is nothing to draw, the renderer returns no rows, not a blank canvas."""
    assert (
        render_path_graph(
            [], 60, glyph_of=_glyph, label_of=lambda n: None, label_rgb_of=lambda n: WHITE
        )
        == []
    )


def test_identical_paths_collapse_to_the_top_layer() -> None:
    """The same route in two layers draws one time, in the colour of the higher priority."""
    layers = [
        PathLayer(("aa", "bb"), YELLOW, 2),
        PathLayer(("aa", "bb"), WHITE, 4),
    ]
    joined = "\n".join(_render(layers))
    assert _sgr(WHITE) in joined
    assert _sgr(YELLOW) not in joined


def test_emphasis_moves_the_highlight_not_the_layout() -> None:
    """Emphasis moves the highlight, never the layout.

    Emphasis changes the colours of the drawn fan on a fixed geometry. The layout is the
    same and only the colours differ. Thus a caller can light another route and the layout
    does not change.
    """

    # A fan of two routes. The spine (priority) is fixed to route 0. Only the emphasis is
    # different between the two renders. If emphasis changed the geometry (as a higher
    # priority does), route 1 would take the centre lane when it becomes the highlight,
    # and the markers would move.
    def fan(emph: int):
        return [
            PathLayer(("aa",), WHITE if emph == 0 else GREY, 2, emphasis=1 if emph == 0 else 0),
            PathLayer(("bb",), WHITE if emph == 1 else GREY, 1, emphasis=1 if emph == 1 else 0),
        ]

    lit_spine = _render(fan(0))
    lit_alt = _render(fan(1))
    # The geometry is the same. If you remove the colour, the two renders are the same picture.
    assert _ANSI.sub("", "\n".join(lit_spine)) == _ANSI.sub("", "\n".join(lit_alt))
    # But the colouring changed: each render still lights one route white, and the renders
    # are different.
    assert _sgr(WHITE) in "\n".join(lit_spine) and _sgr(WHITE) in "\n".join(lit_alt)
    assert lit_spine != lit_alt


def test_bidir_clusters_finds_knots_not_pairs() -> None:
    """Bidirectional clustering finds knots, not pairs.

    Three or more nodes that are each bidirectional with the others are a cluster to
    contract. A tidy two-way pair is not a cluster, and a lone node is not a cluster.
    """
    from meshterm.ui.pathgraph import bidir_clusters

    # A single two-way pair draws correctly by itself. It is not a cluster.
    pair = [(SRC_NODE, "aa", "bb", DST_NODE), (SRC_NODE, "bb", "aa", DST_NODE)]
    assert bidir_clusters(pair) == []
    # aa<->bb and bb<->cc in both directions make {aa, bb, cc} one strongly connected knot.
    knot = [(SRC_NODE, "aa", "bb", "cc", DST_NODE), (SRC_NODE, "cc", "bb", "aa", DST_NODE)]
    groups = bidir_clusters(knot)
    assert len(groups) == 1 and set(groups[0]) == {"aa", "bb", "cc"}
    # An endpoint is never a member of a cluster.
    assert SRC_NODE not in groups[0] and DST_NODE not in groups[0]


def test_emphasis_wins_a_shared_edge_over_a_higher_priority_spine() -> None:
    """On a shared edge, emphasis beats a higher-priority spine.

    A highlighted alternative paints its whole run, with the edge that it shares. It does
    not stop at the edge where the spine would own the colour.
    """
    # Both routes leave SRC through the same first relay (aa), so they share the SRC-aa edge.
    # The spine (priority 2, grey) would win that shared edge by priority. But the emphasised
    # alternative (priority 1, white) must win it by draw rank, so that its highlight has no
    # break.
    layers = [
        PathLayer(("aa", "bb"), GREY, 2),
        PathLayer(("aa", "cc"), WHITE, 1, emphasis=1),
    ]
    joined = "\n".join(_render(layers))
    assert _sgr(WHITE) in joined  # the emphasised route drew, with the shared edge


def test_distinct_layers_draw_in_their_own_colours() -> None:
    """Three different routes keep their three edge colours on one canvas."""
    layers = [
        PathLayer(("aa", "bb"), YELLOW, 2),
        PathLayer(("cc",), GREEN, 3),
        PathLayer(("dd", "ee", "ff"), WHITE, 4),
    ]
    joined = "\n".join(_render(layers))
    for rgb in (YELLOW, GREEN, WHITE):
        assert _sgr(rgb) in joined


def test_labels_follow_the_callback_only_self_named() -> None:
    """A ``None`` label leaves the marker bare. A caller uses this for a relay with no name."""
    layers = [PathLayer(("ab", "cd"), WHITE, 4)]
    lines = _render(
        layers,
        label_of=lambda node: "Base" if node in (SRC_NODE, DST_NODE) else None,
    )
    plain = _ANSI.sub("", "\n".join(lines))
    assert "Base" in plain
    assert "ab" not in plain and "cd" not in plain


def test_endpoint_labels_may_leave_the_marker_row() -> None:
    """An endpoint label is placed like a relay label.

    A long name stays whole, also when it is not on the centre row.
    """
    layers = [PathLayer(tuple(f"{i:02x}" for i in range(6)), WHITE, 4)]
    lines = _render(
        layers,
        label_of=lambda node: "VeryLongStationName" if node in (SRC_NODE, DST_NODE) else None,
    )
    plain = _ANSI.sub("", "\n".join(lines))
    # The name fits the canvas of 60 cells, so it stays whole. No fixed label budget cuts it.
    assert "VeryLongStationName" in plain
    assert "…" not in plain


def test_revisited_hops_names_only_within_path_repeats() -> None:
    """The detector reports a hop that one path touches two times.

    The hops are in the order of first appearance.
    """
    assert revisited_hops(("7f", "c1", "8e", "da", "ee", "c1", "27")) == ("c1",)
    assert revisited_hops(("aa", "bb", "aa", "cc", "bb")) == ("aa", "bb")
    assert revisited_hops(("aa", "bb", "cc")) == ()
    assert revisited_hops(()) == ()


def test_revisited_path_folds_into_a_cycle_that_piles_its_relays() -> None:
    """A folded revisit makes a cycle that the balanced rank cannot settle.

    This test shows why the flag exists.

    ``c1`` two times closes a loop over ``8e``/``da``/``ee``. The longest-path relaxation
    ranks that cyclic edge set and never reaches a fixed point. Thus the members of the loop
    are almost at the same place (one column of piled markers), and the relays outside the
    loop are pressed against the two ends. This test pins the broken behaviour that the flag
    avoids.

    The thresholds are loose because the folded numbers are not stable. The relaxation
    iterates an edge **set**. On an acyclic graph, it reaches the same fixed point in any
    order. On this cycle, it stops where the sweep budget ended. Thus the column of the pile
    changes with the hash seed of the interpreter. This is the jump from paint to paint that
    the node order of first appearance exists to prevent. Each seed agrees on the shape that
    the test asserts. If the revisit is split, the fixed point returns (refer to
    :func:`test_allow_duplicate_nodes_keeps_x_strictly_rising_along_the_walk`).
    """
    hops = ("7f", "c1", "8e", "da", "ee", "c1", "27")
    seq = (SRC_NODE, *hops, DST_NODE)
    ordered = list(dict.fromkeys(seq))
    x = _balanced_x(ordered, set(pairwise(seq)))
    # If the eight nodes were evenly spread, they would be 1/7 apart, and the four members of
    # the loop would span three of those gaps. When the revisit is folded, the whole loop
    # fits in one gap.
    loop = [x[node] for node in ("c1", "8e", "da", "ee")]
    assert max(loop) - min(loop) < 1 / 7
    assert x["7f"] < 0.15 and x["27"] > 0.85  # its neighbours are pressed against the ends


def test_allow_duplicate_nodes_keeps_x_strictly_rising_along_the_walk() -> None:
    """If the revisit is split, the rank gets the fixed point again.

    The ordering invariant of the widget also returns.

    ``_balanced_x`` promises that the fraction rises strictly along each path, so edges
    always run from left to right. A folded revisit breaks this promise. The cycle stops the
    relaxation before it converges, and it can give a node a lower rank than the hop before
    it. A split restores the promise. It also makes the layout the same from paint to paint.
    """
    hops = ("7f", "c1", "8e", "da", "ee", "c1", "27")
    [layer] = _split_revisits([PathLayer(hops, WHITE, 3)])
    seq = (SRC_NODE, *layer.hops, DST_NODE)
    ordered = list(dict.fromkeys(seq))
    x = _balanced_x(ordered, set(pairwise(seq)))
    walked = [x[node] for node in seq]
    assert walked == sorted(walked) and len(set(walked)) == len(walked)
    assert x[SRC_NODE] == 0.0 and x[DST_NODE] == 1.0
    # A single walk with nothing else to balance against spreads exactly evenly from end to end.
    gaps = [b - a for a, b in pairwise(walked)]
    assert max(gaps) - min(gaps) < 1e-9


def test_allow_duplicate_nodes_spreads_a_revisited_path_evenly() -> None:
    """If the revisit is split, the same walk is an acyclic run again.

    Each hop has its own column, and the columns are evenly spaced.
    """
    hops = ("7f", "c1", "8e", "da", "ee", "c1", "27")
    layers = [PathLayer(hops, WHITE, 3)]
    plain = _ANSI.sub("", "\n".join(_render(layers)))
    split = _ANSI.sub(
        "",
        "\n".join(
            render_path_graph(
                layers,
                60,
                glyph_of=_glyph,
                label_of=lambda node: "you" if node in (SRC_NODE, DST_NODE) else node[:2],
                label_rgb_of=lambda _node: WHITE,
                allow_duplicate_nodes=True,
            )
        ),
    )
    # Both visits of c1 draw their own marker, with the same label: the node, two times.
    assert split.count("c1") == 2
    # Each other hop keeps exactly one marker. Only the real revisit split.
    for hop in ("7f", "8e", "da", "ee", "27"):
        assert split.count(hop) == 1
    # When the revisit is folded, the second marker does not exist. The pile is also so tight
    # that the label placer sometimes cannot seat the first marker. Thus the test checks for
    # at most one marker, not for exactly one.
    assert plain.count("c1") <= 1


def test_allow_duplicate_nodes_still_merges_a_relay_two_paths_share() -> None:
    """A split is only inside one path. The diverge and converge behaviour does not change.

    Two routes that use one relay are the merge for which the widget exists. The flag must
    not break this merge. ``zz`` stays one marker, and both lanes go through it.
    """
    layers = [PathLayer(("aa", "zz"), WHITE, 4), PathLayer(("bb", "zz"), GREY, 2)]
    plain = _ANSI.sub(
        "",
        "\n".join(
            render_path_graph(
                layers,
                60,
                glyph_of=_glyph,
                label_of=lambda node: node[:2],
                label_rgb_of=lambda _node: WHITE,
                allow_duplicate_nodes=True,
            )
        ),
    )
    assert plain.count("zz") == 1


def test_split_revisits_qualifies_only_the_later_visits() -> None:
    """The first visit keeps the bare id of the caller. Later visits get a private qualifier."""
    [layer] = _split_revisits([PathLayer(("aa", "bb", "aa", "bb", "aa"), WHITE, 3)])
    assert layer.hops == (
        "aa",
        "bb",
        f"aa{_OCCURRENCE_SEP}1",
        f"bb{_OCCURRENCE_SEP}1",
        f"aa{_OCCURRENCE_SEP}2",
    )
    assert layer.color == WHITE and layer.priority == 3
    # Each qualified id maps back to the node that the caller named.
    assert [_base_node(hop) for hop in layer.hops] == ["aa", "bb", "aa", "bb", "aa"]


def test_prefix_dupe_relay_folds_onto_its_only_wide_match() -> None:
    """A 1-byte hop and the wide id of the same node, in different layers, become one node."""
    layers = [
        PathLayer(("e9043958308d", "be"), WHITE, 3),
        PathLayer(("3d63c6429436", "be1d1c1dbc4b"), GREY, 2),
    ]
    out = _coalesce_prefixes(layers)
    assert out[0].hops == ("e9043958308d", "be1d1c1dbc4b")  # "be" is rewritten to the wide id
    assert out[1].hops == ("3d63c6429436", "be1d1c1dbc4b")


def test_ambiguous_prefix_relay_is_left_alone() -> None:
    """A short hop that is the prefix of two different nodes is not assigned to either node."""
    layers = [
        PathLayer(("be",), WHITE, 3),
        PathLayer(("be1d1c1dbc4b",), GREY, 2),
        PathLayer(("bedd2bff0011",), GREY, 2),
    ]
    out = _coalesce_prefixes(layers)
    assert out[0].hops == ("be",)  # it fits two nodes, so it stays a short hop


def test_a_shared_relay_draws_a_single_marker() -> None:
    """A relay that two paths go through is one vertex. It has one label, never two."""
    layers = [PathLayer(("aa", "bb"), WHITE, 3), PathLayer(("cc", "bb"), YELLOW, 2)]
    plain = _ANSI.sub("", "\n".join(_render(layers)))
    assert plain.count("bb") == 1  # the shared relay is seated one time
    assert plain.count("aa") == 1 and plain.count("cc") == 1


def _marker_rows(lines, glyph):  # noqa: ANN001, ANN202
    """The canvas rows on which a marker glyph is drawn (endpoints ``★``, relays ``●``)."""
    plain = _ANSI.sub("", "\n".join(lines)).splitlines()
    return sorted({i for i, ln in enumerate(plain) if glyph in ln})


def test_even_lane_count_centres_the_endpoints() -> None:
    """If the number of lanes is even, the endpoints are exactly midway between them.

    The endpoints are the origin and the destination.

    Two separate routes with one relay each make two lanes: an upper lane and a lower lane.
    The endpoints are on neither lane. The layout opens an extra padding row between the two
    central lanes, so that the endpoints are on the exact centre row. They are not fixed to
    the upper lane (where the discrete lane of the best path would put them), and they are
    not one row away from the centre.
    """
    layers = [
        PathLayer(("aa",), WHITE, 4),  # best -> upper lane
        PathLayer(("bb",), GREY, 2),  # -> lower lane
    ]
    lines = _render(layers, label_of=lambda _n: None)  # markers only, so no label rows interfere
    (star,) = _marker_rows(lines, "★")  # both endpoints share the one centre row
    top, bottom = _marker_rows(lines, "●")  # the two relays are on their own lanes
    assert top < star < bottom  # centred between the lanes, not fixed to either
    assert star - top == bottom - star  # exact integer centring, because of the extra pad row


def test_odd_lane_count_seats_endpoints_on_the_straight_middle_lane() -> None:
    """If the number of lanes is odd, the endpoints have a real middle lane.

    The spine is straight.

    Three separate routes with one relay each make three lanes with equal spacing. The relay
    of the best path owns the centre lane. The origin and the destination share that centre
    row (no bend, no extra pad row), and the row is midway between the two outer lanes.
    """
    layers = [
        PathLayer(("mm",), WHITE, 4),  # best -> centre lane
        PathLayer(("aa",), GREY, 2),  # -> a flanking lane
        PathLayer(("bb",), GREY, 1),  # -> the other flanking lane
    ]
    lines = _render(layers, label_of=lambda _n: None)
    (star,) = _marker_rows(lines, "★")
    top, mid, bottom = _marker_rows(lines, "●")  # three relays, one per lane
    assert star == mid  # the endpoints are on the centre lane, the same row as the best relay
    assert mid - top == bottom - mid  # equal lane spacing: no extra gap for an odd count


def test_every_label_shows_on_a_busy_graph() -> None:
    """Four different routes make separate lanes. Each relay label is placed, none is lost."""
    names = {
        "aa": "Alpha",
        "bb": "Bravo",
        "cc": "Charlie",
        "dd": "Delta",
        "ee": "Echo",
        "ff": "Foxtrot",
        "gg": "Golf",
    }
    layers = [
        PathLayer(("aa", "bb"), WHITE, 4),
        PathLayer(("cc", "dd"), GREY, 2),
        PathLayer(("ee", "ff"), GREY, 2),
        PathLayer(("gg",), GREY, 2),
    ]
    plain = _ANSI.sub("", "\n".join(_render(layers, label_of=lambda n: names.get(n[:2]))))
    for name in names.values():
        assert name in plain, f"{name} was dropped from the graph"


def _packed_lanes(layers, width=60, max_rows=15):  # noqa: ANN001
    """Run the lane pipeline through layout and compression, as ``render_path_graph`` does.

    Returns ``(signed, columns, route_lanes)``. These are the packed signed lane of each
    relay (``0`` on the best spine, ``<0`` above, ``>0`` below), the cell column of each
    relay, and the number of lanes that the assignment for each path used before the
    packing. Thus a test can assert that the packing made the lanes fewer. ``max_rows`` is
    the budget against which the layout weighs detours. The default is large, the same as
    the default of the renderer. Pass a small value to force the fold fallback.
    """
    drawn = _collapse(_coalesce_prefixes(layers))
    seqs = [(SRC_NODE, *layer.hops, DST_NODE) for layer in drawn]
    ordered: list[str] = []
    seen: set[str] = set()
    edges: set[tuple[str, str]] = set()
    for seq in seqs:
        for node in seq:
            if node not in seen:
                seen.add(node)
                ordered.append(node)
        edges.update(pairwise(seq))
    xfrac = _balanced_x(ordered, edges)
    best = max(range(len(drawn)), key=lambda j: drawn[j].priority)
    owner: dict[str, int] = {}
    for i in sorted(range(len(drawn)), key=lambda j: -drawn[j].priority):
        for node in seqs[i]:
            owner.setdefault(node, i)
    route_lanes = max(_assign_lanes(drawn, seqs, owner, best).values()) + 1
    span = width * 2 - 2 * _GRAPH_PAD_DOTS
    col_of = lambda node: (_GRAPH_PAD_DOTS + round(xfrac[node] * span)) >> 1  # noqa: E731
    signed = _layout_lanes(drawn, seqs, ordered, owner, best, col_of, max_rows, _LANE_PITCH_ROWS)
    columns = {node: col_of(node) for node in signed}
    return signed, columns, route_lanes


def _lane_count(signed) -> int:  # noqa: ANN001
    """The number of different rows that the packed lanes span (with the spine)."""
    return max(signed.values()) - min(signed.values()) + 1


def test_detour_nests_outside_its_sibling_when_rows_allow() -> None:
    """A route that is a sibling with one more relay makes an arc outside the lane of the sibling.

    ``cc`` adds a relay before ``bb``, and its sibling also converges through ``bb``. If
    there are rows to spare, the detour nests. ``cc`` takes the lane just outside the lane of
    ``bb`` on the same flank. Thus the straight run SRC → bb of the sibling does not go over
    the marker of ``cc``, and the detour shows as the wider arc through ``cc`` that it is.
    This is the real Lakeside shape: CDN-FENDALL1 → UpperSalaberry.
    """
    layers = [
        PathLayer(("xx",), WHITE, 3),  # best spine: SRC -> xx -> DST
        PathLayer(("bb",), GREY, 2),  # sibling:    SRC -> bb -> DST
        PathLayer(("cc", "bb"), GREY, 2),  # detour:     SRC -> cc -> bb -> DST
    ]
    signed, _columns, _route_lanes = _packed_lanes(layers)
    assert signed["xx"] == 0  # the best route holds the spine
    assert abs(signed["bb"]) == 1  # the sibling takes the first flank row
    assert signed["cc"] == 2 * signed["bb"]  # cc one lane outside bb, same flank
    assert _lane_count(signed) == 3


def test_detour_folds_onto_its_siblings_lane_when_rows_are_tight() -> None:
    """The layout gives up the nested lane only when the rows are not enough for it.

    Here ``cc`` uses the lane of ``bb``. The same Lakeside shape, with a row budget that is
    too short for the nested third lane, falls back to the picture with one lane. ``cc``
    changes to the lane of ``bb``, as a waypoint through which the branch dips. The band
    stays two lanes tall. This is the last resort, not the default.
    """
    layers = [
        PathLayer(("xx",), WHITE, 3),
        PathLayer(("bb",), GREY, 2),
        PathLayer(("cc", "bb"), GREY, 2),
    ]
    signed, _columns, _route_lanes = _packed_lanes(layers, max_rows=8)
    assert signed["cc"] == signed["bb"]  # folded: cc is on the lane of bb
    assert _lane_count(signed) == 2


def test_disjoint_routes_are_never_folded() -> None:
    """Routes that share no convergence relay each keep their own lane. A fold is targeted."""
    layers = [
        PathLayer(("xx",), WHITE, 3),
        PathLayer(("aa",), GREY, 2),
        PathLayer(("bb",), GREY, 2),
    ]
    signed, _columns, _route_lanes = _packed_lanes(layers, max_rows=8)
    assert _lane_count(signed) == 3  # also at a tight budget, separate routes do not merge
    assert signed["aa"] == -signed["bb"]  # one flank each, balanced around the spine


def test_two_detours_sharing_a_column_nest_at_distinct_depths() -> None:
    """Two siblings that add a relay at the same column stack outward, and never use one cell.

    ``cc`` and ``dd`` are each one hop from the origin and feed the same ``bb``, so they
    share a column. If there is room to spare, they nest in different rows outside the lane
    of ``bb`` on its flank. Markers never overprint.
    """
    layers = [
        PathLayer(("xx",), WHITE, 3),
        PathLayer(("bb",), GREY, 2),
        PathLayer(("cc", "bb"), GREY, 2),
        PathLayer(("dd", "bb"), GREY, 1),
    ]
    signed, columns, _route_lanes = _packed_lanes(layers)
    assert columns["cc"] == columns["dd"]  # the two added relays do share a column
    assert signed["cc"] != signed["dd"]  # so they hold different rows, with no overprint
    for n in ("cc", "dd"):
        assert signed[n] * signed["bb"] > 0 and abs(signed[n]) > abs(signed["bb"])


def test_two_detours_sharing_a_column_do_not_overprint_when_folded() -> None:
    """With a tight budget, the fold still does not stack two markers in one cell.

    If both ``cc`` and ``dd`` fold onto the lane of ``bb``, they overprint their shared
    column. Thus a maximum of one folds. The other keeps a row of its own, and the two stay on
    different lanes.
    """
    layers = [
        PathLayer(("xx",), WHITE, 3),
        PathLayer(("bb",), GREY, 2),
        PathLayer(("cc", "bb"), GREY, 2),
        PathLayer(("dd", "bb"), GREY, 1),
    ]
    signed, columns, _route_lanes = _packed_lanes(layers, max_rows=8)
    assert columns["cc"] == columns["dd"]
    assert signed["cc"] != signed["dd"]  # never stacked into one cell, with any budget
    assert _lane_count(signed) == 3


def test_routes_pack_onto_shared_flanks_when_their_columns_differ() -> None:
    """Routes that diverge at different columns share a flank lane, so the band packs tight.

    There are four routes: a best spine with three relays, and three alternatives. Each
    alternative swaps a different relay of the spine (a different column). If each route has
    a lane of full width, the picture is four lanes deep. But at no column are there more
    than two nodes. Thus the packing puts the routes on the spine and one row above and one
    row below: three lanes, not four. This is the SUTTON-680M shape (five routes, a maximum
    of two nodes in each column) drawn small.
    """
    layers = [
        PathLayer(("aa", "bb", "cc"), WHITE, 4),  # spine
        PathLayer(("xx", "bb", "cc"), GREY, 2),  # swaps the first relay  (col of aa)
        PathLayer(("aa", "yy", "cc"), GREY, 2),  # swaps the middle relay (col of bb)
        PathLayer(("aa", "bb", "zz"), GREY, 2),  # swaps the last relay   (col of cc)
    ]
    signed, columns, route_lanes = _packed_lanes(layers)
    assert route_lanes == 4  # one lane for each route before the packing
    assert _lane_count(signed) == 3  # packed to the spine and one flank on each side
    assert signed["aa"] == signed["bb"] == signed["cc"] == 0  # best is on the straight spine
    assert all(signed[n] != 0 for n in ("xx", "yy", "zz"))  # alternatives leave the spine
    # Two alternatives on one flank must be in different columns (no overprint).
    for lane in {signed["xx"], signed["yy"], signed["zz"]}:
        on_lane = [n for n in ("xx", "yy", "zz") if signed[n] == lane]
        assert len({columns[n] for n in on_lane}) == len(on_lane)


def test_routes_stacking_in_one_column_keep_separate_lanes() -> None:
    """The packing does not put routes that share a column on one row.

    Three alternatives all diverge through the same column after the origin, so three nodes
    are stacked there. The packing cannot reduce this, because the column needs a different
    row for each node. Thus the spine and the three stacked alternatives keep their own
    lanes. This is the real minimum.
    """
    layers = [
        PathLayer(("aa", "zz"), WHITE, 4),  # spine relay aa, then shared zz near us
        PathLayer(("bb", "zz"), GREY, 3),  # bb shares the column of aa
        PathLayer(("cc", "zz"), GREY, 2),  # cc shares the column of aa
        PathLayer(("dd", "zz"), GREY, 1),  # dd shares the column of aa
    ]
    signed, columns, _route_lanes = _packed_lanes(layers)
    diverging = ("aa", "bb", "cc", "dd")
    assert len({columns[n] for n in diverging}) == 1  # they are all stacked in one column
    assert len({signed[n] for n in diverging}) == 4  # so each keeps a row of its own


def test_balancing_splits_same_column_pairs_across_both_flanks() -> None:
    """Two routes that share a column split above and below, so their flank is not deep.

    The best spine goes through two relays, ``c1`` and ``c2``. Two alternatives swap ``c1``
    for a relay of their own (``x1`` and ``y1``, both in the column of ``c1``). Two more
    swap ``c2`` (``x2`` and ``y2``, both in the column of ``c2``). If only the jog order
    seats them, each pair is on the same flank. One column is two deep above the spine, and
    the other column is two deep below it. This is a band of five lanes. If the flanks are
    balanced, each pair splits across the spine. Thus each column has one node above and one
    node below, and the band is the spine and one row on each side: three lanes. This is the
    7bc505 shape drawn tight. The pack with only a straight spine did not give this gain.
    """
    layers = [
        PathLayer(("c1", "c2"), WHITE, 4),  # spine: SRC -> c1 -> c2 -> us
        PathLayer(("x1", "c2"), GREY, 2),  # swaps c1 -> x1 (x1 in c1's column)
        PathLayer(("y1", "c2"), GREY, 2),  # swaps c1 -> y1 (shares x1's column)
        PathLayer(("c1", "x2"), GREY, 2),  # swaps c2 -> x2 (x2 in c2's column)
        PathLayer(("c1", "y2"), GREY, 2),  # swaps c2 -> y2 (shares x2's column)
    ]
    signed, columns, route_lanes = _packed_lanes(layers)
    assert route_lanes == 5  # five lanes of full width before the packing
    assert _lane_count(signed) == 3  # balanced to the spine and one flank on each side
    assert signed["c1"] == signed["c2"] == 0  # spine relays stay on the straight centre
    for a, b in (("x1", "y1"), ("x2", "y2")):
        assert columns[a] == columns[b]  # each pair does share a column
        assert signed[a] == -signed[b]  # so the balancer seats one above and one below


def test_balancing_never_draws_a_taller_band_than_the_jog_order() -> None:
    """A column that has three stacked nodes does not get more height for symmetry.

    ``ee``, ``ff``, and ``gg`` are all in one column (three parallel detours from ``bf``).
    Thus one flank is two deep, with any colouring. If the opposite flank is filled to match
    it, the picture is centred but needs one more row. The balancer does not do this. It
    never draws a band that is taller than the band that the jog order gives. Thus the stack
    of three packs as two up and one down (four lanes), not two up and two down (five). This
    is the c5ba shape. The ceiling protects it.
    """
    layers = [
        PathLayer(("bf", "tt"), WHITE, 4),  # spine: SRC -> bf -> tt -> us
        PathLayer(("bf", "ee", "dd"), GREY, 2),  # ee in the mid column, on to dd
        PathLayer(("bf", "ee", "ww"), GREY, 2),  # reuses ee, on to ww
        PathLayer(("bf", "ff", "dd"), GREY, 2),  # ff shares ee's column, rejoins dd
        PathLayer(("bf", "gg", "ww"), GREY, 2),  # gg shares ee's column, rejoins ww
    ]
    signed, columns, _route_lanes = _packed_lanes(layers)
    mid = ("ee", "ff", "gg")
    assert len({columns[n] for n in mid}) == 1  # the three detours do stack in one column
    assert len({signed[n] for n in mid}) == 3  # each holds a different row, the real minimum
    assert _lane_count(signed) == 4  # spine, two up and one down, not extended to a symmetric five


def test_edge_pinned_relay_labels_are_not_dropped() -> None:
    """A relay that the balanced rank puts near the edge of the canvas still shows its whole name.

    A long chain on ``cc`` pushes its balanced rank hard against the origin. It also pushes
    the shared ``bb`` hard against us. Thus both are near an edge, where a centred long label
    goes over the edge of the canvas. These anchors must slide inward, as the anchor of an
    endpoint does. They must not place nothing and leave the marker with no label and no
    warning.
    """
    names = {"aa": "UpperStation", "bb": "RightEdgeStation", "cc": "LeftEdgeStation"}
    tail = tuple(f"g{i}" for i in range(1, 13))
    layers = [
        PathLayer(("bb",), WHITE, 3),
        PathLayer(("aa", "bb"), GREY, 2),
        PathLayer(("cc", *tail, "bb"), GREY, 1),
    ]
    plain = _ANSI.sub("", "\n".join(_render(layers, label_of=lambda n: names.get(n))))
    for name in names.values():
        assert name in plain, f"{name} was dropped near a canvas edge"


def _vias_for(layers, width=60, max_rows=15):  # noqa: ANN001
    """Run the pipeline before the render through the bypass pass, as ``render_path_graph`` does.

    Returns ``(vias, signed)``. ``vias`` is the bypass via map, with an unordered edge pair as
    the key. Each entry is ``[(skipped node, via signed lane), …]``. ``signed`` is the packed
    signed lane of each relay. Thus a test can assert where a via is, in relation to the
    seated nodes.
    """
    drawn = _collapse(_coalesce_prefixes(layers))
    seqs = [(SRC_NODE, *layer.hops, DST_NODE) for layer in drawn]
    ordered: list[str] = []
    seen: set[str] = set()
    edges: set[tuple[str, str]] = set()
    for seq in seqs:
        for node in seq:
            if node not in seen:
                seen.add(node)
                ordered.append(node)
        edges.update(pairwise(seq))
    xfrac = _balanced_x(ordered, edges)
    best = max(range(len(drawn)), key=lambda j: drawn[j].priority)
    owner: dict[str, int] = {}
    for i in sorted(range(len(drawn)), key=lambda j: -drawn[j].priority):
        for node in seqs[i]:
            owner.setdefault(node, i)
    span = width * 2 - 2 * _GRAPH_PAD_DOTS
    col_of = lambda node: (_GRAPH_PAD_DOTS + round(xfrac[node] * span)) >> 1  # noqa: E731
    signed = _layout_lanes(drawn, seqs, ordered, owner, best, col_of, max_rows, _LANE_PITCH_ROWS)
    bidir = {frozenset((u, v)) for (u, v) in edges if (v, u) in edges}
    vias = _bypass_vias(seqs, bidir, signed, col_of, max_rows, _LANE_PITCH_ROWS)
    return vias, signed


def test_subset_route_bypasses_the_relay_it_skips() -> None:
    """For routes ABCD and ACD, the skip edge arcs around the relay that it skips.

    The arc goes through a free flank lane.

    The spine goes SRC → c1 → c2 → us, with a balanced flank on each side. Thus the endpoints
    are on the lane of the spine, and the edge SRC → c2 of the subset route is a level run
    straight through the marker of ``c1``. This is the Furthur → Hilltop-Repeater shape. There
    the selected route lit the run of the spine through C14903, which the route never uses.
    The edge must bend around ``c1`` through a virtual waypoint in its column. It must use the
    flank that ``x1`` does not occupy, because ``x1`` shares the column of ``c1`` and its
    lane there is taken.
    """
    layers = [
        PathLayer(("c1", "c2"), WHITE, 4),  # spine: SRC -> c1 -> c2 -> us
        PathLayer(("x1", "c2"), GREY, 3),  # swaps c1 (x1 shares the column of c1, one flank)
        PathLayer(("c1", "y2"), GREY, 2),  # swaps c2 (the other flank, so the spine stays centred)
        PathLayer(("c2",), GREY, 1),  # the subset: skips c1, uses c2
    ]
    vias, signed = _vias_for(layers)
    [(skipped, lane)] = vias[frozenset((SRC_NODE, "c2"))]
    assert skipped == "c1"  # the bypass protects only the relay that the route skips
    assert lane == -signed["x1"]  # x1 holds one flank there, so the via takes the other


def test_bypass_opens_a_lane_when_the_rows_afford_it() -> None:
    """If there is no flank to use, the bypass opens one. The band grows and the arc uses it."""
    layers = [
        PathLayer(("aa", "bb", "cc"), WHITE, 4),  # the whole graph on one straight lane
        PathLayer(("aa", "cc"), GREY, 2),  # the shortcut that skips bb
    ]
    vias, _signed = _vias_for(layers)
    [(skipped, lane)] = vias[frozenset(("aa", "cc"))]
    assert skipped == "bb"
    assert lane == -1  # a new lane next to the spine, above it when the choice is equal
    # The render does open it: the endpoints leave the row of the relays for the band centre.
    lines = _render(layers, label_of=lambda _n: None)
    (star,) = _marker_rows(lines, "★")
    (relay_row,) = _marker_rows(lines, "●")  # all three relays are still level on the spine
    assert star < relay_row  # the endpoints are between the opened bypass lane and the spine


def test_bypass_gives_up_when_the_rows_cannot_afford_a_lane() -> None:
    """If there is no free lane and no spare height, the level pass-over stays.

    The band does not grow.
    """
    layers = [
        PathLayer(("aa", "bb", "cc"), WHITE, 4),
        PathLayer(("aa", "cc"), GREY, 2),
    ]
    vias, _signed = _vias_for(layers, max_rows=5)  # too short for a second lane at full pitch
    assert vias == {}


def test_route_threads_bypass_vias_level_at_each_peak() -> None:
    """A bent edge leaves level, is level at its peak on its via, and arrives level.

    It has no corners.
    """
    pts = _route("u", "v", {"u": (0, 9), "v": (80, 9)}, bidir=False, vias=[(40, 1)])
    assert pts[0] == (0, 9) and pts[-1] == (80, 9)  # the markers are still the anchors of the ends
    assert (40, 1) in pts  # the via is an anchor that the edge does go through
    near_peak = [y for x, y in pts if 36 <= x <= 44]
    assert near_peak and all(y <= 2.0 for y in near_peak)  # level over the skipped marker
    assert pts[0][1] == pts[1][1] and pts[-1][1] == pts[-2][1]  # level at both ends


def test_bypass_render_keeps_every_label() -> None:
    """The arcs that a bypass adds never push a name off the canvas."""
    names = {"c1": "C14903", "c2": "Cartier", "x1": "Poly", "y2": "Upper"}
    layers = [
        PathLayer(("c1", "c2"), GREY, 4),
        PathLayer(("x1", "c2"), GREY, 3),
        PathLayer(("c1", "y2"), GREY, 2),
        PathLayer(("c2",), WHITE, 1, emphasis=1),  # the subset is selected, as on the node page
    ]
    plain = _ANSI.sub("", "\n".join(_render(layers, label_of=lambda n: names.get(n))))
    for name in names.values():
        assert name in plain, f"{name} was dropped from the graph"


def test_a_subsumed_route_adds_no_duplicate_markers() -> None:
    """A route with only hops that a stronger route has draws through them, with no new marker."""
    layers = [
        PathLayer(("aa", "bb", "cc"), WHITE, 4),  # the backbone owns aa, bb, cc
        PathLayer(("aa", "cc"), GREY, 2),  # a shortcut over the same relays, with no new node
    ]
    plain = _ANSI.sub("", "\n".join(_render(layers)))
    for hop in ("aa", "bb", "cc"):
        assert plain.count(hop) == 1  # each relay is seated exactly one time


def test_route_seats_each_node_on_a_level_platform() -> None:
    """An edge that changes lane leaves level and arrives level, so no node is a pointed apex."""
    pts = _route("u", "v", {"u": (0, 1), "v": (40, 9)}, bidir=False)
    assert pts[0] == (0, 1) and pts[-1] == (40, 9)  # the two markers are the ends
    assert pts[0][1] == pts[1][1]  # level out of u, so u is flat
    assert pts[-1][1] == pts[-2][1]  # level into v, so v is flat


def test_route_same_lane_is_one_level_run() -> None:
    """Two nodes on the same lane join with a single straight horizontal segment."""
    assert _route("u", "v", {"u": (0, 5), "v": (40, 5)}, bidir=False) == [(0, 5), (40, 5)]


def test_route_orients_left_to_right() -> None:
    """A node pair from right to left is flipped, so the trapezium always shows forward flow."""
    forward = _route("u", "v", {"u": (0, 1), "v": (40, 9)}, bidir=False)
    reverse = _route("u", "v", {"u": (40, 9), "v": (0, 1)}, bidir=False)
    assert reverse == forward  # the same picture, in whichever order the pair is given


def test_route_bidirectional_is_a_single_straight_segment() -> None:
    """A two-way pair is the only straight (almost vertical) edge. It is never a trapezium."""
    assert _route("u", "v", {"u": (20, 1), "v": (24, 30)}, bidir=True) == [(20, 1), (24, 30)]


def test_bidir_pair_does_not_jam_a_sibling_relay_against_the_origin() -> None:
    """The 2-cycle of a pair in both directions must not raise the rank and push another relay left.

    ``aa`` and ``bb`` are walked in both directions (the pair that draws vertical). ``cc`` is
    a plain relay one hop from the origin that feeds ``bb``. If the rank uses the raw edge
    set, the cycle drives ``cc`` toward ``0``. If the pair is collapsed, ``cc`` stays at its
    natural place, one third of the way across. The pair shares one x (they draw as one
    vertical line).
    """
    layers = [
        PathLayer(("bb",), WHITE, 3),
        PathLayer(("aa", "bb"), GREY, 2),
        PathLayer(("bb", "aa"), GREY, 2),  # the reverse, which makes aa<->bb a 2-cycle
        PathLayer(("cc", "bb"), GREY, 1),
    ]
    seqs = [(SRC_NODE, *layer.hops, DST_NODE) for layer in layers]
    ordered = list(dict.fromkeys(n for seq in seqs for n in seq))
    edges = {pair for seq in seqs for pair in pairwise(seq)}
    x = _balanced_x(ordered, edges)
    assert x[SRC_NODE] == 0.0 and x[DST_NODE] == 1.0
    assert x["cc"] == pytest.approx(1 / 3)  # its natural place, not pushed toward 0
    assert x["aa"] == x["bb"]  # the pair shares one x, so it draws as one vertical line
    assert x["cc"] < x["aa"]  # and it is still left of the pair that it feeds


def test_labels_ellipsize_only_when_wider_than_the_canvas() -> None:
    """A name is shortened only when it cannot fit: when it is wider than the whole canvas."""
    name = "x" * 30
    lines = render_path_graph(
        [PathLayer(("aa",), WHITE, 4)],
        20,
        glyph_of=_glyph,
        label_of=lambda node: name if node in (SRC_NODE, DST_NODE) else None,
        label_rgb_of=lambda _node: WHITE,
    )
    plain = _ANSI.sub("", "\n".join(lines))
    assert name not in plain  # the full name of 30 cells cannot fit a canvas of 20 cells
    assert "…" in plain  # so it is cut with an ellipsis to the size that fits
    assert all(len(_ANSI.sub("", ln)) <= 20 for ln in lines)  # never goes over the width


def test_the_emphasised_path_is_the_only_one_drawn_in_colour() -> None:
    """Each marker and label that is not on the highlighted path changes to the off-route grey.

    The fan shows which route it is about only if all that is not on the route is dim. The
    lines that are not used already draw grey, and a marker in full hue on one of them is the
    loudest item in the picture. The endpoints are on each path, so they keep their colour.
    """
    layers = [
        PathLayer(hops=("aa",), color=WHITE, priority=2, emphasis=1),
        PathLayer(hops=("bb", "cc"), color=GREY, priority=1),
    ]
    body = "\n".join(
        render_path_graph(
            layers,
            60,
            glyph_of=_glyph,
            label_of=lambda node: "you" if node in (SRC_NODE, DST_NODE) else node,
            label_rgb_of=lambda _node: GREEN,
        )
    )
    lit = [ln for ln in body.split("\n") if "aa" in _ANSI.sub("", ln)]
    dim = [ln for ln in body.split("\n") if "bb" in _ANSI.sub("", ln)]
    assert lit and _sgr(GREEN) in lit[0], "the selected route's label keeps its hue"
    assert dim and _sgr(GREEN) not in dim[0]
    assert _sgr(OFF_ROUTE) in dim[0], "an off-route label draws in the off-route grey"
    # The marker dims with the label. The colour of the relay glyph is drawn for the on-route
    # node and never for the off-route node. The off-route node draws grey two times (the
    # mark and the label).
    assert _sgr((0x65, 0x43, 0x21)) in body, "an on-route relay keeps its marker colour"
    assert body.count(_sgr(OFF_ROUTE)) > 1


def test_a_fan_with_nothing_emphasised_fades_nothing() -> None:
    """A fan with no emphasised route does not fade anything.

    No emphasis means no selection. If all the layers are equal, no route dims, and the
    renderer uses the colours of the caller exactly as the caller gives them.
    """
    layers = [
        PathLayer(hops=("aa",), color=WHITE, priority=2),
        PathLayer(hops=("bb",), color=GREY, priority=1),
    ]
    body = "\n".join(
        render_path_graph(
            layers,
            60,
            glyph_of=_glyph,
            label_of=lambda node: "you" if node in (SRC_NODE, DST_NODE) else node,
            label_rgb_of=lambda _node: GREEN,
        )
    )
    assert _sgr(OFF_ROUTE) not in body
    assert body.count(_sgr(GREEN)) >= 2  # the labels of both relays keep the hue that they got


def test_a_lone_path_is_never_faded() -> None:
    """One path has no other path to compare with, with or without emphasis."""
    body = "\n".join(
        render_path_graph(
            [PathLayer(hops=("aa",), color=WHITE, priority=1, emphasis=1)],
            60,
            glyph_of=_glyph,
            label_of=lambda node: node[:2],
            label_rgb_of=lambda _node: GREEN,
        )
    )
    assert _sgr(OFF_ROUTE) not in body


def test_the_highlight_lights_a_relay_reached_by_a_short_hash() -> None:
    """The highlight lights a relay that a route reaches by a short hash.

    The graph finds the membership in its own id space, after the prefix coalesce. Thus a
    route that names a relay ``3d`` still lights the wide marker into which its hop folded.
    """
    layers = [
        PathLayer(hops=("3d63c6429436",), color=GREY, priority=2),
        PathLayer(hops=("3d",), color=WHITE, priority=1, emphasis=1),
    ]
    body = "\n".join(
        render_path_graph(
            layers,
            60,
            glyph_of=_glyph,
            label_of=lambda node: "you" if node in (SRC_NODE, DST_NODE) else node[:2],
            label_rgb_of=lambda _node: GREEN,
        )
    )
    assert _sgr(OFF_ROUTE) not in body, "the one relay drawn is on the selected route"
    assert _sgr(GREEN) in body
