# SPDX-License-Identifier: Apache-2.0
"""Decoding of the packet body: the address of an overheard packet.

The RX log of the companion gives MeshTerm the header of each packet already parsed, and
the body raw. These tests cover these parts:

- The layouts that :mod:`meshterm.core.frames` reads from the body.
- The length checks. A cut packet must not give an address that is not true.
- The round trip through storage. The feed opens on stored history, so what a live packet
  said about itself must survive the write and the read.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from meshterm.core.connection import packet_observation_from_event
from meshterm.core.frames import frame_addressing, trace_link_snrs
from meshterm.core.models import Observation, utcnow
from meshterm.persistence.repository import Repository


class _Event:
    """A meshcore event stand-in: anything exposing a ``payload`` mapping."""

    def __init__(self, **payload) -> None:
        self.payload = payload


def _frame(typename: str, body: bytes, **extra) -> dict:
    return {"payload_typename": typename, "pkt_payload": body, **extra}


def test_addressed_classes_name_both_ends_and_their_mac() -> None:
    """A direct message, request, response, or returned path starts with dest, src, then MAC.

    The MAC is a tag over the encrypted body. Thus it is a fingerprint of the message that
    a packet carries, and the app does not read the packet. The message-paths screen uses
    this to group the retransmissions of one direct message and to tell them from the next
    message (refer to :mod:`~meshterm.services.message_paths`).
    """
    body = bytes.fromhex("3da1") + b"\xab\xcd" + b"\x00" * 16
    for typename in ("TEXT_MSG", "REQ", "RESPONSE", "PATH"):
        assert frame_addressing(_frame(typename, body)) == {
            "dest_hash": "3d",
            "src_hash": "a1",
            "cipher_mac": "abcd",
        }


def test_anonymous_request_carries_its_senders_whole_key() -> None:
    """There is no shared secret yet, so the sender gives its full key: 32 bytes."""
    key = bytes(range(32))
    decoded = frame_addressing(_frame("ANON_REQ", b"\x3d" + key + b"\xab\xcd" + b"\x00" * 16))
    assert decoded == {"dest_hash": "3d", "src_key": key.hex()}


def test_tokened_classes_name_their_token() -> None:
    """An ack has the checksum of what it answers. A trace has its tag.

    The checksum of the ack keeps the order on the wire. This is the form that the
    companion uses to report its own delivery acks. The tag of the trace is the
    little-endian ``uint32`` that the firmware wrote. Thus it reads as the same number as
    the ``tag`` field of a trace reply.
    """
    assert frame_addressing(_frame("ACK", bytes.fromhex("9b71e004"))) == {
        "ack_crc": "9b71e004",
    }
    trace = bytes.fromhex("102a3c5f") + bytes.fromhex("deadbeef") + b"\x01"
    assert frame_addressing(_frame("TRACE", trace)) == {"trace_tag": "5f3c2a10"}


def test_a_future_payload_version_is_left_undecoded() -> None:
    """The code does not decode a future payload version.

    Version 2 makes the hashes and the MAC wider. If the code used the offsets of version 1,
    it would take the wrong bytes.
    """
    body = bytes.fromhex("3da1") + b"\xab\xcd" + b"\x00" * 16
    assert frame_addressing(_frame("TEXT_MSG", body, payload_ver=0))["dest_hash"] == "3d"
    assert frame_addressing(_frame("TEXT_MSG", body, payload_ver=1)) == {}
    assert frame_addressing(_frame("TEXT_MSG", body))["dest_hash"] == "3d"  # no version: v1


def test_channel_datagram_is_broken_out_like_a_channel_text() -> None:
    """A channel datagram is broken out in the same way as a channel text.

    ``GRP_DATA`` has the same channel envelope as ``GRP_TXT``, but the library decodes the
    envelope only for ``GRP_TXT``.
    """
    decoded = frame_addressing(_frame("GRP_DATA", b"\xa3\xab\xcd" + bytes(range(16))))
    assert decoded == {
        "chan_hash": "a3",
        "cipher_mac": "abcd",
        "crypted": bytes(range(16)).hex(),
    }


def test_a_body_too_short_for_its_layout_decodes_to_nothing() -> None:
    """A body too short for its layout decodes to nothing.

    An empty lane is better than a hash that looks true but comes from the wrong bytes.
    """
    assert frame_addressing(_frame("TEXT_MSG", b"\x3d")) == {}
    assert frame_addressing(_frame("ANON_REQ", b"\x3d" + bytes(20))) == {}
    assert frame_addressing(_frame("ACK", b"\x9b")) == {}
    assert frame_addressing(_frame("TRACE", bytes(6))) == {}
    assert frame_addressing(_frame("GRP_DATA", b"\xa3")) == {}


def test_classes_that_address_nothing_stay_silent() -> None:
    """Classes that have no address give no address.

    An advert names its node in the header. Multipart and control packets have no address.
    """
    for typename in ("ADVERT", "MULTIPART", "CONTROL", "UNK"):
        assert frame_addressing(_frame(typename, bytes(64))) == {}
    assert frame_addressing({"payload_typename": "TEXT_MSG"}) == {}  # no body
    assert frame_addressing({"pkt_payload": bytes(20)}) == {}  # no class


def test_rx_log_events_carry_their_addressing_into_the_observation() -> None:
    """The decode occurs one time, at the edge. Thus all the code after it sees the same keys."""
    event = _Event(
        payload_typename="TEXT_MSG",
        pkt_payload=bytes.fromhex("3da1abcd") + bytes(16),
        path_len=1,
        path_hash_size=1,
        path="3d",
        snr=6.5,
    )
    obs = packet_observation_from_event(event)
    assert obs is not None and obs.raw is not None
    assert obs.raw["dest_hash"] == "3d" and obs.raw["src_hash"] == "a1"
    assert obs.node is None  # an addressed packet still names no origin node


def test_stored_frames_remember_what_they_addressed(tmp_path: Path) -> None:
    """A replayed row reads back exactly as the live packet did, with the same keys and values.

    The feed opens with rows from history. If the address were only in the raw payload of a
    live event, each row on the first screen would be blank.
    """
    repo = Repository(tmp_path / "frames.db")
    run = repo.start_run("monitor", {}, None)
    frames = {
        "TEXT_MSG": {"dest_hash": "3d", "src_hash": "a1"},
        "ANON_REQ": {"dest_hash": "3d", "src_key": bytes(range(32)).hex()},
        "TRACE": {"trace_tag": "5f3c2a10"},
        "ACK": {"ack_crc": "9b71e004"},
        "GRP_DATA": {"chan_hash": "a3", "cipher_mac": "abcd", "crypted": "00" * 16},
    }
    for typename, decoded in frames.items():
        repo.record_observation(
            run,
            Observation(
                node=None,
                kind="packet",
                path="3d",
                raw={"payload_typename": typename, **decoded},
            ),
        )

    stored = repo.recent_observations(since=utcnow() - timedelta(hours=2))
    read_back = {(o.raw or {}).get("payload_typename"): (o.raw or {}) for o in stored}
    assert set(read_back) == set(frames)
    for typename, decoded in frames.items():
        for key, value in decoded.items():
            assert read_back[typename][key] == value, (typename, key)
    repo.close()


def test_a_trace_path_is_link_readings_not_relay_hashes() -> None:
    """The path of a trace is link readings, not relay hashes.

    This is the only class where the path field of the header has a different meaning. A
    trace adds one signed SNR byte to its path for each hop. If the code reads the bytes as
    hashes, it makes adjacencies that do not exist, and it loses the readings. The bytes
    here cover the signed range of the wire: +13.25 dB, then two negative legs.
    """
    payload = _frame("TRACE", bytes(9), path_len=3, path_hash_size=1, path="35eeef")
    assert trace_link_snrs(payload) == [13.25, -4.5, -4.25]


def test_only_a_trace_reads_its_path_that_way() -> None:
    """Each other class keeps hashes in the path, so the code must not decode them as readings."""
    for typename in ("TEXT_MSG", "ADVERT", "GRP_TXT", "ACK", "PATH"):
        assert trace_link_snrs(_frame(typename, bytes(9), path_len=1, path="35")) is None


def test_an_unrelayed_trace_has_readings_for_no_hops() -> None:
    """No node has forwarded the trace yet, so no node measured it. This is not a fault."""
    assert trace_link_snrs(_frame("TRACE", bytes(9), path_len=0, path="")) == []


def test_a_trace_path_shorter_than_announced_is_not_invented() -> None:
    """The header gives three hops but the path has one byte. The missing readings stay missing."""
    assert trace_link_snrs(_frame("TRACE", bytes(9), path_len=3, path="35")) is None


def test_a_traces_readings_reach_the_observation_and_its_hops_do_not() -> None:
    """The readings of a trace go in the raw payload. ``path`` stays empty, because it has no hops.

    When the code stored those bytes as a path, the topology graph got links that nobody
    observed. These links joined the nodes that had the same first digits as an SNR
    reading.
    """
    obs = packet_observation_from_event(
        _Event(
            payload_typename="TRACE",
            pkt_payload=bytes.fromhex("5f3c2a10") + bytes(5),
            path_len=3,
            path_hash_size=1,
            path="35eeef",
            snr=13.75,
        )
    )
    assert obs is not None and obs.raw is not None
    assert obs.path == ""
    assert obs.raw["trace_snrs"] == [13.25, -4.5, -4.25]
    assert obs.raw["trace_tag"] == "102a3c5f"


def test_a_frame_heard_straight_off_its_sender_is_kept() -> None:
    """A packet that is heard directly from its sender is kept.

    Zero relays is the strongest evidence of adjacency. It is not an absence of evidence.
    A device next to this one puts only such packets on the air. No node has relayed them,
    so they name no repeater, and only an advert has an origin key. When the code removed
    these packets, it made each trace, message, and ack of a neighbour invisible.
    """
    for typename in ("TRACE", "TEXT_MSG", "ACK", "REQ", "GRP_TXT"):
        obs = packet_observation_from_event(
            _Event(
                payload_typename=typename,
                pkt_payload=bytes(24),
                path_len=0,
                path_hash_size=1,
                path="",
                snr=9.25,
                rssi=-61,
            )
        )
        assert obs is not None, typename
        assert obs.path == "" and obs.snr == 9.25
        assert (obs.raw or {})["payload_typename"] == typename


def test_a_frame_with_no_class_at_all_is_still_dropped() -> None:
    """A packet with no class is still removed.

    The library uses a sentinel for a packet that is too short to parse. The sentinel gives
    no information.
    """
    assert (
        packet_observation_from_event(
            _Event(payload_typename="UNK", pkt_payload=b"", path_len=0, path="")
        )
        is None
    )
    assert packet_observation_from_event(_Event(snr=6.0)) is None


def test_a_traces_readings_survive_being_stored(tmp_path: Path) -> None:
    """A replayed trace reads back with the same links as the live trace. The lane is not blank.

    The feed opens with rows from history. If the readings were only in the raw payload of
    a live event, they would be lost when the screen draws from storage. The crypto and
    address columns were added to close the same gap.
    """
    repo = Repository(tmp_path / "trace.db")
    run = repo.start_run("monitor", {}, None)
    repo.record_observation(
        run,
        Observation(
            node=None,
            kind="packet",
            path="",
            raw={
                "payload_typename": "TRACE",
                "trace_tag": "102a3c5f",
                "trace_snrs": [13.25, -4.5, -4.25],
            },
        ),
    )
    (stored,) = repo.recent_observations(since=utcnow() - timedelta(hours=2))
    assert stored.path == ""
    assert (stored.raw or {})["trace_snrs"] == [13.25, -4.5, -4.25]
    assert (stored.raw or {})["trace_tag"] == "102a3c5f"
    repo.close()


def test_only_a_trace_stores_readings(tmp_path: Path) -> None:
    """Only a trace stores readings.

    No other class has readings. Thus the store does not write a stray value on another
    class as if it were a reading.
    """
    repo = Repository(tmp_path / "notrace.db")
    run = repo.start_run("monitor", {}, None)
    repo.record_observation(
        run,
        Observation(
            node=None,
            kind="packet",
            path="3d",
            raw={"payload_typename": "TEXT_MSG", "trace_snrs": [1.0]},
        ),
    )
    (stored,) = repo.recent_observations(since=utcnow() - timedelta(hours=2))
    assert "trace_snrs" not in (stored.raw or {})
    repo.close()
