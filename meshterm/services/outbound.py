# SPDX-License-Identifier: Apache-2.0
"""Outbound paths in use: the list that applies to the connected device, and its evidence.

The store (:mod:`meshterm.core.outbound_store`) keeps the sites, the active site, and the
fixed devices. This module joins the store to the live session. It finds the public key
of the connected companion, so that the store can give the list that applies to it. It
also builds the evidence graph and reads the two directions of the first link of a path
(refer to :meth:`~meshterm.services.topology.MeshTopology.readings`). The pages show this
evidence beside each path, because a radio link is not symmetric: the user must see how
well a repeater hears our node, and not only how well our node hears the repeater.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ..core.outbound_store import OutboundChoice, OutboundPath
from .topology import MeshTopology, build_topology

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import AppContext
    from ..core.models import Contact


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    """The connected companion as the Outbound paths features know it.

    Attributes:
        key: The public key of the companion in lowercase hex, or ``""`` when it is not
            known (no device is connected).
        name: The name of the companion, or ``""``.
    """

    key: str = ""
    name: str = ""


@dataclass(frozen=True, slots=True)
class FirstLink:
    """The evidence for the first link of an outbound path, one direction at a time.

    Attributes:
        out_snr: The median SNR (dB) at which the first hop received our node, or ``None``
            when no reading exists. Only traces give these readings.
        out_count: How many readings ``out_snr`` comes from.
        in_snr: The median SNR (dB) at which our node received the first hop, or
            ``None``.
        in_count: How many readings ``in_snr`` comes from.
        last_heard: The most recent time that the link carried a packet, from any source,
            or ``None`` when the graph has no such link.
    """

    out_snr: float | None = None
    out_count: int = 0
    in_snr: float | None = None
    in_count: int = 0
    last_heard: datetime | None = None


async def device_identity(ctx: AppContext) -> DeviceIdentity:
    """The key and the name of the connected companion, from the session cache.

    A page about outbound paths works also with no device. The sites belong to the
    computer, and the user can prepare them anywhere. Thus a failed read gives an empty
    identity, and not an error.
    """
    try:
        info = await ctx.devstate.self_info()
    except Exception:  # noqa: BLE001 - an optional read. With no device, only sites apply.
        return DeviceIdentity()
    key = str(info.get("public_key") or "").lower().removeprefix("0x")
    return DeviceIdentity(key=key, name=str(info.get("name") or ""))


async def session_contacts(ctx: AppContext) -> list[Contact]:
    """The contacts of the connected companion, or an empty list when there is no device."""
    try:
        return list(await ctx.devstate.contacts())
    except Exception:  # noqa: BLE001 - an optional read. The pages still work without it.
        return []


async def current_choice(ctx: AppContext) -> OutboundChoice:
    """The outbound paths that apply to the connected companion now.

    Refer to :meth:`~meshterm.core.outbound_store.OutboundStore.lookup` for the order.
    """
    identity = await device_identity(ctx)
    return ctx.outbound_store.lookup(identity.key or None)


def session_topology(ctx: AppContext, self_key: str, contacts: list[Contact]) -> MeshTopology:
    """Build the evidence graph from the stored data, around our node.

    Args:
        ctx: The shared application context (for the repository).
        self_key: The public key of our node, or ``""`` when it is not known.
        contacts: The contacts of the companion, for the names and the canonical ids.

    Returns:
        The populated graph.
    """
    return build_topology(
        self_id=self_key or "local",
        contacts=contacts,
        trace_paths=ctx.repo.trace_paths(),
        packet_paths=ctx.repo.packet_paths(),
        neighbour_links=ctx.repo.neighbour_links(),
    )


def first_link(topo: MeshTopology, path: OutboundPath) -> FirstLink:
    """The evidence for the link from our node to the first hop of ``path``.

    Args:
        topo: The evidence graph around our node.
        path: The outbound path.

    Returns:
        The two directions of the first link. Each field is empty when the graph has no
        evidence for it.
    """
    if not path:
        return FirstLink()
    hop = topo.canonical(path[0]) or path[0]
    us = topo.self_id
    out = topo.readings(us, hop)
    heard = topo.readings(hop, us)
    link = topo.link(us, hop)
    return FirstLink(
        out_snr=statistics.median(out) if out else None,
        out_count=len(out),
        in_snr=statistics.median(heard) if heard else None,
        in_count=len(heard),
        last_heard=link.last_seen if link is not None else None,
    )
