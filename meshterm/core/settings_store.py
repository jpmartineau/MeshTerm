# SPDX-License-Identifier: Apache-2.0
"""The store for the device settings that MeshTerm changed, for a device that forgets them.

Most companions keep their settings in firmware. Thus MeshTerm reads the settings live from the
device in each session, and does not have to remember them. A radio bridge without firmware is
the exception. It keeps its settings only in RAM. Thus, when the bridge process restarts, the
node name, the radio parameters, and the tuning that you set in the last session go back to the
firmware defaults.

This store is the durable backup for exactly that case. It stores only the settings that were
changed through MeshTerm. (The editor, the ``config`` CLI, and a TOML restore all go through
:func:`~meshterm.tools.config._apply_setting`.) The key is the public key of the device itself,
so that two devices keep separate sets.

Channels are different: MeshTerm replays them silently, because it can fill a slot without an
overwrite. But a setting is a single canonical value, so a replay of an old value can overwrite a
change that was made elsewhere. Thus the store never writes to the device on its own. At connect
time, it only finds the drift between what it remembers and what the device now reports. Then
the startup offer (refer to :mod:`meshterm.ui.settings_offer`) lets the user choose: restore the
saved values, adopt the current values of the device, or forget the device completely.

The gate is the origin of a value, not the device type. The store keeps only the settings that
you changed through MeshTerm. Thus a device with firmware that you never edit through MeshTerm
keeps an empty record, and MeshTerm never flags it. A radio bridge without firmware gets its
values back, because those values necessarily came from MeshTerm.

This store is global machine state in a small JSON file (``<config_dir>/settings.json``), the
same as the other state of the user (mutes, watched nodes, remembered channels). It is not in
the SQLite database, which can be different for each run. After the first read, the store gives
all reads from memory. Each write goes to disk immediately and atomically.
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

# The JSON-native scalar types that a setting value can have. The store removes all other
# values in a file that it reads, because they are malformed (settings are strings, numbers,
# and booleans, never containers).
_SCALARS = (str, int, float, bool)


def _norm(pubkey: str) -> str:
    """Normalize the public key of a device to lowercase hex, the device key of the store."""
    return (pubkey or "").lower().removeprefix("0x")


class SettingsStore:
    """Reads and writes the set of MeshTerm-changed settings for each device, in memory first.

    Use :meth:`settings` (the remembered ``key -> value`` map of a device), :meth:`remember`
    (store one changed setting), :meth:`forget` (remove one setting), and :meth:`forget_all`
    (forget a device). The store reads the map one time at the first access, and keeps it in
    memory. Each change writes the full map atomically.
    """

    def __init__(self, path: Path) -> None:
        """Open the store on the location of a JSON file.

        Args:
            path: Path to the JSON state file (made only when the first setting is
                remembered).
        """
        self._path = path
        self._devices: dict[str, dict[str, Any]] | None = None

    @property
    def _state(self) -> dict[str, dict[str, Any]]:
        """The map from device to remembered settings, read from disk at the first access."""
        if self._devices is None:
            self._devices = self._load()
        return self._devices

    def _load(self) -> dict[str, dict[str, Any]]:
        """Parse the file into the device map (empty if the file is missing or corrupt)."""
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
            # Keep only the plain scalar values of the settings that the registry knows. A
            # container or a null is a malformed entry. A key that is no longer a setting
            # (or a typo) must not be offered for a write back to the device. All that is
            # left out here is removed from the file at its next save.
            values = {
                str(key): value
                for key, value in entries.items()
                if isinstance(value, _SCALARS) and is_setting_key(str(key))
            }
            if values:
                devices[_norm(str(pubkey))] = values
        return devices

    def settings(self, pubkey: str) -> dict[str, Any]:
        """The settings that are remembered for a device (a copy), empty if there are none."""
        return dict(self._state.get(_norm(pubkey), {}))

    def remember(self, pubkey: str, key: str, value: Any) -> None:
        """Store the new value of a setting, and replace the earlier value for that key.

        MeshTerm calls this function after it applies a setting to the device. If the value
        is the same as the stored value, the function does nothing (no unnecessary write).

        Args:
            pubkey: The public key of the device itself.
            key: The setting key (a :class:`~meshterm.core.device_config.SettingSpec` key).
            value: The applied scalar value, already typed.
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
        """Remove one remembered setting. Write the file only for a real change."""
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
        """Forget all the settings of a device. Write the file only for a real change."""
        pub = _norm(pubkey)
        if pub not in self._state:
            return
        del self._state[pub]
        self._save()

    def _save(self) -> None:
        """Write the full device map atomically.

        If a crash occurs during the write, the old file stays.
        """
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
    """One setting whose remembered value is not the value that the device now reports.

    Attributes:
        key: The setting key.
        remembered: The value that MeshTerm stored for this device.
        current: The value that the device reports now (``None`` if it reports no value).
    """

    key: str
    remembered: Any
    current: Any


def settings_drift(store: SettingsStore, pubkey: str, snapshot: dict) -> list[SettingDrift]:
    """Return the remembered settings that are different from the current snapshot of a device.

    The function compares the values in their formatted form (through the spec of the
    setting). Thus a difference only in representation (an integer that reads the same, a
    float that the firmware rounds to the same value) is not reported as drift. The function
    skips a remembered key that the registry no longer knows. When nothing is remembered, the
    function returns an empty list. Thus a device with firmware that you do not manage through
    MeshTerm never shows drift.

    Args:
        store: The settings store from which to read the remembered values.
        pubkey: The public key of the device itself.
        snapshot: The current settings of the device (refer to
            :func:`~meshterm.core.device_config.build_snapshot`).

    Returns:
        The settings with drift, in the key order of the registry.
    """
    remembered = store.settings(pubkey)
    if not remembered:
        return []
    drifted: list[SettingDrift] = []
    for key, saved in remembered.items():
        try:
            spec = get_spec(key)
        except DeviceConfigError:
            continue  # a key that the current registry no longer defines
        current = snapshot.get(key)
        if format_value(spec, saved) != format_value(spec, current):
            drifted.append(SettingDrift(key=key, remembered=saved, current=current))
    order = {spec.key: i for i, spec in enumerate(_ordered_specs())}
    return sorted(drifted, key=lambda d: order.get(d.key, len(order)))


def _ordered_specs() -> list:
    """The settings of the registry, in display order.

    The import is lazy, to avoid a cost at load time.
    """
    from .device_config import DEVICE_SETTINGS

    return DEVICE_SETTINGS


async def restore(store: SettingsStore, device: Device, snapshot: dict, keys: list[str]) -> int:
    """Write the remembered values of ``keys`` to the device, and change ``snapshot`` in place.

    The function applies each setting through its registry spec, on the same path as the
    editor. Thus the coupled commands (radio, coordinates, tuning, telemetry) are built again
    from the running snapshot, and a setting can be restored alone. Each key is best-effort: if
    a firmware does not have a setter, the function skips that key and does not stop the others.

    Args:
        store: The store that holds the remembered values.
        device: The connected device to write to.
        snapshot: The current snapshot of the device. The function changes it in place when
            it applies the values, so that a later coupled key is built from the current
            values.
        keys: The setting keys to restore (usually the keys with drift).

    Returns:
        The number of settings that were written successfully.
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
    """Change the remembered copy of ``keys`` to the current values of the device.

    This is the other answer to a drift. The function does not restore the saved values.
    Instead, it takes the current values of the device as the new truth. Thus a change that was
    made outside MeshTerm stays, and is not overwritten. If the device no longer reports a key,
    the function forgets that key instead of storing it.

    Args:
        store: The store to change.
        pubkey: The public key of the device itself.
        snapshot: The current snapshot of the device, from which to read the values.
        keys: The setting keys to adopt.
    """
    for key in keys:
        current = snapshot.get(key)
        if current is None:
            store.forget(pubkey, key)
        else:
            store.remember(pubkey, key, current)
