# SPDX-License-Identifier: Apache-2.0
"""Tests for the message-paths dialog: the graph above the rows, which ^P opens in the chat.

The tests run without a terminal, in the same way as the mesh walk tests. ``render_body``
only returns lines, and ``handle()`` only changes state. Thus a test can assert the
highlight, the horizontal scroll of a line, and the label of the selected path without a
terminal.
"""

from __future__ import annotations

from datetime import timedelta

import meshterm.ui.pathline as pathline
from meshterm.core.models import ChatMessage, utcnow
from meshterm.services.message_paths import Arrival
from meshterm.ui.message_paths_screen import MessagePathsScreen
from meshterm.ui.pathline import CRACK_HEAD, CRACK_TAIL
from tests.conftest import plain as _plain  # the only function that strips and joins a screen


def _resolve(hop: str) -> str:
    return {"3d63": "Hilltop-Repeater", "a1b2": "Waymarker"}.get(hop, hop)


def _screen(arrivals: list[Arrival], **kwargs) -> MessagePathsScreen:
    message = ChatMessage(text="on my way", is_channel=True, created_at=utcnow())
    defaults = dict(
        matched=True,
        resolve=_resolve,
        prefix_bytes=1,
        self_name="Homestead",
        summary="heard twice",
        source="Alice",
    )
    defaults.update(kwargs)
    screen = MessagePathsScreen(message, arrivals, **defaults)
    screen.note_viewport(40)  # the frame records this before each real paint
    return screen


def _arrivals() -> list[Arrival]:
    now = utcnow()
    return [
        Arrival(when=now, hops=("3d63",), snr=4.0),
        Arrival(when=now + timedelta(seconds=2), hops=("a1b2", "77aa"), snr=-2.0),
    ]


def test_paths_screen_renders_graph_rows_and_cursor() -> None:
    """The screen draws the quote, the graph, and two lines for each arrival.

    The two lines are the route that you select, and the reception facts under it.
    """
    screen = _screen(_arrivals())
    body = _plain(screen.render_body(76))
    assert "“on my way”" in body
    assert "Alice" in body and "Homestead" in body  # the origin and our node, on the graph
    assert "via" not in body  # the route lane has only the route
    rows = body.split("sensor\n\n")[1].split("\n")
    assert len(rows) == 2 * len(_arrivals())
    assert rows[0].startswith("❯ ") and "Hilltop-Repeater" in rows[0]  # the selected route
    # The line below it has its length, time, and SNR, and the hop count is first.
    assert rows[1].strip().startswith("1 hop  ")
    assert _arrivals()[0].when.astimezone().strftime("%H:%M") in rows[1]
    assert "+4.0 dB" in rows[1]
    assert "❯" in body
    assert "white = selected path" in body  # the graph caption
    assert "★ you" in body and "▲ repeater" in body  # the node-type legend
    assert screen.cursor_line() is not None


def test_paths_screen_warns_once_for_a_path_that_revisits_a_hop() -> None:
    """A path that has one hop two times draws it two times, with one warning for the whole fan.

    The note is part of the picture, not part of the arrival that ↑↓ select. Thus it names
    each hop that is repeated anywhere in the fan, and it says this one time under the
    legend.
    """
    now = utcnow()
    screen = _screen(
        [
            Arrival(when=now, hops=("3d63", "a1b2", "77aa", "3d63"), snr=4.0),
            Arrival(when=now + timedelta(seconds=2), hops=("a1b2",), snr=-2.0),
        ]
    )
    body = _plain(screen.render_body(76))
    assert body.count("⚠") == 1
    assert "Hilltop-Repeater repeats — a loop, or two nodes sharing one hash" in body
    graph = body.split("origin →")[0]
    assert graph.count("3d") == 2  # both visits have a marker
    assert graph.count("a1") == 1  # the relay that the two paths *share* stays one marker


def test_paths_screen_stays_quiet_when_no_path_revisits() -> None:
    """Two paths that cross the same relay share it.

    They do not revisit it, so there is no warning.
    """
    body = _plain(_screen(_arrivals()).render_body(76))
    assert "⚠" not in body


def test_paths_screen_labels_every_relay_with_its_hash_byte() -> None:
    """The relays in the graph have their first hash byte, for both paths at the same time.

    The rows have only the names. They do not repeat the hash after a name. The hue of the
    node connects the two.
    """
    screen = _screen(_arrivals())
    body = _plain(screen.render_body(76))
    graph = body.split("origin →")[0]
    for byte in ("3d", "a1", "77"):  # each relay has a label, selected or not
        assert byte in graph
    assert "Hilltop-Rep" not in graph and "Waymarker" not in graph
    assert "Hilltop-Repeater" in body and "Waymarker" in body
    assert "(3d)" not in body and "(a1)" not in body  # the hex is on the graph


def test_paths_screen_shows_unknown_relay_as_grey_mode_width_hash() -> None:
    """The screen shows an unnamed relay as its own hash, in muted grey.

    The screen draws it at the path hash width of the device. It does not draw the bare
    one-byte prefix, lit, and it never adds a hash to itself.
    """
    now = utcnow()
    arrivals = [
        Arrival(when=now, hops=("3d63",), snr=4.0),  # selected: a relay with a name
        Arrival(when=now + timedelta(seconds=2), hops=("e839f2ab",), snr=-2.0),
    ]
    screen = _screen(arrivals, prefix_bytes=3)  # routing with 3 bytes → e839f2
    lines = screen.render_body(76)
    row = next(ln for ln in lines if "e839f2" in _plain([ln]))
    assert "(e8)" not in _plain([row])  # the identity with the mode width is alone
    assert "38;2;148;163;184" in row  # muted grey over the hash
    assert not _plain([row]).startswith("❯")  # not selected, so no highlight on it
    graph = _plain(lines).split("origin →")[0]
    assert "e8" in graph  # the byte of the row still matches the label in the graph


def test_paths_screen_draws_selected_path_white_over_gray() -> None:
    """The edges of the selected path render white. The edges of the other path render grey."""
    screen = _screen(_arrivals())
    raw = "\n".join(screen.render_body(76)).split("origin →")[0]
    assert "38;2;255;255;255" in raw  # the selected path, white
    assert "38;2;110;110;110" in raw  # the other path, grey under it
    screen.handle("down")  # move the selection to the other path
    raw2 = "\n".join(screen.render_body(76)).split("origin →")[0]
    assert "38;2;255;255;255" in raw2 and "38;2;110;110;110" in raw2


def test_paths_screen_graph_geometry_holds_still_across_the_selection() -> None:
    """↑↓ paint the colours of the fan again, but never its shape.

    This is the rule of the Routes tab. The layout rank is the order of first heard, so only
    ``emphasis`` follows the selected row. The glyphs and lanes that the screen draws are the
    same, byte for byte, for each selection.
    """
    now = utcnow()
    arrivals = [
        Arrival(when=now, hops=("3d63",), snr=4.0),
        Arrival(when=now + timedelta(seconds=2), hops=("a1b2", "77aa"), snr=-2.0),
        Arrival(when=now + timedelta(seconds=4), hops=("a1b2", "5c5c", "77aa"), snr=1.0),
    ]
    screen = _screen(arrivals)
    shapes = []
    for _ in range(len(arrivals)):
        shapes.append(_plain(screen._graph_lines(76, 15)))
        screen.handle("down")
    assert len(set(shapes)) == 1, "the graph's shape moved when the selection did"
    assert shapes[0].count("\n") > 1  # a real fan with many lanes, not one flat line


def test_paths_screen_marks_a_repeater_relay_with_its_triangle() -> None:
    """A relay whose type is a repeater draws ▲, not the generic dot."""
    screen = _screen(_arrivals(), type_of=lambda h: 2 if h == "3d63" else None)
    graph = _plain(screen.render_body(76)).split("origin →")[0]
    assert "▲" in graph  # the repeater relay has its map glyph
    screen.handle("down")
    raw = "\n".join(screen.render_body(76)).split("origin →")[0]
    assert "38;2;255;255;255" in raw and "38;2;110;110;110" in raw


def test_paths_screen_scrolls_the_selected_route_sideways() -> None:
    """→ moves the selected *route* sideways, with a … at the start. ↑↓ move it back.

    The reception facts under it never move. They always fit. If the lane moved with the
    route, a long path would look as if it had no timestamp.
    """
    now = utcnow()
    long = Arrival(when=now, hops=tuple(f"{i:02x}{i:02x}" for i in range(12)), snr=1.0)
    screen = _screen([long, Arrival(when=now, hops=(), snr=None)])
    narrow = 40
    before = _plain(screen.render_body(narrow))
    selected_before = next(ln for ln in before.split("\n") if ln.startswith("❯"))
    assert screen._hmax > 0  # the route is too wide at this width
    assert "00" in selected_before and "0b" not in selected_before  # head shown, tail cut
    for _ in range(3):
        screen.handle("right")
    after = _plain(screen.render_body(narrow))
    selected_after = next(ln for ln in after.split("\n") if ln.startswith("❯"))
    assert selected_after != selected_before
    assert "…" in selected_after  # the left edge marks the hidden head
    stamp = long.when.astimezone().strftime("%H:%M:%S")
    assert stamp in after and stamp not in selected_after  # the facts keep their lane
    screen.handle("down")
    assert screen._hshift == 0  # only the selected route stays scrolled


def test_paths_screen_cracks_the_chips_the_scroll_cuts(monkeypatch) -> None:  # noqa: ANN001
    """Where the terminal draws chips, the scroll cracks them. It does not use an ellipsis.

    At each edge that the route goes past, the chip breaks on a half block in its own
    colour. The row slides over a route that continues. It does not make a word shorter.
    """
    monkeypatch.setattr(pathline, "powerline_enabled", lambda: True)
    now = utcnow()
    long = Arrival(when=now, hops=tuple(f"{i:02x}{i:02x}" for i in range(12)), snr=1.0)
    screen = _screen([long, Arrival(when=now, hops=(), snr=None)])
    narrow = 40

    def selected() -> str:
        return next(
            ln for ln in _plain(screen.render_body(narrow)).split("\n") if ln.startswith("❯")
        )

    row = selected()
    assert row.rstrip().endswith(CRACK_TAIL) and "…" not in row  # only the tail continues
    for _ in range(3):
        screen.handle("right")
    row = selected()
    assert row.startswith("❯ " + CRACK_HEAD)  # now the head is also off to the left
    assert row.rstrip().endswith(CRACK_TAIL) and "…" not in row


def test_paths_screen_route_runs_origin_to_us_not_relay_to_relay() -> None:
    """A row is the whole route, not the relay chain that the message used.

    The ends of a path are the nodes that it went *between*. Thus the sender is at the start
    of the line, and the line ends on our ★. These are the same two endpoints that the
    graph one row above draws between, and this lets the user match the row with the
    picture. A chain that started on its first relay looked like a route from a node that
    only passed the message on.
    """
    screen = _screen(_arrivals())
    rows = _plain(screen.render_body(76)).split("sensor\n\n")[1].split("\n")
    assert rows[0] == "❯ Alice → Hilltop-Repeater → ★"
    assert rows[2] == "  Alice → Waymarker → 77 → ★"  # an unnamed relay is still its hash


def test_paths_screen_origin_is_a_star_for_us_and_a_question_for_nobody() -> None:
    """The origin is a ``★`` for our node and a ``?`` for nobody.

    The start of the line has the same name as the left endpoint of the graph. It is our
    star on a message that we sent. It is a bare question mark where the packet gave no
    name. It is never a name that MeshTerm guessed.
    """
    now = utcnow()
    one = [Arrival(when=now, hops=("3d63",), snr=1.0)]
    ours = _plain(_screen(one, source="Homestead").render_body(76))
    assert "❯ ★ → Hilltop-Repeater → ★" in ours
    nameless = _plain(_screen(one, source=None).render_body(76))
    assert "❯ ? → Hilltop-Repeater → ★" in nameless


def test_paths_screen_cuts_unselected_rows_the_same_way(monkeypatch) -> None:  # noqa: ANN001
    """A row that is not selected is cut in the same way as the selected row.

    It goes past the lane in the same way as the selected row. Thus it must show the same
    cracked chip to the user. But it cannot slide to show the rest.
    """
    monkeypatch.setattr(pathline, "powerline_enabled", lambda: True)
    now = utcnow()
    long = tuple(f"{i:02x}{i:02x}" for i in range(12))
    screen = _screen(
        [
            Arrival(when=now, hops=long, snr=1.0),
            Arrival(when=now + timedelta(seconds=2), hops=long[::-1], snr=2.0),
        ]
    )
    lines = _plain(screen.render_body(40)).split("\n")
    rows = lines[next(i for i, ln in enumerate(lines) if ln.startswith("❯")) :]
    assert rows[0].rstrip().endswith(CRACK_TAIL)  # the selected row
    unselected = rows[2]  # row 1 has the reception facts of the selected row
    assert unselected.rstrip().endswith(CRACK_TAIL) and "…" not in unselected


def test_paths_screen_direct_arrival_and_empty_state() -> None:
    """An arrival with no hops draws its two endpoints and nothing between them.

    If there are no arrivals, the screen explains this instead.

    The route no longer becomes the word *direct*. Both ends are on the line, and
    ``Alice → ★`` **is** how a direct delivery looks. It has the same form as each relayed
    row, and the screen does not replace the picture with a caption. The word is still
    there, one row below, where it belongs. It is the hop-count statistic under the line,
    and it shows how far the packet went. It does not replace the picture.
    """
    now = utcnow()
    screen = _screen([Arrival(when=now, hops=(), snr=2.5)])
    body = _plain(screen.render_body(76))
    assert "❯ Alice → ★" in body
    route = next(ln for ln in body.splitlines() if ln.startswith("❯"))
    assert "direct" not in route
    assert body.splitlines()[body.splitlines().index(route) + 1].strip().startswith("direct")
    empty = _screen([], matched=False)
    body = _plain(empty.render_body(76))
    assert "No direct-message frames logged in the window." in body
    assert empty.footer_hint == "Esc close"


def test_an_outgoing_message_ends_on_its_recipient_not_on_us() -> None:
    """This is the round-trip correction: a message that we send does not start and end on our star.

    The path line always ended on our ``★``. This is correct for a message that we received.
    It is wrong for a message that we sent. Our star was also at the start. Thus each
    outgoing message drew ``★ ▶ … ▶ ★``, and it looked as if the message went out and came
    back (JP, 2026-09-02). The start of the line already followed the same rule: a path
    runs between the nodes that it went *between*.
    """
    now = utcnow()
    sent = ChatMessage(text="on my way", outbound=True, peer="d4e5", created_at=now)
    screen = MessagePathsScreen(
        sent,
        [Arrival(when=now, hops=("3d63",), snr=4.0)],
        matched=True,
        resolve=_resolve,
        prefix_bytes=1,
        self_name="Homestead",
        summary="heard once",
        source="Homestead",
        destination="Bob",
    )
    screen.note_viewport(40)  # the frame records this before each real paint
    body = _plain(screen.render_body(72))
    assert "Hilltop-Repeater" in body
    assert body.count("Homestead") == 1, "our name belongs at one end of a send, not both"
    assert "Bob" in body, "the far end is the recipient"
    # The caption names the same far end that the line ends on.
    assert "origin → Bob" in body


def test_a_received_message_still_ends_on_us() -> None:
    """The default does not change: a message that we receive ends at our own star."""
    now = utcnow()
    got = ChatMessage(text="on my way", outbound=False, peer="d4e5", created_at=now)
    screen = MessagePathsScreen(
        got,
        [Arrival(when=now, hops=("3d63",), snr=4.0)],
        matched=True,
        resolve=_resolve,
        prefix_bytes=1,
        self_name="Homestead",
        summary="heard once",
        source="Alice",
    )
    screen.note_viewport(40)  # the frame records this before each real paint
    body = _plain(screen.render_body(72))
    assert "Alice" in body and "Homestead" in body
    assert "origin → you" in body


def test_a_routed_frame_with_no_path_draws_no_graph_and_says_why() -> None:
    """An empty routed path is not an arrival with zero hops.

    The screen must not draw it as one. The route was used on the way, so there is nothing
    to put in the fan. An empty lane would claim that the nodes are adjacent, and the packet
    did not make that claim. The row says this in words. It does not show a ``0 hops`` that
    looks sure.
    """
    now = utcnow()
    got = ChatMessage(text="on my way", outbound=False, peer="d4e5", created_at=now)
    screen = MessagePathsScreen(
        got,
        [Arrival(when=now, hops=(), snr=4.0, routed=True)],
        matched=True,
        resolve=_resolve,
        prefix_bytes=1,
        self_name="Homestead",
        summary="heard once",
        source="Alice",
    )
    body = _plain(screen.render_body(72))
    assert "route not carried" in body
    assert "origin →" not in body, "no graph, so nothing for the caption to label"


def _many(n: int) -> list[Arrival]:
    """``n`` arrivals, each on its own two-hop route, oldest first."""
    now = utcnow()
    return [
        Arrival(when=now + timedelta(seconds=i), hops=(f"{i:02x}{i:02x}", "77aa"), snr=1.0)
        for i in range(n)
    ]


def test_the_arrival_list_windows_and_the_graph_stays_put() -> None:
    """Only the arrivals scroll. The quote, the fan, and its captions are fixed chrome.

    When the user goes to the last arrival, the list window slides under them. The body
    still fits in the space of the dialog. The picture that the user compares the routes
    with never leaves the dialog.
    """
    screen = _screen(_many(12))
    screen.note_viewport(24)
    lines = screen.render_body(72)
    assert len(lines) <= 24  # nothing scrolls off: the list took the space that the head left
    body = _plain(lines)
    assert "“on my way”" in body and "white = selected path" in body
    assert "↓" in body and "more" in body  # the edge marker counts the hidden arrivals
    assert "PgUp/PgDn scroll" in screen.footer_hint  # the hint shows paging only if necessary

    for _ in range(11):
        screen.handle("down")
    lines = screen.render_body(72)
    body = _plain(lines)
    assert len(lines) <= 24
    assert "white = selected path" in body, "the graph is pinned, not scrolled away"
    assert "↑" in body and "more" in body  # now the hidden rows are above
    assert screen.cursor_line() is not None


def test_paging_moves_the_selection_by_a_windowful() -> None:
    """PgDn moves the selection by the rows that the list window had, not by body lines."""
    screen = _screen(_many(12))
    screen.note_viewport(24)
    screen.render_body(72)
    page = screen._list.page
    assert 1 <= page < 12
    screen.handle("pagedown")
    assert screen._index == page


def test_a_short_list_advertises_no_paging() -> None:
    """If each arrival is visible, the pager moves nothing, and the hint does not mention it."""
    screen = _screen(_arrivals())
    screen.render_body(72)
    assert "PgUp/PgDn" not in screen.footer_hint
    assert "more" not in _plain(screen.render_body(72))


def test_the_selection_clamps_at_both_ends() -> None:
    """↑ on the first arrival and ↓ on the last arrival do not move the selection.

    The list window follows the highlight. Thus a wrap would move the whole list from one
    end to the other end, and not one row.
    """
    screen = _screen(_many(6))
    screen.note_viewport(24)
    screen.render_body(72)
    screen.handle("up")
    assert screen._index == 0
    for _ in range(10):
        screen.handle("down")
    assert screen._index == 5
