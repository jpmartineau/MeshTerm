# SPDX-License-Identifier: Apache-2.0
"""The store for the channels that MeshTerm made, for a device that forgets them.

Most companions keep their own channel table in firmware. Thus MeshTerm reads the channels live
from the device, and does not have to remember them. A radio bridge without firmware is the
exception. It keeps its channels only in RAM, so it loses all of them when the bridge process
restarts. Then the Channels page is empty in each session, although you added channels in the
last session.

This store is the durable backup for exactly that case. It stores each channel that was written
through MeshTerm. (The channel manager and the ``channels`` CLI both go through
:func:`~meshterm.ui.channels.write_channel`.) The key is the public key of the device itself, so
that two devices keep separate sets. At connect time, :func:`reconcile` replays each remembered
channel that the device does not report now into a free slot. It fills gaps, and it never
overwrites a slot that is in use.

The gate is the origin of a channel, not the device type. The store keeps only the channels that
you wrote through MeshTerm. Thus a device with firmware that you never edit through MeshTerm
keeps an empty record, and MeshTerm does not change it at all. (When nothing is remembered,
``reconcile`` does a single identity probe and returns.) A radio bridge without firmware gets its
full set back, because each of its channels was necessarily made through MeshTerm.

This store is global machine state in a small JSON file (``<config_dir>/channels.json``), the
same as the other state of the user (mutes, watched nodes, admin passwords). It is not in the
SQLite database, which can be different for each run. After the first read, the store gives all
reads from memory. The writes are rare and come from the user. Each write goes to disk
immediately and atomically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from .atomicwrite import write_atomically
from .channels import (
    CHANNEL_SECRET_BYTES,
    CHANNEL_SLOT_EMPTY_RUN,
    CHANNEL_SLOT_PROBE_CAP,
    MAX_CHANNELS,
    channel_identity,
)

if TYPE_CHECKING:
    from .connection import Device


@dataclass(frozen=True)
class RememberedChannel:
    """One channel that MeshTerm remembers for a device: its slot, name, and 16-byte secret."""

    idx: int
    name: str
    secret: bytes

    #: The fields that this record held before. It must never hold them again with a
    #: different meaning (refer to :data:`meshterm.core.preferences.RETIRED` for the
    #: reason that a name is never used again).
    RETIRED: ClassVar[frozenset[str]] = frozenset()

    @property
    def identity(self) -> str:
        """The intrinsic identity of the channel, which does not depend on the slot.

        Refer to :func:`channel_identity`.
        """
        return channel_identity(self.name, self.secret)


def _norm(pubkey: str) -> str:
    """Normalize the public key of a device to lowercase hex, the device key of the store."""
    return (pubkey or "").lower().removeprefix("0x")


class ChannelStore:
    """Reads and writes the set of MeshTerm-managed channels for each device, in memory first.

    Use :meth:`channels` (the remembered channels of a device), :meth:`remember` (store a
    channel that was written to a slot), and :meth:`forget` (a cleared slot). The store reads
    the map one time at the first access, and keeps it in memory. Each change writes the full
    map atomically.
    """

    def __init__(self, path: Path) -> None:
        """Open the store on the location of a JSON file.

        Args:
            path: Path to the JSON state file (made only when the first channel is
                remembered).
        """
        self._path = path
        self._devices: dict[str, list[RememberedChannel]] | None = None

    @property
    def _state(self) -> dict[str, list[RememberedChannel]]:
        """The map from device to remembered channels, read from disk at the first access."""
        if self._devices is None:
            self._devices = self._load()
        return self._devices

    def _load(self) -> dict[str, list[RememberedChannel]]:
        """Parse the file into the device map (empty if the file is missing or corrupt)."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        devices: dict[str, list[RememberedChannel]] = {}
        for pubkey, entries in (data.get("devices") or {}).items():
            if not isinstance(entries, list):
                continue
            channels: list[RememberedChannel] = []
            for entry in entries:
                channel = _channel_from_json(entry)
                if channel is not None:
                    channels.append(channel)
            devices[_norm(str(pubkey))] = channels
        return devices

    def channels(self, pubkey: str) -> list[RememberedChannel]:
        """The remembered channels of a device (a copy, in slot order), empty if there are none."""
        return sorted(self._state.get(_norm(pubkey), []), key=lambda c: c.idx)

    def remember(self, pubkey: str, idx: int, name: str, secret: bytes) -> None:
        """Store the channel that is now in a slot, and replace the earlier entry for that slot.

        MeshTerm calls this function after it writes a channel to the device. If the slot
        already holds an identical channel, the function does nothing (no unnecessary write).

        Args:
            pubkey: The public key of the device itself.
            idx: The slot to which the channel was written.
            name: The channel name.
            secret: The effective 16-byte secret of the channel. For a channel whose key
                comes from its name, the store keeps the derived key. Thus MeshTerm can
                replay the channel without a new derivation.
        """
        key = _norm(pubkey)
        if not key:
            return
        channels = list(self._state.get(key, []))
        new = RememberedChannel(idx=idx, name=name, secret=bytes(secret))
        existing = next((c for c in channels if c.idx == idx), None)
        if existing == new:
            return
        channels = [c for c in channels if c.idx != idx]
        channels.append(new)
        self._state[key] = channels
        self._save()

    def forget(self, pubkey: str, idx: int) -> None:
        """Remove the remembered channel of a cleared slot.

        Write the file only for a real change.
        """
        key = _norm(pubkey)
        channels = self._state.get(key)
        if not channels or not any(c.idx == idx for c in channels):
            return
        self._state[key] = [c for c in channels if c.idx != idx]
        self._save()

    def _save(self) -> None:
        """Write the full device map atomically.

        If a crash occurs during the write, the old file stays.
        """
        data = {
            "devices": {
                pubkey: [
                    {"idx": c.idx, "name": c.name, "secret": c.secret.hex()}
                    for c in sorted(channels, key=lambda c: c.idx)
                ]
                for pubkey, channels in sorted(self._state.items())
                if channels
            }
        }
        write_atomically(self._path, json.dumps(data, indent=2))


def _channel_from_json(entry: object) -> RememberedChannel | None:
    """Parse one stored channel entry, or return ``None`` if it is malformed."""
    if not isinstance(entry, dict):
        return None
    try:
        idx = int(entry["idx"])
        name = str(entry["name"])
        secret = bytes.fromhex(str(entry["secret"]))
    except (KeyError, ValueError, TypeError):
        return None
    if not name or len(secret) != CHANNEL_SECRET_BYTES:
        return None
    return RememberedChannel(idx=idx, name=name, secret=secret)


async def reconcile(store: ChannelStore, device: Device) -> int:
    """Replay the remembered channels of a device into the slots that it does not report now.

    The function restores each remembered channel that the device does not have. It compares
    the channels by intrinsic identity, so a channel that moved to a different slot still
    counts as present. The channel goes into its remembered slot when that slot is free, else
    into the lowest free slot. The function never overwrites a slot that is in use. Thus a
    device with firmware that kept its own channels stays as it is.

    The function reads little, by design. When nothing is remembered for this device, it does
    a single identity probe and returns. Thus a device with firmware that you do not manage
    through MeshTerm pays almost nothing. The function is best-effort. The caller runs it at
    connect time, and must not let a failure here break the connection.

    Args:
        store: The channel store from which to read the remembered channels.
        device: The device that connected a moment ago, to reconcile.

    Returns:
        The number of channels that were restored (0 when no replay was necessary).
    """
    info = await device.get_self_info()
    remembered = store.channels(info.get("public_key", ""))
    if not remembered:
        return 0

    used: set[int] = set()
    present_ids: set[str] = set()
    empty_run = 0
    for idx in range(CHANNEL_SLOT_PROBE_CAP):
        try:
            payload = await device.get_channel(idx)
        except Exception:  # noqa: BLE001 - firmware may reject an out-of-range index
            break
        if payload and payload.get("channel_name"):
            empty_run = 0
            used.add(idx)
            name = str(payload["channel_name"])
            secret = bytes(payload.get("channel_secret") or b"\x00" * CHANNEL_SECRET_BYTES)
            present_ids.add(channel_identity(name, secret))
        else:
            empty_run += 1
            if empty_run >= CHANNEL_SLOT_EMPTY_RUN:
                break  # past the end, on a firmware that never refuses. No more channels.

    restored = 0
    for channel in remembered:
        if channel.identity in present_ids:
            continue  # already on the device (maybe in another slot). Do not change it.
        target = channel.idx if channel.idx not in used else _next_free_slot(used)
        if target is None:
            break  # each slot is full. Nothing more can be restored.
        await device.set_channel(target, channel.name, channel.secret)
        used.add(target)
        present_ids.add(channel.identity)
        restored += 1
    return restored


def _next_free_slot(used: set[int]) -> int | None:
    """The lowest slot index that is not in ``used``, in the stock capacity.

    The function returns ``None`` if all the slots are full.
    """
    return next((i for i in range(MAX_CHANNELS) if i not in used), None)
