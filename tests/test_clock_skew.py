# SPDX-License-Identifier: Apache-2.0
"""A sender with a wrong clock: the two times of a message, and what each one is for.

A received message carries the sender time, from the clock of the sender. MeshTerm also
keeps the arrival time, from our clock. Each part of the app that is about our side (heard
times, the order of conversations, channel activity, the search of the packet log) uses
the arrival time. The transcript shows the sender time while it is correct, and the arrival
time with a ``?`` mark when it is not.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

from meshterm.core.connection import message_from_event
from meshterm.core.models import (
    CLOCK_TOLERANCE_S,
    ChatMessage,
    Contact,
    Conversation,
    Message,
    utcnow,
)
from meshterm.persistence import db
from meshterm.persistence.repository import Repository
from meshterm.services.message_paths import direct_arrivals
from meshterm.tools.chat import _transcript
from meshterm.ui.chat import ChatScreen
from meshterm.ui.widgets import format_offset
from tests.conftest import plain as _strip_ansi

TOLERANCE = CLOCK_TOLERANCE_S
PEER = "d4e5f6a7b8c9"


def _received(sender_offset: timedelta, *, waited: bool = False, **kw) -> ChatMessage:
    """A received direct message whose sender clock is ``sender_offset`` from ours."""
    now = utcnow()
    message = Message(
        text="hello",
        sender=PEER,
        sender_timestamp=now + sender_offset,
        received_at=now,
        waited=waited,
        **kw,
    )
    return ChatMessage.from_message(message)


class _StubSession:
    def invalidate(self) -> None:
        pass


# -- the rules on ChatMessage ---------------------------------------------------------------


def test_a_correct_clock_shows_the_sender_time() -> None:
    """A sender time within the tolerance is the time that the transcript shows."""
    message = _received(timedelta(seconds=-40))
    assert not message.corrected(TOLERANCE)
    assert message.shown_at(TOLERANCE) == message.created_at
    assert message.arrived_at == message.received_at


def test_a_clock_behind_or_ahead_shows_the_arrival_time() -> None:
    """A sender time past the tolerance, in either direction, is wrong."""
    for offset in (timedelta(days=-700), timedelta(hours=3)):
        message = _received(offset)
        assert message.corrected(TOLERANCE)
        assert message.shown_at(TOLERANCE) == message.received_at
        assert message.arrived_at == message.received_at


def test_a_message_that_waited_is_wrong_only_in_the_future() -> None:
    """A message from the queue of the companion arrived some time before MeshTerm got it.

    An old sender time is then possible and correct, so only a time in the future is proof.
    Its arrival is the earlier of the two times.
    """
    old = _received(timedelta(hours=-3), waited=True)
    assert not old.corrected(TOLERANCE)
    assert old.arrived_at == old.created_at

    ahead = _received(timedelta(hours=3), waited=True)
    assert ahead.corrected(TOLERANCE)
    assert ahead.arrived_at == ahead.received_at


def test_a_room_post_from_the_backlog_keeps_its_old_time() -> None:
    """A room sends old posts with the times when they were posted. Those times are correct."""
    post = _received(timedelta(days=-3), author="a1b2c3d4")
    assert post.is_post
    assert not post.corrected(TOLERANCE)
    assert _received(timedelta(days=3), author="a1b2c3d4").corrected(TOLERANCE)


def test_our_messages_and_old_rows_have_no_arrival_time() -> None:
    """Our clock stamps our own messages. A row from before the arrival time keeps its time."""
    now = utcnow()
    sent = ChatMessage(text="hi", outbound=True, created_at=now)
    old = ChatMessage(text="hi", created_at=now - timedelta(days=900))
    for message in (sent, old):
        assert message.clock_offset_s is None
        assert not message.corrected(TOLERANCE)
        assert message.arrived_at == message.created_at


def test_the_offset_reads_in_two_units() -> None:
    """The largest unit, and the next one when it is not zero."""
    assert format_offset(0) == "in sync"
    assert format_offset(45) == "45s ahead"
    assert format_offset(-(3 * 3600 + 12 * 60 + 5)) == "3h 12m behind"
    assert format_offset(-(2 * 3600 + 30)) == "2h behind"
    assert format_offset(-(365 * 86400 + 44 * 604800)) == "1y 44w behind"


# -- the companion queue --------------------------------------------------------------------


class _Event:
    def __init__(self, payload: dict) -> None:
        self.payload = payload


def test_a_message_from_the_queue_is_marked() -> None:
    """The pump marks each message of the first drain as one that waited."""
    payload = {"text": "hi", "pubkey_prefix": PEER, "sender_timestamp": 1_700_000_000}
    assert message_from_event(_Event(payload), waited=True).waited
    assert not message_from_event(_Event(payload)).waited


# -- the database ---------------------------------------------------------------------------


def test_the_two_times_survive_a_round_trip(tmp_path: Path) -> None:
    """The arrival time and the waited flag are stored and read back."""
    repo = Repository(tmp_path / "chat.db")
    message = _received(timedelta(days=-700), waited=True)
    repo.record_chat_message(message)
    (back,) = repo.recent_chat_messages(is_channel=False, peer=PEER)
    assert back.received_at == message.received_at
    assert back.waited is True
    assert back.created_at == message.created_at
    repo.close()


def test_a_dm_from_a_clock_ahead_does_not_count_as_heard_now(tmp_path: Path) -> None:
    """The heard evidence is the arrival time, not the sender time (it was the sender time)."""
    repo = Repository(tmp_path / "chat.db")
    message = _received(timedelta(days=365))
    repo.record_chat_message(message)
    heard = repo.last_message_by_peer()[PEER]
    assert abs((heard - message.received_at).total_seconds()) < 1
    repo.close()


def test_channel_activity_counts_by_arrival(tmp_path: Path) -> None:
    """A channel message from a clock two years behind still counts in the recent activity."""
    repo = Repository(tmp_path / "chat.db")
    now = utcnow()
    message = ChatMessage.from_message(
        Message(
            text="Bob: hi",
            channel=0,
            is_channel=True,
            sender_timestamp=now - timedelta(days=730),
            received_at=now,
        ),
        channel_id="chan-1",
    )
    repo.record_chat_message(message)
    stats = repo.channel_stats()["chan-1"]
    assert stats.recent == 1
    assert stats.histogram[0] == 1
    repo.close()


def test_the_migration_adds_the_columns_and_leaves_old_rows_alone(tmp_path: Path) -> None:
    """A database from before the arrival time gets the columns. Its rows keep NULL and 0."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER,
            outbound INTEGER NOT NULL DEFAULT 0, is_channel INTEGER NOT NULL DEFAULT 0,
            channel_id TEXT, channel_idx INTEGER, peer TEXT, peer_name TEXT,
            text TEXT NOT NULL, snr REAL, acked INTEGER, created_at TEXT NOT NULL,
            scope TEXT, author TEXT
        );
        INSERT INTO messages (peer, text, created_at)
            VALUES ('d4e5f6a7b8c9', 'old', '2026-01-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()

    migrated = db.connect(path)
    row = migrated.execute("SELECT received_at, waited FROM messages").fetchone()
    assert row["received_at"] is None and row["waited"] == 0
    observation_cols = {r["name"] for r in migrated.execute("PRAGMA table_info(observations)")}
    assert "sent_at" in observation_cols
    migrated.close()


# -- the clock offset of a node -------------------------------------------------------------

NODE = "c0ffee123456"


def _advert(repo: Repository, run: int, node_key: str, sent, heard) -> None:  # noqa: ANN001
    """An ADVERT packet in the RX log: the time of its node, and our time of reception."""
    from meshterm.core.models import Observation

    raw = {
        "payload_typename": "ADVERT",
        "adv_key": node_key,
        "adv_timestamp": int(sent.timestamp()),
    }
    repo.record_observation(
        run,
        Observation(node=node_key[:12], kind="packet", path="", observed_at=heard, raw=raw),
    )


def test_an_advert_keeps_its_own_time(tmp_path: Path) -> None:
    """The RX log gives the advert time. Other packets and a time of zero store nothing."""
    from meshterm.core.models import Observation

    repo = Repository(tmp_path / "obs.db")
    run = repo.start_run("monitor", {}, None)
    now = utcnow().replace(microsecond=0)
    _advert(repo, run, NODE + "00" * 26, now - timedelta(hours=2), now)
    never_set = {"payload_typename": "ADVERT", "adv_key": "ab" * 32, "adv_timestamp": 0}
    repo.record_observation(
        run, Observation(node="ab" * 6, kind="packet", path="", observed_at=now, raw=never_set)
    )
    (sample,) = repo.advert_clock_samples()
    assert sample == (NODE, now - timedelta(hours=2), now)
    repo.close()


def test_the_offset_is_the_median_of_the_newest_three() -> None:
    """One wrong sample has no effect, and the old samples do not count."""
    from meshterm.services.clock_offsets import ClockSample, offsets

    now = utcnow()

    def sample(minutes_ago: int, offset_s: float) -> ClockSample:
        arrived = now - timedelta(minutes=minutes_ago)
        return ClockSample(NODE, True, arrived + timedelta(seconds=offset_s), arrived)

    # Old samples a day behind, then the clock was set: two good samples and one glitch.
    found = offsets(
        [sample(90, -86400), sample(80, -86400), sample(30, 4), sample(20, 9000), sample(10, 6)]
    )
    (offset,) = found
    assert offset.offset_s == 6
    assert offset.samples == 5
    assert offset.last_at == now - timedelta(minutes=10)
    assert not offset.out_of_sync(TOLERANCE)


def test_a_channel_sender_counts_for_its_node_or_under_its_name(tmp_path: Path) -> None:
    """A name with one key gives samples to that node. Another name keeps its own samples."""
    from meshterm.services.clock_offsets import offsets, window_samples

    repo = Repository(tmp_path / "chat.db")
    run = repo.start_run("monitor", {}, None)
    now = utcnow().replace(microsecond=0)
    _advert(
        repo, run, NODE + "00" * 26, now - timedelta(hours=3, minutes=5), now - timedelta(minutes=5)
    )
    for text in ("Bob: hi", "Stranger: hello"):
        repo.record_chat_message(
            ChatMessage.from_message(
                Message(
                    text=text,
                    channel=0,
                    is_channel=True,
                    sender_timestamp=now - timedelta(hours=3),
                    received_at=now,
                ),
                channel_id="c",
            )
        )
    keys = {"bob": NODE + "00" * 26}
    samples = window_samples(repo, since=None, key_of=lambda name: keys.get(name.casefold()))
    found = {(o.who, o.keyed): o for o in offsets(samples)}
    assert found[(NODE, True)].samples == 2  # the advert and the channel message
    assert found[(NODE, True)].offset_s == -3 * 3600
    assert found[("Stranger", False)].samples == 1
    repo.close()


def test_the_node_offset_joins_adverts_dms_and_its_channel_messages(tmp_path: Path) -> None:
    """The node page reads all the history of one node.

    Its channel messages count only when its name resolves to it: another node can use
    the same name.
    """
    from meshterm.services.clock_offsets import node_offset

    repo = Repository(tmp_path / "chat.db")
    run = repo.start_run("monitor", {}, None)
    now = utcnow().replace(microsecond=0)
    _advert(repo, run, NODE + "00" * 26, now - timedelta(days=2), now - timedelta(hours=1))
    repo.record_chat_message(
        ChatMessage.from_message(
            Message(
                text="hi", sender=NODE, sender_timestamp=now - timedelta(days=2), received_at=now
            )
        )
    )
    repo.record_chat_message(
        ChatMessage.from_message(
            Message(
                text="Bob: on the channel",
                channel=0,
                is_channel=True,
                sender_timestamp=now - timedelta(days=2),
                received_at=now,
            ),
            channel_id="c",
        )
    )
    mine = node_offset(repo, NODE, name="Bob", key_of=lambda _name: NODE + "00" * 26)
    assert mine is not None and mine.samples == 3
    assert mine.out_of_sync(TOLERANCE)
    assert format_offset(mine.offset_s) == "2d behind"

    other = node_offset(repo, NODE, name="Bob", key_of=lambda _name: "ab" * 32)
    assert other is not None and other.samples == 2  # the name belongs to another key
    assert node_offset(repo, "ab" * 6) is None
    repo.close()


# -- the packet log -------------------------------------------------------------------------


def test_the_paths_of_a_received_message_are_found_by_its_arrival(tmp_path: Path) -> None:
    """A sender clock a year behind does not move the search away from the packets.

    The packets of a received message come just before it arrives, so the correct MAC group
    is the last one that started before the arrival, not the next one.
    """
    from meshterm.core.models import Observation

    repo = Repository(tmp_path / "paths.db")
    run = repo.start_run("monitor", {}, None)
    now = utcnow()
    for offset, mac in ((-20, "aaaa"), (-1, "beef"), (12, "f00d")):
        repo.record_observation(
            run,
            Observation(
                node=None,
                kind="packet",
                path="3d63",
                snr=2.0,
                observed_at=now + timedelta(seconds=offset),
                raw={
                    "payload_typename": "TEXT_MSG",
                    "src_hash": "d4",
                    "dest_hash": "a1",
                    "cipher_mac": mac,
                },
            ),
        )
    message = ChatMessage(
        text="hi",
        peer=PEER,
        created_at=now - timedelta(days=365),
        received_at=now,
    )
    got, exact = direct_arrivals(repo, message, self_key="a1" + "0" * 62, peer_key=PEER)
    assert exact is True
    assert [a.frame["cipher_mac"] for a in got] == ["beef"]
    repo.close()


# -- the backfill from the packet log -------------------------------------------------------


def _old_channel_row(repo: Repository, channel_id: str, text: str, sent) -> int:  # noqa: ANN001
    """A received channel message stored before the arrival time: only the sender time."""
    message = ChatMessage(text=text, is_channel=True, channel_idx=0, channel_id=channel_id)
    message.created_at = sent
    return repo.record_chat_message(message)


def _heard(repo: Repository, run: int, body: dict, when) -> None:  # noqa: ANN001
    from meshterm.core.models import Observation

    repo.record_observation(
        run, Observation(node=None, kind="packet", path="3d", snr=1.0, observed_at=when, raw=body)
    )


def test_the_backfill_finds_the_arrival_in_the_packet_log(tmp_path: Path) -> None:
    """A stored packet with the same text and sender time is the message. Its first copy wins.

    A packet with the same text but another sender time is another message, and a message
    with no packet keeps no arrival time.
    """
    import asyncio

    from meshterm.core.channels import DEFAULT_PUBLIC_SECRET, channel_identity
    from meshterm.demo.traffic import grp_txt_body
    from meshterm.services.arrival_backfill import backfill_arrivals

    repo = Repository(tmp_path / "chat.db")
    run = repo.start_run("monitor", {}, None)
    cid = channel_identity("Public", DEFAULT_PUBLIC_SECRET)
    now = utcnow().replace(microsecond=0)
    behind = now - timedelta(days=4)  # the clock of the sender

    late = _old_channel_row(repo, cid, "klkdeck: hi", behind)
    lost = _old_channel_row(repo, cid, "klkdeck: nobody heard this", behind)
    for when, attempt in (
        (now, 0),
        (now + timedelta(seconds=1), 0),
        (now + timedelta(seconds=9), 1),
    ):
        _heard(repo, run, grp_txt_body(DEFAULT_PUBLIC_SECRET, "klkdeck: hi", behind, attempt), when)
    # The same text at another sender time: another message, which matches nothing.
    other = behind + timedelta(hours=1)
    _heard(
        repo,
        run,
        grp_txt_body(DEFAULT_PUBLIC_SECRET, "klkdeck: hi", other),
        now - timedelta(days=1),
    )

    channels = {cid: ("Public", DEFAULT_PUBLIC_SECRET)}
    assert asyncio.run(backfill_arrivals(repo, channels)) == 1
    rows = {m.row_id: m for m in repo.recent_chat_messages(is_channel=True, channel_id=cid)}
    assert rows[late].received_at == now
    assert rows[late].corrected(TOLERANCE)
    assert rows[lost].received_at is None

    # The channel is done: a second start does not decrypt the log again.
    assert repo.arrival_backfill_done() == {cid}
    assert asyncio.run(backfill_arrivals(repo, channels)) == 0
    repo.close()


# -- the screens ----------------------------------------------------------------------------


def _screen(messages: list[ChatMessage]) -> ChatScreen:
    contact = Contact(name="Bob", key_prefix=PEER)
    conv = Conversation(label="Bob", is_channel=False, contact=contact)
    return ChatScreen(conv, messages, send=None, names={PEER: "Bob"}, session=_StubSession())


def test_the_transcript_marks_a_corrected_time() -> None:
    """The corrected time shows the arrival, then ``?``. A correct time has no mark."""
    good = _received(timedelta(seconds=-10))
    bad = _received(timedelta(days=-700))
    screen = _screen([good, bad])
    lines = [_strip_ansi(line) for line in screen._render_grouped(72)]
    arrival = f"{bad.received_at.astimezone():%H:%M}"
    assert any(f"{arrival}? hello" in line for line in lines)
    assert any(f"{good.created_at.astimezone():%H:%M}  hello" in line for line in lines)
    # No divider for a day two years ago: the day follows the time that the line shows.
    assert not any(f"{bad.created_at.astimezone():%b}" in line and "──" in line for line in lines)


def test_a_picked_corrected_message_gives_the_sender_time() -> None:
    """On the selected message, a line under the body says what the sender's clock wrote."""
    bad = _received(timedelta(days=-700))
    screen = _screen([bad])
    screen._selected = 0
    text = "\n".join(_strip_ansi(line) for line in screen._render_grouped(72))
    sent = bad.created_at.astimezone()
    assert f"? its clock: {sent:%Y-%m-%d %H:%M}" in text
    assert "behind" in text

    screen._selected = None
    assert "its clock:" not in "\n".join(_strip_ansi(line) for line in screen._render_grouped(72))


def test_the_cli_history_marks_a_corrected_time_and_gives_both_times() -> None:
    """The plain face puts ``?`` after the time. The document has the two times and the flag."""
    bad = _received(timedelta(days=-700))
    listing = _transcript([bad])
    (row,) = listing.rows
    assert row["clock_corrected"] is True
    assert row["created_at"] == bad.created_at
    assert row["received_at"] == bad.received_at
    time_lane = next(c for c in listing.columns if c.lanes and c.lanes[0].header == "TIME")
    assert time_lane.lanes[0].render(row["shown_at"]).endswith("?")
