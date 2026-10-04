# SPDX-License-Identifier: Apache-2.0
"""Message-paths dialog tests: the graph-over-rows view behind the chat's ^P.

Driven headless like the mesh walk tests: render_body is pure lines-out, handle() pure
state, so the selection cursor, the horizontal line scroll, and the selected-path
labelling are all assertable without a terminal.
"""

from __future__ import annotations

from datetime import timedelta

import meshterm.ui.pathline as pathline
from meshterm.core.models import ChatMessage, utcnow
from meshterm.services.message_paths import Arrival
from meshterm.ui.message_paths_screen import MessagePathsScreen
from meshterm.ui.pathline import CRACK_HEAD, CRACK_TAIL
from tests.conftest import plain as _plain  # THE strip-and-join screen reader


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
    screen.note_viewport(40)  # the frame records this before every real paint
    return screen


def _arrivals() -> list[Arrival]:
    now = utcnow()
    return [
        Arrival(when=now, hops=("3d63",), snr=4.0),
        Arrival(when=now + timedelta(seconds=2), hops=("a1b2", "77aa"), snr=-2.0),
    ]


def test_paths_screen_renders_graph_rows_and_cursor() -> None:
    """The screen draws the quote, the graph, and two lines for every arrival.

    The route you pick, and the reception facts hanging under it.
    """
    screen = _screen(_arrivals())
    body = _plain(screen.render_body(76))
    assert "“on my way”" in body
    assert "Alice" in body and "Homestead" in body  # origin and us, on the graph
    assert "via" not in body  # the route lane holds nothing but the route
    rows = body.split("sensor\n\n")[1].split("\n")
    assert len(rows) == 2 * len(_arrivals())
    assert rows[0].startswith("❯ ") and "Hilltop-Repeater" in rows[0]  # the picked route
    # …with its length, time and SNR tucked beneath it, the hop count leading
    assert rows[1].strip().startswith("1 hop  ")
    assert _arrivals()[0].when.astimezone().strftime("%H:%M") in rows[1]
    assert "+4.0 dB" in rows[1]
    assert "❯" in body
    assert "white = selected path" in body  # the graph caption
    assert "★ you" in body and "▲ repeater" in body  # the node-type legend
    assert screen.cursor_line() is not None


def test_paths_screen_warns_once_for_a_path_that_revisits_a_hop() -> None:
    """A path touching one hop twice draws it twice, warned about once for the whole fan.

    The note belongs to the picture, not to the arrival ↑↓ rest on, so it names every hop
    repeated anywhere in the fan and says so a single time under the legend.
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
    assert graph.count("3d") == 2  # both visits marked
    assert graph.count("a1") == 1  # the relay the two paths *share* stays one marker


def test_paths_screen_stays_quiet_when_no_path_revisits() -> None:
    """Two paths crossing the same relay is sharing, not revisiting — nothing to warn about."""
    body = _plain(_screen(_arrivals()).render_body(76))
    assert "⚠" not in body


def test_paths_screen_labels_every_relay_with_its_hash_byte() -> None:
    """Graph relays carry their first hash byte, both paths at once.

    The rows carry the names alone — no hash repeated after one, the two tied
    together by the node's hue.
    """
    screen = _screen(_arrivals())
    body = _plain(screen.render_body(76))
    graph = body.split("origin →")[0]
    for byte in ("3d", "a1", "77"):  # every relay labelled, selected or not
        assert byte in graph
    assert "Hilltop-Rep" not in graph and "Waymarker" not in graph
    assert "Hilltop-Repeater" in body and "Waymarker" in body
    assert "(3d)" not in body and "(a1)" not in body  # the hex lives on the graph


def test_paths_screen_shows_unknown_relay_as_grey_mode_width_hash() -> None:
    """An unnamed relay stands in its own hash, in muted grey.

    It is drawn at the device's path-hash width — not the bare one-byte prefix, lit,
    and never a hash annotated onto itself.
    """
    now = utcnow()
    arrivals = [
        Arrival(when=now, hops=("3d63",), snr=4.0),  # selected: a named relay
        Arrival(when=now + timedelta(seconds=2), hops=("e839f2ab",), snr=-2.0),
    ]
    screen = _screen(arrivals, prefix_bytes=3)  # 3-byte routing → e839f2
    lines = screen.render_body(76)
    row = next(ln for ln in lines if "e839f2" in _plain([ln]))
    assert "(e8)" not in _plain([row])  # the mode-width identity stands alone
    assert "38;2;148;163;184" in row  # muted grey over the hash
    assert not _plain([row]).startswith("❯")  # unselected, so no cursor highlight over it
    graph = _plain(lines).split("origin →")[0]
    assert "e8" in graph  # the row's byte still cross-references the graph label


def test_paths_screen_draws_selected_path_white_over_gray() -> None:
    """The selected path's edges render white; the unused path's edges gray."""
    screen = _screen(_arrivals())
    raw = "\n".join(screen.render_body(76)).split("origin →")[0]
    assert "38;2;255;255;255" in raw  # the selected path, white
    assert "38;2;110;110;110" in raw  # the other path, gray beneath it
    screen.handle("down")  # move the selection to the other path
    raw2 = "\n".join(screen.render_body(76)).split("origin →")[0]
    assert "38;2;255;255;255" in raw2 and "38;2;110;110;110" in raw2


def test_paths_screen_graph_geometry_holds_still_across_the_selection() -> None:
    """↑↓ repaint the fan's colours, never its shape — the Routes tab's rule.

    Layout rank is first-heard order, so only ``emphasis`` follows the pick; the drawn
    glyphs/lanes are byte-identical from every selection.
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
    assert shapes[0].count("\n") > 1  # a genuine multi-lane fan, not one flat line


def test_paths_screen_marks_a_repeater_relay_with_its_triangle() -> None:
    """A relay whose type resolves to a repeater draws ▲, not the generic dot."""
    screen = _screen(_arrivals(), type_of=lambda h: 2 if h == "3d63" else None)
    graph = _plain(screen.render_body(76)).split("origin →")[0]
    assert "▲" in graph  # the repeater relay wears its map glyph
    screen.handle("down")
    raw = "\n".join(screen.render_body(76)).split("origin →")[0]
    assert "38;2;255;255;255" in raw and "38;2;110;110;110" in raw


def test_paths_screen_scrolls_the_selected_route_sideways() -> None:
    """→ shifts the selected *route* under a leading …; ↑↓ snap it back.

    The reception facts under it never move — they always fit, and a lane that slid
    with the route would make a long path look like it had lost its timestamp.
    """
    now = utcnow()
    long = Arrival(when=now, hops=tuple(f"{i:02x}{i:02x}" for i in range(12)), snr=1.0)
    screen = _screen([long, Arrival(when=now, hops=(), snr=None)])
    narrow = 40
    before = _plain(screen.render_body(narrow))
    selected_before = next(ln for ln in before.split("\n") if ln.startswith("❯"))
    assert screen._hmax > 0  # the route genuinely overflows at this width
    assert "00" in selected_before and "0b" not in selected_before  # head shown, tail cut
    for _ in range(3):
        screen.handle("right")
    after = _plain(screen.render_body(narrow))
    selected_after = next(ln for ln in after.split("\n") if ln.startswith("❯"))
    assert selected_after != selected_before
    assert "…" in selected_after  # the left edge marks the hidden head
    stamp = long.when.astimezone().strftime("%H:%M:%S")
    assert stamp in after and stamp not in selected_after  # the facts hold their lane
    screen.handle("down")
    assert screen._hshift == 0  # only the selected route stays scrolled


def test_paths_screen_cracks_the_chips_the_scroll_cuts(monkeypatch) -> None:  # noqa: ANN001
    """Where the terminal draws chips, the scroll cracks them rather than ellipsizing.

    Each edge the route runs past breaks the chip off on a half block in its own
    colour: the row is sliding over a route that continues, not shortening a word.
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
    assert row.rstrip().endswith(CRACK_TAIL) and "…" not in row  # only the tail runs on
    for _ in range(3):
        screen.handle("right")
    row = selected()
    assert row.startswith("❯ " + CRACK_HEAD)  # …and now the head is off to the left too
    assert row.rstrip().endswith(CRACK_TAIL) and "…" not in row


def test_paths_screen_route_runs_origin_to_us_not_relay_to_relay() -> None:
    """A row is the whole route, not the relay chain it rode.

    A path's ends are the nodes it went *between*, so the sender leads the line and we
    close it on the ★ — the same two endpoints the graph one row up draws between, which
    is what lets the row and the picture cross-read. A chain that opened on its first
    relay read as a route from a node that had only passed the message on.
    """
    screen = _screen(_arrivals())
    rows = _plain(screen.render_body(76)).split("sensor\n\n")[1].split("\n")
    assert rows[0] == "❯ Alice → Hilltop-Repeater → ★"
    assert rows[2] == "  Alice → Waymarker → 77 → ★"  # an unnamed relay still stands in its hash


def test_paths_screen_origin_is_a_star_for_us_and_a_question_for_nobody() -> None:
    """The origin is a ``★`` for us and a ``?`` for nobody.

    The head is named exactly as the graph's left endpoint is: our own star on a
    message we sent, a bare question mark where the frame named nobody — never a
    guessed name.
    """
    now = utcnow()
    one = [Arrival(when=now, hops=("3d63",), snr=1.0)]
    ours = _plain(_screen(one, source="Homestead").render_body(76))
    assert "❯ ★ → Hilltop-Repeater → ★" in ours
    nameless = _plain(_screen(one, source=None).render_body(76))
    assert "❯ ? → Hilltop-Repeater → ★" in nameless


def test_paths_screen_cuts_unselected_rows_the_same_way(monkeypatch) -> None:  # noqa: ANN001
    """An unselected row is cut the same way the selected one is.

    It runs off the lane exactly as the selected row does, so it owes the reader the
    same cracked chip — it just cannot slide to read the rest.
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
    unselected = rows[2]  # row 1 is the selected row's hanging reception facts
    assert unselected.rstrip().endswith(CRACK_TAIL) and "…" not in unselected


def test_paths_screen_direct_arrival_and_empty_state() -> None:
    """A hop-less arrival draws its two endpoints and nothing between.

    With no arrivals at all, the screen explains itself instead.

    The route no longer collapses to the word *direct*: with both ends on the line,
    ``Alice → ★`` **is** what a direct delivery looks like, and it reads on the same rails
    as every relayed row instead of swapping the picture for a caption. The word survives
    one row down, where it belongs — as the hop-count stat under the line, saying how far
    the frame came rather than standing in for the picture of it.
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
    """THE round-trip fix: our own sends stopped starting and ending on our own star.

    The path line always closed on our ``★``, which is right for a message we received and
    wrong for one we sent — with our star opening it too, every outgoing message drew
    ``★ ▶ … ▶ ★`` and read as having gone out and come back (JP, 2026-09-02). It is the same
    rule the head already followed: a path runs between the nodes it went *between*.
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
    screen.note_viewport(40)  # the frame records this before every real paint
    body = _plain(screen.render_body(72))
    assert "Hilltop-Repeater" in body
    assert body.count("Homestead") == 1, "our name belongs at one end of a send, not both"
    assert "Bob" in body, "the far end is the recipient"
    # The caption names the same far end the line ends on.
    assert "origin → Bob" in body


def test_a_received_message_still_ends_on_us() -> None:
    """The default is unchanged: what we receive does end at our own star."""
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
    screen.note_viewport(40)  # the frame records this before every real paint
    body = _plain(screen.render_body(72))
    assert "Alice" in body and "Homestead" in body
    assert "origin → you" in body


def test_a_routed_frame_with_no_path_draws_no_graph_and_says_why() -> None:
    """An empty routed path is not a zero-hop arrival, and must not be drawn as one.

    Its route was consumed on the way, so there is nothing to put in the fan — an empty lane
    would be a claim of adjacency the frame never made — and the row says so in words rather
    than showing a confident ``0 hops``.
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
    """Only the arrivals scroll: the quote, the fan and its captions are pinned chrome.

    Walking to the last arrival slides the window under them — the body still fits the
    dialog's budget, and the picture the routes are being compared against never leaves it.
    """
    screen = _screen(_many(12))
    screen.note_viewport(24)
    lines = screen.render_body(72)
    assert len(lines) <= 24  # nothing scrolls off: the list took what the head left
    body = _plain(lines)
    assert "“on my way”" in body and "white = selected path" in body
    assert "↓" in body and "more" in body  # the edge marker counts the hidden arrivals
    assert "PgUp/PgDn scroll" in screen.footer_hint  # paging advertised only when needed

    for _ in range(11):
        screen.handle("down")
    lines = screen.render_body(72)
    body = _plain(lines)
    assert len(lines) <= 24
    assert "white = selected path" in body, "the graph is pinned, not scrolled away"
    assert "↑" in body and "more" in body  # rows now hidden above instead
    assert screen.cursor_line() is not None


def test_paging_moves_the_selection_by_a_windowful() -> None:
    """PgDn steps the pick by the rows the window actually carried, not by body lines."""
    screen = _screen(_many(12))
    screen.note_viewport(24)
    screen.render_body(72)
    page = screen._list.page
    assert 1 <= page < 12
    screen.handle("pagedown")
    assert screen._index == page


def test_a_short_list_advertises_no_paging() -> None:
    """Every arrival on screen means the pager moves nothing, and the hint says nothing."""
    screen = _screen(_arrivals())
    screen.render_body(72)
    assert "PgUp/PgDn" not in screen.footer_hint
    assert "more" not in _plain(screen.render_body(72))


def test_the_selection_clamps_at_both_ends() -> None:
    """↑ on the first arrival and ↓ on the last stay put.

    The window follows the highlight, so a wrap would haul the whole list end to end
    instead of moving one row.
    """
    screen = _screen(_many(6))
    screen.note_viewport(24)
    screen.render_body(72)
    screen.handle("up")
    assert screen._index == 0
    for _ in range(10):
        screen.handle("down")
    assert screen._index == 5
