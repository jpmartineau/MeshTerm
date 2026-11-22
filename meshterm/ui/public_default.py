# SPDX-License-Identifier: Apache-2.0
"""Add the standard Public channel at startup to a device that has no channels.

A MeshCore companion ships with the standard ``Public`` channel on slot 0. A device that
arrives with **no channel at all** cannot send to anybody: each send needs a slot, and the
device refuses a send on a slot that it does not have (issue #36). A new user does not know
this. The user reads an empty Chat picker as "nobody is here", and not as "this radio has
no channel".

Thus MeshTerm gives such a device the standard Public channel at startup, on slot 0, and
says what it did (JP, 2026-11-22). It does not ask. The channel is the one that MeshCore
itself ships, its key is well known, and the device had nothing to lose: the write
overwrites nothing. This is the same judgment as the position that MeshTerm restores
silently (refer to :mod:`meshterm.ui.settings_offer`): an empty slot is not a value that
competes.

The step is one preference (``public_on_empty_device``, on by default). A user who wants a
radio with no channel turns it off, and then nothing is written. The Chat picker still
offers Public there, because that offer only asks (refer to
:func:`meshterm.tools.chat._add_public_channel`).

The step is quiet and cheap. It reads slot 0 first, and a channel there ends the step after
one round trip. Thus a device that has channels pays almost nothing at each startup. Only a
device whose slot 0 is empty pays the full slot probe.

The step runs after the replay of the remembered channels (refer to
:meth:`meshterm.context.AppContext._reconcile_channels`). Thus a device that forgot its
channels gets its own channels back first, and this step sees them.

The simulator is the one device that the step passes over. It is not a radio, it keeps
nothing between launches, and it starts with no channel each time. Thus the step would
write and give its card at each ``--mock`` launch, which is each launch of the development
loop and of the emulator. The simulator instead shows the other half of the same feature:
the Chat picker offers Public on its own (refer to
:func:`meshterm.tools.chat._add_public_channel`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text

from ..core.channels import DEFAULT_PUBLIC_SECRET

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.connection import Device

#: The slot that the new channel goes into. The device had no channel, so slot 0 is free,
#: and slot 0 is where the firmware itself keeps Public.
PUBLIC_SLOT = 0


async def _has_a_channel(ctx: AppContext, device: Device) -> bool:
    """Whether the device has any channel at all, and whether that answer is proved.

    The function reads slot 0 by itself first, because a channel there answers the question
    with one round trip. A full probe of the slots follows only for an empty slot 0.

    An unproved answer counts as "it has a channel". A probe that stopped because a read
    failed returns a short list, and an empty short list looks exactly the same as a device
    with no channels (refer to
    :func:`~meshterm.core.channel_probe.probe_channel_slots`). MeshTerm must not write to a
    device on that evidence.

    Args:
        ctx: The shared application context, for the log.
        device: The connected device.

    Returns:
        ``True`` if the device has a channel, or if the code could not prove that it has
        none.
    """
    from ..core.channel_probe import probe_channel_slots

    try:
        first = await device.get_channel(PUBLIC_SLOT)
    except Exception as exc:  # noqa: BLE001 - a read that failed proves nothing
        ctx.log.debug("channels: could not read slot %s at startup: %s", PUBLIC_SLOT, exc)
        return True
    if first and first.get("channel_name"):
        return True
    slots, complete = await probe_channel_slots(device)
    if not complete:
        ctx.log.debug("channels: the slot probe did not finish, so the startup step stops")
        return True
    return bool(slots)


async def add_public_when_empty(ctx: AppContext) -> bool:
    """Give a device with no channels the standard Public channel, and say so.

    The function does its best only, and it is quiet. It does nothing in these cases: the
    preference is off, the simulator, no live link, a device that has a channel, or a probe
    that could not prove that the device has none. It never opens the radio itself. A deferred connect
    (``connect_on_start`` off) skips the function, because ``is_connected`` is false. A
    failure here must not stop the user from reaching the menu, so the function guards each
    step.

    Args:
        ctx: The shared application context.

    Returns:
        ``True`` if the function wrote the channel.
    """
    from .channels import write_channel
    from .logo import load_logo

    if not ctx.preferences.public_on_empty_device:
        return False
    if ctx.mock or not ctx.is_connected:
        return False
    try:
        device = await ctx.device()
        if await _has_a_channel(ctx, device):
            return False
        await write_channel(ctx, device, PUBLIC_SLOT, "Public", DEFAULT_PUBLIC_SECRET)
    except Exception as exc:  # noqa: BLE001 - a startup step must not stop the menu
        ctx.log.debug("channels: could not add Public at startup: %s", exc)
        return False
    ctx.log.info("channels: added the standard Public channel to slot %s", PUBLIC_SLOT)
    notice = Text.assemble(
        ("This radio had no channel, so it could not send anywhere.\n", ""),
        ("MeshTerm added ", ""),
        ("Public", "accent"),
        (f" to slot {PUBLIC_SLOT}: MeshCore's built-in meshwide channel.\n", ""),
        ("Chat opens it. Preferences can turn this step off.", "muted"),
    )
    await ctx.ui.notify_startup(notice, title="Public channel added", banner=load_logo())
    return True
