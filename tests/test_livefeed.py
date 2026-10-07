# SPDX-License-Identifier: Apache-2.0
"""Live feed tests: the state, the rows, and the list window of the feed screen.

The tests drive the screen without a terminal. They use fake sessions and canned
observations, the same as the dashboard tests. This screen was first the feed panel of the
dashboard.
"""

from __future__ import annotations

import re
from datetime import timedelta

from rich.cells import cell_len

from meshterm.core.events import MeshEvent
from meshterm.core.models import Ack, Message, Observation, utcnow
from meshterm.ui.livefeed_screen import _ICON_LANE, LiveFeedScreen
from tests.conftest import plain as _plain  # strips the ANSI, joins the text


class _FakeSession:
    def __init__(self) -> None:
        self.repaints = 0
        #: The last screen that the feed floated over itself (the packet viewer).
        self.opened = None

    def invalidate(self) -> None:
        self.repaints += 1

    def run_screen(self, screen):
        """Record the floated screen. Do not run it, because there is no loop here."""
        self.opened = screen

    def run_detached(self, coro) -> None:
        """The start of a task that the session does not wait for.

        ``run_screen`` already did the recording.
        """


class _Fut:
    """A minimal replacement for a future, so that the screen can resolve without an event loop."""

    def __init__(self) -> None:
        self.value = None
        self._done = False

    def done(self) -> bool:
        return self._done

    def set_result(self, value) -> None:
        self.value = value
        self._done = True


#: The nodes that the resolver of the feed can name. The keys are the hash that a frame uses
#: for the address (one byte) and the longer prefixes that an advert has.
_KNOWN = {"a1b2": "Alice", "3d63": "Hub", "a1": "Alice", "3d": "Hub", "c0": "Us"}


def _screen(seed=None, **kwargs) -> LiveFeedScreen:
    screen = LiveFeedScreen(
        session=_FakeSession(),
        resolve=lambda h: _KNOWN.get(h, ""),
        seed=list(seed or []),
        **kwargs,
    )
    screen.note_viewport(48)  # the frame records this before each real paint
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
    """The rendered lines without the ANSI escapes, for assertions about the structure."""
    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]


def _rows(screen: LiveFeedScreen, width: int) -> list[str]:
    """Only the packet rows: the body after its column header."""
    return _stripped(screen.render_body(width))[1:]


def _col(line: str, needle: str) -> int:
    """The display column (in cells, not characters) where ``needle`` starts in ``line``."""
    return cell_len(line[: line.index(needle)])


def test_livefeed_tool_registers_under_the_dashboard() -> None:
    """The livefeed tool is in the Watch section, directly below the dashboard."""
    from meshterm.tools import load_all_tools
    from meshterm.tools.base import all_tools

    load_all_tools()
    tools = {t.name: t for t in all_tools()}
    tool = tools["livefeed"]
    assert tool.title == "Live feed" and tool.category == "Watch"
    assert tools["dashboard"].category == tool.category
    assert tools["dashboard"].order < tool.order < tools["watchtower"].order


def test_livefeed_live_events_land_in_the_feed() -> None:
    """Each observation, message, and ack goes into the feed when it arrives."""
    screen = _screen()
    screen.on_event(MeshEvent.observation_event(_obs(snr=7.5)))
    screen.on_event(MeshEvent.message_event(Message(text="hi", sender="a1b2")))
    screen.on_event(MeshEvent.ack_event(Ack(code="01c3")))
    body = _plain(screen.render_body(100))
    assert "+7.5 dB" in body
    assert "message" in body and "ack" in body
    # The newest is first: the ack row is above the message row, and the message row is above
    # the advert.
    assert body.index("ack") < body.index("message") < body.index("advert")


def test_livefeed_rows_leave_the_relay_path_to_the_viewer() -> None:
    """A row shows what arrived and how well it was heard. The Enter key shows the route.

    The cells that the fixed lanes left for a route were never enough to draw one. The route
    was always elided to a stub. Thus the feed has no route. The row is its fixed lanes,
    with the class name, and a wide terminal shows the whole row.
    """
    screen = _screen()
    screen.on_event(MeshEvent.observation_event(_obs(kind="packet", path="3d63,a1b2", snr=1.0)))
    row = _rows(screen, 100)[0]
    assert "via" not in row and "Hub" not in row  # no route, not even a stub of one
    assert "📦 packet" in row  # the class, in words
    assert "+1.0 dB" in row and "-90 dBm" in row  # and the reception where it was heard


def test_livefeed_class_lane_names_the_payload_class_once() -> None:
    """The feed files a raw frame under its own class, and the subject lane does not repeat it.

    ``packet`` is only the name of the event family that the frame arrived in. Thus the
    class lane shows the parsed payload class. This is the same label as the heading of
    the card in the viewer. The subject lane is then free for the packet's own
    subject. If the class has no subject, the lane shows a dash. It does not show the class
    a second time.
    """
    screen = _screen()
    raw = {"payload_typename": "MULTIPART", "route_typename": "FLOOD"}
    screen.on_event(
        MeshEvent.observation_event(
            _obs(node="", kind="packet", snr=1.0, path="3d63,a1b2", raw=raw)
        )
    )
    row = _rows(screen, 100)[0]
    assert "🧩 multipart" in row  # the class lane, with the payload class's own icon
    assert row.count("multipart") == 1  # one time, not one time for each lane
    assert "packet" not in row  # and never as the generic event family
    assert "—" in row  # the subject lane: the class has no subject
    assert "?" not in row  # and never the placeholder that has no use


def test_livefeed_names_a_channel_message_by_its_sender() -> None:
    """The ``Name:`` prefix of a channel message names the subject lane, not the bare channel."""
    screen = _screen()
    screen.on_event(
        MeshEvent.message_event(Message(text="Alice: hi all", channel=3, is_channel=True))
    )
    body = _plain(screen.render_body(100))
    assert "Alice" in body  # the parsed sender is at the start of the row
    assert "ch 3" in body  # and the channel stays as the context at the end


def test_livefeed_unsigned_channel_post_is_filed_under_its_channel() -> None:
    """If nobody signs a channel message, it is about the channel. The row names it one time."""
    screen = _screen(channel_names={3: "Alerts"})
    screen.on_event(MeshEvent.message_event(Message(text="beep boop", channel=3, is_channel=True)))
    row = _rows(screen, 100)[0]
    assert "Alerts" in row  # the channel name, not its slot number
    assert row.count("Alerts") == 1  # and the note at the end does not say it again
    assert "ch 3" not in row


def test_livefeed_channel_frame_is_about_its_channel() -> None:
    """The feed files a channel frame that it heard under the channel whose key confirms its MAC."""
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

    # If we have no key for a channel, only the fingerprint that the frame has can name the
    # channel. A name is never used, because a fingerprint alone does not prove which
    # channel it is.
    other = _screen(channels=[("Public", secret)])
    other.on_event(
        MeshEvent.observation_event(_obs(node="", kind="packet", raw={**raw, "chan_hash": "a3"}))
    )
    row = _rows(other, 100)[0]
    assert "hash a3" in row and "Public" not in row


def test_livefeed_addressed_frame_is_about_its_two_ends() -> None:
    """A frame that names a recipient shows as sender → recipient, with the names resolved."""
    screen = _screen(self_name="Us")
    raw = {"payload_typename": "TEXT_MSG", "dest_hash": "c0", "src_hash": "a1"}
    screen.on_event(MeshEvent.observation_event(_obs(node="", kind="packet", raw=raw)))
    row = _rows(screen, 100)[0]
    assert "📩 dir messg" in row
    # This includes our node, when a frame is addressed to us. The row shows it as the ★ of
    # the app. The user does not need a name for this endpoint, and the cells are necessary
    # for the name of the other end.
    assert "Alice → ★" in row

    # If a pair is too wide for the lane, the cells go to the addressee. The sender changes
    # to the hash that named it, so that the recipient is not the half that is cut off.
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
    """An ack names the message that it answers. A trace names its tag. Neither says "packet"."""
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
    """The header names each lane, and each label is over the values under it."""
    screen = _screen(seed=[_obs(node="3d63", kind="telemetry", snr=12.8, rssi=-105.0)])
    header, row = _stripped(screen.render_body(90))[:2]
    assert header.split() == ["TIME", "CLASS", "SUBJECT", "SCOPE", "SNR", "RSSI"]
    # The test measures in display cells, not characters. The class icon is one character
    # but two cells wide. A character index puts each later lane one column too early. The
    # test reads the clock from the row itself. A literal needle matches only in the
    # minute when it was written.
    stamp = re.search(r"\d\d:\d\d:\d\d", row)
    assert stamp is not None
    assert _col(header, "TIME") == _col(row, stamp.group(0))
    assert _col(header, "CLASS") == _col(row, "telemetry") - _ICON_LANE
    assert _col(header, "SUBJECT") == _col(row, "Hub")
    # The label of each reading starts where its lane starts, the same as each other label.
    assert _col(header, "SNR") == _col(row, "+12.8")
    assert _col(header, "RSSI") == _col(row, "-105")
    # The one gap of the row separates the readings, not the width of a spare field.
    assert "+12.8 dB  -105 dBm" in row


def test_livefeed_column_header_is_pinned_and_carries_no_sort_cue() -> None:
    """The header does not scroll away, and it shows no sort mark. The feed has one order."""
    screen = _screen(seed=[_obs(node=f"n{i}", age_s=i) for i in range(40)])
    screen.note_viewport(12)
    lines = _stripped(screen.render_body(100))
    assert "TIME" in lines[0]
    assert not any(mark in lines[0] for mark in ("▲", "▼"))  # nothing here sorts
    screen.handle("end")  # the last row of the list
    screen.handle("up")  # one up from it, the oldest packet: the list window scrolls
    after = _stripped(screen.render_body(100))
    assert after[0] == lines[0]  # the header is in the same place as before
    assert "↑" in after[1] and "more" in after[1]  # the rows did move


def test_livefeed_highlight_uses_the_app_wide_cursor() -> None:
    """The feed marks its row as each other list does: ``❯`` over a ``cursor``-white row."""
    screen = _screen(seed=[_obs(node="n0", age_s=1), _obs(node="n1", age_s=0)])
    screen.handle("down")  # off the pin, onto the newest packet that is normally selected
    rows = _rows(screen, 100)
    assert rows[0].startswith("❯ ") and rows[1].startswith("  ")
    assert "▸" not in "\n".join(rows)  # the old mark that was different from the others is gone
    raw = screen.render_body(100)[1]
    assert raw.startswith("\x1b[")  # the pointer has the cursor style, not bare text


def test_livefeed_opens_pinned_and_the_pin_rides_the_newest_packet() -> None:
    """The top stop follows the stream: the highlight moves with each arrival.

    The normal state of a live feed is to watch. Thus the screen opens on the pin, drawn as
    ``^`` instead of ``❯``. Each packet that arrives keeps the highlight on the top row. The
    highlight does not move down with the packet that it was on.
    """
    screen = _screen(seed=[_obs(node="n0", age_s=1)])
    assert screen._pinned and screen._selected == 0
    assert _rows(screen, 100)[0].startswith("^ ")  # the pin's own mark

    screen.on_event(MeshEvent.observation_event(_obs(node="a1b2")))
    assert screen._selected == 0, "the pin let go of the top"
    rows = _rows(screen, 100)
    assert rows[0].startswith("^ ") and rows[1].startswith("  ")
    assert "Alice" in rows[0]  # and the highlight is on the packet that arrived last


def test_livefeed_down_off_the_pin_selects_the_newest_packet_itself() -> None:
    """``↓`` off the pin does not move a row. It changes what the highlight is fixed to.

    The two stops share the top line. The pin holds the position, and a selection holds the
    packet. Thus the first ``↓`` goes to the same newest packet, under a plain ``❯``. Only
    the second ``↓`` goes to the packet below it. ``↑`` puts the pin back.
    """
    screen = _screen(seed=[_obs(node="n0", age_s=2), _obs(node="n1", age_s=1)])
    screen.handle("down")
    assert screen._selected == 0 and not screen._pinned, "↓ skipped past the newest packet"
    assert _rows(screen, 100)[0].startswith("❯ ")

    # Now the highlight is on that packet: an arrival pushes it down, as before.
    screen.on_event(MeshEvent.observation_event(_obs(node="a1b2")))
    assert screen._selected == 1

    screen.handle("down")
    assert screen._selected == 2 and not screen._pinned  # the next ↓ does move
    screen.handle("up")
    screen.handle("up")
    assert screen._selected == 0 and not screen._pinned  # back on the newest packet
    screen.handle("up")
    assert screen._pinned  # one more ↑ puts the pin back


def test_livefeed_home_resumes_following_from_deep_in_the_history() -> None:
    """Home goes to the pin, not only to the packet that is the newest at this time."""
    screen = _screen(seed=[_obs(node=f"n{i}", age_s=i) for i in range(10)])
    screen.handle("end")
    assert screen._selected == 9 and not screen._pinned  # the oldest packet
    screen.handle("home")
    assert screen._pinned and screen._selected == 0
    screen.on_event(MeshEvent.observation_event(_obs(node="a1b2")))
    assert screen._selected == 0  # the feed follows again, and does not stay on the old newest


def test_livefeed_opening_a_packet_drops_the_pin() -> None:
    """Enter names this packet, so an arrival during a long read cannot move the highlight."""
    screen = _screen(seed=[_obs(node="n0", age_s=1)])
    screen.future = _Fut()
    assert screen._pinned
    screen._select_row(0)  # this is what _open_packet does before it floats the viewer
    assert not screen._pinned and screen._selected == 0
    screen.on_event(MeshEvent.observation_event(_obs(node="a1b2")))
    assert screen._selected == 1, "the cursor left the packet being viewed"


def test_livefeed_viewer_that_walks_up_to_the_stream_re_pins_the_feed() -> None:
    """The viewer has the second stop of this screen, and it returns the pin through it.

    Enter still names this packet, so the dialog opens with one packet. The user can press
    up at the top of the viewer. This is the same move as up at the top of the feed. Thus
    the feed follows the stream again, and it still follows when the dialog closes.
    """
    screen = _screen(seed=[_obs(node="n0", age_s=2), _obs(node="n1", age_s=1)])
    screen.handle("enter")
    viewer = screen._session.opened
    assert viewer is not None and not screen._pinned

    viewer.handle("up")
    assert viewer._pinned and "following" in viewer.title
    assert screen._pinned, "the feed stayed parked while the viewer followed the stream"

    # After one arrival, both stops are at the top: the highlight of the feed and the card.
    screen.on_event(MeshEvent.observation_event(_obs(node="a1b2")))
    assert screen._selected == 0
    assert "Alice" in _plain(viewer.render_body(72))


def test_livefeed_page_keys_move_the_feed_selection() -> None:
    """PgUp/PgDn move the highlight one list window at a time. The two ends of the list clamp it."""
    seed = [_obs(node=f"n{i}", age_s=i) for i in range(20)]
    screen = _screen(seed=seed)
    screen._feed_window.page = 5  # as if the last paint set a list window of five rows
    assert screen._selected == 0  # the newest packet is highlighted from the start
    screen.handle("pagedown")
    # A page is five stops, and the pin is the first of them (refer to
    # LiveFeedScreen._cursor). Thus a page down from the pin goes to row 4. This is the same
    # five positions as each other page.
    assert screen._selected == 4  # the selection moved down a page, not only the viewport
    screen.handle("pageup")
    assert screen._selected == 0 and screen._pinned
    screen.handle("pageup")
    assert screen._selected == 0  # and it stops at the pin instead of wrapping

    for _ in range(5):  # after pages past the oldest packet, the highlight clamps on it
        screen.handle("pagedown")
    assert screen._selected == 19


def test_livefeed_carries_no_exit_row() -> None:
    """The screen ends on the feed itself. Esc leaves, so no row says that.

    The list window still fits the viewport when the frame removes the chrome. The two lines
    of the removed exit group went to the packets, not to a gap.
    """
    seed = [_obs(node=f"n{i}", age_s=i) for i in range(40)]
    screen = _screen(seed=seed)
    screen.note_viewport(24)
    lines = _stripped(screen.render_body(100))
    assert len(lines) <= 24  # the body fits, and it does not grow
    assert "Back" not in " ".join(lines)
    assert lines[-1].strip()  # the last line is content, not a gap
    assert "↓" in lines[-1] and "more" in lines[-1]  # the marker that counts the rows below

    # The heading and the column header stay in place while the feed scrolls under them.
    screen.handle("end")
    after = _stripped(screen.render_body(100))
    assert "↑" in after[1] and "more" in after[1]
    assert "Back" not in " ".join(after)


def test_livefeed_cursor_stops_on_the_oldest_packet() -> None:
    """↓ clamps on the oldest packet and End goes to it. There is no row after it."""
    screen = _screen(seed=[_obs(node="n0", age_s=1), _obs(node="n1", age_s=0)])
    screen.future = _Fut()

    screen.handle("down")  # off the pin, onto the newest
    screen.handle("down")
    assert screen._selected == 1  # the oldest packet
    screen.handle("down")
    assert screen._selected == 1  # and the highlight stops there
    screen.handle("up")
    assert screen._selected == 0
    screen.handle("end")
    assert screen._selected == 1  # End goes to the oldest packet

    screen.handle("escape")  # Esc is the way out
    assert screen.future.done() and screen.future.value is None


def test_livefeed_empty_feed_draws_its_note_and_nothing_else() -> None:
    """If nothing is heard, there is no highlight. The screen shows only the note."""
    screen = _screen()
    screen.future = _Fut()
    lines = _stripped(screen.render_body(100))
    assert "nothing heard yet" in lines[0]
    assert "Back" not in " ".join(lines)
    assert not any(line.startswith("❯") for line in lines)  # nothing to point at
    screen.handle("up")  # there is no place to go, and no crash
    assert screen._selected is None and not screen._pinned
    screen.handle("enter")  # Enter opens nothing, and it does not leave by accident
    assert not screen.future.done()


def test_livefeed_windows_inside_the_fixed_screen() -> None:
    """The header stays pinned. The body fits the viewport, and the feed rows scroll in it."""
    seed = [_obs(node=f"n{i}", age_s=i) for i in range(40)]
    screen = _screen(seed=seed)
    screen.note_viewport(24)
    lines = screen.render_body(100)
    assert len(lines) <= 24  # the column header and the list window equal the viewport, never more
    body = _plain(lines)
    assert "TIME" in body  # the pinned column header is there
    assert "↓" in body and "more" in body  # the marker counts the hidden feed rows below


#: A terminal too narrow for the fixed lanes of the feed. This is the only cause of a row
#: overflow now, because a row has no route (refer to ``_FEED_LABEL_MIN_WIDTH``).
_CRAMPED = 44


def test_livefeed_the_whole_row_fits_a_wide_screen() -> None:
    """No row overflows where the lanes fit. Then ←→ has nothing to show in the hint."""
    screen = _screen(seed=[_obs(node="n0", kind="telemetry")])
    for line in _stripped(screen.render_body(100)):
        assert len(line.rstrip()) <= 100
    assert screen._hmax == 0
    assert "←→" not in screen.footer_hint


def test_livefeed_highlighted_row_scrolls_sideways_to_its_tail() -> None:
    """On a terminal too narrow for the lanes, ←→ move the highlighted row sideways to its end."""
    screen = _screen(seed=[_obs()])
    opening = _rows(screen, _CRAMPED)[0]
    assert screen._hmax > 0  # the row does go past the right edge
    assert not opening.rstrip().endswith("dBm")  # thus its reception tail is not visible

    for _ in range(40):  # → stops at the end of the row, never past it
        screen.handle("right")
    scrolled = _rows(screen, _CRAMPED)[0]
    assert screen._hshift == screen._hmax
    # The cursor lane keeps its two cells while the row slides under it. Here it has the
    # ``^`` of the pin, because a feed that just opened follows the stream.
    assert scrolled.startswith("^ ")
    assert scrolled.rstrip().endswith("-90 dBm")  # the user can read the tail now
    assert opening[2:10] not in scrolled  # the time lane slid off to the left

    for _ in range(40):
        screen.handle("left")
    assert screen._hshift == 0
    assert _rows(screen, _CRAMPED)[0] == opening  # back where it started


def test_livefeed_moving_the_selection_abandons_the_rows_scroll() -> None:
    """Each row scrolls on its own. A newly highlighted packet starts at its beginning."""
    screen = _screen(seed=[_obs(node="n0", age_s=1), _obs(node="n1", age_s=0)])
    screen.render_body(_CRAMPED)
    screen.handle("right")
    assert screen._hshift > 0
    screen.handle("down")
    assert screen._hshift == 0


def test_livefeed_advertises_line_scroll_only_where_it_acts() -> None:
    """The footer has the ←→ atom only while the highlighted row overflows."""
    screen = _screen(seed=[_obs()])
    screen.render_body(100)
    assert "←→" not in screen.footer_hint  # a row with room to spare does not scroll

    screen.render_body(_CRAMPED)
    assert "←→ scroll line" in screen.footer_hint
    assert len(screen.footer_hint) <= 72  # the rule for screens at 72 cells, in the fullest state
