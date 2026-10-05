# SPDX-License-Identifier: Apache-2.0
"""The Courier: store-and-forward delivery for the outbox.

A session-long background loop over the :class:`~meshterm.core.courier_store.
CourierStore`. Messages queue for contacts that aren't reachable right now; the courier
listens to the hub for signs of life and delivers when the recipient is *fresh* —
heard within the last few minutes — or when a scheduled entry's time arrives. Each
attempt is one ordinary chat send (:meth:`~meshterm.services.chat_service.ChatService.
send_direct`, so it lands in the conversation history with its delivery state, exactly
as if typed), and an acknowledgement settles the entry as delivered.

Deliberately transmission-shy, like the advert scheduler:

* At most **one** delivery attempt per pass, so a backlog drains politely instead of
  bursting onto a shared mesh.
* Failed attempts back off exponentially (5 min doubling to an hour) and — after the
  first shot — wait until the contact has been heard *again*, so an absent contact is never
  hammered on faith alone. Only a scheduled entry's first attempt fires blind: the
  schedule was an explicit instruction.
* After :data:`MAX_ATTEMPTS` unacknowledged tries the courier gives up and says so.

Outcomes (delivered, given-up) are also raised as Watchtower alerts, so the header's
``▲`` badge lights when an overnight delivery finally lands.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING

from ..core.connection import Unsubscribe
from ..core.courier_store import DELIVERED, QUEUED, QueuedMessage
from ..core.events import EventKind, MeshEvent
from ..core.models import Contact, utcnow

if TYPE_CHECKING:
    from ..context import AppContext

#: Seconds between courier passes.
POLL_S = 30.0

#: How recently (seconds) a node must have been heard to count as reachable.
FRESH_S = 600.0

#: Delivery attempts before the courier gives up on an entry.
MAX_ATTEMPTS = 5

#: Backoff after a failed attempt: this base doubling per failure, capped below.
BACKOFF_BASE_S = 300.0
BACKOFF_CAP_S = 3600.0


def _preview(text: str, width: int = 32) -> str:
    """The message body shortened for alert/log one-liners."""
    return text if len(text) <= width else text[: width - 1] + "…"


class CourierService:
    """Owns the store-and-forward loop for an interactive session.

    Interact through the async lifecycle methods (:meth:`start`, :meth:`stop`,
    :meth:`aclose`) and :meth:`attempt_now`; eligibility lives in the synchronous
    :meth:`eligible` so tests can drive it directly.
    """

    def __init__(self, ctx: AppContext) -> None:
        """Initialize the (idle) service.

        Args:
            ctx: The shared application context (store, chat service, event hub).
        """
        self._ctx = ctx
        self._task: asyncio.Task | None = None
        self._unsubscribe: Unsubscribe | None = None
        #: When each node id was last overheard this session (the freshness map).
        self._heard: dict[str, datetime] = {}
        #: Re-entry guard: one attempt in flight at a time, pass or forced.
        self._sending = False

    @property
    def active(self) -> bool:
        """Whether the courier loop is running."""
        return self._task is not None and not self._task.done()

    def pending_count(self) -> int:
        """How many messages wait in the outbox."""
        return self._ctx.courier_store.pending_count()

    # --- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        """Start the loop and the freshness subscription. Idempotent, device-free."""
        if self.active:
            return

        def on_event(event: MeshEvent) -> None:
            obs = event.observation
            if obs is not None and obs.node:
                self._heard[obs.node] = obs.observed_at or utcnow()

        self._unsubscribe = self._ctx.events.subscribe(on_event, EventKind.OBSERVATION)
        self._task = asyncio.ensure_future(self._run())
        self._ctx.log.info("courier started (%d queued)", self._ctx.courier_store.pending_count())

    async def stop(self) -> None:
        """Stop the loop and the subscription. Idempotent."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def aclose(self) -> None:
        """Stop the loop at session end (an alias for :meth:`stop`)."""
        await self.stop()

    async def _run(self) -> None:
        """Tick forever: sleep, then make one best-effort pass."""
        while True:
            await asyncio.sleep(POLL_S)
            try:
                await self._pass()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad pass must not kill the loop
                self._ctx.log.debug("courier: pass failed: %s", exc)

    # --- eligibility ---------------------------------------------------------------------

    def heard_recently(self, node_key: str, now: datetime | None = None) -> bool:
        """Whether ``node_key`` was overheard within the freshness window."""
        heard = self._heard.get(node_key)
        if heard is None:
            return False
        return ((now or utcnow()) - heard).total_seconds() <= FRESH_S

    def eligible(self, message: QueuedMessage, now: datetime | None = None) -> bool:
        """Whether one queued entry may be attempted right now.

        The rules, in order: only queued entries; a schedule holds until its time;
        failed attempts wait out an exponential backoff; and — except for a scheduled
        entry's first, explicitly-timed shot — the recipient must have been heard
        within the freshness window.

        Args:
            message: The outbox entry.
            now: The evaluation time (defaults to the current time; tests inject).

        Returns:
            ``True`` when an attempt is allowed.
        """
        now = now or utcnow()
        if message.status != QUEUED:
            return False
        if message.not_before is not None and now < message.not_before:
            return False
        if message.last_attempt is not None and message.attempts > 0:
            backoff = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (message.attempts - 1))
            if (now - message.last_attempt).total_seconds() < backoff:
                return False
        scheduled_first_shot = message.not_before is not None and message.attempts == 0
        if not scheduled_first_shot and not self.heard_recently(message.node_key, now):
            return False
        return True

    def next_retry_s(self, message: QueuedMessage, now: datetime | None = None) -> float | None:
        """Seconds until the backoff releases a failed entry, or ``None`` if free now."""
        if message.last_attempt is None or message.attempts == 0:
            return None
        backoff = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (message.attempts - 1))
        remaining = backoff - ((now or utcnow()) - message.last_attempt).total_seconds()
        return remaining if remaining > 0 else None

    # --- delivery ------------------------------------------------------------------------

    async def _pass(self) -> None:
        """One pass: attempt the oldest eligible entry, if any (one per pass)."""
        if not self._ctx.is_connected or self._sending:
            return
        now = utcnow()
        for message in self._ctx.courier_store.pending():
            if self.eligible(message, now):
                await self._attempt(message)
                return

    async def attempt_now(self, ident: int) -> str:
        """Force one delivery attempt for the screen's *Send now*, skipping eligibility.

        Args:
            ident: The outbox entry's id.

        Returns:
            A short outcome string: ``delivered``, ``no ack``, ``gave up``,
            ``unknown contact``, ``busy``, or ``gone``.
        """
        message = self._ctx.courier_store.get(ident)
        if message is None or message.status != QUEUED:
            return "gone"
        if self._sending:
            return "busy"
        return await self._attempt(message)

    async def _attempt(self, message: QueuedMessage) -> str:
        """Run one delivery attempt and settle the entry's state."""
        store = self._ctx.courier_store
        self._sending = True
        try:
            contact = await self._resolve(message)
            if contact is None:
                # Not addressable (yet): the device doesn't know the contact. Leave the
                # entry waiting — no budget spent on a send that can't happen.
                self._ctx.log.debug("courier: no contact for %r; leaving queued", message.node_name)
                return "unknown contact"
            store.note_attempt(message.ident)
            chat = await self._ctx.chat.send_direct(
                contact,
                message.text,
                on_late_ack=lambda _chat, ident=message.ident: self._delivered_late(ident),
            )
            if chat.acked:
                store.mark_delivered(message.ident)
                self._ctx.watch_store.add_alert(
                    "courier",
                    message.node_name,
                    f"delivered “{_preview(message.text)}” (attempt {message.attempts})",
                )
                self._ctx.log.info("courier: delivered to %s", message.node_name)
                return "delivered"
            if message.attempts >= MAX_ATTEMPTS:
                store.mark_gave_up(message.ident)
                self._ctx.watch_store.add_alert(
                    "courier",
                    message.node_name,
                    f"gave up on “{_preview(message.text)}” after {message.attempts} attempts",
                )
                return "gave up"
            self._ctx.log.debug(
                "courier: no ack from %s (attempt %d/%d)",
                message.node_name,
                message.attempts,
                MAX_ATTEMPTS,
            )
            return "no ack"
        finally:
            self._sending = False

    def _delivered_late(self, ident: int) -> None:
        """An attempt's ack arrived after the attempt stopped waiting: it was delivered.

        Without this the entry stayed queued (or gave up) and the next attempt sent the
        recipient the same message again — every late ack a duplicate on their screen.
        """
        store = self._ctx.courier_store
        message = store.get(ident)
        if message is None or message.status == DELIVERED:
            return
        store.mark_delivered(ident)
        self._ctx.watch_store.add_alert(
            "courier",
            message.node_name,
            f"delivered “{_preview(message.text)}” (attempt {message.attempts}, ack came late)",
        )
        self._ctx.log.info("courier: delivered to %s (late ack)", message.node_name)

    async def _resolve(self, message: QueuedMessage) -> Contact | None:
        """Find the recipient in the device's contact list, by key then by name."""
        device = await self._ctx.device()
        contacts = await device.get_contacts()
        for contact in contacts:
            ident = (contact.public_key or contact.key_prefix or "").lower()
            ident = ident.removeprefix("0x")[:12]
            if ident and ident == message.node_key:
                return contact
        needle = message.node_name.casefold()
        return next((c for c in contacts if c.name.casefold() == needle), None)
