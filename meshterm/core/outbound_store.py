# SPDX-License-Identifier: Apache-2.0
"""The store for outbound paths: the sites of this computer, and the fixed devices.

An *outbound path* is a path that starts at our node and goes out into the mesh, one or
more hops long. The user trusts it to carry the first hops of a route. A radio link is not
symmetric. A repeater that our node hears well can hear our node badly, and the evidence of
MeshTerm comes mostly from what our node hears. Thus the user, who knows which way out
works, gives these paths, in order of preference. The route suggestions then start on one
of them (refer to :meth:`~meshterm.services.topology.MeshTopology.scenarios`).

The paths depend on where the radio is. MeshTerm runs on a computer that the user takes
from site to site, with whichever companion is at hand. Thus the paths belong to the
computer, not to the radio, and this store keeps them in two forms:

* **Sites.** Each site (``Home``, ``Foulab``, ``Work``) has a name and an ordered list of
  outbound paths. One site is *active*: the site where the user is now. It applies to each
  companion that MeshTerm connects to.
* **Fixed devices.** A companion that never moves (a radio on the roof, a uConsole on a
  shelf) keeps its own list. The key is the public key of the companion. The active site
  does not apply to a fixed device.

:meth:`OutboundStore.lookup` gives the list that applies to a device, in this order: the
list of the fixed device, then the list of the active site, then no list.

Each site and each fixed device also holds **device routes** for single contacts (refer to
:meth:`~meshterm.core.connection.Device.set_route`). A device route is the route that the
radio uses to send to one contact. It depends on where the radio is, as an outbound path
does. Two maps hold them:

* **Kept routes.** The user chose the route and told MeshTerm to keep it. MeshTerm writes it
  back to the radio each time that the radio replaces it. :meth:`OutboundStore.route_owner`
  gives the record that holds the kept routes of a device, in the order of the lookup above.
  With no fixed device and no active site, no record holds them.
* **Saved routes** (sites only). These are routes that the radio learned at a site. When the
  user changes site, MeshTerm saves them here and clears them from the radio, because their
  first hops are out of reach at the new site. When the user comes back, MeshTerm writes
  them to the radio again.

The file is ``<config_dir>/outbound.json``, global machine state, the same as the other
JSON stores. It is intentionally outside the SQLite database, which can be different for
each run. The file is read into typed records (:class:`Site`, :class:`FixedDevice`) and
written back from them. The store never writes back the raw JSON that it read. Thus a field
that no record knows is removed after the next write.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from .atomicwrite import write_atomically

#: One outbound path: the hops in order from our node outward. Each hop is lowercase hex,
#: usually the 12-digit key prefix of the node, or a shorter hash that the user typed.
OutboundPath = tuple[str, ...]

#: One device route: the relays from our node to one contact, in order outward. Each hop is
#: as in :data:`OutboundPath`. Unlike an outbound path, a device route can be empty: no
#: relay, because the contact is a neighbour.
RoutePath = tuple[str, ...]

#: The source of a lookup when the list comes from a fixed device.
SOURCE_DEVICE = "device"
#: The source of a lookup when the list comes from the active site.
SOURCE_SITE = "site"

_HEX = frozenset("0123456789abcdef")


def clean_hop(value: object) -> str | None:
    """A hop as the store keeps it, or ``None`` when the value cannot be a hop.

    A hop is a whole number of bytes in hex, from one byte to a full public key. The store
    keeps it in lowercase, so that a comparison never depends on how the user typed it.

    Args:
        value: The raw hop, from the file or from the user.

    Returns:
        The hop in lowercase hex, or ``None``.
    """
    if not isinstance(value, str):
        return None
    hop = value.strip().lower()
    if not hop or len(hop) % 2 or len(hop) > 64 or not set(hop) <= _HEX:
        return None
    return hop


def clean_path(value: object) -> OutboundPath | None:
    """An outbound path as the store keeps it, or ``None`` when the value is not one.

    A path must have at least one hop, and each hop must pass :func:`clean_hop`. A node
    that comes two times in a path is never useful, because repeaters discard a packet that
    they relayed before. Thus such a path is not valid either.

    Args:
        value: The raw path: a list of hops, or a comma-separated spec (``"27d4,f2c2"``).

    Returns:
        The path as a tuple of hops, or ``None``.
    """
    if isinstance(value, str):
        value = [part for part in value.split(",") if part.strip()]
    if not isinstance(value, list | tuple) or not value:
        return None
    hops = tuple(clean_hop(hop) for hop in value)
    if any(hop is None for hop in hops) or len(set(hops)) != len(hops):
        return None
    return hops  # type: ignore[return-value] - the check above removed each None


def clean_route(value: object) -> RoutePath | None:
    """A device route as the store keeps it, or ``None`` when the value is not one.

    The rules are those of :func:`clean_path`, but a route can have no hops.

    Args:
        value: The raw route: a list of hops, or a comma-separated spec (``""`` for none).

    Returns:
        The route as a tuple of hops, or ``None``.
    """
    if isinstance(value, str):
        value = [part for part in value.split(",") if part.strip()]
    if isinstance(value, list | tuple) and not value:
        return ()
    return clean_path(value)


def _clean_routes(raw: object) -> dict[str, RoutePath]:
    """The valid device routes of a file entry, with the contact key of each as the key."""
    routes: dict[str, RoutePath] = {}
    for key, entry in raw.items() if isinstance(raw, dict) else ():
        contact = clean_hop(key)
        route = clean_route(entry)
        if contact is not None and route is not None:
            routes[contact] = route
    return routes


def _routes_json(routes: dict[str, RoutePath]) -> dict[str, list[str]]:
    """The device routes as written."""
    return {key: list(route) for key, route in routes.items()}


def _clean_paths(raw: object) -> list[OutboundPath]:
    """The valid outbound paths of a file entry, in order, without duplicates."""
    paths: list[OutboundPath] = []
    for entry in raw if isinstance(raw, list) else []:
        path = clean_path(entry)
        if path is not None and path not in paths:
            paths.append(path)
    return paths


@dataclass(slots=True)
class Site:
    """One site: a name, and the outbound paths that work there, in order of preference.

    Attributes:
        name: The name that the user gave the site (``Home``, ``Foulab``). Names are
            unique without regard to case.
        paths: The outbound paths, the preferred path first.
        kept: The kept device routes at this site, with the contact key as the key.
        saved: The device routes that the radio learned at this site, which MeshTerm saved
            when the user changed site. The contact key is the key.
    """

    name: str
    paths: list[OutboundPath] = field(default_factory=list)
    kept: dict[str, RoutePath] = field(default_factory=dict)
    saved: dict[str, RoutePath] = field(default_factory=dict)

    #: The fields that this record held before. It must never hold them again with a
    #: different meaning (refer to :data:`meshterm.core.preferences.RETIRED` for the
    #: reason that a name is never used again). There are none yet.
    RETIRED: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def read(cls, raw: object) -> Site | None:
        """Build a site from one file entry, or ``None`` when the entry has no name."""
        if not isinstance(raw, dict):
            return None
        name = str(raw.get("name") or "").strip()
        if not name:
            return None
        return cls(
            name=name,
            paths=_clean_paths(raw.get("paths")),
            kept=_clean_routes(raw.get("kept")),
            saved=_clean_routes(raw.get("saved")),
        )

    def to_json(self) -> dict:
        """The site as written: its own fields only."""
        return {
            "name": self.name,
            "paths": [list(path) for path in self.paths],
            "kept": _routes_json(self.kept),
            "saved": _routes_json(self.saved),
        }


@dataclass(slots=True)
class FixedDevice:
    """A companion that never moves, and its own outbound paths.

    Attributes:
        name: The name of the companion when the user fixed it. The list of fixed devices
            shows it, because a public key means nothing to the user.
        paths: The outbound paths, the preferred path first.
        kept: The kept device routes of this companion, with the contact key as the key.
    """

    name: str
    paths: list[OutboundPath] = field(default_factory=list)
    kept: dict[str, RoutePath] = field(default_factory=dict)

    #: The fields that this record held before. There are none yet (refer to
    #: :attr:`Site.RETIRED`).
    RETIRED: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def read(cls, raw: object) -> FixedDevice:
        """Build a fixed device from one file entry, with only the fields that it knows."""
        record = raw if isinstance(raw, dict) else {}
        return cls(
            name=str(record.get("name") or ""),
            paths=_clean_paths(record.get("paths")),
            kept=_clean_routes(record.get("kept")),
        )

    def to_json(self) -> dict:
        """The fixed device as written: its own fields only."""
        return {
            "name": self.name,
            "paths": [list(path) for path in self.paths],
            "kept": _routes_json(self.kept),
        }


@dataclass(slots=True)
class _OutboundFile:
    """All of the file: the sites, the active site, and the fixed devices.

    Attributes:
        sites: The sites, in the order in which the user made them.
        active: The name of the active site, or ``None`` when no site is active.
        devices: The fixed devices, with the lowercase public key of each as the key.
    """

    sites: list[Site] = field(default_factory=list)
    active: str | None = None
    devices: dict[str, FixedDevice] = field(default_factory=dict)

    #: The fields that this record held before. There are none yet (refer to
    #: :attr:`Site.RETIRED`).
    RETIRED: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def read(cls, raw: object) -> _OutboundFile:
        """Build the records from the parsed file, with only the fields that they know.

        A second site with the name of an earlier site is not read. An active name that
        names no site reads as no active site.
        """
        record = raw if isinstance(raw, dict) else {}
        sites: list[Site] = []
        for entry in record.get("sites") if isinstance(record.get("sites"), list) else []:
            site = Site.read(entry)
            if site is not None and _find(sites, site.name) is None:
                sites.append(site)
        active = _find(sites, str(record.get("active") or ""))
        devices: dict[str, FixedDevice] = {}
        raw_devices = record.get("devices")
        if isinstance(raw_devices, dict):
            for key, entry in raw_devices.items():
                device_key = clean_hop(key)
                if device_key is not None:
                    devices[device_key] = FixedDevice.read(entry)
        return cls(sites=sites, active=active.name if active else None, devices=devices)

    def to_json(self) -> dict:
        """The file as written: its own fields only."""
        return {
            "active": self.active,
            "sites": [site.to_json() for site in self.sites],
            "devices": {key: device.to_json() for key, device in self.devices.items()},
        }


@dataclass(frozen=True, slots=True)
class OutboundChoice:
    """The outbound paths that apply to one device, and where they come from.

    Attributes:
        paths: The outbound paths, the preferred path first. The tuple is empty when no
            list applies.
        source: :data:`SOURCE_DEVICE` for the list of a fixed device,
            :data:`SOURCE_SITE` for the list of the active site, or ``""`` for no list.
        name: The name of the site, or the name of the fixed device. It is ``""`` when
            no list applies.
    """

    paths: tuple[OutboundPath, ...] = ()
    source: str = ""
    name: str = ""


@dataclass(frozen=True, slots=True)
class RouteOwner:
    """The record that holds the kept device routes of one device.

    Attributes:
        source: :data:`SOURCE_DEVICE` for a fixed device, :data:`SOURCE_SITE` for the
            active site.
        name: The name of the site, or the name of the fixed device.
    """

    source: str
    name: str


class NoRouteOwnerError(LookupError):
    """No record can keep a device route: the device is not fixed, and no site is active."""


def _find(sites: list[Site], name: str) -> Site | None:
    """The site with ``name``, without regard to case, or ``None``."""
    wanted = name.strip().casefold()
    return (
        next((site for site in sites if site.name.casefold() == wanted), None) if wanted else None
    )


class OutboundStore:
    """Reads and writes the sites, the active site, and the fixed devices."""

    def __init__(self, path: Path) -> None:
        """Open the store on the location of a JSON file.

        Args:
            path: Path to the JSON file (made only at the first write).
        """
        self._path = path

    def _load(self) -> _OutboundFile:
        """Read the file into records (empty records when the file is absent or corrupt)."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return _OutboundFile()
        return _OutboundFile.read(data)

    def _write(self, records: _OutboundFile) -> None:
        """Write ``records`` atomically.

        If a crash occurs during the write, the previous file stays.
        """
        write_atomically(self._path, json.dumps(records.to_json(), indent=2))

    # -- sites ----------------------------------------------------------------------

    def sites(self) -> list[Site]:
        """All the sites, in the order in which the user made them."""
        return self._load().sites

    def site(self, name: str) -> Site | None:
        """The site with ``name``, without regard to case, or ``None``."""
        return _find(self._load().sites, name)

    def active_site(self) -> Site | None:
        """The active site, or ``None`` when no site is active."""
        records = self._load()
        return _find(records.sites, records.active or "")

    def set_active(self, name: str | None) -> None:
        """Make the site with ``name`` active, or make no site active with ``None``.

        Raises:
            KeyError: If no site has that name.
        """
        records = self._load()
        if name is None:
            records.active = None
        else:
            site = _find(records.sites, name)
            if site is None:
                raise KeyError(name)
            records.active = site.name
        self._write(records)

    def put_site(
        self, name: str, paths: list[OutboundPath], *, old_name: str | None = None
    ) -> Site:
        """Make a site, or replace the paths (and the name) of a site.

        Args:
            name: The name of the site. The spelling that the user gives is kept.
            paths: Its outbound paths, the preferred path first. Duplicates are removed.
            old_name: The current name of a site that this call renames. Omit it to make
                a site, or to replace the paths of the site with ``name``.

        Returns:
            The site as stored.

        Raises:
            ValueError: If the name is empty, or if another site already has it.
            KeyError: If ``old_name`` names no site.
        """
        name = name.strip()
        if not name:
            raise ValueError("a site needs a name")
        records = self._load()
        current = _find(records.sites, old_name if old_name is not None else name)
        if old_name is not None and current is None:
            raise KeyError(old_name)
        clash = _find(records.sites, name)
        if clash is not None and clash is not current:
            raise ValueError(f"a site named {clash.name} already exists")
        site = Site(name=name, paths=_clean_paths([list(path) for path in paths]))
        if current is not None:
            # A rename or a new list of paths keeps the device routes of the site.
            site.kept, site.saved = current.kept, current.saved
        if current is None:
            records.sites.append(site)
        else:
            records.sites[records.sites.index(current)] = site
            if records.active is not None and records.active.casefold() == current.name.casefold():
                records.active = site.name
        self._write(records)
        return site

    def remove_site(self, name: str) -> bool:
        """Delete the site with ``name``. When it was active, no site is active after this.

        Returns:
            Whether a site was deleted.
        """
        records = self._load()
        site = _find(records.sites, name)
        if site is None:
            return False
        records.sites.remove(site)
        if records.active is not None and records.active.casefold() == site.name.casefold():
            records.active = None
        self._write(records)
        return True

    # -- fixed devices --------------------------------------------------------------

    def fixed_devices(self) -> dict[str, FixedDevice]:
        """The fixed devices, with the lowercase public key of each as the key."""
        return self._load().devices

    def fixed_device(self, device_key: str) -> FixedDevice | None:
        """The record of a fixed device, or ``None`` when the device is not fixed."""
        key = clean_hop(device_key)
        return self._load().devices.get(key) if key else None

    def fix_device(self, device_key: str, name: str, paths: list[OutboundPath]) -> FixedDevice:
        """Fix a device, or replace the paths of a fixed device.

        Args:
            device_key: The public key of the companion.
            name: The name of the companion now, for the list of fixed devices.
            paths: Its outbound paths, the preferred path first. An empty list is valid:
                the device then has no outbound paths, whatever site is active.

        Returns:
            The fixed device as stored.

        Raises:
            ValueError: If ``device_key`` is not a key in hex.
        """
        key = clean_hop(device_key)
        if key is None:
            raise ValueError(f"not a device key: {device_key!r}")
        records = self._load()
        device = FixedDevice(name=name.strip(), paths=_clean_paths([list(p) for p in paths]))
        if key in records.devices:
            device.kept = records.devices[key].kept  # a new list of paths keeps the routes
        records.devices[key] = device
        self._write(records)
        return device

    def unfix_device(self, device_key: str) -> bool:
        """Remove the fixed list of a device, so that the active site applies to it again.

        Returns:
            Whether the device was fixed.
        """
        key = clean_hop(device_key)
        records = self._load()
        if key is None or records.devices.pop(key, None) is None:
            return False
        self._write(records)
        return True

    # -- the lookup -----------------------------------------------------------------

    def lookup(self, device_key: str | None) -> OutboundChoice:
        """The outbound paths that apply to the device with ``device_key``.

        The order is: the list of the fixed device, then the list of the active site, then
        no list. A fixed device keeps its list also when the list is empty. The user fixed
        the device to say what applies to it, and "nothing" is a valid answer.

        Args:
            device_key: The public key of the connected companion, or ``None`` when it is
                not known (then only the active site can apply).

        Returns:
            The paths, with their source.
        """
        records = self._load()
        key = clean_hop(device_key) if device_key else None
        device = records.devices.get(key) if key else None
        if device is not None:
            return OutboundChoice(tuple(device.paths), SOURCE_DEVICE, device.name)
        site = _find(records.sites, records.active or "")
        if site is not None:
            return OutboundChoice(tuple(site.paths), SOURCE_SITE, site.name)
        return OutboundChoice()

    # -- device routes --------------------------------------------------------------

    def _owner_record(
        self, records: _OutboundFile, device_key: str | None
    ) -> tuple[RouteOwner, Site | FixedDevice] | None:
        """The record that holds the kept routes of a device, in the order of the lookup."""
        key = clean_hop(device_key) if device_key else None
        device = records.devices.get(key) if key else None
        if device is not None:
            return RouteOwner(SOURCE_DEVICE, device.name), device
        site = _find(records.sites, records.active or "")
        if site is not None:
            return RouteOwner(SOURCE_SITE, site.name), site
        return None

    def route_owner(self, device_key: str | None) -> RouteOwner | None:
        """The record that holds the kept routes of a device, or ``None`` when none can.

        The order is that of :meth:`lookup`: the fixed device, then the active site.
        """
        found = self._owner_record(self._load(), device_key)
        return found[0] if found is not None else None

    def kept_routes(self, device_key: str | None) -> dict[str, RoutePath]:
        """The kept device routes that apply to a device, with the contact key as the key."""
        found = self._owner_record(self._load(), device_key)
        return dict(found[1].kept) if found is not None else {}

    def kept_route(self, device_key: str | None, contact_key: str) -> RoutePath | None:
        """The kept device route to one contact, or ``None`` when the user keeps none."""
        key = clean_hop(contact_key)
        return self.kept_routes(device_key).get(key) if key else None

    def keep_route(self, device_key: str | None, contact_key: str, route: RoutePath) -> RouteOwner:
        """Keep a device route to one contact, in the record that applies to the device.

        Args:
            device_key: The public key of the companion, or ``None``.
            contact_key: The public key of the contact.
            route: The relays, in order outward. An empty route means no relay.

        Returns:
            The record that keeps the route.

        Raises:
            NoRouteOwnerError: If the device is not fixed and no site is active.
            ValueError: If the contact key or the route is not valid.
        """
        key = clean_hop(contact_key)
        clean = clean_route(list(route))
        if key is None or clean is None:
            raise ValueError(f"not a device route: {contact_key!r} {route!r}")
        records = self._load()
        found = self._owner_record(records, device_key)
        if found is None:
            raise NoRouteOwnerError(contact_key)
        found[1].kept[key] = clean
        self._write(records)
        return found[0]

    def unkeep_route(self, device_key: str | None, contact_key: str) -> bool:
        """Stop keeping the device route to one contact.

        Returns:
            Whether a kept route was removed.
        """
        key = clean_hop(contact_key)
        records = self._load()
        found = self._owner_record(records, device_key)
        if key is None or found is None or found[1].kept.pop(key, None) is None:
            return False
        self._write(records)
        return True

    def saved_routes(self, site_name: str) -> dict[str, RoutePath]:
        """The device routes saved for a site, with the contact key as the key."""
        site = _find(self._load().sites, site_name)
        return dict(site.saved) if site is not None else {}

    def save_routes(self, site_name: str, routes: dict[str, RoutePath]) -> None:
        """Add device routes to the saved routes of a site. A newer route replaces an older.

        Raises:
            KeyError: If no site has that name.
        """
        records = self._load()
        site = _find(records.sites, site_name)
        if site is None:
            raise KeyError(site_name)
        site.saved.update(_clean_routes({key: list(route) for key, route in routes.items()}))
        self._write(records)

    def take_saved(self, site_name: str) -> dict[str, RoutePath]:
        """Remove and return the saved device routes of a site (empty for no such site)."""
        records = self._load()
        site = _find(records.sites, site_name)
        if site is None or not site.saved:
            return {}
        taken, site.saved = site.saved, {}
        self._write(records)
        return taken
