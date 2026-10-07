# SPDX-License-Identifier: Apache-2.0
"""Regression tests for channel reads that are serialized and checked against the index.

The ``get_channel`` of the meshcore library waits for "the next CHANNEL_INFO event". It does
not match this event to the slot that it asked for, and the dispatcher sends the event to
each waiter that is in flight. Thus two concurrent reads both resolve on the first response.
One caller silently gets the channel of the other caller, and this files the messages of that
channel in the wrong place. :class:`MeshCoreDevice` prevents this. It serializes the reads and
checks that the response is for the slot that it requested.
"""

from __future__ import annotations

import asyncio

import pytest

from meshterm.core.connection import DeviceCommandError, MeshCoreDevice


class _Event:
    """A minimal stand-in for a meshcore CHANNEL_INFO event."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def is_error(self) -> bool:
        return False


class _CrosstalkCommands:
    """A fake ``commands`` that causes the race of unmatched responses if reads overlap.

    Each call records itself as in flight and gives control to other tasks. If a second read
    starts while the first read is not complete, both reads return the payload of the read
    that finishes first. This is the behaviour of the library that sends messages to the wrong
    place. Serialized callers never overlap, so each caller gets its own channel.
    """

    def __init__(self) -> None:
        self.in_flight: list[int] = []
        self.peak_concurrency = 0

    async def get_channel(self, index: int) -> _Event:
        self.in_flight.append(index)
        self.peak_concurrency = max(self.peak_concurrency, len(self.in_flight))
        try:
            await asyncio.sleep(0)  # a real await point, where an interleave can occur
            # Model "the first response resolves everyone". The crossed answer is the
            # oldest request in flight, which is not always this request.
            served = self.in_flight[0]
            return _Event(
                {
                    "channel_idx": served,
                    "channel_name": f"chan{served}",
                    "channel_secret": bytes([served]) * 16,
                }
            )
        finally:
            self.in_flight.remove(index)


class _MismatchCommands:
    """A fake ``commands`` that always answers with the wrong slot index."""

    async def get_channel(self, index: int) -> _Event:
        wrong = index + 1
        return _Event(
            {
                "channel_idx": wrong,
                "channel_name": f"chan{wrong}",
                "channel_secret": bytes([wrong]) * 16,
            }
        )


def _device_with(commands: object) -> MeshCoreDevice:
    device = MeshCoreDevice("COM-TEST")
    device._mc = type("_MC", (), {"commands": commands})()
    return device


async def test_get_channel_serializes_concurrent_reads() -> None:
    """Concurrent reads never overlap, so each read returns its own slot (no cross-talk)."""
    commands = _CrosstalkCommands()
    device = _device_with(commands)

    results = await asyncio.gather(*(device.get_channel(i) for i in range(8)))

    assert commands.peak_concurrency == 1
    for i, payload in enumerate(results):
        assert payload is not None
        assert payload["channel_idx"] == i
        assert payload["channel_name"] == f"chan{i}"


async def test_get_channel_rejects_response_for_a_different_slot() -> None:
    """The device refuses a response with an index that does not match the request.

    It never returns the response.
    """
    device = _device_with(_MismatchCommands())

    with pytest.raises(DeviceCommandError, match="returned slot"):
        await device.get_channel(0)


class _Refusal:
    """A meshcore ERROR event: a refusal with an error code of the companion, or a timeout."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def is_error(self) -> bool:
        return True


class _ScriptedCommands:
    """A fake ``commands`` that answers each slot from a queue of scripted replies.

    ``replies[i]`` is a list of what the next reads of slot ``i`` get: a channel name, or an
    error payload. A slot with no script is past the end. The firmware answers NOT_FOUND for it.
    """

    def __init__(self, replies: dict[int, list]) -> None:
        self.replies = {i: list(r) for i, r in replies.items()}
        self.reads: list[int] = []

    async def get_channel(self, index: int):  # noqa: ANN201
        self.reads.append(index)
        queue = self.replies.get(index)
        reply = queue.pop(0) if queue else {"error_code": 2}
        if isinstance(reply, dict):
            return _Refusal(reply)
        return _Event({"channel_idx": index, "channel_name": reply, "channel_secret": bytes(16)})


#: The refusal of the clock sync, as a concurrent channel read receives it.
_MALFORMED = {"error_code": 6}


async def test_a_borrowed_refusal_does_not_end_the_slot_probe() -> None:
    """The error of another command, delivered to a slot read, does not end the list of slots.

    The library gives the first ERROR that arrives to each waiter. Thus, at connect, the
    channel prewarm can take the "malformed" error of the clock sync as the end of the list
    at slot 2. Then it caches a list that does not have the channels after slot 2.
    """
    from meshterm.core.channel_probe import probe_channel_slots

    commands = _ScriptedCommands({0: ["Public"], 1: ["ops"], 2: [_MALFORMED, "#montreal"]})
    slots, complete = await probe_channel_slots(_device_with(commands))
    assert [s.name for s in slots] == ["Public", "ops", "#montreal"]
    assert complete
    assert commands.reads.count(2) == 2


async def test_a_refusal_that_repeats_is_a_failed_read_not_a_layout() -> None:
    """Two refusals in a row that are not NOT_FOUND leave the probe unfinished.

    MeshTerm does not cache the result.
    """
    from meshterm.core.channel_probe import probe_channel_slots

    commands = _ScriptedCommands({0: ["Public"], 1: [_MALFORMED, _MALFORMED], 2: ["ops"]})
    slots, complete = await probe_channel_slots(_device_with(commands))
    assert [s.name for s in slots] == ["Public"]
    assert not complete


async def test_no_answer_is_a_failed_read_and_not_found_is_the_end() -> None:
    """A timeout is never the end of the slots. The NOT_FOUND of the firmware is the end."""
    from meshterm.core.channel_probe import probe_channel_slots

    timeout = {"reason": "no_event_received"}
    slots, complete = await probe_channel_slots(
        _device_with(_ScriptedCommands({0: ["Public"], 1: [timeout]}))
    )
    assert [s.name for s in slots] == ["Public"] and not complete

    slots, complete = await probe_channel_slots(_device_with(_ScriptedCommands({0: ["Public"]})))
    assert [s.name for s in slots] == ["Public"] and complete
