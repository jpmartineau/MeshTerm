# SPDX-License-Identifier: Apache-2.0
"""Persistence for the weekly flood advert: when each device's week began.

A MeshCore companion never advertises by itself — no timer, not even at boot; only an app
asking (``CMD_SEND_SELF_ADVERT``) or a press on the device's own menu puts one on the air.
So a node that nobody remembers to advertise slowly drops out of everyone else's contact
list. The ``weekly_flood_advert`` preference is MeshTerm's answer, and deliberately a
frugal one: at most one flood advert a week from any one device, and only once that device
has gone a full week without one (see
:class:`~meshterm.services.advert_scheduler.AdvertScheduler`, which sends it).

This store remembers what that rule needs, and nothing else:

* **when the preference was turned on** — turning it on starts the week rather than
  sending, so a week has to pass before the first automatic advert from any device;
* **per device** (keyed by its public key), **when its week began** — the last flood
  advert that went out from it, whoever asked for it, or failing that the first time
  MeshTerm connected to it.

A device's advert is due one week after the *latest* of those instants. The clock is real
time rather than run time, so a week with the app closed counts, and a due advert that the
app did not live to send is still due the next time that device connects.

Like the remembered devices (:mod:`meshterm.core.device_store`), this is global machine
state in a small JSON file (``<config_dir>/adverts.json``) rather than the per-invocation
SQLite database, read into typed records and written back out of them (see
:class:`AdvertStore`). A file in an older shape (the per-device cadences this replaced)
reads as empty: every device simply starts its week again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar

from .atomicwrite import write_atomically
from .models import utcnow

#: How long a device must go without a flood advert before the weekly one is due.
WEEK = timedelta(days=7)

#: The quiet spells the ``advert_quiet_s`` preference offers, in seconds: how long the air
#: must go without a packet, heard or sent, before the due advert goes out.
QUIET_CHOICES_S = (5, 15, 30, 60)

#: The quiet spell waited for by default, in seconds.
DEFAULT_QUIET_S = 30


@dataclass(slots=True)
class _Device:
    """One device's record: when its week began.

    Attributes:
        since: The last flood advert from it, or its first connection, whichever is
            later — the instant its week counts from.
    """

    since: datetime | None = None

    #: Fields this record once held and must never hold again under another meaning (see
    #: :data:`meshterm.core.preferences.RETIRED` for why a name is never reused).
    RETIRED: ClassVar[frozenset[str]] = frozenset(
        {
            # The per-device cadences the weekly flood advert replaced.
            "direct_hours",
            "flood_hours",
            "last_direct",
            "last_flood",
        }
    )

    @classmethod
    def read(cls, raw: object) -> _Device:
        """Build a record from the file's JSON, taking only the fields it knows."""
        record = raw if isinstance(raw, dict) else {}
        return cls(since=_as_time(record.get("since")))

    def to_json(self) -> dict:
        """The record as written: its own fields and nothing it happened to be read with."""
        return {"since": _stamp(self.since)}


@dataclass(slots=True)
class _Document:
    """The whole file: the switch-on instant, and every device's record by public key.

    Attributes:
        enabled_since: When the weekly advert was last switched on; ``None`` while off.
        devices: Each device's record, keyed by its normalized public key.
    """

    enabled_since: datetime | None = None
    devices: dict[str, _Device] = field(default_factory=dict)

    #: Top-level fields this document once held; none yet (see :attr:`_Device.RETIRED`).
    RETIRED: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def read(cls, raw: object) -> _Document:
        """Build the document from the file's JSON, taking only the fields it knows.

        A file without a ``devices`` map is from before this shape — the per-device
        cadences — and reads as empty rather than being mined for look-alike fields.
        """
        if not isinstance(raw, dict) or not isinstance(raw.get("devices"), dict):
            return cls()
        return cls(
            enabled_since=_as_time(raw.get("enabled_since")),
            devices={str(k): _Device.read(v) for k, v in raw["devices"].items()},
        )

    def to_json(self) -> dict:
        """The document as written: known fields only, so a stale one dies at the next save."""
        return {
            "enabled_since": _stamp(self.enabled_since),
            "devices": {key: device.to_json() for key, device in self.devices.items()},
        }


class AdvertStore:
    """Reads and writes the weekly-advert clock: the switch-on instant, and each device's.

    The file is read into typed records and written back out of them — never round-tripped
    as the raw JSON it was read as — so a field no record knows (a retired one, one a hand
    edit invented) is gone after the next write.
    """

    def __init__(self, path: Path) -> None:
        """Open the store against a JSON file location.

        Args:
            path: Path to the JSON state file (created lazily on first write).
        """
        self._path = path

    @staticmethod
    def _key(public_key: str) -> str:
        """Normalize a device public key into the storage key."""
        return public_key.lower().removeprefix("0x")

    def _load(self) -> _Document:
        """Read the file's document; an empty one when it is missing, corrupt or outdated."""
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _Document()
        return _Document.read(raw)

    def _write(self, document: _Document) -> None:
        """Persist ``document`` atomically (a crash mid-write keeps the previous file)."""
        write_atomically(self._path, json.dumps(document.to_json(), indent=2))

    # -- the switch ---------------------------------------------------------------------

    def enabled_since(self) -> datetime | None:
        """When the weekly advert was last turned on, or ``None`` while it is off."""
        return self._load().enabled_since

    def set_enabled(self, on: bool, when: datetime | None = None) -> None:
        """Record the preference being turned on (starting the week) or off.

        Turning it on always restarts the clock, even when it was already recorded as on:
        the call means "it was switched on just now", and switching on never sends.

        Args:
            on: Whether the weekly advert is now on.
            when: The switch-on instant (defaults to now); ignored when turning off.
        """
        document = self._load()
        if on:
            document.enabled_since = when or utcnow()
        elif document.enabled_since is not None:
            document.enabled_since = None
        else:
            return  # already off; nothing to write
        self._write(document)

    def sync_enabled(self, on: bool) -> None:
        """Bring the recorded switch into line with the preference, where they disagree.

        The Preferences page and ``preferences set`` record the switch as it happens (see
        :meth:`set_enabled`); this catches the one path neither sees, a hand edit of
        ``preferences.toml``, so a preference found on with no switch-on instant starts its
        week now instead of never.
        """
        recorded = self.enabled_since() is not None
        if on != recorded:
            self.set_enabled(on)

    # -- the devices ----------------------------------------------------------------------

    def week_began(self, public_key: str) -> datetime | None:
        """When a device's own week began, or ``None`` if MeshTerm has never marked it."""
        device = self._load().devices.get(self._key(public_key))
        return device.since if device is not None else None

    def arm(self, public_key: str, when: datetime | None = None) -> None:
        """Start a device's week on its first connection, without sending anything.

        Only writes for a device with no mark yet, so it is safe to call on every connect.

        Args:
            public_key: The device's public key (hex).
            when: The week's start (defaults to now).
        """
        if self.week_began(public_key) is None:
            self.mark_flood(public_key, when=when)

    def mark_flood(self, public_key: str, when: datetime | None = None) -> None:
        """Record a flood advert from a device, restarting its week.

        Called for the weekly advert and for a flood advert sent by hand alike, so the
        next automatic one is never less than a week after the last of either.

        Args:
            public_key: The device's public key (hex).
            when: The send time (defaults to now).
        """
        document = self._load()
        document.devices[self._key(public_key)] = _Device(since=when or utcnow())
        self._write(document)

    # -- the verdict ----------------------------------------------------------------------

    def due_at(self, public_key: str) -> datetime | None:
        """When a device's weekly advert falls due, or ``None`` while it cannot.

        ``None`` while the weekly advert is off, and for a device not yet armed.

        Args:
            public_key: The device's public key (hex).
        """
        document = self._load()
        device = document.devices.get(self._key(public_key))
        if document.enabled_since is None or device is None or device.since is None:
            return None
        return max(document.enabled_since, device.since) + WEEK

    def due(self, public_key: str, now: datetime | None = None) -> bool:
        """Whether a device's weekly advert is due at ``now`` (defaults to now)."""
        at = self.due_at(public_key)
        return at is not None and (now or utcnow()) >= at


def _stamp(when: datetime | None) -> str | None:
    """A timestamp as the file stores it: ISO-8601, or ``null`` for absent."""
    return when.isoformat() if when is not None else None


def _as_time(value: object) -> datetime | None:
    """Parse a stored ISO-8601 timestamp, or ``None`` if absent/corrupt.

    A timestamp without a zone is treated as corrupt too: the store only ever writes
    aware UTC stamps, and a naive one would poison the ``due`` arithmetic.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
