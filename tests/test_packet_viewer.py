# SPDX-License-Identifier: Apache-2.0
"""Packet viewer tests: per-kind flavouring, the raw-field dedup, and channel decrypt.

The viewer takes Rich content in and gives ANSI lines out (``render_body``). Thus the tests
run it with no terminal, against a synthetic :class:`PacketEntry`. The dashboard tests use
the same method.
"""

from __future__ import annotations

import re

from Crypto.Cipher import AES
from Crypto.Hash import HMAC, SHA256

from meshterm.core.channels import channel_hash, derive_secret
from meshterm.core.models import utcnow
from meshterm.platforms import set_platform
from meshterm.ui.packet_viewer import PacketEntry, PacketViewer
from tests.conftest import plain as _plain  # the one function that strips and joins the screen


def _stripped(lines: list[str]) -> list[str]:
    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]


def _grp_txt_entry(raw_extra: dict) -> PacketEntry:
    raw = {"payload_typename": "GRP_TXT", "route_typename": "FLOOD", **raw_extra}
    return PacketEntry(when=utcnow(), kind="packet", path="a1b2c3d4e5f6", raw=raw)


def _viewer(entry: PacketEntry, channels=()) -> PacketViewer:
    return PacketViewer([entry], 0, resolve=lambda h: "", channels=channels)


def _grp_txt_frame(secret: bytes, text: str) -> tuple[str, str, str]:
    """A GRP_TXT frame for ``text``, in the firmware shape, encrypted with ``secret``."""
    plain = (0).to_bytes(4, "little") + bytes([0]) + text.encode("utf-8")
    plain += b"\x00" * (-len(plain) % 16)
    crypted = AES.new(secret, AES.MODE_ECB).encrypt(plain)
    mac = HMAC.new(secret, digestmod=SHA256)
    mac.update(crypted)
    return channel_hash(secret), mac.digest()[:2].hex(), crypted.hex()


def test_packet_viewer_shows_class_and_route() -> None:
    """The parsed payload class of a raw packet is the headline of the card. The route is a row."""
    entry = _grp_txt_entry({"chan_hash": "ff", "cipher_mac": "0000", "crypted": "00" * 16})
    body = _plain(_viewer(entry).render_body(80))
    assert "📻 CHAN TEXT" in body
    assert "flood" in body


def test_packet_viewer_class_row_leads_the_card() -> None:
    """The class headline is the first row, above heard and from, for each kind."""
    packet = _grp_txt_entry({"chan_hash": "ff", "cipher_mac": "0000", "crypted": "00" * 16})
    body = _plain(_viewer(packet).render_body(80))
    assert body.index("CHAN TEXT") < body.index("heard")

    advert = PacketEntry(when=utcnow(), kind="advert", node="aa")
    body = _plain(_viewer(advert).render_body(80))
    assert "📢 ADVERT" in body
    assert body.index("ADVERT") < body.index("heard")


def test_packet_viewer_class_marks_an_unknown_typename() -> None:
    """A payload class that this build does not know keeps the ❔ mark and its raw name."""
    entry = PacketEntry(when=utcnow(), kind="packet", raw={"payload_typename": "XYZZY"})
    body = _plain(_viewer(entry).render_body(80))
    assert "❔ XYZZY" in body


def test_packet_viewer_title_carries_no_emoji() -> None:
    """Dialog titles have no emoji (a rule of the UX standards). The class row has the icon."""
    entry = PacketEntry(when=utcnow(), kind="advert", node="aa")
    assert _viewer(entry).title == "advert"


def test_packet_viewer_decrypts_a_known_channel() -> None:
    """A channel-text frame for which MeshTerm has the key decrypts to its plaintext."""
    name, secret = "#general", derive_secret("#general")
    chash, mac, crypted = _grp_txt_frame(secret, "hi mesh")
    entry = _grp_txt_entry({"chan_hash": chash, "cipher_mac": mac, "crypted": crypted})
    body = _plain(_viewer(entry, channels=[(name, secret)]).render_body(80))
    assert "#general" in body
    assert "hi mesh" in body


def test_packet_viewer_reports_an_unknown_channel() -> None:
    """A frame from a channel for which MeshTerm has no key says so, and does not guess."""
    entry = _grp_txt_entry({"chan_hash": "ab", "cipher_mac": "0000", "crypted": "00" * 16})
    body = _plain(_viewer(entry).render_body(80))
    assert "unknown" in body and "can't decrypt" in body


def test_packet_viewer_reaches_packets_that_arrive_after_open() -> None:
    """A live source lets the viewer page up to packets that arrive after it opens."""
    old = PacketEntry(when=utcnow(), kind="advert", node="aa")
    feed = [old]
    viewer = PacketViewer(list(feed), 0, resolve=lambda h: "", source=lambda: list(feed))
    assert viewer._entries[viewer._index] is old

    # A newer packet is prepended (the feed has the newest first) while the dialog shows the
    # old one. A paint adds it to the list, and the viewer stays on `old` (now at index 1).
    newer = PacketEntry(when=utcnow(), kind="message", node="bb", text="new!")
    feed.insert(0, newer)
    viewer.render_body(80)
    assert viewer._entries[viewer._index] is old
    assert "2/2" in viewer.title

    # ↑ (newer) now goes to the packet that arrived after the dialog opened.
    viewer.handle("up")
    assert viewer._entries[viewer._index] is newer
    assert "1/2" in viewer.title


def test_packet_viewer_heard_row_says_now_not_now_ago() -> None:
    """The heard row of a packet that was just heard reads "(now)", the grammar of format_ago."""
    entry = PacketEntry(when=utcnow(), kind="advert", node="aa")
    body = _plain(_viewer(entry).render_body(80))
    assert "(now)" in body
    assert "now ago" not in body


def test_packet_viewer_lane_names_the_ends_of_the_list_not_the_body() -> None:
    """Home and End go to the newest and the oldest packet here, so the Shift bank says so."""
    old = PacketEntry(when=utcnow(), kind="advert", node="aa")
    newer = PacketEntry(when=utcnow(), kind="message", node="bb", text="new!")

    lone = PacketViewer([old], 0, resolve=lambda h: "")
    lone.note_metrics(4, 20)  # a short body in a large box: nothing to page either
    assert [pair.opp_label for pair in lone.picocalc_lyra_lane[3:]] == ["Oldest", "Newest"]
    assert not any(pair.enabled or pair.opp_enabled for pair in lone.picocalc_lyra_lane[3:])

    # A second packet lights the jumps by themselves. The pager still needs a tall body.
    pair_view = PacketViewer([newer, old], 0, resolve=lambda h: "")
    pair_view.note_metrics(4, 20)
    assert [pair.opp_enabled for pair in pair_view.picocalc_lyra_lane[3:]] == [True, True]
    assert [pair.enabled for pair in pair_view.picocalc_lyra_lane[3:]] == [False, False]
    pair_view.note_metrics(80, 20)
    assert [pair.enabled for pair in pair_view.picocalc_lyra_lane[3:]] == [True, True]


def test_packet_viewer_follows_the_stream_off_the_top_of_a_live_list() -> None:
    """``↑`` at the newest packet goes to the second stop of the feed. It does not clamp.

    On the pin, the viewer holds the top position. Each arrival becomes the card that the
    user reads, the title says so, and ``↓`` steps back to the packet that is visible at
    that moment.
    """
    old = PacketEntry(when=utcnow(), kind="advert", node="aa")
    feed = [old]
    pinned_by_viewer = []
    viewer = PacketViewer(
        list(feed),
        0,
        resolve=lambda h: "",
        source=lambda: list(feed),
        on_pin=lambda: pinned_by_viewer.append(True),
    )
    assert not viewer._pinned, "opening a packet named that packet, not the stream"
    assert "↑ follow" in viewer.footer_hint

    viewer.handle("up")
    assert viewer._pinned and pinned_by_viewer == [True]
    assert viewer.title.endswith("· following")  # the word, not a constant 1/n
    assert "↓ hold packet" in viewer.footer_hint

    newer = PacketEntry(when=utcnow(), kind="message", node="bb", text="new!")
    feed.insert(0, newer)
    assert "new!" in _plain(viewer.render_body(80))  # the new packet is now the card
    assert viewer.title.endswith("· following")

    viewer.handle("down")
    assert not viewer._pinned and viewer._entries[viewer._index] is newer
    assert "1/2" in viewer.title  # the viewer holds that packet, which now moves down the list


def test_packet_viewer_over_a_snapshot_has_nothing_to_follow() -> None:
    """With no live source there is no pin. ``↑`` at the newest packet clamps, as before."""
    newer = PacketEntry(when=utcnow(), kind="message", node="bb", text="new!")
    old = PacketEntry(when=utcnow(), kind="advert", node="aa")
    viewer = PacketViewer([newer, old], 0, resolve=lambda h: "")

    viewer.handle("up")
    assert not viewer._pinned and "following" not in viewer.title
    assert viewer.footer_hint.startswith("↑↓ newer/older")
    viewer.handle("home")
    assert not viewer._pinned and viewer._index == 0


def test_packet_viewer_home_resumes_the_stream() -> None:
    """Home goes to the pin on a live list. This is Home of the feed, and its ``Newest`` chip.

    For this reason the jump chips also light on a live list that has only one packet.
    The viewer can go to the pin, also when there is no second packet.
    """
    feed = [PacketEntry(when=utcnow(), kind="advert", node=f"n{i}") for i in range(3)]
    viewer = PacketViewer(list(feed), 0, resolve=lambda h: "", source=lambda: list(feed))
    viewer.handle("end")
    assert viewer._index == 2 and not viewer._pinned  # the oldest packet
    viewer.handle("home")
    assert viewer._pinned

    lone = PacketViewer(feed[:1], 0, resolve=lambda h: "", source=lambda: feed[:1])
    lone.note_metrics(4, 20)  # a short body in a large box: nothing to page
    assert [pair.opp_enabled for pair in lone.picocalc_lyra_lane[3:]] == [True, True]


def test_packet_viewer_without_a_source_stays_a_snapshot() -> None:
    """With no live source, the viewer keeps the list that it opened with (the old behaviour)."""
    entry = PacketEntry(when=utcnow(), kind="advert", node="aa")
    viewer = PacketViewer([entry], 0, resolve=lambda h: "")
    viewer.render_body(80)
    assert viewer._entries == [entry]
    assert viewer.footer_hint == "Esc close"


def test_packet_viewer_grows_its_dialog_only() -> None:
    """The viewer uses a grow-only dialog. Paging makes the dialog larger and never smaller."""
    tiny = PacketEntry(when=utcnow(), kind="ack", where="01c3")
    viewer = PacketViewer([tiny], 0, resolve=lambda h: "")
    assert viewer.grow_only is True
    # The body keeps its natural height, with no padding. The frame pads it, not a floor.
    assert len(viewer.render_body(80)) < 10
    # ratchet_viewport is the grow-only contract: it rises to a taller body and never falls.
    assert viewer.ratchet_viewport(6) == 6
    assert viewer.ratchet_viewport(14) == 14  # a taller packet makes the box larger
    assert viewer.ratchet_viewport(4) == 14  # a shorter packet after it keeps the larger box


def test_packet_viewer_raw_dump_skips_fields_folded_into_flavoured_rows() -> None:
    """Fields that the class, route, via, and channel rows already show are not also dumped."""
    entry = _grp_txt_entry(
        {
            "chan_hash": "ab",
            "cipher_mac": "0000",
            "crypted": "00" * 16,
            "path_len": 1,
            "path_hash_size": 1,
            "header": 5,
            "novel_field": "surprise",
        }
    )
    body = _plain(_viewer(entry).render_body(80))
    assert "path_len" not in body
    assert "header" not in body
    assert "novel_field" in body  # an unknown field appears with its full name


def test_packet_viewer_shows_full_raw_field_labels() -> None:
    """A long raw-field name is shown whole. The label lane gets wider instead of a clip."""
    entry = _grp_txt_entry(
        {
            "chan_hash": "ab",
            "cipher_mac": "0000",
            "crypted": "00" * 16,
            "battery_millivolts": 4102,
        }
    )
    body = _plain(_viewer(entry).render_body(80))
    assert "battery_millivolts" in body  # the key has 18 characters and is not clipped to 8


def test_packet_viewer_draws_a_relayed_packets_route_graph() -> None:
    """A packet that crossed relays gets the route graph: braille edges, caption, and legend."""
    entry = PacketEntry(when=utcnow(), kind="packet", path="3d63,a1b2")
    viewer = _viewer(entry)
    viewer.note_viewport(30)  # the frame records this before each real paint
    body = _plain(viewer.render_body(80))
    assert "origin → you" in body  # the graph caption
    assert "★ you" in body and "▲ repeater" in body  # the node-type legend
    assert any("⠀" <= ch <= "⣿" for ch in body)  # the viewer draws braille edges
    assert "3d" in body and "a1" in body  # each relay has its hash byte as a label


def test_packet_viewer_draws_a_revisited_hop_twice_and_warns() -> None:
    """An overheard chain that names one hop twice draws it twice, and says why.

    Suppose the graph folded the hop to one marker. The walk would close a cycle that the
    left-to-right flow cannot place, and the whole graph would collapse into a pile that is
    one column wide. Thus the packet card splits the revisit. It must also warn the user,
    because a one-byte hash cannot show if the repeat is a real loop or two different nodes
    that have the same byte.
    """
    names = {"c1": "C14903", "8e": "MileEnd", "da": "Relais", "ee": "ParcEx"}
    entry = PacketEntry(when=utcnow(), kind="packet", path="7f,c1,8e,da,ee,c1,27")
    body = _plain(PacketViewer([entry], 0, resolve=lambda h: names.get(h, "")).render_body(80))
    assert "⚠" in body and "repeats" in body
    assert "a loop, or two nodes sharing one hash" in body
    assert body.count("C14903") == 3  # twice in the via row, once named in the warning
    assert body.count("c1") == 2  # also, both visits have a marker label in the graph


def test_packet_viewer_stays_quiet_when_every_hop_is_distinct() -> None:
    """The revisit warning is not chrome. A normal relayed packet never shows it."""
    entry = PacketEntry(when=utcnow(), kind="packet", path="3d63,a1b2")
    body = _plain(_viewer(entry).render_body(80))
    assert "⚠" not in body and "repeats" not in body


def test_packet_viewer_skips_the_graph_for_a_direct_packet() -> None:
    """A packet with no relays says so in the via row, and has no (useless) two-node graph."""
    entry = PacketEntry(when=utcnow(), kind="packet", path="")
    body = _plain(_viewer(entry).render_body(80))
    assert "direct — no relays" in body
    assert "origin → you" not in body
    assert not any("⠀" <= ch <= "⣿" for ch in body)


def test_packet_viewer_via_wraps_at_hop_boundaries_under_its_own_lane() -> None:
    """A long ``via`` chain wraps between hops and hangs under the value lane, never in a name."""
    names = {f"{i:02d}aa": f"Relay-Number-{i:02d}" for i in range(8)}
    entry = PacketEntry(when=utcnow(), kind="packet", path=",".join(names))
    lines = _stripped(PacketViewer([entry], 0, resolve=lambda h: names.get(h, "")).render_body(72))
    via_at = next(i for i, line in enumerate(lines) if line.startswith("via"))
    end = next(i for i, line in enumerate(lines) if line.strip() == "8 hops")
    folded = lines[via_at:end]
    assert len(folded) > 1  # a chain this long does not fit in one lane
    assert all(line.startswith(" " * 11) for line in folded[1:])  # hangs past the label lane
    for name in names.values():
        assert any(name in line for line in folded)  # each hop stays whole in the wrap
    assert all(line.rstrip().endswith("→") for line in folded[:-1])  # the "it continues" cue
    assert all(len(line.rstrip()) <= 72 for line in folded)


def test_packet_viewer_via_chain_is_drawn_as_the_middle_of_a_route() -> None:
    """``via`` names relays, so neither end of it is the start or the end of the frame.

    The chain opens and closes on the mark for "there is more": the chevron in chips, the
    bare separator in arrows. It does not use the flat end, because a flat end would claim
    that the first relay was the origin.
    """
    entry = PacketEntry(when=utcnow(), kind="packet", node="c0ffee", path="a1b2c3,d4e5f6")
    via = next(ln for ln in _stripped(_viewer(entry).render_body(80)) if ln.startswith("via"))
    chain = via.split(None, 1)[1].strip()
    assert chain.startswith("→ ") and chain.endswith(" →")
    assert chain.count("→") == 3  # both open ends, and the join between the two relays


def test_packet_viewer_from_row_leads_with_the_node_type_mark() -> None:
    """The sender has its shared node glyph: from the packet, from the contacts, or ``○``."""
    from meshterm.core.models import NODE_TYPE_REPEATER

    def from_row(viewer: PacketViewer) -> str:
        return next(ln for ln in _stripped(viewer.render_body(80)) if ln.startswith("from"))

    advertised = PacketEntry(
        when=utcnow(), kind="advert", node="3d63", name="Hub", node_type=NODE_TYPE_REPEATER
    )
    assert "▲ Hub" in from_row(_viewer(advertised))  # the type that the packet itself has

    bare = PacketEntry(when=utcnow(), kind="packet", node="3d63", name="Hub")
    assert "○ Hub" in from_row(_viewer(bare))  # nothing gives its type: the unknown ring
    typed = PacketViewer([bare], 0, resolve=lambda h: "", type_of=lambda h: NODE_TYPE_REPEATER)
    assert "▲ Hub" in from_row(typed)  # …until the contacts give it

    mine = PacketEntry(when=utcnow(), kind="advert", node="3d63", name="Waymarker")
    ours = PacketViewer([mine], 0, resolve=lambda h: "", self_name="Waymarker")
    assert "★ Waymarker" in from_row(ours)  # our node keeps the star of the app


def test_packet_viewer_graph_names_a_known_origin_else_a_question_mark() -> None:
    """The left endpoint of the graph is the resolved origin name, or ``?`` if there is none."""
    known = PacketEntry(when=utcnow(), kind="packet", node="c0ffee", path="3d63")
    body = _plain(
        PacketViewer([known], 0, resolve=lambda h: "Base" if h == "c0ffee" else "").render_body(80)
    )
    assert "Base" in body  # the origin, resolved to its contact name

    nameless = PacketEntry(when=utcnow(), kind="packet", path="3d63")
    body2 = _plain(_viewer(nameless).render_body(80))
    assert "?" in body2  # a flood with no origin has a "?" endpoint


def test_packet_viewer_lays_out_what_a_frame_addressed() -> None:
    """A frame that names no origin still names its two ends, as rows and not as raw hex.

    The endpoints come from the frame body (``meshterm.core.frames``). Thus the card can
    say for whom a relayed direct message was, although the class has no origin node.
    """
    entry = PacketEntry(
        when=utcnow(),
        kind="packet",
        path="3d63",
        raw={
            "payload_typename": "TEXT_MSG",
            "route_typename": "FLOOD",
            "dest_hash": "c0",
            "src_hash": "a1",
        },
    )
    viewer = PacketViewer(
        [entry],
        0,
        resolve=lambda h: {"c0": "Waymarker", "a1": "Alice"}.get(h, ""),
        prefix_bytes=1,
        self_name="Waymarker",
    )
    lines = _stripped(viewer.render_body(80))
    to_row = next(ln for ln in lines if ln.startswith("to"))
    from_row = next(ln for ln in lines if ln.startswith("from"))
    assert "Waymarker" in to_row and "c0" in to_row  # the name, then the hash that named it
    assert "Alice" in from_row and "a1" in from_row
    # …and the generic raw dump at the end of the card does not repeat either one.
    assert _plain(lines).count("dest_hash") == 0


def test_packet_viewer_names_a_tokened_frame_by_its_token() -> None:
    """An ack points to the message that it answers. A trace points to its own tag."""
    ack = PacketEntry(
        when=utcnow(),
        kind="packet",
        path="3d63",
        raw={"payload_typename": "ACK", "ack_crc": "9b71e004"},
    )
    assert "9b71e004" in _plain(_viewer(ack).render_body(80))

    trace = PacketEntry(
        when=utcnow(),
        kind="packet",
        path="3d63",
        raw={"payload_typename": "TRACE", "trace_tag": "5f3c2a10"},
    )
    body = _plain(_viewer(trace).render_body(80))
    assert "tag" in body and "5f3c2a10" in body


def test_packet_viewer_names_a_channel_datagram_by_its_confirmed_channel() -> None:
    """The body of a datagram is not text, but the MAC that guards it names its channel."""
    secret = derive_secret("Public")
    crypted = bytes(range(16))
    mac = HMAC.new(secret, digestmod=SHA256)
    mac.update(crypted)
    entry = PacketEntry(
        when=utcnow(),
        kind="packet",
        path="3d63",
        raw={
            "payload_typename": "GRP_DATA",
            "chan_hash": channel_hash(secret),
            "cipher_mac": mac.digest()[:2].hex(),
            "crypted": crypted.hex(),
        },
    )
    body = _plain(_viewer(entry, channels=[("Public", secret)]).render_body(80))
    assert "💽 CHAN DATA" in body and "Public" in body

    unknown = _plain(_viewer(entry).render_body(80))  # no keys: only a fingerprint
    assert "unknown" in unknown and "Public" not in unknown


def test_packet_viewer_reception_row_keeps_rssi_on_one_line() -> None:
    """SNR and RSSI never wrap: the separator gets narrower before the row wraps.

    The narrowest lane of the card is the lane of the PicoCalc dialog (53 cells, less the
    gutter of the backdrop and the chrome of the box). There, a two-digit SNR next to a
    three-digit RSSI is too wide for the wide ``  ·  `` separator by exactly its padding.
    If the row wraps, the bare word ``rssi`` goes on a line by itself.
    """
    from meshterm.platforms import PICOCALC_LYRA, REGULAR
    from meshterm.ui.tui.frame import _dialog_layout

    entry = PacketEntry(when=utcnow(), kind="advert", node="3d63", snr=-13.5, rssi=-101.0)
    for platform, cols in ((REGULAR, 72), (PICOCALC_LYRA, PICOCALC_LYRA.readable_cols)):
        set_platform(platform)
        viewer = _viewer(entry)
        _, _, _, body = _dialog_layout(viewer, cols, 26)
        rows = [row for row in _stripped(body) if "rssi" in row]
        assert len(rows) == 1, (platform.name, _stripped(body))
        assert "-13.5 dB" in rows[0] and "-101 dBm rssi" in rows[0], platform.name
    # Where there are enough cells, the wide separator stays.
    set_platform(REGULAR)
    assert "  ·  " in _plain(_viewer(entry).render_body(80))


def test_packet_viewer_shows_a_traces_per_hop_links() -> None:
    """A trace reports on the mesh as it crosses it, so the card shows each leg that it heard.

    The card shows one reading for each hop, in the order of the walk. For a walk out and back
    through a repeater, the readings of the return legs are weaker than the outbound one.
    """
    entry = PacketEntry(
        when=utcnow(),
        kind="packet",
        path="",
        raw={
            "payload_typename": "TRACE",
            "route_typename": "DIRECT",
            "trace_tag": "52a37882",
            "trace_snrs": [13.25, -5.0, -4.25],
        },
    )
    body = _plain(_viewer(entry).render_body(80))
    assert "🎯 TRACE" in body
    assert "links" in body
    for reading in ("+13.25 dB", "-5.00 dB", "-4.25 dB"):
        assert reading in body, reading
    assert "trace_snrs" not in body  # it is in its own row, not in the raw dump


def test_packet_viewer_omits_links_for_a_trace_nobody_relayed() -> None:
    """No hop has measured it yet, so the card has no link row. It does not draw an empty one."""
    entry = PacketEntry(
        when=utcnow(),
        kind="packet",
        path="",
        raw={"payload_typename": "TRACE", "trace_tag": "52a37882", "trace_snrs": []},
    )
    assert "links" not in _plain(_viewer(entry).render_body(80))


def test_packet_viewer_counts_the_relays_under_the_chain() -> None:
    """The hop count hangs under the ``via`` chain. The chips never show this figure.

    It is the hop atom of the app. Thus a count here reads in the same way as on the routes
    of the node page and under a trace scenario. An empty chain has no count. The ``via``
    row already says "direct — no relays" in words, and ``direct`` under it would give the
    same answer two times.
    """

    def note(path: str) -> str:
        entry = PacketEntry(
            when=utcnow(), kind="packet", path=path, snr=9.0, raw={"payload_typename": "TRACE"}
        )
        return _plain(_viewer(entry).render_body(80))

    assert "2 hops" in note("a1b2c3,d4e5f6")
    assert "1 hop" in note("a1b2c3") and "1 hops" not in note("a1b2c3")
    assert "direct" not in note("a1b2c3")
    assert note("").count("direct") == 1  # the words of the via row, and nothing under them


def test_packet_viewer_does_not_call_a_walked_trace_direct() -> None:
    """A trace names no relays, so an empty chain there means "unrecorded", not "none".

    The readings of the trace prove that three nodes forwarded it. If the card said
    "direct — no relays" two lines under a links row that lists three legs, the card would
    contradict its own evidence.
    """
    entry = PacketEntry(
        when=utcnow(),
        kind="packet",
        path="",
        snr=13.75,
        raw={"payload_typename": "TRACE", "trace_snrs": [13.25, -5.0, -4.25]},
    )
    body = _plain(_viewer(entry).render_body(80))
    assert "3 hops walked" in body
    assert "direct — no relays" not in body


def test_packet_viewer_still_calls_an_unwalked_frame_direct() -> None:
    """Nothing forwarded the frame and nothing measured it. It is a direct transmission."""
    for raw in ({"payload_typename": "TRACE", "trace_snrs": []}, {"payload_typename": "TEXT_MSG"}):
        body = _plain(
            _viewer(
                PacketEntry(when=utcnow(), kind="packet", path="", snr=9.0, raw=raw)
            ).render_body(80)
        )
        assert "direct — no relays" in body, raw


def test_packet_viewer_counts_one_walked_hop_in_the_singular() -> None:
    """One leg is "1 hop walked", not "1 hops walked"."""
    entry = PacketEntry(
        when=utcnow(),
        kind="packet",
        path="",
        snr=13.75,
        raw={"payload_typename": "TRACE", "trace_snrs": [13.25]},
    )
    assert "1 hop walked" in _plain(_viewer(entry).render_body(80))


def test_packet_viewer_legend_steps_aside_on_a_short_frame() -> None:
    """Under the route graph of a short frame, the caption stays and the node-type legend goes."""
    entry = PacketEntry(when=utcnow(), kind="packet", path="3d63,a1b2")
    viewer = _viewer(entry)
    viewer.note_viewport(10)  # the dialog budget of the Cardputer
    body = _plain(viewer.render_body(49))
    assert "origin → you" in body and "▲ repeater" not in body
