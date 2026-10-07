# SPDX-License-Identifier: Apache-2.0
"""Tests for the event hub that is always on: fan-out, filtering, streaming, and lifecycle.

All tests run against the :class:`MockDevice` simulator. They do not need hardware.
"""

from __future__ import annotations

import asyncio
import logging

from meshterm.core.connection import (
    _MOCK_MONITOR_INTERVAL_S as _MOCK_INTERVAL,
)
from meshterm.core.connection import MockDevice
from meshterm.core.events import EventKind, MeshEvent
from meshterm.core.models import Observation
from meshterm.services.event_hub import EventHub


class _StubContext:
    """A minimal stand-in for :class:`~meshterm.context.AppContext` in the hub tests."""

    def __init__(self, device: MockDevice) -> None:
        self._device = device
        self.log = logging.getLogger("test.event_hub")

    async def device(self) -> MockDevice:
        await self._device.connect()
        return self._device


def _obs_event(node: str) -> MeshEvent:
    """Build an observation event for a node id. This is a helper for the tests."""
    return MeshEvent.observation_event(Observation(node=node))


def test_publish_fans_out_to_all_subscribers() -> None:
    """Each subscriber receives each event that the hub publishes."""
    hub = EventHub(_StubContext(MockDevice()))
    a: list[MeshEvent] = []
    b: list[MeshEvent] = []
    hub.subscribe(a.append)
    hub.subscribe(b.append)

    event = _obs_event("a1")
    hub.publish(event)

    assert a == [event]
    assert b == [event]


def test_kind_filter_only_delivers_requested_kinds() -> None:
    """A subscriber with a filter for a kind receives only that kind.

    A subscriber with no filter receives all kinds.
    """
    hub = EventHub(_StubContext(MockDevice()))
    observations: list[MeshEvent] = []
    everything: list[MeshEvent] = []
    hub.subscribe(observations.append, EventKind.OBSERVATION)
    hub.subscribe(everything.append)  # no kinds means all kinds

    obs_event = _obs_event("a1")
    other = MeshEvent(kind=EventKind.OBSERVATION, payload=None)  # it is also an OBSERVATION kind
    hub.publish(obs_event)
    hub.publish(other)

    assert observations == [obs_event, other]  # both are the OBSERVATION kind
    assert everything == [obs_event, other]


def test_unsubscribe_stops_delivery() -> None:
    """A subscriber that the hub removes receives nothing more, and unsubscribe is idempotent."""
    hub = EventHub(_StubContext(MockDevice()))
    seen: list[MeshEvent] = []
    unsubscribe = hub.subscribe(seen.append)

    hub.publish(_obs_event("a1"))
    unsubscribe()
    hub.publish(_obs_event("b2"))
    unsubscribe()  # the second call does nothing, and this is not a problem

    assert len(seen) == 1


def test_failing_subscriber_does_not_break_others() -> None:
    """The hub skips a subscriber that raises an error.

    The other subscribers still receive the event.
    """
    hub = EventHub(_StubContext(MockDevice()))
    good: list[MeshEvent] = []

    def boom(_event: MeshEvent) -> None:
        raise RuntimeError("subscriber blew up")

    hub.subscribe(boom)
    hub.subscribe(good.append)

    event = _obs_event("a1")
    hub.publish(event)  # this call must not raise an error

    assert good == [event]


async def test_async_handler_is_scheduled() -> None:
    """If a handler returns a coroutine, the hub runs it as a task. It does not await it inline."""
    hub = EventHub(_StubContext(MockDevice()))
    ran = asyncio.Event()

    async def handler(_event: MeshEvent) -> None:
        ran.set()

    hub.subscribe(handler)
    hub.publish(_obs_event("a1"))

    assert not ran.is_set()  # the hub did not await it inline
    await asyncio.wait_for(ran.wait(), timeout=1.0)  # the hub scheduled it, and it runs on the loop


async def test_stream_yields_matching_events_then_unsubscribes() -> None:
    """stream() yields the events that the hub publishes.

    It removes its subscription when it closes.
    """
    hub = EventHub(_StubContext(MockDevice()))
    stream = hub.stream(EventKind.OBSERVATION)

    event = _obs_event("a1")
    hub.publish(event)
    received = await asyncio.wait_for(stream.__anext__(), timeout=1.0)
    assert received is event

    await stream.aclose()  # the close removes the subscription under the stream
    assert hub._subs == []


async def test_message_events_delivered_from_device() -> None:
    """The inbound messages of the device reach a MESSAGE subscriber as message events."""
    device = MockDevice()
    hub = EventHub(_StubContext(device))
    messages: list[MeshEvent] = []
    hub.subscribe(messages.append, EventKind.MESSAGE)

    await hub.start()  # the first burst of the simulator has one message, synchronously
    assert messages
    assert all(e.kind is EventKind.MESSAGE and e.message is not None for e in messages)
    assert messages[0].message.text

    await hub.stop()
    await device.disconnect()


async def test_wait_for_returns_matching_event() -> None:
    """wait_for resolves with the next event of the requested kind."""
    device = MockDevice()
    hub = EventHub(_StubContext(device))
    await hub.start()

    event = await hub.wait_for(EventKind.OBSERVATION, timeout=1.0)
    assert event is not None
    assert event.observation is not None

    await hub.stop()
    await device.disconnect()


async def test_wait_for_honors_predicate_and_times_out() -> None:
    """wait_for returns None when no event satisfies the predicate before the timeout."""
    device = MockDevice()
    hub = EventHub(_StubContext(device))
    await hub.start()

    event = await hub.wait_for(EventKind.OBSERVATION, predicate=lambda _e: False, timeout=0.2)
    assert event is None

    await hub.stop()
    await device.disconnect()


async def test_start_pumps_device_observations_then_stops() -> None:
    """The start subscribes to the device. Observations arrive as events until the hub stops."""
    device = MockDevice()
    hub = EventHub(_StubContext(device))
    events: list[MeshEvent] = []
    hub.subscribe(events.append, EventKind.OBSERVATION)

    assert not hub.active
    await hub.start()
    assert hub.active

    await asyncio.sleep(_MOCK_INTERVAL * 3)
    assert events  # the adverts of the simulator went through as observation events
    assert all(e.kind is EventKind.OBSERVATION for e in events)
    assert all(e.observation is not None for e in events)

    await hub.stop()
    assert not hub.active
    frozen = len(events)
    await asyncio.sleep(_MOCK_INTERVAL * 3)
    assert len(events) == frozen  # no more events after the stop

    await device.disconnect()
