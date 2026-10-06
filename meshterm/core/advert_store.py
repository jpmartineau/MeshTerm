# SPDX-License-Identifier: Apache-2.0
"""Storage for the weekly flood advert: the time when the week of each device started.

A MeshCore companion never sends an advert by itself. It has no timer, and it does not
send an advert at boot. Only a request from an app (``CMD_SEND_SELF_ADVERT``) or a press
on the menu of the device itself makes it transmit an advert. Thus, if nobody remembers to
send an advert for a node, that node slowly goes out of the contact lists of all the other
nodes. The ``weekly_flood_advert`` preference is the MeshTerm solution, and we made it
economical on purpose. It sends a maximum of one flood advert each week from one device,
and only after that device has had no flood advert for a full week (refer to
:class:`~meshterm.services.advert_scheduler.AdvertScheduler`, which sends it).

This store keeps the data that this rule must have, and no other data:

* **The time when the preference was turned on.** When the preference is turned on, the
  week starts, but MeshTerm does not send an advert. Thus a week must pass before the
  first automatic advert from a device.
* **For each device** (with its public key as the key), **the time when its week
  started.** This is the time of the last flood advert from the device. It is not
  important who asked for that advert. If there is no such advert, it is the first time
  that MeshTerm connected to the device.

The advert of a device is due one week after the later of these two times. The clock is
real time, not run time. Thus a week in which the app is closed counts. Also, if an advert
is due and the app stops before it sends the advert, the advert is still due the next time
that the device connects.

This data is global state of the machine, the same as the remembered devices
(:mod:`meshterm.core.device_store`). It is in a small JSON file
(``<config_dir>/adverts.json``), not in the SQLite database of each invocation. The store
reads the file into typed records, and writes the file from these records (refer to
:class:`AdvertStore`). A file in an older shape (the per-device cadences that this store
replaced) is read as empty. Then each device starts its week again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar

from .atomicwrite import write_atomically
from .models import utcnow

#: The time that a device must be without a flood advert before the weekly advert is due.
WEEK = timedelta(days=7)

#: The quiet periods that the ``advert_quiet_s`` preference offers, in seconds. A quiet
#: period is the time that the air must have no packet (heard or sent) before MeshTerm
#: sends the due advert.
QUIET_CHOICES_S = (5, 15, 30, 60)

#: The default quiet period, in seconds.
DEFAULT_QUIET_S = 30


@dataclass(slots=True)
class _Device:
    """The record of one device: the time when its week started.

    Attributes:
        since: The time of the last flood advert from the device, or of its first
            connection, whichever is later. The week of the device starts at this time.
    """

    since: datetime | None = None

    #: Fields that this record held before. The record must never hold them again with a
    #: different meaning (refer to :data:`meshterm.core.preferences.RETIRED` for the
    #: reason that a name is never used again).
    RETIRED: ClassVar[frozenset[str]] = frozenset(
        {
            # The per-device cadences that the weekly flood advert replaced.
            "direct_hours",
            "flood_hours",
            "last_direct",
            "last_flood",
        }
    )

    @classmethod
    def read(cls, raw: object) -> _Device:
        """Build a record from the JSON of the file, with only the fields that it knows."""
        record = raw if isinstance(raw, dict) else {}
        return cls(since=_as_time(record.get("since")))

    def to_json(self) -> dict:
        """The record as the store writes it: its own fields, and no other field it read."""
        return {"since": _stamp(self.since)}


@dataclass(slots=True)
class _Document:
    """The full file: the switch-on time, and the record of each device by public key.

    Attributes:
        enabled_since: The time when the weekly advert was last switched on. ``None``
            while it is off.
        devices: The record of each device, with its normalized public key as the key.
    """

    enabled_since: datetime | None = None
    devices: dict[str, _Device] = field(default_factory=dict)

    #: Top-level fields that this document held before. There are none yet (refer to
    #: :attr:`_Device.RETIRED`).
    RETIRED: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def read(cls, raw: object) -> _Document:
        """Build the document from the JSON of the file, with only the fields that it knows.

        A file without a ``devices`` map is from before this shape (the per-device
        cadences). The method reads it as empty. It does not search the file for fields
        that look the same.
        """
        if not isinstance(raw, dict) or not isinstance(raw.get("devices"), dict):
            return cls()
        return cls(
            enabled_since=_as_time(raw.get("enabled_since")),
            devices={str(k): _Device.read(v) for k, v in raw["devices"].items()},
        )

    def to_json(self) -> dict:
        """The document as the store writes it: only the known fields.

        Thus an old field that the store does not know is removed at the next write.
        """
        return {
            "enabled_since": _stamp(self.enabled_since),
            "devices": {key: device.to_json() for key, device in self.devices.items()},
        }


class AdvertStore:
    """Reads and writes the clock of the weekly advert: the switch-on time, and each device's.

    The store reads the file into typed records, and writes the file from these records.
    It never writes back the raw JSON that it read. Thus a field that no record knows (a
    retired field, or a field that a hand edit added) is removed at the next write.
    """

    def __init__(self, path: Path) -> None:
        """Open the store on the location of a JSON file.

        Args:
            path: The path to the JSON state file. The store makes the file at the first
                write.
        """
        self._path = path

    @staticmethod
    def _key(public_key: str) -> str:
        """Normalize the public key of a device to the storage key."""
        return public_key.lower().removeprefix("0x")

    def _load(self) -> _Document:
        """Read the document of the file, or an empty one if the file is bad or missing.

        A bad file is a corrupt file, or a file in an old shape.
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _Document()
        return _Document.read(raw)

    def _write(self, document: _Document) -> None:
        """Write ``document`` atomically (after a crash during the write, the old file stays)."""
        write_atomically(self._path, json.dumps(document.to_json(), indent=2))

    # -- the switch ---------------------------------------------------------------------

    def enabled_since(self) -> datetime | None:
        """The time when the weekly advert was last turned on, or ``None`` while it is off."""
        return self._load().enabled_since

    def set_enabled(self, on: bool, when: datetime | None = None) -> None:
        """Store that the preference is turned on (this starts the week) or off.

        When the preference is turned on, the clock always starts again, also when the
        store already has it as on. The call means "it was switched on just now". A
        switch-on never sends an advert.

        Args:
            on: Whether the weekly advert is now on.
            when: The switch-on time (the default is now). The method ignores it when the
                preference is turned off.
        """
        document = self._load()
        if on:
            document.enabled_since = when or utcnow()
        elif document.enabled_since is not None:
            document.enabled_since = None
        else:
            return  # already off, thus nothing to write
        self._write(document)

    def sync_enabled(self, on: bool) -> None:
        """Make the stored switch agree with the preference, if they are different.

        The Preferences page and ``preferences set`` store the switch when it changes
        (refer to :meth:`set_enabled`). This method catches the one path that these two do
        not see: a hand edit of ``preferences.toml``. Thus, if the preference is on and has
        no switch-on time, its week starts now, not never.
        """
        recorded = self.enabled_since() is not None
        if on != recorded:
            self.set_enabled(on)

    # -- the devices ----------------------------------------------------------------------

    def week_began(self, public_key: str) -> datetime | None:
        """The time when the week of a device started, or ``None`` if MeshTerm never marked it."""
        device = self._load().devices.get(self._key(public_key))
        return device.since if device is not None else None

    def arm(self, public_key: str, when: datetime | None = None) -> None:
        """Start the week of a device at its first connection, and send nothing.

        The method writes only for a device that has no mark yet. Thus it is safe to call
        it at each connection.

        Args:
            public_key: The public key of the device (hex).
            when: The start of the week (the default is now).
        """
        if self.week_began(public_key) is None:
            self.mark_flood(public_key, when=when)

    def mark_flood(self, public_key: str, when: datetime | None = None) -> None:
        """Store a flood advert from a device, and start its week again.

        MeshTerm calls this method for the weekly advert, and also for a flood advert that
        the user sends by hand. Thus the next automatic advert is never less than a week
        after the last advert of either type.

        Args:
            public_key: The public key of the device (hex).
            when: The send time (the default is now).
        """
        document = self._load()
        document.devices[self._key(public_key)] = _Device(since=when or utcnow())
        self._write(document)

    # -- the verdict ----------------------------------------------------------------------

    def due_at(self, public_key: str) -> datetime | None:
        """The time when the weekly advert of a device is due, or ``None`` while it cannot be.

        The value is ``None`` while the weekly advert is off, and for a device that is not
        armed yet.

        Args:
            public_key: The public key of the device (hex).
        """
        document = self._load()
        device = document.devices.get(self._key(public_key))
        if document.enabled_since is None or device is None or device.since is None:
            return None
        return max(document.enabled_since, device.since) + WEEK

    def due(self, public_key: str, now: datetime | None = None) -> bool:
        """Whether the weekly advert of a device is due at ``now`` (the default is now)."""
        at = self.due_at(public_key)
        return at is not None and (now or utcnow()) >= at


def _stamp(when: datetime | None) -> str | None:
    """A timestamp as the file stores it: ISO-8601, or ``null`` if there is no time."""
    return when.isoformat() if when is not None else None


def _as_time(value: object) -> datetime | None:
    """Parse a stored ISO-8601 timestamp, or return ``None`` if it is absent or corrupt.

    The function also treats a timestamp without a time zone as corrupt. The store writes
    only aware UTC timestamps, and a naive timestamp makes the ``due`` arithmetic wrong.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
