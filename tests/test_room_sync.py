# SPDX-License-Identifier: Apache-2.0
"""A room whose clock went back: the test that finds it, and the repair of the post sync.

A room stamps each post with its own clock, and it sends us only the posts after the newest
one that we got. When its clock goes back, it sends no new post. The repair adds the room
contact back as a new contact, whose sync time the firmware sets to zero.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from meshterm.core.connection import (
    ContactNotOnDeviceError,
    DeviceCommandError,
    MeshCoreDevice,
    MockDevice,
)
from meshterm.core.models import (
    CLOCK_TOLERANCE_S,
    NODE_TYPE_ROOM,
    ChatMessage,
    Contact,
    Message,
    utcnow,
)
from meshterm.persistence.repository import Repository
from meshterm.services.clock_offsets import ClockOffset, room_posts_blocked

ROOM = "ab" * 32


def _offset(seconds: float) -> ClockOffset:
    return ClockOffset(ROOM[:12], True, seconds, 3, utcnow())


def test_a_room_whose_clock_went_back_past_its_newest_post_is_blocked() -> None:
    """The newest post is later than the clock of the room now: the room sends nothing new."""
    newest = utcnow() - timedelta(minutes=10)
    assert room_posts_blocked(newest, _offset(-3 * 86400), CLOCK_TOLERANCE_S)
    assert not room_posts_blocked(newest, _offset(5), CLOCK_TOLERANCE_S)
    assert not room_posts_blocked(newest, None, CLOCK_TOLERANCE_S)  # no offset: no claim
    assert not room_posts_blocked(None, _offset(-3 * 86400), CLOCK_TOLERANCE_S)


def test_the_newest_post_counts_only_posts(tmp_path: Path) -> None:
    """A command reply of the room and our own posts are not posts that the room sent us."""
    repo = Repository(tmp_path / "rooms.db")
    now = utcnow().replace(microsecond=0)
    post = Message(
        text="hello board",
        sender=ROOM[:12],
        sender_timestamp=now - timedelta(hours=2),
        received_at=now,
        author="c0ffee12",
    )
    reply = Message(
        text="OK", sender=ROOM[:12], sender_timestamp=now + timedelta(days=9), received_at=now
    )
    repo.record_chat_message(ChatMessage.from_message(post))
    repo.record_chat_message(ChatMessage.from_message(reply))
    ours = ChatMessage(text="mine", outbound=True, peer=ROOM[:12], created_at=now)
    repo.record_chat_message(ours)
    assert repo.newest_room_post(ROOM[:12]) == now - timedelta(hours=2)
    assert repo.newest_room_post("cd" * 6) is None
    repo.close()


class _Commands:
    """A stand-in for the command set of the library: one stored record and each command."""

    def __init__(self, record: dict, *, add_fails: int = 0) -> None:
        self.record = record
        self.calls: list[tuple[str, object]] = []
        self._add_fails = add_fails

    async def get_contact_by_key(self, pubkey: bytes):  # noqa: ANN201
        return SimpleNamespace(payload=dict(self.record), is_error=lambda: False)

    async def remove_contact(self, pub: str):  # noqa: ANN201
        self.calls.append(("remove", pub))
        return SimpleNamespace(payload={}, is_error=lambda: False)

    async def add_contact(self, record: dict):  # noqa: ANN201
        self.calls.append(("add", record))
        failed = self._add_fails > 0
        self._add_fails -= 1
        return SimpleNamespace(payload={"reason": "table full"}, is_error=lambda: failed)


def _record() -> dict:
    return {
        "public_key": ROOM,
        "type": NODE_TYPE_ROOM,
        "flags": 0,
        "out_path_len": 1,
        "out_path_hash_mode": 0,
        "out_path": "3d",
        "adv_name": "MTL BBS",
        "last_advert": int(utcnow().timestamp()) - 3600,
        "adv_lat": 45.5,
        "adv_lon": -73.6,
    }


def _device(commands: _Commands) -> MeshCoreDevice:
    device = MeshCoreDevice(port="mock")
    device._mc = SimpleNamespace(commands=commands)
    return device


def test_the_reset_removes_the_room_and_adds_the_same_record_back() -> None:
    """Remove, then add: the record goes back as the device had it, as a new contact."""
    commands = _Commands(_record())
    asyncio.run(_device(commands).reset_room_sync(Contact(name="MTL BBS", public_key=ROOM)))
    assert [name for name, _ in commands.calls] == ["remove", "add"]
    assert commands.calls[1][1] == _record() | {"last_advert": commands.record["last_advert"]}


def test_a_failed_add_back_tries_again_then_says_the_room_is_off_the_device() -> None:
    """One lost reply is tried again. Two failures raise, and the message says what is gone."""
    commands = _Commands(_record(), add_fails=1)
    asyncio.run(_device(commands).reset_room_sync(Contact(name="MTL BBS", public_key=ROOM)))
    assert [name for name, _ in commands.calls] == ["remove", "add", "add"]

    commands = _Commands(_record(), add_fails=2)
    with pytest.raises(DeviceCommandError, match="removed from the device"):
        asyncio.run(_device(commands).reset_room_sync(Contact(name="MTL BBS", public_key=ROOM)))


def test_the_simulator_refuses_a_room_that_it_does_not_have() -> None:
    """The simulator keeps no sync time, but it refuses an unknown contact as firmware does."""
    device = MockDevice()
    room = Contact(name="Room", public_key=ROOM, key_prefix=ROOM[:12], node_type=NODE_TYPE_ROOM)
    with pytest.raises(ContactNotOnDeviceError):
        asyncio.run(device.reset_room_sync(room))
    asyncio.run(device.add_contact(room))
    asyncio.run(device.reset_room_sync(room))
