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
SQLite database. A file in an older shape (the per-device cadences this replaced) reads as
empty: every device simply starts its week again.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .atomicwrite import write_atomically
from .models import utcnow

#: How long a device must go without a flood advert before the weekly one is due.
WEEK = timedelta(days=7)

#: The quiet spells the ``advert_quiet_s`` preference offers, in seconds: how long the air
#: must go without a packet, heard or sent, before the due advert goes out.
QUIET_CHOICES_S = (5, 15, 30, 60)

#: The quiet spell waited for by default, in seconds.
DEFAULT_QUIET_S = 30


class AdvertStore:
    """Reads and writes the weekly-advert clock: the switch-on instant, and each device's."""

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

    def _load(self) -> dict:
        """Return the file's document, or an empty one when missing, corrupt or outdated."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or not isinstance(data.get("devices"), dict):
            return {}
        return data

    def _write(self, data: dict) -> None:
        """Persist ``data`` atomically (a crash mid-write keeps the previous file)."""
        write_atomically(self._path, json.dumps(data, indent=2))

    # -- the switch ---------------------------------------------------------------------

    def enabled_since(self) -> datetime | None:
        """When the weekly advert was last turned on, or ``None`` while it is off."""
        return _as_time(self._load().get("enabled_since"))

    def set_enabled(self, on: bool, when: datetime | None = None) -> None:
        """Record the preference being turned on (starting the week) or off.

        Turning it on always restarts the clock, even when it was already recorded as on:
        the call means "it was switched on just now", and switching on never sends.

        Args:
            on: Whether the weekly advert is now on.
            when: The switch-on instant (defaults to now); ignored when turning off.
        """
        data = self._load()
        data.setdefault("devices", {})
        if on:
            data["enabled_since"] = (when or utcnow()).isoformat()
        elif "enabled_since" in data:
            del data["enabled_since"]
        else:
            return  # already off; nothing to write
        self._write(data)

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
        record = self._load().get("devices", {}).get(self._key(public_key))
        return _as_time(record.get("since")) if isinstance(record, dict) else None

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
        data = self._load()
        devices = data.setdefault("devices", {})
        devices[self._key(public_key)] = {"since": (when or utcnow()).isoformat()}
        self._write(data)

    # -- the verdict ----------------------------------------------------------------------

    def due_at(self, public_key: str) -> datetime | None:
        """When a device's weekly advert falls due, or ``None`` while it cannot.

        ``None`` while the weekly advert is off, and for a device not yet armed.

        Args:
            public_key: The device's public key (hex).
        """
        data = self._load()
        enabled = _as_time(data.get("enabled_since"))
        record = data.get("devices", {}).get(self._key(public_key))
        began = _as_time(record.get("since")) if isinstance(record, dict) else None
        if enabled is None or began is None:
            return None
        return max(enabled, began) + WEEK

    def due(self, public_key: str, now: datetime | None = None) -> bool:
        """Whether a device's weekly advert is due at ``now`` (defaults to now)."""
        at = self.due_at(public_key)
        return at is not None and (now or utcnow()) >= at


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
