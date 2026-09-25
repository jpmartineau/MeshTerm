# SPDX-License-Identifier: Apache-2.0
"""Regression tests for serialized, index-validated channel reads.

The meshcore library's ``get_channel`` waits for "the next CHANNEL_INFO event" with no
correlation to the slot it asked for, and the dispatcher fans that event to every in-flight
waiter. Two concurrent reads therefore both resolve on the first response and one caller
silently gets the other's channel — which misfiles that channel's messages. :class:`MeshCoreDevice`
guards against this by serializing reads and verifying the response is for the slot requested.
"""

from __future__ import annotations

import asyncio

import pytest

from meshterm.core.connection import DeviceCommandError, MeshCoreDevice


class _Event:
    """Minimal stand-in for a meshcore CHANNEL_INFO event."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def is_error(self) -> bool:
        return False


class _CrosstalkCommands:
    """Fake ``commands`` that reproduces the uncorrelated-response race if reads overlap.

    Each call records itself as in-flight and yields control. If a second read starts while
    the first is still outstanding, both return the payload of whichever finishes first —
    exactly the library behavior that misroutes messages. Serialized callers never overlap, so
    each gets its own channel back.
    """

    def __init__(self) -> None:
        self.in_flight: list[int] = []
        self.peak_concurrency = 0

    async def get_channel(self, index: int) -> _Event:
        self.in_flight.append(index)
        self.peak_concurrency = max(self.peak_concurrency, len(self.in_flight))
        try:
            await asyncio.sleep(0)  # a real await point where interleaving would happen
            # Model "the first response resolves everyone": the crossed answer is the
            # oldest in-flight request, not necessarily this one.
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
    """Fake ``commands`` that always answers with the wrong slot index."""

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
    """Concurrent reads never overlap, so each returns its own slot (no cross-talk)."""
    commands = _CrosstalkCommands()
    device = _device_with(commands)

    results = await asyncio.gather(*(device.get_channel(i) for i in range(8)))

    assert commands.peak_concurrency == 1
    for i, payload in enumerate(results):
        assert payload is not None
        assert payload["channel_idx"] == i
        assert payload["channel_name"] == f"chan{i}"


async def test_get_channel_rejects_response_for_a_different_slot() -> None:
    """A response whose index doesn't match the request is refused, never returned as-is."""
    device = _device_with(_MismatchCommands())

    with pytest.raises(DeviceCommandError, match="returned slot"):
        await device.get_channel(0)


class _Refusal:
    """A meshcore ERROR event: a refusal with a companion error code, or a timeout's."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def is_error(self) -> bool:
        return True


class _ScriptedCommands:
    """Fake ``commands`` answering each slot from a queue of scripted replies.

    ``replies[i]`` is a list of what successive reads of slot ``i`` get: a channel name, or
    an error payload. A slot with no script is past the end — the firmware's NOT_FOUND.
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


#: What the clock sync's refusal looks like when a concurrent channel read is handed it.
_MALFORMED = {"error_code": 6}


async def test_a_borrowed_refusal_does_not_end_the_slot_probe() -> None:
    """Another command's error, delivered to a slot read, is asked past — not a list's end.

    The library hands every waiter the first ERROR to arrive, so at connect the channel
    prewarm could take the clock sync's "malformed" as slot 2 being past the end, and cache
    a list missing every channel after it.
    """
    from meshterm.core.channel_probe import probe_channel_slots

    commands = _ScriptedCommands({0: ["Public"], 1: ["ops"], 2: [_MALFORMED, "#montreal"]})
    slots, complete = await probe_channel_slots(_device_with(commands))
    assert [s.name for s in slots] == ["Public", "ops", "#montreal"]
    assert complete
    assert commands.reads.count(2) == 2


async def test_a_refusal_that_repeats_is_a_failed_read_not_a_layout() -> None:
    """Two refusals in a row that aren't NOT_FOUND leave the probe unfinished (not cached)."""
    from meshterm.core.channel_probe import probe_channel_slots

    commands = _ScriptedCommands({0: ["Public"], 1: [_MALFORMED, _MALFORMED], 2: ["ops"]})
    slots, complete = await probe_channel_slots(_device_with(commands))
    assert [s.name for s in slots] == ["Public"]
    assert not complete


async def test_no_answer_is_a_failed_read_and_not_found_is_the_end() -> None:
    """A timeout never reads as the end of the slots; the firmware's NOT_FOUND does."""
    from meshterm.core.channel_probe import probe_channel_slots

    timeout = {"reason": "no_event_received"}
    slots, complete = await probe_channel_slots(
        _device_with(_ScriptedCommands({0: ["Public"], 1: [timeout]}))
    )
    assert [s.name for s in slots] == ["Public"] and not complete

    slots, complete = await probe_channel_slots(_device_with(_ScriptedCommands({0: ["Public"]})))
    assert [s.name for s in slots] == ["Public"] and complete
