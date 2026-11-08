# SPDX-License-Identifier: Apache-2.0
"""Device routes: forget, set, and keep the route that the radio uses to each contact.

The companion holds one *device route* for each contact: the relays, in order from our
node outward. It sends each direct message, login, request, and admin command to the
contact on that route. It learns the route from a path return: the answer to a flood that
reached the contact. It does not keep the best route. It replaces the route each time that
a path return arrives, and it never forgets a route that stopped to work. After that, each
packet sent on the route is lost with no error.

This module gives the user control over that route:

* **Forget.** The radio clears the route, so the next message floods, and the radio
  learns a new route from the answer (:meth:`RouteKeeper.forget`).
* **Set.** The radio gets a route that the user chose (:meth:`RouteKeeper.set`). It stays
  until the radio learns another one.
* **Keep.** MeshTerm remembers the route, and writes it back each time that the radio
  replaces it (:meth:`RouteKeeper.keep`). A kept route belongs to the active site, or to a
  fixed device (refer to :mod:`meshterm.core.outbound_store`). A kept route whose first
  hop is silent is not written: the repeater is probably off, or out of reach from here.

When the user changes site, the routes that the radio learned at the old site start on
hops that are out of reach now. :meth:`RouteKeeper.reconcile` saves them for that site
and clears them from the radio, and writes back the routes saved for the new site. Thus
the routes of the radio follow the user from site to site. The reconcile runs at each
connection and after each change of site.

Each action here is local to the radio: nothing is transmitted, except by
:meth:`RouteKeeper.discover`, which sends one path discovery.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ..core.events import EventKind, MeshEvent
from ..core.models import Contact, DiscoveredRoute, utcnow
from ..core.outbound_store import (
    SOURCE_DEVICE,
    OutboundStore,
    RouteOwner,
    RoutePath,
    Site,
)
from ..persistence.logging import get_logger
from .outbound import device_identity, session_topology

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable, Iterable

    from ..context import AppContext
    from ..core.connection import Device
    from .topology import MeshTopology

#: How the radio got the route that it holds now (refer to :attr:`RouteStatus.origin`).
ORIGIN_FLOOD = ""
ORIGIN_LEARNED = "learned"
ORIGIN_SET = "set"
ORIGIN_KEPT = "kept"


def _matches(radio_hop: str, hop: str) -> bool:
    """Whether a hop that the radio holds can be the stored hop: one is a prefix of the other.

    The radio holds each hop at its hash width (one to three bytes). The store holds a
    longer key prefix when it knows one.
    """
    radio_hop, hop = radio_hop.lower(), hop.lower()
    if not radio_hop or not hop:
        return False
    return hop.startswith(radio_hop) or radio_hop.startswith(hop)


def same_route(radio: tuple[str, ...] | None, route: RoutePath) -> bool:
    """Whether the route that the radio holds is ``route``, hop by hop, at its own width."""
    if radio is None or len(radio) != len(route):
        return False
    return all(_matches(a, b) for a, b in zip(radio, route, strict=True))


def first_hops(paths: Iterable[tuple[str, ...]]) -> list[str]:
    """The first hop of each outbound path, in order, without duplicates."""
    hops: list[str] = []
    for path in paths:
        if path and path[0] not in hops:
            hops.append(path[0])
    return hops


def starts_on(route: tuple[str, ...] | None, hops: Iterable[str]) -> bool:
    """Whether a route has a first relay, and that relay is one of ``hops``."""
    return bool(route) and any(_matches(route[0], hop) for hop in hops)  # type: ignore[index]


def silent_since(
    topo: MeshTopology,
    route: RoutePath,
    contact_key: str,
    silent_before: datetime | None,
) -> datetime | None:
    """When the first hop of a route was last heard, if that is before ``silent_before``.

    The first hop is the first relay, or the contact itself for a route with no relay. A
    first hop with no evidence at all is not silent, because nothing says so. This is the
    rule of the outbound paths (refer to
    :meth:`~meshterm.services.topology.MeshTopology.outbound_scenarios`).

    Returns:
        The time of the last packet on the link from our node to the first hop, when the
        hop is silent. ``None`` when the hop is not silent.
    """
    if silent_before is None:
        return None
    first = route[0] if route else contact_key.lower()[:12]
    node = topo.canonical(first) or first
    link = topo.link(topo.self_id, node)
    last = link.last_seen if link is not None else None
    return last if last is not None and last < silent_before else None


def silent_before_now(ctx: AppContext) -> datetime | None:
    """The time before which a first hop counts as silent (the ``outbound_silent_hours`` window)."""
    prefs = getattr(ctx, "preferences", None)
    if prefs is None:
        return None
    return utcnow() - timedelta(hours=prefs.outbound_silent_hours)


@dataclass(frozen=True, slots=True)
class RouteStatus:
    """What MeshTerm knows about the device route to one contact.

    Attributes:
        hops: The route that the radio holds now, or ``None`` for no route (flood).
        origin: How the radio got that route: :data:`ORIGIN_KEPT`, :data:`ORIGIN_SET` (the
            user set it in this session), :data:`ORIGIN_LEARNED`, or :data:`ORIGIN_FLOOD`
            for no route.
        changed_at: When the route changed in this session, if MeshTerm saw it change.
        kept: The kept route to the contact, or ``None`` when the user keeps none.
        owner: The record that keeps routes for the connected device (the fixed device or
            the active site), or ``None`` when none can.
        skipped_since: When the kept route is not in the radio because its first hop is
            silent, the time that the hop was last heard. Otherwise ``None``.
    """

    hops: tuple[str, ...] | None
    origin: str
    changed_at: datetime | None = None
    kept: RoutePath | None = None
    owner: RouteOwner | None = None
    skipped_since: datetime | None = None

    @property
    def kept_in_radio(self) -> bool:
        """Whether the radio holds the kept route now."""
        return self.kept is not None and same_route(self.hops, self.kept)


@dataclass(slots=True)
class RouteReport:
    """What one reconcile did.

    Attributes:
        saved: For each other site, how many learned routes MeshTerm saved for it and
            cleared from the radio.
        restored: How many saved routes of the active site MeshTerm wrote back.
        kept_written: How many kept routes MeshTerm wrote back.
        kept_skipped: The names of the contacts whose kept route has a silent first hop.
        failed: The names of the contacts whose route the radio refused to change.
        site: The name of the active site, or ``""``.
    """

    saved: dict[str, int] = field(default_factory=dict)
    restored: int = 0
    kept_written: int = 0
    kept_skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    site: str = ""

    @property
    def changed(self) -> bool:
        """Whether the reconcile changed a route in the radio."""
        return bool(self.saved) or bool(self.restored) or bool(self.kept_written)


def _routes(count: int) -> str:
    return f"{count} route" if count == 1 else f"{count} routes"


def report_notice(report: RouteReport) -> str:
    """The notice that tells the user what a reconcile did, or ``""`` when it did nothing."""
    parts: list[str] = []
    for site, count in report.saved.items():
        parts.append(f"saved {_routes(count)} for {site} and cleared them from the radio")
    if report.restored:
        where = f" for {report.site}" if report.site else ""
        parts.append(f"restored {_routes(report.restored)}{where}")
    if report.kept_written:
        parts.append(f"wrote back {_routes(report.kept_written)} you keep")
    if report.kept_skipped:
        parts.append(f"skipped {_routes(len(report.kept_skipped))} with a silent first hop")
    if report.failed:
        parts.append(f"the radio refused {_routes(len(report.failed))}")
    if not parts:
        return ""
    text = " · ".join(parts)
    return text[0].upper() + text[1:]


def _key(contact: Contact) -> str:
    return (contact.public_key or "").lower().removeprefix("0x")


class RouteKeeper:
    """The session service for device routes: the actions, the reconcile, and the listener.

    The interactive session starts it with the other always-on services
    (:meth:`start`). Then it listens for the route changes of the radio, and the context
    calls :meth:`on_connected` for each stable connection. A scripted CLI run does not
    start it, and calls the actions and :meth:`reconcile` directly.
    """

    def __init__(self, ctx: AppContext) -> None:
        """Make the service idle."""
        self._ctx = ctx
        self._armed = False
        self._unsubscribe: Callable[[], None] | None = None
        self._task: asyncio.Future | None = None
        self._settled: int | None = None
        #: The routes that MeshTerm wrote in this session, and when, by contact key.
        self._written: dict[str, tuple[tuple[str, ...], datetime]] = {}
        #: When the radio said that it learned a new route, by contact key.
        self._learned: dict[str, datetime] = {}
        #: What the last reconcile at connection did, for the menu to show.
        self.last_report: RouteReport | None = None

    # -- the session --------------------------------------------------------------------

    async def start(self) -> None:
        """Listen for route changes, and reconcile a device that is already connected."""
        if self._armed:
            return
        self._armed = True
        self._unsubscribe = self._ctx.events.subscribe(self._on_event, EventKind.ROUTE)
        device = self._ctx._device
        if device is not None:
            self.on_connected(device)

    def on_connected(self, device: Device) -> None:
        """Reconcile a new stable connection in the background. Idle until :meth:`start`."""
        if not self._armed or self._settled == id(device):
            return
        self._settled = id(device)
        self._task = asyncio.ensure_future(self._settle())

    async def _settle(self) -> None:
        """Run one reconcile at connection, and write what it did to the log."""
        try:
            report = await self.reconcile()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a best-effort action. It never breaks the session
            get_logger().warning("couldn't reconcile the device routes: %s", exc)
            return
        self.last_report = report
        notice = report_notice(report)
        if notice:
            get_logger().info("device routes: %s", notice)

    def _on_event(self, event: MeshEvent):  # type: ignore[no-untyped-def]
        """Note a route that the radio learned. Write a kept route back (the hub runs it)."""
        change = event.route
        if change is None:
            return None
        self._learned[change.public_key] = utcnow()
        self._written.pop(change.public_key, None)
        devstate = getattr(self._ctx, "devstate", None)
        if devstate is not None:
            devstate.invalidate_contacts()
        return self._restore_kept(change.public_key)

    async def _restore_kept(self, contact_key: str) -> None:
        """Write the kept route to one contact back, after the radio replaced it."""
        try:
            identity = await device_identity(self._ctx)
            kept = self._ctx.outbound_store.kept_route(identity.key or None, contact_key)
            if kept is None:
                return
            contacts = await self._ctx.devstate.contacts()
            contact = next((c for c in contacts if _key(c) == contact_key), None)
            if contact is None or same_route(contact.route_hops, kept):
                return
            topo = session_topology(self._ctx, identity.key, contacts)
            if silent_since(topo, kept, contact_key, silent_before_now(self._ctx)) is not None:
                get_logger().info(
                    "the radio replaced the kept route to %s; its first hop is silent, so "
                    "the new route stays",
                    contact.name,
                )
                return
            await self._write(contact, kept)
            get_logger().info("wrote the kept route to %s back to the radio", contact.name)
        except Exception as exc:  # noqa: BLE001 - a background action. The log says why
            get_logger().warning("couldn't write a kept route back: %s", exc)

    async def aclose(self) -> None:
        """Stop listening at the end of the session, and stop a reconcile in progress."""
        self._armed = False
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - ends the session
                pass

    # -- the actions ----------------------------------------------------------------------

    def _invalidate(self) -> None:
        devstate = getattr(self._ctx, "devstate", None)
        if devstate is not None:
            devstate.invalidate_contacts()

    async def _write(self, contact: Contact, route: RoutePath) -> tuple[str, ...]:
        device = await self._ctx.device()
        written = await device.set_route(contact, tuple(route))
        self._written[_key(contact)] = (written, utcnow())
        self._invalidate()
        return written

    async def forget(self, contact: Contact) -> None:
        """Clear the device route to ``contact``, so that its next message floods.

        A kept route stays kept: the user forgets what the radio holds, not the choice.
        """
        device = await self._ctx.device()
        await device.reset_route(contact)
        self._written.pop(_key(contact), None)
        self._invalidate()

    async def set(self, contact: Contact, route: RoutePath) -> tuple[str, ...]:
        """Write a device route to ``contact``. It stays until the radio learns another.

        Returns:
            The route as the radio holds it now, at its hash width.
        """
        return await self._write(contact, route)

    async def keep(self, contact: Contact, route: RoutePath) -> RouteOwner:
        """Keep a device route to ``contact``, and write it to the radio now.

        Returns:
            The record that keeps it: the fixed device, or the active site.

        Raises:
            NoRouteOwnerError: If the device is not fixed and no site is active.
        """
        identity = await device_identity(self._ctx)
        owner = self._ctx.outbound_store.keep_route(identity.key or None, _key(contact), route)
        await self._write(contact, route)
        return owner

    async def unkeep(self, contact: Contact) -> bool:
        """Stop keeping the route to ``contact``. The radio keeps the route that it holds."""
        identity = await device_identity(self._ctx)
        return self._ctx.outbound_store.unkeep_route(identity.key or None, _key(contact))

    async def discover(self, contact: Contact) -> DiscoveredRoute | None:
        """Send one path discovery to ``contact`` (one flood), and return the two routes."""
        device = await self._ctx.device()
        return await device.discover_route(contact)

    # -- what the pages show ----------------------------------------------------------

    def status(
        self,
        contact: Contact,
        *,
        device_key: str,
        topo: MeshTopology | None = None,
        silent_before: datetime | None = None,
    ) -> RouteStatus:
        """The device route to ``contact``, how it got there, and what the user keeps.

        Args:
            contact: The contact, as the radio reported it (with its route).
            device_key: The public key of the connected companion, or ``""``.
            topo: The evidence graph, to find a silent first hop. Without it, no kept route
                counts as skipped.
            silent_before: The time before which a first hop counts as silent.
        """
        store: OutboundStore = self._ctx.outbound_store
        key = _key(contact)
        owner = store.route_owner(device_key or None)
        kept = store.kept_route(device_key or None, key) if key else None
        hops = contact.route_hops
        written = self._written.get(key)
        if hops is None:
            origin, changed = ORIGIN_FLOOD, written[1] if written else None
        elif kept is not None and same_route(hops, kept):
            origin, changed = ORIGIN_KEPT, written[1] if written else None
        elif written is not None and same_route(hops, written[0]):
            origin, changed = ORIGIN_SET, written[1]
        else:
            origin, changed = ORIGIN_LEARNED, self._learned.get(key)
        skipped = None
        if kept is not None and not same_route(hops, kept) and topo is not None:
            skipped = silent_since(topo, kept, key, silent_before)
        return RouteStatus(hops, origin, changed, kept, owner, skipped)

    # -- the reconcile -------------------------------------------------------------------

    async def reconcile(self) -> RouteReport:
        """Make the routes of the radio follow the active site and the kept routes.

        In order:

        1. A route that the radio learned at another site (it starts on a first hop of
           that site, and on no first hop of the active site) is saved for that site and
           cleared from the radio.
        2. The routes saved for the active site are written back, unless the radio learned
           a route here since, or the first hop is silent.
        3. Each kept route that the radio does not hold is written, unless its first hop is
           silent.

        Steps 1 and 2 apply only to a device that is not fixed: a fixed device never moves.

        Returns:
            What the reconcile did.
        """
        ctx = self._ctx
        store: OutboundStore = ctx.outbound_store
        identity = await device_identity(ctx)
        device_key = identity.key or None
        contacts = await ctx.devstate.contacts(force=True)
        topo = session_topology(ctx, identity.key, contacts)
        silent_before = silent_before_now(ctx)
        by_key = {_key(c): c for c in contacts if _key(c)}
        kept = store.kept_routes(device_key)
        owner = store.route_owner(device_key)
        report = RouteReport(site=owner.name if owner is not None else "")
        cleared: set[str] = set()
        if owner is not None and owner.source != SOURCE_DEVICE:
            active = store.active_site()
            if active is not None:
                cleared = await self._save_elsewhere(store, active, by_key, kept, report)
                await self._restore_saved(store, active, by_key, kept, cleared, topo, report)
        for key, route in kept.items():
            contact = by_key.get(key)
            if contact is None:
                continue
            radio = None if key in cleared else contact.route_hops
            if same_route(radio, route):
                continue
            if silent_since(topo, route, key, silent_before) is not None:
                report.kept_skipped.append(contact.name)
                continue
            try:
                await self._write(contact, route)
                report.kept_written += 1
            except Exception:  # noqa: BLE001 - one refusal must not stop the others
                report.failed.append(contact.name)
        return report

    async def _save_elsewhere(
        self,
        store: OutboundStore,
        active: Site,
        by_key: dict[str, Contact],
        kept: dict[str, RoutePath],
        report: RouteReport,
    ) -> set[str]:
        """Save and clear the learned routes that start on another site's first hop."""
        own = first_hops(active.paths)
        others = [
            (site.name, first_hops(site.paths))
            for site in store.sites()
            if site.name.casefold() != active.name.casefold()
        ]
        to_save: dict[str, dict[str, tuple[str, ...]]] = {}
        for key, contact in by_key.items():
            route = contact.route_hops
            if key in kept or not route or starts_on(route, own):
                continue
            site = next((name for name, hops in others if starts_on(route, hops)), None)
            if site is not None:
                to_save.setdefault(site, {})[key] = route
        cleared: set[str] = set()
        if not to_save:
            return cleared
        device = await self._ctx.device()
        for site, routes in to_save.items():
            store.save_routes(site, routes)
            for key in routes:
                try:
                    await device.reset_route(by_key[key])
                    cleared.add(key)
                except Exception:  # noqa: BLE001 - one refusal must not stop the others
                    report.failed.append(by_key[key].name)
            if any(key in cleared for key in routes):
                report.saved[site] = sum(1 for key in routes if key in cleared)
        self._invalidate()
        return cleared

    async def _restore_saved(
        self,
        store: OutboundStore,
        active: Site,
        by_key: dict[str, Contact],
        kept: dict[str, RoutePath],
        cleared: set[str],
        topo: MeshTopology,
        report: RouteReport,
    ) -> None:
        """Write back the routes saved for the active site, where they still apply."""
        silent_before = silent_before_now(self._ctx)
        for key, route in store.take_saved(active.name).items():
            contact = by_key.get(key)
            if contact is None or key in kept:
                continue
            radio = None if key in cleared else contact.route_hops
            if radio is not None:
                continue  # the radio learned a route here since: it is newer
            if silent_since(topo, route, key, silent_before) is not None:
                continue
            try:
                await self._write(contact, route)
                report.restored += 1
            except Exception:  # noqa: BLE001 - one refusal must not stop the others
                report.failed.append(contact.name)

    async def change_site(self, name: str | None) -> RouteReport | None:
        """Make a site active (or none), then reconcile the routes when a radio is connected.

        Returns:
            What the reconcile did, or ``None`` when no radio is connected. The next
            connection then reconciles.

        Raises:
            KeyError: If no site has that name.
        """
        self._ctx.outbound_store.set_active(name)
        if not self._ctx.is_connected:
            return None
        return await self.reconcile()

    # -- automatic routes ------------------------------------------------------------

    async def prepare_message(self, contact: Contact) -> tuple[str, ...] | None:
        """Set the suggested route before a direct message, when the ``auto_route`` preference asks.

        MeshTerm acts only when the radio has no route to the contact, or when its route
        starts on a first hop of another site. A kept route is never changed here: the
        reconcile owns it. The write is local, and the message itself is the only
        transmission.

        Returns:
            The route that MeshTerm wrote, or ``None`` when it wrote nothing.
        """
        ctx = self._ctx
        prefs = getattr(ctx, "preferences", None)
        if prefs is None or not prefs.auto_route:
            return None
        key = _key(contact)
        if not key:
            return None
        store: OutboundStore = ctx.outbound_store
        identity = await device_identity(ctx)
        if store.kept_route(identity.key or None, key) is not None:
            return None
        contacts = await ctx.devstate.contacts()
        held = next((c for c in contacts if _key(c) == key), None)
        if held is None:
            return None
        choice = store.lookup(identity.key or None)
        if held.route_hops is not None:
            others = [
                hop
                for site in store.sites()
                if choice.source == SOURCE_DEVICE or site.name.casefold() != choice.name.casefold()
                for hop in first_hops(site.paths)
            ]
            own = first_hops(choice.paths)
            if not starts_on(held.route_hops, others) or starts_on(held.route_hops, own):
                return None
        topo = session_topology(ctx, identity.key, contacts)
        target = topo.canonical(key[:12]) or key[:12]
        outbound = tuple(tuple(topo.canonical(h) or h for h in path) for path in choice.paths)
        suggested = topo.suggested(target, outbound=outbound, silent_before=silent_before_now(ctx))
        if suggested is None:
            return None
        written = await self._write(held, suggested.hops)
        get_logger().info(
            "set the suggested route to %s before the message: %s",
            held.name,
            ",".join(written) or "no relays",
        )
        return written
