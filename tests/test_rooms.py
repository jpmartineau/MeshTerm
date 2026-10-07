# SPDX-License-Identifier: Apache-2.0
"""Room servers: joining one, reading its board under the author of each post, and posting.

A room server is a bulletin board on a radio. It keeps the latest posts of its members.
It sends each member the posts that the member missed. It sends them as direct messages
from itself, and each message has the first four bytes of the key of its author as a
signature. Thus these tests check the facts that a room adds to a direct exchange: who
wrote a post (``author``), what a login let us do
(:class:`~meshterm.core.models.RoomAccess`), and when a room needs a new login. They also
check that the posts of a room stay a board. The code removes duplicates, keeps the posts
apart from the command replies of the room, and raises the unread badge. The posts must
not go into anything else.

All the tests run against the simulator and temporary stores. They do not use hardware.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.cells import cell_len

from meshterm.core.admin_store import AdminStore
from meshterm.core.config import Settings
from meshterm.core.connection import MeshCoreDevice, MockDevice, message_from_event
from meshterm.core.events import MeshEvent
from meshterm.core.models import (
    NODE_TYPE_CHAT,
    NODE_TYPE_REPEATER,
    NODE_TYPE_ROOM,
    ChatMessage,
    Contact,
    Conversation,
    LoginResult,
    Message,
    RoomAccess,
    RoomLogin,
    is_direct_messageable,
    is_room,
)
from meshterm.core.preferences import Preferences
from meshterm.core.remote_config import get_setting
from meshterm.core.room_store import RoomStore
from meshterm.persistence.repository import Repository
from meshterm.services.chat_service import ChatService
from meshterm.services.event_hub import EventHub
from meshterm.services.rooms import RoomService
from meshterm.ui.room import LOGGING_IN, NO_REPLY, NOT_JOINED, RoomScreen, author_label
from meshterm.ui.tui import fkeys
from tests.conftest import plain
from tests.test_admin_login import _device, _Event, _FakeMeshCore, quick_budget  # noqa: F401

#: The room server of the simulator, in the same form as MockDevice lists it.
ROOM = Contact(
    name="Lakeside BBS",
    public_key="f6a7b8c9" + "0" * 56,
    key_prefix="f6a7b8c9",
    node_type=NODE_TYPE_ROOM,
    route_hops=("a1b2c3d4",),
)
ALICE_KEY = "d4e5f6a7"
#: A node that no contact names (the neighbour of the simulator that never advertised to us).
STRANGER_KEY = "e5f6a7b8"
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _post(text: str, author: str = ALICE_KEY, *, minutes: int = 0) -> Message:
    """One room post in the form that arrives on the wire: from the room, signed by its author."""
    return Message(
        text=text,
        sender=ROOM.key_prefix,
        sender_timestamp=NOW + timedelta(minutes=minutes),
        author=author,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Repository:
    """A Repository on a temporary database."""
    r = Repository(tmp_path / "rooms.db")
    yield r
    r.close()


# --- the model ------------------------------------------------------------------------


def test_only_a_room_server_is_a_room() -> None:
    """A room is its own section, and it is not a recipient of direct messages."""
    assert is_room(NODE_TYPE_ROOM)
    assert not is_room(NODE_TYPE_CHAT) and not is_room(None) and not is_room(NODE_TYPE_REPEATER)
    assert not is_direct_messageable(NODE_TYPE_ROOM)
    assert ROOM.is_room
    assert Conversation(label=ROOM.name, is_channel=False, contact=ROOM).is_room
    assert not Conversation(label="#x", is_channel=True, channel_id="c").is_room


@pytest.mark.parametrize(
    "payload,access",
    [
        ({"acl_permissions": 3, "permissions": 1}, RoomAccess.ADMIN),
        ({"acl_permissions": 2, "permissions": 0}, RoomAccess.MEMBER),
        # Role 1 is the "read-only" of the firmware, set by hand. The room still keeps its posts.
        ({"acl_permissions": 1, "permissions": 0}, RoomAccess.MEMBER),
        ({"acl_permissions": 0, "permissions": 2}, RoomAccess.READ_ONLY),
        # Only the high bits are different. The role is the low two bits.
        ({"acl_permissions": 0x80 | 2}, RoomAccess.MEMBER),
        # A reply from before the permissions byte: only the legacy flag.
        ({"permissions": 1}, RoomAccess.ADMIN),
        ({"permissions": 2}, RoomAccess.READ_ONLY),
        ({"permissions": 0}, RoomAccess.MEMBER),
        ({}, RoomAccess.MEMBER),
    ],
)
def test_the_access_a_login_reply_grants(payload: dict, access: RoomAccess) -> None:
    """The code reads the access from the access-list byte if it exists, or else the legacy flag."""
    assert RoomAccess.from_login(payload) is access


def test_only_a_read_only_member_cannot_post() -> None:
    """The room drops the posts of only one access."""
    assert RoomAccess.ADMIN.can_post and RoomAccess.MEMBER.can_post
    assert not RoomAccess.READ_ONLY.can_post


def test_a_room_login_is_truthy_only_when_accepted() -> None:
    """The ``if not login`` idiom keeps the meaning of LoginResult."""
    assert RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER)
    assert not RoomLogin(LoginResult.NO_REPLY)
    assert not RoomLogin(LoginResult.REFUSED)


def test_a_post_keeps_its_author_and_the_time_it_was_posted() -> None:
    """The room is the sender, the author is a separate field, and the time is that of the post."""
    chat = ChatMessage.from_message(_post("swap meet?"))
    assert chat.peer == ROOM.key_prefix
    assert chat.author == ALICE_KEY
    assert chat.is_post
    assert chat.created_at == NOW
    assert chat.key == f"dm:{ROOM.key_prefix}"  # the board of the room, with the key of the room


# --- the wire -------------------------------------------------------------------------


def _received(txt_type: int, **extra) -> SimpleNamespace:
    payload = {
        "type": "PRIV",
        "pubkey_prefix": "f6a7b8c90000",
        "txt_type": txt_type,
        "sender_timestamp": int(NOW.timestamp()),
        "text": "hello",
        **extra,
    }
    return SimpleNamespace(payload=payload)


def test_a_signed_message_names_its_author() -> None:
    """The ``signature`` of the library is the key prefix of the author. It is not a signature."""
    message = message_from_event(_received(2, signature="E5F6A7B8"))
    assert message.author == STRANGER_KEY
    assert message.is_post
    assert message.sender == "f6a7b8c90000"


@pytest.mark.parametrize("txt_type", [0, 1])
def test_plain_text_and_command_replies_have_no_author(txt_type: int) -> None:
    """A command reply of a room has the same key as its posts. Only a post has an author."""
    message = message_from_event(_received(txt_type, signature="e5f6a7b8"))
    assert message.author is None and not message.is_post


def test_a_channel_message_never_has_an_author() -> None:
    """Only room posts have authors. A channel names its sender in the text."""
    event = SimpleNamespace(
        payload={"type": "CHAN", "channel_idx": 0, "txt_type": 2, "signature": "ab", "text": "x"}
    )
    assert message_from_event(event).author is None


def test_a_room_login_reads_the_access_from_the_reply(quick_budget) -> None:  # noqa: ANN001, F811
    """One login exchange, read for the role that the room gave us."""
    from meshcore import EventType

    answer = _Event(
        EventType.LOGIN_SUCCESS,
        {"pubkey_prefix": "f6a7b8c90000", "permissions": 2, "acl_permissions": 0},
    )
    mc = _FakeMeshCore(answer=answer)
    login = asyncio.run(_device(mc).room_login(ROOM, "anything"))
    assert login.result is LoginResult.ACCEPTED
    assert login.access is RoomAccess.READ_ONLY
    assert mc.sent == [(ROOM.public_key, "anything")]


def test_a_room_that_stays_silent_is_no_reply_with_no_access(quick_budget) -> None:  # noqa: ANN001, F811
    """A room says nothing to a wrong password. It is silent, and it never refuses."""
    login = asyncio.run(_device(_FakeMeshCore()).room_login(ROOM, "wrong"))
    assert login == RoomLogin(LoginResult.NO_REPLY)


class _CommandMeshCore:
    """A replacement for a meshcore client for an admin command: it arms, sends, then hears replies.

    The class delivers ``replies`` after the command goes out. Each reply goes through the
    attribute filter of the subscription, in the same way as the dispatcher of the library
    applies it.
    """

    def __init__(self, replies: list[dict]) -> None:
        self._replies = replies
        self._subs: list = []
        self.commands = self

    def subscribe(self, event_type, callback, attribute_filters=None):  # noqa: ANN001, ANN201
        sub = SimpleNamespace(callback=callback, filters=attribute_filters or {})
        sub.unsubscribe = lambda: self._subs.remove(sub) if sub in self._subs else None
        self._subs.append(sub)
        return sub

    async def send_cmd(self, pub, cmd):  # noqa: ANN001, ANN201
        for payload in self._replies:
            event = SimpleNamespace(payload=payload)
            for sub in list(self._subs):
                if all(payload.get(k) == v for k, v in sub.filters.items()):
                    sub.callback(event)
        return None


def test_an_admin_command_reply_is_not_a_post_or_someone_elses_message() -> None:
    """An admin command reply is not a post, and it is not a message from another node.

    This is the main trap of a room: the posts of its members arrive on the same channel as
    the command reply. In the past, a post that the room pushed was taken for the answer of
    the node. A message of a companion that arrived during the command was also taken for
    the answer.
    """
    room_prefix = ROOM.public_key[:12]
    mc = _CommandMeshCore(
        [
            {"pubkey_prefix": "d4e5f6a70000", "txt_type": 0, "text": "hi from Alice"},
            {"pubkey_prefix": room_prefix, "txt_type": 2, "text": "a member's post"},
            {"pubkey_prefix": room_prefix, "txt_type": 1, "text": "> hello"},
        ]
    )
    device = MeshCoreDevice(port="mock")
    device._mc = mc
    assert asyncio.run(device._send_admin_cmd(ROOM, "get guest.password", timeout=1)) == "> hello"


# --- the room store -------------------------------------------------------------------


@pytest.fixture()
def rooms(tmp_path: Path) -> RoomStore:
    """A room store on a temporary file."""
    return RoomStore(tmp_path / "rooms.json")


def test_an_accepted_member_login_remembers_the_room_password(rooms: RoomStore) -> None:
    """The password that gave us access is the password that opens the room again."""
    rooms.record(ROOM, "hello", RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER))
    assert rooms.password(ROOM) == "hello"
    assert rooms.access(ROOM) is RoomAccess.MEMBER


def test_an_open_room_remembers_its_empty_password(rooms: RoomStore) -> None:
    """``""`` is a password that worked. It is not an absence."""
    rooms.record(ROOM, "", RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER))
    assert rooms.password(ROOM) == ""


def test_an_admin_login_remembers_the_access_but_not_the_password(rooms: RoomStore) -> None:
    """The admin password is in one place, where Repeater admin can change it."""
    rooms.record(ROOM, "s3cret", RoomLogin(LoginResult.ACCEPTED, RoomAccess.ADMIN))
    assert rooms.password(ROOM) is None
    assert rooms.access(ROOM) is RoomAccess.ADMIN


def test_silence_neither_remembers_nor_forgets(rooms: RoomStore) -> None:
    """Silence does not store a room, and it does not forget a room.

    A room is silent for a wrong password, so silence does not prove anything.
    """
    rooms.record(ROOM, "typo", RoomLogin(LoginResult.NO_REPLY))
    assert rooms.membership(ROOM) is None
    rooms.record(ROOM, "hello", RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER))
    rooms.record(ROOM, "hello", RoomLogin(LoginResult.NO_REPLY))
    assert rooms.password(ROOM) == "hello"


def test_a_refusal_forgets_the_room(rooms: RoomStore) -> None:
    """The node said no, so the code removes the password that the node refused."""
    rooms.record(ROOM, "hello", RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER))
    rooms.record(ROOM, "hello", RoomLogin(LoginResult.REFUSED))
    assert rooms.membership(ROOM) is None


# --- the room service -----------------------------------------------------------------


class _Ctx:
    """The part of AppContext that the room service and the chat recorder read."""

    def __init__(self, tmp_path: Path, device: MockDevice, repo: Repository | None = None):
        self._device = device
        self.repo = repo
        self.log = logging.getLogger("test.rooms")
        self.profile_name = None
        self.settings = Settings()
        self.preferences = Preferences()
        self.admin_store = AdminStore(tmp_path / "admin.json")
        self.room_store = RoomStore(tmp_path / "rooms.json")
        self.events = EventHub(self)
        self.rooms = RoomService(self)

    async def device(self) -> MockDevice:
        await self._device.connect()
        return self._device


def test_the_admin_password_is_tried_before_the_room_password(tmp_path: Path) -> None:
    """The code tries the admin password first. A login replaces the role of a known member.

    Thus the room password demotes an owner.
    """
    ctx = _Ctx(tmp_path, MockDevice())
    assert ctx.rooms.password(ROOM) is None and not ctx.rooms.joined(ROOM)
    ctx.room_store.record(ROOM, "hello", RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER))
    assert ctx.rooms.password(ROOM) == "hello"
    ctx.admin_store.remember(ROOM, "admin")
    assert ctx.rooms.password(ROOM) == "admin"


def test_an_admin_password_alone_is_not_a_join(tmp_path: Path) -> None:
    """The Repeater admin password lets a join go through with no question. A join is a login."""
    ctx = _Ctx(tmp_path, MockDevice())
    ctx.admin_store.remember(ROOM, "admin")
    assert ctx.rooms.password(ROOM) == "admin"
    assert not ctx.rooms.joined(ROOM) and not ctx.rooms.login_due(ROOM)


async def test_forgetting_a_room_leaves_it(tmp_path: Path) -> None:
    """The membership and its password are removed. The code transmits nothing."""
    ctx = _Ctx(tmp_path, MockDevice())
    await ctx.rooms.login(ROOM, "hello")
    ctx.rooms.forget(ROOM)
    assert not ctx.rooms.joined(ROOM) and ctx.rooms.password(ROOM) is None
    assert not ctx.rooms.heard_recently(ROOM)


def test_read_only_never_replaces_the_room_password(rooms: RoomStore) -> None:
    """A read-only login never replaces the room password.

    A room that lets read-only members in accepts any password. This does not prove that a
    password is correct.
    """
    rooms.record(ROOM, "hello", RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER))
    rooms.record(ROOM, "helo", RoomLogin(LoginResult.ACCEPTED, RoomAccess.READ_ONLY))
    assert rooms.password(ROOM) == "hello"
    assert rooms.access(ROOM) is RoomAccess.READ_ONLY
    first = RoomStore(rooms._path.with_name("other.json"))
    first.record(ROOM, "guess", RoomLogin(LoginResult.ACCEPTED, RoomAccess.READ_ONLY))
    assert first.password(ROOM) == "guess", "with nothing better, it is what gets back in"


def test_a_blank_login_never_replaces_a_password(rooms: RoomStore) -> None:
    """A blank login shows that the room knows us, until the room starts and forgets a member."""
    rooms.record(ROOM, "hello", RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER))
    rooms.record(ROOM, "", RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER))
    assert rooms.password(ROOM) == "hello"
    rooms.record(ROOM, "", RoomLogin(LoginResult.ACCEPTED, RoomAccess.ADMIN))
    assert rooms.password(ROOM) == "hello"


def test_an_admin_login_keeps_the_room_password_beside_it(rooms: RoomStore) -> None:
    """The admin password is in the admin store. The member password stays here as a fallback."""
    rooms.record(ROOM, "hello", RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER))
    rooms.record(ROOM, "admin", RoomLogin(LoginResult.ACCEPTED, RoomAccess.ADMIN))
    assert rooms.password(ROOM) == "hello" and rooms.access(ROOM) is RoomAccess.ADMIN


def test_a_refusal_forgets_only_the_password_it_refused(rooms: RoomStore) -> None:
    """If a node refuses one password, this does not show anything about another password."""
    rooms.record(ROOM, "hello", RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER))
    rooms.record(ROOM, "other", RoomLogin(LoginResult.REFUSED))
    assert rooms.password(ROOM) == "hello"


async def test_a_blank_admin_login_leaves_the_admin_password_alone(tmp_path: Path) -> None:
    """Repeater admin uses the same password, so a blank probe must not delete it."""
    ctx = _Ctx(tmp_path, MockDevice())
    await ctx.rooms.login(ROOM, "admin")
    login = await ctx.rooms.login(ROOM, "")
    assert login.access is RoomAccess.ADMIN
    assert ctx.admin_store.get(ROOM) == "admin"


async def test_one_login_to_a_room_at_a_time(tmp_path: Path) -> None:
    """If the user closes and opens a view during a login, the view uses that login."""
    device = MockDevice()
    ctx = _Ctx(tmp_path, device)
    ctx.room_store.record(ROOM, "hello", RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER))
    sent: list[str] = []
    original = device.room_login

    async def slow(room, password):  # noqa: ANN001, ANN202
        sent.append(password)
        await asyncio.sleep(0.05)
        return await original(room, password)

    device.room_login = slow
    first = asyncio.ensure_future(ctx.rooms.login(ROOM, "hello"))
    await asyncio.sleep(0)
    assert not ctx.rooms.login_due(ROOM), "a login on its way is not due again"
    second = await ctx.rooms.login(ROOM, "hello")
    assert (await first) == second
    assert sent == ["hello"]
    third = await ctx.rooms.login(ROOM, "hello")  # after the answer, the code can send a new login
    assert third and sent == ["hello", "hello"]


async def test_joining_with_the_room_password_makes_us_a_member(tmp_path: Path) -> None:
    """The stock room password, in the room of the simulator, makes us a member."""
    ctx = _Ctx(tmp_path, MockDevice())
    login = await ctx.rooms.login(ROOM, "hello")
    assert login.access is RoomAccess.MEMBER
    assert ctx.rooms.joined(ROOM) and ctx.rooms.access(ROOM) is RoomAccess.MEMBER
    assert ctx.admin_store.get(ROOM) is None  # a member password is never an admin password


async def test_joining_with_the_admin_password_remembers_it_for_repeater_admin(
    tmp_path: Path,
) -> None:
    """If the user types the admin password in a join prompt, it also administers the room."""
    ctx = _Ctx(tmp_path, MockDevice())
    login = await ctx.rooms.login(ROOM, "admin")
    assert login.access is RoomAccess.ADMIN
    assert ctx.admin_store.get(ROOM) == "admin"
    assert ctx.room_store.password(ROOM) is None
    assert ctx.rooms.password(ROOM) == "admin"


async def test_a_wrong_password_is_silence_and_is_never_remembered(tmp_path: Path) -> None:
    """The code never stores a password that did not work."""
    ctx = _Ctx(tmp_path, MockDevice())
    login = await ctx.rooms.login(ROOM, "nope")
    assert login.result is LoginResult.NO_REPLY
    assert not ctx.rooms.joined(ROOM)


async def test_a_room_heard_lately_needs_no_login(tmp_path: Path) -> None:
    """A login is a transmission. A room that still sends to us needs no login."""
    ctx = _Ctx(tmp_path, MockDevice())
    assert not ctx.rooms.login_due(ROOM), "nothing to log in with yet"
    ctx.room_store.record(ROOM, "hello", RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER))
    assert ctx.rooms.login_due(ROOM), "joined, but not heard this session"
    ctx.rooms.heard("F6A7B8C90000")  # a post, with its prefix at the width of the wire
    assert not ctx.rooms.login_due(ROOM)


async def test_a_login_counts_as_hearing_the_room(tmp_path: Path) -> None:
    """A room that just gave us access is sending. The code sends nothing when it opens it again."""
    ctx = _Ctx(tmp_path, MockDevice())
    await ctx.rooms.login(ROOM, "hello")
    assert not ctx.rooms.login_due(ROOM)


# --- the simulator's room -------------------------------------------------------------


async def test_the_simulators_room_sends_its_board_after_a_login(tmp_path: Path) -> None:
    """The room sends the oldest post first, under its author. It never sends our posts."""
    device = MockDevice()
    heard: list[Message] = []
    unsubscribe = await device.subscribe_events(
        lambda event: heard.append(event.message) if event.message is not None else None
    )
    try:
        heard.clear()  # the first burst of the stream has a synthetic DM of its own
        assert not (await device.send_direct_message(ROOM, "before joining")).acked
        login = await device.room_login(ROOM, "hello")
        assert login.access is RoomAccess.MEMBER
        while len([m for m in heard if m.is_post]) < 5:
            await asyncio.sleep(0.01)
        posts = [m for m in heard if m.is_post]
        assert all(m.sender == ROOM.key_prefix for m in posts)
        assert [m.sender_timestamp for m in posts] == sorted(m.sender_timestamp for m in posts)
        assert {m.author for m in posts} == {ALICE_KEY, "c3d4e5f6", STRANGER_KEY, "f6a7b8c9"}

        assert (await device.send_direct_message(ROOM, "mine")).acked  # kept, acked
        heard.clear()
        await device.room_login(ROOM, "")  # the room knows us. Nothing is new, except our post
        await asyncio.sleep(0.2)
        assert not [m for m in heard if m.is_post]
    finally:
        unsubscribe()
        await device.disconnect()


async def test_a_read_only_member_is_heard_but_not_kept(tmp_path: Path) -> None:
    """``allow.read.only`` accepts any password, and it drops what the member posts."""
    device = MockDevice()
    device._remote_config(ROOM)["allow.read.only"] = "on"
    login = await device.room_login(ROOM, "anything")
    assert login.access is RoomAccess.READ_ONLY
    assert not (await device.send_direct_message(ROOM, "lost")).acked
    await device.disconnect()


async def test_an_admin_posts_in_the_rooms_own_name(tmp_path: Path) -> None:
    """``room.post`` adds a notice that the room itself is the author of."""
    device = MockDevice()
    assert await device.admin_login(ROOM, "admin") is LoginResult.ACCEPTED
    assert await device.send_remote_command(ROOM, "room.post Meeting at 7") == "OK"
    assert device._room_board[-1][0] == "f6a7b8c9"
    assert await device.send_remote_command(ROOM, "get guest.password") == "> hello"
    await device.disconnect()


# --- history and the recorder ---------------------------------------------------------


def test_a_rooms_board_leaves_out_its_command_replies(repo: Repository) -> None:
    """The board has the posts and our posts. A reply under the key of the room is not on it."""
    peer = ROOM.key_prefix
    repo.record_chat_message(ChatMessage.from_message(_post("first", minutes=1)))
    repo.record_chat_message(ChatMessage(text="OK", peer=peer))  # the reply to an admin command
    repo.record_chat_message(ChatMessage(text="mine", outbound=True, peer=peer, acked=True))

    board = repo.recent_chat_messages(is_channel=False, peer=peer, posts_only=True)
    assert [m.text for m in board] == ["first", "mine"]
    assert board[0].author == ALICE_KEY
    everything = repo.recent_chat_messages(is_channel=False, peer=peer)
    assert [m.text for m in everything] == ["first", "OK", "mine"]

    repo.record_chat_message(ChatMessage(text="OK again", peer=peer))
    assert repo.last_chat_messages()[f"dm:{peer}"].text == "OK again"
    assert repo.last_chat_messages(rooms=[peer])[f"dm:{peer}"].text == "mine"


def test_a_post_already_stored_is_recognised(repo: Repository) -> None:
    """The same room, author, time, and text identify one post."""
    post = ChatMessage.from_message(_post("swap meet?"))
    assert not repo.has_room_post(post)
    repo.record_chat_message(post)
    assert repo.has_room_post(ChatMessage.from_message(_post("swap meet?")))
    assert not repo.has_room_post(ChatMessage.from_message(_post("swap meet?", minutes=1)))
    assert not repo.has_room_post(ChatMessage.from_message(_post("swap meet?", STRANGER_KEY)))


class _GatedCtx(_Ctx):
    """A context with the device-state cache, so that the badge rule can place a sender."""

    def __init__(self, tmp_path: Path, device: MockDevice, repo: Repository) -> None:
        super().__init__(tmp_path, device, repo)
        from meshterm.services.device_state import DeviceState

        self.devstate = DeviceState(self)


async def _record(chat: ChatService, ctx, message: Message) -> None:  # noqa: ANN001
    ctx.events.publish(MeshEvent.message_event(message))
    await chat._queue.join()


def _join(ctx) -> None:  # noqa: ANN001
    """Store a membership, as an accepted login does."""
    ctx.room_store.record(ROOM, "hello", RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER))


async def test_a_room_not_joined_records_its_posts_but_raises_no_badge(
    tmp_path: Path, repo: Repository
) -> None:
    """Chat has no row for a room that is not joined, so the badge points at nothing."""
    ctx = _GatedCtx(tmp_path, MockDevice(), repo)
    chat = ChatService(ctx)
    await chat.start()
    try:
        await _record(chat, ctx, _post("still sending after we forgot it"))
        assert chat.unread(f"dm:{ROOM.key_prefix}") == 0
        board = repo.recent_chat_messages(is_channel=False, peer=ROOM.key_prefix, posts_only=True)
        assert [m.text for m in board] == ["still sending after we forgot it"]
    finally:
        await chat.stop()


async def test_a_post_is_recorded_once_and_raises_the_badge_once(
    tmp_path: Path, repo: Repository
) -> None:
    """A post that the room sends again arrives two times on the wire and one time in history."""
    ctx = _GatedCtx(tmp_path, MockDevice(), repo)
    _join(ctx)
    chat = ChatService(ctx)
    await chat.start()
    try:
        await _record(chat, ctx, _post("swap meet?"))
        await _record(chat, ctx, _post("swap meet?"))
        key = f"dm:{ROOM.key_prefix}"
        assert chat.unread(key) == 1
        board = repo.recent_chat_messages(is_channel=False, peer=ROOM.key_prefix, posts_only=True)
        assert [(m.text, m.author) for m in board] == [("swap meet?", ALICE_KEY)]
        assert ctx.rooms.heard_recently(ROOM)
    finally:
        await chat.stop()


async def test_a_rooms_command_reply_is_recorded_but_silent(
    tmp_path: Path, repo: Repository
) -> None:
    """The board is listed. Other data from a room has no row for the badge to point at."""
    ctx = _GatedCtx(tmp_path, MockDevice(), repo)
    chat = ChatService(ctx)
    await chat.start()
    try:
        await _record(chat, ctx, Message(text="> hello", sender=ROOM.key_prefix))
        assert chat.unread(f"dm:{ROOM.key_prefix}") == 0
        assert [m.text for m in repo.recent_chat_messages(is_channel=False, peer=ROOM.key_prefix)]
    finally:
        await chat.stop()


async def test_posting_records_the_post_under_the_room(tmp_path: Path, repo: Repository) -> None:
    """An acknowledgement means that the room kept the post. It does not keep a read-only post."""
    device = MockDevice()
    ctx = _Ctx(tmp_path, device, repo)
    chat = ChatService(ctx)
    await device.room_login(ROOM, "hello")
    kept = await chat.send_post(ROOM, "count me in")
    assert kept.acked is True and kept.outbound and kept.peer == ROOM.key_prefix

    device._room_access[device._mock_key(ROOM)] = RoomAccess.READ_ONLY
    dropped = await chat.send_post(ROOM, "and me")
    assert dropped.acked is False
    board = repo.recent_chat_messages(is_channel=False, peer=ROOM.key_prefix, posts_only=True)
    assert [m.text for m in board] == ["count me in", "and me"]


async def test_a_post_is_never_soft_retried(tmp_path: Path, repo: Repository) -> None:
    """A retry is a new post, and each member receives it two times."""
    device = MockDevice()
    ctx = _Ctx(tmp_path, device, repo)
    ctx.preferences.direct_message_soft_retries = 2
    sends: list[str] = []
    original = device.send_direct_message

    async def counting(contact, text):  # noqa: ANN001, ANN202
        sends.append(text)
        return await original(contact, text)

    device.send_direct_message = counting
    await ChatService(ctx).send_post(ROOM, "unacked")  # we are not a member, so there is no ack
    assert sends == ["unacked"]


# --- the repeater-admin page in the words of a room server ---------------------------


def test_a_room_servers_guest_password_is_its_room_password() -> None:
    """The key is the same on both firmwares. The page of a room uses the name that fits there."""
    spec = get_setting("guest.password")
    assert spec.for_node(NODE_TYPE_ROOM).label == "Room password"
    assert spec.for_node(NODE_TYPE_REPEATER).label == "Guest password"
    assert spec.for_node(NODE_TYPE_ROOM).key == "guest.password"
    plain_spec = get_setting("txdelay")
    assert plain_spec.for_node(NODE_TYPE_ROOM) is plain_spec


# --- the room view --------------------------------------------------------------------


class _Session:
    def __init__(self) -> None:
        self.invalidations = 0

    def invalidate(self) -> None:
        self.invalidations += 1

    def run_detached(self, work):  # noqa: ANN001, ANN201
        return asyncio.ensure_future(work)


def _resolve(hash_: str | None) -> str | None:
    """The key of Alice names her, and the key of the room names it. Nothing names the stranger."""
    return {ALICE_KEY: "Alice", "f6a7b8c9": ROOM.name}.get(hash_ or "", hash_)


def _view(
    messages: list[ChatMessage] | None = None,
    *,
    access: RoomAccess | None = RoomAccess.MEMBER,
    joined: bool = True,
    login: RoomLogin | None = None,
) -> tuple[RoomScreen, dict]:
    """A board with stub hooks. ``calls`` records the requests to the hooks."""
    calls: dict = {"auto": 0, "joins": [], "sent": []}
    outcome = login if login is not None else RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER)

    async def auto_login() -> RoomLogin | None:
        calls["auto"] += 1
        await asyncio.sleep(0)  # an exchange on the air: the board continues to draw
        return outcome

    async def join(ask: bool) -> RoomLogin | None:
        calls["joins"].append(ask)
        await asyncio.sleep(0)
        return outcome

    async def send(text: str) -> ChatMessage:
        calls["sent"].append(text)
        return ChatMessage(text=text, outbound=True, peer=ROOM.key_prefix, acked=True)

    screen = RoomScreen(
        Conversation(label=ROOM.name, is_channel=False, contact=ROOM),
        messages or [],
        send=send,
        names={},
        session=_Session(),
        resolve=_resolve,
        auto_login=auto_login,
        join=join,
        joined=joined,
        access=access,
    )
    return screen, calls


async def _settled(screen: RoomScreen) -> None:
    while screen._login_open:
        await asyncio.sleep(0)


def _board() -> list[ChatMessage]:
    return [
        ChatMessage.from_message(_post("Anyone driving Saturday?", minutes=-30)),
        ChatMessage.from_message(_post("Is the north repeater down?", STRANGER_KEY, minutes=-20)),
        ChatMessage.from_message(_post("Board keeps 32 posts.", "f6a7b8c9", minutes=-10)),
        ChatMessage(text="I'll check it", outbound=True, peer=ROOM.key_prefix, acked=True),
    ]


def _compose(screen: RoomScreen) -> str:
    return plain("\n".join(screen.render_body(72))).splitlines()[-1]


def test_an_author_is_named_by_key_or_stands_as_a_grey_hash() -> None:
    """A key that something names shows as that name, in its hue. Other keys stay a hash."""
    assert author_label(ALICE_KEY, _resolve) == ("Alice", ALICE_KEY)
    assert author_label(STRANGER_KEY, _resolve) == (STRANGER_KEY, None)


def test_the_board_draws_each_post_under_its_author() -> None:
    """A contact, a stranger, the room's own notice, and us: each has its own chip."""
    screen, _ = _view(_board())
    text = plain("\n".join(screen.render_body(72)))
    for expected in ("Alice", STRANGER_KEY, ROOM.name, "Anyone driving Saturday?", "I'll check it"):
        assert expected in text
    assert screen.title == f"{ROOM.name} · member"


def test_a_post_that_arrives_twice_is_shown_once_and_a_reply_not_at_all() -> None:
    """A post that arrives two times shows one time. A reply does not show at all.

    The live view applies the same two rules as the recorder.
    """
    screen, _ = _view(_board())
    screen.append(ChatMessage.from_message(_post("Board keeps 32 posts.", "f6a7b8c9", minutes=-10)))
    screen.append(ChatMessage(text="> hello", peer=ROOM.key_prefix))  # a command reply
    assert len(screen._messages) == 4
    screen.append(ChatMessage.from_message(_post("New one", minutes=1)))
    assert screen._messages[-1].text == "New one"


async def test_a_member_posts_from_the_compose_line() -> None:
    """Enter posts, through the same path as a direct message."""
    screen, calls = _view()
    for ch in "hi":
        screen.handle("text", ch)
    screen.handle("enter")
    while screen._sending:
        await asyncio.sleep(0)
    assert calls["sent"] == ["hi"]


async def test_the_compose_line_waits_for_the_quiet_login() -> None:
    """A room that is quiet logs in in the background. Posting opens after the room accepts us."""
    screen, calls = _view(access=RoomAccess.MEMBER)
    screen.begin_auto_login()
    assert screen.title == f"{ROOM.name} · {LOGGING_IN}"
    assert "posting opens once" in _compose(screen)
    screen.handle("text", "x")
    assert screen._editor.text == "", "nothing typed while the room may not know us"
    await _settled(screen)
    assert calls["auto"] == 1 and screen.title == f"{ROOM.name} · member"
    assert screen._composing


async def test_after_silence_nothing_can_be_posted_and_the_line_says_why() -> None:
    """The reported bug: a login got silence, and the compose line continued to take posts."""
    screen, calls = _view(login=RoomLogin(LoginResult.NO_REPLY, flood=True))
    screen.begin_auto_login()
    await _settled(screen)
    assert screen.title.endswith(NO_REPLY)
    assert "no answer" in _compose(screen) and "^L" in _compose(screen)
    for ch in "hi":
        screen.handle("text", ch)
    screen.handle("enter")
    await asyncio.sleep(0)
    assert calls["sent"] == []
    assert "Enter send" not in screen.footer_hint


async def test_control_l_runs_the_explained_login() -> None:
    """^L gives the login to the Rooms flow, with its dialogs over the board, and shows the end."""
    screen, calls = _view(access=None, login=RoomLogin(LoginResult.ACCEPTED, RoomAccess.ADMIN))
    screen.handle("login")
    assert screen._login_open
    assert "^L log in" not in screen.footer_hint  # a second press does nothing
    await _settled(screen)
    assert calls["joins"] == [False]
    assert screen.title == f"{ROOM.name} · admin" and screen._composing


async def test_a_read_only_member_is_asked_for_another_password_and_offered_no_retry() -> None:
    """^L asks, because the room password lets a read-only member post. ^R is only dropped."""
    unacked = ChatMessage(text="lost", outbound=True, peer=ROOM.key_prefix, acked=False)
    screen, calls = _view([unacked], access=RoomAccess.READ_ONLY)
    assert "read-only" in _compose(screen)
    assert screen._retry_target() is None and "^R" not in screen.footer_hint
    screen.handle("login")
    await _settled(screen)
    assert calls["joins"] == [True]


def test_a_board_not_joined_takes_no_posts() -> None:
    """The user reads the stored posts on the Rooms page. To post, the user must join first."""
    screen, _ = _view(_board(), access=None, joined=False)
    assert screen.title.endswith(NOT_JOINED)
    assert "not joined" in _compose(screen) and not screen._composing


async def test_enter_on_a_picked_post_replies_to_its_author() -> None:
    """A board has many authors, so Enter on a post prepares an @mention, as a channel does."""
    screen, _ = _view(_board())
    for _ in range(3):  # our post, the notice of the room, then the post of the stranger
        screen.handle("up")
    screen.handle("enter")
    assert screen._editor.text == f"@[{STRANGER_KEY}] "


async def test_a_read_only_member_picks_a_post_to_see_its_paths_not_to_reply() -> None:
    """A read-only member has no compose line, so Enter on a post has nothing to prepare.

    A paste has no place to go.
    """
    screen, _ = _view(_board(), access=RoomAccess.READ_ONLY)
    opened: list[ChatMessage] = []

    async def paths(message: ChatMessage) -> None:
        opened.append(message)

    screen._paths = paths
    screen.handle("paste", "pasted text")
    assert not screen._paste_open and screen._editor.text == ""
    screen.handle("up")
    assert screen.footer_hint.startswith("Enter paths")
    screen.handle("enter")
    await asyncio.sleep(0)
    assert screen._editor.text == ""
    assert [m.text for m in opened] == ["I'll check it"]


def test_the_hint_names_log_in_and_fits() -> None:
    """The hint names ^L where it acts, in 72 cells, with or without a selected post."""
    screen, _ = _view(_board())
    assert "^L log in" in screen.footer_hint
    assert cell_len(screen.footer_hint) <= 72
    screen.handle("up")
    assert cell_len(screen.footer_hint) <= 72


def test_the_handheld_lane_puts_log_in_behind_f1() -> None:
    """Each chord has a chip where the keyboard has no Ctrl key to reach it."""
    screen, _ = _view(_board())
    lane = screen.picocalc_lyra_lane
    assert lane[0].opp_label == "Log in"
    assert fkeys.PICOCALC_LYRA_DECK.action_for(lane, 6) == "login"


# --- joining: the explained flow ------------------------------------------------------


class _ScriptedUi:
    """A UI surface that answers the prompt and the dialogs of the join flow from a script.

    ``choices`` are button labels, and the script presses them in order. The class keeps each
    dialog that it shows as ``(text, labels, default label)``, so that the test can read it.
    """

    def __init__(self, *, passwords=(), choices=()) -> None:  # noqa: ANN001
        self.passwords = list(passwords)
        self.choices = list(choices)
        self.asked: list[str] = []
        self.defaults: list[str] = []
        self.dialogs: list[tuple[str, list[str], str]] = []

    async def text(self, title: str, **kwargs):  # noqa: ANN003, ANN201
        self.asked.append(title)
        self.defaults.append(kwargs.get("default", ""))
        assert kwargs["validate"]("x" * 16) is not True, "a password the radio would cut"
        return self.passwords.pop(0) if self.passwords else None

    async def dialog(self, prompt, buttons, **kwargs):  # noqa: ANN001, ANN003, ANN201
        labels = [label for label, _ in buttons]
        default = labels[kwargs.get("default", 0)]
        self.dialogs.append((getattr(prompt, "plain", prompt), labels, default))
        pressed = self.choices.pop(0) if self.choices else labels[0]
        return dict(buttons)[pressed]

    def busy_dialog(self, message: str = "", *, title: str = ""):  # noqa: ANN201
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def card():  # noqa: ANN202
            yield SimpleNamespace(message=message)

        return card()


class _JoinCtx(_Ctx):
    """A context with a scripted UI and a device-state stub that the flow invalidates."""

    def __init__(self, tmp_path: Path, device: MockDevice, ui: _ScriptedUi) -> None:
        super().__init__(tmp_path, device)
        self.ui = ui
        self.devstate = SimpleNamespace(invalidate_contacts=lambda: None)


async def test_joining_with_the_right_password_says_nothing_more(tmp_path: Path) -> None:
    """A login that lets us post ends the flow with no message. The page shows the access."""
    from meshterm.ui.rooms import join_room

    ui = _ScriptedUi(passwords=["hello"])
    ctx = _JoinCtx(tmp_path, MockDevice(), ui)
    login = await join_room(ctx, ROOM)
    assert login.access is RoomAccess.MEMBER and ctx.rooms.joined(ROOM)
    assert ui.asked == [f"Join — {ROOM.name}"] and ui.dialogs == []


async def test_a_stale_route_is_explained_and_a_flood_gets_through(tmp_path: Path) -> None:
    """The case on hardware: a learned route that is dead with no error, and a flood that works."""
    from meshterm.ui.rooms import join_room

    device = MockDevice()
    device._stale_routes.add(ROOM.name)
    ui = _ScriptedUi(passwords=["hello"], choices=["Try by flood"])
    ctx = _JoinCtx(tmp_path, device, ui)
    login = await join_room(ctx, ROOM)
    assert login.access is RoomAccess.MEMBER and login.flood is True
    text, labels, default = ui.dialogs[0]
    assert text.startswith(f"{ROOM.name} didn't answer.")
    assert "route your radio learned to the room (1 hop)" in text
    assert labels == ["Cancel", "Try by flood"] and default == "Try by flood"
    assert len(ui.asked) == 1, "the flood carries the same password"


async def test_silence_by_flood_names_both_causes_and_offers_another_password(
    tmp_path: Path,
) -> None:
    """A password that never worked here can be the problem, and the dialog says so."""
    from meshterm.ui.rooms import join_room

    device = MockDevice()
    await device.reset_route(ROOM)  # no route: each login floods
    ui = _ScriptedUi(passwords=["nope", "hello"], choices=["Try again…"])
    ctx = _JoinCtx(tmp_path, device, ui)
    login = await join_room(ctx, ROOM)
    assert login.access is RoomAccess.MEMBER and len(ui.asked) == 2
    text, labels, default = ui.dialogs[0]
    assert "went out to the whole mesh" in text and "the password is wrong" in text
    assert labels == ["Cancel", "Try again…"] and default == "Try again…"
    assert ui.defaults == ["", "nope"], "the second prompt holds the password just tried"


async def test_silence_with_a_password_that_worked_points_at_reach(tmp_path: Path) -> None:
    """A stored password is still correct. The room most likely did not hear us."""
    from meshterm.ui.rooms import join_room

    device = MockDevice()
    await device.reset_route(ROOM)
    ui = _ScriptedUi(choices=["Cancel"])
    ctx = _JoinCtx(tmp_path, device, ui)
    await ctx.rooms.login(ROOM, "hello")
    await device.reset_route(ROOM)  # the answer gave it a route, so the next login floods as well
    device._unreachable.add(ROOM.name)
    login = await join_room(ctx, ROOM)
    assert login.result is LoginResult.NO_REPLY and ui.asked == []
    text, labels, _ = ui.dialogs[0]
    assert "got you in before" in text and labels == ["Cancel", "Try again"]
    assert ctx.rooms.password(ROOM) == "hello", "silence keeps the password"


async def test_read_only_is_explained_and_another_password_offered(tmp_path: Path) -> None:
    """The user has access, but not as a member. The dialog says what this means and why."""
    from meshterm.ui.rooms import join_room

    device = MockDevice()
    device._remote_config(ROOM)["allow.read.only"] = "on"
    ui = _ScriptedUi(passwords=["guess"], choices=["Keep read-only"])
    ctx = _JoinCtx(tmp_path, device, ui)
    login = await join_room(ctx, ROOM)
    assert login.access is RoomAccess.READ_ONLY
    text, labels, default = ui.dialogs[0]
    assert "read-only" in text and labels == ["Keep read-only", "Try again…"]


async def test_a_room_the_radio_forgot_is_put_back_and_tried_again(tmp_path: Path) -> None:
    """A login that the radio did not send is a separate case. The radio says why.

    The correction is one write.
    """
    from meshterm.ui.rooms import join_room

    device = MockDevice()
    device._contacts = [c for c in device._contacts if not c.is_room]
    ui = _ScriptedUi(passwords=["hello"], choices=["Add it & try again"])
    ctx = _JoinCtx(tmp_path, device, ui)
    login = await join_room(ctx, ROOM)
    assert login.access is RoomAccess.MEMBER
    text, labels, _ = ui.dialogs[0]
    assert text == "Your radio didn't send the login: this node isn't in the radio's contacts."
    assert labels == ["Cancel", "Add it & try again"]


async def test_backing_out_of_the_prompt_sends_nothing(tmp_path: Path) -> None:
    """Esc on the password prompt ends the flow, and the code sends no login."""
    from meshterm.ui.rooms import join_room

    device = MockDevice()
    ui = _ScriptedUi()
    ctx = _JoinCtx(tmp_path, device, ui)
    assert await join_room(ctx, ROOM) is None
    assert not device._room_access


def test_a_password_the_radio_would_cut_is_refused() -> None:
    """MeshCore sends 15 bytes of a password. A longer password cannot work."""
    from meshterm.ui.rooms import valid_password

    assert valid_password("x" * 15) is True
    assert valid_password("x" * 16) is not True
    assert valid_password("é" * 8) is not True, "bytes, not characters"


def test_how_long_since_the_room_was_heard_is_part_of_the_explanation(tmp_path: Path) -> None:
    """Never heard, heard just now: this is the evidence that the user uses to judge the reach."""
    from dataclasses import replace

    from meshterm.ui.rooms import silence_explanation

    ctx = _Ctx(tmp_path, MockDevice())
    silent = RoomLogin(LoginResult.NO_REPLY, flood=True)
    never = silence_explanation(ctx, replace(ROOM, last_seen=None), silent, "x")
    assert "The room has never been heard here." in never
    now = datetime.now(timezone.utc)
    fresh = silence_explanation(ctx, replace(ROOM, last_seen=now), silent, "x")
    assert "The room was heard just now." in fresh


# --- the radio's confirmation ---------------------------------------------------------


class _ConfirmingMeshCore:
    """A replacement for meshcore. Its login sends confirmations and answers as the script says."""

    def __init__(self, frames: list[tuple[str, dict]]) -> None:
        self.frames = frames
        self.subs: list = []
        self.commands = self

    def subscribe(self, event_type, callback, attribute_filters=None):  # noqa: ANN001, ANN201
        sub = SimpleNamespace(event_type=event_type, callback=callback)
        sub.unsubscribe = lambda: self.subs.remove(sub) if sub in self.subs else None
        self.subs.append(sub)
        return sub

    async def send_login_sync(self, pub, password):  # noqa: ANN001, ANN201
        from meshcore import EventType

        for name, payload in self.frames:
            event = SimpleNamespace(type=getattr(EventType, name), payload=payload)
            for sub in list(self.subs):
                if sub.event_type is event.type:
                    sub.callback(event)
        return None


def _confirmed_login(frames: list[tuple[str, dict]]) -> RoomLogin:
    device = MeshCoreDevice(port="mock")
    device._mc = _ConfirmingMeshCore(frames)
    return asyncio.run(device.room_login(ROOM, "hello"))


def test_the_radios_confirmation_says_how_the_login_went_out(quick_budget) -> None:  # noqa: ANN001, F811
    """The code matches the confirmation by the expected ack of the login.

    The expected ack is the first four key bytes of the room.
    """
    ours = bytes.fromhex(ROOM.public_key[:8])
    flood = _confirmed_login(
        [("MSG_SENT", {"type": 1, "expected_ack": ours}), ("LOGIN_SUCCESS", {"acl_permissions": 2})]
    )
    assert flood.access is RoomAccess.MEMBER and flood.flood is True
    direct = _confirmed_login([("MSG_SENT", {"type": 0, "expected_ack": ours})])
    assert direct.result is LoginResult.NO_REPLY and direct.flood is False


def test_another_commands_confirmation_is_not_ours(quick_budget) -> None:  # noqa: ANN001, F811
    """A MSG_SENT for another command in progress does not show anything about this login."""
    other = _confirmed_login([("MSG_SENT", {"type": 1, "expected_ack": b"\x00\x01\x02\x03"})])
    assert other.flood is None and other.radio_error is None


def test_a_login_the_radio_would_not_send_carries_its_reason(quick_budget) -> None:  # noqa: ANN001, F811
    """There is no confirmation, and the radio gives an error: the room is not in its contacts."""
    refused = _confirmed_login([("ERROR", {"error_code": 2})])
    assert refused.flood is None
    assert refused.radio_error == "this node isn't in the radio's contacts"


async def test_the_simulator_floods_once_its_route_is_forgotten(tmp_path: Path) -> None:
    """A stale route gets silence. If the code forgets the route, the login floods.

    The answer then gives a new route.
    """
    device = MockDevice()
    device._stale_routes.add(ROOM.name)
    stale = await device.room_login(ROOM, "hello")
    assert stale.result is LoginResult.NO_REPLY and stale.flood is False
    await device.reset_route(ROOM)
    flooded = await device.room_login(ROOM, "hello")
    assert flooded.access is RoomAccess.MEMBER and flooded.flood is True
    again = await device.room_login(ROOM, "")
    assert again.flood is False and again.access is RoomAccess.MEMBER


# --- the Rooms page -------------------------------------------------------------------


def test_the_page_lists_joined_rooms_first(tmp_path: Path, repo: Repository) -> None:
    """The joined rooms are first. The other rooms follow, with the most recently heard first.

    Each row has its lanes.
    """
    from dataclasses import replace

    from meshterm.ui.rooms import _page_items

    ctx = _Ctx(tmp_path, MockDevice(), repo)
    ctx.chat = SimpleNamespace(unread=lambda key: 0)
    now = datetime.now(timezone.utc)
    joined = replace(ROOM, last_seen=now)
    hilltop = Contact(
        name="Hilltop Swap", public_key="b7" * 32, key_prefix="b7" * 6, node_type=NODE_TYPE_ROOM
    )
    _join(ctx)
    title, items = _page_items(ctx, [hilltop, joined])
    assert title == "Rooms · 1 joined"
    rows = [i.title().plain for i in items if hasattr(i, "value")]
    assert rows[0].startswith("■ Lakeside BBS") and "member" in rows[0] and "now" in rows[0]
    assert rows[1].startswith("■ Hilltop Swap") and "never" in rows[1]


# --- end to end, through the real session ---------------------------------------------


async def test_joining_from_the_rooms_page_end_to_end(tmp_path: Path) -> None:
    """Rooms, the page of the room, Join…, the password, Open the board: read, post, leave."""
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from rich.console import Console

    from meshterm.context import AppContext
    from meshterm.core.device_store import DeviceStore
    from meshterm.ui.rooms import manage_rooms
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.tui.session import TuiSession

    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "e2e.db")
    ctx = AppContext(
        console=Console(),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )

    def board() -> list[ChatMessage]:
        return ctx.repo.recent_chat_messages(
            is_channel=False, peer=ROOM.key_prefix, posts_only=True
        )

    def top() -> str:
        return getattr(session._stack[-1], "title", "") if session._stack else ""

    async def until(check, what: str) -> None:  # noqa: ANN001
        for _ in range(500):
            if check():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"never reached: {what} (top is {top()!r})")

    try:
        with create_pipe_input() as inp:
            session = TuiSession(input=inp, output=DummyOutput())
            ctx.ui = TuiUi(session)

            async def drive() -> None:
                await until(lambda: top().startswith("Rooms"), "the Rooms page")
                inp.send_text("\r")  # the one room, under Not joined
                await until(lambda: top() == f"Room — {ROOM.name}", "the room's page")
                inp.send_text("\r")  # Join…
                await until(lambda: top() == f"Join — {ROOM.name}", "the password prompt")
                inp.send_text("hello\r")
                await until(lambda: ctx.rooms.joined(ROOM), "the join")
                await until(lambda: top() == f"Room — {ROOM.name}", "back on the room's page")
                inp.send_text("\r")  # Open the board
                await until(lambda: top().startswith(f"{ROOM.name} ·"), "the board")
                await until(lambda: len([m for m in board() if m.is_post]) >= 5, "the catch-up")
                inp.send_text("count me in\r")
                await until(lambda: [m for m in board() if m.outbound], "the post")
                inp.send_text("\x1b")  # from the board, back to the page of the room
                await until(lambda: top() == f"Room — {ROOM.name}", "the room's page again")
                inp.send_text("\x1b")  # from the page of the room, back to Rooms
                await until(lambda: top().startswith("Rooms"), "the Rooms page again")
                inp.send_text("\x1b")

            async def main() -> None:
                driver = asyncio.ensure_future(drive())
                try:
                    await manage_rooms(ctx)
                finally:
                    driver.cancel()

            await asyncio.wait_for(session.run(main()), timeout=20)

        mine = [m for m in board() if m.outbound]
        assert [(m.text, m.acked) for m in mine] == [("count me in", True)]
        assert ctx.room_store.password(ROOM) == "hello"
    finally:
        await ctx.aclose()


# --- the command line -----------------------------------------------------------------


@pytest.fixture()
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201 - a closure
    """The real CLI against the simulator, in its own home (refer to test_cli_contract)."""
    from typer.testing import CliRunner

    from meshterm.cli import app

    monkeypatch.setenv("MESHTERM_HOME", str(tmp_path / "home"))
    runner = CliRunner()
    db = tmp_path / "cli.db"

    def invoke(*args: str):  # noqa: ANN202
        return runner.invoke(app, ["--mock", "--db", str(db), *args])

    invoke.db = db
    return invoke


def test_chat_list_names_a_room_as_one(cli) -> None:  # noqa: ANN001
    """A room is its own type of conversation, on both faces."""
    import json

    assert "Lakeside BBS    room" in cli("chat", "list").stdout
    rows = json.loads(cli("--json", "chat", "list").stdout)
    kinds = {r["node"]["name"]: r["kind"] for r in rows if r["node"]}
    assert kinds["Lakeside BBS"] == "room" and kinds["Alice"] == "direct"


def test_rooms_join_answers_with_the_access_won(cli) -> None:  # noqa: ANN001
    """The plain face shows only the access word. JSON has the room, the access, and the route."""
    import json

    from meshterm.core import exitcodes

    unknown = cli("rooms", "join", "Lakeside BBS")
    assert unknown.exit_code == exitcodes.USAGE, "nothing remembered, nothing sent"
    assert cli("rooms", "join", "Alice", "--password", "x").exit_code == exitcodes.USAGE
    long = cli("rooms", "join", "Lakeside BBS", "--password", "x" * 16)
    assert long.exit_code == exitcodes.USAGE, "a password the radio would cut"

    silent = cli("rooms", "join", "Lakeside BBS", "--password", "nope")
    assert silent.exit_code == exitcodes.DEVICE
    assert silent.stdout == "" and "didn't answer" in silent.stderr

    joined = cli("rooms", "join", "Lakeside BBS", "--password", "hello")
    assert joined.exit_code == exitcodes.OK and joined.stdout == "member\n"
    again = json.loads(cli("--json", "rooms", "join", "Lakeside BBS").stdout)  # a stored password
    assert again["access"] == "member" and again["route"] == "direct"
    assert again["room"]["name"] == "Lakeside BBS" and again["room"]["type"] == "room server"
    flooded = json.loads(cli("--json", "rooms", "join", "Lakeside BBS", "--flood").stdout)
    assert flooded["route"] == "flood"


def test_rooms_list_and_forget(cli) -> None:  # noqa: ANN001
    """The list has each room that the radio knows, joined or not. A forget is local and silent."""
    import json

    from meshterm.core import exitcodes

    header, *rows = cli("rooms", "list").stdout.splitlines()
    assert header.split() == ["ROOM", "ACCESS", "HEARD", "LAST_POST", "UNREAD"]
    assert rows[0].startswith("Lakeside BBS")
    cli("rooms", "join", "Lakeside BBS", "--password", "hello")
    docs = json.loads(cli("--json", "rooms", "list").stdout)
    assert docs[0]["joined"] is True and docs[0]["access"] == "member"
    forgot = cli("rooms", "forget", "Lakeside BBS")
    assert forgot.exit_code == exitcodes.OK and forgot.stdout == ""
    assert cli("rooms", "forget", "Lakeside BBS").exit_code == exitcodes.NO_RESULT
    assert json.loads(cli("--json", "rooms", "list").stdout)[0]["joined"] is False


def test_chat_history_reads_a_room_by_author(cli) -> None:  # noqa: ANN001
    """AUTHOR is in the place of PEER. A command reply is not on the board. The form is the same."""
    import json

    repo = Repository(cli.db)
    try:
        repo.record_chat_message(ChatMessage.from_message(_post("swap meet?")))
        repo.record_chat_message(ChatMessage.from_message(_post("north down?", STRANGER_KEY)))
        repo.record_chat_message(ChatMessage(text="> hello", peer=ROOM.key_prefix))
    finally:
        repo.close()

    header, *rows = cli("chat", "history", "--to", "Lakeside BBS").stdout.splitlines()
    assert header.split() == ["TIME", "DIR", "AUTHOR", "SNR_DB", "TEXT"]
    assert "Alice (d4e5f6a7)" in rows[0] and rows[0].endswith("swap meet?")
    assert STRANGER_KEY in rows[1] and "(" not in rows[1].split("north")[0]
    assert len(rows) == 2

    docs = json.loads(cli("--json", "chat", "history", "--to", "Lakeside BBS").stdout)
    assert docs[0]["author"]["name"] == "Alice" and docs[0]["author"]["hash"] == ALICE_KEY
    assert docs[1]["author"] == {
        "name": None,
        "key": None,
        "hash": STRANGER_KEY,
        "type": None,
        "self": False,
    }


def test_chat_send_to_a_room_is_a_post(cli) -> None:  # noqa: ANN001
    """The receipt says that the message went to a room."""
    import json

    receipt = json.loads(cli("--json", "chat", "send", "--to", "Lakeside BBS", "hi").stdout)
    assert receipt["kind"] == "room"
    assert receipt["acked"] is False, "this simulator never joined, so the room drops it"
