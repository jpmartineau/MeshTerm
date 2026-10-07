# SPDX-License-Identifier: Apache-2.0
"""The offer at connect time to reconcile the device settings with what MeshTerm remembers.

A radio bridge with no firmware forgets its configuration each time that it restarts. Thus
the settings that you changed through MeshTerm in the last session come back as the
defaults of the firmware. Channels are different: MeshTerm replays them silently, because
it writes a channel only into an empty slot, and this overwrites nothing. A setting is one
canonical value, so if MeshTerm restored an old value, it could overwrite a change that
was made in another place. Thus this screen *asks* and does not act. When the device
connects, and its current settings are different from what MeshTerm saved for it, the
screen offers three actions. The user can restore the saved values, adopt the current
values of the device, or stop remembering the device (refer to
:mod:`meshterm.core.settings_store`).

One difference is the channel case in a different form, and MeshTerm acts on it silently
(JP, 2026-08-08): a position that the user set by hand on a device with no GPS. After a
restart, such a device reports no usable fix at all. This is an *empty slot*, not a value
that competes. Thus MeshTerm writes the saved position back immediately. It does not ask
the user if it must overwrite a position with an undefined one.

This runs one time at startup, on the splash. It runs only when a device is connected and
MeshTerm remembers something for it. Thus a radio with firmware that you do not manage
through MeshTerm is never interrupted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text

from ..context import AppContext
from ..core.device_config import build_snapshot, get_spec
from ..core.geo import usable_fix
from ..core.settings_store import SettingDrift, adopt, restore, settings_drift
from .menus import menu_rows
from .tui import Separator

if TYPE_CHECKING:
    from ..core.connection import Device

# The actions of the offer. The startup select returns one of them.
_RESTORE = "restore"
_ADOPT = "adopt"
_FORGET = "forget"

#: The pair of settings for the advertised position. MeshTerm judges their difference as
#: one fix and not as two scalars, against :func:`_position_undefined`.
_POSITION_KEYS = frozenset({"adv_lat", "adv_lon"})


def _position_undefined(snapshot: dict) -> bool:
    """Whether the device currently advertises no usable fix at all.

    This is the same judgment of :func:`~meshterm.core.geo.usable_fix` that the map screens
    make. The 0/0 null island that a device with no GPS (or a device that just restarted)
    reports is not a position. Values that are out of range are not a position either.
    """
    try:
        lat = float(snapshot.get("adv_lat") or 0.0)
        lon = float(snapshot.get("adv_lon") or 0.0)
    except (TypeError, ValueError):
        return True
    return not usable_fix(lat, lon)


async def _gps_running(device: Device) -> bool:
    """Whether the device reports its GPS running (the firmware's ``gps`` variable is ``1``).

    MeshTerm asks only when a position is different from the saved one, and it does its
    best only. Firmware that does not have the variables, or a read that failed, means a
    device with no GPS to consider.
    """
    try:
        found = await device.get_custom_vars()
    except Exception:  # noqa: BLE001 - an optional read. If it is absent, there is no GPS.
        return False
    return str(found.get("gps", "")).strip() == "1"


async def offer_remembered_settings(ctx: AppContext) -> None:
    """Offer to reconcile the connected device's settings with MeshTerm's saved copy.

    The function does its best only, and it is quiet. It does nothing in these cases: no
    live link, nothing remembered for the device, or the device already matches. It never
    opens the radio itself. A deferred connect (``connect_on_start`` off) skips this
    function, because ``is_connected`` is false. A failure here must not stop the user from
    reaching the menu, so the function guards each step.

    Args:
        ctx: The shared application context.
    """
    store = ctx.settings_store
    if store is None or not ctx.is_connected:
        return
    try:
        device = await ctx.device()
        info = await device.get_self_info()
        pubkey = str(info.get("public_key") or "")
        if not store.settings(pubkey):
            return  # nothing remembered. A radio with firmware pays only this small probe.
        snapshot = await build_snapshot(device)
        drifted = settings_drift(store, pubkey, snapshot)
    except Exception as exc:  # noqa: BLE001 - a failure of the probe must not stop the menu
        ctx.log.debug("settings: drift check on connect failed: %s", exc)
        return
    if not drifted:
        return

    # A position that is different, on a device that reports none, is the empty-slot case.
    # If MeshTerm restores it, it overwrites nothing. The other action on offer is "keep
    # the device's settings". This would replace a position that the user set on purpose
    # with an undefined one. Thus MeshTerm restores those keys silently, and only the real
    # conflicts between two values go to the user.
    if _position_undefined(snapshot):
        silent = [d for d in drifted if d.key in _POSITION_KEYS]
        if silent:
            try:
                restored = await restore(store, device, snapshot, [d.key for d in silent])
                ctx.devstate.invalidate_config()
                ctx.log.info(
                    "settings: restored the saved position (%d value(s)) — the device "
                    "reported no fix to weigh it against",
                    restored,
                )
            except Exception as exc:  # noqa: BLE001 - best effort, like the rest of the offer
                ctx.log.debug("settings: silent position restore failed: %s", exc)
            drifted = [d for d in drifted if d.key not in _POSITION_KEYS]
            if not drifted:
                return

    # A device whose GPS runs moves its own position when it travels. Thus a fix that is
    # different from the saved one is a reading, not a setting that changed. MeshTerm does
    # not restore it and does not ask about it. If it did, each connect away from home
    # would offer to put the node back at home. (The code above handled a device that has
    # no fix yet: the saved position holds until the first fix.)
    if any(d.key in _POSITION_KEYS for d in drifted) and await _gps_running(device):
        drifted = [d for d in drifted if d.key not in _POSITION_KEYS]
        if not drifted:
            return

    choice = await _prompt(ctx, drifted)
    keys = [d.key for d in drifted]
    try:
        if choice == _RESTORE:
            restored = await restore(store, device, snapshot, keys)
            ctx.devstate.invalidate_config()
            ctx.log.info("settings: restored %d remembered setting(s) to the device", restored)
        elif choice == _ADOPT:
            adopt(store, pubkey, snapshot, keys)
            ctx.log.info("settings: adopted the device's values for %d setting(s)", len(keys))
        elif choice == _FORGET:
            store.forget_all(pubkey)
            ctx.log.info("settings: stopped remembering this device's settings")
    except Exception as exc:  # noqa: BLE001 - an action on the choice must not stop the menu
        ctx.log.debug("settings: reconciling on connect failed: %s", exc)


async def _prompt(ctx: AppContext, drifted: list[SettingDrift]) -> object:
    """Show the startup offer and return the action that the user selects, or ``None``.

    The function returns ``None`` if the user dismisses the offer.
    """
    from .logo import load_logo

    count = len(drifted)
    plural = "" if count == 1 else "s"
    rows = menu_rows(
        [
            (
                "📂 Restore my saved settings",
                f"Write the {count} saved value{plural} back onto the device",
                _RESTORE,
            ),
            (
                "💾 Keep the device's settings",
                "Update my saved copy to match the device now",
                _ADOPT,
            ),
            (
                "🗑 Stop remembering this device",
                "Forget the saved settings and don't ask again",
                _FORGET,
            ),
        ]
    )
    items = [
        Separator(
            f"  This device reports {count} setting{plural} that differ from what "
            "you last saved through MeshTerm:"
        ),
        Separator(_drift_summary(drifted)),
        Separator(" "),
        *rows,
    ]
    return await ctx.ui.select_startup(
        "Saved settings differ",
        items,
        default=_RESTORE,
        banner=load_logo(),
    )


def _drift_summary(drifted: list[SettingDrift], *, cap: int = 6) -> Text:
    """A muted line with the labels of the settings that differ, with a ``+N more`` tail.

    The list has a maximum of ``cap`` labels. The ``+N more`` tail counts the others.
    """
    labels = [get_spec(d.key).label for d in drifted]
    shown = labels[:cap]
    text = "  " + ", ".join(shown)
    if len(labels) > cap:
        text += f", +{len(labels) - cap} more"
    return Text(text, style="muted")
