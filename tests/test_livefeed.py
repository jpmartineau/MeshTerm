# SPDX-License-Identifier: Apache-2.0
"""Live feed tests: the promoted feed screen's state, rows, and windowing.

The screen is driven headless against fake sessions and canned observations — the
same approach as the dashboard tests, whose feed panel this screen grew out of.
"""

from __future__ import annotations

import re
from datetime import timedelta

from rich.cells import cell_len

from meshterm.core.events import MeshEvent
from meshterm.core.models import Ack, Message, Observation, utcnow
from meshterm.ui.livefeed_screen import _ICON_LANE, LiveFeedScreen
from tests.conftest import plain as _plain  # THE strip-and-join screen reader


class _FakeSession:
    def __init__(self) -> None:
        self.repaints = 0
        #: The screen the feed last floated over itself (the packet viewer).
        self.opened = None

    def invalidate(self) -> None:
        self.repaints += 1

    def run_screen(self, screen):
        """Record the floated screen instead of running it — there is no loop here."""
        self.opened = screen

    def run_detached(self, coro) -> None:
        """The session's fire-and-forget launch; ``run_screen`` already did the recording."""


class _Fut:
    """A minimal future stand-in so the screen can resolve without an event loop."""

    def __init__(self) -> None:
        self.value = None
        self._done = False

    def done(self) -> bool:
        return self._done

    def set_result(self, value) -> None:
        self.value = value
        self._done = True


#: Nodes the feed's resolver can name, by the hash a frame addresses them with (one byte)
#: and by the wider prefixes an advert carries.
_KNOWN = {"a1b2": "Alice", "3d63": "Hub", "a1": "Alice", "3d": "Hub", "c0": "Us"}


def _screen(seed=None, **kwargs) -> LiveFeedScreen:
    screen = LiveFeedScreen(
        session=_FakeSession(),
        resolve=lambda h: _KNOWN.get(h, ""),
        seed=list(seed or []),
        **kwargs,
    )
    screen.note_viewport(48)  # the frame records this before every real paint
    return screen


def _obs(node="a1b2", kind="advert", snr=5.0, rssi=-90.0, age_s=0, **extra) -> Observation:
    return Observation(
        node=node,
        name=node,
        kind=kind,
        snr=snr,
        rssi=rssi,
        observed_at=utcnow() - timedelta(seconds=age_s),
        **extra,
    )


def _stripped(lines: list[str]) -> list[str]:
    """The rendered lines with ANSI escapes removed, for structural assertions."""
    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]


def _rows(screen: LiveFeedScreen, width: int) -> list[str]:
    """Just the packet rows — the body past its column header."""
    return _stripped(screen.render_body(width))[1:]


def _col(line: str, needle: str) -> int:
    """The display column (cells, not characters) ``needle`` starts at within ``line``."""
    return cell_len(line[: line.index(needle)])


def test_livefeed_tool_registers_under_the_dashboard() -> None:
    """The livefeed tool lands in the Watch section, right below the dashboard."""
    from meshterm.tools import load_all_tools
    from meshterm.tools.base import all_tools

    load_all_tools()
    tools = {t.name: t for t in all_tools()}
    tool = tools["livefeed"]
    assert tool.title == "Live feed" and tool.category == "Watch"
    assert tools["dashboard"].category == tool.category
    assert tools["dashboard"].order < tool.order < tools["watchtower"].order


def test_livefeed_live_events_land_in_the_feed() -> None:
    """Observations, messages, and acks each fold into the feed as they arrive."""
    screen = _screen()
    screen.on_event(MeshEvent.observation_event(_obs(snr=7.5)))
    screen.on_event(MeshEvent.message_event(Message(text="hi", sender="a1b2")))
    screen.on_event(MeshEvent.ack_event(Ack(code="01c3")))
    body = _plain(screen.render_body(100))
    assert "+7.5 dB" in body
    assert "message" in body and "ack" in body
    # Newest first: the ack row sits above the message row, which sits above the advert.
    assert body.index("ack") < body.index("message") < body.index("advert")


def test_livefeed_rows_leave_the_relay_path_to_the_viewer() -> None:
    """A row says what arrived and how well it was heard; the route is Enter's job.

    Whatever the fixed lanes left a route was never enough to draw one in — it arrived
    elided to a stub — so the feed carries none: the row is its fixed lanes, class name
    and all, and a wide terminal holds the whole of it.
    """
    screen = _screen()
    screen.on_event(MeshEvent.observation_event(_obs(kind="packet", path="3d63,a1b2", snr=1.0)))
    row = _rows(screen, 100)[0]
    assert "via" not in row and "Hub" not in row  # no route, not even a stub of one
    assert "📦 packet" in row  # the class, in words
    assert "+1.0 dB" in row and "-90 dBm" in row  # …and the reception it was heard at


def test_livefeed_class_lane_names_the_payload_class_once() -> None:
    """A raw frame is filed under what it *is*, and the subject lane doesn't repeat it.

    ``packet`` names only the event family the frame arrived in, so the class lane
    reads its parsed payload class — the very label the viewer's card headlines. The
    subject lane is then free to be about the packet's own subject, and a class that
    carries nothing to be about says so with a dash rather than standing the class in a
    second time.
    """
    screen = _screen()
    raw = {"payload_typename": "MULTIPART", "route_typename": "FLOOD"}
    screen.on_event(
        MeshEvent.observation_event(
            _obs(node="", kind="packet", snr=1.0, path="3d63,a1b2", raw=raw)
        )
    )
    row = _rows(screen, 100)[0]
    assert "🧩 multipart" in row  # the class lane, under the payload class's own icon
    assert row.count("multipart") == 1  # said once, not once per lane
    assert "packet" not in row  # …and never as the generic event family
    assert "—" in row  # the subject lane: the class is about nothing
    assert "?" not in row  # …but never the useless placeholder


def test_livefeed_names_a_channel_message_by_its_sender() -> None:
    """A channel message's ``Name:`` prefix names the subject lane, not the bare channel."""
    screen = _screen()
    screen.on_event(
        MeshEvent.message_event(Message(text="Alice: hi all", channel=3, is_channel=True))
    )
    body = _plain(screen.render_body(100))
    assert "Alice" in body  # the parsed sender leads the row
    assert "ch 3" in body  # …with the channel kept as the trailing context


def test_livefeed_unsigned_channel_post_is_filed_under_its_channel() -> None:
    """With nobody signing it, a channel message is about the channel — named, once."""
    screen = _screen(channel_names={3: "Alerts"})
    screen.on_event(MeshEvent.message_event(Message(text="beep boop", channel=3, is_channel=True)))
    row = _rows(screen, 100)[0]
    assert "Alerts" in row  # the channel name, not its slot number
    assert row.count("Alerts") == 1  # …and the trailing note doesn't say it again
    assert "ch 3" not in row


def test_livefeed_channel_frame_is_about_its_channel() -> None:
    """An overheard channel frame is filed under the channel whose key confirms its MAC."""
    from Crypto.Hash import HMAC, SHA256

    from meshterm.core.channels import channel_hash, derive_secret

    secret = derive_secret("Public")
    crypted = bytes(range(16))
    mac = HMAC.new(secret, digestmod=SHA256)
    mac.update(crypted)
    raw = {
        "payload_typename": "GRP_TXT",
        "chan_hash": channel_hash(secret),
        "cipher_mac": mac.digest()[:2].hex(),
        "crypted": crypted.hex(),
    }
    screen = _screen(channels=[("Public", secret)])
    screen.on_event(MeshEvent.observation_event(_obs(node="", kind="packet", raw=raw)))
    assert "Public" in _rows(screen, 100)[0]

    # A channel we hold no key for can only be named by the fingerprint it advertised —
    # never by a name, since a fingerprint alone is not proof of which channel it is.
    other = _screen(channels=[("Public", secret)])
    other.on_event(
        MeshEvent.observation_event(_obs(node="", kind="packet", raw={**raw, "chan_hash": "a3"}))
    )
    row = _rows(other, 100)[0]
    assert "hash a3" in row and "Public" not in row


def test_livefeed_addressed_frame_is_about_its_two_ends() -> None:
    """A frame that names a recipient reads as sender → recipient, resolved to names."""
    screen = _screen(self_name="Us")
    raw = {"payload_typename": "TEXT_MSG", "dest_hash": "c0", "src_hash": "a1"}
    screen.on_event(MeshEvent.observation_event(_obs(node="", kind="packet", raw=raw)))
    row = _rows(screen, 100)[0]
    assert "📩 dir messg" in row
    # …including us, when a frame is addressed to us — as the app-wide ★, which is the one
    # endpoint the reader never has to be told and the cells the other end's name needs.
    assert "Alice → ★" in row

    # A pair too wide for the lane spends its cells on the addressee: the sender drops to
    # the hash it was named by rather than the recipient being the half cut off.
    wide = _screen()
    wide._resolve = lambda h: {"a1": "Alice-With-A-Long-Name", "3d": "Hilltop-Repeater"}.get(h, "")
    wide.on_event(
        MeshEvent.observation_event(
            _obs(
                node="",
                kind="packet",
                raw={"payload_typename": "REQ", "dest_hash": "3d", "src_hash": "a1"},
            )
        )
    )
    assert "a1 → Hilltop-Repe" in _rows(wide, 100)[0]


def test_livefeed_tokened_classes_are_about_their_token() -> None:
    """An ack names the message it answers; a trace names its tag. Neither says "packet"."""
    screen = _screen()
    screen.on_event(
        MeshEvent.observation_event(
            _obs(
                node="",
                kind="packet",
                age_s=1,
                raw={"payload_typename": "TRACE", "trace_tag": "5f3c2a10"},
            )
        )
    )
    screen.on_event(
        MeshEvent.observation_event(
            _obs(node="", kind="packet", raw={"payload_typename": "ACK", "ack_crc": "9b71e004"})
        )
    )
    acked, traced = _rows(screen, 100)[:2]
    assert "for 9b71e004" in acked
    assert "tag 5f3c2a10" in traced


def test_livefeed_column_header_sits_over_the_lanes_it_names() -> None:
    """The header names each lane, and every label lands on the values beneath it."""
    screen = _screen(seed=[_obs(node="3d63", kind="telemetry", snr=12.8, rssi=-105.0)])
    header, row = _stripped(screen.render_body(90))[:2]
    assert header.split() == ["TIME", "CLASS", "SUBJECT", "SCOPE", "SNR", "RSSI"]
    # Measured in display cells, not characters — the class icon is one character wide
    # but two cells, so a character index would report every later lane one column early.
    # The clock is read off the row itself: a literal needle would only match during the
    # minute it was written in.
    stamp = re.search(r"\d\d:\d\d:\d\d", row)
    assert stamp is not None
    assert _col(header, "TIME") == _col(row, stamp.group(0))
    assert _col(header, "CLASS") == _col(row, "telemetry") - _ICON_LANE
    assert _col(header, "SUBJECT") == _col(row, "Hub")
    # The two readings right-align their number, so their labels end where the digits do.
    assert _col(header, "SNR") + 3 == _col(row, "+12.8") + 5
    assert _col(header, "RSSI") + 4 == _col(row, "-105") + 4


def test_livefeed_column_header_is_pinned_and_carries_no_sort_cue() -> None:
    """The header never scrolls away, and it advertises no sort — the feed has one order."""
    screen = _screen(seed=[_obs(node=f"n{i}", age_s=i) for i in range(40)])
    screen.note_viewport(12)
    lines = _stripped(screen.render_body(100))
    assert "TIME" in lines[0]
    assert not any(mark in lines[0] for mark in ("▲", "▼"))  # nothing here sorts
    screen.handle("end")  # the list's last row — Back
    screen.handle("up")  # …and one up from it, the oldest packet: the window scrolls
    after = _stripped(screen.render_body(100))
    assert after[0] == lines[0]  # …and the header is exactly where it was
    assert "↑" in after[1] and "more" in after[1]  # the rows really did travel


def test_livefeed_highlight_uses_the_app_wide_cursor() -> None:
    """The feed marks its row like every other list: ``❯`` over a ``cursor``-white row."""
    screen = _screen(seed=[_obs(node="n0", age_s=1), _obs(node="n1", age_s=0)])
    screen.handle("down")  # off the pin, onto the newest packet normally selected
    rows = _rows(screen, 100)
    assert rows[0].startswith("❯ ") and rows[1].startswith("  ")
    assert "▸" not in "\n".join(rows)  # the old odd-one-out mark is gone
    raw = screen.render_body(100)[1]
    assert raw.startswith("\x1b[")  # the pointer carries the cursor style, not bare text


def test_livefeed_opens_pinned_and_the_pin_rides_the_newest_packet() -> None:
    """The top stop follows the stream: arrivals take the highlight with them.

    A live feed's resting state is *watching*, so the screen opens on the pin — drawn
    ``^`` rather than ``❯`` — and every packet that lands keeps the cursor on the very
    topmost row instead of being pushed down with the packet it was on.
    """
    screen = _screen(seed=[_obs(node="n0", age_s=1)])
    assert screen._pinned and screen._selected == 0
    assert _rows(screen, 100)[0].startswith("^ ")  # the pin's own mark

    screen.on_event(MeshEvent.observation_event(_obs(node="a1b2")))
    assert screen._selected == 0, "the pin let go of the top"
    rows = _rows(screen, 100)
    assert rows[0].startswith("^ ") and rows[1].startswith("  ")
    assert "Alice" in rows[0]  # …and it is the *newly arrived* packet under the cursor


def test_livefeed_down_off_the_pin_selects_the_newest_packet_itself() -> None:
    """``↓`` off the pin does not move a row — it changes what the cursor is attached to.

    The two stops share the top line: the pin holds the position, a selection holds the
    packet. So the first ``↓`` lands on that same newest packet under a plain ``❯``, and
    only the *second* reaches the packet below it. ``↑`` re-pins.
    """
    screen = _screen(seed=[_obs(node="n0", age_s=2), _obs(node="n1", age_s=1)])
    screen.handle("down")
    assert screen._selected == 0 and not screen._pinned, "↓ skipped past the newest packet"
    assert _rows(screen, 100)[0].startswith("❯ ")

    # Now the highlight belongs to that packet: an arrival pushes it down, as ever.
    screen.on_event(MeshEvent.observation_event(_obs(node="a1b2")))
    assert screen._selected == 1

    screen.handle("down")
    assert screen._selected == 2 and not screen._pinned  # the next ↓ really does move
    screen.handle("up")
    screen.handle("up")
    assert screen._selected == 0 and not screen._pinned  # back on the newest packet…
    screen.handle("up")
    assert screen._pinned  # …and one more ↑ re-pins


def test_livefeed_home_resumes_following_from_deep_in_the_history() -> None:
    """Home lands on the pin, not merely on whichever packet is newest right now."""
    screen = _screen(seed=[_obs(node=f"n{i}", age_s=i) for i in range(10)])
    screen.handle("end")
    assert screen._selected == 9 and not screen._pinned  # the oldest packet
    screen.handle("home")
    assert screen._pinned and screen._selected == 0
    screen.on_event(MeshEvent.observation_event(_obs(node="a1b2")))
    assert screen._selected == 0  # following again, not parked on the old newest


def test_livefeed_opening_a_packet_drops_the_pin() -> None:
    """Enter names *this* packet, so arrivals during a long read can't walk off it."""
    screen = _screen(seed=[_obs(node="n0", age_s=1)])
    screen.future = _Fut()
    assert screen._pinned
    screen._select_row(0)  # what _open_packet does before floating the viewer
    assert not screen._pinned and screen._selected == 0
    screen.on_event(MeshEvent.observation_event(_obs(node="a1b2")))
    assert screen._selected == 1, "the cursor left the packet being viewed"


def test_livefeed_viewer_that_walks_up_to_the_stream_re_pins_the_feed() -> None:
    """The viewer carries this screen's second stop, and hands the pin back through it.

    Enter still names *this* packet, so the dialog opens holding one. Walking up off the
    top inside it is the same move as walking up off the top out here, so the feed
    follows the stream again — and is still following when the dialog closes over it.
    """
    screen = _screen(seed=[_obs(node="n0", age_s=2), _obs(node="n1", age_s=1)])
    screen.handle("enter")
    viewer = screen._session.opened
    assert viewer is not None and not screen._pinned

    viewer.handle("up")
    assert viewer._pinned and "following" in viewer.title
    assert screen._pinned, "the feed stayed parked while the viewer followed the stream"

    # One arrival, both stops: the feed's cursor holds the top and so does the card.
    screen.on_event(MeshEvent.observation_event(_obs(node="a1b2")))
    assert screen._selected == 0
    assert "Alice" in _plain(viewer.render_body(72))


def test_livefeed_page_keys_move_the_feed_selection() -> None:
    """PgUp/PgDn walk the cursor a windowful at a time, clamped to the list's two ends."""
    seed = [_obs(node=f"n{i}", age_s=i) for i in range(20)]
    screen = _screen(seed=seed)
    screen._feed_window.page = 5  # as if the last paint settled a five-row window
    assert screen._selected == 0  # the newest packet is highlighted from the start
    screen.handle("pagedown")
    # A page is five *stops*, and the pin is the first of them (see LiveFeedScreen._cursor),
    # so paging off it lands on row 4 — the same five positions every other page travels.
    assert screen._selected == 4  # the selection travelled down a page, not just the view
    screen.handle("pageup")
    assert screen._selected == 0 and screen._pinned
    screen.handle("pageup")
    assert screen._selected == 0  # …and stops at the pin rather than wrapping

    for _ in range(5):  # paged past the oldest packet, the cursor clamps on it
        screen.handle("pagedown")
    assert screen._selected == 19


def test_livefeed_carries_no_exit_row() -> None:
    """The screen ends on the feed itself: Esc leaves, so no row is spent saying so.

    The window still fits the viewport with the chrome struck off it — losing the exit
    group gave its two lines back to the packets rather than to a gap.
    """
    seed = [_obs(node=f"n{i}", age_s=i) for i in range(40)]
    screen = _screen(seed=seed)
    screen.note_viewport(24)
    lines = _stripped(screen.render_body(100))
    assert len(lines) <= 24  # the body fits, it doesn't grow
    assert "Back" not in " ".join(lines)
    assert lines[-1].strip()  # the last line is content, not a gap
    assert "↓" in lines[-1] and "more" in lines[-1]  # …the marker counting what's below

    # The heading and column header stay put as the feed scrolls under them.
    screen.handle("end")
    after = _stripped(screen.render_body(100))
    assert "↑" in after[1] and "more" in after[1]
    assert "Back" not in " ".join(after)


def test_livefeed_cursor_stops_on_the_oldest_packet() -> None:
    """↓ clamps on the oldest packet and End jumps to it — there is no row past it."""
    screen = _screen(seed=[_obs(node="n0", age_s=1), _obs(node="n1", age_s=0)])
    screen.future = _Fut()

    screen.handle("down")  # off the pin, onto the newest
    screen.handle("down")
    assert screen._selected == 1  # the oldest packet
    screen.handle("down")
    assert screen._selected == 1  # …and the cursor stops there
    screen.handle("up")
    assert screen._selected == 0
    screen.handle("end")
    assert screen._selected == 1  # End is the oldest packet

    screen.handle("escape")  # …and Esc is the way out
    assert screen.future.done() and screen.future.value is None


def test_livefeed_empty_feed_draws_its_note_and_nothing_else() -> None:
    """With nothing heard there is no cursor at all — just the note, and Esc to leave."""
    screen = _screen()
    screen.future = _Fut()
    lines = _stripped(screen.render_body(100))
    assert "nothing heard yet" in lines[0]
    assert "Back" not in " ".join(lines)
    assert not any(line.startswith("❯") for line in lines)  # nothing to point at
    screen.handle("up")  # nowhere to go, and no crash on the way
    assert screen._selected is None and not screen._pinned
    screen.handle("enter")  # …and Enter opens nothing rather than leaving by accident
    assert not screen.future.done()


def test_livefeed_windows_inside_the_fixed_screen() -> None:
    """The header stays pinned: the body fits the viewport and the feed rows window."""
    seed = [_obs(node=f"n{i}", age_s=i) for i in range(40)]
    screen = _screen(seed=seed)
    screen.note_viewport(24)
    lines = screen.render_body(100)
    assert len(lines) <= 24  # column header + feed window == the viewport, never more
    body = _plain(lines)
    assert "TIME" in body  # the pinned column header is still there
    assert "↓" in body and "more" in body  # hidden feed rows are counted below


#: A terminal too narrow to hold the feed's fixed lanes — the only thing that makes a
#: row overflow now that no route rides in it (see ``_FEED_LABEL_MIN_WIDTH``).
_CRAMPED = 44


def test_livefeed_the_whole_row_fits_a_wide_screen() -> None:
    """No row overflows where the lanes fit — which is when ←→ has nothing to advertise."""
    screen = _screen(seed=[_obs(node="n0", kind="telemetry")])
    for line in _stripped(screen.render_body(100)):
        assert len(line.rstrip()) <= 100
    assert screen._hmax == 0
    assert "←→" not in screen.footer_hint


def test_livefeed_highlighted_row_scrolls_sideways_to_its_tail() -> None:
    """On a terminal too narrow for the lanes, ←→ slide the highlighted row to its end."""
    screen = _screen(seed=[_obs()])
    opening = _rows(screen, _CRAMPED)[0]
    assert screen._hmax > 0  # the row does run past the right edge
    assert not opening.rstrip().endswith("dBm")  # …so its reception tail is off-screen

    for _ in range(40):  # → saturates at the row's own end, never past it
        screen.handle("right")
    scrolled = _rows(screen, _CRAMPED)[0]
    assert screen._hshift == screen._hmax
    # The cursor lane holds its two cells while the row slides under it — here carrying
    # the pin's ``^``, since a feed just opened is following the stream.
    assert scrolled.startswith("^ ")
    assert scrolled.rstrip().endswith("-90 dBm")  # the tail is now readable
    assert opening[2:10] not in scrolled  # …at the cost of the time lane, slid off left

    for _ in range(40):
        screen.handle("left")
    assert screen._hshift == 0
    assert _rows(screen, _CRAMPED)[0] == opening  # back where it started


def test_livefeed_moving_the_selection_abandons_the_rows_scroll() -> None:
    """Each row scrolls on its own: landing on another packet starts it at its beginning."""
    screen = _screen(seed=[_obs(node="n0", age_s=1), _obs(node="n1", age_s=0)])
    screen.render_body(_CRAMPED)
    screen.handle("right")
    assert screen._hshift > 0
    screen.handle("down")
    assert screen._hshift == 0


def test_livefeed_advertises_line_scroll_only_where_it_acts() -> None:
    """←→ earns its footer atom exactly while the highlighted row overflows."""
    screen = _screen(seed=[_obs()])
    screen.render_body(100)
    assert "←→" not in screen.footer_hint  # a row with room to spare scrolls nowhere

    screen.render_body(_CRAMPED)
    assert "←→ scroll line" in screen.footer_hint
    assert len(screen.footer_hint) <= 72  # the screens-at-72 rule, fullest state
