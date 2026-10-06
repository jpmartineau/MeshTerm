# SPDX-License-Identifier: Apache-2.0
"""A store for the contacts that MeshTerm heard, so that they stay when a device forgets them.

Most companions keep their contact table in firmware. Thus MeshTerm reads the contacts live
from the device, and it does not have to remember them. A radio bridge without firmware is the
exception. It keeps its contact table only in RAM, so all the contacts are gone when the bridge
process starts again. Then the Contacts screen and the list of chat recipients are empty, or
they show only the nodes whose adverts MeshTerm heard in this session. This occurs although
you sent messages to those nodes in the last session.

This store is the durable backup for exactly that case. Each time MeshTerm reads the contacts
of the device, it stores them here, under the public key of the device itself. Thus two
devices keep separate sets. When MeshTerm draws a list, :func:`merge_contacts` makes the union
of the live contacts of the device and each remembered contact that the device does not report
now. Thus a device that forgets still shows the nodes that you know, and you can still send
messages to them.

A direct message addresses a node by its key prefix (the first six bytes of its public key),
and the remembered contact has the full key. Thus MeshTerm does not have to write anything
back to the device to reach these nodes.

The union has no effect where it is not necessary. A companion with firmware always reports
its full table. Thus each remembered contact is already present, and the union adds nothing.
No check of the device type is necessary. The store never removes a contact because a device
read is empty. Thus a bridge that started again does not delete the memory of what it knew.

A contact can also be **archived**. This is the opposite arrangement, and the Contacts sweep
uses it (refer to :mod:`~meshterm.core.contact_score`). MeshTerm deletes the contact from the
device, which makes a slot free in the limited contact table of a companion. The store keeps
the contact here with an :attr:`~RememberedContact.archived_at` stamp, so no information about
it is lost. :func:`merge_contacts` skips an archived contact. Without this, the union above
puts it back into each list immediately, the user cannot tell it from a live contact, and the
sweep seems to do nothing.

An archive keeps all the data except the device row: the full reception history of the node
(not changed, because it is in the SQLite database, not here), its direct-message transcript
(stored under the key prefix, not under a contact row), and its full public key.
The full key is exactly what :meth:`~meshterm.core.connection.Device.add_contact` must have
to put the contact back. Thus a restore is one write. The chat screen already does this write
when the device rejects a send to an unknown recipient (refer to
:class:`~meshterm.core.connection.ContactNotOnDeviceError`).

A live contact can also be **locked**. This is the decision of the user that the contact
stays. A locked contact is never a candidate for a sweep (refer to
:data:`~meshterm.core.contact_score.PROTECT_LOCKED`), and its page does not offer Archive. The
flag is in this store, not on the device, because the firmware has no such field. It is a
statement of MeshTerm about the contact, kept next to the other statement that MeshTerm makes
(the archive stamp). A device read never clears it.

Be careful with a radio bridge without firmware. Its contact table is only in RAM, and for
this bridge, this store is the memory. An archive there removes the contact from the lists
completely, and only a restore brings it back.

This store is global machine state in a small JSON file (``<config_dir>/contacts.json``), the
same as the other user state (mutes, remembered channels, saved settings). It is not in the
per-run SQLite database. After the first read of the file, reads come from memory. A write
occurs only when the set of contacts changes, and the write is atomic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar

from .atomicwrite import write_atomically
from .models import Contact, advert_time


def _norm(pubkey: str) -> str:
    """Normalize a public key (of a device or a contact) to the lowercase hex of the store."""
    return (pubkey or "").lower().removeprefix("0x")


def _opt_int(value: object) -> int | None:
    """Coerce a JSON value to ``int``, or ``None`` if it is absent or cannot be parsed."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value: object) -> float | None:
    """Coerce a JSON value to ``float``, or ``None`` if it is absent or cannot be parsed."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class RememberedContact:
    """A remembered contact of a device: enough data to list it and to send a message to it.

    Attributes:
        public_key: The full public key of the contact (lowercase hex). A direct message
            addresses the contact by this key, so with this key, you can send messages to
            a remembered contact.
        name: The advertised name of the node.
        node_type: The advert type (refer to the ``NODE_TYPE_*`` constants). Thus the filter
            of the chat picker for direct messages works the same as for a live contact.
        last_advert: The Unix seconds of the most recent advert of the node when it was last
            heard, for the last-heard column of the list. ``None`` when it is not known.
        lat: The last advertised latitude, if the node shared one.
        lon: The last advertised longitude, if the node shared one.
        archived_at: The Unix seconds when a sweep removed this contact from the device, or
            ``None`` while it is a live contact. The store remembers an archived contact
            completely, but keeps it out of :func:`merge_contacts`. Thus the contact does not
            use a device slot, but MeshTerm does not forget it. With the stamp, a list can
            show how long ago the contact went.
        locked: Whether the user locked this contact against an archive. This is the state
            of MeshTerm, not of the device. Thus :meth:`ContactStore.remember_all` keeps it
            at each new read of the contact, and does not reset it.
    """

    public_key: str
    name: str
    node_type: int | None = None
    last_advert: int | None = None
    lat: float | None = None
    lon: float | None = None
    archived_at: int | None = None
    locked: bool = False

    #: The fields that this record once held and must never hold again with a different
    #: meaning (refer to :data:`meshterm.core.preferences.RETIRED` for the reason why a name
    #: is never used again).
    RETIRED: ClassVar[frozenset[str]] = frozenset()

    @property
    def archived(self) -> bool:
        """Whether a sweep removed this contact from the device, and the store kept it here."""
        return self.archived_at is not None

    @classmethod
    def from_contact(cls, contact: Contact) -> RememberedContact:
        """Reduce a live :class:`~meshterm.core.models.Contact` to the fields that we store."""
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
        """Build a :class:`~meshterm.core.models.Contact` again, for the merged list.

        The learned route is removed (``route_hops=None``). A remembered contact floods until
        the device learns a path again from received traffic, exactly as a newly heard
        contact does. The stored epoch goes back in through
        :func:`~meshterm.core.models.advert_time`. Thus, if the store remembered an advert
        with a future time before the clock of its sender was corrected, the time is
        unknown. It does not come back as "heard in the future".
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
    """Reads and writes the set of remembered contacts of each device, from memory first.

    Use :meth:`contacts` (the remembered contacts of a device) and :meth:`remember_all`
    (store the contacts just read from a device). :meth:`remember_all` is an upsert that
    never removes a contact because it is absent. The store reads the backing map one time,
    at the first access, and keeps it in memory. A change writes the full map atomically, and
    only when something changed.
    """

    def __init__(self, path: Path) -> None:
        """Open the store on a JSON file location.

        Args:
            path: The path to the JSON state file (created lazily at the first remembered
                contact).
        """
        self._path = path
        self._devices: dict[str, dict[str, RememberedContact]] | None = None

    @property
    def _state(self) -> dict[str, dict[str, RememberedContact]]:
        """The map of device -> {contact key -> remembered contact}, read at the first access."""
        if self._devices is None:
            self._devices = self._load()
        return self._devices

    def _load(self) -> dict[str, dict[str, RememberedContact]]:
        """Parse the file into the device map.

        The map is empty if the file is missing or corrupt.
        """
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
        """The contacts remembered for a device (a copy, sorted by name), or an empty list."""
        remembered = self._state.get(_norm(device_pubkey), {})
        return sorted(remembered.values(), key=lambda c: (c.name.lower(), c.public_key))

    def remember_all(self, device_pubkey: str, contacts: list[Contact]) -> None:
        """Store the contacts just read from a device, with an upsert by public key.

        The store keeps a contact that is new to the device, or one whose fields changed (a
        new name, a newer advert). It does not touch a contact that did not change. A contact
        that the device does not report now is **kept**. Thus an empty read from a device that
        forgets never deletes what the device knew before. The store writes one time, and only
        if something changed.

        Args:
            device_pubkey: The public key of the device itself.
            contacts: The contacts just read from that device.
        """
        dev = _norm(device_pubkey)
        if not dev:
            return
        current = dict(self._state.get(dev, {}))
        changed = False
        for contact in contacts:
            if not contact.public_key:
                continue  # no key to address it: nothing to remember it by
            remembered = RememberedContact.from_contact(contact)
            known = current.get(remembered.public_key)
            if known is not None and known.locked:
                # The lock is ours, not the device's: a new read knows nothing about it.
                remembered = replace(remembered, locked=True)
            if known != remembered:
                current[remembered.public_key] = remembered
                changed = True
        if changed:
            self._state[dev] = current
            self._save()

    def archived(self, device_pubkey: str) -> list[RememberedContact]:
        """The contacts archived from this device, the most recently archived first.

        This is the ``Archived`` section of the Contacts screen. The order is by the time of
        each sweep, not by name, because the section answers the question: what did the last
        sweep take? Thus the work of a new sweep is at the top of the section.
        """
        remembered = self._state.get(_norm(device_pubkey), {}).values()
        return sorted(
            (c for c in remembered if c.archived),
            key=lambda c: (-(c.archived_at or 0), c.name.lower()),
        )

    def archive(self, device_pubkey: str, contact: Contact, *, when: int) -> None:
        """Mark one contact as archived: remembered completely, but not merged into lists.

        This is the store part of a sweep. The device part is
        :meth:`~meshterm.core.connection.Device.remove_contact`, and this part makes sure
        that the removal is not a loss. The store first does an upsert of the contact. Thus,
        if the store did not have a record of the contact before (a contact that is only
        live, on a companion with firmware, which is the usual case), the archive still
        remembers all that is necessary to put it back.

        Args:
            device_pubkey: The public key of the device itself.
            contact: The contact that the sweep removes.
            when: The Unix seconds for the archive stamp.
        """
        dev = _norm(device_pubkey)
        if not dev or not contact.public_key:
            return  # no key to address it: nothing to restore it by
        entry = replace(RememberedContact.from_contact(contact), archived_at=int(when))
        current = dict(self._state.get(dev, {}))
        if current.get(entry.public_key) == entry:
            return
        current[entry.public_key] = entry
        self._state[dev] = current
        self._save()

    def restore(self, device_pubkey: str, contact_pubkey: str) -> RememberedContact | None:
        """Clear the archived mark of one contact, and return the archived record.

        This is only the store part. The caller writes the contact back to the device (refer
        to :meth:`~meshterm.core.connection.Device.add_contact`), and calls this method after
        that write succeeded. Thus a failed write never leaves a contact listed as live on a
        device that does not hold it.

        Args:
            device_pubkey: The public key of the device itself.
            contact_pubkey: The contact to take out of the archive.

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
        """The full keys (lowercase hex) of all the locked contacts on this device."""
        remembered = self._state.get(_norm(device_pubkey), {}).values()
        return frozenset(c.public_key for c in remembered if c.locked)

    def is_locked(self, device_pubkey: str, contact_pubkey: str) -> bool:
        """Whether one contact is locked against an archive on this device."""
        entry = self._state.get(_norm(device_pubkey), {}).get(_norm(contact_pubkey))
        return entry is not None and entry.locked

    def set_locked(self, device_pubkey: str, contact: Contact, locked: bool) -> None:
        """Lock or unlock one contact, with an upsert for a contact that the store did not have.

        Only a live contact can be locked. The lock exists to keep a contact off the archive
        path. Thus a lock on an archived contact makes a claim about a contact that is
        already gone. The store writes only a real change.

        Args:
            device_pubkey: The public key of the device itself.
            contact: The contact to lock or unlock, addressed by its full key.
            locked: The state in which to leave it.
        """
        dev = _norm(device_pubkey)
        if not dev or not contact.public_key:
            return  # no key to address it: nothing to put the flag on
        current = dict(self._state.get(dev, {}))
        key = _norm(contact.public_key)
        known = current.get(key) or RememberedContact.from_contact(contact)
        if known.archived or known.locked == locked:
            return
        current[key] = replace(known, locked=locked)
        self._state[dev] = current
        self._save()

    def forget(self, device_pubkey: str, contact_pubkey: str) -> None:
        """Remove one remembered contact, and write only a real change."""
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
        """Write the full device map atomically.

        If a crash occurs during the write, the old file stays.
        """
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
    """Serialize one remembered contact, without the fields that it does not have."""
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
    """Parse one stored contact entry, or ``None`` if it is malformed (no key or no name)."""
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
    """Join the live contacts of a device with the remembered contacts that it does not report.

    The live contacts come first, without a change. If the device already reports the key of
    a remembered contact, the function uses the live entry (the newer of the two), so nothing
    is duplicated. A companion with firmware reports its full table, so this function adds
    nothing there. A device that forgets gets the missing contacts back, built again from the
    remembered data.

    Args:
        store: The contact store from which to read the remembered contacts.
        device_pubkey: The public key of the device itself.
        live: The contacts that the device reports now.

    Returns:
        The live contacts, then the remembered contacts that the device does not have.
    """
    present = {_norm(c.public_key) for c in live if c.public_key}
    extra = [
        remembered.to_contact()
        for remembered in store.contacts(device_pubkey)
        # The merge does not add an archived contact, on purpose. A sweep removed it from the
        # device to make a slot free. If the merge adds it back, this undoes the sweep in the
        # only place where anyone looks.
        if remembered.public_key
        and not remembered.archived
        and remembered.public_key not in present
    ]
    return list(live) + extra
