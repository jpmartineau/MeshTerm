# SPDX-License-Identifier: Apache-2.0
"""Tests for the session cache of DeviceState: read once, serve from the cache, invalidate on write.

The cache keeps the navigation between screens away from the slow radio round-trips
(contacts, self-info, the channel probe). These tests check the two behaviours that make
the cache correct:

* MeshTerm reads the stable facts one time and uses them again until a write in the app
  invalidates them.
* The contacts list refreshes in the background when it is older than the TTL, and it
  never blocks a read.

A fake device counts the round-trips. Thus a test can assert "served from the cache"
without hardware.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from meshterm.core.models import Contact
from meshterm.services.device_state import _CONTACTS_TTL_S, _SELF_INFO_TTL_S, DeviceState


class FakeDevice:
    """A stand-in device that counts how often each cached read goes to the wire."""

    def __init__(self) -> None:
        """Start with a call count of zero for each read and no fixed contact table."""
        self.contacts_calls = 0
        self.self_info_calls = 0
        self.mode_calls = 0
        self.channel_calls = 0
        self.capacity_calls = 0
        self.contact_rows: list[Contact] | None = None  # a fixed table, if a test needs one

    async def get_contacts(self) -> list:
        """Return the contact list and count the call.

        The list is the fixed table that a test installed. If there is no table, it is one
        contact whose name has the call number. Thus a second read is visible in the result.
        """
        self.contacts_calls += 1
        if self.contact_rows is not None:
            return list(self.contact_rows)
        # a new identity for each read, so that a second read is visible
        return [Contact(name=f"contact-{self.contacts_calls}")]

    async def get_self_info(self) -> dict:
        """Return a fixed self-description and count the call."""
        self.self_info_calls += 1
        return {"name": "node", "tx_power": 20}

    async def get_path_hash_mode(self) -> int:
        """Return a fixed path hash mode and count the call."""
        self.mode_calls += 1
        return 2

    async def get_channel(self, idx: int):
        """Return one configured slot at index 0, then raise an error. Count each call.

        The firmware refuses the next index to answer for a slot that it does not have.
        This stops the caller before it goes past the last slot.
        """
        self.channel_calls += 1
        if idx == 0:
            return {"channel_name": "public", "channel_secret": b"\x00" * 16}
        raise RuntimeError("out of range")

    async def channel_capacity(self) -> int:
        """Return the slot count and count the call.

        The count is a fixed constant of the hardware, so the cache must read it exactly one
        time in each session.
        """
        self.capacity_calls += 1
        return 8


def _devstate(
    device: FakeDevice,
    heard: dict | None = None,
    messaged: dict | None = None,
) -> DeviceState:
    """A DeviceState over a fake ctx: ``device()``, a silent logger, and our reception history.

    ``heard`` maps a node id to the time when we last heard it. ``messaged`` maps a peer
    prefix to the time when it last sent us a direct message. Together they are the
    first-hand evidence that the contacts merge compares with the advert times of the
    device.
    """

    async def device_getter():
        return device

    ctx = SimpleNamespace(
        device=device_getter,
        log=SimpleNamespace(debug=lambda *a, **k: None),
        repo=SimpleNamespace(
            last_heard_by_node=lambda: dict(heard or {}),
            last_message_by_peer=lambda: dict(messaged or {}),
        ),
    )
    return DeviceState(ctx)  # type: ignore[arg-type]


def test_stable_facts_are_fetched_once_and_served_from_cache() -> None:
    """MeshTerm reads self-info, the path hash mode, and channels from the radio one time.

    Then it uses the value again.
    """
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        for _ in range(3):
            assert (await ds.self_info())["name"] == "node"
            assert await ds.path_hash_mode() == 2
            assert [s.name for s in await ds.channel_slots()] == ["public"]

    asyncio.run(run())
    assert dev.self_info_calls == 1
    assert dev.mode_calls == 1
    # The channel probe ran one time (idx 0 correct, idx 1 refused) and the cache kept the
    # whole result.
    assert dev.channel_calls == 2


def test_contacts_served_from_cache_within_ttl() -> None:
    """Many contacts reads inside the TTL go to the wire exactly one time."""
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        first = await ds.contacts()
        for _ in range(5):
            assert await ds.contacts() is first  # the same list object from the cache

    asyncio.run(run())
    assert dev.contacts_calls == 1


def test_contacts_refresh_in_background_past_ttl() -> None:
    """After the TTL, a read returns the old list at once.

    It refreshes the list in the background.
    """
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        stale = await ds.contacts()  # first read
        assert dev.contacts_calls == 1
        # Make the cache older than the TTL, then read. The read must return at once (the
        # old copy) and schedule a refresh in the background. It must not wait for the slow
        # call.
        ds._contacts_at = time.monotonic() - _CONTACTS_TTL_S - 1
        served = await ds.contacts()
        assert served is stale  # the old list, with no wait for a second read
        # Let the scheduled refresh in the background run.
        await asyncio.gather(*list(ds._tasks))
        assert dev.contacts_calls == 2  # the refresh happened in the background
        assert (await ds.contacts())[0].name == "contact-2"  # now the new list

    asyncio.run(run())


def test_self_info_refreshes_in_background_past_ttl() -> None:
    """A node with a GPS can move, so MeshTerm reads our self-info again after its TTL.

    A read after the TTL still answers at once with the copy that MeshTerm has. The next
    screen gets the position that the node reports now.
    """
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        first = await ds.self_info()
        assert await ds.self_info() is first and dev.self_info_calls == 1  # within the TTL
        ds._self_info_at = time.monotonic() - _SELF_INFO_TTL_S - 1
        assert await ds.self_info() is first  # answered at once, with no wait for the second read
        await asyncio.gather(*list(ds._tasks))
        assert dev.self_info_calls == 2
        assert await ds.self_info() is not first  # the new copy from this point

    asyncio.run(run())


def test_heard_time_takes_the_later_of_the_device_and_our_own_receptions() -> None:
    """The heard time of a contact is the latest of the advert time of the device and our evidence.

    The clock of the *sender* gives the ``last_advert`` of the device its time, so it is
    hearsay. It can be absent (``models.advert_time`` refuses it earlier in the chain). It
    can look correct but be many days old, because the RTC of the node is slow. Our own
    history is first-hand: it has the time when we received something. Thus the merge uses
    the later time. A time from the device still wins if the firmware caught an advert that
    we did not record.
    """
    dev = FakeDevice()
    stale = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    fresh = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    dev.contact_rows = [
        Contact(name="Bogus-Clock", public_key="aa" * 32, key_prefix="aa" * 6),
        Contact(name="Behind-Clock", public_key="bb" * 32, key_prefix="bb" * 6, last_seen=stale),
        Contact(name="Ahead", public_key="cc" * 32, key_prefix="cc" * 6, last_seen=fresh),
        Contact(name="Quiet", public_key="dd" * 32, key_prefix="dd" * 6),
    ]
    ds = _devstate(dev, heard={"aa" * 6: fresh, "bb" * 6: fresh, "cc" * 6: stale})

    async def run() -> None:
        by_name = {c.name: c for c in await ds.contacts()}
        assert by_name["Bogus-Clock"].last_seen == fresh  # filled from our own history
        assert by_name["Behind-Clock"].last_seen == fresh  # our proof is better than an old stamp
        assert by_name["Ahead"].last_seen == fresh  # a newer time from the device stays
        assert by_name["Quiet"].last_seen is None  # a node that was never heard stays never heard

    asyncio.run(run())


def test_a_direct_message_counts_as_hearing_its_sender() -> None:
    """An inbound DM updates the heard time. "Heard" means received from.

    A direct message never changes the ``last_advert`` of the firmware. MeshTerm stores it
    as a message and not as an observation. Thus a node that we talk with could look many
    days old while it talks to us, and the quiet rungs of the archive ladder could sweep it.
    The peer prefix of the wire address can have a different width from the contact table.
    Either of the two can be the shorter one.
    """
    dev = FakeDevice()
    stale = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    messaged_at = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    dev.contact_rows = [
        Contact(name="Chatty", public_key="ab" * 32, key_prefix="ab" * 6, last_seen=stale),
        Contact(name="Silent", public_key="cd" * 32, key_prefix="cd" * 6, last_seen=stale),
    ]
    # A peer with six hex digits and a contact id with twelve hex digits: the shorter one is
    # from the wire.
    ds = _devstate(dev, messaged={"ababab": messaged_at})

    async def run() -> None:
        by_name = {c.name: c for c in await ds.contacts()}
        assert by_name["Chatty"].last_seen == messaged_at
        assert by_name["Silent"].last_seen == stale  # no other contact gets the time

    asyncio.run(run())


def test_a_naive_stored_stamp_still_compares() -> None:
    """A row without a time zone from an older build is read as UTC, as the storage contract says.

    A comparison of a naive datetime with an aware datetime raises an error. This error
    would stop the whole contacts read, not only one lane.
    """
    dev = FakeDevice()
    stale = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 7, 29, 12, 0)  # written before timestamps had a zone
    dev.contact_rows = [
        Contact(name="Legacy", public_key="aa" * 32, key_prefix="aa" * 6, last_seen=stale)
    ]
    ds = _devstate(dev, heard={"aa" * 6: naive})

    async def run() -> None:
        assert (await ds.contacts())[0].last_seen == naive.replace(tzinfo=timezone.utc)

    asyncio.run(run())


def test_a_history_read_failure_leaves_the_contacts_untouched() -> None:
    """The merge is best-effort. A history read that fails never blocks a contacts read."""
    dev = FakeDevice()
    device_says = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)
    dev.contact_rows = [
        Contact(name="Solo", public_key="ee" * 32, key_prefix="ee" * 6, last_seen=device_says)
    ]
    ds = _devstate(dev)

    def boom() -> dict:
        raise RuntimeError("history unavailable")

    ds._ctx.repo.last_heard_by_node = boom  # type: ignore[attr-defined]

    async def run() -> None:
        assert (await ds.contacts())[0].last_seen == device_says

    asyncio.run(run())


def test_force_bypasses_the_contacts_cache() -> None:
    """A forced read always reads again. A caller that needs new data (this is rare) uses it."""
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        await ds.contacts()
        await ds.contacts(force=True)

    asyncio.run(run())
    assert dev.contacts_calls == 2


def test_invalidation_forces_a_re_read() -> None:
    """Each invalidation removes exactly its own entry. Thus the next read gets it again."""
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        await ds.self_info()
        await ds.path_hash_mode()
        await ds.channel_slots()
        await ds.contacts()
        # invalidate_config removes self-info and the path hash mode together (this is the
        # write of the config editor).
        ds.invalidate_config()
        await ds.self_info()
        await ds.path_hash_mode()
        assert dev.self_info_calls == 2 and dev.mode_calls == 2
        # This invalidation did not change channels and contacts.
        assert dev.channel_calls == 2 and dev.contacts_calls == 1
        ds.invalidate_channels()
        await ds.channel_slots()
        assert dev.channel_calls == 4  # the probe ran again

    asyncio.run(run())


def test_reset_clears_everything() -> None:
    """The reset after a reconnect clears the whole cache.

    Thus MeshTerm reads each fact again at the next use.
    """
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        await ds.self_info()
        await ds.contacts()
        await ds.path_hash_mode()
        await ds.channel_capacity()
        ds.reset()
        await ds.self_info()
        await ds.contacts()
        await ds.path_hash_mode()
        await ds.channel_capacity()

    asyncio.run(run())
    assert dev.self_info_calls == 2
    assert dev.contacts_calls == 2
    assert dev.mode_calls == 2
    assert dev.capacity_calls == 2  # a hardware constant, but a reconnect reads it again


def test_channel_capacity_is_fetched_once_and_served_from_cache() -> None:
    """The capacity is a hardware constant.

    MeshTerm probes it one time and uses it for the session.
    """
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        for _ in range(3):
            assert await ds.channel_capacity() == 8

    asyncio.run(run())
    assert dev.capacity_calls == 1


def test_prewarm_fills_every_cache_off_the_read_path() -> None:
    """``prewarm()`` warms all five facts, so the first screen that opens does not use the wire."""
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        ds.prewarm()
        await asyncio.gather(*list(ds._tasks))  # let the warm in the background finish
        # The prewarm filled each cache: self-info and the routing mode one time each,
        # contacts one time, the channel probe one time (idx 0 correct, idx 1 refused), and
        # the capacity one time.
        assert dev.self_info_calls == 1
        assert dev.mode_calls == 1
        assert dev.contacts_calls == 1
        assert dev.channel_calls == 2
        assert dev.capacity_calls == 1
        # A screen that opens now gets its data from the cache, with no more round-trips.
        # The path hash mode must be in that list. Contacts and Trace both read it to find
        # the size of the key hash highlight, and nothing else in the session warms it.
        await ds.contacts()
        await ds.self_info()
        await ds.path_hash_mode()
        await ds.channel_slots()
        await ds.channel_capacity()
        assert dev.self_info_calls == 1
        assert dev.mode_calls == 1
        assert dev.contacts_calls == 1
        assert dev.channel_calls == 2
        assert dev.capacity_calls == 1

    asyncio.run(run())


def test_prewarm_reads_the_cheap_facts_before_the_slot_probes() -> None:
    """The order is the purpose of this test: the reads share one link, and the probes are slow.

    Both slot reads go through the slot table of the firmware, one index at a time. They
    take seconds. The facts that each list screen needs are single round-trips. MeshTerm
    warms them first. Thus Contacts, if it opens one second after the connect, does not
    wait behind a slot walk.
    """
    dev = FakeDevice()
    ds = _devstate(dev)
    order: list[str] = []
    for name in (
        "get_self_info",
        "get_path_hash_mode",
        "get_contacts",
        "get_channel",
        "channel_capacity",
    ):
        inner = getattr(dev, name)

        async def traced(*a, _name=name, _inner=inner, **k):  # noqa: ANN001, ANN202
            if _name != "get_channel" or "get_channel" not in order:
                order.append(_name)
            return await _inner(*a, **k)

        setattr(dev, name, traced)

    async def run() -> None:
        ds.prewarm()
        await asyncio.gather(*list(ds._tasks))

    asyncio.run(run())
    assert order == [
        "get_self_info",
        "get_path_hash_mode",
        "get_contacts",
        "get_channel",
        "channel_capacity",
    ]


def test_a_concurrent_path_hash_read_joins_the_warm_instead_of_racing_it() -> None:
    """The prewarm reads the mode now.

    Thus a screen that opens at the same time must not read it again.
    """
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        await asyncio.gather(ds.path_hash_mode(), ds.path_hash_mode(), ds.path_hash_mode())
        assert dev.mode_calls == 1

    asyncio.run(run())
