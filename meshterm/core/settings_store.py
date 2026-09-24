# SPDX-License-Identifier: Apache-2.0
"""Persistence for device settings changed through MeshTerm, so they survive a forgetful device.

Most companions keep their configuration in firmware, so MeshTerm reads settings live from the
device each session and never has to remember them. A firmware-less radio bridge is the
exception: it holds its configuration only in RAM, so the node name, radio parameters, and
tuning you set last session are back to firmware defaults when the bridge process restarts.

This store is the durable backup for exactly that case. It records every setting *changed
through MeshTerm* (the editor, the ``config`` CLI, and a TOML restore all funnel through
:func:`~meshterm.tools.config._apply_setting`), keyed by the device's own public key so two
radios keep separate sets. Unlike channels — which are silently replayed because a slot can be
filled without overwriting anything — a setting is a single canonical value, so replaying a
stale one could clobber a change made elsewhere. So the store never writes on its own: on
connect it only *detects drift* between what it remembers and what the device now reports, and
the startup offer (see :mod:`meshterm.ui.settings_offer`) lets the operator choose to restore
the saved values, adopt the device's current ones, or stop remembering the device entirely.

The gate is provenance, not device type: only settings you changed through MeshTerm are
recorded, so a firmware radio you never edit through MeshTerm keeps an empty record and is never
flagged. A firmware-less bridge, where the values necessarily came from MeshTerm, gets them back.

Like the other operator state (mutes, watched nodes, remembered channels), this is global
machine state in a small JSON file (``<config_dir>/settings.json``), not the per-invocation
SQLite database. Reads are served from memory after the first load; writes flush immediately
and atomically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .atomicwrite import write_atomically
from .device_config import (
    DeviceConfigError,
    format_value,
    get_spec,
    is_setting_key,
    parse_value,
)

if TYPE_CHECKING:
    from .connection import Device

# The JSON-native scalar types a setting value may take; anything else in a loaded file is
# dropped as malformed (settings are strings, numbers, and booleans — never containers).
_SCALARS = (str, int, float, bool)


def _norm(pubkey: str) -> str:
    """Normalise a device public key to the lowercase hex used as the store's device key."""
    return (pubkey or "").lower().removeprefix("0x")


class SettingsStore:
    """Reads and writes the per-device set of MeshTerm-changed settings, memory-first.

    Interact through :meth:`settings` (a device's remembered ``key -> value`` map),
    :meth:`remember` (record one changed setting), :meth:`forget` (drop one), and
    :meth:`forget_all` (stop remembering a device). The backing map is loaded once on first
    access and kept in memory; each mutation persists the whole map atomically.
    """

    def __init__(self, path: Path) -> None:
        """Open the store against a JSON file location.

        Args:
            path: Path to the JSON state file (created lazily on the first remembered setting).
        """
        self._path = path
        self._devices: dict[str, dict[str, Any]] | None = None

    @property
    def _state(self) -> dict[str, dict[str, Any]]:
        """The device -> remembered-settings map, loaded from disk on first access."""
        if self._devices is None:
            self._devices = self._load()
        return self._devices

    def _load(self) -> dict[str, dict[str, Any]]:
        """Parse the file into the device map, or empty on a missing/corrupt file."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        devices: dict[str, dict[str, Any]] = {}
        for pubkey, entries in (data.get("devices") or {}).items():
            if not isinstance(entries, dict):
                continue
            # Keep only plain scalar values of settings the registry knows: a container or
            # null is a malformed entry, and a key that is no longer a setting (or a typo)
            # is not something to offer to write back onto the radio. Whatever is left out
            # here is gone from the file at its next save.
            values = {
                str(key): value
                for key, value in entries.items()
                if isinstance(value, _SCALARS) and is_setting_key(str(key))
            }
            if values:
                devices[_norm(str(pubkey))] = values
        return devices

    def settings(self, pubkey: str) -> dict[str, Any]:
        """The settings remembered for a device (a copy), empty if none."""
        return dict(self._state.get(_norm(pubkey), {}))

    def remember(self, pubkey: str, key: str, value: Any) -> None:
        """Record the value a setting was changed to, replacing any prior value for that key.

        Called after a setting is applied to the device through MeshTerm. Re-recording the
        same value is a no-op (no needless write).

        Args:
            pubkey: The device's own public key.
            key: The setting key (a :class:`~meshterm.core.device_config.SettingSpec` key).
            value: The applied, already-typed scalar value.
        """
        pub = _norm(pubkey)
        if not pub or not isinstance(value, _SCALARS):
            return
        values = self._state.get(pub, {})
        if values.get(key) == value and key in values:
            return
        values = {**values, key: value}
        self._state[pub] = values
        self._save()

    def forget(self, pubkey: str, key: str) -> None:
        """Drop one remembered setting; persist only a real change."""
        pub = _norm(pubkey)
        values = self._state.get(pub)
        if not values or key not in values:
            return
        values = {k: v for k, v in values.items() if k != key}
        if values:
            self._state[pub] = values
        else:
            del self._state[pub]
        self._save()

    def forget_all(self, pubkey: str) -> None:
        """Stop remembering a device's settings entirely; persist only a real change."""
        pub = _norm(pubkey)
        if pub not in self._state:
            return
        del self._state[pub]
        self._save()

    def _save(self) -> None:
        """Persist the whole device map atomically (a crash mid-write keeps the old file)."""
        data = {
            "devices": {
                pubkey: dict(sorted(values.items()))
                for pubkey, values in sorted(self._state.items())
                if values
            }
        }
        write_atomically(self._path, json.dumps(data, indent=2))


@dataclass(frozen=True)
class SettingDrift:
    """One setting whose remembered value differs from what the device now reports.

    Attributes:
        key: The setting key.
        remembered: The value MeshTerm has saved for this device.
        current: The value the device currently reports (``None`` if it reports none).
    """

    key: str
    remembered: Any
    current: Any


def settings_drift(store: SettingsStore, pubkey: str, snapshot: dict) -> list[SettingDrift]:
    """Return the remembered settings that differ from a device's current snapshot.

    Values are compared by their *formatted* form (via the setting's spec), so a difference
    only in representation — an integer that reads the same, a float the firmware rounds
    identically — is not reported as drift. A remembered key the registry no longer knows is
    skipped. With nothing remembered this returns an empty list, so a firmware radio you don't
    manage through MeshTerm never shows drift.

    Args:
        store: The settings store to read remembered values from.
        pubkey: The device's own public key.
        snapshot: The device's current configuration (see
            :func:`~meshterm.core.device_config.build_snapshot`).

    Returns:
        The drifted settings, in the registry's key order.
    """
    remembered = store.settings(pubkey)
    if not remembered:
        return []
    drifted: list[SettingDrift] = []
    for key, saved in remembered.items():
        try:
            spec = get_spec(key)
        except DeviceConfigError:
            continue  # a key the current registry no longer defines
        current = snapshot.get(key)
        if format_value(spec, saved) != format_value(spec, current):
            drifted.append(SettingDrift(key=key, remembered=saved, current=current))
    order = {spec.key: i for i, spec in enumerate(_ordered_specs())}
    return sorted(drifted, key=lambda d: order.get(d.key, len(order)))


def _ordered_specs() -> list:
    """The registry's settings in display order (imported lazily to avoid a load-time cost)."""
    from .device_config import DEVICE_SETTINGS

    return DEVICE_SETTINGS


async def restore(store: SettingsStore, device: Device, snapshot: dict, keys: list[str]) -> int:
    """Write remembered values for ``keys`` back onto the device, updating ``snapshot`` in place.

    Applies each setting through its registry spec — the same path the editor uses — so coupled
    commands (radio, coordinates, tuning, telemetry) rebuild from the running snapshot and a
    setting can be restored in isolation. Best-effort per key: a setter a given firmware lacks is
    skipped rather than aborting the rest.

    Args:
        store: The store holding the remembered values.
        device: The connected device to write to.
        snapshot: The device's current snapshot; mutated in place as values are applied so a
            later coupled key is rebuilt from current values.
        keys: The setting keys to restore (typically the drifted ones).

    Returns:
        The number of settings successfully written.
    """
    pubkey = str(snapshot.get("public_key") or "")
    remembered = store.settings(pubkey)
    restored = 0
    for key in keys:
        if key not in remembered:
            continue
        try:
            spec = get_spec(key)
            value = parse_value(spec, remembered[key], snapshot)
            await spec.apply(device, value, snapshot)
        except Exception:  # noqa: BLE001 - a firmware-unsupported setting must not abort the rest
            continue
        snapshot[key] = value
        restored += 1
    return restored


def adopt(store: SettingsStore, pubkey: str, snapshot: dict, keys: list[str]) -> None:
    """Update the remembered copy of ``keys`` to the device's current values.

    The other side of a drift: rather than restore the saved values, take the device's current
    ones as the new truth — so a change made outside MeshTerm is kept, not clobbered. A key the
    device no longer reports is forgotten instead of stored.

    Args:
        store: The store to update.
        pubkey: The device's own public key.
        snapshot: The device's current snapshot to read values from.
        keys: The setting keys to adopt.
    """
    for key in keys:
        current = snapshot.get(key)
        if current is None:
            store.forget(pubkey, key)
        else:
            store.remember(pubkey, key, current)
