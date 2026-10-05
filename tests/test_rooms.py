# SPDX-License-Identifier: Apache-2.0
"""Room servers: joining one, reading its board under each post's author, and posting.

A room server is a bulletin board on a radio. It keeps its members' latest posts and sends
each member the ones they missed — as direct messages from *itself*, each signed with the
first four bytes of its author's key. So the facts these tests pin are the ones a room adds
to a direct exchange: who wrote a post (``author``), what a login let us do
(:class:`~meshterm.core.models.RoomAccess`), when a room needs logging in to again, and
that a room's posts stay a board — deduplicated, kept apart from its command replies, and
raising the unread badge — rather than leaking into anything else.

Everything runs against the simulator and scratch stores; no hardware.
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

#: The simulator's room server, exactly as MockDevice lists it.
ROOM = Contact(
    name="Lakeside BBS",
    public_key="f6a7b8c9" + "0" * 56,
    key_prefix="f6a7b8c9",
    node_type=NODE_TYPE_ROOM,
    route_hops=("a1b2c3d4",),
)
ALICE_KEY = "d4e5f6a7"
#: A node no contact names (the simulator's neighbour that never advertised to us).
STRANGER_KEY = "e5f6a7b8"
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)


def _post(text: str, author: str = ALICE_KEY, *, minutes: int = 0) -> Message:
    """One room post as the wire hands it over: from the room, signed by its author."""
    return Message(
        text=text,
        sender=ROOM.key_prefix,
        sender_timestamp=NOW + timedelta(minutes=minutes),
        author=author,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Repository:
    """A Repository on a scratch database."""
    r = Repository(tmp_path / "rooms.db")
    yield r
    r.close()


# --- the model ------------------------------------------------------------------------


def test_only_a_room_server_is_a_room() -> None:
    """A room is its own section — and still not a direct-message recipient."""
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
        # Role 1 is the firmware's "read-only", set by hand; the room still keeps its posts.
        ({"acl_permissions": 1, "permissions": 0}, RoomAccess.MEMBER),
        ({"acl_permissions": 0, "permissions": 2}, RoomAccess.READ_ONLY),
        # Only the high bits differ — the role is the low two.
        ({"acl_permissions": 0x80 | 2}, RoomAccess.MEMBER),
        # A reply from before the permissions byte: only the legacy flag.
        ({"permissions": 1}, RoomAccess.ADMIN),
        ({"permissions": 2}, RoomAccess.READ_ONLY),
        ({"permissions": 0}, RoomAccess.MEMBER),
        ({}, RoomAccess.MEMBER),
    ],
)
def test_the_access_a_login_reply_grants(payload: dict, access: RoomAccess) -> None:
    """Read from the access-list byte where there is one, the legacy flag where not."""
    assert RoomAccess.from_login(payload) is access


def test_only_a_read_only_member_cannot_post() -> None:
    """The one access a room drops posts from."""
    assert RoomAccess.ADMIN.can_post and RoomAccess.MEMBER.can_post
    assert not RoomAccess.READ_ONLY.can_post


def test_a_room_login_is_truthy_only_when_accepted() -> None:
    """The ``if not login`` idiom keeps LoginResult's meaning."""
    assert RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER)
    assert not RoomLogin(LoginResult.NO_REPLY)
    assert not RoomLogin(LoginResult.REFUSED)


def test_a_post_keeps_its_author_and_the_time_it_was_posted() -> None:
    """The room is the sender; the author rides beside it; the time is the post's own."""
    chat = ChatMessage.from_message(_post("swap meet?"))
    assert chat.peer == ROOM.key_prefix
    assert chat.author == ALICE_KEY
    assert chat.is_post
    assert chat.created_at == NOW
    assert chat.key == f"dm:{ROOM.key_prefix}"  # the room's board, keyed like the room


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
    """The library's ``signature`` is the author's key prefix, not a signature."""
    message = message_from_event(_received(2, signature="E5F6A7B8"))
    assert message.author == STRANGER_KEY
    assert message.is_post
    assert message.sender == "f6a7b8c90000"


@pytest.mark.parametrize("txt_type", [0, 1])
def test_plain_text_and_command_replies_have_no_author(txt_type: int) -> None:
    """A room's command reply shares its key with its posts; only a post has an author."""
    message = message_from_event(_received(txt_type, signature="e5f6a7b8"))
    assert message.author is None and not message.is_post


def test_a_channel_message_never_has_an_author() -> None:
    """Authors belong to room posts; a channel names its sender in the text."""
    event = SimpleNamespace(
        payload={"type": "CHAN", "channel_idx": 0, "txt_type": 2, "signature": "ab", "text": "x"}
    )
    assert message_from_event(event).author is None


def test_a_room_login_reads_the_access_from_the_reply(quick_budget) -> None:  # noqa: ANN001, F811
    """One login exchange, read for the role the room filed us under."""
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
    """A room says nothing to a wrong password — silence, never a refusal."""
    login = asyncio.run(_device(_FakeMeshCore()).room_login(ROOM, "wrong"))
    assert login == RoomLogin(LoginResult.NO_REPLY)


class _CommandMeshCore:
    """A meshcore client stand-in for an admin command: arms, sends, then hears replies.

    ``replies`` are delivered after the command goes out, each through the subscription's
    attribute filter exactly as the library's dispatcher applies it.
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
    """THE trap a room sets: its members' posts arrive down the command reply's channel.

    A post pushed by the room, and a companion's message landing mid-command, both used to
    be taken for the node's answer.
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
    """A room store on a scratch file."""
    return RoomStore(tmp_path / "rooms.json")


def test_an_accepted_member_login_remembers_the_room_password(rooms: RoomStore) -> None:
    """The password that got us in is the one opening the room again uses."""
    rooms.record(ROOM, "hello", RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER))
    assert rooms.password(ROOM) == "hello"
    assert rooms.access(ROOM) is RoomAccess.MEMBER


def test_an_open_room_remembers_its_empty_password(rooms: RoomStore) -> None:
    """``""`` is a password that worked, not an absence."""
    rooms.record(ROOM, "", RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER))
    assert rooms.password(ROOM) == ""


def test_an_admin_login_remembers_the_access_but_not_the_password(rooms: RoomStore) -> None:
    """The admin password lives in one place, where Repeater admin can change it."""
    rooms.record(ROOM, "s3cret", RoomLogin(LoginResult.ACCEPTED, RoomAccess.ADMIN))
    assert rooms.password(ROOM) is None
    assert rooms.access(ROOM) is RoomAccess.ADMIN


def test_silence_neither_remembers_nor_forgets(rooms: RoomStore) -> None:
    """A room is silent for a wrong password, so silence proves nothing either way."""
    rooms.record(ROOM, "typo", RoomLogin(LoginResult.NO_REPLY))
    assert rooms.membership(ROOM) is None
    rooms.record(ROOM, "hello", RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER))
    rooms.record(ROOM, "hello", RoomLogin(LoginResult.NO_REPLY))
    assert rooms.password(ROOM) == "hello"


def test_a_refusal_forgets_the_room(rooms: RoomStore) -> None:
    """The node said no: the password it refused is gone."""
    rooms.record(ROOM, "hello", RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER))
    rooms.record(ROOM, "hello", RoomLogin(LoginResult.REFUSED))
    assert rooms.membership(ROOM) is None


# --- the room service -----------------------------------------------------------------


class _Ctx:
    """The slice of AppContext the room service and the chat recorder read."""

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


def test_the_room_password_is_tried_before_the_admin_password(tmp_path: Path) -> None:
    """A member's password first; the admin password, which also gets us in, after."""
    ctx = _Ctx(tmp_path, MockDevice())
    assert ctx.rooms.password(ROOM) is None and not ctx.rooms.joined(ROOM)
    ctx.admin_store.remember(ROOM, "admin")
    assert ctx.rooms.password(ROOM) == "admin"
    ctx.room_store.record(ROOM, "hello", RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER))
    assert ctx.rooms.password(ROOM) == "hello"


async def test_joining_with_the_room_password_makes_us_a_member(tmp_path: Path) -> None:
    """The stock room password, the simulator's room, a member."""
    ctx = _Ctx(tmp_path, MockDevice())
    login = await ctx.rooms.login(ROOM, "hello")
    assert login.access is RoomAccess.MEMBER
    assert ctx.rooms.joined(ROOM) and ctx.rooms.access(ROOM) is RoomAccess.MEMBER
    assert ctx.admin_store.get(ROOM) is None  # a member password is never an admin one


async def test_joining_with_the_admin_password_remembers_it_for_repeater_admin(
    tmp_path: Path,
) -> None:
    """Typed into a room's join prompt, it works for administering the room too."""
    ctx = _Ctx(tmp_path, MockDevice())
    login = await ctx.rooms.login(ROOM, "admin")
    assert login.access is RoomAccess.ADMIN
    assert ctx.admin_store.get(ROOM) == "admin"
    assert ctx.room_store.password(ROOM) is None
    assert ctx.rooms.password(ROOM) == "admin"


async def test_a_wrong_password_is_silence_and_is_never_remembered(tmp_path: Path) -> None:
    """A password that never worked is never written down."""
    ctx = _Ctx(tmp_path, MockDevice())
    login = await ctx.rooms.login(ROOM, "nope")
    assert login.result is LoginResult.NO_REPLY
    assert not ctx.rooms.joined(ROOM)


async def test_a_room_heard_lately_needs_no_login(tmp_path: Path) -> None:
    """A login is a transmission; a room still sending to us needs none."""
    ctx = _Ctx(tmp_path, MockDevice())
    assert not ctx.rooms.login_due(ROOM), "nothing to log in with yet"
    ctx.room_store.record(ROOM, "hello", RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER))
    assert ctx.rooms.login_due(ROOM), "joined, but not heard this session"
    ctx.rooms.heard("F6A7B8C90000")  # a post, its prefix at the wire's width
    assert not ctx.rooms.login_due(ROOM)


async def test_a_login_counts_as_hearing_the_room(tmp_path: Path) -> None:
    """A room that just let us in is sending: opening it again sends nothing."""
    ctx = _Ctx(tmp_path, MockDevice())
    await ctx.rooms.login(ROOM, "hello")
    assert not ctx.rooms.login_due(ROOM)


# --- the simulator's room -------------------------------------------------------------


async def test_the_simulators_room_sends_its_board_after_a_login(tmp_path: Path) -> None:
    """Oldest first, from the room, each under its author — and never our own posts."""
    device = MockDevice()
    heard: list[Message] = []
    unsubscribe = await device.subscribe_events(
        lambda event: heard.append(event.message) if event.message is not None else None
    )
    try:
        heard.clear()  # the stream's opening burst carries a synthetic DM of its own
        assert await device.send_direct_message(ROOM, "before joining") is None
        login = await device.room_login(ROOM, "hello")
        assert login.access is RoomAccess.MEMBER
        while len([m for m in heard if m.is_post]) < 5:
            await asyncio.sleep(0.01)
        posts = [m for m in heard if m.is_post]
        assert all(m.sender == ROOM.key_prefix for m in posts)
        assert [m.sender_timestamp for m in posts] == sorted(m.sender_timestamp for m in posts)
        assert {m.author for m in posts} == {ALICE_KEY, "c3d4e5f6", STRANGER_KEY, "f6a7b8c9"}

        assert await device.send_direct_message(ROOM, "mine") is not None  # kept, acked
        heard.clear()
        await device.room_login(ROOM, "")  # the room knows us; nothing new but our own
        await asyncio.sleep(0.2)
        assert not [m for m in heard if m.is_post]
    finally:
        unsubscribe()
        await device.disconnect()


async def test_a_read_only_member_is_heard_but_not_kept(tmp_path: Path) -> None:
    """``allow.read.only`` lets any password in — and drops what it posts."""
    device = MockDevice()
    device._remote_config(ROOM)["allow.read.only"] = "on"
    login = await device.room_login(ROOM, "anything")
    assert login.access is RoomAccess.READ_ONLY
    assert await device.send_direct_message(ROOM, "lost") is None
    await device.disconnect()


async def test_an_admin_posts_in_the_rooms_own_name(tmp_path: Path) -> None:
    """``room.post`` adds a notice authored by the room itself."""
    device = MockDevice()
    assert await device.admin_login(ROOM, "admin") is LoginResult.ACCEPTED
    assert await device.send_remote_command(ROOM, "room.post Meeting at 7") == "OK"
    assert device._room_board[-1][0] == "f6a7b8c9"
    assert await device.send_remote_command(ROOM, "get guest.password") == "> hello"
    await device.disconnect()


# --- history and the recorder ---------------------------------------------------------


def test_a_rooms_board_leaves_out_its_command_replies(repo: Repository) -> None:
    """Posts and our own posts are the board; a reply under the room's key is not."""
    peer = ROOM.key_prefix
    repo.record_chat_message(ChatMessage.from_message(_post("first", minutes=1)))
    repo.record_chat_message(ChatMessage(text="OK", peer=peer))  # an admin command's reply
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
    """The same room, author, time and text name one post."""
    post = ChatMessage.from_message(_post("swap meet?"))
    assert not repo.has_room_post(post)
    repo.record_chat_message(post)
    assert repo.has_room_post(ChatMessage.from_message(_post("swap meet?")))
    assert not repo.has_room_post(ChatMessage.from_message(_post("swap meet?", minutes=1)))
    assert not repo.has_room_post(ChatMessage.from_message(_post("swap meet?", STRANGER_KEY)))


class _GatedCtx(_Ctx):
    """With the device-state cache, so the badge rule can place a sender."""

    def __init__(self, tmp_path: Path, device: MockDevice, repo: Repository) -> None:
        super().__init__(tmp_path, device, repo)
        from meshterm.services.device_state import DeviceState

        self.devstate = DeviceState(self)


async def _record(chat: ChatService, ctx, message: Message) -> None:  # noqa: ANN001
    ctx.events.publish(MeshEvent.message_event(message))
    await chat._queue.join()


async def test_a_post_is_recorded_once_and_raises_the_badge_once(
    tmp_path: Path, repo: Repository
) -> None:
    """A re-sent post lands twice on the wire and once in history."""
    ctx = _GatedCtx(tmp_path, MockDevice(), repo)
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
    """The board is listed; what else a room sends has no row to point the badge at."""
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
    """Acknowledged means the room kept it; a read-only member's post is not."""
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
    """A retry is a new post, which every member would receive twice."""
    device = MockDevice()
    ctx = _Ctx(tmp_path, device, repo)
    ctx.preferences.direct_message_soft_retries = 2
    sends: list[str] = []
    original = device.send_direct_message

    async def counting(contact, text):  # noqa: ANN001, ANN202
        sends.append(text)
        return await original(contact, text)

    device.send_direct_message = counting
    await ChatService(ctx).send_post(ROOM, "unacked")  # not a member: no ack
    assert sends == ["unacked"]


# --- the repeater-admin page in a room server's words --------------------------------


def test_a_room_servers_guest_password_is_its_room_password() -> None:
    """Same key on both firmwares; a room's page calls it what it is there."""
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
    """Alice's key names her and the room's names the room; nothing names the stranger."""
    return {ALICE_KEY: "Alice", "f6a7b8c9": ROOM.name}.get(hash_ or "", hash_)


def _view(
    messages: list[ChatMessage] | None = None,
    *,
    access: RoomAccess | None = RoomAccess.MEMBER,
    password: str | None = "hello",
    ask_answer: str | None = "hello",
    login: RoomLogin | None = None,
) -> tuple[RoomScreen, dict]:
    """A room view over stubbed send/login hooks; ``calls`` records what they were asked."""
    calls: dict = {"asked": 0, "logins": [], "sent": []}

    async def ask() -> str | None:
        calls["asked"] += 1
        return ask_answer

    async def log_in(pw: str) -> RoomLogin:
        calls["logins"].append(pw)
        await asyncio.sleep(0)  # an exchange on the air: the board goes on drawing meanwhile
        # Not ``login or …``: a login that failed is falsy, which is the point of it.
        return login if login is not None else RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER)

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
        password=lambda: password,
        ask=ask,
        log_in=log_in,
        access=access,
    )
    return screen, calls


def _board() -> list[ChatMessage]:
    return [
        ChatMessage.from_message(_post("Anyone driving Saturday?", minutes=-30)),
        ChatMessage.from_message(_post("Is the north repeater down?", STRANGER_KEY, minutes=-20)),
        ChatMessage.from_message(_post("Board keeps 32 posts.", "f6a7b8c9", minutes=-10)),
        ChatMessage(text="I'll check it", outbound=True, peer=ROOM.key_prefix, acked=True),
    ]


def test_an_author_is_named_by_key_or_stands_as_a_grey_hash() -> None:
    """A key something names is that name, in its hue; one nothing names stays a hash."""
    assert author_label(ALICE_KEY, _resolve) == ("Alice", ALICE_KEY)
    assert author_label(STRANGER_KEY, _resolve) == (STRANGER_KEY, None)


def test_the_board_draws_each_post_under_its_author() -> None:
    """A contact, a stranger, the room's own notice, and us — each under its own chip."""
    screen, _ = _view(_board())
    text = plain("\n".join(screen.render_body(72)))
    for expected in ("Alice", STRANGER_KEY, ROOM.name, "Anyone driving Saturday?", "I'll check it"):
        assert expected in text
    assert screen.title == f"{ROOM.name} · member"


def test_a_post_that_arrives_twice_is_shown_once_and_a_reply_not_at_all() -> None:
    """The live view applies the same two rules the recorder does."""
    screen, _ = _view(_board())
    screen.append(ChatMessage.from_message(_post("Board keeps 32 posts.", "f6a7b8c9", minutes=-10)))
    screen.append(ChatMessage(text="> hello", peer=ROOM.key_prefix))  # a command reply
    assert len(screen._messages) == 4
    screen.append(ChatMessage.from_message(_post("New one", minutes=1)))
    assert screen._messages[-1].text == "New one"


async def test_a_read_only_member_gets_a_line_instead_of_a_compose_box() -> None:
    """Typing that could go nowhere is not taken, and the line says why."""
    screen, calls = _view(access=RoomAccess.READ_ONLY)
    for ch in "hi":
        screen.handle("text", ch)
    screen.handle("enter")
    await asyncio.sleep(0)
    assert calls["sent"] == []
    body = plain("\n".join(screen.render_body(72)))
    assert "read-only" in body
    assert "Enter send" not in screen.footer_hint
    assert screen.title.endswith("· read-only")


async def test_a_member_posts_from_the_compose_line() -> None:
    """Enter posts, through the same path a direct message takes."""
    screen, calls = _view()
    for ch in "hi":
        screen.handle("text", ch)
    screen.handle("enter")
    while screen._sending:
        await asyncio.sleep(0)
    assert calls["sent"] == ["hi"]


async def test_enter_on_a_picked_post_replies_to_its_author() -> None:
    """A board has many voices, so Enter on a post primes an @mention, as a channel does."""
    screen, _ = _view(_board())
    for _ in range(3):  # ours, the room's notice, then the stranger's post
        screen.handle("up")
    screen.handle("enter")
    assert screen._editor.text == f"@[{STRANGER_KEY}] "


async def test_control_l_logs_in_with_the_remembered_password() -> None:
    """^L logs in without asking, and the title says so while it does."""
    screen, calls = _view(access=None)
    screen.handle("login")
    assert screen._login_open
    assert "^L log in" not in screen.footer_hint  # a second press would do nothing
    titles = []
    while screen._login_open:
        titles.append(screen.title)
        await asyncio.sleep(0)
    assert f"{ROOM.name} · {LOGGING_IN}" in titles
    assert calls == {"asked": 0, "logins": ["hello"], "sent": []}
    assert screen.title == f"{ROOM.name} · member"


async def test_after_silence_control_l_asks_for_the_password() -> None:
    """A room is as silent for a wrong password as for a missing room — so ask next time."""
    screen, calls = _view(login=RoomLogin(LoginResult.NO_REPLY))
    screen.begin_login()
    while screen._login_open:
        await asyncio.sleep(0)
    assert screen.title.endswith(NO_REPLY)
    assert "^L" in screen._status
    screen.handle("login")
    while screen._login_open:
        await asyncio.sleep(0)
    assert calls["asked"] == 1


async def test_declining_to_join_leaves_the_board_open_and_says_so() -> None:
    """Esc on the join prompt keeps the board to read, titled for what it is."""
    screen, calls = _view(access=None, password=None, ask_answer=None)
    screen.begin_login(ask=True)
    while screen._login_open:
        await asyncio.sleep(0)
    assert calls["logins"] == []
    assert screen.title.endswith(NOT_JOINED)


def test_the_hint_names_log_in_and_fits() -> None:
    """^L is named where it acts, inside 72 cells, picked or not."""
    screen, _ = _view(_board())
    assert "^L log in" in screen.footer_hint
    assert cell_len(screen.footer_hint) <= 72
    screen.handle("up")
    assert cell_len(screen.footer_hint) <= 72


def test_the_handheld_lane_puts_log_in_behind_f1() -> None:
    """Every chord earns a chip where the keyboard has no Ctrl to reach it by."""
    screen, _ = _view(_board())
    deck = fkeys.PICOCALC_LYRA_DECK
    lane = screen.picocalc_lyra_lane
    assert lane[0].opp_label == "Log in"
    assert deck.action_for(lane, 6) == "login"


# --- end to end, through the real session ---------------------------------------------


async def test_joining_a_room_end_to_end(tmp_path: Path) -> None:
    """Open an unjoined room, answer the join prompt, read the catch-up, post, leave."""
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from rich.console import Console

    from meshterm.context import AppContext
    from meshterm.core.device_store import DeviceStore
    from meshterm.ui.chat import open_chat
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
    conv = Conversation(label=ROOM.name, is_channel=False, contact=ROOM)

    def board() -> list[ChatMessage]:
        return ctx.repo.recent_chat_messages(
            is_channel=False, peer=ROOM.key_prefix, posts_only=True
        )

    try:
        with create_pipe_input() as inp:
            session = TuiSession(input=inp, output=DummyOutput())
            ctx.ui = TuiUi(session)

            async def drive() -> None:
                while not session._stack or len(session._stack) < 2:
                    await asyncio.sleep(0.01)  # the board, and the join prompt over it
                inp.send_text("hello\r")
                while len([m for m in board() if m.is_post]) < 5:
                    await asyncio.sleep(0.01)
                inp.send_text("count me in\r")
                while not [m for m in board() if m.outbound]:
                    await asyncio.sleep(0.01)
                inp.send_text("\x1b")

            async def main() -> None:
                driver = asyncio.ensure_future(drive())
                try:
                    await open_chat(ctx, conv)
                finally:
                    driver.cancel()

            await asyncio.wait_for(session.run(main()), timeout=10)

        posts = board()
        assert len([m for m in posts if m.is_post]) == 5
        mine = [m for m in posts if m.outbound]
        assert [(m.text, m.acked) for m in mine] == [("count me in", True)]
        assert ctx.room_store.password(ROOM) == "hello"
        assert ctx.rooms.access(ROOM) is RoomAccess.MEMBER
    finally:
        await ctx.aclose()
