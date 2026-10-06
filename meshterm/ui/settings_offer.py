# SPDX-License-Identifier: Apache-2.0
"""The connect-time offer to reconcile a device's settings with what MeshTerm remembers.

A firmware-less radio bridge forgets its configuration whenever it restarts, so the settings you
changed through MeshTerm last session come back as firmware defaults. Unlike channels — silently
replayed because filling an empty slot overwrites nothing — a setting is a single canonical
value, so restoring a stale one could clobber a change made elsewhere. This surface therefore
*asks* rather than acting: on connect, when the device's current settings drift from what
MeshTerm saved for it, it offers to restore the saved values, adopt the device's current ones, or
stop remembering the device (see :mod:`meshterm.core.settings_store`).

One drift is the channel case in disguise and acts silently (JP, 2026-08-08): a manually set
position on a device with no GPS. After a restart such a device reports no usable fix at all —
an *empty slot*, not a competing value — so the saved position is written straight back instead
of asking the operator whether to override a position with an undefined one.

It runs once at startup, on the splash, only when a device is connected and something is actually
remembered for it — so a firmware radio you don't manage through MeshTerm is never interrupted.
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

# The offer's actions, returned by the startup select.
_RESTORE = "restore"
_ADOPT = "adopt"
_FORGET = "forget"

#: The advertised-position pair — the settings whose drift is judged as one fix, not two
#: scalars, against :func:`_position_undefined`.
_POSITION_KEYS = frozenset({"adv_lat", "adv_lon"})


def _position_undefined(snapshot: dict) -> bool:
    """Whether the device currently advertises no usable fix at all.

    The same :func:`~meshterm.core.geo.usable_fix` judgment the map surfaces make: the
    0/0 null island a GPS-less (or freshly restarted) device reports is no position, and
    out-of-range junk is no position either.
    """
    try:
        lat = float(snapshot.get("adv_lat") or 0.0)
        lon = float(snapshot.get("adv_lon") or 0.0)
    except (TypeError, ValueError):
        return True
    return not usable_fix(lat, lon)


async def _gps_running(device: Device) -> bool:
    """Whether the device reports its GPS running (the firmware's ``gps`` variable is ``1``).

    Asked only when a position has drifted, and best-effort: firmware without the variables,
    or a failed read, is a device with no GPS to weigh.
    """
    try:
        found = await device.get_custom_vars()
    except Exception:  # noqa: BLE001 - optional read; absence means no GPS
        return False
    return str(found.get("gps", "")).strip() == "1"


async def offer_remembered_settings(ctx: AppContext) -> None:
    """Offer to reconcile the connected device's settings with MeshTerm's saved copy.

    Best-effort and quiet: does nothing without a live link, without anything remembered for the
    device, or when the device already matches. Never opens the radio itself — a deferred connect
    (``connect_on_start`` off) simply skips this, since ``is_connected`` is false. A failure here
    must not block reaching the menu, so everything is guarded.

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
            return  # nothing remembered — a firmware radio pays only this cheap probe
        snapshot = await build_snapshot(device)
        drifted = settings_drift(store, pubkey, snapshot)
    except Exception as exc:  # noqa: BLE001 - a probe failure must not block the menu
        ctx.log.debug("settings: drift check on connect failed: %s", exc)
        return
    if not drifted:
        return

    # A drifted position on a device that reports none is the empty-slot case: restoring
    # it overwrites nothing, and the alternative on offer — "keep the device's settings"
    # — would mean trading a deliberately set position for an undefined one. So those
    # keys restore silently, and only real value-against-value conflicts reach the
    # operator.
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
            except Exception as exc:  # noqa: BLE001 - best-effort, like the rest of the offer
                ctx.log.debug("settings: silent position restore failed: %s", exc)
            drifted = [d for d in drifted if d.key not in _POSITION_KEYS]
            if not drifted:
                return

    # A device whose GPS is running moves its own position as it travels, so a fix unlike
    # the saved one is a reading, not a setting that drifted: neither restored nor asked
    # about, or every connect away from home would offer to put the node back there. (One
    # with no fix yet was handled above: the saved position holds until the first fix.)
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
    except Exception as exc:  # noqa: BLE001 - acting on the choice must not block the menu
        ctx.log.debug("settings: reconciling on connect failed: %s", exc)


async def _prompt(ctx: AppContext, drifted: list[SettingDrift]) -> object:
    """Show the startup offer and return the chosen action, or ``None`` if dismissed."""
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
    """A muted one-line list of the drifted settings' labels, capped with a ``+N more`` tail."""
    labels = [get_spec(d.key).label for d in drifted]
    shown = labels[:cap]
    text = "  " + ", ".join(shown)
    if len(labels) > cap:
        text += f", +{len(labels) - cap} more"
    return Text(text, style="muted")
