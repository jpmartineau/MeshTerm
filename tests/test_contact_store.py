# SPDX-License-Identifier: Apache-2.0
"""Tests for the contacts that MeshTerm remembers across sessions (``core.contact_store``).

The tests cover these parts:

- The permanent store for each device.
- The round-trip between ``RememberedContact`` and ``Contact``.
- The ``merge_contacts`` union. It shows the remembered contacts that a device no longer
  reports.
- The write-through and the merge in :class:`~meshterm.services.device_state.DeviceState`.

The integration test runs against the :class:`MockDevice` simulator. The test empties the
contact table of the simulator. This stands for a radio bridge with no firmware, which
loses its contacts when it restarts.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core.admin_store import AdminStore
from meshterm.core.config import Settings
from meshterm.core.contact_store import ContactStore, RememberedContact, merge_contacts
from meshterm.core.device_store import DeviceStore
from meshterm.core.models import (
    NODE_TYPE_CHAT,
    NODE_TYPE_REPEATER,
    Contact,
    is_direct_messageable,
    utcnow,
)
from meshterm.persistence.repository import Repository

PUB_A = "aa" * 32
PUB_B = "bb" * 32


def _contact(name: str, key_byte: str, **kw) -> Contact:
    """A live contact with a full 32-byte key. The key is one hex byte, repeated."""
    pub = key_byte * 32
    return Contact(name=name, public_key=pub, key_prefix=pub[:12], **kw)


@pytest.fixture()
def ctx(tmp_path: Path) -> AppContext:
    """An application context that uses the mock device, with the plain (console) UI surface."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "contacts.db")
    context = AppContext(
        console=Console(file=io.StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    yield context
    context.repo.close()


# --- the store ---------------------------------------------------------------


def test_store_round_trips_and_persists(tmp_path: Path) -> None:
    """Remembered contacts survive a new store instance.

    The store keeps them for each device, and it sorts them by name.
    """
    path = tmp_path / "contacts.json"
    store = ContactStore(path)
    store.remember_all(
        PUB_A, [_contact("Bob", "cc", node_type=NODE_TYPE_CHAT), _contact("Al", "dd")]
    )
    store.remember_all(PUB_B, [_contact("Carol", "ee")])

    reloaded = ContactStore(path)  # a new process reads the same file
    a = reloaded.contacts(PUB_A)
    assert [c.name for c in a] == ["Al", "Bob"]  # name order
    assert a[1].public_key == "cc" * 32 and a[1].node_type == NODE_TYPE_CHAT
    assert [c.name for c in reloaded.contacts(PUB_B)] == ["Carol"]  # separate device


def test_remember_all_upserts_and_never_drops_absent(tmp_path: Path) -> None:
    """A rename updates the contact in place. A contact that a later read does not report stays."""
    store = ContactStore(tmp_path / "contacts.json")
    store.remember_all(PUB_A, [_contact("Alice", "cc"), _contact("Bob", "dd")])
    # In a later read, the device reports only Alice, under a new name. Bob must stay.
    store.remember_all(PUB_A, [_contact("Alice-2", "cc")])

    by_key = {c.public_key: c.name for c in store.contacts(PUB_A)}
    assert by_key == {"cc" * 32: "Alice-2", "dd" * 32: "Bob"}


def test_remember_all_skips_keyless_contacts(tmp_path: Path) -> None:
    """A contact with no public key has no address, so the store does not remember it."""
    store = ContactStore(tmp_path / "contacts.json")
    store.remember_all(PUB_A, [Contact(name="Ghost", public_key="")])
    assert store.contacts(PUB_A) == []


def test_store_normalises_keys(tmp_path: Path) -> None:
    """The store matches the device key and the contact key in any case, and removes ``0x``."""
    store = ContactStore(tmp_path / "contacts.json")
    store.remember_all("AABB", [Contact(name="Al", public_key="0xCCDD")])
    remembered = store.contacts("0xaabb")
    assert remembered and remembered[0].public_key == "ccdd"


def test_store_ignores_corrupt_file(tmp_path: Path) -> None:
    """A file of garbage reads as empty, with no exception, and the store can still write it."""
    path = tmp_path / "contacts.json"
    path.write_text("not json at all", encoding="utf-8")
    store = ContactStore(path)
    assert store.contacts(PUB_A) == []
    store.remember_all(PUB_A, [_contact("Al", "cc")])  # the store recovers and writes the file
    assert [c.name for c in ContactStore(path).contacts(PUB_A)] == ["Al"]


def test_store_drops_malformed_entries(tmp_path: Path) -> None:
    """The store skips an entry that has no public key or no name. It keeps the good entries."""
    path = tmp_path / "contacts.json"
    path.write_text(
        json.dumps(
            {
                "devices": {
                    PUB_A: [
                        {"public_key": "cc" * 32, "name": "Good"},
                        {"name": "Keyless"},  # no public_key
                        {"public_key": "dd" * 32},  # no name
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    assert [c.name for c in ContactStore(path).contacts(PUB_A)] == ["Good"]


def test_forget_drops_one_contact(tmp_path: Path) -> None:
    """Forgetting removes one remembered contact and stores the change."""
    store = ContactStore(tmp_path / "contacts.json")
    store.remember_all(PUB_A, [_contact("Al", "cc"), _contact("Bob", "dd")])
    store.forget(PUB_A, "cc" * 32)
    assert [c.name for c in store.contacts(PUB_A)] == ["Bob"]


# --- RememberedContact <-> Contact -------------------------------------------


def test_remembered_contact_round_trip() -> None:
    """A live contact becomes remembered fields, and it rebuilds from them with no loss."""
    heard = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    live = _contact("Alice", "cc", node_type=NODE_TYPE_CHAT, last_seen=heard, lat=45.5, lon=-73.6)
    remembered = RememberedContact.from_contact(live)
    assert remembered.public_key == "cc" * 32 and remembered.last_advert == int(heard.timestamp())

    rebuilt = remembered.to_contact()
    assert rebuilt.name == "Alice"
    assert rebuilt.public_key == "cc" * 32 and rebuilt.key_prefix == "cc" * 6
    assert rebuilt.last_seen == heard
    assert rebuilt.node_type == NODE_TYPE_CHAT and rebuilt.lat == 45.5 and rebuilt.lon == -73.6
    assert rebuilt.route_hops is None  # a remembered contact floods until MeshTerm learns a path


def test_remembered_future_advert_rebuilds_as_never_heard() -> None:
    """An epoch in the future, remembered before this guard existed, rebuilds as unknown.

    The stored epoch goes through ``models.advert_time`` again. Thus a bad value that an old
    session wrote cannot make a contact show as "heard in the future" on a bridge.
    """
    future = int(utcnow().timestamp()) + 7 * 86400
    entry = RememberedContact(public_key="cc" * 32, name="Bogus-Clock", last_advert=future)
    assert entry.to_contact().last_seen is None


# --- merge -------------------------------------------------------------------


def test_merge_unions_remembered_the_device_forgot(tmp_path: Path) -> None:
    """The merge adds the remembered contacts that the device forgot.

    It does not copy the contacts that are present.
    """
    store = ContactStore(tmp_path / "contacts.json")
    store.remember_all(PUB_A, [_contact("Al", "cc"), _contact("Bob", "dd"), _contact("Cy", "ee")])

    live = [_contact("Al", "cc")]  # the device reports only Al now
    merged = merge_contacts(store, PUB_A, live)
    names = [c.name for c in merged]
    assert names[0] == "Al"  # the live contact is first, and the merge does not change it
    assert set(names) == {"Al", "Bob", "Cy"}  # the union adds the two forgotten contacts
    assert names.count("Al") == 1  # the merge does not copy a present contact


def test_merge_adds_nothing_when_device_reports_all(tmp_path: Path) -> None:
    """A device that reports its whole table (a radio with firmware) gets no additions."""
    store = ContactStore(tmp_path / "contacts.json")
    live = [_contact("Al", "cc"), _contact("Bob", "dd")]
    store.remember_all(PUB_A, live)
    assert len(merge_contacts(store, PUB_A, live)) == 2


# --- DeviceState integration -------------------------------------------------


async def test_devstate_remembers_and_restores_forgotten_contacts(ctx) -> None:
    """A read of the contacts remembers them.

    A device that then forgets its table still lists them.
    """
    device = await ctx.device()
    original = await ctx.devstate.contacts(force=True)  # the first read remembers the mock table
    original_keys = {c.public_key for c in original if c.public_key}
    assert original_keys  # the simulator has a contact table that is not empty

    # The bridge "restarts", and its RAM contact table is cleared. A new read finds no live contact.
    device._contacts = []
    restored = await ctx.devstate.contacts(force=True)
    restored_keys = {c.public_key for c in restored}

    # But the merge adds each remembered contact again, and each can still get messages.
    assert original_keys <= restored_keys
    messageable = {c.public_key for c in original if is_direct_messageable(c.node_type)}
    still = {c.public_key for c in restored if is_direct_messageable(c.node_type)}
    assert messageable <= still


async def test_devstate_merge_keeps_a_repeater_a_repeater(ctx) -> None:
    """A remembered repeater rebuilds with its type, so the DM filter still excludes it."""
    device = await ctx.device()
    device._contacts = [
        Contact(
            name="Big-Repeater",
            public_key="ab" * 32,
            key_prefix="ab" * 6,
            node_type=NODE_TYPE_REPEATER,
        ),
    ]
    await ctx.devstate.contacts(force=True)  # remember it
    device._contacts = []  # forget it
    restored = await ctx.devstate.contacts(force=True)
    repeater = next(c for c in restored if c.public_key == "ab" * 32)
    assert repeater.node_type == NODE_TYPE_REPEATER
    assert not is_direct_messageable(repeater.node_type)


def test_an_archived_contact_is_remembered_but_kept_out_of_the_merge(tmp_path) -> None:  # noqa: ANN001
    """Archiving frees a slot on the device, and the merge does not undo it.

    The store keeps the contact. This is the whole design of the sweep (refer to
    :mod:`meshterm.ui.sweep_screen`). The sweep
    deletes the contact from the device, because the slots of the device are limited. The
    store keeps the full contact. The merge is the problem. The normal job of this store is
    to add the remembered contacts to each list that the device does not report. For an
    archived contact, this adds it again at once, and the sweep looks as if it did nothing.
    """
    from meshterm.core.models import Contact

    store = ContactStore(tmp_path / "contacts.json")
    alice = Contact(name="Alice", public_key="aa" * 32, key_prefix="aa" * 6)
    bob = Contact(name="Bob", public_key="bb" * 32, key_prefix="bb" * 6)
    store.remember_all(PUB_A, [alice, bob])

    store.archive(PUB_A, alice, when=1_700_000_000)

    # The store keeps the full contact, with the public key that a restore needs.
    archived = store.archived(PUB_A)
    assert [c.name for c in archived] == ["Alice"]
    assert archived[0].public_key == "aa" * 32
    assert archived[0].archived_at == 1_700_000_000

    # But the union does not include it, so the slot on the device is free.
    merged = merge_contacts(store, PUB_A, [])
    assert [c.name for c in merged] == ["Bob"]

    # It survives a reload, because the store writes the mark to the file and not only to memory.
    reloaded = ContactStore(tmp_path / "contacts.json")
    assert [c.name for c in reloaded.archived(PUB_A)] == ["Alice"]
    assert [c.name for c in merge_contacts(reloaded, PUB_A, [])] == ["Bob"]


def test_restoring_clears_the_mark_and_the_contact_merges_again(tmp_path) -> None:  # noqa: ANN001
    """A restore is the exact inverse. The contact goes back into each list that did not have it."""
    from meshterm.core.models import Contact

    store = ContactStore(tmp_path / "contacts.json")
    alice = Contact(name="Alice", public_key="aa" * 32, key_prefix="aa" * 6)
    store.archive(PUB_A, alice, when=1_700_000_000)
    assert merge_contacts(store, PUB_A, []) == []

    was = store.restore(PUB_A, "aa" * 32)
    assert was is not None and was.name == "Alice"
    assert store.archived(PUB_A) == []
    assert [c.name for c in merge_contacts(store, PUB_A, [])] == ["Alice"]

    # A restore of a contact that is not archived does nothing. It is not an error.
    assert store.restore(PUB_A, "aa" * 32) is None
    assert store.restore(PUB_A, "ff" * 32) is None


def test_archiving_a_contact_the_store_never_saw_still_remembers_it(tmp_path) -> None:  # noqa: ANN001
    """The usual case: the contact of a radio with firmware is live only, until the sweep takes it.

    Archiving does an upsert first. Thus the record that a restore needs exists because the
    sweep made it. No earlier read is necessary.
    """
    from meshterm.core.models import Contact

    store = ContactStore(tmp_path / "contacts.json")
    unseen = Contact(name="Carol", public_key="cc" * 32, key_prefix="cc" * 6, lat=45.5, lon=-73.6)
    store.archive(PUB_A, unseen, when=1_700_000_000)

    archived = store.archived(PUB_A)
    assert [c.name for c in archived] == ["Carol"]
    assert (archived[0].lat, archived[0].lon) == (45.5, -73.6)


def test_a_keyless_contact_cannot_be_archived(tmp_path) -> None:  # noqa: ANN001
    """A contact with no key cannot be archived.

    A restore cannot find such a contact, so archiving it would delete it with no message.
    """
    from meshterm.core.models import Contact

    store = ContactStore(tmp_path / "contacts.json")
    store.archive(PUB_A, Contact(name="Ghost"), when=1_700_000_000)
    assert store.archived(PUB_A) == []


def test_a_lock_is_ours_and_survives_every_fresh_read_of_the_contact(tmp_path) -> None:  # noqa: ANN001
    """The device does not know about a lock, so a read of the device must never clear a lock."""
    from meshterm.core.models import Contact

    store = ContactStore(tmp_path / "contacts.json")
    alice = Contact(name="Alice", public_key="aa" * 32, key_prefix="aa" * 6)
    bob = Contact(name="Bob", public_key="bb" * 32, key_prefix="bb" * 6)
    # A lock on a contact that the store did not know does an upsert, the same as archiving.
    store.set_locked(PUB_A, alice, True)
    assert store.is_locked(PUB_A, "aa" * 32)
    assert store.locked_keys(PUB_A) == frozenset({"aa" * 32})

    # A new read, with a rename, keeps the lock and changes the other fields.
    store.remember_all(PUB_A, [Contact(name="Alice II", public_key="aa" * 32), bob])
    assert store.is_locked(PUB_A, "aa" * 32)
    assert not store.is_locked(PUB_A, "bb" * 32)
    assert [c.name for c in store.contacts(PUB_A)] == ["Alice II", "Bob"]

    # The store writes the lock to the file, and the lock is only for the device that it was set on.
    reloaded = ContactStore(tmp_path / "contacts.json")
    assert reloaded.locked_keys(PUB_A) == frozenset({"aa" * 32})
    assert reloaded.locked_keys("ff" * 32) == frozenset()

    reloaded.set_locked(PUB_A, alice, False)
    assert ContactStore(tmp_path / "contacts.json").locked_keys(PUB_A) == frozenset()


def test_an_archived_or_keyless_contact_takes_no_lock(tmp_path) -> None:  # noqa: ANN001
    """A contact that is archived or has no key takes no lock.

    The lock keeps a live contact off the archive path.
    """
    from meshterm.core.models import Contact

    store = ContactStore(tmp_path / "contacts.json")
    alice = Contact(name="Alice", public_key="aa" * 32, key_prefix="aa" * 6)
    store.archive(PUB_A, alice, when=1_700_000_000)
    store.set_locked(PUB_A, alice, True)
    assert store.locked_keys(PUB_A) == frozenset()

    store.set_locked(PUB_A, Contact(name="Ghost"), True)
    assert store.locked_keys(PUB_A) == frozenset()
