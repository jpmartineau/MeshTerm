# SPDX-License-Identifier: Apache-2.0
"""The Courier screens: the outbox, queueing flow, and per-message actions.

The interactive face of the ``courier`` tool. One select-list screen holds the whole
feature (the persistent-backdrop pattern). It has these parts:

- The waiting outbox. Each entry shows its live state: it waits to hear the contact, it is
  scheduled for a time, or it backs off between retries.
- The finished history (delivered or given up).
- The queueing flow: select a contact, write the message, choose when to send it.

Enter on a waiting entry offers *Send now* (one forced attempt, and the outcome is in a
dialog) and *Cancel*.

The outbox is **live** while it is open. Each row is a callable title that the code
computes again at each paint, so "retry in ~N m" counts down in real time. A ticker
compares the shape of the store (the set of entries and their statuses) once each second.
When something changed, the ticker builds the sections again in place, and the highlight
stays on its entry. For example, a delivery puts its row in *Finished* within one second,
with no key press.

The background service (:mod:`meshterm.services.courier`) does the delivery. The menu
starts this service with the other services that are always on. Thus this screen does not
need to stay open for a queued message to go out.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from rich.cells import cell_len
from rich.text import Text

from ..core.courier_store import DELIVERED, QUEUED, QueuedMessage
from ..core.models import Contact, is_direct_messageable, utcnow
from .contactlist import SORT_COLUMNS, SORT_OPENS_ASCENDING, ContactListScreen, ContactRow
from .menus import icon_lane, marked_label, run_steps, section_heading
from .tui import CANCEL, DM_BYTE_LIMIT, Choice, SelectScreen, Separator
from .watchtower_screen import contact_watch_key
from .widgets import ContactsSort, age_seconds, contact_packets, format_ago

if TYPE_CHECKING:
    from ..context import AppContext

# The sentinels of the menu actions (tuples, so that they never collide with entry ids).
_QUEUE = ("queue",)
_CLEAR = ("clear",)

#: The status marks that an outbox entry starts with: waiting, delivered, gave up. They are
#: the outcome of the row and not decoration. Thus, unlike the icon of a command row, they
#: are drawn on each platform. The code measures them raw, because the render fold of the
#: PicoCalc pads a folded mark again to the width that the emoji measured.
_ENTRY_MARKS = ("⏳", "✓", "✗")

#: The width of the widest entry mark in cells. This is the part of the icon column of the
#: outbox that no platform removes.
_MARK_LANE = max(cell_len(mark) for mark in _ENTRY_MARKS)

#: The decorative command icons of the outbox: queue a message, and clear the finished
#: history.
_COMMAND_ICONS = ("📨", "🗑")

#: The seconds between the refresh ticks of the open outbox (shape check and paint).
_REFRESH_S = 1.0

#: The maximum width at which a message body renders in a row, before the row gets an
#: ellipsis.
_TEXT_W = 36


def _shorten(text: str, width: int = _TEXT_W) -> str:
    """The message body, shortened for the display in a row."""
    return text if len(text) <= width else text[: width - 1] + "…"


def _local_stamp(when: datetime) -> str:
    """A compact local timestamp: ``Jul 12 07:00``."""
    return when.astimezone().strftime("%b %d %H:%M")


def parse_clock(text: str, now: datetime | None = None) -> datetime | None:
    """Parse a local ``HH:MM`` into its *next* occurrence, as aware UTC.

    If the user types ``07:00`` at 23:40, it means tomorrow morning. If the user types it at
    06:00, it means one hour from now. The function returns ``None`` for text that is not a
    plausible clock time.

    Args:
        text: The typed time.
        now: The reference time (the default is the current time, and tests give a value).

    Returns:
        The next occurrence as an aware UTC datetime, or ``None``.
    """
    match = re.fullmatch(r"\s*(\d{1,2})[:h](\d{2})\s*", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    now = now or utcnow()
    local = now.astimezone()
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


async def open_courier(ctx: AppContext) -> dict[str, Any] | None:
    """Run the Courier screen until the user leaves it.

    Args:
        ctx: The shared application context (the interactive TUI must run).

    Returns:
        A summary of what happened (for the log of the tool), or ``None`` when the user
        leaves with no action.

    Raises:
        RuntimeError: If the caller is outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the courier is only available in the menu")
    session = ctx.ui.session
    await ctx.courier.start()  # idempotent. Normally it already runs.

    contacts: list[Contact] = []
    try:
        if ctx.is_connected or ctx.settings.connect_on_start:
            contacts = await ctx.devstate.contacts()
    except Exception:  # noqa: BLE001 - the outbox renders without contacts without a problem
        contacts = []

    store = ctx.courier_store
    # One screen for the whole visit, and it refreshes in place. The outbox already knows
    # how to compose its sections again around the highlight
    # (:meth:`CourierOutboxScreen.refresh`). The ticker calls this method once each second
    # while the list is open. The code runs the screen through a visit. Thus each sub-flow
    # (queue a message, the actions of an entry, the clear confirm) floats over the list and
    # returns to the row from which it opened. Also, the code starts the ticker one time,
    # and not for each round.
    menu = CourierOutboxScreen(ctx)

    async def tick() -> None:
        """Take in the store changes and paint, once each second, while the list is open."""
        while True:
            await asyncio.sleep(_REFRESH_S)
            menu.refresh()
            session.invalidate()

    async with session.stay(menu) as visit:
        ticker = asyncio.ensure_future(tick())
        try:
            while True:
                choice = await visit.result()
                if choice in (None, CANCEL):
                    return {"queued": store.pending_count()}
                if choice == _QUEUE:
                    await _queue_flow(ctx, contacts)
                elif choice == _CLEAR:
                    done = store.done_count()
                    if await ctx.ui.dialog(
                        f"Clear {done} finished "
                        f"{'entry' if done == 1 else 'entries'} from the history?",
                        [("Cancel", False), ("Clear", True)],
                        title="Clear finished",
                        default=1,
                        destructive=True,
                    ):
                        store.clear_done()
                elif isinstance(choice, tuple) and choice[0] == "msg":
                    await _entry_actions(ctx, int(choice[1]))
                # Take in the effect of the flow at once, and do not wait for a tick. Thus
                # the list to which the user returns already shows what the user did.
                menu.refresh()
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - teardown must never show a fault of the tick
                pass


# --- the menu ---------------------------------------------------------------------------


class CourierOutboxScreen(SelectScreen):
    """The live outbox list: the row text is computed at each paint, the sections at each tick.

    The rows are callable titles (refer to :attr:`~meshterm.ui.tui.select.Choice.title`).
    Thus each paint reads the live state of each entry again: the retry countdown, the note
    that the contact is fresh or that the entry waits, and the age of a finished row. A row
    cannot show a change of structure by a new render of itself. Examples are an entry that
    moves from waiting to finished, a new queue, and a cleared history. Thus the ticker of
    the opener calls :meth:`refresh`. This method makes a fingerprint of the shape of the
    store. It composes the sections again in place only when the fingerprint changed, and
    the highlight stays on its entry (the rebuild idiom of
    :class:`~meshterm.ui.contactlist.ContactListScreen`).
    """

    def __init__(self, ctx: AppContext, *, default: Any = None) -> None:
        """Open the outbox on the current entries of the store, and keep their shape.

        :meth:`refresh` compares against the kept shape. Thus a tick that changed nothing
        leaves the highlight in the place where the user put it.
        """
        self._ctx = ctx
        self._shape = self._fingerprint()
        super().__init__(
            "Courier — store-and-forward outbox",
            _menu_items(ctx, ctx.courier_store.entries()),
            default=default,
            footer_hint="↑↓ move · Enter select · Esc back",
        )

    def _fingerprint(self) -> tuple:
        """The shape of the store: which entries exist and the status of each."""
        return tuple((m.ident, m.status) for m in self._ctx.courier_store.entries())

    def refresh(self) -> None:
        """Compose the sections again if the shape of the store changed. Keep the highlight."""
        shape = self._fingerprint()
        if shape == self._shape:
            return
        self._shape = shape
        current = self._current_choice()
        keep = current.value if current is not None else None
        self._items = _menu_items(self._ctx, self._ctx.courier_store.entries())
        self._reselect(keep)

    def _reselect(self, value: Any) -> None:
        """Move the highlight back onto the choice with ``value``. If none has it, clamp."""
        choices = self._choices()
        for i, choice in enumerate(choices):
            if choice.value == value:
                self._index = i
                return
        self._index = max(0, min(self._index, len(choices) - 1)) if choices else 0


def _menu_items(ctx: AppContext, entries: list[QueuedMessage]) -> list:
    """Build the rows of the screen: the waiting outbox, then the finished history.

    The entry rows are callables with no argument, so that their live state renders again at
    each paint. The fixed action rows stay plain strings.
    """
    waiting = [m for m in entries if m.status == QUEUED]
    done = [m for m in entries if m.status != QUEUED]

    # One icon column for the whole list, across both sections. The terminal draws ⏳ and
    # 📨 in two cells, but ✓ ✗ 🗑 in one cell. Thus rows that were written as
    # ``mark + " "`` started the recipient of a finished entry one column to the left of the
    # recipient of a waiting entry. They also started *Clear finished* one column to the left
    # of *Queue a message…*. The entry marks always keep their share of the column. The
    # command icons go where the platform draws no icon lane, and marked_label leaves those
    # two labels bare. It does not pad them for a mark that is not there.
    lane = max(icon_lane(_COMMAND_ICONS), _MARK_LANE)

    items: list = [section_heading("Outbox")]
    if not waiting:
        items.append(Separator("  empty — queued messages wait here for their moment"))
    for message in waiting:
        items.append(Choice(lambda m=message: _waiting_row(ctx, m, lane), ("msg", message.ident)))
    items.append(Separator(" "))  # put space between the action and the outbox rows above it
    items.append(Choice(marked_label("📨", "Queue a message…", "", lane=lane), _QUEUE))

    if done:
        items.append(Separator(" "))
        items.append(section_heading("Finished"))
        for message in done[:15]:
            items.append(Choice(lambda m=message: _done_row(m, lane), ("msg", message.ident)))
        items.append(Choice(marked_label("🗑", "Clear finished", "", lane=lane), _CLEAR))

    return items


def _entry_mark(mark: str, style: str, lane: int) -> Text:
    """The status mark of an entry, with a tint, and padded to the icon column of the outbox.

    This is the twin of :func:`~meshterm.ui.menus.icon_mark` for entry rows. That function
    cannot serve here. It removes its icon where the platform draws no icon lane. The mark
    of an entry is its outcome (waiting, delivered, gave up), and each platform must show it.

    Args:
        mark: One of :data:`_ENTRY_MARKS`.
        style: The theme style in which the mark is drawn.
        lane: The icon column of the list in cells (refer to :func:`_menu_items`).

    Returns:
        The mark, followed by enough spaces to start the words in the column after it.
    """
    text = Text(mark, style=style)
    text.append(" " * (lane - cell_len(mark) + 1))
    return text


def _waiting_row(ctx: AppContext, message: QueuedMessage, lane: int = _MARK_LANE) -> Text:
    """One waiting entry: the recipient, the body, and what the entry waits for."""
    row = _entry_mark("⏳", "warn", lane)
    row.append(message.node_name)
    row.append(f"  “{_shorten(message.text)}”", style="muted")
    row.append("  ·  ", style="muted")
    now = utcnow()
    if message.not_before is not None and now < message.not_before:
        row.append(f"scheduled {_local_stamp(message.not_before)}", style="brand")
    else:
        retry = ctx.courier.next_retry_s(message, now)
        if retry is not None:
            row.append(
                f"try {message.attempts} · retry in ~{max(1, round(retry / 60))} m",
                style="warn",
            )
        elif message.attempts > 0:
            row.append(f"try {message.attempts} · waiting to hear it again", style="muted")
        elif ctx.courier.heard_recently(message.node_key, now):
            row.append("contact is fresh — next pass", style="ok")
        else:
            row.append("waiting to hear the contact", style="muted")
    return row


def _done_row(message: QueuedMessage, lane: int = _MARK_LANE) -> Text:
    """One finished entry: the outcome mark, the recipient, the body, and when it ended."""
    if message.status == DELIVERED:
        row = _entry_mark("✓", "ok", lane)
    else:
        row = _entry_mark("✗", "err", lane)
    row.append(message.node_name, style="muted")
    row.append(f"  “{_shorten(message.text)}”", style="muted")
    when = message.finished or message.created
    verb = "delivered" if message.status == DELIVERED else "gave up"
    row.append(
        f"  ·  {verb} {format_ago(age_seconds(when))}"
        f" · {message.attempts} attempt{'s' if message.attempts != 1 else ''}",
        style="muted",
    )
    return row


# --- the flows --------------------------------------------------------------------------


#: The footer of the recipient picker. It uses the grammar of the shared contact list, with
#: an Enter that commits. Esc cancels the queueing step in which the picker is.
_PICK_HINT = "↑↓ move · ^←→↑↓ sort · type to filter · Enter select · Esc cancel"


class CourierRecipientScreen(ContactListScreen):
    """The recipient picker on the shared contact list.

    It has the full ``NAME · HEARD · PKTS · KEY`` lanes, the Ctrl+arrow sort ring, and
    type-to-filter. These are the same as in the Contacts screen and the Time Machine
    picker. Names have their hue from their key, and heard ages have the recency heat.
    Unlike the Contacts screen, Enter *commits*. The Enter of the shared list resolves the
    value of the highlighted row, which is the :class:`~meshterm.core.models.Contact`
    itself.

    The caller passes only companion contacts. A courier message is a direct message, and
    direct messages go only to companions (refer to
    :func:`~meshterm.core.models.is_direct_messageable`). The caller filters the contacts
    before it builds the picker.
    """

    def __init__(
        self,
        *,
        contacts: list[Contact],
        prefix_bytes: int,
        counts: dict[str, int],
        sort: ContactsSort,
    ) -> None:
        """Build the picker over the companion contacts of the device.

        Args:
            contacts: The candidate recipients: companion contacts only (the caller
                removes the contacts that are not companions).
            prefix_bytes: The hash width in bytes to light at the start of each key.
            counts: The tallies of overheard packets, with the lowercase 12-hex node id as
                the key.
            sort: The sort state (by default it opens on ``name``, A→Z).
        """
        rows = [
            ContactRow(
                value=c,
                name=c.name,
                key=c.public_key or c.key_prefix or "",
                node_type=c.node_type,
                last_seen=c.last_seen,
                count=contact_packets(c, counts),
            )
            for c in contacts
        ]
        super().__init__(
            "Courier — recipient",
            rows=rows,
            prefix_bytes=prefix_bytes,
            sort=sort,
            prompt="The message waits in the outbox until this contact can take it:",
            footer_hint=_PICK_HINT,
        )


async def _queue_flow(ctx: AppContext, contacts: list[Contact]) -> None:
    """Float the queueing flow (recipient, message, schedule) as a stack.

    The flow has three prompts. Thus Esc means "go back one step" and does not mean "throw
    away all of it". Esc goes from the schedule to the message, and the text that the user
    wrote is still in the field. It goes from the message to the recipient list. The list
    stays pushed all the time, so it is still on the row for which the user wrote the
    message. From the list, Esc goes out to the outbox. One key press does not lose
    anything that the user typed (refer to :func:`~meshterm.ui.menus.run_steps`).
    """
    session = ctx.ui.session
    # A courier message is a direct message, so only companions can receive one. A
    # repeater, a room, or a sensor is never a recipient (the DM rule of the whole app, refer
    # to is_direct_messageable). Filter before the picker, so that nodes that are not
    # companions never appear.
    companions = [c for c in contacts if is_direct_messageable(c.node_type)]
    if not companions:
        await session.message_dialog(
            Text(
                "No companion contacts available — connect a device that knows a "
                "companion to message first.",
                style="muted",
            ),
            title="Queue a message",
        )
        return

    # The shared contact-list presentation (refer to CourierRecipientScreen). It opens A→Z by
    # name, which is the default of a list that the user can sort again. The heard, packets,
    # and key sorts are one Ctrl+arrow away.
    counts = {n.node: n.count for n in ctx.repo.heard_nodes() if n.node}
    prefix_bytes = await ctx.devstate.routing_prefix_bytes()
    picker = CourierRecipientScreen(
        contacts=companions,
        prefix_bytes=prefix_bytes,
        counts=counts,
        sort=ContactsSort.from_name("name", SORT_COLUMNS, SORT_OPENS_ASCENDING),
    )
    async with session.stay(picker) as visit:
        answers = await run_steps(
            [
                lambda vals: _next_recipient(visit),
                lambda vals: session.text(
                    f"Message for {vals[0].name}",
                    prompt="Delivered as a normal direct message when its moment comes.",
                    byte_limit=DM_BYTE_LIMIT,
                    default=vals[1] or "",
                ),
                lambda vals: _schedule_step(ctx, vals[0].name),
            ]
        )
    if answers is None:  # Esc on the recipient list: out to the outbox
        return
    contact, text, when = answers
    not_before = None if when is WHEN_HEARD else when
    key = contact_watch_key(contact)
    if key is None:
        await session.message_dialog(
            Text(f"{contact.name!r} has no usable key to address.", style="err"),
            title="Queue a message",
        )
        return
    ctx.courier_store.queue(key, contact.name, text, not_before=not_before)


async def _next_recipient(visit: Any) -> Contact | None:
    """One round of the visited recipient list: the selected contact, or ``None`` on Esc."""
    chosen = await visit.result()
    return chosen if isinstance(chosen, Contact) else None


async def _schedule_step(ctx: AppContext, name: str) -> object | None:
    """The schedule step, in the shape that :func:`~meshterm.ui.menus.run_steps` reads.

    A step of the chain uses ``None`` as the signal to *step back*. But :func:`_pick_schedule`
    also returns ``None`` for the real answer "no schedule, send on the next sign of life".
    Thus this function swaps the two. That answer goes on as :data:`WHEN_HEARD` (the value
    of its own row), and the cancel becomes the ``None``.
    """
    picked = await _pick_schedule(ctx, name)
    if picked is CANCEL_SCHEDULE:
        return None
    return WHEN_HEARD if picked is None else picked


#: The sentinel for a schedule picker that the user cancelled (this is not "no schedule").
CANCEL_SCHEDULE = object()

#: The sentinel for "send when it is next heard", the choice for no schedule. It has its own
#: value (never ``None``), because :meth:`session.select` already returns ``None`` for a
#: cancel. When the two choices shared that value, a user who selected this row caused a
#: cancel, and the code dropped the message without a word and did not queue it.
WHEN_HEARD = object()


async def _pick_schedule(ctx: AppContext, name: str):
    """Float the picker for when to send. Return an aware UTC time, ``None``, or a cancel.

    ``None`` means "no schedule, send on the next sign of life" (the explicit
    ``When it's next heard`` row). An aware UTC datetime means that the message waits until
    that time. :data:`CANCEL_SCHEDULE` means that the user went back, and the code must not
    queue anything.

    The *At a time…* row opens a second prompt. Esc there goes back to these choices and
    not out of the queueing flow. This is the chain rule, one level lower (refer to
    :func:`~meshterm.ui.menus.run_steps`).
    """
    session = ctx.ui.session
    while True:
        now = utcnow()
        tomorrow_7 = parse_clock("07:00", now)
        items = [
            Choice("When it's next heard  (recommended)", WHEN_HEARD),
            Choice("In 1 h", now + timedelta(hours=1)),
            Choice("In 3 h", now + timedelta(hours=3)),
            Choice("In 8 h", now + timedelta(hours=8)),
            Choice(f"Next 07:00  ({_local_stamp(tomorrow_7)})", tomorrow_7),
            Choice("At a time… (HH:MM, next occurrence)", "custom"),
        ]
        picked = await session.select(
            f"When should {name} get it?",
            items,
            filterable=False,
            footer_hint="↑↓ move · Enter select · Esc cancel",
        )
        if picked is None:
            # Esc on the picker steps back out of the schedule. "When next heard" is its
            # own explicit row (WHEN_HEARD), so going back never queues anything without
            # a word.
            return CANCEL_SCHEDULE
        if picked is WHEN_HEARD:
            return None  # no schedule limit: the courier delivers on the next pass
        if picked != "custom":
            return picked
        while True:
            typed = await session.text(
                "Send at (local HH:MM)",
                prompt="A time already past today means tomorrow.",
            )
            if not typed:
                break  # Esc on the time: back to the choices from which the user came
            when = parse_clock(typed)
            if when is not None:
                return when


async def _entry_actions(ctx: AppContext, ident: int) -> None:
    """Float the action menu of one entry: send now, cancel, or only look at it."""
    session = ctx.ui.session
    store = ctx.courier_store
    message = store.get(ident)
    if message is None:
        return
    if message.status != QUEUED:
        # A finished entry has no actions. Show its full text instead.
        body = Text(message.text)
        body.append(
            f"\n\n{message.status} · {message.attempts} attempt"
            f"{'s' if message.attempts != 1 else ''}",
            style="muted",
        )
        await session.message_dialog(body, title=message.node_name)
        return
    # One measured column for both rows. 📤 draws two cells and ✗ draws one cell. The Cancel
    # row measured only its own mark, so its words started one column to the left of the
    # words of Send now. Where the platform draws no icon lane, both rows have no icon, and
    # the err tint of Cancel moves onto its words.
    lane = icon_lane(("📤", "✗"))
    items = [
        Choice(marked_label("📤", "Send now — one forced attempt", "", lane=lane), "send"),
        Choice(marked_label("✗", "Cancel this message", "err", lane=lane), "cancel"),
    ]
    picked = await session.select(
        f"{message.node_name} — “{_shorten(message.text, 28)}”",
        items,
        filterable=False,
        footer_hint="↑↓ move · Enter select · Esc back",
    )
    if picked == "cancel":
        store.cancel(ident)
    elif picked == "send":
        try:
            async with ctx.ui.busy_overlay(f"sending to {message.node_name}…"):
                outcome = await ctx.courier.attempt_now(ident)
        except Exception as exc:  # noqa: BLE001 - show the failure, and keep the queue
            await session.message_dialog(Text(f"send failed: {exc}", style="err"), title="Courier")
            return
        notes = {
            "delivered": Text("✓ delivered — acknowledged by the contact", style="ok"),
            "no ack": Text(
                "sent, but no acknowledgement — it stays queued and the courier "
                "will retry with backoff",
                style="warn",
            ),
            "gave up": Text("no acknowledgement — the retry budget is spent", style="err"),
            "unknown contact": Text(
                "the device's contact list doesn't know this contact yet; it stays queued",
                style="warn",
            ),
            "busy": Text("another delivery is in flight — try again in a moment", style="muted"),
            "gone": Text("this entry is no longer queued", style="muted"),
        }
        await session.message_dialog(notes.get(outcome, Text(outcome)), title="Courier")
