# SPDX-License-Identifier: Apache-2.0
"""Courier tests: the outbox store, the eligibility for delivery, and the flow of an attempt.

The tests run the service synchronously (eligibility) and through chat sends that are
stubs (attempts). Thus the tests use no timers and no hardware.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.cells import cell_len
from rich.text import Text

from meshterm.core import exitcodes
from meshterm.core.courier_store import DONE_CAP, QUEUED, CourierStore
from meshterm.core.models import ChatMessage, Contact, utcnow
from meshterm.core.watch_store import WatchStore
from meshterm.services.courier import FRESH_S, MAX_ATTEMPTS, CourierService
from meshterm.ui.courier_screen import (
    CANCEL_SCHEDULE,
    WHEN_HEARD,
    _entry_actions,
    _pick_schedule,
    parse_clock,
)

NODE = "3d" * 6
CONTACT = Contact(name="Hub", public_key="3d" * 32)


class _StubChat:
    """A scripted chat service. It takes the next ack outcome for each send."""

    def __init__(self, outcomes: list[bool]) -> None:
        self.outcomes = list(outcomes)
        self.sent: list[tuple[str, str]] = []
        self.late: list = []  # the late-ack listener that each send without an ack gave

    async def send_direct(self, contact: Contact, text: str, *, on_late_ack=None) -> ChatMessage:  # noqa: ANN001
        self.sent.append((contact.name, text))
        acked = self.outcomes.pop(0) if self.outcomes else False
        chat = ChatMessage(text=text, outbound=True, acked=acked)
        if not acked:
            self.late.append((chat, on_late_ack))
        return chat


class _StubDevice:
    async def get_contacts(self) -> list[Contact]:
        return [CONTACT]


class _StubContext:
    """A minimal stand-in for :class:`~meshterm.context.AppContext`."""

    def __init__(self, tmp_path: Path, outcomes: list[bool]) -> None:
        self.courier_store = CourierStore(tmp_path / "courier.json")
        self.watch_store = WatchStore(tmp_path / "watchtower.json")
        self.chat = _StubChat(outcomes)
        self.is_connected = True
        self.log = logging.getLogger("test.courier")
        self._device = _StubDevice()

    async def device(self) -> _StubDevice:
        return self._device


def _service(tmp_path: Path, outcomes: list[bool]) -> CourierService:
    return CourierService(_StubContext(tmp_path, outcomes))


# --- the store --------------------------------------------------------------------------


def test_store_queue_round_trips_and_persists(tmp_path: Path) -> None:
    """Queued entries stay in a new store instance, and their schedule does not change."""
    path = tmp_path / "courier.json"
    store = CourierStore(path)
    when = utcnow() + timedelta(hours=8)
    queued = store.queue(NODE, "Hub", "hello there", not_before=when)
    assert queued.ident == 1 and queued.status == QUEUED

    again = CourierStore(path)
    entry = again.get(1)
    assert entry is not None and entry.text == "hello there"
    assert entry.not_before == when
    assert again.pending_count() == 1


def test_store_lifecycle_attempts_finish_cancel_clear(tmp_path: Path) -> None:
    """The attempt marks, the delivery, the cancellation, and the clear work correctly."""
    store = CourierStore(tmp_path / "courier.json")
    a = store.queue(NODE, "Hub", "one")
    b = store.queue(NODE, "Hub", "two")
    store.note_attempt(a.ident)
    assert store.get(a.ident).attempts == 1
    store.mark_delivered(a.ident)
    assert store.pending() == [store.get(b.ident)]
    assert store.cancel(b.ident) is True
    assert store.pending_count() == 0
    assert store.get(a.ident) is not None  # the history of delivered entries stays
    store.clear_done()
    assert store.entries() == []


def test_store_caps_the_finished_history(tmp_path: Path) -> None:
    """The store removes old finished entries. It never makes the waiting queue shorter."""
    store = CourierStore(tmp_path / "courier.json")
    keeper = store.queue(NODE, "Hub", "still waiting")
    for i in range(DONE_CAP + 5):
        entry = store.queue(NODE, "Hub", f"m{i}")
        store.mark_delivered(entry.ident)
    done = [m for m in store.entries() if m.status != QUEUED]
    assert len(done) == DONE_CAP
    assert store.get(keeper.ident) is not None


# --- eligibility -------------------------------------------------------------------------


def test_eligibility_waits_for_freshness_and_schedule(tmp_path: Path) -> None:
    """A plain entry needs a contact that was heard. A scheduled entry waits until its time."""
    service = _service(tmp_path, [])
    store = service._ctx.courier_store
    now = utcnow()

    plain = store.queue(NODE, "Hub", "hi")
    assert not service.eligible(plain, now)  # not heard in this session
    service._heard[NODE] = now - timedelta(seconds=FRESH_S + 1)
    assert not service.eligible(plain, now)  # heard, but too long ago
    service._heard[NODE] = now
    assert service.eligible(plain, now)

    scheduled = store.queue(NODE, "Hub", "later", not_before=now + timedelta(hours=1))
    assert not service.eligible(scheduled, now)  # the schedule holds it
    # After its time, the *first* attempt of a scheduled entry runs also if the contact was
    # not heard.
    service._heard.clear()
    assert service.eligible(scheduled, now + timedelta(hours=2))


def test_eligibility_backs_off_after_failures(tmp_path: Path) -> None:
    """A failed attempt waits for its backoff (it doubles).

    This is also true when the contact was heard lately.
    """
    service = _service(tmp_path, [])
    store = service._ctx.courier_store
    now = utcnow()
    entry = store.queue(NODE, "Hub", "hi")
    service._heard[NODE] = now
    store.note_attempt(entry.ident, when=now)
    entry = store.get(entry.ident)
    assert not service.eligible(entry, now + timedelta(minutes=2))
    assert service.eligible(entry, now + timedelta(minutes=6))  # the base of 5 minutes passed
    assert service.next_retry_s(entry, now + timedelta(minutes=2)) is not None


# --- the attempt flow ---------------------------------------------------------------------


async def test_attempt_delivers_and_raises_the_good_news(tmp_path: Path) -> None:
    """A send with an ack settles the entry and lights the Watchtower badge."""
    service = _service(tmp_path, [True])
    ctx = service._ctx
    entry = ctx.courier_store.queue(NODE, "Hub", "hello")
    service._heard[NODE] = utcnow()

    await service._pass()
    assert ctx.chat.sent == [("Hub", "hello")]
    settled = ctx.courier_store.get(entry.ident)
    assert settled.status == "delivered" and settled.attempts == 1
    alerts = ctx.watch_store.alerts()
    assert len(alerts) == 1 and alerts[0].kind == "courier"
    assert "delivered" in alerts[0].message


async def test_attempt_gives_up_after_the_budget(tmp_path: Path) -> None:
    """Attempts without an ack use all the budget, and the service says this exactly one time."""
    service = _service(tmp_path, [False] * MAX_ATTEMPTS)
    ctx = service._ctx
    entry = ctx.courier_store.queue(NODE, "Hub", "hello")
    for _ in range(MAX_ATTEMPTS):
        message = ctx.courier_store.get(entry.ident)
        await service._attempt(message)
    settled = ctx.courier_store.get(entry.ident)
    assert settled.status == "gave-up" and settled.attempts == MAX_ATTEMPTS
    gave = [a for a in ctx.watch_store.alerts() if "gave up" in a.message]
    assert len(gave) == 1


async def test_a_late_ack_is_a_delivery_not_a_reason_to_send_again(tmp_path: Path) -> None:
    """The ack came after the wait of the attempt. When it arrives, the entry is delivered.

    The entry once stayed queued, and the next attempt sent the same message to the
    recipient again. On a mesh, most acks from nodes that are not neighbours arrive late.
    Thus each message had a duplicate.
    """
    service = _service(tmp_path, [False])
    ctx = service._ctx
    entry = ctx.courier_store.queue(NODE, "Hub", "hello")
    await service._attempt(ctx.courier_store.get(entry.ident))
    assert ctx.courier_store.get(entry.ident).status == QUEUED
    chat, on_late_ack = ctx.chat.late[0]
    on_late_ack(chat)
    settled = ctx.courier_store.get(entry.ident)
    assert settled.status == "delivered" and settled.attempts == 1
    assert any("ack came late" in a.message for a in ctx.watch_store.alerts())
    on_late_ack(chat)  # the radio can send the same ack two times
    assert len([a for a in ctx.watch_store.alerts() if "delivered" in a.message]) == 1


async def test_pass_attempts_at_most_one_entry(tmp_path: Path) -> None:
    """A backlog goes down by one message in each pass. The courier never sends a burst."""
    service = _service(tmp_path, [True, True])
    ctx = service._ctx
    ctx.courier_store.queue(NODE, "Hub", "first")
    ctx.courier_store.queue(NODE, "Hub", "second")
    service._heard[NODE] = utcnow()
    await service._pass()
    assert len(ctx.chat.sent) == 1
    await service._pass()
    assert len(ctx.chat.sent) == 2
    assert [t for _n, t in ctx.chat.sent] == ["first", "second"]  # the oldest first


async def test_unknown_contact_spends_no_budget(tmp_path: Path) -> None:
    """A recipient that the device does not know yet stays queued, and the entry does not change."""
    service = _service(tmp_path, [True])
    ctx = service._ctx
    entry = ctx.courier_store.queue("ff" * 6, "Stranger", "hello")
    outcome = await service._attempt(ctx.courier_store.get(entry.ident))
    assert outcome == "unknown contact"
    settled = ctx.courier_store.get(entry.ident)
    assert settled.status == QUEUED and settled.attempts == 0
    assert ctx.chat.sent == []


async def test_attempt_now_forces_a_send(tmp_path: Path) -> None:
    """The Send now of the screen works with no regard for freshness or schedule."""
    service = _service(tmp_path, [True])
    ctx = service._ctx
    entry = ctx.courier_store.queue(NODE, "Hub", "hello", not_before=utcnow() + timedelta(hours=8))
    assert await service.attempt_now(entry.ident) == "delivered"
    assert await service.attempt_now(entry.ident) == "gone"  # the entry is already settled


# --- the scripted CLI actions -------------------------------------------------------------


class _NoteUi:
    """A UI surface that only collects the notes and the renderables that it shows."""

    def __init__(self) -> None:
        self.notes: list[str] = []
        self.shown: list[object] = []

    def note(self, markup: str) -> None:
        self.notes.append(markup)

    def ack(self, markup: str) -> None:
        # The answer of the menu: an acknowledgement is a note. (The scripted CLI removes it.)
        self.note(markup)

    def show(self, *renderables: object) -> None:
        self.shown.extend(renderables)


class _StubCourier:
    """A courier service whose ``attempt_now`` returns a fixed outcome."""

    def __init__(self, store: CourierStore, outcome: str) -> None:
        self._store = store
        self._outcome = outcome

    async def attempt_now(self, ident: int) -> str:
        message = self._store.get(ident)
        if message is None or message.status != QUEUED:
            return "gone"
        if self._outcome == "delivered":
            self._store.mark_delivered(ident)
        return self._outcome


class _ToolCtx:
    """A minimal context for the scripted CLI actions of the courier tool.

    No device is necessary.
    """

    def __init__(self, tmp_path: Path, *, outcome: str = "delivered") -> None:
        self.courier_store = CourierStore(tmp_path / "courier.json")
        self.ui = _NoteUi()
        self.courier = _StubCourier(self.courier_store, outcome)


async def test_cli_list_renders_waiting_and_finished(tmp_path: Path) -> None:
    """`courier list` shows the outbox, and it does not need a device."""
    from meshterm.tools.courier import CourierTool

    ctx = _ToolCtx(tmp_path)
    ctx.courier_store.queue(NODE, "Hub", "still waiting")
    done = ctx.courier_store.queue(NODE, "Hub", "landed")
    ctx.courier_store.mark_delivered(done.ident)

    result = await CourierTool().run(ctx, {"cli_action": "list"})
    assert result.summary == {"entries": 2}
    # The tool *states* the outbox. It does not print it. The tool returns the rows, and the
    # CLI boundary selects a renderer for them (refer to meshterm.ui.report).
    listing = result.report[0]
    assert sorted(row["state"] for row in listing.rows) == ["delivered", "waiting"]
    assert sorted(row["text"] for row in listing.rows) == ["landed", "still waiting"]


async def test_cli_list_empty_notes_and_counts_zero(tmp_path: Path) -> None:
    """An empty outbox gives zero entries and shows no table."""
    from meshterm.tools.courier import CourierTool

    ctx = _ToolCtx(tmp_path)
    result = await CourierTool().run(ctx, {"cli_action": "list"})
    assert result.summary == {"entries": 0}
    assert result.exit_code == exitcodes.NO_RESULT
    # An empty listing, not an absent listing. The plain face draws nothing from it, and the
    # machine face gets `[]`, which is a document that a consumer can read.
    assert result.report[0].rows == []


async def test_cli_cancel_removes_a_waiting_entry(tmp_path: Path) -> None:
    """`courier cancel` removes a waiting entry. A second cancel changes nothing."""
    from meshterm.tools.courier import CourierTool

    ctx = _ToolCtx(tmp_path)
    entry = ctx.courier_store.queue(NODE, "Hub", "nope")
    tool = CourierTool()

    result = await tool.run(ctx, {"cli_action": "cancel", "id": entry.ident})
    assert result.summary == {"id": entry.ident, "cancelled": True}
    assert ctx.courier_store.get(entry.ident) is None

    again = await tool.run(ctx, {"cli_action": "cancel", "id": entry.ident})
    assert again.summary == {"id": entry.ident, "cancelled": False}


async def test_cli_send_forces_one_attempt(tmp_path: Path) -> None:
    """`courier send` forces one delivery attempt and gives the outcome."""
    from meshterm.tools.courier import CourierTool

    ctx = _ToolCtx(tmp_path, outcome="delivered")
    entry = ctx.courier_store.queue(NODE, "Hub", "hello")
    result = await CourierTool().run(ctx, {"cli_action": "send", "id": entry.ident})
    assert result.summary == {"id": entry.ident, "outcome": "delivered"}


async def test_cli_send_unknown_entry_is_a_bad_argument(tmp_path: Path) -> None:
    """An id that does not exist is a *usage* error. It once said that the device failed.

    The outbox is a local file. MeshTerm transmitted nothing, and a new attempt cannot make
    an id exist. Exit 4 tells a caller "the radio was reached and the operation failed, try
    again". This sent a scheduled sender into a loop of attempts because of a typing error.
    Exit 2 says "correct the command", and this is the truth.
    """
    import typer

    from meshterm.tools.courier import CourierTool

    ctx = _ToolCtx(tmp_path)
    with pytest.raises(typer.BadParameter):
        await CourierTool().run(ctx, {"cli_action": "send", "id": 999})


async def test_cli_clear_drops_finished_only(tmp_path: Path) -> None:
    """`courier clear` removes the finished entries and does not change the waiting queue."""
    from meshterm.tools.courier import CourierTool

    ctx = _ToolCtx(tmp_path)
    ctx.courier_store.queue(NODE, "Hub", "still here")
    done = ctx.courier_store.queue(NODE, "Hub", "gone soon")
    ctx.courier_store.mark_delivered(done.ident)

    result = await CourierTool().run(ctx, {"cli_action": "clear"})
    assert result.summary == {"cleared": 1}
    assert [m.text for m in ctx.courier_store.entries()] == ["still here"]


# --- the clock parser ---------------------------------------------------------------------


def test_parse_clock_finds_the_next_occurrence() -> None:
    """HH:MM becomes the next future occurrence, in local time.

    The function returns it as UTC.
    """
    now = utcnow()
    when = parse_clock("07:00", now)
    assert when is not None and when > now
    assert (when - now) <= timedelta(days=1)
    assert when.astimezone().hour == 7 and when.astimezone().minute == 0
    assert parse_clock("7h30", now) is not None
    assert parse_clock("25:00", now) is None
    assert parse_clock("soonish", now) is None


# --- the schedule picker ------------------------------------------------------------------


class _StubScheduleSession:
    """A script for the when-to-send picker.

    ``choose`` maps the rows that it gets to a selection.
    """

    def __init__(self, choose) -> None:
        self._choose = choose

    async def select(self, title, items, **kwargs):
        return self._choose(items)

    async def text(self, *args, **kwargs):  # pragma: no cover - unused on these paths
        return ""


class _ScheduleCtx:
    def __init__(self, session) -> None:
        self.ui = SimpleNamespace(session=session)


async def test_pick_schedule_when_next_heard_returns_no_schedule() -> None:
    """If the user selects ``When it's next heard``, the function returns ``None``.

    ``None`` means "queue with no hold". It is not a cancel.

    This test guards against a regression. The row once had the value ``None``, and
    ``session.select`` also returns ``None`` on Esc. Thus the selection of the row looked
    like a cancel, and MeshTerm did not queue the message.
    """
    ctx = _ScheduleCtx(_StubScheduleSession(lambda items: items[0].value))
    result = await _pick_schedule(ctx, "Hub")
    assert result is None
    assert result is not CANCEL_SCHEDULE
    assert WHEN_HEARD is not None  # the sentinel of the row, different from the None of Esc


async def test_pick_schedule_esc_cancels_the_queueing() -> None:
    """Esc on the picker (``select`` returns ``None``) goes back, and MeshTerm queues nothing."""
    ctx = _ScheduleCtx(_StubScheduleSession(lambda items: None))
    assert await _pick_schedule(ctx, "Hub") is CANCEL_SCHEDULE


async def test_pick_schedule_a_fixed_delay_holds_until_its_time() -> None:
    """A row with a fixed offset (for example In 1 h) returns as an aware datetime.

    The datetime is in the future.
    """
    ctx = _ScheduleCtx(_StubScheduleSession(lambda items: items[1].value))  # In 1 h
    result = await _pick_schedule(ctx, "Hub")
    assert result is not None and result is not CANCEL_SCHEDULE
    assert result > utcnow()


# --- the live outbox screen --------------------------------------------------------------


def _outbox_ctx(tmp_path: Path) -> _StubContext:
    ctx = _StubContext(tmp_path, [])
    ctx.courier = CourierService(ctx)
    return ctx


def _outbox_plain(screen, width: int = 100) -> str:
    import re

    return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in screen.render_body(width))


def test_outbox_refresh_moves_a_delivered_entry_without_a_keypress(tmp_path: Path) -> None:
    """A delivery puts its row in Finished at ``refresh()``.

    The user does not need to do anything.
    """
    from meshterm.ui.courier_screen import CourierOutboxScreen

    ctx = _outbox_ctx(tmp_path)
    entry = ctx.courier_store.queue(NODE, "Hub", "hold this")
    screen = CourierOutboxScreen(ctx)
    before = _outbox_plain(screen)
    assert "⏳ Hub" in before and "Finished" not in before

    ctx.courier_store.mark_delivered(entry.ident)
    screen.refresh()
    after = _outbox_plain(screen)
    assert "Finished" in after and re.search(r"✓ +Hub", after) and "⏳" not in after


def test_outbox_refresh_keeps_the_highlight_on_its_entry(tmp_path: Path) -> None:
    """A row that finishes above the highlight does not move the highlight off its entry."""
    from meshterm.ui.courier_screen import CourierOutboxScreen

    ctx = _outbox_ctx(tmp_path)
    first = ctx.courier_store.queue(NODE, "Hub", "first")
    second = ctx.courier_store.queue(NODE, "Hub", "second")
    screen = CourierOutboxScreen(ctx, default=("msg", second.ident))
    assert screen._current_choice().value == ("msg", second.ident)

    ctx.courier_store.mark_delivered(first.ident)
    screen.refresh()
    assert screen._current_choice().value == ("msg", second.ident)


def test_outbox_refresh_without_change_recomposes_nothing(tmp_path: Path) -> None:
    """If the shape of the store does not change, the row objects do not change.

    Thus there is no work at each tick.
    """
    from meshterm.ui.courier_screen import CourierOutboxScreen

    ctx = _outbox_ctx(tmp_path)
    ctx.courier_store.queue(NODE, "Hub", "steady")
    screen = CourierOutboxScreen(ctx)
    items = screen._items
    screen.refresh()
    assert screen._items is items


def test_outbox_rows_recompute_live_state_per_repaint(tmp_path: Path) -> None:
    """The text of the waiting row is a callable. A change of state shows at the next paint."""
    from meshterm.ui.courier_screen import CourierOutboxScreen

    ctx = _outbox_ctx(tmp_path)
    entry = ctx.courier_store.queue(NODE, "Hub", "patience")
    screen = CourierOutboxScreen(ctx)
    assert "waiting to hear the contact" in _outbox_plain(screen)

    # An attempt just happened. The same row now shows the countdown to the next attempt.
    # ``refresh()`` is not necessary, because the callable title reads the entry again at
    # each paint.
    ctx.courier_store.note_attempt(entry.ident)
    assert "try 1" in _outbox_plain(screen)


def _title_plain(choice) -> str:
    """The label of a row as plain text.

    It resolves a live (callable) title in the same way as a paint.
    """
    title = choice.title() if callable(choice.title) else choice.title
    return title.plain if isinstance(title, Text) else title


def _outbox_titles(tmp_path: Path) -> list[str]:
    """Each row of an outbox with three entries.

    The entries are one waiting entry, one delivered entry, and one entry that was given up.
    """
    from meshterm.ui.courier_screen import CourierOutboxScreen

    ctx = _outbox_ctx(tmp_path)
    ctx.courier_store.queue(NODE, "Waiting", "hold this")
    landed = ctx.courier_store.queue(NODE, "Landed", "got it")
    ctx.courier_store.mark_delivered(landed.ident)
    lost = ctx.courier_store.queue(NODE, "Lost", "never")
    ctx.courier_store.mark_gave_up(lost.ident)
    return [_title_plain(choice) for choice in CourierOutboxScreen(ctx)._choices()]


async def _entry_action_titles(tmp_path: Path) -> list[str]:
    """The labels in the action menu of a waiting entry (the menu closes with no answer)."""
    offered: list = []

    class _Session:
        async def select(self, title, items, **kwargs):
            offered.extend(items)
            return None

    store = CourierStore(tmp_path / "courier.json")
    entry = store.queue(NODE, "Hub", "hello")
    ctx = SimpleNamespace(ui=SimpleNamespace(session=_Session()), courier_store=store)
    await _entry_actions(ctx, entry.ident)
    return [_title_plain(choice) for choice in offered]


def _word_starts(rows: list[str], leads: dict[str, str]) -> set[int]:
    """The display cell where the first word of each row starts.

    The function checks the mark that starts the row.
    """
    starts = set()
    for word, mark in leads.items():
        row = next(row for row in rows if word in row)
        assert row.startswith(mark), f"{word!r} lost its mark: {row!r}"
        starts.add(cell_len(row[: row.index(word)]))
    return starts


async def test_courier_rows_start_every_label_in_the_same_cell(tmp_path: Path) -> None:
    """The two lists of the courier have one icon column, with no regard for the width of each mark.

    The outbox has marks of two cells (⏳ a waiting entry, 📨 the queue row) and marks of one
    cell (✓ ✗ a finished entry, 🗑 the clear row). It wrote each as ``mark + " "``. Thus a
    finished recipient was one column to the left of a waiting recipient, and *Clear
    finished* was one column to the left of *Queue a message…*. The actions of an entry
    had the same fault with 📤 over ✗. The measure is cells, not characters, because the
    terminal aligns cells.
    """
    outbox = {"Waiting": "⏳", "Queue": "📨", "Landed": "✓", "Lost": "✗", "Clear": "🗑"}
    actions = {"Send": "📤", "Cancel": "✗"}
    assert {cell_len(mark) for mark in outbox.values()} == {1, 2}, "a mixed list, or no proof"
    assert {cell_len(mark) for mark in actions.values()} == {1, 2}

    assert len(_word_starts(_outbox_titles(tmp_path), outbox)) == 1
    assert len(_word_starts(await _entry_action_titles(tmp_path), actions)) == 1


async def test_courier_command_rows_go_bare_where_the_platform_draws_no_icons(
    tmp_path: Path,
) -> None:
    """If there is no icon lane, the command icons go with no padding. The entry marks stay.

    The ✓, ✗, or ⏳ of an entry is its outcome, not decoration. Thus the PicoCalc keeps it,
    and it keeps those rows in line with each other. The queue, clear, send, and cancel rows
    lose their icons in the same way as each other command row there.
    """
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform

    set_platform(PICOCALC_LYRA)
    try:
        rows = _outbox_titles(tmp_path / "outbox")
        assert "Queue a message…" in rows and "Clear finished" in rows
        marks = {"Waiting": "⏳", "Landed": "✓", "Lost": "✗"}
        assert len(_word_starts(rows, marks)) == 1
        assert await _entry_action_titles(tmp_path / "actions") == [
            "Send now — one forced attempt",
            "Cancel this message",
        ]
    finally:
        set_platform(REGULAR)


# --- the recipient picker ----------------------------------------------------------------


def test_recipient_picker_rides_the_shared_node_list(tmp_path: Path) -> None:
    """The picker draws all the lanes and opens with the node that was heard last at the top.

    Enter commits.
    """
    from meshterm.ui.contactlist import SORT_COLUMNS, SORT_OPENS_ASCENDING
    from meshterm.ui.courier_screen import _PICK_HINT, CourierRecipientScreen
    from meshterm.ui.widgets import ContactsSort

    fresh = Contact(name="Fresh", public_key="aa" * 32, last_seen=utcnow())
    stale = Contact(name="Stale", public_key="bb" * 32, last_seen=utcnow() - timedelta(days=2))
    counts = {"aa" * 6: 7}
    screen = CourierRecipientScreen(
        contacts=[stale, fresh],
        prefix_bytes=1,
        counts=counts,
        sort=ContactsSort.from_name("heard", SORT_COLUMNS, SORT_OPENS_ASCENDING),
    )
    import re

    body = "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in screen.render_body(80))
    assert "NAME" in body and "HEARD" in body and "PKTS" in body and "KEY" in body
    assert body.index("Fresh") < body.index("Stale")  # heard opens with the newest first
    assert "    7" in body  # the count of packets heard from Fresh is in the PKTS lane
    assert screen.footer_hint == _PICK_HINT

    # Enter resolves the value of the highlighted row: the Contact itself.
    resolved: list = []
    screen.resolve = resolved.append  # type: ignore[method-assign]
    screen.handle("enter")
    assert resolved == [fresh]


def test_recipient_picker_sort_keys_walk_the_ring(tmp_path: Path) -> None:
    """The ^←→ keys that the Nodes list uses sort the columns of the picker again."""
    from meshterm.ui.contactlist import SORT_COLUMNS, SORT_OPENS_ASCENDING
    from meshterm.ui.courier_screen import CourierRecipientScreen
    from meshterm.ui.widgets import ContactsSort

    a = Contact(name="Alpha", public_key="aa" * 32, last_seen=utcnow())
    z = Contact(name="Zulu", public_key="bb" * 32)
    sort = ContactsSort.from_name("heard", SORT_COLUMNS, SORT_OPENS_ASCENDING)
    screen = CourierRecipientScreen(contacts=[z, a], prefix_bytes=0, counts={}, sort=sort)
    screen.handle("ctrl_left")  # heard -> name (opens ascending)
    assert sort.column == "name" and sort.ascending
    values = [c.value for c in screen._choices()]
    assert values == [a, z]
