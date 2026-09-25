# SPDX-License-Identifier: Apache-2.0
"""Packet viewer tests: per-kind flavouring, the raw-field dedup, and channel decrypt.

The viewer is pure Rich-in, ANSI-lines-out (``render_body``), so it's driven headless
against a synthetic :class:`PacketEntry` — the same approach as the dashboard tests.
"""

from __future__ import annotations

import re

from Crypto.Cipher import AES
from Crypto.Hash import HMAC, SHA256

from meshterm.core.channels import channel_hash, derive_secret
from meshterm.core.models import utcnow
from meshterm.platforms import set_platform
from meshterm.ui.packet_viewer import PacketEntry, PacketViewer
from tests.conftest import plain as _plain  # THE strip-and-join screen reader


def _stripped(lines: list[str]) -> list[str]:
    return [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]


def _grp_txt_entry(raw_extra: dict) -> PacketEntry:
    raw = {"payload_typename": "GRP_TXT", "route_typename": "FLOOD", **raw_extra}
    return PacketEntry(when=utcnow(), kind="packet", path="a1b2c3d4e5f6", raw=raw)


def _viewer(entry: PacketEntry, channels=()) -> PacketViewer:
    return PacketViewer([entry], 0, resolve=lambda h: "", channels=channels)


def _grp_txt_frame(secret: bytes, text: str) -> tuple[str, str, str]:
    """A firmware-shaped GRP_TXT frame for ``text``, encrypted under ``secret``."""
    plain = (0).to_bytes(4, "little") + bytes([0]) + text.encode("utf-8")
    plain += b"\x00" * (-len(plain) % 16)
    crypted = AES.new(secret, AES.MODE_ECB).encrypt(plain)
    mac = HMAC.new(secret, digestmod=SHA256)
    mac.update(crypted)
    return channel_hash(secret), mac.digest()[:2].hex(), crypted.hex()


def test_packet_viewer_shows_class_and_route() -> None:
    """A raw packet's parsed payload class headlines the card; the route is a row."""
    entry = _grp_txt_entry({"chan_hash": "ff", "cipher_mac": "0000", "crypted": "00" * 16})
    body = _plain(_viewer(entry).render_body(80))
    assert "📻 CHAN TEXT" in body
    assert "flood" in body


def test_packet_viewer_class_row_leads_the_card() -> None:
    """The class headline is the first row — above heard/from — for every kind."""
    packet = _grp_txt_entry({"chan_hash": "ff", "cipher_mac": "0000", "crypted": "00" * 16})
    body = _plain(_viewer(packet).render_body(80))
    assert body.index("CHAN TEXT") < body.index("heard")

    advert = PacketEntry(when=utcnow(), kind="advert", node="aa")
    body = _plain(_viewer(advert).render_body(80))
    assert "📢 ADVERT" in body
    assert body.index("ADVERT") < body.index("heard")


def test_packet_viewer_class_marks_an_unknown_typename() -> None:
    """A payload class this build has never heard of keeps the ❔ mark and its raw name."""
    entry = PacketEntry(when=utcnow(), kind="packet", raw={"payload_typename": "XYZZY"})
    body = _plain(_viewer(entry).render_body(80))
    assert "❔ XYZZY" in body


def test_packet_viewer_title_carries_no_emoji() -> None:
    """Dialog titles stay emoji-free (the standards' rule); the class row has the icon."""
    entry = PacketEntry(when=utcnow(), kind="advert", node="aa")
    assert _viewer(entry).title == "advert"


def test_packet_viewer_decrypts_a_known_channel() -> None:
    """A channel-text frame we hold the key for decrypts to its plaintext."""
    name, secret = "#general", derive_secret("#general")
    chash, mac, crypted = _grp_txt_frame(secret, "hi mesh")
    entry = _grp_txt_entry({"chan_hash": chash, "cipher_mac": mac, "crypted": crypted})
    body = _plain(_viewer(entry, channels=[(name, secret)]).render_body(80))
    assert "#general" in body
    assert "hi mesh" in body


def test_packet_viewer_reports_an_unknown_channel() -> None:
    """A frame from a channel we don't hold the key for says so instead of guessing."""
    entry = _grp_txt_entry({"chan_hash": "ab", "cipher_mac": "0000", "crypted": "00" * 16})
    body = _plain(_viewer(entry).render_body(80))
    assert "unknown" in body and "can't decrypt" in body


def test_packet_viewer_reaches_packets_that_arrive_after_open() -> None:
    """A live source lets the viewer page up into packets that arrive after it opens."""
    old = PacketEntry(when=utcnow(), kind="advert", node="aa")
    feed = [old]
    viewer = PacketViewer(list(feed), 0, resolve=lambda h: "", source=lambda: list(feed))
    assert viewer._entries[viewer._index] is old

    # A newer packet is prepended (the feed is newest-first) while the dialog sits on the
    # old one; a repaint folds it in and the view stays put on `old` (now at index 1).
    newer = PacketEntry(when=utcnow(), kind="message", node="bb", text="new!")
    feed.insert(0, newer)
    viewer.render_body(80)
    assert viewer._entries[viewer._index] is old
    assert "2/2" in viewer.title

    # ↑ (newer) now reaches the packet that arrived after the dialog opened.
    viewer.handle("up")
    assert viewer._entries[viewer._index] is newer
    assert "1/2" in viewer.title


def test_packet_viewer_heard_row_says_now_not_now_ago() -> None:
    """A just-heard packet's heard row reads "(now)" — the format_ago grammar."""
    entry = PacketEntry(when=utcnow(), kind="advert", node="aa")
    body = _plain(_viewer(entry).render_body(80))
    assert "(now)" in body
    assert "now ago" not in body


def test_packet_viewer_lane_names_the_ends_of_the_list_not_the_body() -> None:
    """Home/End reach the newest and oldest packet here, so the Shift bank says so."""
    old = PacketEntry(when=utcnow(), kind="advert", node="aa")
    newer = PacketEntry(when=utcnow(), kind="message", node="bb", text="new!")

    lone = PacketViewer([old], 0, resolve=lambda h: "")
    lone.note_metrics(4, 20)  # a short body in a roomy box: nothing to page either
    assert [pair.opp_label for pair in lone.fkey_lane[3:]] == ["Oldest", "Newest"]
    assert not any(pair.enabled or pair.opp_enabled for pair in lone.fkey_lane[3:])

    # A second packet lights the jumps on their own — the pager still needs a tall body.
    pair_view = PacketViewer([newer, old], 0, resolve=lambda h: "")
    pair_view.note_metrics(4, 20)
    assert [pair.opp_enabled for pair in pair_view.fkey_lane[3:]] == [True, True]
    assert [pair.enabled for pair in pair_view.fkey_lane[3:]] == [False, False]
    pair_view.note_metrics(80, 20)
    assert [pair.enabled for pair in pair_view.fkey_lane[3:]] == [True, True]


def test_packet_viewer_follows_the_stream_off_the_top_of_a_live_list() -> None:
    """``↑`` off the newest packet is the feed's second stop, not a clamp.

    On the pin the view holds the top *position*: each arrival becomes the card being
    read, the title says so, and ``↓`` steps back off onto whichever packet is showing
    at that moment.
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
    assert viewer.title.endswith("· following")  # the word, not a perpetual 1/n
    assert "↓ hold packet" in viewer.footer_hint

    newer = PacketEntry(when=utcnow(), kind="message", node="bb", text="new!")
    feed.insert(0, newer)
    assert "new!" in _plain(viewer.render_body(80))  # the arrival is now the card
    assert viewer.title.endswith("· following")

    viewer.handle("down")
    assert not viewer._pinned and viewer._entries[viewer._index] is newer
    assert "1/2" in viewer.title  # holding that packet, which now rides down the list


def test_packet_viewer_over_a_snapshot_has_nothing_to_follow() -> None:
    """No live source, no pin: ``↑`` on the newest packet clamps as it always did."""
    newer = PacketEntry(when=utcnow(), kind="message", node="bb", text="new!")
    old = PacketEntry(when=utcnow(), kind="advert", node="aa")
    viewer = PacketViewer([newer, old], 0, resolve=lambda h: "")

    viewer.handle("up")
    assert not viewer._pinned and "following" not in viewer.title
    assert viewer.footer_hint.startswith("↑↓ newer/older")
    viewer.handle("home")
    assert not viewer._pinned and viewer._index == 0


def test_packet_viewer_home_resumes_the_stream() -> None:
    """Home reaches the pin on a live list — the feed's own Home, and its ``Newest`` chip.

    Which is also why the jump chips light over a live list holding a single packet:
    there is somewhere to go even where there is no second packet to go to.
    """
    feed = [PacketEntry(when=utcnow(), kind="advert", node=f"n{i}") for i in range(3)]
    viewer = PacketViewer(list(feed), 0, resolve=lambda h: "", source=lambda: list(feed))
    viewer.handle("end")
    assert viewer._index == 2 and not viewer._pinned  # the oldest packet
    viewer.handle("home")
    assert viewer._pinned

    lone = PacketViewer(feed[:1], 0, resolve=lambda h: "", source=lambda: feed[:1])
    lone.note_metrics(4, 20)  # a short body in a roomy box: nothing to page
    assert [pair.opp_enabled for pair in lone.fkey_lane[3:]] == [True, True]


def test_packet_viewer_without_a_source_stays_a_snapshot() -> None:
    """With no live source the viewer is frozen on its opening list (unchanged behaviour)."""
    entry = PacketEntry(when=utcnow(), kind="advert", node="aa")
    viewer = PacketViewer([entry], 0, resolve=lambda h: "")
    viewer.render_body(80)
    assert viewer._entries == [entry]
    assert viewer.footer_hint == "Esc close"


def test_packet_viewer_grows_its_dialog_only() -> None:
    """The viewer opts into a grow-only dialog so paging enlarges but never shrinks it."""
    tiny = PacketEntry(when=utcnow(), kind="ack", where="01c3")
    viewer = PacketViewer([tiny], 0, resolve=lambda h: "")
    assert viewer.grow_only is True
    # The body is left at its natural (unpadded) height; the frame, not a floor, pads it.
    assert len(viewer.render_body(80)) < 10
    # ratchet_viewport is the grow-only contract: it rises to a taller body and never drops.
    assert viewer.ratchet_viewport(6) == 6
    assert viewer.ratchet_viewport(14) == 14  # a taller packet enlarges the box
    assert viewer.ratchet_viewport(4) == 14  # a shorter one after keeps the larger box


def test_packet_viewer_raw_dump_skips_fields_folded_into_flavoured_rows() -> None:
    """Fields already shown as class/route/via/channel rows don't also dump generically."""
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
    assert "novel_field" in body  # an unrecognized field surfaces with its full name


def test_packet_viewer_shows_full_raw_field_labels() -> None:
    """A long raw-field name is shown whole — the label lane widens rather than clipping it."""
    entry = _grp_txt_entry(
        {
            "chan_hash": "ab",
            "cipher_mac": "0000",
            "crypted": "00" * 16,
            "battery_millivolts": 4102,
        }
    )
    body = _plain(_viewer(entry).render_body(80))
    assert "battery_millivolts" in body  # the 18-char key is not clipped to 8


def test_packet_viewer_draws_a_relayed_packets_route_graph() -> None:
    """A packet that crossed relays gets THE route graph — braille edges, caption, legend."""
    entry = PacketEntry(when=utcnow(), kind="packet", path="3d63,a1b2")
    body = _plain(_viewer(entry).render_body(80))
    assert "origin → you" in body  # the graph caption
    assert "★ you" in body and "▲ repeater" in body  # the node-type legend
    assert any("⠀" <= ch <= "⣿" for ch in body)  # braille edges are drawn
    assert "3d" in body and "a1" in body  # each relay labelled by its hash byte


def test_packet_viewer_draws_a_revisited_hop_twice_and_warns() -> None:
    """An overheard chain naming one hop twice draws it twice, and says why it did.

    Folded to a single marker the walk would close a cycle the left-to-right flow cannot seat,
    and the whole graph collapses into a pile one column wide. So the packet card splits the
    revisit — and owes the reader the warning, because nothing in a one-byte hash can say
    whether the repeat is a genuine loop or two different nodes colliding on that byte.
    """
    names = {"c1": "C14903", "8e": "MileEnd", "da": "Relais", "ee": "ParcEx"}
    entry = PacketEntry(when=utcnow(), kind="packet", path="7f,c1,8e,da,ee,c1,27")
    body = _plain(PacketViewer([entry], 0, resolve=lambda h: names.get(h, "")).render_body(80))
    assert "⚠" in body and "repeats" in body
    assert "a loop, or two nodes sharing one hash" in body
    assert body.count("C14903") == 3  # twice in the via row, once named in the warning
    assert body.count("c1") == 2  # and both visits carry a marker label in the graph


def test_packet_viewer_stays_quiet_when_every_hop_is_distinct() -> None:
    """The revisit warning is not chrome: a normal relayed packet never shows it."""
    entry = PacketEntry(when=utcnow(), kind="packet", path="3d63,a1b2")
    body = _plain(_viewer(entry).render_body(80))
    assert "⚠" not in body and "repeats" not in body


def test_packet_viewer_skips_the_graph_for_a_direct_packet() -> None:
    """A packet with no relays says so in the via row and draws no (pointless) two-node graph."""
    entry = PacketEntry(when=utcnow(), kind="packet", path="")
    body = _plain(_viewer(entry).render_body(80))
    assert "direct — no relays" in body
    assert "origin → you" not in body
    assert not any("⠀" <= ch <= "⣿" for ch in body)


def test_packet_viewer_via_wraps_at_hop_boundaries_under_its_own_lane() -> None:
    """A long ``via`` chain folds between hops, hanging under the value lane — never mid-name."""
    names = {f"{i:02d}aa": f"Relay-Number-{i:02d}" for i in range(8)}
    entry = PacketEntry(when=utcnow(), kind="packet", path=",".join(names))
    lines = _stripped(PacketViewer([entry], 0, resolve=lambda h: names.get(h, "")).render_body(72))
    via_at = next(i for i, line in enumerate(lines) if line.startswith("via"))
    end = next(i for i, line in enumerate(lines) if line.strip() == "8 hops")
    folded = lines[via_at:end]
    assert len(folded) > 1  # a chain this long does not fit one lane
    assert all(line.startswith(" " * 11) for line in folded[1:])  # hangs past the label lane
    for name in names.values():
        assert any(name in line for line in folded)  # every hop survives the fold whole
    assert all(line.rstrip().endswith("→") for line in folded[:-1])  # the "goes on" cue
    assert all(len(line.rstrip()) <= 72 for line in folded)


def test_packet_viewer_via_chain_is_drawn_as_the_middle_of_a_route() -> None:
    """``via`` names relays, so neither end of it is where the frame set out or arrived.

    The chain opens and closes on the "there is more out there" mark — the chevron in
    chips, the bare separator in arrows — rather than on the flat end that would claim
    the first relay was the origin.
    """
    entry = PacketEntry(when=utcnow(), kind="packet", node="c0ffee", path="a1b2c3,d4e5f6")
    via = next(ln for ln in _stripped(_viewer(entry).render_body(80)) if ln.startswith("via"))
    chain = via.split(None, 1)[1].strip()
    assert chain.startswith("→ ") and chain.endswith(" →")
    assert chain.count("→") == 3  # both open ends, and the one join between the two relays


def test_packet_viewer_from_row_leads_with_the_node_type_mark() -> None:
    """The sender wears its shared node glyph — typed by the packet, by the contacts, or ``○``."""
    from meshterm.core.models import NODE_TYPE_REPEATER

    def from_row(viewer: PacketViewer) -> str:
        return next(ln for ln in _stripped(viewer.render_body(80)) if ln.startswith("from"))

    advertised = PacketEntry(
        when=utcnow(), kind="advert", node="3d63", name="Hub", node_type=NODE_TYPE_REPEATER
    )
    assert "▲ Hub" in from_row(_viewer(advertised))  # the type the packet itself carried

    bare = PacketEntry(when=utcnow(), kind="packet", node="3d63", name="Hub")
    assert "○ Hub" in from_row(_viewer(bare))  # nothing can type it: the unknown ring
    typed = PacketViewer([bare], 0, resolve=lambda h: "", type_of=lambda h: NODE_TYPE_REPEATER)
    assert "▲ Hub" in from_row(typed)  # …until the contacts can

    mine = PacketEntry(when=utcnow(), kind="advert", node="3d63", name="Waymarker")
    ours = PacketViewer([mine], 0, resolve=lambda h: "", self_name="Waymarker")
    assert "★ Waymarker" in from_row(ours)  # our own node keeps the app-wide star


def test_packet_viewer_graph_names_a_known_origin_else_a_question_mark() -> None:
    """The graph's left endpoint is the resolved origin name, or ``?`` when the frame named none."""
    known = PacketEntry(when=utcnow(), kind="packet", node="c0ffee", path="3d63")
    body = _plain(
        PacketViewer([known], 0, resolve=lambda h: "Base" if h == "c0ffee" else "").render_body(80)
    )
    assert "Base" in body  # the origin, resolved to its contact name

    nameless = PacketEntry(when=utcnow(), kind="packet", path="3d63")
    body2 = _plain(_viewer(nameless).render_body(80))
    assert "?" in body2  # an origin-less flood draws a plain "?" endpoint


def test_packet_viewer_lays_out_what_a_frame_addressed() -> None:
    """A frame that names no origin still names its two ends — as rows, not raw hex.

    The endpoints come out of the frame body (``meshterm.core.frames``), so the card can
    say who a relayed direct message was for even though the class carries no origin node.
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
    assert "Waymarker" in to_row and "c0" in to_row  # the name, then the hash it was named by
    assert "Alice" in from_row and "a1" in from_row
    # …and neither is repeated by the generic raw dump at the foot of the card.
    assert _plain(lines).count("dest_hash") == 0


def test_packet_viewer_names_a_tokened_frame_by_its_token() -> None:
    """An ack points at the message it answers; a trace at its own tag."""
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
    """A datagram's body is not text, but the MAC that guards it still names its channel."""
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

    unknown = _plain(_viewer(entry).render_body(80))  # no keys held: named by fingerprint only
    assert "unknown" in unknown and "Public" not in unknown


def test_packet_viewer_reception_row_keeps_rssi_on_one_line() -> None:
    """SNR and RSSI never fold: the separator tightens before the row does.

    The card's narrowest lane is the PicoCalc dialog's (53 columns less the backdrop
    gutter and the box's own chrome), where a two-digit SNR beside a three-digit RSSI
    misses the roomy ``  ·  `` by exactly its padding — and folding puts the bare word
    ``rssi`` on a line of its own.
    """
    from meshterm.platforms import PICOCALC, REGULAR
    from meshterm.ui.tui.frame import _dialog_layout

    entry = PacketEntry(when=utcnow(), kind="advert", node="3d63", snr=-13.5, rssi=-101.0)
    for platform, cols in ((REGULAR, 72), (PICOCALC, PICOCALC.readable_cols)):
        set_platform(platform)
        viewer = _viewer(entry)
        _, _, _, body = _dialog_layout(viewer, cols, 26)
        rows = [row for row in _stripped(body) if "rssi" in row]
        assert len(rows) == 1, (platform.name, _stripped(body))
        assert "-13.5 dB" in rows[0] and "-101 dBm rssi" in rows[0], platform.name
    # Where the cells are there, the roomy separator stays.
    set_platform(REGULAR)
    assert "  ·  " in _plain(_viewer(entry).render_body(80))


def test_packet_viewer_shows_a_traces_per_hop_links() -> None:
    """A trace reports on the mesh as it crosses it, so the card shows every leg it heard.

    One reading per hop travelled, in walk order — which for a walk out and back through
    a repeater means the return legs read weaker than the outbound one.
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
    assert "trace_snrs" not in body  # folded into its own row, not dumped raw


def test_packet_viewer_omits_links_for_a_trace_nobody_relayed() -> None:
    """No hop has measured it yet, so there is no link row to draw — not an empty one."""
    entry = PacketEntry(
        when=utcnow(),
        kind="packet",
        path="",
        raw={"payload_typename": "TRACE", "trace_tag": "52a37882", "trace_snrs": []},
    )
    assert "links" not in _plain(_viewer(entry).render_body(80))


def test_packet_viewer_counts_the_relays_under_the_chain() -> None:
    """The hop count hangs under the ``via`` chain — the figure the chips never state.

    It is the app's own hop atom, so a count here reads as it does on the node page's
    routes and under a trace scenario. An empty chain has no count: the ``via`` row has
    already said "direct — no relays" in words, and ``direct`` under it would be the same
    answer twice.
    """

    def note(path: str) -> str:
        entry = PacketEntry(
            when=utcnow(), kind="packet", path=path, snr=9.0, raw={"payload_typename": "TRACE"}
        )
        return _plain(_viewer(entry).render_body(80))

    assert "2 hops" in note("a1b2c3,d4e5f6")
    assert "1 hop" in note("a1b2c3") and "1 hops" not in note("a1b2c3")
    assert "direct" not in note("a1b2c3")
    assert note("").count("direct") == 1  # the via row's own words, and nothing under them


def test_packet_viewer_does_not_call_a_walked_trace_direct() -> None:
    """A trace names no relays, so an empty chain there means "unrecorded", not "none".

    Its readings prove three nodes forwarded it; saying "direct — no relays" two lines
    under a links row listing three legs would contradict the card's own evidence.
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
    """Nothing forwarded it and nothing measured it — that really is a direct shot."""
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
