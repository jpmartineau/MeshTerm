# SPDX-License-Identifier: Apache-2.0
"""The regions MeshTerm knows by name, and the scope each channel is sent under.

A scoped flood carries no region name — only a transport code keyed on one (see
:mod:`~meshterm.core.regions`) — so *which* region a packet was flooded into can only be
answered by trying the names already known. This store is that list. A name arrives from
wherever MeshTerm meets one:

* ``default`` — the companion's own default flood scope, read or set on Device config;
* ``channel`` — a scope given to a channel, so its messages stay in its region;
* ``repeater`` — a repeater's answer to the regions request, or its ``region`` listing,
  which also records *which* repeaters carry it (the node page lists them, and a scoped
  send that nothing relayed can say whether anything in earshot was ever heard to carry
  its region) — and, beside the regions, each repeater's last whole answer: whether it
  relays unscoped floods too, and when it said so (:class:`CarriedAnswer`);
* ``typed`` — a name the reader entered themselves.

The store also holds **channel scopes**: the region a channel's messages are sent under.
The firmware has no per-channel scope (``// TODO: have per-channel send_scope``), so a
client that wants one sets the companion's session scope just before each channel send,
which is what every official app does. The scope is keyed by the channel's intrinsic
identity (:func:`~meshterm.core.channels.channel_identity`), exactly like a mute, so it
follows the channel across slots and two devices sharing the channel share its scope.

Resolution is memoized here too (:meth:`RegionStore.scope_of`): a packet list repaints
often and resolving a scoped frame costs an HMAC per known name, so an answer is kept per
``(body, code)`` until the set of names changes.

Like the other operator state (mutes, watched nodes, admin passwords) this is global
machine state in a small JSON file (``<config_dir>/regions.json``), read once and written
atomically on each real change. It writes from its typed records, never from the document
it read, so a field no record knows is gone at the next save.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from .atomicwrite import write_atomically
from .regions import (
    WILDCARD,
    RegionNameError,
    Scope,
    frame_scope,
    normalize,
    raw_scope_body,
    validate,
)

#: Where a region name was learned — the closed set :attr:`KnownRegion.sources` draws from.
SOURCES = ("default", "channel", "repeater", "typed")

#: Resolutions kept before the memo is cleared wholesale (a long capture meets many bodies;
#: a repaint meets the same few hundred again and again).
_MEMO_CAP = 4096


@dataclass(frozen=True)
class KnownRegion:
    """One region MeshTerm knows by name.

    Attributes:
        name: The bare region name (no ``#``).
        sources: Where it was learned, members of :data:`SOURCES`, in first-learned order.
        repeaters: The 12-hex node ids of repeaters that were heard to carry it.
        learned_at: When it was first learned (UTC).
    """

    name: str
    sources: tuple[str, ...] = ()
    repeaters: tuple[str, ...] = ()
    learned_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    #: Fields this record once held and must never hold again under another meaning (see
    #: :data:`meshterm.core.preferences.RETIRED` for why a name is never reused).
    RETIRED: ClassVar[frozenset[str]] = frozenset()


@dataclass(frozen=True)
class CarriedAnswer:
    """The last whole answer one repeater gave about what it carries.

    The regions themselves live on :class:`KnownRegion` (``repeaters``), where resolution
    wants them; this keeps what does not belong to any one region — whether the repeater
    also relays *unscoped* floods (the ``*`` a region list leads with, which names no
    region), and when it said so. A repeater with no record here was never asked, which is
    a different thing from a repeater that answered with nothing.

    Attributes:
        node: The repeater's 12-hex node id.
        unscoped: Whether its answer included the wildcard.
        answered_at: When it answered (UTC).
    """

    node: str
    unscoped: bool
    answered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    #: Fields this record once held and must never hold again under another meaning.
    RETIRED: ClassVar[frozenset[str]] = frozenset()


class RegionStore:
    """Reads and writes the known regions and the channel scopes, memory-first.

    Interact through :meth:`names`/:meth:`regions` (what is known), :meth:`learn` and
    :meth:`forget` (changing it), :meth:`channel_scope`/:meth:`set_channel_scope` (a
    channel's send scope), :meth:`carriers` (which repeaters carry a region), and
    :meth:`scope_of` (what a received frame's scope is). :attr:`revision` bumps on every
    change, for a screen that memoizes on it.
    """

    def __init__(self, path: Path) -> None:
        """Open the store against a JSON file location.

        Args:
            path: Path to the JSON state file (created lazily on the first change).
        """
        self._path = path
        self._regions: dict[str, KnownRegion] | None = None
        self._channels: dict[str, str] = {}
        self._answers: dict[str, CarriedAnswer] = {}
        self._memo: dict[tuple[bytes, int], str | None] = {}
        self.revision = 0

    # -- loading ---------------------------------------------------------------------

    @property
    def _state(self) -> dict[str, KnownRegion]:
        """The name -> record map, loaded from disk on first access."""
        if self._regions is None:
            self._regions, self._channels, self._answers = self._load()
        return self._regions

    def _load(
        self,
    ) -> tuple[dict[str, KnownRegion], dict[str, str], dict[str, CarriedAnswer]]:
        """Parse the file, or start empty on a missing or corrupt one."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}, {}, {}
        if not isinstance(data, dict):
            return {}, {}, {}
        regions: dict[str, KnownRegion] = {}
        for entry in data.get("regions") or []:
            region = _region_from_json(entry)
            if region is not None:
                regions[region.name] = region
        channels: dict[str, str] = {}
        for identity, name in (data.get("channels") or {}).items():
            try:
                channels[str(identity)] = validate(str(name))
            except RegionNameError:
                continue
        answers: dict[str, CarriedAnswer] = {}
        for entry in data.get("answers") or []:
            answer = _answer_from_json(entry)
            if answer is not None:
                answers[answer.node] = answer
        return regions, channels, answers

    # -- what is known ---------------------------------------------------------------

    def regions(self) -> list[KnownRegion]:
        """Every known region, in the order names are tried when resolving a frame.

        Names the reader chose (a default scope, a channel scope, a typed name) lead, then
        names only a repeater has mentioned — the ones this station most likely sends
        under are the ones its own frames will carry. Ties keep the order learned.
        """
        chosen = [r for r in self._state.values() if set(r.sources) - {"repeater"}]
        heard = [r for r in self._state.values() if not set(r.sources) - {"repeater"}]
        return chosen + heard

    def names(self) -> list[str]:
        """The known region names, in resolution order (see :meth:`regions`)."""
        return [r.name for r in self.regions()]

    def get(self, name: str) -> KnownRegion | None:
        """The record for one region name, or ``None`` if it is not known."""
        return self._state.get(normalize(name))

    def carriers(self, name: str) -> tuple[str, ...]:
        """The node ids of the repeaters heard to carry a region (empty when none)."""
        region = self.get(name)
        return region.repeaters if region else ()

    def carried_by(self, node: str) -> list[str]:
        """The regions a repeater was heard to carry, by its node id (12-hex prefix)."""
        node = node.lower()[:12]
        return [r.name for r in self.regions() if node in r.repeaters]

    def answer_of(self, node: str) -> CarriedAnswer | None:
        """The last whole answer a repeater gave (unscoped too, and when), or ``None``.

        ``None`` means it was never asked — or never answered — which the node page states
        as such, rather than reading an empty :meth:`carried_by` as "carries nothing".
        """
        self._state  # noqa: B018 - load on first access
        return self._answers.get(node.lower()[:12])

    # -- changing it -----------------------------------------------------------------

    def learn(self, name: str, source: str, *, repeater: str | None = None) -> str | None:
        """Record a region name and where it came from; persist only a real change.

        Args:
            name: The name, bare or ``#``-prefixed. The wildcard and names the firmware
                would refuse are ignored (a repeater listing ``*`` teaches no region).
            source: One of :data:`SOURCES`.
            repeater: For ``source="repeater"``, the node id of the repeater that carries it.

        Returns:
            The bare name recorded, or ``None`` when nothing was learnable.
        """
        if source not in SOURCES:
            raise ValueError(f"unknown region source {source!r}")
        try:
            bare = validate(name)
        except RegionNameError:
            return None
        current = self._state.get(bare) or KnownRegion(name=bare)
        sources = current.sources if source in current.sources else current.sources + (source,)
        repeaters = current.repeaters
        if repeater:
            node = repeater.lower()[:12]
            if node not in repeaters:
                repeaters = repeaters + (node,)
        updated = replace(current, sources=sources, repeaters=repeaters)
        if self._state.get(bare) != updated:
            self._state[bare] = updated
            self._changed()
        return bare

    def learn_carried(self, repeater: str, names: Iterable[str]) -> list[str]:
        """Record what one repeater says it carries, replacing what it said before.

        A repeater's list is its whole answer, so a region it no longer names loses that
        repeater (and, if nothing else taught it, the region itself). Whether it listed the
        wildcard — relays unscoped floods — and when it answered are kept beside the
        regions (:meth:`answer_of`).

        Args:
            repeater: The repeater's node id (12-hex prefix or full key).
            names: The regions it listed (the wildcard is recorded as the unscoped flag).

        Returns:
            The bare region names learned, in the order given.
        """
        node = repeater.lower()[:12]
        given = [normalize(x) for x in names]
        self._state  # noqa: B018 - load on first access
        self._answers[node] = CarriedAnswer(node=node, unscoped=WILDCARD in given)
        self._changed()
        wanted = [n for n in given if n and n != WILDCARD]
        learned: list[str] = []
        for name in wanted:
            bare = self.learn(name, "repeater", repeater=node)
            if bare:
                learned.append(bare)
        for region in list(self._state.values()):
            if node in region.repeaters and region.name not in learned:
                repeaters = tuple(r for r in region.repeaters if r != node)
                sources = region.sources
                if not repeaters:
                    sources = tuple(s for s in sources if s != "repeater")
                if not sources:
                    del self._state[region.name]
                else:
                    self._state[region.name] = replace(region, sources=sources, repeaters=repeaters)
                self._changed()
        return learned

    def forget(self, name: str) -> None:
        """Drop a region name entirely, and any channel scope that used it."""
        bare = normalize(name)
        if bare not in self._state and bare not in self._channels.values():
            return
        self._state.pop(bare, None)
        self._channels = {k: v for k, v in self._channels.items() if v != bare}
        self._changed()

    # -- channel scopes ----------------------------------------------------------------

    def channel_scope(self, channel_id: str | None) -> str | None:
        """The region a channel's messages are sent under, or ``None`` for the default."""
        self._state  # noqa: B018 - load on first access
        return self._channels.get(channel_id) if channel_id else None

    def channel_scopes(self) -> dict[str, str]:
        """Every channel scope, channel identity -> region (a copy)."""
        self._state  # noqa: B018 - load on first access
        return dict(self._channels)

    def set_channel_scope(self, channel_id: str, name: str | None) -> None:
        """Give a channel a send scope, or clear it back to the device's default.

        Args:
            channel_id: The channel's intrinsic identity.
            name: The region, or ``None``/empty to clear.

        Raises:
            RegionNameError: If ``name`` is a name the firmware would refuse.
        """
        self._state  # noqa: B018 - load on first access
        if not name or not normalize(name):
            if self._channels.pop(channel_id, None) is not None:
                self._changed()
            return
        bare = validate(name)
        self.learn(bare, "channel")
        if self._channels.get(channel_id) != bare:
            self._channels[channel_id] = bare
            self._changed()

    # -- resolution ------------------------------------------------------------------

    def scope_of(self, raw: dict | None) -> Scope | None:
        """The scope of a received frame against every known name (memoized).

        Args:
            raw: The frame's raw payload (live, or restored from history).

        Returns:
            As :func:`~meshterm.core.regions.frame_scope`: ``None`` where the frame has no
            scope to state (direct, or a route type never kept).
        """
        if not isinstance(raw, dict):
            return None
        route = str(raw.get("route_typename") or "").upper()
        if route != "TC_FLOOD":
            return frame_scope(raw, ())
        body = raw_scope_body(raw)
        scope = frame_scope(raw, ())  # parses the code; resolution happens below
        if scope is None or scope.code is None or body is None:
            return scope
        memo_key = (body, int(scope.code, 16))
        if memo_key not in self._memo:
            if len(self._memo) >= _MEMO_CAP:
                self._memo.clear()
            self._memo[memo_key] = (frame_scope(raw, self.names()) or scope).region
        region = self._memo[memo_key]
        return Scope("scoped", region, scope.code) if region else scope

    # -- persistence -----------------------------------------------------------------

    def _changed(self) -> None:
        """Bump the revision, drop the memo, and persist."""
        self.revision += 1
        self._memo.clear()
        self._save()

    def _save(self) -> None:
        """Persist the whole store atomically (a crash mid-write keeps the old file)."""
        data = {
            "regions": [
                {
                    "name": r.name,
                    "sources": list(r.sources),
                    "repeaters": list(r.repeaters),
                    "learned_at": r.learned_at.astimezone(timezone.utc).isoformat(),
                }
                for r in self._state.values()
            ],
            "channels": dict(sorted(self._channels.items())),
            "answers": [
                {
                    "node": a.node,
                    "unscoped": a.unscoped,
                    "answered_at": a.answered_at.astimezone(timezone.utc).isoformat(),
                }
                for a in self._answers.values()
            ],
        }
        write_atomically(self._path, json.dumps(data, indent=2, ensure_ascii=False))


def _answer_from_json(entry: object) -> CarriedAnswer | None:
    """Parse one stored repeater answer, or ``None`` if it is malformed."""
    if not isinstance(entry, dict) or not entry.get("node"):
        return None
    try:
        answered_at = datetime.fromisoformat(str(entry["answered_at"]))
    except (KeyError, ValueError):
        return None
    if answered_at.tzinfo is None:
        answered_at = answered_at.replace(tzinfo=timezone.utc)
    return CarriedAnswer(
        node=str(entry["node"]).lower()[:12],
        unscoped=bool(entry.get("unscoped")),
        answered_at=answered_at,
    )


def _region_from_json(entry: object) -> KnownRegion | None:
    """Parse one stored region, or ``None`` if it is malformed."""
    if not isinstance(entry, dict):
        return None
    try:
        name = validate(str(entry["name"]))
    except (KeyError, RegionNameError):
        return None
    sources = tuple(s for s in entry.get("sources") or () if s in SOURCES)
    repeaters = tuple(str(r).lower()[:12] for r in entry.get("repeaters") or () if r)
    try:
        learned_at = datetime.fromisoformat(str(entry["learned_at"]))
    except (KeyError, ValueError):
        learned_at = datetime.now(timezone.utc)
    if learned_at.tzinfo is None:
        learned_at = learned_at.replace(tzinfo=timezone.utc)
    return KnownRegion(name=name, sources=sources, repeaters=repeaters, learned_at=learned_at)
