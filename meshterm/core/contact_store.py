# SPDX-License-Identifier: Apache-2.0
"""Persistence for contacts heard through MeshTerm, so they survive a device that forgets them.

Most companions keep their contact table in firmware, so MeshTerm reads contacts live from the
device and never has to remember them. A firmware-less radio bridge is the exception: it holds
its contact table only in RAM, so every contact is gone when the bridge process restarts — the
Contacts screen and the chat recipient list come up empty (or only as sparse as the adverts
heard so far this session), even though you were messaging those nodes last time.

This store is the durable backup for exactly that case. Every time MeshTerm reads the device's
contacts it records them here, keyed by the device's own public key so two radios keep separate
sets. When a list is then drawn, :func:`merge_contacts` unions the device's live contacts with
any remembered contact the device isn't currently reporting — so a forgetful device still shows
the nodes you know, and they stay messageable: a direct message addresses a node by a prefix of
its public key, which the remembered contact carries, so nothing has to be written back onto the
device to reach them.

The union is inert where it isn't needed: a firmware radio always reports its whole table, so
every remembered contact is already present and nothing is added — no device-type check required.
Recording never *removes* a contact on a device's empty read, so a bridge that just restarted
doesn't wipe the memory of what it knew.

A contact can also be **archived**, which is the opposite arrangement and the one the
Contacts sweep uses (see :mod:`~meshterm.core.contact_score`): the contact is deleted from
the *device*, freeing a slot in a companion's finite contact table, and kept here with an
:attr:`~RememberedContact.archived_at` stamp so nothing about it is actually lost.
:func:`merge_contacts` skips an archived contact — without that the union above would put it
straight back into every list, indistinguishable from a live one, and the sweep would appear
to have done nothing. What survives an archive is everything except the device row: the
node's whole reception history (untouched — it lives in the SQLite database, not here), its
direct-message transcript (keyed by key prefix, not by a contact row), and its full public
key, which is exactly what :meth:`~meshterm.core.connection.Device.add_contact` needs to put
it back. So a restore is one write, and the chat screen already performs it on demand when a
send is rejected for an unknown recipient (see
:class:`~meshterm.core.connection.ContactNotOnDeviceError`).

A live contact can also be **locked**, which is the reader's own word that it stays: a
locked contact is never a sweep candidate (see
:data:`~meshterm.core.contact_score.PROTECT_LOCKED`) and its page offers no Archive. The flag
lives here rather than on the device because the firmware has no such field — it is
MeshTerm's claim about the contact, kept beside the other one it makes (the archive stamp),
and a device read never clears it.

The one place to be careful is a firmware-less bridge, whose contact table is RAM-only and
for which this store *is* the memory: archiving there removes the contact from the lists for
real, and only a restore brings it back.

Like the other operator state (mutes, remembered channels, saved settings), this is global
machine state in a small JSON file (``<config_dir>/contacts.json``), not the per-invocation
SQLite database. Reads are served from memory after the first load; a write happens only when the
contact set actually changes, and flushes atomically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar

from .atomicwrite import write_atomically
from .models import Contact, advert_time


def _norm(pubkey: str) -> str:
    """Normalise a public key (device or contact) to the lowercase hex used as a key."""
    return (pubkey or "").lower().removeprefix("0x")


def _opt_int(value: object) -> int | None:
    """Coerce a JSON value to ``int``, or ``None`` if absent/unparseable."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value: object) -> float | None:
    """Coerce a JSON value to ``float``, or ``None`` if absent/unparseable."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class RememberedContact:
    """One contact MeshTerm remembers for a device: enough to list it and address a message.

    Attributes:
        public_key: The contact's full public key (lowercase hex) — how a direct message
            addresses it, so it is what makes a remembered contact messageable.
        name: The node's advertised name.
        node_type: The advert type (see the ``NODE_TYPE_*`` constants), so the chat picker's
            direct-messageable filter keeps behaving as it would for a live contact.
        last_advert: Unix seconds of the node's most recent advert when last heard, for the
            list's last-heard column; ``None`` when unknown.
        lat: Last advertised latitude, if it shared one.
        lon: Last advertised longitude, if it shared one.
        archived_at: Unix seconds when this contact was swept off the device, or ``None``
            while it is a live contact. An archived contact is remembered in full but kept
            *out* of :func:`merge_contacts`, so it stops occupying a device slot without
            being forgotten — and the stamp is what lets a list say how long ago it went.
        locked: Whether the reader locked this contact against archiving. MeshTerm's own
            state, not the device's, so :meth:`ContactStore.remember_all` carries it across
            every fresh read of the contact rather than resetting it.
    """

    public_key: str
    name: str
    node_type: int | None = None
    last_advert: int | None = None
    lat: float | None = None
    lon: float | None = None
    archived_at: int | None = None
    locked: bool = False

    #: Fields this record once held and must never hold again under another meaning (see
    #: :data:`meshterm.core.preferences.RETIRED` for why a name is never reused).
    RETIRED: ClassVar[frozenset[str]] = frozenset()

    @property
    def archived(self) -> bool:
        """Whether this contact has been swept off the device but kept here."""
        return self.archived_at is not None

    @classmethod
    def from_contact(cls, contact: Contact) -> RememberedContact:
        """Distil a live :class:`~meshterm.core.models.Contact` into the fields we persist."""
        epoch = int(contact.last_seen.timestamp()) if contact.last_seen else None
        return cls(
            public_key=_norm(contact.public_key),
            name=contact.name,
            node_type=contact.node_type,
            last_advert=epoch,
            lat=contact.lat,
            lon=contact.lon,
        )

    def to_contact(self) -> Contact:
        """Rebuild a :class:`~meshterm.core.models.Contact` for the merged list.

        The learned route is dropped (``route_hops=None``): a remembered contact floods until
        the device relearns a path from received traffic, exactly as a freshly-heard one does.
        The stored epoch re-enters through :func:`~meshterm.core.models.advert_time`, so a
        future-stamped advert remembered before its sender's clock was corrected reads as
        unknown rather than resurfacing as "heard in the future".
        """
        last_seen = advert_time(self.last_advert)
        return Contact(
            name=self.name,
            public_key=self.public_key,
            key_prefix=self.public_key[:12],
            last_seen=last_seen,
            node_type=self.node_type,
            lat=self.lat,
            lon=self.lon,
            route_hops=None,
        )


class ContactStore:
    """Reads and writes the per-device set of remembered contacts, memory-first.

    Interact through :meth:`contacts` (a device's remembered contacts) and :meth:`remember_all`
    (record the contacts just read from a device — an upsert that never drops one on absence).
    The backing map is loaded once on first access and kept in memory; a mutation persists the
    whole map atomically, and only when something actually changed.
    """

    def __init__(self, path: Path) -> None:
        """Open the store against a JSON file location.

        Args:
            path: Path to the JSON state file (created lazily on the first remembered contact).
        """
        self._path = path
        self._devices: dict[str, dict[str, RememberedContact]] | None = None

    @property
    def _state(self) -> dict[str, dict[str, RememberedContact]]:
        """The device -> {contact key -> remembered contact} map, loaded on first access."""
        if self._devices is None:
            self._devices = self._load()
        return self._devices

    def _load(self) -> dict[str, dict[str, RememberedContact]]:
        """Parse the file into the device map, or empty on a missing/corrupt file."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        devices: dict[str, dict[str, RememberedContact]] = {}
        for pubkey, entries in (data.get("devices") or {}).items():
            if not isinstance(entries, list):
                continue
            contacts: dict[str, RememberedContact] = {}
            for entry in entries:
                contact = _contact_from_json(entry)
                if contact is not None:
                    contacts[contact.public_key] = contact
            if contacts:
                devices[_norm(str(pubkey))] = contacts
        return devices

    def contacts(self, device_pubkey: str) -> list[RememberedContact]:
        """The contacts remembered for a device (a copy, sorted by name), empty if none."""
        remembered = self._state.get(_norm(device_pubkey), {})
        return sorted(remembered.values(), key=lambda c: (c.name.lower(), c.public_key))

    def remember_all(self, device_pubkey: str, contacts: list[Contact]) -> None:
        """Record the contacts just read from a device, upserting by public key.

        A contact new to the device, or one whose fields changed (a rename, a fresher advert),
        is stored; an unchanged one is left alone. A contact the device is *no longer* reporting
        is **kept** — so a forgetful device's empty read never erases what it once knew. Persists
        once, only if anything changed.

        Args:
            device_pubkey: The device's own public key.
            contacts: The contacts just read from that device.
        """
        dev = _norm(device_pubkey)
        if not dev:
            return
        current = dict(self._state.get(dev, {}))
        changed = False
        for contact in contacts:
            if not contact.public_key:
                continue  # unaddressable — nothing to remember it by
            remembered = RememberedContact.from_contact(contact)
            known = current.get(remembered.public_key)
            if known is not None and known.locked:
                # The lock is ours, not the device's: a fresh read knows nothing about it.
                remembered = replace(remembered, locked=True)
            if known != remembered:
                current[remembered.public_key] = remembered
                changed = True
        if changed:
            self._state[dev] = current
            self._save()

    def archived(self, device_pubkey: str) -> list[RememberedContact]:
        """The contacts archived off this device, most recently archived first.

        The Contacts screen's ``Archived`` section. Ordered by when each was swept rather
        than by name, because that is the question the section answers — *what did the last
        sweep take?* — and a fresh sweep's work should be at the top of it.
        """
        remembered = self._state.get(_norm(device_pubkey), {}).values()
        return sorted(
            (c for c in remembered if c.archived),
            key=lambda c: (-(c.archived_at or 0), c.name.lower()),
        )

    def archive(self, device_pubkey: str, contact: Contact, *, when: int) -> None:
        """Mark one contact archived — remembered in full, but no longer merged into lists.

        The store half of a sweep: the device half is
        :meth:`~meshterm.core.connection.Device.remove_contact`, and this is what keeps the
        removal from being a loss. The contact is *upserted* first, so archiving one the
        store had never recorded (a live-only contact on a firmware radio, which is the
        usual case) still remembers everything needed to put it back.

        Args:
            device_pubkey: The device's own public key.
            contact: The contact being swept.
            when: Unix seconds to stamp the archive with.
        """
        dev = _norm(device_pubkey)
        if not dev or not contact.public_key:
            return  # unaddressable — there would be nothing to restore it by
        entry = replace(RememberedContact.from_contact(contact), archived_at=int(when))
        current = dict(self._state.get(dev, {}))
        if current.get(entry.public_key) == entry:
            return
        current[entry.public_key] = entry
        self._state[dev] = current
        self._save()

    def restore(self, device_pubkey: str, contact_pubkey: str) -> RememberedContact | None:
        """Clear one contact's archived mark, returning what was archived.

        Only the *store* side: the caller writes the contact back onto the device (see
        :meth:`~meshterm.core.connection.Device.add_contact`) and calls this once that
        succeeded, so a failed write never leaves a contact listed as live on a device that
        doesn't hold it.

        Args:
            device_pubkey: The device's own public key.
            contact_pubkey: The contact to un-archive.

        Returns:
            The record as it was archived, or ``None`` if no archived contact matched.
        """
        dev = _norm(device_pubkey)
        key = _norm(contact_pubkey)
        contacts = self._state.get(dev) or {}
        entry = contacts.get(key)
        if entry is None or not entry.archived:
            return None
        current = dict(contacts)
        current[key] = replace(entry, archived_at=None)
        self._state[dev] = current
        self._save()
        return entry

    def locked_keys(self, device_pubkey: str) -> frozenset[str]:
        """The full keys (lowercase hex) of every contact locked on this device."""
        remembered = self._state.get(_norm(device_pubkey), {}).values()
        return frozenset(c.public_key for c in remembered if c.locked)

    def is_locked(self, device_pubkey: str, contact_pubkey: str) -> bool:
        """Whether one contact is locked against archiving on this device."""
        entry = self._state.get(_norm(device_pubkey), {}).get(_norm(contact_pubkey))
        return entry is not None and entry.locked

    def set_locked(self, device_pubkey: str, contact: Contact, locked: bool) -> None:
        """Lock or unlock one contact, upserting it so a contact the store never saw can be.

        Only a live contact is locked — the lock exists to keep a contact *off* the archive
        path, so locking an archived one would claim something about a contact that has
        already gone. Persists only a real change.

        Args:
            device_pubkey: The device's own public key.
            contact: The contact to lock or unlock, addressed by its full key.
            locked: The state to leave it in.
        """
        dev = _norm(device_pubkey)
        if not dev or not contact.public_key:
            return  # unaddressable — nothing to hang the flag on
        current = dict(self._state.get(dev, {}))
        key = _norm(contact.public_key)
        known = current.get(key) or RememberedContact.from_contact(contact)
        if known.archived or known.locked == locked:
            return
        current[key] = replace(known, locked=locked)
        self._state[dev] = current
        self._save()

    def forget(self, device_pubkey: str, contact_pubkey: str) -> None:
        """Drop one remembered contact; persist only a real change."""
        dev = _norm(device_pubkey)
        contacts = self._state.get(dev)
        key = _norm(contact_pubkey)
        if not contacts or key not in contacts:
            return
        contacts = {k: v for k, v in contacts.items() if k != key}
        if contacts:
            self._state[dev] = contacts
        else:
            del self._state[dev]
        self._save()

    def _save(self) -> None:
        """Persist the whole device map atomically (a crash mid-write keeps the old file)."""
        data = {
            "devices": {
                pubkey: [
                    _contact_to_json(c)
                    for c in sorted(contacts.values(), key=lambda c: (c.name.lower(), c.public_key))
                ]
                for pubkey, contacts in sorted(self._state.items())
                if contacts
            }
        }
        write_atomically(self._path, json.dumps(data, indent=2))


def _contact_to_json(contact: RememberedContact) -> dict:
    """Serialise one remembered contact, omitting the fields it doesn't carry."""
    entry: dict = {"public_key": contact.public_key, "name": contact.name}
    if contact.node_type is not None:
        entry["node_type"] = contact.node_type
    if contact.last_advert is not None:
        entry["last_advert"] = contact.last_advert
    if contact.lat is not None:
        entry["lat"] = contact.lat
    if contact.lon is not None:
        entry["lon"] = contact.lon
    if contact.archived_at is not None:
        entry["archived_at"] = contact.archived_at
    if contact.locked:
        entry["locked"] = True
    return entry


def _contact_from_json(entry: object) -> RememberedContact | None:
    """Parse one stored contact entry, or ``None`` if it is malformed (no key or name)."""
    if not isinstance(entry, dict):
        return None
    pubkey = _norm(str(entry.get("public_key", "")))
    name = entry.get("name")
    if not pubkey or not isinstance(name, str):
        return None
    return RememberedContact(
        public_key=pubkey,
        name=name,
        node_type=_opt_int(entry.get("node_type")),
        last_advert=_opt_int(entry.get("last_advert")),
        lat=_opt_float(entry.get("lat")),
        lon=_opt_float(entry.get("lon")),
        archived_at=_opt_int(entry.get("archived_at")),
        locked=entry.get("locked") is True,
    )


def merge_contacts(store: ContactStore, device_pubkey: str, live: list[Contact]) -> list[Contact]:
    """Union a device's live contacts with any remembered ones it isn't currently reporting.

    Live contacts pass through unchanged and first; a remembered contact whose key the device
    already reports is left to the live entry (the fresher of the two), so nothing is
    duplicated. A firmware radio reports its whole table, so this adds nothing there; a
    forgetful device gets the missing contacts back, rebuilt from what was remembered.

    Args:
        store: The contact store to read remembered contacts from.
        device_pubkey: The device's own public key.
        live: The contacts the device is currently reporting.

    Returns:
        The live contacts followed by the remembered contacts the device is missing.
    """
    present = {_norm(c.public_key) for c in live if c.public_key}
    extra = [
        remembered.to_contact()
        for remembered in store.contacts(device_pubkey)
        # An archived contact is deliberately withheld: it was swept off the device to free
        # a slot, and merging it back would undo the sweep in the only place anyone looks.
        if remembered.public_key
        and not remembered.archived
        and remembered.public_key not in present
    ]
    return list(live) + extra
