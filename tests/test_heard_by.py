# SPDX-License-Identifier: Apache-2.0
"""Tests for the verdict on our channel messages: did a repeater send a copy back?

The pure part (:func:`judge` and its helpers) is tested with plain paths. The tracker runs
against a real temporary repository, with GRP_TXT packets in the firmware form, so the
tests also use the window query, the decryption, and the stored verdicts.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Hash import HMAC, SHA256

from meshterm.core.channels import channel_hash, derive_secret
from meshterm.core.models import ChatMessage, Observation, utcnow
from meshterm.persistence.repository import Repository
from meshterm.services.heard_by import (
    HEARD,
    UNHEARD,
    WAITING,
    HeardTracker,
    judge,
    relay_paths,
)
from meshterm.services.message_paths import Arrival, channel_arrivals

SECRET = derive_secret("#general")


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


def test_each_relay_in_the_copies_counts_one_time() -> None:
    """The repeater next to us counts, and a relay in two copies counts one time."""
    heard = judge([("3D",), ("27", "3d")], age_s=1, wait_s=30)
    assert heard.state == HEARD
    assert heard.relays == ("3d", "27")
    assert heard.copies == 2


def test_the_verdict_waits_before_it_says_unheard() -> None:
    """With no copy, the verdict waits, then it is unheard. A copy ends the wait at once."""
    assert judge([], age_s=5, wait_s=30).state == WAITING
    assert judge([], age_s=31, wait_s=30).state == UNHEARD
    assert judge([("3d",)], age_s=5, wait_s=30).state == HEARD


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


def _tracker(repo: Repository, now: datetime) -> HeardTracker:
    return HeardTracker(
        repo,
        channel_name="#general",
        secret=SECRET,
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
    """A copy after the wait turns the verdict from unheard to heard."""
    repo, run = _repo(tmp_path)
    sent_at = utcnow().replace(microsecond=0) - timedelta(seconds=40)
    message = _sent(repo, "hello", sent_at)
    tracker = _tracker(repo, sent_at + timedelta(seconds=40))
    asyncio.run(tracker.fill([message]))
    assert tracker.verdict(message).state == UNHEARD  # type: ignore[union-attr]
    _copy(repo, run, when=sent_at + timedelta(seconds=35), path="3d", text="hello", sent=sent_at)
    assert tracker.refresh([message]) is True
    verdict = tracker.verdict(message)
    assert verdict is not None and verdict.state == HEARD and verdict.relays == ("3d",)
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
    assert repo.heard_records([message.row_id]) == {message.row_id: [("27", "9f")]}  # type: ignore[list-item,dict-item]

    again = _tracker(repo, utcnow())
    asyncio.run(again.fill([message]))
    verdict = again.verdict(message)
    assert verdict is not None and verdict.relays == ("27", "9f")
    repo.close()


def test_a_message_sent_while_the_screen_is_open_waits(tmp_path: Path) -> None:
    """A new send starts in the wait."""
    repo, _run = _repo(tmp_path)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    message = _sent(repo, "new", now)
    tracker = _tracker(repo, now)
    before = tracker.revision
    tracker.note_sent(message)
    assert tracker.revision != before
    assert tracker.waiting([message])
    repo.close()


def test_the_first_build_of_the_table_is_made_again(tmp_path: Path) -> None:
    """A database with the near relays column of the first build gets the table again."""
    path = tmp_path / "old.db"
    Repository(path).close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE heard_relays")
    conn.execute(
        "CREATE TABLE heard_relays (message_id INTEGER PRIMARY KEY, near_hops TEXT, paths TEXT)"
    )
    conn.commit()
    conn.close()
    repo = Repository(path)
    cols = {row["name"] for row in repo._conn.execute("PRAGMA table_info(heard_relays)")}
    assert cols == {"message_id", "paths"}
    repo.close()
