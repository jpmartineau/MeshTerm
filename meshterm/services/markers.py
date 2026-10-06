# SPDX-License-Identifier: Apache-2.0
"""Collect the nodes of the mesh that have a location into map markers.

One list for three surfaces: the full-screen map of the ``map`` tool, the preview minimap
of the node page, and the map where the editors pick a location. All three plot the same
mesh, so they all get their markers from :func:`gather_markers`. The function is in
``services/`` for two reasons. It assembles data from the contacts of the device and the
observation history, which is the layer below all the code that draws. Also, two screens
and one tool use it, and none of them can import from the others.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.geo import usable_fix
from ..core.models import NODE_TYPE_REPEATER, MapMarker

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import AppContext


async def gather_markers(ctx: AppContext, *, wait: bool = True) -> list[MapMarker]:
    """Collect all the nodes with a location to plot: the contacts of the device, and our node.

    The **contact list** of the companion is the authoritative source for the name of a
    node, its type (repeater or leaf), and its advertised position. The observations of
    the passive monitor have a location only for the rare node that broadcasts one in an
    advert. Thus the contacts set the markers. When MeshTerm heard a contact directly, the
    marker of that contact also gets signal detail from the observations.

    Args:
        ctx: The shared application context.
        wait: Whether to wait for the device to send the contacts and the position of our
            node, when the session has not cached them yet. ``False`` makes the markers
            from the data that MeshTerm already has (the cache and our own history), and
            never makes a round trip. Use it for a surface whose nodes are only context
            (the location picker). Without it, a companion that refuses the contacts read
            can keep that surface closed for twenty seconds or more.
    """
    observed = {n.node: n for n in ctx.repo.heard_nodes() if n.node}
    markers: list[MapMarker] = []
    seen: set[str] = set()

    contacts = await _contacts(ctx) if wait else (ctx.devstate.peek_contacts() or [])
    for contact in contacts:
        if not contact.has_location or not usable_fix(contact.lat, contact.lon):
            continue
        key = contact.key_prefix or (contact.public_key or "")[:12]
        if key:
            seen.add(key)
        markers.append(
            MapMarker(
                label=contact.name or key or "?",
                lat=float(contact.lat),
                lon=float(contact.lon),
                is_repeater=contact.is_repeater,
                detail=_signal_detail(observed.get(key)),
                key=contact.public_key or contact.key_prefix or None,
            )
        )

    # A node that MeshTerm heard when it advertised a location, but that is not in our
    # contacts.
    for node in observed.values():
        if not node.has_location or node.node in seen:
            continue
        if not usable_fix(node.lat, node.lon):
            continue
        markers.append(
            MapMarker(
                label=node.name or node.node or "?",
                lat=float(node.lat),
                lon=float(node.lon),
                is_repeater=node.is_repeater,
                detail=_signal_detail(node),
                key=node.public_key or node.node or None,
            )
        )

    if wait:
        self_marker = await _self_marker(ctx)
    else:
        info = ctx.devstate.peek_self_info()
        self_marker = _marker_for_self(info) if info is not None else None
    if self_marker is not None:
        markers.append(self_marker)
    return markers


async def _contacts(ctx: AppContext) -> list:
    """Get the contacts of the device, best-effort (none if MeshTerm cannot reach the device)."""
    try:
        return await ctx.devstate.contacts()
    except Exception:  # noqa: BLE001 - the map still works from observations alone
        return []


async def _self_marker(ctx: AppContext) -> MapMarker | None:
    """Make a marker for our node from the device, if its location is known."""
    try:
        info = await ctx.devstate.self_info()
    except Exception:  # noqa: BLE001 - the map is useful without our own position
        return None
    return _marker_for_self(info)


def _marker_for_self(info: dict) -> MapMarker | None:
    """The marker of our node from a self-info payload, or ``None`` when it has no fix."""
    lat, lon = _as_float(info.get("adv_lat")), _as_float(info.get("adv_lon"))
    if lat is None or lon is None or not usable_fix(lat, lon):
        return None  # a device with no fix reports 0/0 (or bad values out of range)
    return MapMarker(
        label=str(info.get("name") or "this node"),
        lat=lat,
        lon=lon,
        is_repeater=info.get("adv_type") == NODE_TYPE_REPEATER,
        is_self=True,
        key=str(info.get("public_key") or "") or None,
    )


def _signal_detail(node: object) -> str:
    """A short 'N pkts · +x.x dB' reception note for a heard node, or '' if it is not heard."""
    if node is None:
        return ""
    detail = f"{node.count} pkts"  # type: ignore[attr-defined]
    if node.median_snr is not None:  # type: ignore[attr-defined]
        detail += f" · {node.median_snr:+.1f} dB"  # type: ignore[attr-defined]
    return detail


def _as_float(value: object) -> float | None:
    """Best-effort float conversion (``None`` for a missing or bad value)."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
