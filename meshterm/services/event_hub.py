# SPDX-License-Identifier: Apache-2.0
"""The mesh event hub, which is always on.

The hub is the only connection point between the raw event stream of the companion device
and all the code that reacts to it. It opens one subscription to the connected device. It
normalizes the data that arrives into typed :class:`~meshterm.core.events.MeshEvent`
values, and it sends each value to all the subscribers (any number of them). A one-shot
capture stops, but the hub runs for the full life of an interactive session, because a
MeshCore client must always listen. Thus subscribers come and go while the pump continues
to run below them.

This design makes the code that listens independent of the code that uses the events.
The database logging of the passive monitor is only one subscriber (refer to
:class:`~meshterm.services.monitor_service.MonitorService`). Future client features (a
live node list, an inbox for received messages) attach as more subscribers, and they do
not change the device layer.

The hub is state of the session, on the :class:`~meshterm.context.AppContext`
(``ctx.events``). The fan-out runs synchronously on the event loop when packets arrive.
Thus handlers must be fast. When a handler returns a coroutine, the hub schedules it as a
task.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..core.connection import Unsubscribe
from ..core.events import EventKind, MeshEvent

if TYPE_CHECKING:
    from ..context import AppContext

#: A subscriber callback. The hub calls it with each matching :class:`MeshEvent`. Keep it
#: fast, because it runs inline on the event loop when packets arrive. It can return a
#: coroutine. Then the hub schedules the coroutine as a task, and does not await it inline.
EventHandler = Callable[[MeshEvent], None | Awaitable[None]]


@dataclass(slots=True)
class _Subscription:
    """One registered subscriber: a handler, and the event kinds that it wants.

    Attributes:
        handler: The callback to call with matching events.
        kinds: The event kinds to deliver. An empty set means all kinds.
    """

    handler: EventHandler
    kinds: frozenset = field(default_factory=frozenset)

    def wants(self, kind: EventKind) -> bool:
        """Whether this subscription wants an event of ``kind``."""
        return not self.kinds or kind in self.kinds


class EventHub:
    """Owns the event subscription to the device, and sends the events to subscribers.

    Use :meth:`subscribe` or :meth:`stream` to get events. Use the async lifecycle methods
    (:meth:`start`, :meth:`stop`, :meth:`aclose`) to control the pump.
    """

    def __init__(self, ctx: AppContext) -> None:
        """Make an idle hub for an application context.

        Args:
            ctx: The shared application context. The hub uses it to open the device
                connection and to log. The hub opens no device until a call to
                :meth:`start`.
        """
        self._ctx = ctx
        self._device_unsubscribe: Unsubscribe | None = None
        self._subs: list[_Subscription] = []
        self._tasks: set[asyncio.Task] = set()

    @property
    def active(self) -> bool:
        """Whether the pump runs (with a subscription to the device)."""
        return self._device_unsubscribe is not None

    async def start(self) -> None:
        """Open the device subscription, and start the pump of events. Idempotent.

        Opens the companion connection (which can raise an error if no device can be
        selected), and subscribes to its observation stream. While the pump runs, all the
        subscribers get events, if they registered before this call or after it.

        Raises:
            Exception: Each error from the device or the subscription goes to the caller.
                The hub stays inactive, so that the caller can show the problem and try
                again later.
        """
        if self.active:
            return
        device = await self._ctx.device()
        self._device_unsubscribe = await device.subscribe_events(self.publish)
        self._ctx.log.info("event hub started")

    async def stop(self) -> None:
        """Release the device subscription, and cancel all the scheduled handler tasks.

        Idempotent. The registered subscribers stay, so a later :meth:`start` delivers to
        them again.
        """
        if self._device_unsubscribe is not None:
            try:
                self._device_unsubscribe()
            finally:
                self._device_unsubscribe = None
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._ctx.log.info("event hub stopped")

    def subscribe(self, handler: EventHandler, *kinds: EventKind) -> Unsubscribe:
        """Register ``handler`` for events, and return a callable that removes it.

        Args:
            handler: The callback that the hub calls with each matching :class:`MeshEvent`.
            *kinds: The event kinds to get. Give none to get all kinds.

        Returns:
            A zero-argument callable that unregisters the subscription.
        """
        sub = _Subscription(handler=handler, kinds=frozenset(kinds))
        self._subs.append(sub)

        def unsubscribe() -> None:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass  # already removed: unsubscribe is idempotent

        return unsubscribe

    def stream(self, *kinds: EventKind, maxsize: int = 0):
        """Return an async iterator that yields the matching events when they arrive.

        An easy ``async for event in hub.stream(...)`` adapter on :meth:`subscribe`, with
        an :class:`asyncio.Queue` below it. When the iterator closes, the subscription is
        removed automatically (for example, when the ``async for`` loop breaks, or when the
        consumer is cancelled).

        Args:
            *kinds: The event kinds to get. Give none to get all kinds.
            maxsize: An optional limit on the size of the queue (``0`` = no limit).

        Returns:
            An async generator of :class:`MeshEvent`.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        unsubscribe = self.subscribe(queue.put_nowait, *kinds)

        async def _iterator():
            try:
                while True:
                    yield await queue.get()
            finally:
                unsubscribe()

        return _iterator()

    def publish(self, event: MeshEvent) -> None:
        """Send ``event`` to each subscriber that wants its kind.

        Delivery is synchronous and best-effort. When a handler raises an error, the hub
        logs it and continues with the next handler. Thus one bad subscriber can never
        stop the pump, or stop the delivery to the other subscribers. When a handler
        returns a coroutine, the hub schedules it as a background task.

        Args:
            event: The event to deliver.
        """
        for sub in list(self._subs):
            if not sub.wants(event.kind):
                continue
            try:
                result = sub.handler(event)
            except Exception as exc:  # noqa: BLE001 - one bad subscriber must not break the pump
                self._ctx.log.debug("event hub: subscriber raised: %s", exc)
                continue
            if asyncio.iscoroutine(result):
                self._schedule(result)

    def _schedule(self, coro: Awaitable[None]) -> None:
        """Run the coroutine of an async handler as a tracked background task."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_for(
        self,
        *kinds: EventKind,
        predicate: Callable[[MeshEvent], bool] | None = None,
        timeout: float | None = None,
    ) -> MeshEvent | None:
        """Await the next event that matches ``kinds`` (and ``predicate``), or time out.

        A one-shot helper for consumers that want to block for a specific event, instead
        of a permanent handler. The method always removes the temporary subscription
        before it returns.

        Args:
            *kinds: The event kinds to accept. Give none to accept all kinds.
            predicate: An optional extra filter. To match, the event must also pass it.
            timeout: The seconds to wait before the method stops, or ``None`` to wait with
                no limit.

        Returns:
            The matching :class:`MeshEvent`, or ``None`` if the ``timeout`` ended first.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def handler(event: MeshEvent) -> None:
            if not future.done() and (predicate is None or predicate(event)):
                future.set_result(event)

        unsubscribe = self.subscribe(handler, *kinds)
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            unsubscribe()

    async def aclose(self) -> None:
        """Stop the pump at the end of the session (an alias for :meth:`stop`)."""
        await self.stop()
