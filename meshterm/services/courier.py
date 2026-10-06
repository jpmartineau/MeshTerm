# SPDX-License-Identifier: Apache-2.0
"""The Courier: store-and-forward delivery for the outbox.

A background loop on the :class:`~meshterm.core.courier_store.CourierStore`, which runs
for the full session. Messages wait in the queue for contacts that MeshTerm cannot reach
now. The courier listens to the hub for signs of life. It delivers when the recipient is
*fresh* (heard in the last few minutes), or when the time of a scheduled entry arrives.
Each try is one usual chat send
(:meth:`~meshterm.services.chat_service.ChatService.send_direct`). Thus the message goes
into the conversation history with its delivery state, exactly as if the user typed it.
An acknowledgement marks the entry as delivered.

On purpose, the courier transmits as little as possible, as the advert scheduler does:

* A maximum of **one** delivery try in each pass. Thus a backlog empties slowly, and it
  does not go onto a shared mesh in a burst.
* After a failed try, the wait increases exponentially (5 min, doubled up to one hour).
  After the first try, the courier also waits until it hears the contact again. Thus it
  never sends many tries to an absent contact without evidence that the contact is
  there. Only the first try of a scheduled entry goes without that evidence, because the
  schedule was an explicit instruction.
* After :data:`MAX_ATTEMPTS` tries with no acknowledgement, the courier stops and tells
  the user.

The results (delivered, given up) also become Watchtower alerts. Thus the ``▲`` badge in
the header comes on when a delivery during the night is at last complete.
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

#: The maximum time (seconds) since MeshTerm heard a node, for the node to count as
#: reachable.
FRESH_S = 600.0

#: The number of delivery tries before the courier stops its tries for an entry.
MAX_ATTEMPTS = 5

#: The backoff after a failed try: this base, doubled for each failure, with the limit
#: below.
BACKOFF_BASE_S = 300.0
BACKOFF_CAP_S = 3600.0


def _preview(text: str, width: int = 32) -> str:
    """The message body, shortened for one-line alerts and log lines."""
    return text if len(text) <= width else text[: width - 1] + "…"


class CourierService:
    """Owns the store-and-forward loop for an interactive session.

    Use the async lifecycle methods (:meth:`start`, :meth:`stop`, :meth:`aclose`) and
    :meth:`attempt_now`. The eligibility rules are in the synchronous :meth:`eligible`, so
    that tests can call them directly.
    """

    def __init__(self, ctx: AppContext) -> None:
        """Initialize the (idle) service.

        Args:
            ctx: The shared application context (store, chat service, event hub).
        """
        self._ctx = ctx
        self._task: asyncio.Task | None = None
        self._unsubscribe: Unsubscribe | None = None
        #: The time when MeshTerm last heard each node id in this session (the freshness
        #: map).
        self._heard: dict[str, datetime] = {}
        #: Re-entry guard: only one try at a time is in progress, from a pass or forced.
        self._sending = False

    @property
    def active(self) -> bool:
        """Whether the courier loop runs."""
        return self._task is not None and not self._task.done()

    def pending_count(self) -> int:
        """How many messages wait in the outbox."""
        return self._ctx.courier_store.pending_count()

    # --- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        """Start the loop and the freshness subscription. Idempotent, with no device use."""
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
        """Stop the loop at the end of the session (an alias for :meth:`stop`)."""
        await self.stop()

    async def _run(self) -> None:
        """Loop with no end: sleep, then do one best-effort pass."""
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
        """Whether MeshTerm heard ``node_key`` in the freshness window."""
        heard = self._heard.get(node_key)
        if heard is None:
            return False
        return ((now or utcnow()) - heard).total_seconds() <= FRESH_S

    def eligible(self, message: QueuedMessage, now: datetime | None = None) -> bool:
        """Whether the courier can try one queued entry now.

        The rules, in order:

        1. Only queued entries.
        2. A schedule holds the entry until its time.
        3. After failed tries, the entry waits for the end of an exponential backoff.
        4. MeshTerm must have heard the recipient in the freshness window. The exception
           is the first try of a scheduled entry, at its explicit time.

        Args:
            message: The outbox entry.
            now: The evaluation time. The default is the current time. Tests give their
                own time.

        Returns:
            ``True`` when a try is permitted.
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
        """The seconds until the backoff releases a failed entry, or ``None`` if it is free."""
        if message.last_attempt is None or message.attempts == 0:
            return None
        backoff = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (message.attempts - 1))
        remaining = backoff - ((now or utcnow()) - message.last_attempt).total_seconds()
        return remaining if remaining > 0 else None

    # --- delivery ------------------------------------------------------------------------

    async def _pass(self) -> None:
        """One pass: try the oldest eligible entry, if there is one (one for each pass)."""
        if not self._ctx.is_connected or self._sending:
            return
        now = utcnow()
        for message in self._ctx.courier_store.pending():
            if self.eligible(message, now):
                await self._attempt(message)
                return

    async def attempt_now(self, ident: int) -> str:
        """Force one delivery try for *Send now* on the screen, without the eligibility rules.

        Args:
            ident: The id of the outbox entry.

        Returns:
            A short result string: ``delivered``, ``no ack``, ``gave up``,
            ``unknown contact``, ``busy``, or ``gone``.
        """
        message = self._ctx.courier_store.get(ident)
        if message is None or message.status != QUEUED:
            return "gone"
        if self._sending:
            return "busy"
        return await self._attempt(message)

    async def _attempt(self, message: QueuedMessage) -> str:
        """Run one delivery try, and set the new state of the entry."""
        store = self._ctx.courier_store
        self._sending = True
        try:
            contact = await self._resolve(message)
            if contact is None:
                # Not addressable (yet): the device does not know the contact. Leave the
                # entry in the queue, and do not count a try for a send that cannot occur.
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
        """The ack of a try arrived after the try stopped its wait: the message was delivered.

        Without this method, the entry stayed in the queue (or the courier gave up on it),
        and the next try sent the same message to the recipient again. Each late ack was a
        duplicate message on the screen of the recipient.
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
        """Find the recipient in the contact list of the device, by key and then by name."""
        device = await self._ctx.device()
        contacts = await device.get_contacts()
        for contact in contacts:
            ident = (contact.public_key or contact.key_prefix or "").lower()
            ident = ident.removeprefix("0x")[:12]
            if ident and ident == message.node_key:
                return contact
        needle = message.node_name.casefold()
        return next((c for c in contacts if c.name.casefold() == needle), None)
