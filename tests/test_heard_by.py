# SPDX-License-Identifier: Apache-2.0
"""Tests for the verdict on our channel messages: did a relay past our near relays hear it?

The pure part (:func:`judge` and its helpers) is tested with plain paths. The tracker runs
against a real temporary repository, with GRP_TXT packets in the firmware form, so the
tests also use the window query, the decryption, and the stored verdicts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Hash import HMAC, SHA256

from meshterm.core.channels import channel_hash, derive_secret
from meshterm.core.models import ChatMessage, Observation, utcnow
from meshterm.persistence.repository import Repository
from meshterm.services.heard_by import (
    BEYOND,
    NEARBY,
    UNHEARD,
    WAITING,
    HeardTracker,
    is_near,
    judge,
    near_relays,
    relay_paths,
)
from meshterm.services.message_paths import Arrival, channel_arrivals

SECRET = derive_secret("#general")

# Invented repeaters: two near relays at home (one of them on a yagi), and two far ones.
HOME_OMNI = "3d00aa000001"
HOME_YAGI = "27d4bb000002"
NEAR = (HOME_OMNI, HOME_YAGI)


def _grp_txt_raw(secret: bytes, text: str, *, sent: datetime | None = None) -> dict:
    """A GRP_TXT packet payload in the firmware form, with ``sent`` as its sender time."""
    stamp = int(sent.timestamp()) if sent is not None else 0
    plain = stamp.to_bytes(4, "little") + bytes([0]) + text.encode("utf-8")
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
    return repo, repo.start_run("monitor", {}, None)


def _copy(repo: Repository, run: int, *, when: datetime, path: str, text: str, sent=None):
    repo.record_observation(
        run,
        Observation(
            node=None,
            kind="packet",
            path=path,
            snr=1.0,
            observed_at=when,
            raw=_grp_txt_raw(SECRET, f"Homestead: {text}", sent=sent),
        ),
    )


def _sent(repo: Repository, text: str, at: datetime) -> ChatMessage:
    message = ChatMessage(
        text=text, outbound=True, is_channel=True, channel_id="general", created_at=at
    )
    message.row_id = repo.record_chat_message(message)
    return message


# --- the pure verdict ------------------------------------------------------------------


def test_near_relays_are_the_first_hops_of_the_outbound_paths() -> None:
    """Each outbound path gives its first hop one time, in order, in lowercase."""
    paths = [("3D00AA000001", "51548cda57ae"), ("27d4bb000002", "3d00aa000001"), ()]
    assert near_relays(paths) == (HOME_OMNI, HOME_YAGI)


def test_a_hash_matches_a_near_relay_by_its_prefix() -> None:
    """A one-byte hash matches the longer key prefix of a near relay, in either case."""
    assert is_near("3D", NEAR)
    assert is_near("27d4", NEAR)
    assert not is_near("51", NEAR)
    assert not is_near("", NEAR)


def test_a_relay_past_the_near_relays_makes_the_verdict_beyond() -> None:
    """A copy through a far relay proves that the message went farther, at once."""
    heard = judge([("3d", "51"), ("27", "3d")], NEAR, age_s=1, wait_s=30)
    assert heard.state == BEYOND
    assert heard.beyond == ("51",)
    assert heard.via == ("3d",)
    assert heard.copies == 2


def test_near_relays_are_near_at_any_place_in_the_path() -> None:
    """The yagi relay after the omni relay is still a near relay, so nothing went farther."""
    heard = judge([("27", "3d")], NEAR, age_s=60, wait_s=30)
    assert heard.state == NEARBY
    assert heard.beyond == ()


def test_the_verdict_waits_before_it_says_nearby_or_unheard() -> None:
    """Inside the wait, no copy and near copies both wait. After it, they differ."""
    assert judge([], NEAR, age_s=5, wait_s=30).state == WAITING
    assert judge([("3d",)], NEAR, age_s=5, wait_s=30).state == WAITING
    assert judge([("3d",)], NEAR, age_s=31, wait_s=30).state == NEARBY
    assert judge([], NEAR, age_s=31, wait_s=30).state == UNHEARD


def test_with_no_near_relays_each_relay_counts() -> None:
    """With no outbound paths, a relay next to us is proof too."""
    heard = judge([("3d",), ("27", "51")], (), age_s=1, wait_s=30)
    assert heard.state == BEYOND
    assert heard.beyond == ("3d", "27", "51")
    assert heard.via == ()


def test_the_via_names_each_near_relay_that_started_a_far_copy() -> None:
    """Both repeaters at home show when each of them carried the message out."""
    heard = judge([("3d", "51"), ("27", "a9", "51")], NEAR, age_s=1, wait_s=30)
    assert heard.beyond == ("51", "a9")
    assert heard.via == ("3d", "27")


def test_relay_paths_skip_the_copies_that_tell_nothing() -> None:
    """An empty path, and a direct-routed copy that used its route, name no relay."""
    now = utcnow()
    arrivals = [
        Arrival(when=now, hops=("3d",), snr=None, routed=False),
        Arrival(when=now, hops=(), snr=None, routed=False),
        Arrival(when=now, hops=(), snr=None, routed=True),
    ]
    assert relay_paths(arrivals) == [("3d",)]


# --- the match of our own messages -----------------------------------------------------


def test_two_sends_of_the_same_text_keep_their_own_copies(tmp_path: Path) -> None:
    """Two "test" messages five minutes apart do not share copies: the sender time decides."""
    repo, run = _repo(tmp_path)
    first_at = utcnow().replace(microsecond=0) - timedelta(minutes=5)
    second_at = first_at + timedelta(minutes=5)
    first = _sent(repo, "test", first_at)
    second = _sent(repo, "test", second_at)
    _copy(repo, run, when=first_at + timedelta(seconds=2), path="3d", text="test", sent=first_at)
    _copy(repo, run, when=second_at + timedelta(seconds=2), path="51", text="test", sent=second_at)
    one = channel_arrivals(repo, first, channel_name="#general", secret=SECRET)
    two = channel_arrivals(repo, second, channel_name="#general", secret=SECRET)
    assert [a.hops for a in one] == [("3d",)]
    assert [a.hops for a in two] == [("51",)]
    repo.close()


# --- the tracker -----------------------------------------------------------------------


def _tracker(repo: Repository, now: datetime, *, near=NEAR) -> HeardTracker:
    return HeardTracker(
        repo,
        channel_name="#general",
        secret=SECRET,
        near_now=near,
        wait_s=lambda: 30.0,
        clock=lambda: now,
    )


def test_the_tracker_gives_no_verdict_before_it_examined_the_message(tmp_path: Path) -> None:
    """The paint never touches the packet log: an unexamined message has no mark yet."""
    repo, _run = _repo(tmp_path)
    now = utcnow().replace(microsecond=0)
    message = _sent(repo, "hello", now)
    tracker = _tracker(repo, now)
    assert tracker.verdict(message) is None
    asyncio.run(tracker.fill([message]))
    verdict = tracker.verdict(message)
    assert verdict is not None and verdict.state == WAITING
    repo.close()


def test_the_tracker_ignores_other_messages(tmp_path: Path) -> None:
    """A received message and a direct message have no verdict."""
    repo, _run = _repo(tmp_path)
    now = utcnow()
    inbound = ChatMessage(text="hi", is_channel=True, created_at=now, row_id=1)
    direct = ChatMessage(text="hi", outbound=True, created_at=now, row_id=2)
    tracker = _tracker(repo, now)
    asyncio.run(tracker.fill([inbound, direct]))
    assert tracker.verdict(inbound) is None
    assert tracker.verdict(direct) is None
    repo.close()


def test_a_late_copy_changes_the_verdict(tmp_path: Path) -> None:
    """A copy through a far relay after the wait turns the verdict from nearby to beyond."""
    repo, run = _repo(tmp_path)
    sent_at = utcnow().replace(microsecond=0) - timedelta(seconds=40)
    message = _sent(repo, "hello", sent_at)
    _copy(repo, run, when=sent_at + timedelta(seconds=1), path="3d", text="hello", sent=sent_at)
    tracker = _tracker(repo, sent_at + timedelta(seconds=40))
    asyncio.run(tracker.fill([message]))
    assert tracker.verdict(message).state == NEARBY  # type: ignore[union-attr]
    _copy(repo, run, when=sent_at + timedelta(seconds=35), path="3d,51", text="hello", sent=sent_at)
    assert tracker.refresh([message]) is True
    verdict = tracker.verdict(message)
    assert verdict is not None and verdict.state == BEYOND and verdict.beyond == ("51",)
    repo.close()


def test_an_old_message_is_stored_and_read_back(tmp_path: Path) -> None:
    """Out of the match window, the paths are stored, and the next visit reads them."""
    repo, run = _repo(tmp_path)
    sent_at = utcnow().replace(microsecond=0) - timedelta(hours=2)
    message = _sent(repo, "old one", sent_at)
    _copy(
        repo, run, when=sent_at + timedelta(seconds=3), path="27,9f", text="old one", sent=sent_at
    )
    asyncio.run(_tracker(repo, utcnow()).fill([message]))
    stored = repo.heard_records([message.row_id])  # type: ignore[list-item]
    assert stored[message.row_id] == (NEAR, [("27", "9f")])  # type: ignore[index]

    # A second visit with other near relays keeps the ones that the verdict used.
    again = _tracker(repo, utcnow(), near=())
    asyncio.run(again.fill([message]))
    verdict = again.verdict(message)
    assert verdict is not None and verdict.beyond == ("9f",) and verdict.via == ("27",)
    repo.close()


def test_the_near_relays_of_the_send_win_over_the_ones_of_today(tmp_path: Path) -> None:
    """A message keeps the near relays of its send, also after a change of site."""
    repo, run = _repo(tmp_path)
    sent_at = utcnow().replace(microsecond=0) - timedelta(seconds=60)
    message = _sent(repo, "from home", sent_at)
    repo.set_near_hops(message.row_id, NEAR)  # type: ignore[arg-type]
    _copy(repo, run, when=sent_at + timedelta(seconds=2), path="3d", text="from home", sent=sent_at)
    tracker = _tracker(repo, sent_at + timedelta(seconds=60), near=("51000000",))
    asyncio.run(tracker.fill([message]))
    assert tracker.verdict(message).state == NEARBY  # type: ignore[union-attr]
    repo.close()


def test_a_message_sent_while_the_screen_is_open_waits(tmp_path: Path) -> None:
    """A new send starts in the wait, with the near relays that apply now."""
    repo, _run = _repo(tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    message = _sent(repo, "new", now)
    tracker = _tracker(repo, now)
    before = tracker.revision
    tracker.note_sent(message)
    assert tracker.revision != before
    assert tracker.waiting([message])
    repo.close()
