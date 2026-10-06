# SPDX-License-Identifier: Apache-2.0
"""The store for confirmed companion devices.

MeshTerm keeps each device that ever spoke the MeshCore protocol to it with success. It
keeps the device permanently in a small JSON registry in the config directory
(``<config_dir>/devices.json``). The key is
:attr:`~meshterm.core.discovery.DiscoveredDevice.stable_id` (a USB serial number when
there is one, so that the record stays valid after a change of the COM number). We do
not put this registry in the SQLite database, and this is intentional. The ``--db`` flag
can change the database for each run, but the remembered devices are global machine
state that must stay after such a change.

The registry also keeps the device that was connected most recently (``last``). The
startup splash uses it to select a default and to star it. A device is a confirmed
MeshCore companion when it is in the registry. The splash gives its "MeshCore device" tag
only to these devices, instead of a guess from the USB vendor ID.

Next to these, the registry keeps ``hidden``: the stable ids that the splash must no
longer list. This set is here, not in the preferences, for two reasons. It is a fact
about the hardware of this machine: the USB adapters and development boards that are
always connected to it and are not companions. Also, the ids in the set are the ids of
the registry. The hidden set is only for the **splash**: nothing else uses it. Thus
``--port`` and each scripted path can still get to a hidden device by name.

A hidden id does not have to be a remembered device. Usually it is the opposite: a
serial adapter that nobody wants to see. Thus the set is separate, and not a flag on a
record.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .atomicwrite import write_atomically
from .discovery import (
    TRANSPORT_BLE,
    TRANSPORT_SERIAL,
    TRANSPORT_TCP,
    DiscoveredDevice,
)
from .models import utcnow


@dataclass(slots=True)
class RememberedDevice:
    """A device that was confirmed before, stored to give a default the next time.

    Attributes:
        stable_id: The :attr:`DiscoveredDevice.stable_id` of the device at connect time.
        port: The serial port on which MeshTerm last found the device (for information
            only, because it can change). Blank for a BLE or TCP device.
        label: A friendly label to show in prompts and tables.
        last_connected: The ISO-8601 timestamp of the last successful connection.
        node_name: The mesh node name of the device itself, learned at connect time (it
            can be empty).
        transport: ``"serial"``, ``"ble"``, or ``"tcp"``: how MeshTerm last connected to
            this device.
        address: The Bluetooth address, for a BLE device (blank for other devices). With
            it, MeshTerm can connect again directly, without a new scan.
        hardware_model: The model string of the firmware itself (for example
            ``"Seeed Tracker T1000-E"``), learned from the device query at connect time.
            This is the only reliable source of the model, because a BLE companion gives
            no model in its BLE advertisement. Thus the store keeps it here, to fill the
            hardware column also when the device is only attached and not connected. It
            can be empty for a serial device that an older firmware confirmed, if that
            firmware is older than the query.
        host: The network host, for a TCP device (blank for other devices). With it,
            MeshTerm can connect again directly. Discovery cannot find a TCP companion,
            so this remembered endpoint is the only way for it to come back into the
            picker.
        tcp_port: The TCP port, for a TCP device (0 for other devices).
    """

    stable_id: str
    port: str
    label: str
    last_connected: str
    node_name: str = ""
    transport: str = TRANSPORT_SERIAL
    address: str = ""
    hardware_model: str = ""
    host: str = ""
    tcp_port: int = 0

    #: The fields that this record held before. It must never hold them again with a
    #: different meaning (refer to :data:`meshterm.core.preferences.RETIRED` for the
    #: reason that a name is never used again).
    RETIRED: ClassVar[frozenset[str]] = frozenset()

    @property
    def is_ble(self) -> bool:
        """Whether MeshTerm connected to this remembered device over Bluetooth LE."""
        return self.transport == TRANSPORT_BLE

    @property
    def is_tcp(self) -> bool:
        """Whether MeshTerm connected to this remembered device over a TCP network connection."""
        return self.transport == TRANSPORT_TCP

    @property
    def target(self) -> str:
        """Where MeshTerm connects to this device.

        ``host:port`` for TCP, the BLE address for Bluetooth, else the serial port.
        """
        if self.is_tcp:
            return f"{self.host}:{self.tcp_port}"
        return self.address or self.port if self.is_ble else self.port

    def matches(self, device: DiscoveredDevice) -> bool:
        """Return whether ``device`` is the same hardware as this record."""
        return device.stable_id == self.stable_id


class DeviceStore:
    """Reads and writes the registry of confirmed companion devices."""

    def __init__(self, path: Path) -> None:
        """Open the store on the location of a JSON file.

        Args:
            path: Path to the JSON state file (made only at the first write).
        """
        self._path = path

    def _read(self) -> tuple[dict[str, RememberedDevice], str | None, set[str]]:
        """Return the parsed ``(registry, last_id, hidden_ids)``, empty for a missing file.

        The function reads a missing or corrupt file as "nothing remembered", not as an
        error. Thus a bad edit never stops the startup. An old flat-format file (a single
        record at the top level, from before the registry) becomes a registry with one
        entry, in memory. Thus an upgrade causes no problem.
        """
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}, None, set()
        if not isinstance(data, dict):
            return {}, None, set()

        # The old flat shape: a single record with ``stable_id`` at the top level.
        if "devices" not in data and "stable_id" in data:
            record = self._record_from(data)
            if record is None:
                return {}, None, set()
            return {record.stable_id: record}, record.stable_id, set()

        registry: dict[str, RememberedDevice] = {}
        for entry in (data.get("devices") or {}).values():
            record = self._record_from(entry)
            if record is not None:
                registry[record.stable_id] = record
        last = data.get("last")
        if last not in registry:
            last = None
        raw_hidden = data.get("hidden")
        hidden = {str(x) for x in raw_hidden} if isinstance(raw_hidden, list) else set()
        return registry, last, hidden

    @staticmethod
    def _record_from(entry: object) -> RememberedDevice | None:
        """Build a :class:`RememberedDevice` from one raw JSON entry (``None`` if invalid)."""
        if not isinstance(entry, dict):
            return None
        try:
            return RememberedDevice(
                stable_id=entry["stable_id"],
                port=entry.get("port", ""),
                label=entry.get("label", entry.get("port", "")),
                last_connected=entry.get("last_connected", ""),
                node_name=entry.get("node_name", ""),
                transport=entry.get("transport", TRANSPORT_SERIAL),
                address=entry.get("address", ""),
                hardware_model=entry.get("hardware_model", ""),
                host=entry.get("host", ""),
                tcp_port=int(entry.get("tcp_port", 0) or 0),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def load(self) -> RememberedDevice | None:
        """Return the device that was connected most recently, or ``None`` if there is none.

        This is the "last known good" default. The startup splash uses it to select a row
        by default and to star it. MeshTerm also uses it to find a port when it cannot ask
        the user.

        Returns:
            The :class:`RememberedDevice` that was connected last, or ``None``.
        """
        registry, last, _hidden = self._read()
        return registry.get(last) if last is not None else None

    def load_all(self) -> dict[str, RememberedDevice]:
        """Return the full registry of confirmed devices, with ``stable_id`` as the key."""
        registry, _last, _hidden = self._read()
        return registry

    def is_known(self, device: DiscoveredDevice) -> bool:
        """Return whether ``device`` was ever confirmed as a MeshCore companion."""
        registry, _last, _hidden = self._read()
        return device.stable_id in registry

    def remember(
        self, device: DiscoveredDevice, *, node_name: str = "", hardware_model: str = ""
    ) -> None:
        """Store ``device`` as a confirmed connection and as the new default.

        The function adds the device to the registry, or replaces its record (so that
        MeshTerm keeps it permanently). It also marks the device as the device that was
        connected most recently.

        Args:
            device: The device that connected successfully a moment ago.
            node_name: The mesh node name of the device itself, if known. The store keeps
                it across reconnections, and the picker shows it. A blank value does not
                remove the name that is already on file for this device. It keeps that name.
            hardware_model: The model string of the firmware, if known. The store keeps it
                in the same way. Thus a reconnection on firmware that could not answer the
                query does not remove a model that MeshTerm learned before.
        """
        registry, _last, hidden = self._read()
        existing = registry.get(device.stable_id)
        if not node_name and existing is not None:
            node_name = existing.node_name
        if not hardware_model and existing is not None:
            hardware_model = existing.hardware_model
        registry[device.stable_id] = RememberedDevice(
            stable_id=device.stable_id,
            port=device.port,
            label=device.label,
            last_connected=utcnow().isoformat(),
            node_name=node_name,
            transport=device.transport,
            address=device.address or "",
            hardware_model=hardware_model,
            host=device.host or "",
            tcp_port=device.tcp_port or 0,
        )
        # A connection to a device is the clearest statement that the splash must list it.
        # Thus a confirmed device is no longer hidden. If it stays hidden, it goes off the
        # splash at the moment that it proves itself. That is the opposite of the purpose
        # of the hidden set.
        hidden.discard(device.stable_id)
        self._write(registry, device.stable_id, hidden)

    def forget(self, stable_id: str) -> bool:
        """Remove a remembered device from the registry.

        This is the inverse of :meth:`remember`. It removes the record whose key is
        ``stable_id``, so that the device is no longer listed as a confirmed companion. The
        Delete action of the device picker uses it to remove a network (TCP) companion that
        the user no longer wants. The picker lists a TCP device only from its remembered
        endpoint, so the device goes out of the picker only when the store forgets it. If
        the forgotten device was the default (the device connected most recently), that
        pointer moves to the newest record that remains, or it is cleared when no record
        remains. Thus the next startup still selects a sensible device by default.

        Args:
            stable_id: The :attr:`RememberedDevice.stable_id` of the device to forget.

        Returns:
            ``True`` if a record was removed, ``False`` if no record matched.
        """
        registry, last, hidden = self._read()
        if stable_id not in registry:
            return False
        del registry[stable_id]
        if last == stable_id:
            last = max(registry, key=lambda k: registry[k].last_connected, default=None)
        hidden.discard(stable_id)  # nothing remains to hide it from
        self._write(registry, last, hidden)
        return True

    def hidden_ids(self) -> set[str]:
        """Return the stable ids that the startup splash must not list."""
        _registry, _last, hidden = self._read()
        return hidden

    def hide(self, stable_id: str) -> None:
        """Hide ``stable_id`` on the startup splash from now on, until it is shown again.

        The store keeps the hidden id permanently, the same as the registry itself. The
        splash is a list of the hardware that is connected to this machine. The adapters
        that are always connected, and that are never companions, are the same each time.
        To hide an id that is not remembered is normal and correct: these are exactly the
        rows that a user wants to hide.

        Args:
            stable_id: The :attr:`DiscoveredDevice.stable_id` of the device.
        """
        registry, last, hidden = self._read()
        if stable_id in hidden:
            return
        hidden.add(stable_id)
        self._write(registry, last, hidden)

    def show_all(self) -> int:
        """Show each hidden device again, and return how many devices came back.

        This is the only way back, and this is intentional. The user hides one row at a
        time, but this function shows all the rows again at one time. The reason: a hidden
        row is not on the screen, so the user cannot press a key on it. The returned count
        lets the caller tell what it did.
        """
        registry, last, hidden = self._read()
        if not hidden:
            return 0
        self._write(registry, last, set())
        return len(hidden)

    def _write(
        self,
        registry: dict[str, RememberedDevice],
        last: str | None,
        hidden: set[str],
    ) -> None:
        """Write the registry, its ``last`` pointer, and the hidden ids of the splash."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "devices": {
                stable_id: {
                    "stable_id": record.stable_id,
                    "port": record.port,
                    "label": record.label,
                    "last_connected": record.last_connected,
                    "node_name": record.node_name,
                    "transport": record.transport,
                    "address": record.address,
                    "hardware_model": record.hardware_model,
                    "host": record.host,
                    "tcp_port": record.tcp_port,
                }
                for stable_id, record in registry.items()
            },
            "last": last,
            # Sorted, so that a different set order alone does not change the file between
            # writes.
            "hidden": sorted(hidden),
        }
        write_atomically(self._path, json.dumps(payload, indent=2))
