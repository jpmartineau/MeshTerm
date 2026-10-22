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


def test_store_edits_a_waiting_body_and_deletes_any_entry(tmp_path: Path) -> None:
    """An edit changes only a waiting body. A delete removes an entry of either section.

    A finished body is what went out, so it never changes. The edit keeps the schedule and
    the attempts, because the backoff counts tries to reach the contact, not versions of
    the text.
    """
    path = tmp_path / "courier.json"
    store = CourierStore(path)
    when = utcnow() + timedelta(hours=1)
    waiting = store.queue(NODE, "Hub", "first draft", not_before=when)
    store.note_attempt(waiting.ident)
    done = store.queue(NODE, "Hub", "sent text")
    store.mark_delivered(done.ident)

    assert store.edit(waiting.ident, "second draft") is True
    again = CourierStore(path).get(waiting.ident)
    assert again.text == "second draft" and again.not_before == when and again.attempts == 1
    assert store.edit(done.ident, "rewritten") is False
    assert store.get(done.ident).text == "sent text"

    assert store.delete(done.ident) is True and store.get(done.ident) is None
    assert store.delete(waiting.ident) is True and store.entries() == []
    assert store.delete(waiting.ident) is False


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


async def test_a_held_entry_waits_while_the_user_edits_it(tmp_path: Path) -> None:
    """A pass does not try an entry that the user edits, and tries it after the edit.

    Without the hold, a pass could send the old text while the edit prompt is open.
    """
    service = _service(tmp_path, [True])
    ctx = service._ctx
    entry = ctx.courier_store.queue(NODE, "Hub", "hello")
    service._heard[NODE] = utcnow()
    with service.holding(entry.ident):
        await service._pass()
        assert ctx.chat.sent == []
    await service._pass()
    assert ctx.chat.sent == [("Hub", "hello")]


async def test_in_flight_names_the_entry_that_a_try_sends(tmp_path: Path) -> None:
    """While a try runs, ``in_flight`` is true for its entry only. After the try, it is false."""
    service = _service(tmp_path, [True])
    ctx = service._ctx
    entry = ctx.courier_store.queue(NODE, "Hub", "hello")
    seen: list[tuple[bool, bool]] = []
    send = ctx.chat.send_direct

    async def spy(contact, text, **kwargs):  # noqa: ANN001, ANN202
        seen.append((service.in_flight(entry.ident), service.in_flight(entry.ident + 1)))
        return await send(contact, text, **kwargs)

    ctx.chat.send_direct = spy
    await service.attempt_now(entry.ident)
    assert seen == [(True, False)]
    assert not service.in_flight(entry.ident)


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
    assert "when heard" in _outbox_plain(screen)

    # An attempt just happened. The same row now shows the countdown to the next attempt.
    # ``refresh()`` is not necessary, because the callable title reads the entry again at
    # each paint.
    ctx.courier_store.note_attempt(entry.ident)
    assert "try 1" in _outbox_plain(screen)


def test_outbox_draws_each_recipient_as_a_destination_chip(tmp_path: Path, powerline) -> None:
    """A queued message goes to its recipient, so the recipient opens on the notch.

    A waiting recipient has the hue of its key. A finished one is muted with the rest of its
    row, because the finished section is history.
    """
    from meshterm.ui.pathline import POWERLINE_ROUND_CLOSE, POWERLINE_SEP

    powerline(True)
    titles = _outbox_titles(tmp_path)
    for name in ("Waiting", "Landed", "Lost"):
        row = next(title for title in titles if name in title)
        assert f"{POWERLINE_SEP} {name} {POWERLINE_ROUND_CLOSE}" in row


def _title_plain(choice) -> str:
    """The label of a row as plain text.

    It resolves a live (callable) title at each read, in the same way as a paint.
    """
    title = choice.label
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


def test_courier_rows_start_every_label_in_the_same_cell(tmp_path: Path) -> None:
    """The outbox has one icon column, with no regard for the width of each mark.

    The outbox has marks of two cells (⏳ a waiting entry, 📨 the queue row) and marks of one
    cell (✓ ✗ a finished entry, 🗑 the clear row). It wrote each as ``mark + " "``. Thus a
    finished recipient was one column to the left of a waiting recipient, and *Clear
    finished* was one column to the left of *Queue a message…*. The measure is cells, not
    characters, because the terminal aligns cells.
    """
    outbox = {"Waiting": "⏳", "Queue": "📨", "Landed": "✓", "Lost": "✗", "Clear": "🗑"}
    assert {cell_len(mark) for mark in outbox.values()} == {1, 2}, "a mixed list, or no proof"

    assert len(_word_starts(_outbox_titles(tmp_path), outbox)) == 1


def test_courier_command_rows_go_bare_where_the_platform_draws_no_icons(
    tmp_path: Path,
) -> None:
    """If there is no icon lane, the command icons go with no padding. The entry marks stay.

    The ✓, ✗, or ⏳ of an entry is its outcome, not decoration. Thus the PicoCalc keeps it,
    and it keeps those rows in line with each other. The queue and clear rows lose their
    icons in the same way as each other command row there.
    """
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform

    set_platform(PICOCALC_LYRA)
    try:
        rows = _outbox_titles(tmp_path / "outbox")
        assert "Queue a message…" in rows and "Clear finished" in rows
        marks = {"Waiting": "⏳", "Landed": "✓", "Lost": "✗"}
        assert len(_word_starts(rows, marks)) == 1
    finally:
        set_platform(REGULAR)


def _cell(line: str, word: str) -> int:
    """The display cell where ``word`` starts in ``line``."""
    return cell_len(line[: line.index(word)])


def test_outbox_rows_are_target_trigger_and_message_under_column_headers(
    tmp_path: Path,
) -> None:
    """Each section is a table: TARGET, TRIGGER, and MESSAGE, or TARGET, OUTCOME, and MESSAGE.

    Each section has its own header, so the lane after TARGET has one meaning in each
    section. Each lane starts in the same cell in both sections, and each label is over its
    lane.
    """
    from meshterm.ui.courier_screen import CourierOutboxScreen

    ctx = _outbox_ctx(tmp_path)
    ctx.courier_store.queue(NODE, "Waiting", "hold this text")
    landed = ctx.courier_store.queue(NODE, "Landed Longer Name", "got it")
    ctx.courier_store.mark_delivered(landed.ident)
    lines = _outbox_plain(CourierOutboxScreen(ctx)).splitlines()

    trigger = next(line for line in lines if "TRIGGER" in line)
    outcome = next(line for line in lines if "OUTCOME" in line)
    waiting = next(line for line in lines if "Waiting" in line)
    done = next(line for line in lines if "Landed" in line)
    assert _cell(trigger, "TARGET") == _cell(waiting, "Waiting") == _cell(done, "Landed")
    assert _cell(outcome, "TARGET") == _cell(trigger, "TARGET")
    assert _cell(trigger, "TRIGGER") == _cell(waiting, "when heard")
    assert _cell(outcome, "OUTCOME") == _cell(done, "delivered") == _cell(trigger, "TRIGGER")
    assert _cell(trigger, "MESSAGE") == _cell(waiting, "hold this text")
    assert _cell(outcome, "MESSAGE") == _cell(done, "got it") == _cell(trigger, "MESSAGE")


def test_outbox_triggers_name_what_each_entry_waits_for(tmp_path: Path) -> None:
    """The TRIGGER lane says what sends the entry: a time, the end of a backoff, or a contact."""
    from meshterm.ui.courier_screen import _trigger

    ctx = _outbox_ctx(tmp_path)
    store = ctx.courier_store
    later = store.queue(NODE, "Hub", "x", not_before=utcnow() + timedelta(hours=2))
    assert _trigger(ctx, later).plain.startswith("at ")
    plain = store.queue(NODE, "Hub", "y")
    assert _trigger(ctx, plain).plain == "when heard"
    ctx.courier._heard[NODE] = utcnow()
    assert _trigger(ctx, plain).plain == "next pass — contact is fresh"
    store.note_attempt(plain.ident)
    assert _trigger(ctx, store.get(plain.ident)).plain == "retry in ~5 m · try 1"


def test_outbox_cut_ends_on_an_ellipsis_not_on_a_stray_crack(tmp_path: Path, powerline) -> None:
    """A row that is wider than the screen ends in ``…``, not in a half block of the chip fill.

    The cut lands in the TRIGGER text, after the chip. The crack is for a cut inside a
    chip. Here it once drew a half block in the fill of the TARGET chip at the right edge of
    the row, far from the chip.
    """
    from meshterm.ui.courier_screen import CourierOutboxScreen
    from meshterm.ui.pathline import CRACK_TAIL

    powerline(True)
    ctx = _outbox_ctx(tmp_path)
    ctx.courier_store.queue(NODE, "Hub", "hello")
    ctx.courier._heard[NODE] = utcnow()  # the longest trigger: "next pass — contact is fresh"
    body = _outbox_plain(CourierOutboxScreen(ctx), 40).splitlines()
    row = next(line for line in body if "⏳" in line)
    assert cell_len(row) == 40
    assert row.endswith("…") and CRACK_TAIL not in row


def test_the_outbox_pans_and_shortens_nothing(tmp_path: Path, powerline) -> None:
    """A narrow screen shows the full name, and ←→ pan the table to the full message.

    No lane gets shorter to fit. The header pans with the rows, so MESSAGE stays over the
    messages. The command rows do not pan, and the footer names ←→ while there is more.
    """
    from meshterm.ui.courier_screen import CourierOutboxScreen
    from meshterm.ui.pathline import CRACK_TAIL

    powerline(True)
    ctx = _outbox_ctx(tmp_path)
    name = "A Rather Long Contact Name For Width"
    ctx.courier_store.queue(NODE, name, "the whole message must be there to read")
    screen = CourierOutboxScreen(ctx)
    lines = _outbox_plain(screen, 53).splitlines()
    row = next(line for line in lines if "⏳" in line)
    assert name in row and CRACK_TAIL not in row
    assert row.endswith("…")  # the row goes on past the edge
    assert "←→ scroll" in screen.footer_hint

    for _ in range(20):
        screen.handle("right")
    lines = _outbox_plain(screen, 53).splitlines()
    header = next(line for line in lines if "MESSAGE" in line)
    row = next(line for line in lines if "message must" in line)
    assert row.endswith("the whole message must be there to read")
    assert _cell(header, "MESSAGE") == _cell(row, "the whole message")
    assert any(line.startswith("  📨 Queue a message…") for line in lines)


def test_outbox_keys_act_only_on_the_rows_they_name(tmp_path: Path) -> None:
    """``^R`` sends a waiting entry, Delete asks to remove an entry, and neither acts elsewhere.

    The footer and the F-key lane name each key only on the rows on which it acts.
    """
    from meshterm.ui.courier_screen import _QUEUE, CourierOutboxScreen
    from meshterm.ui.tui import DeleteRequest

    ctx = _outbox_ctx(tmp_path)
    waiting = ctx.courier_store.queue(NODE, "Waiting", "hold this")
    landed = ctx.courier_store.queue(NODE, "Landed", "got it")
    ctx.courier_store.mark_delivered(landed.ident)
    resolved: list = []

    def on(value) -> CourierOutboxScreen:  # noqa: ANN001
        screen = CourierOutboxScreen(ctx, default=value)
        screen.resolve = resolved.append  # type: ignore[method-assign]
        return screen

    screen = on(("msg", waiting.ident))
    assert screen.footer_hint == (
        "↑↓ move · type to filter · Enter open · ^R send · Del delete · Esc back"
    )
    assert cell_len(screen.footer_hint) <= 72
    slot = screen.picocalc_lyra_lane[2]
    assert (slot.label, slot.opp_label, slot.enabled, slot.opp_enabled) == (
        "Send",
        "Delete",
        True,
        True,
    )
    screen.handle("retry")
    assert resolved.pop() == ("send", waiting.ident)

    screen = on(("msg", landed.ident))
    assert "^R" not in screen.footer_hint and "Del delete" in screen.footer_hint
    assert not screen.picocalc_lyra_lane[2].enabled
    screen.handle("retry")
    assert resolved == []  # a finished entry left the outbox, so there is nothing to send
    screen.handle("delete")
    assert resolved.pop() == DeleteRequest(("msg", landed.ident))

    screen = on(_QUEUE)
    assert screen.footer_hint == "↑↓ move · type to filter · Enter open · Esc back"
    screen.handle("delete")
    screen.handle("retry")
    assert resolved == []


class _EntrySession:
    """A script for the dialogs of one entry. It records what MeshTerm asked and showed."""

    def __init__(self, answer: str | None = None, on_text=None) -> None:  # noqa: ANN001
        self.answer = answer
        self.on_text = on_text
        self.asked: list[dict] = []
        self.shown: list[tuple[str, str]] = []

    async def text(self, title, **kwargs):  # noqa: ANN001, ANN201
        self.asked.append({"title": title, **kwargs})
        if self.on_text is not None:
            await self.on_text()
        return self.answer

    async def message_dialog(self, body, *, title=""):  # noqa: ANN001, ANN201
        self.shown.append((title, body.plain if isinstance(body, Text) else str(body)))


async def test_enter_on_a_waiting_entry_edits_its_message(tmp_path: Path) -> None:
    """Enter opens the message of a waiting entry in a compose field, and Enter there saves it.

    The entry is held while the prompt is open. Thus a pass in that time sends nothing,
    and the message that goes out later is the edited one.
    """
    from meshterm.ui.courier_screen import _open_entry

    ctx = _outbox_ctx(tmp_path)
    entry = ctx.courier_store.queue(NODE, "Hub", "old text")
    ctx.courier._heard[NODE] = utcnow()
    session = _EntrySession("new text", on_text=ctx.courier._pass)
    ctx.ui = SimpleNamespace(session=session)

    await _open_entry(ctx, entry.ident)
    asked = session.asked[0]
    assert asked["title"] == "Message for Hub"
    assert asked["default"] == "old text" and asked["compose"] is True
    assert ctx.chat.sent == []  # the pass during the prompt left the held entry alone
    assert ctx.courier_store.get(entry.ident).text == "new text"
    await ctx.courier._pass()
    assert ctx.chat.sent == [("Hub", "new text")]


async def test_enter_on_a_finished_entry_shows_its_message(tmp_path: Path) -> None:
    """A finished message is history. Enter shows it with its outcome, and asks nothing."""
    from meshterm.ui.courier_screen import _open_entry

    ctx = _outbox_ctx(tmp_path)
    entry = ctx.courier_store.queue(NODE, "Hub", "what went out")
    ctx.courier_store.mark_delivered(entry.ident)
    session = _EntrySession()
    ctx.ui = SimpleNamespace(session=session)

    await _open_entry(ctx, entry.ident)
    assert session.asked == []
    title, body = session.shown[0]
    assert title == "Message for Hub"
    assert body.startswith("what went out") and "delivered" in body


async def test_delete_asks_in_red_and_removes_the_entry(tmp_path: Path) -> None:
    """Delete is a red Cancel/Delete confirm. Cancel keeps the entry, and Delete removes it."""
    from meshterm.ui.courier_screen import _delete_entry

    ctx = _outbox_ctx(tmp_path)
    entry = ctx.courier_store.queue(NODE, "Hub", "nope")
    asked: list[dict] = []
    answers = [False, True]

    async def dialog(prompt, buttons, **kwargs):  # noqa: ANN001, ANN202
        asked.append({"prompt": prompt, "buttons": buttons, **kwargs})
        return answers.pop(0)

    ctx.ui = SimpleNamespace(dialog=dialog)
    await _delete_entry(ctx, entry.ident)
    assert ctx.courier_store.get(entry.ident) is not None
    await _delete_entry(ctx, entry.ident)
    assert ctx.courier_store.get(entry.ident) is None
    assert asked[0]["destructive"] is True and asked[0]["default"] == 1
    assert [label for label, _value in asked[0]["buttons"]] == ["Cancel", "Delete"]


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


def test_a_panned_column_header_pins_with_its_section_heading(tmp_path: Path) -> None:
    """The header under a section heading pans, and it also pins with the heading.

    A header that does not pan was already the preamble of its heading. The pan branch of
    the select list once left it out, so the labels scrolled away above their rows.
    """
    from meshterm.ui.courier_screen import CourierOutboxScreen

    ctx = _outbox_ctx(tmp_path)
    ctx.courier_store.queue(NODE, "Hub", "hello")
    screen = CourierOutboxScreen(ctx)
    screen.render_body(72)
    _line, block = screen._sticky_headers[0]
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in block]
    assert "Outbox" in plain[0] and "TARGET" in plain[1]


def test_the_wide_outbox_footer_drops_delete_first_and_keeps_esc(tmp_path: Path) -> None:
    """On an entry row that pans, the full footer is wider than 72 cells. The line drops Del.

    JP chose the filter over a footer that fits (2026-10-22). The hint line removes atoms
    instead of a cut at the edge, so Esc stays. ``Del delete`` goes first, because the user
    finds the Delete key without a hint. The typed filter narrows the list again.
    """
    from meshterm.ui.courier_screen import CourierOutboxScreen
    from meshterm.ui.tui import frame

    ctx = _outbox_ctx(tmp_path)
    name = "A Rather Long Contact Name For Width"
    ctx.courier_store.queue(NODE, name, "a message that is long enough to pan")
    screen = CourierOutboxScreen(ctx)
    screen.render_body(72)
    assert cell_len(screen.footer_hint) > 72
    assert frame.fitted_footer(screen, 72) == (
        "↑↓ move · ←→ scroll · type to filter · Enter open · ^R send · Esc back"
    )

    for ch in "zzz":
        screen.handle("text", ch)
    assert name not in _outbox_plain(screen)
    for _ in range(3):
        screen.handle("backspace")
    for ch in "Rather":
        screen.handle("text", ch)
    assert "Rather" in _outbox_plain(screen)
