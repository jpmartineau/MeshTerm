# SPDX-License-Identifier: Apache-2.0
"""DeviceState session-cache tests: read once, serve cached, invalidate on write.

The cache exists to keep screen navigation off the slow radio round-trips (contacts,
self-info, the channel probe). These tests pin the two behaviours that make it correct:
stable facts are fetched once and reused until an in-app write invalidates them, and the
contacts list refreshes in the background once it ages past the TTL without ever blocking a
read. A fake device counts round-trips so "served from cache" is assertable without hardware.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from meshterm.core.models import Contact
from meshterm.services.device_state import _CONTACTS_TTL_S, _SELF_INFO_TTL_S, DeviceState


class FakeDevice:
    """A stand-in device that counts how often each cached read hits the wire."""

    def __init__(self) -> None:
        """Start with every call count at zero and no fixed contact table."""
        self.contacts_calls = 0
        self.self_info_calls = 0
        self.mode_calls = 0
        self.channel_calls = 0
        self.capacity_calls = 0
        self.contact_rows: list[Contact] | None = None  # fixed table, when a test needs one

    async def get_contacts(self) -> list:
        """The contact list, counting the call.

        Either the fixed table a test installed, or a single contact whose name carries
        the call number — so a second read is visible in what comes back.
        """
        self.contacts_calls += 1
        if self.contact_rows is not None:
            return list(self.contact_rows)
        # a fresh identity per fetch, so a re-read is visible
        return [Contact(name=f"contact-{self.contacts_calls}")]

    async def get_self_info(self) -> dict:
        """A fixed self-description, counting the call."""
        self.self_info_calls += 1
        return {"name": "node", "tx_power": 20}

    async def get_path_hash_mode(self) -> int:
        """A fixed path hash mode, counting the call."""
        self.mode_calls += 1
        return 2

    async def get_channel(self, idx: int):
        """One configured slot at index 0, then a raised error, counting each call.

        Refusing the next index is how the firmware answers a slot it does not have,
        and it is what stops the caller walking past the last one.
        """
        self.channel_calls += 1
        if idx == 0:
            return {"channel_name": "public", "channel_secret": b"\x00" * 16}
        raise RuntimeError("out of range")

    async def channel_capacity(self) -> int:
        """The slot count, counting the call.

        A fixed hardware constant, so the cache must read it exactly once per session.
        """
        self.capacity_calls += 1
        return 8


def _devstate(
    device: FakeDevice,
    heard: dict | None = None,
    messaged: dict | None = None,
) -> DeviceState:
    """A DeviceState over a fake ctx: device(), a silent logger, and our reception history.

    ``heard`` is node-id → when we last overheard it; ``messaged`` is peer prefix → when it
    last sent us a direct message. Together they are the first-hand evidence the contacts
    merge weighs against the device's advert times.
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
    """self-info, path-hash mode, and channels read the radio once, then reuse the value."""
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
    # The channel probe ran once (idx 0 ok, idx 1 rejected) and was cached wholesale.
    assert dev.channel_calls == 2


def test_contacts_served_from_cache_within_ttl() -> None:
    """Repeated contacts reads inside the TTL hit the wire exactly once."""
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        first = await ds.contacts()
        for _ in range(5):
            assert await ds.contacts() is first  # same cached list object

    asyncio.run(run())
    assert dev.contacts_calls == 1


def test_contacts_refresh_in_background_past_ttl() -> None:
    """Past the TTL a read returns the stale list instantly and refreshes behind it."""
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        stale = await ds.contacts()  # first fetch
        assert dev.contacts_calls == 1
        # Age the cache past the TTL, then read: the read must return immediately (the stale
        # copy) and schedule a background refresh rather than block on the slow call.
        ds._contacts_at = time.monotonic() - _CONTACTS_TTL_S - 1
        served = await ds.contacts()
        assert served is stale  # served the old list, did not block on a re-fetch
        # Let the scheduled background refresh run.
        await asyncio.gather(*list(ds._tasks))
        assert dev.contacts_calls == 2  # refreshed behind the read
        assert (await ds.contacts())[0].name == "contact-2"  # now serving the fresh list

    asyncio.run(run())


def test_self_info_refreshes_in_background_past_ttl() -> None:
    """A node with a GPS moves itself, so past its TTL our own self-info is read again.

    The read past the TTL still answers at once with the copy in hand; the next screen gets
    the position the node reports now.
    """
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        first = await ds.self_info()
        assert await ds.self_info() is first and dev.self_info_calls == 1  # within the TTL
        ds._self_info_at = time.monotonic() - _SELF_INFO_TTL_S - 1
        assert await ds.self_info() is first  # served at once, not blocked on the re-read
        await asyncio.gather(*list(ds._tasks))
        assert dev.self_info_calls == 2
        assert await ds.self_info() is not first  # the fresh copy from here on

    asyncio.run(run())


def test_heard_time_takes_the_later_of_the_device_and_our_own_receptions() -> None:
    """A contact's heard time is the latest of the device's advert time and our evidence.

    The device's ``last_advert`` is stamped by the *sender's* clock, so it is hearsay: it can
    be absent (refused upstream by ``models.advert_time``), or plausible-looking yet days
    stale because the node's RTC runs behind. Our own history is first-hand — stamped when we
    received something — so the merge takes whichever is later. A device time still wins
    whenever the firmware caught an advert we never recorded.
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
        assert by_name["Behind-Clock"].last_seen == fresh  # our proof beats a stale stamp
        assert by_name["Ahead"].last_seen == fresh  # a newer device time still stands
        assert by_name["Quiet"].last_seen is None  # truly never heard stays never

    asyncio.run(run())


def test_a_direct_message_counts_as_hearing_its_sender() -> None:
    """An inbound DM updates the heard time — "heard" means received from.

    A direct message never touches the firmware's ``last_advert`` and is stored as a message
    rather than an observation, so a node we actively chat with could read days stale (and be
    swept by the archive ladder's quiet rungs) while talking to us. The peer prefix the wire
    addressed need not match the contact table's width, so either may be the shorter.
    """
    dev = FakeDevice()
    stale = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    messaged_at = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    dev.contact_rows = [
        Contact(name="Chatty", public_key="ab" * 32, key_prefix="ab" * 6, last_seen=stale),
        Contact(name="Silent", public_key="cd" * 32, key_prefix="cd" * 6, last_seen=stale),
    ]
    # A six-hex peer against a twelve-hex contact id: the shorter one is the wire's.
    ds = _devstate(dev, messaged={"ababab": messaged_at})

    async def run() -> None:
        by_name = {c.name: c for c in await ds.contacts()}
        assert by_name["Chatty"].last_seen == messaged_at
        assert by_name["Silent"].last_seen == stale  # nobody else is credited

    asyncio.run(run())


def test_a_naive_stored_stamp_still_compares() -> None:
    """A tz-less row from an older build reads as the UTC the storage contract says it is.

    Comparing a naive datetime against an aware one raises, which would take the whole
    contacts fetch down rather than one lane.
    """
    dev = FakeDevice()
    stale = datetime(2026, 7, 25, 9, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 7, 29, 12, 0)  # written before timestamps carried a zone
    dev.contact_rows = [
        Contact(name="Legacy", public_key="aa" * 32, key_prefix="aa" * 6, last_seen=stale)
    ]
    ds = _devstate(dev, heard={"aa" * 6: naive})

    async def run() -> None:
        assert (await ds.contacts())[0].last_seen == naive.replace(tzinfo=timezone.utc)

    asyncio.run(run())


def test_a_history_read_failure_leaves_the_contacts_untouched() -> None:
    """The merge is best-effort: a broken history read never blocks a contacts fetch."""
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
    """A forced read always re-fetches, for the rare caller that needs it fresh."""
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        await ds.contacts()
        await ds.contacts(force=True)

    asyncio.run(run())
    assert dev.contacts_calls == 2


def test_invalidation_forces_a_re_read() -> None:
    """Each invalidate drops exactly its own entry so the next read re-fetches it."""
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        await ds.self_info()
        await ds.path_hash_mode()
        await ds.channel_slots()
        await ds.contacts()
        # invalidate_config drops self-info + path-hash mode together (the config editor's write).
        ds.invalidate_config()
        await ds.self_info()
        await ds.path_hash_mode()
        assert dev.self_info_calls == 2 and dev.mode_calls == 2
        # channels and contacts were untouched by that invalidation.
        assert dev.channel_calls == 2 and dev.contacts_calls == 1
        ds.invalidate_channels()
        await ds.channel_slots()
        assert dev.channel_calls == 4  # probed again

    asyncio.run(run())


def test_reset_clears_everything() -> None:
    """A reconnect's reset drops the whole cache so every fact is re-read on next use."""
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
    assert dev.capacity_calls == 2  # capacity is a hardware constant, but a reconnect re-reads it


def test_channel_capacity_is_fetched_once_and_served_from_cache() -> None:
    """Capacity is a hardware constant: probed once, then reused for the session."""
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        for _ in range(3):
            assert await ds.channel_capacity() == 8

    asyncio.run(run())
    assert dev.capacity_calls == 1


def test_prewarm_fills_every_cache_off_the_read_path() -> None:
    """prewarm() warms all five facts, so the first screen open hits no wire at all."""
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        ds.prewarm()
        await asyncio.gather(*list(ds._tasks))  # let the background warm finish
        # Every cache was filled by the prewarm: self-info and the routing mode once each,
        # contacts once, the channel probe once (idx 0 ok, idx 1 rejected), capacity once.
        assert dev.self_info_calls == 1
        assert dev.mode_calls == 1
        assert dev.contacts_calls == 1
        assert dev.channel_calls == 2
        assert dev.capacity_calls == 1
        # A screen opening now is served from cache — no additional round-trips. The
        # path-hash mode belongs in that list: Contacts and Trace both read it to size the
        # key-hash highlight, and nothing else in the session warms it.
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
    """Order is the point: the reads share one link, and the probes are the slow ones.

    Both slot reads walk the firmware's slot table an index at a time and are measured in
    seconds; the facts every list screen needs are single round-trips. Warming them first
    is what stops a Contacts open a second after connect from queueing behind a slot walk.
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
    """The prewarm now reads the mode, so a screen opening beside it must not re-read."""
    dev = FakeDevice()
    ds = _devstate(dev)

    async def run() -> None:
        await asyncio.gather(ds.path_hash_mode(), ds.path_hash_mode(), ds.path_hash_mode())
        assert dev.mode_calls == 1

    asyncio.run(run())
