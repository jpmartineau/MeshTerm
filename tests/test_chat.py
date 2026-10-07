# SPDX-License-Identifier: Apache-2.0
"""Tests for the chat feature: device send, persistence, and the chat service.

All the tests run against the :class:`MockDevice` simulator and a temporary database. They
do not use hardware.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import pytest
from rich.cells import cell_len

from meshterm.core.channels import DEFAULT_PUBLIC_SECRET, derive_secret
from meshterm.core.config import Settings
from meshterm.core.connection import MockDevice
from meshterm.core.events import MeshEvent
from meshterm.core.models import (
    ChatMessage,
    Contact,
    Conversation,
    Message,
    conversation_key,
)
from meshterm.core.preferences import Preferences
from meshterm.persistence.repository import Repository
from meshterm.services.chat_service import ChatService
from meshterm.services.event_hub import EventHub
from meshterm.tools.chat import _lanes, _LiveLasts, _preview_text, _title
from meshterm.ui.chat import ChatScreen
from meshterm.ui.tui import fkeys
from meshterm.ui.tui.screen import CANCEL
from tests.conftest import plain as _strip_ansi  # the only strip-and-join screen reader


class _StubSession:
    """A session stand-in that counts the requests for a paint."""

    def __init__(self) -> None:
        self.invalidations = 0

    def invalidate(self) -> None:
        self.invalidations += 1

    def run_detached(self, work):  # noqa: ANN001, ANN201
        """Start the flow of a key handler as a task, as ``TuiSession.run_detached`` does."""
        return asyncio.ensure_future(work)


class _StubContext:
    """Minimal :class:`~meshterm.context.AppContext` stand-in for chat tests."""

    def __init__(self, device: MockDevice, repo: Repository) -> None:
        self._device = device
        self.repo = repo
        self.log = logging.getLogger("test.chat")
        self.profile_name = None
        self.settings = Settings()
        self.preferences = Preferences()
        self.events = EventHub(self)

    async def device(self) -> MockDevice:
        await self._device.connect()
        return self._device


@pytest.fixture()
def repo(tmp_path: Path) -> Repository:
    """A Repository on a scratch database. It closes when the test ends."""
    r = Repository(tmp_path / "chat.db")
    yield r
    r.close()


# -- domain models ------------------------------------------------------------


def test_conversation_key_distinguishes_channels_and_directs() -> None:
    """A channel key uses the identity of the channel. A direct key ignores the case of the peer."""
    assert conversation_key(True, "deadbeef", None) == "chan:deadbeef"
    assert conversation_key(False, None, "AABBCC") == "dm:aabbcc"

    contact = Contact(name="Alice", public_key="d4e5" + "0" * 60, key_prefix="d4e5f6a7")
    conv = Conversation(label="Alice", is_channel=False, contact=contact)
    assert conv.key == "dm:d4e5f6a7"
    assert conv.peer == "d4e5f6a7"

    chan = Conversation(label="#public", is_channel=True, channel_idx=2, channel_id="deadbeef")
    assert chan.key == "chan:deadbeef"  # keyed by identity, not by the slot index
    assert chan.peer is None


def test_chat_message_from_inbound_message() -> None:
    """An inbound direct Message becomes a received ChatMessage on the correct thread."""
    message = Message(text="hi", sender="a1b2c3d4", is_channel=False, snr=5.5)
    chat = ChatMessage.from_message(message, peer_name="Yagi")
    assert chat.outbound is False
    assert chat.is_channel is False
    assert chat.peer == "a1b2c3d4"
    assert chat.peer_name == "Yagi"
    assert chat.snr == 5.5
    assert chat.key == "dm:a1b2c3d4"


# -- device send --------------------------------------------------------------


async def test_mock_send_direct_returns_ack() -> None:
    """The simulator acknowledges direct messages, so they show as delivered."""
    device = MockDevice()
    await device.connect()
    contact = (await device.get_contacts())[0]
    delivery = await device.send_direct_message(contact, "hello")
    assert delivery.acked and delivery.code
    await device.send_channel_message(0, "hi channel")  # no return value, and it must not raise
    await device.disconnect()


async def test_sending_to_a_contact_the_device_forgot_is_refused_by_name() -> None:
    """A recipient that the radio has no entry for is refused in words that name the contact.

    MeshTerm lists the contacts of the device and the contacts that it remembers for that
    device. Thus a contact that the firmware removed still shows in the list, and only the
    send finds the problem. The rejection must carry the contact, so that the chat can offer
    to write it back.
    """
    from meshterm.core.connection import ContactNotOnDeviceError

    device = MockDevice()
    await device.connect()
    stranger = Contact(name="bob_caribou", public_key="a3" * 32, key_prefix="a3a3a3a3")
    with pytest.raises(ContactNotOnDeviceError) as caught:
        await device.send_direct_message(stranger, "hello")
    assert caught.value.contact is stranger
    assert "bob_caribou" in str(caught.value)
    assert "error_code" not in str(caught.value)  # not the payload from the wire
    await device.disconnect()


async def test_adding_a_forgotten_contact_back_makes_it_messageable() -> None:
    """To write the contact to the device is the whole repair. Then the same send succeeds."""
    device = MockDevice()
    await device.connect()
    stranger = Contact(name="bob_caribou", public_key="a3" * 32, key_prefix="a3a3a3a3")
    await device.add_contact(stranger)
    assert "bob_caribou" in {c.name for c in await device.get_contacts()}
    assert (await device.send_direct_message(stranger, "hello")).acked
    await device.disconnect()


async def test_restore_contact_writes_only_when_the_dialog_is_accepted() -> None:
    """The device write is offered, not silent. If the user declines, the table does not change."""
    from meshterm.core.connection import ContactNotOnDeviceError
    from meshterm.ui.chat import _restore_contact

    class _Ui:
        def __init__(self, answer: bool) -> None:
            self.answer = answer
            self.prompt = ""

        async def dialog(self, prompt, buttons, **kwargs):  # noqa: ANN001, ANN003
            self.prompt = prompt
            return self.answer

    class _Devstate:
        def __init__(self) -> None:
            self.invalidated = 0

        def invalidate_contacts(self) -> None:
            self.invalidated += 1

    class _Ctx:
        def __init__(self, device: MockDevice, answer: bool) -> None:
            self._device = device
            self.ui = _Ui(answer)
            self.devstate = _Devstate()

        async def device(self) -> MockDevice:
            return self._device

    stranger = Contact(name="bob_caribou", public_key="a3" * 32, key_prefix="a3a3a3a3")
    missing = ContactNotOnDeviceError(stranger)

    device = MockDevice()
    await device.connect()
    before = len(await device.get_contacts())

    declined = _Ctx(device, answer=False)
    assert await _restore_contact(declined, missing) is False
    assert len(await device.get_contacts()) == before
    assert declined.devstate.invalidated == 0
    assert "bob_caribou" in declined.ui.prompt

    accepted = _Ctx(device, answer=True)
    assert await _restore_contact(accepted, missing) is True
    assert len(await device.get_contacts()) == before + 1
    assert accepted.devstate.invalidated == 1
    await device.disconnect()


async def test_a_declined_restore_lets_the_rejection_stand() -> None:
    """The send tries again if the contact is back. If not, the send reports the refusal."""
    from meshterm.core.connection import ContactNotOnDeviceError
    from meshterm.ui.chat import _with_restore

    stranger = Contact(name="bob_caribou", public_key="a3" * 32)
    tries = 0

    async def attempt() -> str:
        nonlocal tries
        tries += 1
        if tries == 1:
            raise ContactNotOnDeviceError(stranger)
        return "sent"

    class _Ctx:
        def __init__(self, restored: bool) -> None:
            self.restored = restored

    async def restore(ctx, missing) -> bool:  # noqa: ANN001
        return ctx.restored

    import meshterm.ui.chat as chat_module

    original = chat_module._restore_contact
    chat_module._restore_contact = restore
    try:
        with pytest.raises(ContactNotOnDeviceError):
            await _with_restore(_Ctx(restored=False), attempt)
        assert tries == 1  # declined: no second transmission
        tries = 0
        assert await _with_restore(_Ctx(restored=True), attempt) == "sent"
        assert tries == 2  # the same send runs again after the contact is written back
    finally:
        chat_module._restore_contact = original


def test_a_rejected_command_is_explained_in_words_not_wire_codes() -> None:
    """An error code becomes a sentence. Only an unknown rejection shows its payload."""
    from meshterm.core.connection import reject_reason

    class _Ev:
        def __init__(self, payload: dict) -> None:
            self.payload = payload

    assert reject_reason(_Ev({"error_code": 2})) == "the device has no contact with that key"
    assert reject_reason(_Ev({"error_code": 3})) == "the device's table is full"
    assert reject_reason(None) == "the device didn't answer"
    assert "97" in reject_reason(_Ev({"error_code": 97}))


async def test_add_contact_writes_a_flood_record_with_the_name_the_field_fits() -> None:
    """The record for the firmware has no learned route and a name that is cut to fit in bytes."""
    from meshterm.core.connection import MeshCoreDevice

    class _Ok:
        payload: dict = {}

        def is_error(self) -> bool:
            return False

    class _FakeContacts:
        def __init__(self) -> None:
            self.record = None

        async def add_contact(self, record):  # noqa: ANN001
            self.record = record
            return _Ok()

    class _FakeMc:
        def __init__(self) -> None:
            self.commands = _FakeContacts()

    device = MeshCoreDevice.__new__(MeshCoreDevice)
    mc = _FakeMc()
    device._require = lambda: mc  # type: ignore[method-assign]
    long_name = "é" * 40  # 80 bytes: twice the size of the name field of the firmware
    await device.add_contact(Contact(name=long_name, public_key="a3" * 32, node_type=1))
    record = mc.commands.record
    assert record["out_path_len"] == -1 and record["out_path"] == ""
    assert len(record["adv_name"].encode("utf-8")) == 32
    assert record["public_key"] == "a3" * 32


async def test_message_pump_drains_until_empty() -> None:
    """The RX pump calls get_msg() until the queue is empty (the pull model).

    MeshCore never pushes message bodies, so the client must pull them. Without this, a
    send works but the client receives nothing. The test guards these parts: the immediate
    drain, the loop until NO_MORE_MSGS, the MESSAGES_WAITING subscription, and the clean
    teardown.
    """
    from meshcore import EventType

    from meshterm.core.connection import MeshCoreDevice

    class _Ev:
        def __init__(self, t) -> None:  # noqa: ANN001
            self.type = t

    class _FakeCommands:
        def __init__(self, script: list) -> None:
            self.calls = 0
            self._script = script

        async def get_msg(self, timeout=None):  # noqa: ANN001, ANN201
            i = min(self.calls, len(self._script) - 1)
            self.calls += 1
            return _Ev(self._script[i])

    class _FakeMC:
        def __init__(self, script: list) -> None:
            self.commands = _FakeCommands(script)
            self.subs: list = []

        def subscribe(self, etype, cb):  # noqa: ANN001, ANN201
            self.subs.append(etype)
            return object()

    # One real message, then the empty sentinel: the drain loop pulls two times.
    mc = _FakeMC([EventType.CONTACT_MSG_RECV, EventType.NO_MORE_MSGS])
    device = MeshCoreDevice(port="COM_TEST")
    subs: list = []
    stop = device._message_pump(mc, subs, mc.subscribe)
    try:
        await asyncio.sleep(0.01)  # let the task for the immediate drain run
        assert mc.commands.calls == 2  # CONTACT_MSG_RECV then NO_MORE_MSGS
        assert EventType.MESSAGES_WAITING in mc.subs  # the subscription for a push with low latency
    finally:
        stop()


# -- persistence --------------------------------------------------------------


def test_record_and_load_direct_conversation(repo: Repository) -> None:
    """Direct messages make a round trip and return in time order."""
    repo.record_chat_message(ChatMessage(text="hi", outbound=True, peer="D4E5F6A7", acked=True))
    repo.record_chat_message(ChatMessage(text="yo", outbound=False, peer="d4e5f6a7", snr=3.0))

    got = repo.recent_chat_messages(is_channel=False, peer="d4e5f6a7")
    assert [m.text for m in got] == ["hi", "yo"]
    assert got[0].outbound is True and got[0].acked is True
    assert got[1].outbound is False and got[1].snr == 3.0


def test_record_and_load_channel_conversation(repo: Repository) -> None:
    """Channel messages have the identity of the channel as key, apart from direct messages."""
    repo.record_chat_message(ChatMessage(text="c0", is_channel=True, channel_id="aa00"))
    repo.record_chat_message(ChatMessage(text="c1", is_channel=True, channel_id="bb11"))

    assert [m.text for m in repo.recent_chat_messages(is_channel=True, channel_id="aa00")] == ["c0"]
    assert [m.text for m in repo.recent_chat_messages(is_channel=True, channel_id="bb11")] == ["c1"]


def test_last_chat_messages_returns_latest_per_conversation(repo: Repository) -> None:
    """The preview in the picker shows the newest message of each conversation."""
    repo.record_chat_message(ChatMessage(text="old", peer="aa"))
    repo.record_chat_message(ChatMessage(text="new", peer="aa"))
    repo.record_chat_message(ChatMessage(text="chan", is_channel=True, channel_id="aa00"))

    lasts = repo.last_chat_messages()
    assert lasts["dm:aa"].text == "new"
    assert lasts["chan:aa00"].text == "chan"


# -- picker row rendering -----------------------------------------------------


class _FakeChat:
    """Stand-in for the chat service. It has only the unread lookup that a row title reads."""

    def __init__(self, unread: dict[str, int]) -> None:
        self._unread = unread

    def unread(self, key: str) -> int:
        return self._unread.get(key, 0)


class _RowCtx:
    """Minimal ctx. It has only what the picker-row helpers use (repo and chat)."""

    def __init__(self, repo: Repository, unread: dict[str, int] | None = None) -> None:
        self.repo = repo
        self.chat = _FakeChat(unread or {})


def test_live_lasts_refreshes_preview_after_ttl(repo: Repository) -> None:
    """A new message is visible through _LiveLasts after the cache TTL ends."""
    repo.record_chat_message(ChatMessage(text="first", is_channel=True, channel_id="c0"))
    live = _LiveLasts(_RowCtx(repo), seed=repo.last_chat_messages(), ttl=0)  # 0: always fresh
    assert live.get("chan:c0").text == "first"
    repo.record_chat_message(ChatMessage(text="second", is_channel=True, channel_id="c0"))
    assert live.get("chan:c0").text == "second"  # the live value, not the seed


def test_live_lasts_serves_seed_within_ttl(repo: Repository) -> None:
    """Within the TTL, the seeded snapshot is used. The repository is not queried again."""
    live = _LiveLasts(_RowCtx(repo), seed={"chan:c0": ChatMessage(text="seed")}, ttl=999)
    repo.record_chat_message(ChatMessage(text="later", is_channel=True, channel_id="c0"))
    assert live.get("chan:c0").text == "seed"


#: A resolver that places no name. Each hue falls back to muted.
_NO_KEYS = lambda name: None  # noqa: E731 - a one-line stub is easier to read inline


def _keys_of(mapping: dict[str, str]):
    """A fixed name→key resolver for ``mapping`` (case-folded, like the real resolver)."""
    return lambda name: mapping.get(name.casefold())


def test_preview_prefixes_own_messages_only() -> None:
    """Only an outbound message gets a ``you:`` prefix. Inbound text shows exactly as it is.

    The firmware puts the sender of a channel message in the text of the message. Thus the
    preview does not make an author, because the author is already in the text. The author
    of a direct message is the label of the row.
    """
    chan_in = ChatMessage(text="Bob: hi", is_channel=True, channel_idx=0)  # sender in text
    assert _preview_text(chan_in, _NO_KEYS).plain == "Bob: hi"
    dm_in = ChatMessage(text="hey", peer="aa", peer_name="Bob")
    assert _preview_text(dm_in, _NO_KEYS).plain == "hey"
    mine = ChatMessage(text="yo", outbound=True, is_channel=True, channel_idx=0)
    assert _preview_text(mine, _NO_KEYS).plain == "you: yo"


def test_preview_colours_channel_sender_and_mentions() -> None:
    """A preview lights the names of senders and mentions that resolve, in the hue of the key.

    A name that no known node has stays muted, because colour is only for keyed identities.
    """
    from meshterm.ui.theme import node_style

    key_of = _keys_of({"bob": "d4" + "0" * 62, "alice": "60" + "0" * 62})
    msg = ChatMessage(text="Bob: hi @[Alice] @[Zed]", is_channel=True, channel_idx=0)
    preview = _preview_text(msg, key_of)
    assert preview.plain == "Bob: hi @Alice @Zed"  # the brackets are removed for display
    styles = {span.style for span in preview.spans}
    assert node_style("d4") in styles and node_style("60") in styles
    # The @Zed that does not resolve takes the unknown-node grey, as each name with no place.
    zed = preview.plain.index("@Zed")
    assert any(s.style == "node.unknown" and s.start <= zed < s.end for s in preview.spans)


def test_preview_keeps_the_whole_message() -> None:
    """The preview is built whole. The cut of the row is the part that the ←→ scroll passes."""
    long = ChatMessage(text="x" * 300, is_channel=True, channel_idx=0)
    assert _preview_text(long, _NO_KEYS).plain == "x" * 300


def test_title_shows_badge_and_author_preview(repo: Repository) -> None:
    """A channel row shows its live unread badge and its preview with the author prefix."""
    conv = Conversation(label="General", is_channel=True, channel_idx=0, channel_id="c0")
    ctx = _RowCtx(repo, unread={"chan:c0": 3})
    last = ChatMessage(text="Bob: hi there", is_channel=True, channel_id="c0")  # sender in text
    title = _title(ctx, conv, {"chan:c0": last}, _NO_KEYS, _lanes([conv]))
    line = title.plain  # a Text, because there are unread messages
    assert line.startswith("🔒 General")  # a private channel starts with its openness glyph
    assert "● 3" in line
    assert "Bob: hi there" in line


def test_title_reddens_only_the_unread_dot(repo: Repository) -> None:
    """If there are unread messages, only the ``●`` glyph of the row is red. If not, no dot."""
    from rich.text import Text

    conv = Conversation(label="General", is_channel=True, channel_idx=0, channel_id="c0")
    unread = _title(_RowCtx(repo, unread={"chan:c0": 2}), conv, {}, _NO_KEYS, _lanes([conv]))
    assert isinstance(unread, Text)
    dot = unread.plain.index("●")
    reddened = [
        span for span in unread.spans if span.style == "err" and span.start <= dot < span.end
    ]
    assert reddened and all(span.end - span.start == 1 for span in reddened)  # only the glyph

    read = _title(_RowCtx(repo, unread={}), conv, {}, _NO_KEYS, _lanes([conv]))
    assert isinstance(read, Text)  # always a Text, so its spans can have the colour of the row
    assert "●" not in read.plain  # nothing unread, thus no badge dot
    assert not any(span.style == "err" for span in read.spans)


def test_title_preview_column_aligns_regardless_of_label_length(repo: Repository) -> None:
    """The preview starts at the same column when the label is short or long (and clipped)."""
    ctx = _RowCtx(repo)
    short = Conversation(label="A", is_channel=True, channel_idx=0, channel_id="c0")
    long = Conversation(
        label="A much longer channel name here", is_channel=True, channel_idx=1, channel_id="c1"
    )
    m0 = ChatMessage(text="X: hello", is_channel=True, channel_id="c0")
    m1 = ChatMessage(text="Y: hello", is_channel=True, channel_id="c1")
    lanes = _lanes([short, long])  # one measurement for the list, as the picker does it
    l0 = _title(ctx, short, {"chan:c0": m0}, _NO_KEYS, lanes).plain
    l1 = _title(ctx, long, {"chan:c1": m1}, _NO_KEYS, lanes).plain
    assert l0.index("X: hello") == l1.index("Y: hello")


def test_title_leads_with_openness_glyph(repo: Repository) -> None:
    """A channel row starts with an openness glyph: ＃ from the name, 🌐 public, 🔒 private."""
    ctx = _RowCtx(repo)

    def head(conv):
        return _title(ctx, conv, {}, _NO_KEYS, _lanes([conv])).plain.split(" ", 1)[0]

    named = Conversation(
        label="#general", is_channel=True, channel_id="c0", secret=derive_secret("#general")
    )
    public = Conversation(
        label="Public", is_channel=True, channel_id="c1", secret=DEFAULT_PUBLIC_SECRET
    )
    private = Conversation(label="Ops", is_channel=True, channel_id="c2", secret=bytes(range(16)))
    assert head(named) == "＃"
    assert head(public) == "🌐"
    assert head(private) == "🔒"


def test_title_contact_dot_reflects_conversation_history(repo: Repository) -> None:
    """The contact dot of the title shows if there is history with the contact.

    The dot is filled (●) after the user and the contact talked, and hollow (○) before. It
    is companion pink in both cases. The name has the hue of the key of the contact.
    """
    from meshterm.tools.chat import _COMPANION_DOT_STYLE
    from meshterm.ui.theme import node_style

    ctx = _RowCtx(repo)
    contact = Conversation(
        label="Alice", is_channel=False, contact=Contact(name="Alice", public_key="d4" + "0" * 62)
    )

    def dot_is_pink(row) -> bool:
        return any(
            span.style == _COMPANION_DOT_STYLE and span.start == 0 and span.end == 1
            for span in row.spans
        )

    # No history yet: a hollow ring in the standard companion pink.
    fresh = _title(ctx, contact, {}, _NO_KEYS, _lanes([contact]))
    assert fresh.plain.startswith("○") and dot_is_pink(fresh)
    # The name lane has the hue of the key of Alice.
    name_at = fresh.plain.index("Alice")
    assert any(s.style == node_style("d4") and s.start <= name_at < s.end for s in fresh.spans)

    # After the user and the contact exchange messages, the same pink dot is filled.
    last = ChatMessage(text="hi", peer=contact.peer)
    talked = _title(ctx, contact, {contact.key: last}, _NO_KEYS, _lanes([contact]))
    assert talked.plain.startswith("●") and dot_is_pink(talked)


# -- chat service -------------------------------------------------------------


async def test_service_records_inbound_and_tracks_unread(repo: Repository) -> None:
    """Inbound messages are stored and increase the unread count of the conversation."""
    device = MockDevice()
    ctx = _StubContext(device, repo)
    chat = ChatService(ctx)
    await chat.start()
    try:
        ctx.events.publish(
            MeshEvent.message_event(Message(text="ping", sender="ffeeddcc", is_channel=False))
        )
        await chat._queue.join()  # let the inbound worker resolve and store the message
        assert chat.unread("dm:ffeeddcc") == 1
        assert chat.unread_total() >= 1
        stored = repo.recent_chat_messages(is_channel=False, peer="ffeeddcc")
        assert [m.text for m in stored] == ["ping"]
    finally:
        await chat.stop()
        await device.disconnect()


async def test_service_active_conversation_suppresses_unread(repo: Repository) -> None:
    """The open conversation has no unread messages and gets no more while it is active."""
    device = MockDevice()
    ctx = _StubContext(device, repo)
    chat = ChatService(ctx)
    await chat.start()
    try:
        chat.set_active("dm:ffeeddcc")
        ctx.events.publish(
            MeshEvent.message_event(Message(text="ping", sender="ffeeddcc", is_channel=False))
        )
        await chat._queue.join()  # let the inbound worker resolve and store the message
        assert chat.unread("dm:ffeeddcc") == 0  # an active thread gets no unread messages
        assert repo.recent_chat_messages(is_channel=False, peer="ffeeddcc")  # still stored
    finally:
        await chat.stop()
        await device.disconnect()


async def test_service_files_channel_message_by_current_slot_occupant(repo: Repository) -> None:
    """A channel message is filed under the channel that is in its slot now.

    The wire gives only a slot index, which the firmware assigns from the current position of
    the channel. A change of the order of the slots must not send later messages to the wrong
    channel. (The test does this change out of band, and does not tell the service.) The
    resolution reads the slot again each time. Thus a message on slot 0 goes to the channel
    that is in slot 0 at that time, and never to an old cached identity.
    """
    from meshterm.core.channels import channel_identity

    device = MockDevice()
    ctx = _StubContext(device, repo)
    await device.connect()
    secret_a, secret_b = bytes(range(16)), bytes(range(16, 32))
    await device.set_channel(0, "Alpha", secret_a)
    await device.set_channel(1, "Bravo", secret_b)
    id_a = channel_identity("Alpha", secret_a)
    id_b = channel_identity("Bravo", secret_b)

    chat = ChatService(ctx)
    await chat.start()  # primes slot 0 to Alpha and slot 1 to Bravo
    try:
        ctx.events.publish(
            MeshEvent.message_event(Message(text="from alpha", is_channel=True, channel=0))
        )
        await chat._queue.join()

        # Swap the channels in slots 0 and 1 out of band. The service is not told.
        await device.set_channel(0, "Bravo", secret_b)
        await device.set_channel(1, "Alpha", secret_a)

        ctx.events.publish(
            MeshEvent.message_event(Message(text="from bravo", is_channel=True, channel=0))
        )
        await chat._queue.join()

        alpha = repo.recent_chat_messages(is_channel=True, channel_id=id_a)
        bravo = repo.recent_chat_messages(is_channel=True, channel_id=id_b)
        assert [m.text for m in alpha] == ["from alpha"]  # the new order has no effect on it
        assert [m.text for m in bravo] == ["from bravo"]  # not filed under the identity of Alpha
    finally:
        await chat.stop()
        await device.disconnect()


async def test_service_records_channel_messages_in_arrival_order(repo: Repository) -> None:
    """A burst of channel messages is stored in the order of arrival, also with async resolution.

    Each channel message resolves its identity with a device read. The serial inbound worker
    makes sure that the messages go into the transcript (ordered by insertion) in the order
    that the app received them.
    """
    from meshterm.core.channels import channel_identity

    device = MockDevice()
    ctx = _StubContext(device, repo)
    await device.connect()
    secret = bytes(range(16))
    await device.set_channel(0, "Alpha", secret)
    channel_id = channel_identity("Alpha", secret)

    chat = ChatService(ctx)
    await chat.start()
    try:
        for i in range(5):
            ctx.events.publish(
                MeshEvent.message_event(Message(text=f"m{i}", is_channel=True, channel=0))
            )
        await chat._queue.join()
        stored = repo.recent_chat_messages(is_channel=True, channel_id=channel_id)
        assert [m.text for m in stored] == ["m0", "m1", "m2", "m3", "m4"]
    finally:
        await chat.stop()
        await device.disconnect()


# -- chat screen --------------------------------------------------------------


def _screen(session: _StubSession, send, *, messages=None, resend=None) -> ChatScreen:
    """Build a ChatScreen for a direct conversation, with a stub session and a send hook."""
    conv = Conversation(
        label="Alice",
        is_channel=False,
        contact=Contact(name="Alice", public_key="d4" + "0" * 62, key_prefix="d4e5f6a7"),
    )
    return ChatScreen(
        conv,
        messages or [],
        send=send,
        names={"d4e5f6a7": "Alice"},
        session=session,
        resend=resend,
    )


def test_chat_screen_renders_transcript_and_input() -> None:
    """The body shows each message, and it always ends with the input line."""
    session = _StubSession()
    messages = [ChatMessage(text="hi there", outbound=True, peer="d4e5f6a7", acked=True)]
    screen = _screen(session, send=None, messages=messages)

    lines = screen.render_body(60)
    joined = "\n".join(lines)
    assert "hi there" in joined
    assert "›" in joined  # the input editor's prompt marker


class _PasteSession(_StubSession):
    """A stub session. Its paste confirm gives a scripted answer (Cancel or Paste)."""

    def __init__(self, answer: bool) -> None:
        super().__init__()
        self.answer = answer
        self.dialogs: list = []

    async def button_dialog(self, prompt, buttons, **kwargs):  # noqa: ANN001, ANN201
        self.dialogs.append((prompt, buttons, kwargs))
        return self.answer


async def _drain_paste(screen: ChatScreen) -> None:
    """Let the scheduled paste-confirm task run to its end."""
    for _ in range(100):
        await asyncio.sleep(0)
        if not screen._paste_open:
            return


async def test_chat_paste_confirms_amber_then_inserts() -> None:
    """Ctrl-V paste first asks in an amber Cancel/Paste dialog, then puts the text in compose."""
    session = _PasteSession(answer=True)
    screen = _screen(session, send=None)
    screen.handle("paste", "hello\nworld")
    await _drain_paste(screen)

    # The newline changed to a space, and the text went into the compose line…
    assert screen._editor.text == "hello world"
    prompt, buttons, kwargs = session.dialogs[0]
    assert "Paste 11 characters" in prompt.plain  # the length of the folded and stripped text
    assert kwargs.get("border_style") == "warn"  # the amber (danger) tier, "yellow"
    assert [label for label, _ in buttons] == ["Cancel", "Paste"]  # the safe way out is on the left


async def test_chat_paste_declined_leaves_compose_untouched() -> None:
    """If the user selects Cancel on the paste confirm, the screen inserts nothing."""
    session = _PasteSession(answer=False)
    screen = _screen(session, send=None)
    screen.handle("paste", "unwanted")
    await _drain_paste(screen)
    assert screen._editor.text == ""


async def test_chat_screen_enter_sends_and_appends() -> None:
    """Enter sends the line and appends the returned message to the transcript."""
    session = _StubSession()
    sent: list[str] = []

    async def send(text: str) -> ChatMessage:
        sent.append(text)
        return ChatMessage(text=text, outbound=True, peer="d4e5f6a7", acked=True)

    screen = _screen(session, send=send)
    for ch in "hello":
        screen.handle("text", ch)
    screen.handle("enter")
    await asyncio.sleep(0)  # let the scheduled send task run

    assert sent == ["hello"]
    assert screen._messages[-1].text == "hello"
    assert session.invalidations > 0


async def test_direct_send_spins_until_the_ack_resolves(monkeypatch) -> None:
    """A pending direct message spins while the send is in progress, then shows ✓."""
    from meshterm.ui.tui.spinner import Spinner

    monkeypatch.setattr("meshterm.ui.chat.spinner_interval", lambda: 0.005)
    session = _StubSession()
    release = asyncio.Event()

    async def send(text: str) -> ChatMessage:
        await release.wait()  # keep the send open, so that the glyph spins
        return ChatMessage(text=text, outbound=True, peer="d4e5f6a7", acked=True)

    screen = _screen(session, send=send)
    for ch in "hi":
        screen.handle("text", ch)
    screen.handle("enter")

    # While the send is open, the last glyph is an animation step of the spinner, and it advances.
    await asyncio.sleep(0.03)
    first = screen._spinner.frame
    joined = "\n".join(screen.render_body(60))
    assert first in Spinner.BRAILLE and first in joined and "⏳" not in joined
    await asyncio.sleep(0.03)
    assert screen._spinner.frame != first  # the animation runs

    # When the ack arrives, the spinner is gone and the message shows its delivered glyph.
    release.set()
    for _ in range(100):  # let _send_direct finish (ticker cancel and swap) before the assert
        await asyncio.sleep(0.005)
        if screen._messages[-1].acked is not None:
            break
    assert screen._messages[-1].acked is True
    assert "✓" in "\n".join(screen.render_body(60))


def test_byte_counter_shows_used_over_limit_and_colors_only_used() -> None:
    """The inline budget shows ``used/limit``. Only the used count has a colour (max is muted)."""
    from rich.text import Text

    screen = _screen(_StubSession(), send=None)
    for ch in "hello":
        screen.handle("text", ch)
    counter = screen._byte_counter(screen._byte_limit())
    assert isinstance(counter, Text)
    assert counter.plain.strip() == "5/150"  # 5 bytes used of the 150-byte cap of a direct message
    # The budget is at the right edge of the input line. Padding separates it from the
    # compose text, and it does not follow the cursor. It shares the row of the input, so a
    # full transcript cannot push it off the bottom of the viewport.

    body = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(screen.render_body(80)))
    input_row = next(line for line in body.splitlines() if "5/150" in line)
    assert re.search(r"› hello +5/150$", input_row.rstrip())  # padding, then the counter
    assert len(input_row.rstrip()) == 80  # the counter reaches the right edge of the width
    # Only the "5" has a colour. The "/150" tail stays muted (it does not change).
    used_at = counter.plain.index("5")
    slash_at = counter.plain.index("/")
    used_spans = [s for s in counter.spans if s.start <= used_at < s.end]
    tail_spans = [s for s in counter.spans if s.start <= slash_at < s.end]
    assert used_spans and used_spans[0].style == "ok"  # green: much room remains
    assert tail_spans and tail_spans[0].style == "muted"


def test_byte_counter_stays_bottom_right_when_the_compose_wraps() -> None:
    """The byte counter stays at the bottom right when the compose line wraps.

    It stays at the right edge of the last line. It does not follow the cursor down to the
    second row.
    """
    screen = _screen(_StubSession(), send=None)
    text = "this is a long compose line that certainly wraps onto several rows here ok"
    for ch in text:
        screen.handle("text", ch)
    counter = f"{len(text)}/150"
    body = re.sub(r"\x1b\[[0-9;]*m", "", "\n".join(screen.render_body(40)))
    counter_rows = [line for line in body.splitlines() if counter in line]
    assert len(counter_rows) == 1
    row = counter_rows[0]
    assert row.rstrip().endswith(counter)  # at the right edge, not in the middle of the line
    assert len(row.rstrip()) == 40  # reaches the right edge of the render width
    assert not row.lstrip().startswith("›")  # on a wrapped row, not on the first input row


def test_byte_style_escalates_as_budget_runs_out() -> None:
    """The colour of the used bytes goes from green to yellow, orange, and red as bytes run out."""
    from meshterm.ui.tui.prompt import _BYTES_ORANGE, _BYTES_YELLOW, byte_style

    assert byte_style(80) == "ok"  # much room remains: green
    assert byte_style(20) == _BYTES_YELLOW  # in the tight band: yellow
    assert byte_style(10) == _BYTES_ORANGE  # in the low band: orange
    assert byte_style(0) == "err"  # the limit is reached: red
    assert byte_style(-5) == "err"  # over the limit: still red


def test_channel_byte_limit_is_lower_than_direct() -> None:
    """A channel broadcast has a smaller byte budget than a direct message."""
    from meshterm.ui.tui.prompt import CHANNEL_BYTE_LIMIT, DM_BYTE_LIMIT

    direct = _screen(_StubSession(), send=None)
    channel = _channel_screen([])
    assert direct._byte_limit() == DM_BYTE_LIMIT == 150
    assert channel._byte_limit() == CHANNEL_BYTE_LIMIT == 130


def test_overflow_counts_multibyte_characters_by_byte() -> None:
    """An emoji (4 UTF-8 bytes) counts as 4 in the budget. The overflow mark is on a character."""
    screen = _channel_screen([])  # 130-byte limit
    # 32 emojis are 128 bytes (under the limit). The 33rd makes 132 (over) at that whole character.
    for _ in range(33):
        screen.handle("text", "😀")
    assert screen._used_bytes() == 33 * 4
    assert screen._overflow_at(screen._byte_limit()) == 32  # the 33rd emoji is the first over


async def test_over_limit_message_is_not_sent_and_buffer_is_kept() -> None:
    """Enter on a line over the budget reports the overage and sends nothing. The text stays."""
    session = _StubSession()
    sent: list[str] = []

    async def send(text: str) -> ChatMessage:
        sent.append(text)
        return ChatMessage(text=text, outbound=True, peer="d4e5f6a7", acked=True)

    screen = _screen(session, send=send)
    for ch in "x" * 151:  # one byte over the 150-byte cap of a direct message
        screen.handle("text", ch)
    screen.handle("enter")
    await asyncio.sleep(0)  # the screen must not schedule a task, but let the loop turn

    assert sent == []  # the send was refused
    assert screen._editor.text == "x" * 151  # the buffer is intact, so the user can trim it
    assert "Too long by 1 byte" in screen._status
    # If the user trims the text to the limit, the notice clears.
    screen.handle("backspace")
    assert screen._status == ""


def test_chat_screen_shows_delivery_glyphs() -> None:
    """An outbound direct message ends with a delivery mark: a spinner, then ✓ or ✗."""
    from meshterm.ui.tui.spinner import Spinner

    async def resend(message):  # noqa: ANN001, ANN202 - this test does not await it
        return message

    messages = [
        ChatMessage(text="delivered", outbound=True, peer="d4e5f6a7", acked=True),
        ChatMessage(text="dropped", outbound=True, peer="d4e5f6a7", acked=False),
        ChatMessage(text="inflight", outbound=True, peer="d4e5f6a7", acked=None),
    ]
    screen = _screen(_StubSession(), send=None, messages=messages, resend=resend)

    joined = "\n".join(screen.render_body(60))
    # A message that waits for its ack spins (a Braille animation step). A resolved message
    # shows the app-wide ✓ or ✗ status mark (not the ✅ or ❌ emoji, which are packet-class icons).
    assert "✓" in joined and "✗" in joined
    assert "✅" not in joined and "❌" not in joined
    assert Spinner.BRAILLE[0] in joined and "⏳" not in joined
    # For a failed message, the footer hint names the retry shortcut (in the app-wide
    # compact ^ notation for Ctrl chords).
    assert "^R" in screen.footer_hint


def test_wrapped_body_hangs_under_the_first_line() -> None:
    """A body that is too long for the width wraps with a hanging indent under its first line."""
    from datetime import datetime, timezone

    base = datetime(2026, 7, 5, 14, 24, tzinfo=timezone.utc)
    # Many short words make several wrap points at a narrow width.
    text = " ".join(["word"] * 20)
    messages = [ChatMessage(text=text, peer="d4e5f6a7", created_at=base)]
    screen = _screen(_StubSession(), send=None, messages=messages)
    body_lines = [_strip_ansi(ln) for ln in screen._body_lines(text, messages[0], 30)]

    assert len(body_lines) > 1  # the text wrapped
    stamp = base.astimezone().strftime("%H:%M")
    indent = body_lines[0].index(stamp) + len(stamp) + 2  # gutter: "  HH:MM  "
    first_word = body_lines[0].index("word")
    assert first_word == indent
    # The text of each continuation line starts at the same column as the body of the first line.
    for cont in body_lines[1:]:
        assert cont.startswith(" " * indent)
        assert cont[indent] != " "  # the text continues under the body, not under the gutter


def test_at_mention_renders_as_name_in_sender_hue() -> None:
    """An ``@[Name]`` token renders as a bare ``@Name`` in the hue of the key of the node.

    A mentioned name that no contact or stored advert has stays muted. This follows the
    app-wide rule that colour marks a keyed identity.
    """
    from datetime import datetime, timezone

    from meshterm.ui.theme import node_style

    base = datetime(2026, 7, 5, 14, 24, tzinfo=timezone.utc)
    conv = Conversation(label="#public", is_channel=True, channel_idx=0)
    message = ChatMessage(
        text="Bob: @[Alice] and @[Zed] around?",
        is_channel=True,
        channel_idx=0,
        created_at=base,
    )
    screen = ChatScreen(
        conv,
        [message],
        send=None,
        names={},
        session=_StubSession(),
        key_of=_keys_of({"alice": "60" + "0" * 62}),
    )
    _, body = screen._sender_and_body(message)
    text = screen._render_mentions(body, selected=False)

    assert "@Alice" in text.plain  # the token with brackets became a bare mention
    assert "@[Alice]" not in text.plain and "[Alice]" not in text.plain
    # The "@Alice" text has the hue of the key (the same hue that the header uses).
    hue = screen._sender_style("Alice")
    assert hue == node_style("60")  # the spectrum hue from the key of the sender
    at = text.plain.index("@Alice")
    hue_spans = [
        s for s in text.spans if s.style == hue and s.start <= at and at + len("@Alice") <= s.end
    ]
    assert hue_spans
    # The unknown @Zed gets the unknown-node grey, because it has no key and no hue.
    assert screen._sender_style("Zed") == "node.unknown"


def test_direct_chat_unknown_mention_stays_muted() -> None:
    """In a direct chat, an unresolved ``@mention`` stays muted, as it does in a channel.

    The sender label of a direct thread falls back to the key of the peer. Thus it keeps its
    hue when the display name does not resolve. But this fallback must not go to an
    ``@mention`` in the body, because a mention names any person, and an unknown person is
    grey. This is a regression guard against the fallback to the peer key in mentions.
    """
    from meshterm.ui.theme import node_style

    # Direct chat with the peer key d4…: the sender label can use this hue, but mentions must not.
    conv = Conversation(
        label="Alice",
        is_channel=False,
        contact=Contact(name="Alice", public_key="d4" + "0" * 62, key_prefix="d4e5f6a7"),
    )
    screen = ChatScreen(
        conv,
        [],
        send=None,
        names={"d4e5f6a7": "Alice"},
        session=_StubSession(),
        key_of=_keys_of({"alice": "60" + "0" * 62}),
    )

    # A mention that resolves still lights in the hue of its own key…
    assert screen._sender_style("Alice", mention=True) == node_style("60")
    # …but an unknown mention is grey. It does not get the hue (d4…) of the peer.
    assert screen._sender_style("Zed", mention=True) == "node.unknown"
    assert screen._sender_style("Zed", mention=True) != node_style("d4")
    # The sender label keeps the fallback to the peer key (the behaviour is the same).
    assert screen._sender_style("Zed") == node_style("d4")


def test_direct_transcript_groups_under_sender_headers() -> None:
    """A direct chat has the same grouped layout as a channel: one header for each sender run."""
    from datetime import datetime, timezone

    base = datetime(2026, 7, 5, 14, 24, tzinfo=timezone.utc)
    messages = [
        ChatMessage(text="hi", peer="d4e5f6a7", created_at=base),
        ChatMessage(text="you there?", peer="d4e5f6a7", created_at=base),
        ChatMessage(text="yes!", outbound=True, peer="d4e5f6a7", acked=True, created_at=base),
    ]
    screen = _screen(_StubSession(), send=None, messages=messages)
    rendered = _strip_ansi("\n".join(screen._render_grouped(80)))

    # The inbound sender resolves to the contact name (from the names map), not to the raw key.
    assert rendered.count("Alice") == 1  # the two inbound messages share one header
    assert "d4e5f6a7" not in rendered  # if a name is known, the screen does not show the key
    assert "you" in rendered  # our reply has its own header
    assert "hi" in rendered and "you there?" in rendered and "yes!" in rendered
    assert "✓" in rendered  # the outbound message keeps its delivery mark


async def test_chat_screen_retry_resends_failed_message() -> None:
    """Ctrl-R tries the newest unacknowledged message again and changes it in place."""
    session = _StubSession()
    failed = ChatMessage(text="oops", outbound=True, peer="d4e5f6a7", acked=False, row_id=7)

    async def resend(message: ChatMessage) -> ChatMessage:
        message.acked = True  # this time, the retry succeeds
        return message

    conv = Conversation(
        label="Alice",
        is_channel=False,
        contact=Contact(name="Alice", public_key="d4" + "0" * 62, key_prefix="d4e5f6a7"),
    )
    screen = ChatScreen(conv, [failed], send=None, names={}, session=session, resend=resend)

    screen.handle("retry")
    await asyncio.sleep(0)  # let the scheduled resend task run

    assert screen._messages[-1].acked is True  # the same object, now acknowledged
    assert "✓" in "\n".join(screen.render_body(60))


async def test_chat_fkey_lane_dims_retry_and_the_nav_slots() -> None:
    """The lane offers Retry only for a message to retry, and navigation only with a transcript."""

    async def resend(message: ChatMessage) -> ChatMessage:
        message.acked = True
        return message

    empty = _screen(_StubSession(), send=None, resend=resend)
    assert not any(pair.enabled for pair in empty.picocalc_lyra_lane if pair)  # nothing to move

    failed = ChatMessage(text="oops", outbound=True, peer="d4e5f6a7", acked=False)
    screen = _screen(_StubSession(), send=None, messages=[failed], resend=resend)
    assert all(pair.enabled for pair in screen.picocalc_lyra_lane[3:])  # the pager moves the pick
    assert screen.picocalc_lyra_lane[2].opp_enabled is True  # F8 Retry: the message has no ack

    screen.handle("retry")
    await asyncio.sleep(0)
    assert failed.acked is True
    assert screen.picocalc_lyra_lane[2].opp_enabled is False  # acknowledged: nothing to retry
    assert (
        screen.picocalc_lyra_lane[2].opp_label == "Retry"
    )  # dim, not removed: it is part of this screen


def test_chat_fkey_lane_speaks_the_transcript_and_drops_channel_retry() -> None:
    """A chip names what it does here, and a channel has no retry."""
    messages = [ChatMessage(text="hi", peer="d4e5f6a7")]
    direct = _screen(_StubSession(), send=None, messages=messages)

    # The labels are not "Bottom" and "Top". In a conversation, the Shift bank goes back to
    # the newest message and the compose line, or goes to the oldest message. The chips have
    # new labels in place, so each jump stays on the pager that goes in that direction.
    assert [pair.opp_label for pair in direct.picocalc_lyra_lane[3:]] == ["Latest", "Oldest"]
    assert [pair.opp_action for pair in direct.picocalc_lyra_lane[3:]] == ["ctrl_end", "ctrl_home"]
    assert [pair.label for pair in direct.picocalc_lyra_lane[3:]] == ["Page ↓", "Page ↑"]
    assert direct.picocalc_lyra_lane[2].opp_label == "Retry"

    channel = ChatScreen(
        Conversation(label="#general", is_channel=True, channel_id="1"),
        messages,
        send=None,
        names={},
        session=_StubSession(),
    )
    # A channel message has no acknowledgement, so a retry is not possible here. The slot is
    # empty (it is not dim), and its key resolves to nothing.
    assert channel.picocalc_lyra_lane[2].opp_label == ""
    assert fkeys.PICOCALC_LYRA_DECK.action_for(channel.picocalc_lyra_lane, 8) is None


def test_chat_screen_up_picks_and_ctrl_end_returns_to_compose() -> None:
    """↑ selects the newest message (and leaves the tail). ^End returns to compose."""
    messages = [
        ChatMessage(text="hi", peer="d4e5f6a7"),
        ChatMessage(text="reply", outbound=True, peer="d4e5f6a7", acked=True),
    ]
    screen = _screen(_StubSession(), send=None, messages=messages)
    assert screen._stick is True and screen._selected is None
    screen.handle("up")
    assert screen._stick is False
    assert screen._selected == len(screen._messages) - 1  # the newest message
    screen.handle("ctrl_end")
    assert screen._stick is True and screen._selected is None


def test_split_channel_sender_extracts_name_prefix() -> None:
    """A ``Name: message`` channel line splits into the sender and a body without the prefix."""
    from meshterm.core.channels import split_channel_sender

    assert split_channel_sender("Alice: hey there") == ("Alice", "hey there")
    assert split_channel_sender("Yagi Repeater: online") == ("Yagi Repeater", "online")
    # No possible prefix: the text does not change.
    assert split_channel_sender("just a message") == (None, "just a message")
    assert split_channel_sender("https://example.com") == (None, "https://example.com")
    assert split_channel_sender("14:30 standup") == (None, "14:30 standup")


def test_channel_transcript_groups_by_sender() -> None:
    """Channel messages in a row from the same sender share one header. The body has no prefix."""
    from datetime import datetime, timezone

    conv = Conversation(label="#public", is_channel=True, channel_idx=0)
    base = datetime(2026, 7, 5, 14, 24, tzinfo=timezone.utc)

    def at(minutes: int) -> datetime:
        return base.replace(minute=24 + minutes)

    messages = [
        ChatMessage(text="Alice: hi", is_channel=True, channel_idx=0, created_at=at(0)),
        ChatMessage(text="Alice: again", is_channel=True, channel_idx=0, created_at=at(6)),
        ChatMessage(text="Bob: yo", is_channel=True, channel_idx=0, created_at=at(8)),
        ChatMessage(
            text="hello all", outbound=True, is_channel=True, channel_idx=0, created_at=at(9)
        ),
    ]
    screen = ChatScreen(conv, messages, send=None, names={}, session=_StubSession())
    rendered = _strip_ansi("\n".join(screen._render_grouped(80)))

    assert rendered.count("Alice") == 1  # the two Alice messages share one header
    # The header of our group is the ★ that a route uses for our end, not our name.
    assert "Bob" in rendered and "★" in rendered
    assert "hi" in rendered and "again" in rendered  # the bodies are there, without the prefix
    assert "Alice: hi" not in rendered  # the raw name prefix moved into the header
    # Each message keeps its own timestamp on its line, also when grouped under one sender.
    stamps = [at(m).astimezone().strftime("%H:%M") for m in (0, 6, 8, 9)]
    for stamp in stamps:
        assert stamp in rendered
    assert stamps[0] != stamps[1]  # the grouped Alice messages show different times


def test_transcript_inscribes_the_sender_label_but_never_a_mention(powerline) -> None:
    """The name that opens a group is a chip. The same name in a body is prose."""
    from meshterm.ui.widgets import name_chip

    powerline(True)  # this test is about the chip, so ask for a terminal that draws one
    conv = Conversation(label="#public", is_channel=True, channel_idx=0)
    messages = [
        ChatMessage(text="Alice: @[Bob] you around?", is_channel=True, channel_idx=0),
    ]
    screen = ChatScreen(conv, messages, send=None, names={}, session=_StubSession())
    rendered = _strip_ansi("\n".join(screen._render_grouped(80)))

    # The test builds the chip through the widget, not as literal text. The end caps are the
    # caps of the path line, and the test asserts the framing, not the glyph.
    assert name_chip("Alice").plain in rendered  # the header, as a chip
    assert "@Bob" in rendered  # the mention, as the user typed it
    assert name_chip("Bob").plain not in rendered  # …and not framed in the middle of a sentence


def test_unidentified_sender_takes_the_node_grey_not_the_chrome_grey() -> None:
    """``·`` is a sender that has no place. It is content, so it is ``node.unknown``, not ``muted``.

    The two styles are one hex value apart on the desktop and a whole slot apart on the
    console (light grey and dark grey). On the console, a sender in the grey of the chrome
    does not look like a node.
    """
    conv = Conversation(label="#public", is_channel=True, channel_idx=0)
    screen = ChatScreen(conv, [], send=None, names={}, session=_StubSession())

    assert screen._sender_style("·") == "node.unknown"
    assert screen._sender_style("nobody-knows-me") == "node.unknown"  # no key and no hue


def _two_day_messages():
    """Two days of direct messages. They are long enough to overflow a small viewport."""
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 7, 5, 9, 0, tzinfo=timezone.utc)
    day1 = [
        ChatMessage(text=f"day1-{i}", peer="d4e5f6a7", created_at=base + timedelta(minutes=i))
        for i in range(4)
    ]
    day2 = [
        ChatMessage(
            text=f"day2-{i}", peer="d4e5f6a7", created_at=base + timedelta(days=1, minutes=i)
        )
        for i in range(4)
    ]
    return day1 + day2


def test_chat_sticky_block_pins_the_governing_day_divider() -> None:
    """The day divider above the top row pins there when it scrolls off (as in the picker).

    A day has only its divider, so each block is that one row. The heading of a select list
    can also have its description in the block.
    """
    screen = _screen(_StubSession(), send=None, messages=_two_day_messages())
    screen.render_body(60)
    (idx0, day0), (idx1, day1) = screen._sticky_headers
    assert len(day0) == 1 and len(day1) == 1  # one divider, and no preamble under it
    assert screen.sticky_block(0) == []  # the first divider is the top row
    assert screen.sticky_block(idx1 - 1) == day0  # still in day one, so its divider pins
    assert screen.sticky_block(idx1) == []  # the divider of day two is now the top row
    assert screen.sticky_block(idx1 + 1) == day1  # scrolled past it, so the divider of day two pins


def test_chat_frame_pins_a_day_divider_when_stuck_to_the_newest() -> None:
    """When the frame renders the tail, a day divider is in the pinned top row."""
    from meshterm.ui.tui import frame

    screen = _screen(_StubSession(), send=None, messages=_two_day_messages())
    lines = screen.render_body(60)  # stays at the newest message, so early days scroll off
    visible, above, _below = frame._visible_slice(screen, lines, 6)
    top = _strip_ansi(visible[0]).strip()
    assert top.startswith("──") and "Jul" in top  # a day divider is pinned to the top row
    assert above is True  # and the frame shows that there is more above the pin


def test_chat_top_of_transcript_clears_the_more_above_flag() -> None:
    """If the user selects the oldest message, the head of the transcript scrolls into the viewport.

    The selection of the first message anchors at line 0, so the frame goes to offset 0. Its
    day divider and sender chip are real rows again, not a pin over a hidden line. The clip
    arrow of the title bar is dim, because nothing is above.
    """
    from meshterm.ui.tui import frame

    screen = _screen(_StubSession(), send=None, messages=_two_day_messages())
    lines = screen.render_body(60)
    screen.handle("ctrl_home")  # go to the oldest message
    lines = screen.render_body(60)
    visible, above, below = frame._visible_slice(screen, lines, 6)

    assert screen.scroll == 0
    assert above is False and below is True
    assert _strip_ansi(visible[0]).strip().startswith("──")  # the head, drawn and not pinned


def test_chat_home_end_and_word_keys_move_the_compose_cursor() -> None:
    """In a chat, Home, End, and Ctrl+←/→ act on the compose line, not on the transcript scroll."""
    screen = _screen(_StubSession(), send=None, messages=_two_day_messages())
    for ch in "hello world":
        screen.handle("text", ch)
    assert screen._editor.cursor == 11
    screen.handle("home")
    assert screen._editor.cursor == 0  # the start of the line, not a scroll to the top
    screen.handle("end")
    assert screen._editor.cursor == 11
    screen.handle("ctrl_left")
    assert screen._editor.cursor == 6  # the start of "world"


async def test_chat_paths_key_needs_a_picked_message() -> None:
    """^P shows the paths of the selected message. It does nothing until a message is selected."""
    import asyncio

    shown: list[str] = []

    async def paths(message) -> None:  # noqa: ANN001 - ChatMessage
        shown.append(message.text)

    messages = [
        ChatMessage(text="first", peer="d4e5f6a7"),
        ChatMessage(text="second", peer="d4e5f6a7"),
    ]
    conv = Conversation(
        label="Alice",
        is_channel=False,
        contact=Contact(name="Alice", public_key="d4" + "0" * 62, key_prefix="d4e5f6a7"),
    )
    screen = ChatScreen(conv, messages, send=None, names={}, session=_StubSession(), paths=paths)
    # A path is the route of one message. If the user selects no message, there is nothing
    # to show. The key does nothing, and the footer and the F-key lane both show this. They
    # do not guess a message at the tail.
    screen.handle("paths")
    await asyncio.sleep(0)
    assert shown == []
    assert "^P" not in screen.footer_hint
    assert screen.picocalc_lyra_lane[2].enabled is False

    screen.handle("up")
    screen.handle("up")  # select the older message
    screen.handle("paths")
    await asyncio.sleep(0)
    assert shown == ["first"]
    # With a selected message, the screen offers paths. Here the key is Enter, because a
    # direct chat has no reply to prepare. (The hint of a channel keeps the ^P atom beside
    # its Enter reply.)
    assert "Enter paths" in screen.footer_hint
    assert screen.picocalc_lyra_lane[2].enabled is True


async def test_chat_direct_enter_on_a_pick_opens_paths() -> None:
    """In a direct chat, Enter on a selected message opens its paths (in a channel, it replies)."""
    import asyncio

    shown: list[str] = []

    async def paths(message) -> None:  # noqa: ANN001 - ChatMessage
        shown.append(message.text)

    messages = [ChatMessage(text="only", peer="d4e5f6a7")]
    conv = Conversation(
        label="Alice",
        is_channel=False,
        contact=Contact(name="Alice", public_key="d4" + "0" * 62, key_prefix="d4e5f6a7"),
    )
    screen = ChatScreen(conv, messages, send=None, names={}, session=_StubSession(), paths=paths)
    screen.handle("up")
    assert "Enter paths" in screen.footer_hint
    screen.handle("enter")
    await asyncio.sleep(0)
    assert shown == ["only"]


def test_chat_pick_walks_with_page_keys_and_ctrl_home() -> None:
    """PgUp moves the selection one screen back. ^Home moves it to the first message."""
    screen = _screen(_StubSession(), send=None, messages=_two_day_messages())
    lines = screen.render_body(60)
    screen.note_metrics(total=len(lines), viewport=5)
    screen.handle("pageup")
    assert screen._stick is False and screen._selected is not None
    screen.handle("ctrl_home")
    assert screen._selected == 0  # the first message
    screen.handle("ctrl_end")
    assert screen._stick is True and screen._selected is None


def test_chat_ctrl_page_picks_across_day_dividers() -> None:
    """Ctrl+PageDown and Ctrl+PageUp move the selection between days (in both types of chat)."""
    screen = _screen(_StubSession(), send=None, messages=_two_day_messages())
    starts = screen._day_start_indices()
    assert len(starts) == 2
    screen.handle("ctrl_home")
    assert screen._selected == 0
    screen.handle("ctrl_pagedown")
    assert screen._selected == starts[1]  # the first message of the second day
    screen.handle("ctrl_pageup")
    assert screen._selected == starts[0]  # back to the first day


def _two_day_channel_messages():
    """Channel messages from two local days (one sender), for the tests of section jumps."""
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 7, 5, 9, 0, tzinfo=timezone.utc)
    day1 = [
        ChatMessage(
            text=f"Alice: d1-{i}",
            is_channel=True,
            channel_idx=0,
            created_at=base + timedelta(minutes=i),
        )
        for i in range(3)
    ]
    day2 = [
        ChatMessage(
            text=f"Alice: d2-{i}",
            is_channel=True,
            channel_idx=0,
            created_at=base + timedelta(days=1, minutes=i),
        )
        for i in range(3)
    ]
    return day1 + day2


def test_chat_channel_ctrl_page_selects_across_days() -> None:
    """In a channel, Ctrl+PageDown and Ctrl+PageUp move the reply selection between days."""
    screen = _channel_screen(_two_day_channel_messages())
    starts = screen._day_start_indices()
    assert starts == [0, 3]
    screen.handle("ctrl_home")
    assert screen._selected == 0
    screen.handle("ctrl_pagedown")
    assert screen._selected == starts[1]  # the first message of the second day
    screen.handle("ctrl_pageup")
    assert screen._selected == starts[0]  # back to the first day


def test_chat_channel_home_moves_the_compose_cursor() -> None:
    """The Home key of a channel acts on the compose line. It does not move the selection."""
    screen = _channel_screen(_two_day_channel_messages())
    for ch in "reply":
        screen.handle("text", ch)
    assert screen._editor.cursor == 5
    screen.handle("home")
    assert screen._editor.cursor == 0
    assert screen._selected is None  # a change to the compose line clears the reply selection


def test_channel_self_style_keyed_on_concept_not_label() -> None:
    """Our white 'self' style depends on the message being outbound, not on the 'you' label.

    A remote sender with the name 'you' must not take the white style that is only for us.
    It reads as a normal sender (the hue of its key if the key resolves, and muted if not).
    """
    from meshterm.ui.theme import node_style

    screen = _channel_screen([])
    assert screen._sender_style("you", is_self=True) == "you"  # us: white
    assert screen._sender_style("you", is_self=False) == "node.unknown"  # no key: grey
    keyed = _channel_screen([], key_of=_keys_of({"you": "d4" + "0" * 62}))
    remote = keyed._sender_style("you", is_self=False)
    assert remote == node_style("d4")  # the spectrum hue of the key


def test_channel_own_messages_do_not_merge_with_remote_namesake() -> None:
    """A remote sender with the exact name 'you' is a separate group from our own messages."""
    from datetime import datetime, timezone

    base = datetime(2026, 7, 5, 14, 24, tzinfo=timezone.utc)
    messages = [
        ChatMessage(text="you: impostor", is_channel=True, channel_idx=0, created_at=base),
        ChatMessage(text="mine", outbound=True, is_channel=True, channel_idx=0, created_at=base),
    ]
    screen = _channel_screen(messages)
    rendered = _strip_ansi("\n".join(screen._render_grouped(80)))
    # Two headers, and they are not only apart: theirs is the name that they sent, ours is the ★.
    assert rendered.count("you") == 1 and rendered.count("★") == 1


def _channel_screen(messages, session=None, key_of=None) -> ChatScreen:
    """Build a channel ChatScreen for ``messages``, for the selection and reply tests."""
    conv = Conversation(label="#public", is_channel=True, channel_idx=0)
    return ChatScreen(
        conv,
        messages,
        send=None,
        names={},
        session=session or _StubSession(),
        key_of=key_of,
    )


def _channel_messages():
    """Three inbound channel messages (Alice, Alice, Bob) for the reply-selection tests."""
    from datetime import datetime, timezone

    base = datetime(2026, 7, 5, 14, 24, tzinfo=timezone.utc)
    return [
        ChatMessage(text="Alice: hi", is_channel=True, channel_idx=0, created_at=base),
        ChatMessage(text="Alice: still here", is_channel=True, channel_idx=0, created_at=base),
        ChatMessage(text="Bob: yo", is_channel=True, channel_idx=0, created_at=base),
    ]


def test_channel_up_enters_selection_from_newest() -> None:
    """No message is selected until ↑ selects the newest. Then ↑ moves toward older messages."""
    screen = _channel_screen(_channel_messages())
    assert screen._selected is None  # compose has the focus: nothing is selected at the start

    screen.handle("up")
    assert screen._selected == 2  # the newest message
    assert screen._stick is False
    screen.handle("up")
    assert screen._selected == 1  # moves to the previous message


def test_channel_down_past_newest_clears_selection() -> None:
    """↓ after the newest message returns the focus to compose (nothing is selected)."""
    screen = _channel_screen(_channel_messages())
    screen.handle("up")  # select the newest (index 2)
    assert screen._selected == 2

    screen.handle("down")  # after the newest: deselect, and stay at the tail again
    assert screen._selected is None
    assert screen._stick is True


def test_channel_selected_line_tracked_for_scroll() -> None:
    """The render records the body line of the selected message, so the frame keeps it visible."""
    screen = _channel_screen(_channel_messages())
    screen.handle("up")  # select Bob (the newest)
    screen.render_body(80)
    assert screen._selected_line is not None
    # No selection means no cursor line, so the transcript scrolls freely, as before.
    screen.handle("end")
    screen.render_body(80)
    assert screen._selected_line is None


def test_channel_enter_on_selection_primes_at_mention() -> None:
    """Enter on a selected message puts the @mention of the sender in the compose line."""
    screen = _channel_screen(_channel_messages())
    screen.handle("up")
    screen.handle("up")  # select an Alice message (index 1)
    screen.handle("enter")

    assert screen._editor.text == "@[Alice] "
    assert screen._selected is None  # the focus returns to compose after the reply starts


def test_channel_typing_clears_selection() -> None:
    """A change to the compose line clears the reply selection (compose has the focus)."""
    screen = _channel_screen(_channel_messages())
    screen.handle("up")
    assert screen._selected is not None
    screen.handle("text", "x")
    assert screen._selected is None
    assert screen._editor.text == "x"


def test_chat_screen_escape_cancels() -> None:
    """Esc resolves the future of the screen with CANCEL, so the caller pops it."""
    screen = _screen(_StubSession(), send=None)
    loop = asyncio.new_event_loop()
    try:
        screen.future = loop.create_future()
        screen.handle("escape")
        assert screen.future.result() is CANCEL
    finally:
        loop.close()


async def test_service_send_records_outbound(repo: Repository) -> None:
    """A send through the service stores the outbound message with its ack state."""
    device = MockDevice()
    ctx = _StubContext(device, repo)
    chat = ChatService(ctx)
    await chat.start()
    try:
        contact = next(c for c in await device.get_contacts() if c.name == "Alice")
        sent = await chat.send_direct(contact, "hey")
        assert sent.outbound is True and sent.acked is True

        await chat.send_channel(0, "hello all", label="#public")
        channel_id = await chat.channel_id_for(0)
        stored = repo.recent_chat_messages(is_channel=True, channel_id=channel_id)
        assert [m.text for m in stored] == ["hello all"]
        assert stored[0].outbound is True
    finally:
        await chat.stop()
        await device.disconnect()


async def test_service_resend_updates_ack_in_place(repo: Repository) -> None:
    """A resend of a failed message changes its stored ack. It does not add a duplicate row."""
    device = MockDevice()
    ctx = _StubContext(device, repo)
    chat = ChatService(ctx)
    await chat.start()
    try:
        contact = next(c for c in await device.get_contacts() if c.name == "Alice")
        peer = contact.key_prefix or contact.public_key[:12]
        failed = ChatMessage(
            text="retry me", outbound=True, peer=peer, peer_name=contact.name, acked=False
        )
        failed.row_id = repo.record_chat_message(failed)

        await chat.resend_direct(contact, failed)

        assert failed.acked is True  # the mock always acknowledges
        stored = repo.recent_chat_messages(is_channel=False, peer=peer)
        assert len(stored) == 1  # changed in place, not duplicated
        assert stored[0].acked is True
    finally:
        await chat.stop()
        await device.disconnect()


class _ScriptedDevice:
    """A device stub. The acks of its direct messages follow a fixed script.

    Each :meth:`send_direct_message` takes the next value from ``acks``. The value is an
    :class:`Ack` stand-in (any object that is not ``None`` counts as delivered), or ``None``
    for a transmission with no ack. The method also appends the sent text to :attr:`sent`,
    so that a test can assert how many soft retries happened. When the script has no more
    values, each further send has no ack. Each send gets its own ack code, as the radio does.
    """

    def __init__(self, acks: list) -> None:
        self._acks = list(acks)
        self.sent: list[str] = []

    async def connect(self) -> None:  # for _StubContext.device()
        return None

    async def send_direct_message(self, contact: Contact, text: str):
        from meshterm.core.models import Delivery

        self.sent.append(text)
        ack = self._acks.pop(0) if self._acks else None
        return Delivery(f"{len(self.sent):08x}", ack)


def _contact() -> Contact:
    return Contact(name="Alice", public_key="d4e5" + "0" * 60, key_prefix="d4e5f6a7")


async def test_send_direct_soft_retries_until_acked(repo: Repository) -> None:
    """A DM with no ack on the first tries is sent again. It is stored as delivered after an ack."""
    device = _ScriptedDevice([None, None, object()])  # an ack only on the third try
    ctx = _StubContext(device, repo)
    ctx.preferences.direct_message_soft_retries = 2  # 1 send and 2 retries make 3 tries
    chat = ChatService(ctx)

    sent = await chat.send_direct(_contact(), "hey")

    assert sent.acked is True
    assert device.sent == ["hey", "hey", "hey"]  # three transmissions


async def test_send_direct_soft_retries_capped_by_setting(repo: Repository) -> None:
    """The retry budget stops the resends. A DM that never gets an ack is tried retries+1 times."""
    device = _ScriptedDevice([])  # never acknowledges
    ctx = _StubContext(device, repo)
    ctx.preferences.direct_message_soft_retries = 2
    chat = ChatService(ctx)

    sent = await chat.send_direct(_contact(), "hey")

    assert sent.acked is False
    assert len(device.sent) == 3  # the cap: one send and two soft retries at the most


async def test_send_direct_zero_retries_is_one_shot(repo: Repository) -> None:
    """If soft retries are off, a DM is transmitted one time, with an ack or without it."""
    device = _ScriptedDevice([])  # never acknowledges
    ctx = _StubContext(device, repo)
    ctx.preferences.direct_message_soft_retries = 0
    chat = ChatService(ctx)

    sent = await chat.send_direct(_contact(), "hey")

    assert sent.acked is False
    assert device.sent == ["hey"]  # one transmission, no soft retry


# -- end-to-end through the real session --------------------------------------


async def test_open_chat_sends_through_real_session(tmp_path: Path) -> None:
    """open_chat, driven with piped keys, sends a message and stores it, end to end."""
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from rich.console import Console

    from meshterm.context import AppContext
    from meshterm.core.admin_store import AdminStore
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
    conv = Conversation(
        label="Alice",
        is_channel=False,
        contact=Contact(name="Alice", public_key="d4e5f6a7" + "0" * 56, key_prefix="d4e5f6a7"),
    )
    try:
        with create_pipe_input() as inp:
            session = TuiSession(input=inp, output=DummyOutput())
            ctx.ui = TuiUi(session)

            def stored_texts() -> list[str]:
                rows = ctx.repo.recent_chat_messages(is_channel=False, peer="d4e5f6a7")
                return [m.text for m in rows]

            async def drive() -> None:
                # Send Esc only after the app stores the send. The send runs in a task that
                # the screen starts. If the test pipes Esc next to Enter, a slow runner can
                # leave (and cancel the task) before the app writes the row.
                inp.send_text("hi\r")  # type "hi", then Enter (send)
                while not stored_texts():
                    await asyncio.sleep(0.01)
                inp.send_text("\x1b")  # Esc (leave)

            async def main() -> None:
                driver = asyncio.ensure_future(drive())
                try:
                    await open_chat(ctx, conv)
                finally:
                    driver.cancel()

            await asyncio.wait_for(session.run(main()), timeout=5)

        stored = ctx.repo.recent_chat_messages(is_channel=False, peer="d4e5f6a7")
        assert [m.text for m in stored] == ["hi"]
        assert stored[0].outbound is True
    finally:
        await ctx.aclose()


# -- the picker's companion filter and history delete -------------------------


def test_delete_chat_history_removes_only_that_peer(repo: Repository) -> None:
    """If the user deletes one direct thread, the other peers and the channel history stay."""
    repo.record_chat_message(ChatMessage(text="a", peer="aa"))
    repo.record_chat_message(ChatMessage(text="b", peer="bb"))
    repo.record_chat_message(ChatMessage(text="c", is_channel=True, channel_id="c0"))

    assert repo.delete_chat_history("aa") == 1
    assert repo.recent_chat_messages(is_channel=False, peer="aa") == []
    assert [m.text for m in repo.recent_chat_messages(is_channel=False, peer="bb")] == ["b"]
    assert [m.text for m in repo.recent_chat_messages(is_channel=True, channel_id="c0")] == ["c"]


class _PickerDevstate:
    def __init__(self, contacts) -> None:
        self._contacts = contacts

    async def channel_slots(self):
        return []

    async def contacts(self):
        return list(self._contacts)


class _PickerChat(_FakeChat):
    def __init__(self, unread=None) -> None:
        super().__init__(unread or {})
        self.cleared: list[str] = []

    def clear_unread(self, key: str) -> None:
        self.cleared.append(key)


async def test_picker_lists_companions_only(repo: Repository) -> None:
    """Only companion contacts (and contacts of an unknown type) appear in Direct, not repeaters."""
    from types import SimpleNamespace

    from meshterm.core.models import NODE_TYPE_REPEATER
    from meshterm.tools.chat import ChatTool
    from meshterm.ui.tui import Choice

    contacts = [
        Contact(name="Ally", public_key="d4" + "0" * 62, node_type=1),
        Contact(name="Mystery", public_key="60" + "0" * 62),  # the type was not in an advert
        Contact(name="Tower", public_key="a1" + "0" * 62, node_type=NODE_TYPE_REPEATER),
    ]

    ctx = SimpleNamespace(devstate=_PickerDevstate(contacts), repo=repo, chat=_PickerChat())
    items = await ChatTool()._picker_items(ctx)
    direct = [
        it.value.label
        for it in items
        if isinstance(it, Choice) and isinstance(it.value, Conversation) and not it.value.is_channel
    ]
    assert "Ally" in direct and "Mystery" in direct and "Tower" not in direct


async def test_picker_pins_its_column_header_over_the_group_heading(
    repo: Repository,
) -> None:
    """On a page scrolled into Direct, the lane names stay at the top, over ``👤 Direct``."""
    from types import SimpleNamespace

    from meshterm.tools.chat import ChatTool
    from meshterm.ui.tui import SelectScreen, frame

    ansi = re.compile(r"\x1b\[[0-9;]*m")
    contacts = [
        Contact(name=f"Peer{i:02d}", public_key=f"{i:02x}" + "0" * 62, node_type=1)
        for i in range(12)
    ]

    ctx = SimpleNamespace(devstate=_PickerDevstate(contacts), repo=repo, chat=_PickerChat())
    items = await ChatTool()._picker_items(ctx)

    header = items[0]
    assert header.pinned  # the lanes mean the same in both groups, so they pin for the list
    screen = SelectScreen("Chat", items)
    for _ in range(8):  # down, after the Direct heading
        screen.handle("down")
    visible, above, _below = frame._visible_slice(screen, screen.render_body(72), 8)
    top = [ansi.sub("", row).strip() for row in visible[:2]]
    # Short words over a badge and an age, then the message.
    assert top[0].split() == ["CONVERSATION", "NEW", "LAST", "MESSAGE"]
    assert top[1] == "── 👤 Direct ──"
    assert above is True
    # If the width is too small for the whole line, the last label gets shorter and does
    # not wrap. The lane has the size of these six-cell names, so this starts at a width
    # much smaller than any real terminal. The test is for the fallback, not for the width
    # where it starts.
    assert header.text(36).endswith(" MSG")
    assert "\n" not in header.text(30)


async def test_picker_lane_is_sized_to_the_names_it_holds(repo: Repository) -> None:
    """The conversation lane fits the longest name, between its minimum and its maximum.

    Each cell of the lane after the longest name is padding that the app takes from the
    message preview. The preview is the only content of the row that has no limit. Thus the
    app measures the lane and does not use a fixed number.
    """
    from meshterm.tools.chat import _LABEL_MAX, _LABEL_MIN, _lanes

    def lane(*names: str) -> int:
        return _lanes([Conversation(label=n, is_channel=True, channel_id=n) for n in names]).label

    assert lane("Bob", "Ann") == _LABEL_MIN  # short names cannot make it smaller than the header
    assert lane("Bob", "YUL-Cartierville") == len("YUL-Cartierville")
    assert lane("A Rather Long Contact Name Indeed") == _LABEL_MAX  # and it has a maximum


async def test_picker_rows_scroll_the_message_under_pinned_lanes(repo: Repository) -> None:
    """In a row, ←→ scroll only the preview. The lanes before it stay in place.

    Those lanes show the place of the user in the list, so the scroll starts where the
    message starts. This is one column for each row, with a long name or a short name.
    """
    from types import SimpleNamespace

    from meshterm.tools.chat import ChatTool
    from meshterm.ui.tui import Choice

    contacts = [
        Contact(name="Alice", public_key="d4" + "0" * 62, node_type=1),
        Contact(name="A Rather Long Contact Name", public_key="60" + "0" * 62, node_type=1),
    ]
    alice = Conversation(label="Alice", is_channel=False, contact=contacts[0])
    repo.record_chat_message(ChatMessage(text="x" * 200, peer=alice.peer))
    ctx = SimpleNamespace(devstate=_PickerDevstate(contacts), repo=repo, chat=_PickerChat())
    rows = [it for it in await ChatTool()._picker_items(ctx) if isinstance(it, Choice)]

    heads = {row.hscroll_from for row in rows}
    assert len(heads) == 1 and heads.pop() > 0  # one head block for the whole list
    talked = next(row for row in rows if row.value.label == "Alice")
    line = talked.label.plain
    # The test measures in cells, not in characters: a channel glyph is one character and two cells.
    assert cell_len(line[: line.index("x")]) == talked.hscroll_from
    # Also, the preview keeps the whole message. The row makes the cut, and ←→ move past it.
    assert talked.label.plain.endswith("x" * 200)


def test_picker_marker_lane_lines_the_two_groups_up(repo: Repository) -> None:
    """A channel glyph and a contact dot use the same cells, on each platform.

    That glyph is two cells wide on regular and one cell wide in the console font. Thus the
    dot gets padding to the width that the glyph measures, not to a fixed width. A fixed
    padding put each channel row one cell out of line with each direct row on the PicoCalc.
    """
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
    from meshterm.tools.chat import _lanes

    channel = Conversation(label="Public", is_channel=True, channel_idx=0, channel_id="c0")
    contact = Conversation(
        label="Alice", is_channel=False, contact=Contact(name="Alice", public_key="d4" * 32)
    )
    ctx = _RowCtx(repo)
    try:
        for platform in (REGULAR, PICOCALC_LYRA):
            set_platform(platform)
            lanes = _lanes([channel, contact])
            rows = [_title(ctx, c, {}, _NO_KEYS, lanes).plain for c in (channel, contact)]
            names = ("Public", "Alice")
            at = [cell_len(r[: r.index(n)]) for r, n in zip(rows, names, strict=True)]
            assert at[0] == at[1] == lanes.marker
    finally:
        set_platform(REGULAR)


async def test_picker_footer_fits_with_every_atom_showing(repo: Repository, monkeypatch) -> None:
    """The scroll atom, the erase atom, and the base atoms together fit in the 72-cell footer.

    The three groups of atoms show together on an ordinary row: a thread with a history that
    is long enough to scroll. Thus the budget is a limit on the wording, not a rare case.
    (This is why the atom is ``Del erase``: the row already names the conversation whose
    history the key deletes.)
    """
    import asyncio
    from types import SimpleNamespace

    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from meshterm.tools.chat import ChatTool
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.tui.screen import CANCEL
    from meshterm.ui.tui.session import TuiSession

    ally = Contact(name="Ally", public_key="d4" + "0" * 62, node_type=1)
    conv = Conversation(label="Ally", is_channel=False, contact=ally)
    repo.record_chat_message(ChatMessage(text="y" * 200, peer=conv.peer))
    ctx = SimpleNamespace(devstate=_PickerDevstate([ally]), repo=repo, chat=_PickerChat())
    hints: list[str] = []

    with create_pipe_input() as inp:
        session = TuiSession(input=inp, output=DummyOutput())
        ctx.ui = TuiUi(session)

        async def main() -> None:
            run = asyncio.ensure_future(ChatTool()._run_live(ctx))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2
            while not session._stack:
                assert loop.time() < deadline, "the picker never opened"
                await asyncio.sleep(0)
            picker = session._stack[-1]
            picker.render_body(72)  # the overflow probe reads the last render width
            picker.handle("down")  # to the one Direct row: it can be deleted, and it overflows
            picker.render_body(72)
            hints.append(picker.footer_hint)
            picker.resolve(CANCEL)
            await asyncio.wait_for(run, timeout=2)

        await asyncio.wait_for(session.run(main()), timeout=5)

    hint = hints[0]
    assert cell_len(hint) <= 72, f"{cell_len(hint)} cells: {hint!r}"
    for atom in ("←→ scroll", "Enter open", "Del erase", "Esc back"):
        assert atom in hint


async def test_picker_del_deletes_history_after_a_red_confirm(repo: Repository) -> None:
    """Del on a thread with history deletes the history, after a red confirm.

    The confirm is the destructive type. Afterwards, the history is gone, the unread count
    is cleared, and the rebuilt rows show the thread as hollow and not deletable.
    """
    from types import SimpleNamespace

    from meshterm.tools.chat import ChatTool

    ally = Contact(name="Ally", public_key="d4" + "0" * 62, node_type=1)
    conv = Conversation(label="Ally", is_channel=False, contact=ally)
    repo.record_chat_message(ChatMessage(text="hi", peer=conv.peer))
    repo.record_chat_message(ChatMessage(text="chan", is_channel=True, channel_id="c0"))

    class _Ui:
        def __init__(self) -> None:
            self.dialogs: list[dict] = []

        async def dialog(self, prompt, buttons, **kw):
            self.dialogs.append({"prompt": prompt, "buttons": buttons, **kw})
            return True  # select Delete

    chat = _PickerChat()
    ctx = SimpleNamespace(devstate=_PickerDevstate([ally]), repo=repo, ui=_Ui(), chat=chat)
    tool = ChatTool()
    assert _direct_row(await tool._picker_items(ctx)).deletable  # there is history to delete

    await tool._delete_history(ctx, conv)

    confirm = ctx.ui.dialogs[0]
    assert confirm["destructive"] is True  # the reserved red, for data loss
    assert confirm["buttons"] == [("Cancel", False), ("Delete", True)]

    assert repo.recent_chat_messages(is_channel=False, peer=conv.peer) == []
    assert [m.text for m in repo.recent_chat_messages(is_channel=True, channel_id="c0")] == [
        "chan"
    ]  # the channel history stays
    assert chat.cleared == [conv.key]
    # The new rows show this: the history is gone, so the row changes to a contact with no history.
    assert not _direct_row(await tool._picker_items(ctx)).deletable


async def test_picker_del_cancel_keeps_the_history(repo: Repository) -> None:
    """If the user cancels the confirm, the messages of the thread stay."""
    from types import SimpleNamespace

    from meshterm.tools.chat import ChatTool

    ally = Contact(name="Ally", public_key="d4" + "0" * 62, node_type=1)
    conv = Conversation(label="Ally", is_channel=False, contact=ally)
    repo.record_chat_message(ChatMessage(text="hi", peer=conv.peer))

    class _Ui:
        async def dialog(self, prompt, buttons, **kw):
            return False  # Cancel is the way out

    chat = _PickerChat()
    ctx = SimpleNamespace(devstate=_PickerDevstate([ally]), repo=repo, ui=_Ui(), chat=chat)
    await ChatTool()._delete_history(ctx, conv)
    assert [m.text for m in repo.recent_chat_messages(is_channel=False, peer=conv.peer)] == ["hi"]
    assert chat.cleared == []


def _direct_row(items: list):
    """The one Direct row in a built picker list (the tests above have a single contact)."""
    from meshterm.ui.tui import Choice

    return next(
        it
        for it in items
        if isinstance(it, Choice) and isinstance(it.value, Conversation) and not it.value.is_channel
    )


async def test_the_conversation_picker_stays_pushed_under_an_open_chat(
    repo: Repository, monkeypatch
) -> None:
    """Esc in a chat is one pop, back to the row from which the chat opened.

    The app once gathered the picker in ``prompt_params`` and rebuilt it in each round. A
    ``default=`` restore put the highlight back. Now the app keeps the one screen, so the
    typed filter and the scroll stay with it. The app changes the rows in place, so a thread
    that has new messages is where its recency puts it.
    """
    import asyncio
    from types import SimpleNamespace

    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from meshterm.tools.chat import ChatTool
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.tui.screen import CANCEL
    from meshterm.ui.tui.session import TuiSession

    ally = Contact(name="Ally", public_key="d4" + "0" * 62, node_type=1)
    ctx = SimpleNamespace(devstate=_PickerDevstate([ally]), repo=repo, chat=_PickerChat())
    depths: list[int] = []

    with create_pipe_input() as inp:
        session = TuiSession(input=inp, output=DummyOutput())
        ctx.ui = TuiUi(session)

        async def fake_open_chat(_ctx, conversation) -> int:
            """Stand in for the chat screen: note the stack under it, then leave."""
            depths.append(len(session._stack))
            return 3

        monkeypatch.setattr("meshterm.ui.chat.open_chat", fake_open_chat)

        async def main() -> None:
            run = asyncio.ensure_future(ChatTool()._run_live(ctx))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2
            while not session._stack:
                assert loop.time() < deadline, "the picker never opened"
                await asyncio.sleep(0)
            picker = session._stack[-1]
            picker.resolve(Conversation(label="Ally", is_channel=False, contact=ally))
            while not depths:
                await asyncio.sleep(0)
            picker.resolve(CANCEL)  # Esc in the picker goes to the menu
            result = await asyncio.wait_for(run, timeout=2)
            assert result.summary == {"conversations": 1, "messages": 3}

        await asyncio.wait_for(session.run(main()), timeout=5)

    assert depths == [1], "the picker is still on the stack while the chat runs"
    assert session._stack == [], "and the visit pops it on the way out"


# -- the badge rule: what raises the unread count of the header ----------------


class _GatedContext(_StubContext):
    """A context with the device-state cache, so the badge rule can ask the device.

    The plain :class:`_StubContext` has no cache. This is the path where the app gives the
    message the benefit of the doubt: a message notifies when there is no device state to
    place it against.
    """

    def __init__(self, device: MockDevice, repo: Repository) -> None:
        super().__init__(device, repo)
        from meshterm.services.device_state import DeviceState

        self.devstate = DeviceState(self)


async def _record(chat: ChatService, ctx, message: Message) -> None:
    """Publish one inbound message and wait until the worker files it."""
    ctx.events.publish(MeshEvent.message_event(message))
    await chat._queue.join()


async def test_badge_ignores_direct_messages_from_non_recipients(repo: Repository) -> None:
    """The direct messages of a repeater go into the history, but they do not raise the badge.

    A repeater is not a DM recipient, so the picker does not list it. But a repeater does
    send us direct messages: each reply of the remote CLI from a repeater that you administer
    is one. If the app counted them, the badge pointed at a conversation with no row, and
    the user could not clear it.
    """
    device = MockDevice()  # its Yagi-Repeater contact is a NODE_TYPE_REPEATER
    ctx = _GatedContext(device, repo)
    chat = ChatService(ctx)
    await chat.start()
    try:
        await _record(chat, ctx, Message(text="> ok", sender="a1b2c3d4"))
        assert chat.unread("dm:a1b2c3d4") == 0
        # Silent, but not dropped: the reply is in the transcript.
        stored = repo.recent_chat_messages(is_channel=False, peer="a1b2c3d4")
        assert [m.text for m in stored] == ["> ok"]
    finally:
        await chat.stop()
        await device.disconnect()


async def test_badge_ignores_a_sender_with_no_contact(repo: Repository) -> None:
    """A sender that is not in the contact table is stored silently. There is no row to open."""
    device = MockDevice()
    ctx = _GatedContext(device, repo)
    chat = ChatService(ctx)
    await chat.start()
    try:
        await _record(chat, ctx, Message(text="hi", sender="ffee11223344"))
        assert chat.unread("dm:ffee11223344") == 0
        assert repo.recent_chat_messages(is_channel=False, peer="ffee11223344")
    finally:
        await chat.stop()
        await device.disconnect()


async def test_badge_ignores_a_channel_the_device_has_no_slot_for(repo: Repository) -> None:
    """A channel message that has no configured slot stays silent.

    There are two cases. The slot can be empty, so the message has the fallback identity from
    the slot, which names no channel. Or the device does not list the slot. In both cases the
    picker has no row.
    """
    device = MockDevice()
    await device.connect()
    device._channels[0] = {
        "channel_idx": 0,
        "channel_name": "Public",
        "channel_secret": DEFAULT_PUBLIC_SECRET,
    }
    ctx = _GatedContext(device, repo)
    chat = ChatService(ctx)
    await chat.start()
    try:
        await _record(chat, ctx, Message(text="Bob: yo", channel=3, is_channel=True))
        assert chat.unread("chan:slot:3") == 0
        assert repo.recent_chat_messages(is_channel=True, channel_id="slot:3")
    finally:
        await chat.stop()
        await device.disconnect()


async def test_badge_counts_a_configured_channel_and_a_companion(repo: Repository) -> None:
    """The positive control: a message that the picker lists still raises the badge."""
    from meshterm.core.channels import channel_identity

    device = MockDevice()
    await device.connect()
    device._channels[0] = {
        "channel_idx": 0,
        "channel_name": "Public",
        "channel_secret": DEFAULT_PUBLIC_SECRET,
    }
    ctx = _GatedContext(device, repo)
    chat = ChatService(ctx)
    await chat.start()
    try:
        await _record(chat, ctx, Message(text="Ann: hey", channel=0, is_channel=True))
        await _record(chat, ctx, Message(text="hello", sender="d4e5f6a7"))  # Alice, a companion
        public = channel_identity("Public", DEFAULT_PUBLIC_SECRET)
        assert chat.unread(f"chan:{public}") == 1
        assert chat.unread("dm:d4e5f6a7") == 1
    finally:
        await chat.stop()
        await device.disconnect()


async def test_badge_notifies_when_the_device_state_is_unavailable(repo: Repository) -> None:
    """If there is no device state to place a message against, the message notifies.

    A cold or broken cache is a reason to notify too much. It is never a reason to remove a
    real message.
    """
    device = MockDevice()
    ctx = _StubContext(device, repo)  # no devstate, on purpose
    chat = ChatService(ctx)
    await chat.start()
    try:
        await _record(chat, ctx, Message(text="ping", sender="ffee11223344"))
        assert chat.unread("dm:ffee11223344") == 1
    finally:
        await chat.stop()
        await device.disconnect()


async def test_badge_still_respects_a_muted_channel(tmp_path: Path, repo: Repository) -> None:
    """If the user mutes a configured channel, it stays silent. The mute that the user set wins."""
    from meshterm.core.channels import channel_identity
    from meshterm.core.mute_store import MuteStore

    device = MockDevice()
    await device.connect()
    device._channels[0] = {
        "channel_idx": 0,
        "channel_name": "Public",
        "channel_secret": DEFAULT_PUBLIC_SECRET,
    }
    public = channel_identity("Public", DEFAULT_PUBLIC_SECRET)
    ctx = _GatedContext(device, repo)
    ctx.mute_store = MuteStore(tmp_path / "mutes.json")
    ctx.mute_store.set_muted(public, True)
    chat = ChatService(ctx)
    await chat.start()
    try:
        await _record(chat, ctx, Message(text="Ann: hey", channel=0, is_channel=True))
        assert chat.unread(f"chan:{public}") == 0
        assert repo.recent_chat_messages(is_channel=True, channel_id=public)
    finally:
        await chat.stop()
        await device.disconnect()


async def test_channel_slot_change_drops_the_cached_channel_list(repo: Repository) -> None:
    """If a slot resolves to a new identity, the cached channel list of the screens is invalid.

    The recorder reads a slot for each message, so it sees first a channel that another
    client (the phone app) changed to a new key. The screens and the badge rule above read
    from the session cache, and only a channel edit in the app clears that cache. If the
    cache stays old, it keeps the channels that the device had at connect time. Then a
    message on the new channel goes unnoticed, because the cached list has no row for it.
    """
    from meshterm.core.channels import derive_secret

    device = MockDevice()
    await device.connect()
    device._channels[0] = {
        "channel_idx": 0,
        "channel_name": "Public",
        "channel_secret": DEFAULT_PUBLIC_SECRET,
    }
    ctx = _StubContext(device, repo)

    class _Devstate:
        def __init__(self) -> None:
            self.invalidations = 0

        def invalidate_channels(self) -> None:
            self.invalidations += 1

    ctx.devstate = _Devstate()
    chat = ChatService(ctx)

    first = await chat.channel_id_for(0)
    assert ctx.devstate.invalidations == 0  # the first read of a slot is not a change
    assert await chat.channel_id_for(0) == first
    assert ctx.devstate.invalidations == 0  # a slot that did not change leaves the cache as it is

    # Another client gives slot 0 a new key, and we do not know it.
    device._channels[0] = {
        "channel_idx": 0,
        "channel_name": "#montreal",
        "channel_secret": derive_secret("#montreal"),
    }
    assert await chat.channel_id_for(0) != first
    assert ctx.devstate.invalidations == 1

    # To clear the slot is also a change. It gives the fallback identity from the slot.
    device._channels.pop(0)
    assert await chat.channel_id_for(0) == "slot:0"
    assert ctx.devstate.invalidations == 2
    await device.disconnect()


def test_chat_fkey_lane_steps_by_day_on_the_free_left_pair() -> None:
    """The sections of the transcript are days, so F1 and F2 name the section step ``Day``.

    The keys and the actions are the same as ``Sect ↑`` and ``Sect ↓`` of a grouped select
    list. The lane names what the sections are here. The chips are lit only if there is
    more than one day to step between, as a list with one section has no section jump.
    """
    from datetime import timedelta

    from meshterm.core.models import utcnow

    now = utcnow()
    one_day = [ChatMessage(text="hi", peer="d4e5f6a7", created_at=now)]
    single = _screen(_StubSession(), send=None, messages=one_day)
    assert [pair.label for pair in single.picocalc_lyra_lane[:2]] == ["Day ↑", "Day ↓"]
    assert [pair.action for pair in single.picocalc_lyra_lane[:2]] == [
        "ctrl_pageup",
        "ctrl_pagedown",
    ]
    assert not any(
        pair.enabled for pair in single.picocalc_lyra_lane[:2]
    )  # one day: no place to step to

    spread = _screen(
        _StubSession(),
        send=None,
        messages=[
            ChatMessage(text="then", peer="d4e5f6a7", created_at=now - timedelta(days=2)),
            ChatMessage(text="now", peer="d4e5f6a7", created_at=now),
        ],
    )
    assert all(pair.enabled for pair in spread.picocalc_lyra_lane[:2])
    # A left-hand pair rises toward F1, so a step back through the transcript is the outer
    # key. This is the same direction as the pager on the right.
    assert fkeys.PICOCALC_LYRA_DECK.action_for(spread.picocalc_lyra_lane, 1) == "ctrl_pageup"
    spread.handle(fkeys.PICOCALC_LYRA_DECK.action_for(spread.picocalc_lyra_lane, 1))
    assert spread._selected == 0  # moved to the first message of the older day


# -- URL codes (the PicoCalc) ---------------------------------------------------


def _code_rows(lines: list[str]) -> list[str]:
    """The rendered rows that have braille, that is, the rows of a QR code in a transcript."""
    return [ln for ln in lines if any("⠀" <= ch <= "⣿" for ch in ln)]


def _decode_braille(rows: list[str], left: int, size: int) -> list[list[bool]]:
    """The ``size``-module code drawn in braille from cell ``left`` of ``rows``."""
    dots = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))
    return [
        [
            bool((ord(rows[y // 4][left + x // 2]) - 0x2800) & dots[x % 2][y % 4])
            for x in range(size)
        ]
        for y in range(size)
    ]


def test_on_the_picocalc_each_url_hangs_a_code_under_its_message_side_by_side() -> None:
    """Two links give two braille codes on the same rows, at the indent of the body.

    Each module must stay correct in the packing. The test decodes each code from the
    braille and compares it with the segno matrix for its URL, at the standard fit.
    """
    import segno

    from meshterm.platforms import PICOCALC_LYRA, set_platform

    set_platform(PICOCALC_LYRA)
    first, second = "https://meshterm.net", "https://github.com/jpmartineau/MeshTerm"
    conv = Conversation(label="#ops", is_channel=True, channel_idx=1)
    message = ChatMessage(text=f"Alice: map at {first}, code at {second}!", is_channel=True)
    screen = ChatScreen(conv, [message], send=None, names={}, session=_StubSession())
    rows = _code_rows(_strip_ansi(screen._render_grouped(53)).splitlines())

    indent = 9  # "  HH:MM  ", the hanging indent of the body
    assert rows and all(len(row) <= 53 and row.startswith(" " * indent) for row in rows)
    cells = [row[indent:].ljust(53 - indent) for row in rows]
    left, tallest = 0, 0
    for url in (first, second):
        matrix = [
            [bool(v) for v in row] for row in segno.make(url, error="m").matrix_iter(border=4)
        ]
        assert _decode_braille(cells, left, len(matrix)) == matrix, url
        left += -(-len(matrix) // 2) + 1  # its cells, then the gap of one cell
        tallest = max(tallest, -(-len(matrix) // 4))
    assert len(rows) == tallest  # one band, as tall as the taller code


def test_a_regular_terminal_hangs_no_code_under_a_url() -> None:
    """A code in half blocks has four times the area. Under each link, codes fill the chat."""
    conv = Conversation(label="#ops", is_channel=True, channel_idx=1)
    message = ChatMessage(text="Alice: map at https://meshterm.net", is_channel=True)
    screen = ChatScreen(conv, [message], send=None, names={}, session=_StubSession())
    rendered = _strip_ansi(screen._render_grouped(72))
    assert "https://meshterm.net" in rendered
    assert not _code_rows(rendered.splitlines())
    assert not any(ch in "▀▄█" for ch in rendered)


def test_codes_that_outrun_the_line_start_a_new_band() -> None:
    """Two short links fit next to each other in the 44 cells after the indent. A third wraps."""
    import segno

    from meshterm.platforms import PICOCALC_LYRA, set_platform
    from meshterm.ui.qr import qr_strip

    set_platform(PICOCALC_LYRA)
    urls = [f"https://meshterm.net/{c}" for c in "abc"]
    size = segno.make(urls[0], error="m").symbol_size(border=4)[0]
    cols, height = -(-size // 2), -(-size // 4)
    rows = qr_strip(urls, 53, indent=9).plain.splitlines()

    assert len(rows) == 2 * height and all(len(row) <= 53 for row in rows)
    assert all(row[9 + cols] == " " and row[9 + cols + 1] != " " for row in rows[:height])
    assert all(row[9 + cols :].strip() == "" for row in rows[height:])


def test_a_picked_message_keeps_its_codes_in_view() -> None:
    """If the selection moves down to a link, the screen shows its code, not only the first row."""
    from meshterm.platforms import PICOCALC_LYRA, set_platform
    from meshterm.ui.tui import frame

    set_platform(PICOCALC_LYRA)
    conv = Conversation(label="#ops", is_channel=True, channel_idx=1)
    messages = [ChatMessage(text=f"Alice: line {i}", is_channel=True) for i in range(12)]
    messages.insert(6, ChatMessage(text="Bob: map at https://meshterm.net", is_channel=True))
    screen = ChatScreen(conv, messages, send=None, names={}, session=_StubSession())
    viewport = 16

    def paint() -> list[str]:
        visible, _above, _below = frame._visible_slice(screen, screen.render_body(53), viewport)
        return _strip_ansi(visible).splitlines()

    paint()
    screen.handle("ctrl_home")  # the oldest message, then down to the link
    for _ in range(6):
        paint()
        screen.handle("down")
    shown = paint()
    whole = _code_rows(_strip_ansi(screen._render_grouped(53)).splitlines())
    assert any("map at https://meshterm.net" in row for row in shown)
    assert len(_code_rows(shown)) == len(whole)  # each row of the code is visible


# -- URLs kept whole, and their codes on ^U --------------------------------------------

#: A link that is longer than the regular body lane (72 minus the 9-cell gutter), but that
#: the screen can hold.
_LONG_URL = "https://github.com/jpmartineau/MeshTerm/blob/main/meshterm/ui/chat.py"


def _url_chat(text: str, session=None) -> ChatScreen:  # noqa: ANN001
    conv = Conversation(label="#ops", is_channel=True, channel_idx=1)
    message = ChatMessage(text=f"Alice: {text}", is_channel=True)
    return ChatScreen(conv, [message], send=None, names={}, session=session or _StubSession())


def test_a_url_too_long_to_hang_steps_out_to_the_gutter_whole() -> None:
    """A link that the body lane cannot hold, but the screen can, is drawn whole on one line.

    It starts under the timestamp, away from the ``❯`` of the selected message. The body
    continues at its own indent after the link, so only the link leaves the block.
    """
    screen = _url_chat(f"look at {_LONG_URL} and tell me what you think of it all")
    rows = _strip_ansi(screen._render_grouped(72)).splitlines()
    link = next(row for row in rows if _LONG_URL[:20] in row)
    assert link == "  " + _LONG_URL  # whole, at the gutter
    after = rows[rows.index(link) + 1]
    assert after.startswith(" " * 9 + "and tell me")  # back under the indent of the body

    screen.handle("up")  # selected: the ❯ is on the first line, not on the line of the link
    rows = _strip_ansi(screen._render_grouped(72)).splitlines()
    assert "  " + _LONG_URL in rows


def test_a_short_tail_finishes_the_links_line_and_a_too_wide_link_still_folds() -> None:
    """Text that fits after a link at the gutter stays on its line. A wider link folds."""
    url = "https://meshterm.net/" + "a" * 46  # 67 cells: wider than the lane of 63 cells
    rows = _strip_ansi(_url_chat(f"see {url} ok")._render_grouped(72)).splitlines()
    assert "  " + url + " ok" in rows  # 2 + 67 + 3: the width of the screen
    rows = _strip_ansi(_url_chat(f"see {url} ok!")._render_grouped(72)).splitlines()
    assert "  " + url in rows and " " * 9 + "ok!" in rows  # one cell too many: below it

    wide = "https://meshterm.net/" + "b" * 60  # 81 cells: wider than the screen
    rows = _strip_ansi(_url_chat(f"see {wide}")._render_grouped(72)).splitlines()
    assert not any(wide in row for row in rows)  # cut, as before
    assert all(row.startswith(" " * 9) for row in rows if "bbb" in row)  # hanging, as before


def test_ctrl_u_opens_the_picked_messages_links_on_one_share_screen() -> None:
    """^U shows all the links of the selected message as QR codes, in the order of the message."""
    from meshterm.ui.qr import QrScreen

    opened: list = []

    class Session(_StubSession):
        async def run_screen(self, screen):  # noqa: ANN001, ANN202
            opened.append(screen)

    async def scenario() -> None:
        screen = _url_chat(f"map at https://meshterm.net, code at {_LONG_URL}!", Session())
        screen.handle("url_code")  # nothing selected: no message to take links from
        await asyncio.sleep(0)
        assert opened == []

        screen.handle("up")
        screen.handle("url_code")
        await asyncio.sleep(0)
        assert len(opened) == 1 and isinstance(opened[0], QrScreen)
        assert opened[0].urls == ("https://meshterm.net", _LONG_URL)

    asyncio.run(scenario())


def test_ctrl_u_is_named_only_on_a_picked_message_with_a_link() -> None:
    """The hint names ^U where it acts and fits in 72 cells. The lane lights QR in the same way."""
    plain = _url_chat("no links here")
    linked = _url_chat("map at https://meshterm.net")
    assert "^U" not in linked.footer_hint  # nothing selected yet
    linked.handle("up")
    plain.handle("up")
    assert "^U QR" in linked.footer_hint and "^U" not in plain.footer_hint
    assert (
        fkeys.PICOCALC_LYRA_DECK.action_for(linked.picocalc_lyra_lane, 7) == "url_code"
    )  # Shift+F2
    assert linked.picocalc_lyra_lane[1].opp_enabled and not plain.picocalc_lyra_lane[1].opp_enabled

    # The hint of each selection state, with a link in it, fits in the 72-cell budget.
    direct = _screen(
        _StubSession(),
        send=None,
        messages=[ChatMessage(text="see https://meshterm.net", peer="d4e5f6a7")],
    )
    direct.handle("up")
    scoped = _url_chat("x")
    scoped._retry_target = lambda: 0  # a selected message to send again: the longest hint
    scoped._messages[0] = ChatMessage(text="see https://meshterm.net", is_channel=True)
    scoped.handle("up")
    for screen in (linked, direct, scoped):
        assert "^U QR" in screen.footer_hint
        assert len(screen.footer_hint) <= 72, screen.footer_hint


def test_on_the_cardputer_a_link_takes_the_share_screen_as_on_the_desktop() -> None:
    """The 14 rows have no room for a code under each message, so ^U is the way to a code.

    No code hangs under the message. The lane has QR on its Shift bank (Shift+Fn+5). The
    share screen draws the code in braille. The code is small enough that a link and its
    code share the 53x14 panel, with the URL under the code and nothing a page down.
    """
    from meshterm.platforms import CARDPUTER_ZERO, set_platform
    from meshterm.ui.qr import QrScreen

    set_platform(CARDPUTER_ZERO)
    url = "https://github.com/jpmartineau/MeshTerm"
    screen = _url_chat(f"the road list is at {url}")
    assert not _code_rows(_strip_ansi(screen._render_grouped(53)).splitlines())
    screen.handle("up")
    assert fkeys.CARDPUTER_ZERO_DECK.action_for(screen.cardputer_zero_lane, 17) == "url_code"

    share = QrScreen(url, title="Links")
    share.note_viewport(14)
    lines = _strip_ansi(share.render_body(53)).splitlines()
    assert len(lines) <= 14 and lines[-1].strip() == url
    assert _code_rows(lines), "the code is braille, one module a dot"


def test_a_link_written_without_a_scheme_gets_its_code_too() -> None:
    """``meshterm.net/map`` is a link in the same way as its https form, and it opens as that."""
    from meshterm.ui.qr import QrScreen

    opened: list = []

    class Session(_StubSession):
        async def run_screen(self, screen):  # noqa: ANN001, ANN202
            opened.append(screen)

    async def scenario() -> None:
        screen = _url_chat("map at meshterm.net/map, notes in file.txt", Session())
        screen.handle("up")
        assert "^U QR" in screen.footer_hint
        screen.handle("url_code")
        await asyncio.sleep(0)
        assert isinstance(opened[0], QrScreen) and opened[0].urls == ("https://meshterm.net/map",)

    asyncio.run(scenario())


# -- acks: listen early, wait long enough, and catch the late acks -------------


class _AckingMeshCore:
    """A meshcore stand-in. Its send accepts the message, then acks it as the script says.

    ``ack_at`` is the number of seconds after the send returns (``None`` means never).
    ``during_send`` pushes the ack before the send returns, as an ack from a neighbour can.
    """

    def __init__(self, *, ack_at: float | None, during_send: bool = False, flood: bool = True):
        self.subs: list = []
        self.commands = self
        self._ack_at = ack_at
        self._during = during_send
        self._flood = flood

    def subscribe(self, event_type, callback, attribute_filters=None):  # noqa: ANN001, ANN201
        from types import SimpleNamespace

        sub = SimpleNamespace(event_type=event_type, callback=callback)
        sub.unsubscribe = lambda: self.subs.remove(sub) if sub in self.subs else None
        self.subs.append(sub)
        return sub

    def _push_ack(self) -> None:
        from types import SimpleNamespace

        from meshcore import EventType

        event = SimpleNamespace(type=EventType.ACK, payload={"code": "c0ffee01", "trip_time": 900})
        for sub in list(self.subs):
            if sub.event_type is EventType.ACK:
                sub.callback(event)

    async def send_msg(self, pub, text):  # noqa: ANN001, ANN201
        from types import SimpleNamespace

        if self._during:
            self._push_ack()
        elif self._ack_at is not None:
            asyncio.get_running_loop().call_later(self._ack_at, self._push_ack)
        return SimpleNamespace(
            type="MSG_SENT",
            is_error=lambda: False,
            payload={
                "type": 1 if self._flood else 0,
                "expected_ack": bytes.fromhex("c0ffee01"),
                "suggested_timeout": 100,  # 0.1 s: the estimate of the radio (too optimistic)
            },
        )


def _acking_device(mc) -> object:  # noqa: ANN001
    from meshterm.core.connection import MeshCoreDevice

    device = MeshCoreDevice(port="mock")
    device._mc = mc
    return device


async def test_an_ack_quicker_than_the_send_is_still_caught() -> None:
    """The listener is ready before the send, so an instant ack from a neighbour is not missed."""
    device = _acking_device(_AckingMeshCore(ack_at=None, during_send=True))
    delivery = await device.send_direct_message(_contact(), "hi")
    assert delivery.acked and delivery.code == "c0ffee01" and delivery.flood is True


async def test_an_ack_after_the_radios_estimate_is_still_waited_for(monkeypatch) -> None:  # noqa: ANN001
    """The bug: the wait was the radio estimate times 1.2 (0.12 s here, and 5 s on hardware).

    The ack of a flood returns on the path that the message found. On hardware, it took
    twice the estimate. Now the wait is the estimate plus a round trip, with the same size
    as the round trip for a login.
    """
    import meshterm.core.connection as connection

    monkeypatch.setattr(connection, "trace_timeout", lambda hops: 0.5)
    device = _acking_device(_AckingMeshCore(ack_at=0.3))
    delivery = await device.send_direct_message(_contact(), "hi")
    assert delivery.acked, "an ack at 0.3 s, after the old 0.12 s wait, counts"


async def test_no_ack_in_the_wait_still_says_which_code_to_listen_for(monkeypatch) -> None:  # noqa: ANN001
    """A message that is not yet confirmed has not failed. The send returns the code."""
    import meshterm.core.connection as connection

    monkeypatch.setattr(connection, "trace_timeout", lambda hops: 0.05)
    device = _acking_device(_AckingMeshCore(ack_at=None, flood=False))
    delivery = await device.send_direct_message(_contact(), "hi")
    assert not delivery.acked and delivery.code == "c0ffee01" and delivery.flood is False


async def _late_ack_chat(tmp_path: Path, repo: Repository):  # noqa: ANN202
    """A started chat service with a simulator. The acks for Alice arrive late."""
    device = MockDevice()
    device._late_acks.add("Alice")
    ctx = _StubContext(device, repo)
    chat = ChatService(ctx)
    await chat.start()
    alice = next(c for c in await device.get_contacts() if c.name == "Alice")
    return device, chat, alice


async def test_a_late_ack_turns_the_message_delivered(tmp_path: Path, repo: Repository) -> None:
    """A late ack changes the message in the history and on the chat screen: ✗ becomes ✓."""
    device, chat, alice = await _late_ack_chat(tmp_path, repo)
    heard: list[ChatMessage] = []
    try:
        sent = await chat.send_direct(alice, "you there?", on_late_ack=heard.append)
        assert sent.acked is False
        for _ in range(100):
            if sent.acked:
                break
            await asyncio.sleep(0.01)
        assert sent.acked is True
        stored = repo.recent_chat_messages(is_channel=False, peer=alice.key_prefix)
        assert stored[-1].acked is True
        assert heard == [sent]
    finally:
        await chat.stop()
        await device.disconnect()


async def test_a_late_ack_for_an_earlier_attempt_settles_a_retried_message(
    tmp_path: Path, repo: Repository
) -> None:
    """^R sends the message again with a new code. The ack of the first try still counts."""
    device, chat, alice = await _late_ack_chat(tmp_path, repo)
    try:
        await chat.stop()  # keep the late acks until both tries are sent
        sent = await chat.send_direct(alice, "anyone?")
        first = next(iter(chat._awaiting))
        await chat.resend_direct(alice, sent)
        assert len(chat._awaiting) == 2 and sent.acked is False
        chat._settle_ack(first)
        assert sent.acked is True and not chat._awaiting
        stored = repo.recent_chat_messages(is_channel=False, peer=alice.key_prefix)
        assert len(stored) == 1 and stored[0].acked is True
    finally:
        await device.disconnect()


def test_only_the_acks_the_radio_could_still_push_are_awaited(repo: Repository) -> None:
    """The radio keeps its last eight codes. The service keeps a few more than that, and no more."""
    from meshterm.services.chat_service import _AWAITING_CAP

    chat = ChatService(_StubContext(MockDevice(), repo))
    messages = [ChatMessage(text=str(n), outbound=True, acked=False) for n in range(40)]
    for n, message in enumerate(messages):
        chat._await_ack(message, [f"{n:08x}"], None)
    assert len(chat._awaiting) == _AWAITING_CAP
    chat._settle_ack("00000000")  # removed long ago: nothing to settle
    assert messages[0].acked is False
    chat._settle_ack(f"{39:08x}")
    assert messages[39].acked is True
