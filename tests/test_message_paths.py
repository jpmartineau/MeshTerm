# SPDX-License-Identifier: Apache-2.0
"""Message-paths tests: the match of a chat message to its logged arrivals.

The tests match channel arrivals by a decrypt of the overheard GRP_TXT packets with the key
of the channel. The tests build packets in the firmware form, in the same way as the
packet-viewer tests. The tests match direct arrivals by time only. Everything runs against
a real temporary repository, so the tests also use the window query.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Hash import HMAC, SHA256

from meshterm.core.channels import channel_hash, derive_secret
from meshterm.core.models import ChatMessage, Observation, utcnow
from meshterm.persistence.repository import Repository
from meshterm.services.message_paths import (
    channel_arrivals,
    collapse,
    direct_arrivals,
    distinct_paths,
)

SECRET = derive_secret("#general")


def _grp_txt_raw(secret: bytes, text: str, *, attempt: int = 0) -> dict:
    """A GRP_TXT packet payload in the firmware form for ``text``, encrypted with ``secret``."""
    plain = (0).to_bytes(4, "little") + bytes([attempt]) + text.encode("utf-8")
    plain += b"\x00" * (-len(plain) % 16)
    crypted = AES.new(secret, AES.MODE_ECB).encrypt(plain)
    mac = HMAC.new(secret, digestmod=SHA256)
    mac.update(crypted)
    return {
        "payload_typename": "GRP_TXT",
        "chan_hash": channel_hash(secret),
        "cipher_mac": mac.digest()[:2].hex(),
        "crypted": crypted.hex(),
    }


def _repo(tmp_path: Path) -> tuple[Repository, int]:
    repo = Repository(tmp_path / "test.db")
    run_id = repo.start_run("monitor", {}, None)
    return repo, run_id


def _record_frame(repo: Repository, run_id: int, *, when, path: str, raw: dict, snr=2.0):
    repo.record_observation(
        run_id,
        Observation(node=None, kind="packet", path=path, snr=snr, observed_at=when, raw=raw),
    )


def test_channel_arrivals_match_by_decrypted_content(tmp_path: Path) -> None:
    """Each overheard copy of a channel message is found, and each has its own path."""
    repo, run = _repo(tmp_path)
    now = utcnow()
    wire = "Alice: hi mesh"
    _record_frame(
        repo, run, when=now - timedelta(seconds=5), path="3d63", raw=_grp_txt_raw(SECRET, wire)
    )
    _record_frame(
        repo,
        run,
        when=now - timedelta(seconds=3),
        path="3d63,a1b2",
        raw=_grp_txt_raw(SECRET, wire),
    )
    _record_frame(
        repo,
        run,
        when=now - timedelta(seconds=1),
        path="",
        raw=_grp_txt_raw(SECRET, "Alice: other"),
    )

    message = ChatMessage(text=wire, is_channel=True, created_at=now)
    arrivals = channel_arrivals(repo, message, channel_name="#general", secret=SECRET)
    assert [a.hops for a in arrivals] == [("3d63",), ("3d63", "a1b2")]
    assert distinct_paths(arrivals) == 2
    repo.close()


def test_channel_arrivals_tolerate_the_sender_prefix_on_the_wire(tmp_path: Path) -> None:
    """Our own outbound message (stored as typed) matches its copies on the air.

    The copies on the air have a prefix.
    """
    repo, run = _repo(tmp_path)
    now = utcnow()
    _record_frame(
        repo,
        run,
        when=now + timedelta(seconds=2),
        path="3d63",
        raw=_grp_txt_raw(SECRET, "Homestead: on my way"),
    )
    message = ChatMessage(text="on my way", outbound=True, is_channel=True, created_at=now)
    arrivals = channel_arrivals(repo, message, channel_name="#general", secret=SECRET)
    assert len(arrivals) == 1 and arrivals[0].hops == ("3d63",)
    repo.close()


def test_channel_arrivals_carry_the_resend_counter(tmp_path: Path) -> None:
    """The resend counter of a decrypted packet goes with it.

    The counter shows copies and retries.
    """
    repo, run = _repo(tmp_path)
    now = utcnow()
    _record_frame(repo, run, when=now, path="", raw=_grp_txt_raw(SECRET, "Alice: hi", attempt=0))
    _record_frame(
        repo,
        run,
        when=now + timedelta(seconds=9),
        path="",
        raw=_grp_txt_raw(SECRET, "Alice: hi", attempt=1),
    )
    message = ChatMessage(text="Alice: hi", is_channel=True, created_at=now)
    arrivals = channel_arrivals(repo, message, channel_name="#general", secret=SECRET)
    assert [a.resend for a in arrivals] == [0, 1]
    assert distinct_paths(arrivals) == 1  # both arrived direct
    repo.close()


def test_channel_arrivals_ignore_frames_outside_the_window(tmp_path: Path) -> None:
    """A matching packet far outside the window of the message belongs to another message."""
    repo, run = _repo(tmp_path)
    now = utcnow()
    _record_frame(
        repo,
        run,
        when=now - timedelta(hours=2),
        path="3d63",
        raw=_grp_txt_raw(SECRET, "Alice: hi"),
    )
    message = ChatMessage(text="Alice: hi", is_channel=True, created_at=now)
    assert channel_arrivals(repo, message, channel_name="#general", secret=SECRET) == []
    repo.close()


def _direct_raw(dest: str = "", src: str = "", mac: str = "", route: str = "") -> dict:
    """The raw payload of a direct-message packet, in the form that the meshcore library reports."""
    raw: dict = {"payload_typename": "TEXT_MSG"}
    if dest:
        raw["dest_hash"] = dest
    if src:
        raw["src_hash"] = src
    if mac:
        raw["cipher_mac"] = mac
    if route:
        raw["route_typename"] = route
    return raw


def test_direct_frames_without_a_mac_fall_back_to_address_and_time(tmp_path: Path) -> None:
    """History from before the app kept the MAC still correlates, and it says so.

    The fallback is evidence and not a claim. A packet between the right pair in the right
    window is only probably this message. Thus ``exact`` is ``False``, and the screen can
    show the match as not certain.
    """
    repo, run = _repo(tmp_path)
    now = utcnow()
    _record_frame(
        repo,
        run,
        when=now + timedelta(seconds=3),
        path="3d63",
        raw=_direct_raw(dest="d4", src="a1"),
    )
    _record_frame(  # a channel packet in the window is not evidence of a direct message
        repo,
        run,
        when=now + timedelta(seconds=4),
        path="",
        raw=_grp_txt_raw(SECRET, "Alice: hi"),
    )
    _record_frame(  # a direct packet far outside the window does not correlate
        repo,
        run,
        when=now + timedelta(minutes=10),
        path="",
        raw=_direct_raw(dest="d4", src="a1"),
    )
    message = ChatMessage(text="see you at 8", outbound=True, peer="d4e5", created_at=now)
    arrivals, exact = direct_arrivals(repo, message, self_key="a1" + "0" * 62, peer_key="d4e5")
    assert [a.hops for a in arrivals] == [("3d63",)]
    assert exact is False
    repo.close()


def test_direct_frames_keep_only_the_direction_the_message_travelled(tmp_path: Path) -> None:
    """A sent message shows our outgoing packets. A received message shows the incoming ones.

    The hashes of both ends are on each packet of a conversation, in each direction. Thus a
    match on the pair only put our own sends into the view of a received message. This is
    the cause of the one-hop rows that could not be true (JP, 2026-09-02). Our own
    transmissions came back from the repeaters in range, and each was correctly one hop.
    But the view showed them as an inbound route to us.
    """
    repo, run = _repo(tmp_path)
    now = utcnow()
    ours = dict(dest="d4", src="a1", mac="beef")  # us -> peer
    theirs = dict(dest="a1", src="d4", mac="f00d")  # peer -> us
    _record_frame(repo, run, when=now + timedelta(seconds=1), path="3d63", raw=_direct_raw(**ours))
    _record_frame(repo, run, when=now + timedelta(seconds=2), path="c0", raw=_direct_raw(**theirs))
    # Traffic of other nodes, overheard in the same window.
    _record_frame(
        repo,
        run,
        when=now + timedelta(seconds=3),
        path="3d63",
        raw=_direct_raw(dest="7f", src="c0", mac="dead"),
    )
    _record_frame(
        repo,
        run,
        when=now + timedelta(seconds=4),
        path="",
        raw=_direct_raw(dest="a1", src="7f", mac="cafe"),
    )

    sent = ChatMessage(text="see you at 8", outbound=True, peer="d4e5f6a7", created_at=now)
    got = ChatMessage(text="ok", outbound=False, peer="d4e5f6a7", created_at=now)
    keys = dict(self_key="a1" + "0" * 62, peer_key="d4e5f6a7")

    assert [a.hops for a in direct_arrivals(repo, sent, **keys)[0]] == [("3d63",)]
    assert [a.hops for a in direct_arrivals(repo, got, **keys)[0]] == [("c0",)]
    repo.close()


def test_a_shared_mac_separates_one_message_from_the_next(tmp_path: Path) -> None:
    """Two sends, a few seconds apart, keep their own packets. The MAC is the fingerprint.

    This is the equivalent, for direct messages, of the content match of the channel view.
    The app cannot read the packets. But the copies of one message have the same MAC over
    their ciphertext, and the copies of the next message have a different MAC.
    """
    repo, run = _repo(tmp_path)
    now = utcnow()
    # The first send, with two retries, heard from two repeaters.
    for offset, path in ((1, "3d63"), (1, "27d4"), (6, "3d63"), (6, "27d4")):
        _record_frame(
            repo,
            run,
            when=now + timedelta(seconds=offset),
            path=path,
            raw=_direct_raw(dest="d4", src="a1", mac="beef"),
        )
    # The second send, twelve seconds later, with the same pair and the same paths.
    for offset, path in ((13, "3d63"), (13, "27d4")):
        _record_frame(
            repo,
            run,
            when=now + timedelta(seconds=offset),
            path=path,
            raw=_direct_raw(dest="d4", src="a1", mac="f00d"),
        )

    keys = dict(self_key="a1" + "0" * 62, peer_key="d4e5f6a7")
    first = ChatMessage(text="one", outbound=True, peer="d4e5f6a7", created_at=now)
    second = ChatMessage(
        text="two",
        outbound=True,
        peer="d4e5f6a7",
        created_at=now + timedelta(seconds=12),
    )
    got, exact = direct_arrivals(repo, first, **keys)
    assert exact is True
    assert len(got) == 4, "the first send's four frames, and not the second's"
    assert len(direct_arrivals(repo, second, **keys)[0]) == 2
    repo.close()


def test_the_window_clamps_to_the_messages_going_the_same_way(tmp_path: Path) -> None:
    """The window has a limit from the messages that go the same way, and each way differs.

    Without a clamp, a fast exchange put the packets of each message in the view of each
    other message. Nine messages of one real conversation were in one flat window. With the
    wrong clamp, a received message got an empty window. The time stamp of a received
    message is the clock of the sender. Thus the order against our own sends compares two
    clocks. The result can be a limit that, on the clock of the received message, has not
    yet occurred (JP, 2026-09-02).
    """
    from meshterm.services.message_paths import _DIRECT_WINDOW, direct_window

    repo, run = _repo(tmp_path)
    now = utcnow()
    sent = [now, now + timedelta(seconds=20)]
    received = [now - timedelta(seconds=30), now + timedelta(seconds=50)]
    for at in sent:
        repo.record_chat_message(
            ChatMessage(text="ours", outbound=True, peer="d4e5f6a7", created_at=at)
        )
    for at in received:
        repo.record_chat_message(
            ChatMessage(text="theirs", outbound=False, peer="d4e5f6a7", created_at=at)
        )

    # Our own send uses our clock, so nothing of it is earlier than the message. The window
    # opens at the message and runs forward to the next send. The retries are there.
    ours = ChatMessage(text="ours", outbound=True, peer="d4e5f6a7", created_at=now)
    start, end = direct_window(repo, ours)
    assert (now - start) < timedelta(seconds=5), "no room behind a send"
    assert end == sent[1], "forward to the next send, not to the next received message"

    # A received message keeps a window in the centre. The window goes halfway to the
    # received messages on each side. Our own sends in between do not change it.
    theirs = ChatMessage(
        text="theirs", outbound=False, peer="d4e5f6a7", created_at=now + timedelta(seconds=50)
    )
    start, end = direct_window(repo, theirs)
    assert start == theirs.created_at - timedelta(seconds=40), "halfway back to the last one"
    assert end == theirs.created_at + _DIRECT_WINDOW, "nothing follows, so that side stands"
    repo.close()


def test_collapse_folds_a_repeated_path_into_one_counted_row(tmp_path: Path) -> None:
    """The collapse makes one row for each path.

    The row has the number of copies, the time it was first heard, and the best SNR.
    """
    from meshterm.services.message_paths import Arrival

    base = utcnow()
    arrivals = [
        Arrival(when=base, hops=("3d63",), snr=10.0),
        Arrival(when=base + timedelta(seconds=1), hops=("27d4",), snr=5.0),
        Arrival(when=base + timedelta(seconds=5), hops=("3d63",), snr=13.5),
    ]
    folded = collapse(arrivals)
    assert [a.hops for a in folded] == [("3d63",), ("27d4",)], "first-heard order"
    assert folded[0].copies == 2 and folded[1].copies == 1
    assert folded[0].when == base, "the first sighting is when the path first worked"
    assert folded[0].snr == 13.5, "the best reading is what the path can do"


def test_a_routed_frames_path_is_where_it_was_going_not_where_it_has_been(
    tmp_path: Path,
) -> None:
    """The route type decides what ``path`` means. An empty path of a routed packet means nothing.

    A flooded packet collects its path, because each relay adds itself. Thus the hops are
    the route that the packet took to us. A direct-routed packet has a route that its
    sender wrote, and its relays use up the route. Thus an empty path means that the packet
    used all of the route. It does not mean that the packet crossed no relays. If the code
    reads the second case as the first, a message from five hops away shows as a message
    that came from nowhere: the "impossible direct path" (JP, 2026-09-02).
    """
    repo, run = _repo(tmp_path)
    now = utcnow()
    _record_frame(  # flooded, one relay: it did come to us that way
        repo,
        run,
        when=now + timedelta(seconds=1),
        path="3d63",
        raw=_direct_raw(dest="a1", src="d4", mac="beef", route="FLOOD"),
    )
    _record_frame(  # direct-routed, route used up: it does not show how the packet got here
        repo,
        run,
        when=now + timedelta(seconds=2),
        path="",
        raw=_direct_raw(dest="a1", src="d4", mac="beef", route="DIRECT"),
    )
    message = ChatMessage(text="hi", outbound=False, peer="d4e5f6a7", created_at=now)
    arrivals, _exact = direct_arrivals(repo, message, self_key="a1" + "0" * 62, peer_key="d4e5f6a7")
    flooded, routed = arrivals
    assert flooded.routed is False and flooded.route_known is True
    assert routed.routed is True and routed.route_known is False, (
        "an empty routed path is not a zero-hop arrival"
    )
    repo.close()


def test_history_without_a_route_type_is_unknown_rather_than_assumed(tmp_path: Path) -> None:
    """Packets from before the app kept the route type read as before. They are not guesses."""
    repo, run = _repo(tmp_path)
    now = utcnow()
    _record_frame(
        repo,
        run,
        when=now + timedelta(seconds=1),
        path="",
        raw=_direct_raw(dest="a1", src="d4", mac="beef"),
    )
    message = ChatMessage(text="hi", outbound=False, peer="d4e5f6a7", created_at=now)
    arrivals, _exact = direct_arrivals(repo, message, self_key="a1" + "0" * 62, peer_key="d4e5f6a7")
    assert arrivals[0].routed is None
    assert arrivals[0].route_known is True, "unknown falls back to the old reading"
    repo.close()


def test_collapse_keeps_a_routed_path_apart_from_the_same_hops_flooded(tmp_path: Path) -> None:
    """The same hashes have two different meanings under the two route types."""
    from meshterm.services.message_paths import Arrival

    base = utcnow()
    folded = collapse(
        [
            Arrival(when=base, hops=("3d63",), snr=1.0, routed=False),
            Arrival(when=base + timedelta(seconds=1), hops=("3d63",), snr=2.0, routed=True),
        ]
    )
    assert len(folded) == 2, "a route travelled is not a route intended"
    assert [a.routed for a in folded] == [False, True]
