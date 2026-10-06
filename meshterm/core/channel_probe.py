# SPDX-License-Identifier: Apache-2.0
"""Read the channel slots back from the device.

This module has one configured channel slot (:class:`ChannelSlot`), and the two reads that
discover the slots. Each derived property of the slot delegates to
:mod:`meshterm.core.channels`, which owns the maths of keys and identities. This module is
only the half that faces the device: the scan, and what it means when the scan stops.

The module is in :mod:`meshterm.core`, not next to the screen of the channel manager,
because the screen is not the only code that reads the slots. The session cache of the
device state (:class:`~meshterm.services.device_state.DeviceState`) warms the probe. The
``channels`` and ``chat`` tools read slots for their CLI faces. Also, a service must never
have to reach up into ``ui/`` for a device read.

It is a module separate from :mod:`meshterm.core.channels`, because it needs
:class:`~meshterm.core.connection.Device`, and ``connection`` already imports ``channels``.
When the maths stays free of the transport, that import direction stays one-way.
"""

from __future__ import annotations

from dataclasses import dataclass

from .channels import (
    CHANNEL_SLOT_EMPTY_RUN,
    CHANNEL_SLOT_PROBE_CAP,
    channel_hash,
    channel_identity,
    full_channel_hash,
    is_name_derived,
    is_public_channel,
)
from .connection import Device, DeviceCommandError, is_connection_lost
from .models import Conversation


@dataclass(slots=True)
class ChannelSlot:
    """One configured channel slot, read back from the device.

    Attributes:
        idx: The 0-based slot index.
        name: The name of the channel.
        secret: The 16-byte shared secret of the channel.
    """

    idx: int
    name: str
    secret: bytes

    @property
    def is_name_derived(self) -> bool:
        """Whether the key of this channel can be made again from its name.

        If so, the key does not have to be stored.
        """
        return is_name_derived(self.name, self.secret)

    @property
    def is_public(self) -> bool:
        """Whether this channel is public (shared on the full mesh), not private.

        This includes the channels whose key comes from the name, and also the default
        ``Public`` channel of the firmware, which has a fixed key.
        """
        return is_public_channel(self.name, self.secret)

    @property
    def hash(self) -> str:
        """The two-character hash of the channel (the first byte, which MeshCore shows)."""
        return channel_hash(self.secret)

    @property
    def full_hash(self) -> str:
        """The complete ``sha256(secret)`` digest of the channel.

        Its first byte is :attr:`hash`.
        """
        return full_channel_hash(self.secret)

    @property
    def identity(self) -> str:
        """The identity of the channel, which does not depend on the slot.

        The chat history of the channel uses it as its key.
        """
        return channel_identity(self.name, self.secret)

    @property
    def conversation(self) -> Conversation:
        """A :class:`~meshterm.core.models.Conversation` to open this channel in chat."""
        return Conversation(
            label=self.name,
            is_channel=True,
            channel_idx=self.idx,
            channel_id=self.identity,
            secret=self.secret,
        )


async def read_channel_slots(device: Device) -> list[ChannelSlot]:
    """Probe the channel slots, and return the configured slots in index order.

    The scan stops as soon as the firmware refuses a slot index. Thus it reads exactly the
    slots that a correct device has, whatever its capacity.

    Some firmware never refuses an index that is out of range: it answers each slot with
    an empty payload, and raises no error. Without a limit, the scan reads all
    :data:`CHANNEL_SLOT_PROBE_CAP` slots at each read on such firmware. Thus a run of
    :data:`CHANNEL_SLOT_EMPTY_RUN` empty slots in sequence ends the scan. This is safe,
    because the manager puts the channels into the slots from slot 0 up. Thus an unbroken
    empty run of that length means that the scan already found each configured channel
    (refer to the note of the constant).

    Args:
        device: The connected device to query.

    Returns:
        One :class:`ChannelSlot` for each configured slot (the function skips an empty
        slot).
    """
    slots, _complete = await probe_channel_slots(device)
    return slots


async def probe_channel_slots(device: Device) -> tuple[list[ChannelSlot], bool]:
    """The probe behind :func:`read_channel_slots`. It also reports whether the scan finished.

    The scan stops for two very different reasons, and the plain list cannot show which.

    The first reason: the scan went past the end of the configured slots (a refused index,
    or a sufficiently long run of empty slots). Then the answer is complete, and it is
    worth keeping.

    The second reason: a read failed. For example, a timeout or a short link problem
    occurred. Then each subsequent read also failed, because the reply of the firmware no
    longer matched the requested slot. The scan then ends early with the slots that it had
    at that time. That list is not the layout of the device. MeshTerm must not cache it,
    and must not use it as if it was the layout. An empty list reads as "no channels
    configured". A truncated list makes the next free slot look free, when a channel is
    in it.

    The two endings raise different exceptions, and that is what separates them. When the
    firmware answers "no such slot", the result is a plain refusal. When a link does not
    answer any more, the result is a timeout, a
    :class:`~meshterm.core.connection.DeviceCommandError` (whose only subject is a companion
    that did not reply in time), or one of the signatures of a lost link that
    :func:`~meshterm.core.connection.is_connection_lost` knows. Only the first ending gives
    a layout. If a read fails for a fourth reason, the function reads it as a refusal. That
    is the safe direction: the list is used, but the cache probes again.

    Args:
        device: The connected device to query.

    Returns:
        The slots that were read, and whether the scan ran to a clean end.
    """
    slots: list[ChannelSlot] = []
    empty_run = 0
    for idx in range(CHANNEL_SLOT_PROBE_CAP):
        try:
            payload = await device.get_channel(idx)
        except Exception as exc:  # noqa: BLE001 - a rejected index, or a read that failed
            return slots, not _read_failed(exc)
        if payload and payload.get("channel_name"):
            empty_run = 0
            slots.append(
                ChannelSlot(
                    idx=idx,
                    name=str(payload["channel_name"]),
                    secret=bytes(payload.get("channel_secret") or b"\x00" * 16),
                )
            )
        else:
            empty_run += 1
            if empty_run >= CHANNEL_SLOT_EMPTY_RUN:
                break  # past the end, on a firmware that never refuses. Nothing more to find.
    return slots, True


def _read_failed(exc: BaseException) -> bool:
    """Whether ``exc`` means that the read failed, not that the firmware refused a slot.

    A refusal is the normal end of the probe, and it leaves a complete list. A failure
    leaves a short list that looks exactly the same. That is the reason that the code must
    make this distinction somewhere (refer to :func:`probe_channel_slots`).
    """
    return isinstance(exc, (TimeoutError, DeviceCommandError)) or is_connection_lost(exc)
