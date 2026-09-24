# SPDX-License-Identifier: Apache-2.0
"""Persistence for channels created through MeshTerm, so they survive a device that forgets.

Most companions keep their own channel table in firmware, so MeshTerm reads channels live from
the device and never has to remember them. A firmware-less radio bridge is the exception: it
holds its channels only in RAM, so every one is lost when the bridge process restarts — the
Channels page comes up empty each session even though you added channels last time.

This store is the durable backup for exactly that case. It records every channel *written
through MeshTerm* (the channel manager and the ``channels`` CLI both go through
:func:`~meshterm.ui.channels.write_channel`), keyed by the device's own public key so two radios
keep separate sets. On connect, :func:`reconcile` replays any remembered channel the device
isn't currently reporting into a free slot — *filling gaps, never overwriting an occupied slot*.

The gate is provenance, not device type: only channels you wrote through MeshTerm are recorded,
so a firmware radio you never edit through MeshTerm keeps an empty record and is left completely
untouched (with nothing remembered, ``reconcile`` does a single identity probe and returns). A
firmware-less bridge, where every channel is necessarily created through MeshTerm, gets its
whole set back.

Like the operator preferences (mutes, watched nodes, admin passwords), this is global machine
state in a small JSON file (``<config_dir>/channels.json``), not the per-invocation SQLite
database. Reads are served from memory after the first load; the rare, user-driven writes flush
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
    """One channel MeshTerm remembers for a device: its slot, name, and 16-byte secret."""

    idx: int
    name: str
    secret: bytes

    #: Fields this record once held and must never hold again under another meaning (see
    #: :data:`meshterm.core.preferences.RETIRED` for why a name is never reused).
    RETIRED: ClassVar[frozenset[str]] = frozenset()

    @property
    def identity(self) -> str:
        """The channel's slot-independent intrinsic identity (see :func:`channel_identity`)."""
        return channel_identity(self.name, self.secret)


def _norm(pubkey: str) -> str:
    """Normalise a device public key to the lowercase hex used as the store's device key."""
    return (pubkey or "").lower().removeprefix("0x")


class ChannelStore:
    """Reads and writes the per-device set of MeshTerm-managed channels, memory-first.

    Interact through :meth:`channels` (a device's remembered channels), :meth:`remember` (record
    a channel written to a slot), and :meth:`forget` (a cleared slot). The backing map is loaded
    once on first access and kept in memory; each mutation persists the whole map atomically.
    """

    def __init__(self, path: Path) -> None:
        """Open the store against a JSON file location.

        Args:
            path: Path to the JSON state file (created lazily on the first remembered channel).
        """
        self._path = path
        self._devices: dict[str, list[RememberedChannel]] | None = None

    @property
    def _state(self) -> dict[str, list[RememberedChannel]]:
        """The device -> remembered-channels map, loaded from disk on first access."""
        if self._devices is None:
            self._devices = self._load()
        return self._devices

    def _load(self) -> dict[str, list[RememberedChannel]]:
        """Parse the file into the device map, or empty on a missing/corrupt file."""
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
        """The channels remembered for a device (a copy, in slot order), empty if none."""
        return sorted(self._state.get(_norm(pubkey), []), key=lambda c: c.idx)

    def remember(self, pubkey: str, idx: int, name: str, secret: bytes) -> None:
        """Record the channel now occupying a slot, replacing any prior entry for that slot.

        Called after a channel is written to the device through MeshTerm. A slot already
        holding an identical channel is a no-op (no needless write).

        Args:
            pubkey: The device's own public key.
            idx: The slot the channel was written to.
            name: The channel name.
            secret: The channel's effective 16-byte secret (a name-derived channel stores the
                derived key so it can be replayed without re-deriving).
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
        """Drop the channel remembered for a slot (a cleared slot); persist only a real change."""
        key = _norm(pubkey)
        channels = self._state.get(key)
        if not channels or not any(c.idx == idx for c in channels):
            return
        self._state[key] = [c for c in channels if c.idx != idx]
        self._save()

    def _save(self) -> None:
        """Persist the whole device map atomically (a crash mid-write keeps the old file)."""
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
    """Parse one stored channel entry, or ``None`` if it is malformed."""
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
    """Replay a device's remembered channels into any slots it isn't already reporting.

    Restores each remembered channel the device is missing (matched by intrinsic identity, so a
    channel that moved slots still counts as present) into its remembered slot when free, else
    the lowest free slot. An occupied slot is never overwritten, so a firmware radio that kept
    its own channels is left as-is.

    Read-cheap by design: with nothing remembered for this device it does a single identity
    probe and returns, so a firmware radio you don't manage through MeshTerm pays almost nothing.
    Best-effort — the caller runs it on connect and must not let a failure here break connecting.

    Args:
        store: The channel store to read remembered channels from.
        device: The freshly connected device to reconcile.

    Returns:
        The number of channels restored (0 when nothing needed replaying).
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
                break  # off the end of a never-rejecting firmware; nothing more configured

    restored = 0
    for channel in remembered:
        if channel.identity in present_ids:
            continue  # already on the device (possibly at another slot) — leave it be
        target = channel.idx if channel.idx not in used else _next_free_slot(used)
        if target is None:
            break  # every slot is full; nothing more we can restore
        await device.set_channel(target, channel.name, channel.secret)
        used.add(target)
        present_ids.add(channel.identity)
        restored += 1
    return restored


def _next_free_slot(used: set[int]) -> int | None:
    """The lowest slot index not in ``used`` within the stock capacity, or ``None`` if full."""
    return next((i for i in range(MAX_CHANNELS) if i not in used), None)
