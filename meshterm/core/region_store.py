# SPDX-License-Identifier: Apache-2.0
"""The regions that MeshTerm knows by name, and the scope under which each channel sends.

A scoped flood has no region name. It has only a transport code that comes from a region
name (refer to :mod:`~meshterm.core.regions`). Thus, to find the region into which a
packet was flooded, MeshTerm must try the names that it knows. This store is that list.
MeshTerm learns a name from each place where it finds one:

* ``default``: the default flood scope of the companion, read or set on Device config.
* ``channel``: a scope that is given to a channel, so that its messages stay in its
  region.
* ``repeater``: the answer of a repeater to the regions request, or its ``region``
  listing. The store also keeps which repeaters carry the region. The node page lists
  them. Also, when nothing relayed a scoped send, MeshTerm can tell whether a node in
  radio range was ever heard to carry its region. Next to the regions, the store keeps
  the last full answer of each repeater: whether it also relays unscoped floods, and
  when it said so (:class:`CarriedAnswer`).
* ``typed``: a name that the user typed.

The store also keeps the **channel scopes**. A channel scope is the region under which
MeshTerm sends the messages of a channel. The firmware has no scope for each channel
(``// TODO: have per-channel send_scope``). Thus a client that wants one sets the session
scope of the companion immediately before each channel send. Each official app does
this. The key of a channel scope is the intrinsic identity of the channel
(:func:`~meshterm.core.channels.channel_identity`), the same as for a mute. Thus the
scope follows the channel to a different slot, and two devices that share the channel
also share its scope.

The store also caches the resolution (:meth:`RegionStore.scope_of`). A packet list paints
often, and the resolution of a scoped packet costs one HMAC for each known name. Thus the
store keeps an answer for each ``(body, code)`` until the set of names changes.

This store is global machine state in a small JSON file (``<config_dir>/regions.json``),
the same as the other state of the user (mutes, watched nodes, admin passwords). The
store reads the file one time, and writes it atomically at each real change. It writes
from its typed records, never from the document that it read. Thus a field that no
record knows is removed at the next save.
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

#: Where MeshTerm learned a region name. This is the closed set from which
#: :attr:`KnownRegion.sources` takes its values.
SOURCES = ("default", "channel", "repeater", "typed")

#: The maximum number of cached resolutions. At this number, the store clears the full
#: cache. (A long capture finds many bodies. A paint finds the same few hundred bodies
#: again and again.)
_MEMO_CAP = 4096


@dataclass(frozen=True)
class KnownRegion:
    """One region that MeshTerm knows by name.

    Attributes:
        name: The bare region name (no ``#``).
        sources: Where MeshTerm learned it, as members of :data:`SOURCES`, in the order
            in which they were first learned.
        repeaters: The 12-hex node ids of the repeaters that were heard to carry it.
        learned_at: When MeshTerm first learned it (UTC).
    """

    name: str
    sources: tuple[str, ...] = ()
    repeaters: tuple[str, ...] = ()
    learned_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    #: The fields that this record held before. It must never hold them again with a
    #: different meaning (refer to :data:`meshterm.core.preferences.RETIRED` for the
    #: reason that a name is never used again).
    RETIRED: ClassVar[frozenset[str]] = frozenset()


@dataclass(frozen=True)
class CarriedAnswer:
    """The last full answer of one repeater about what it carries.

    The regions themselves are on :class:`KnownRegion` (``repeaters``), where the
    resolution uses them. This record keeps what does not belong to one region: whether
    the repeater also relays unscoped floods (the ``*`` at the start of a region list,
    which names no region), and when it said so. If a repeater has no record here,
    MeshTerm never asked it. That is different from a repeater that answered with no
    regions.

    Attributes:
        node: The 12-hex node id of the repeater.
        unscoped: Whether its answer included the wildcard.
        answered_at: When it answered (UTC).
    """

    node: str
    unscoped: bool
    answered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    #: The fields that this record held before. It must never hold them again with a
    #: different meaning.
    RETIRED: ClassVar[frozenset[str]] = frozenset()


class RegionStore:
    """Reads and writes the known regions and the channel scopes, in memory first.

    Use :meth:`names`/:meth:`regions` (what is known), :meth:`learn` and :meth:`forget`
    (to change it), :meth:`channel_scope`/:meth:`set_channel_scope` (the send scope of a
    channel), :meth:`carriers` (which repeaters carry a region), and :meth:`scope_of`
    (the scope of a received packet). :attr:`revision` increases at each change, for a
    screen that caches on it.
    """

    def __init__(self, path: Path) -> None:
        """Open the store on the location of a JSON file.

        Args:
            path: Path to the JSON state file (made only at the first change).
        """
        self._path = path
        self._regions: dict[str, KnownRegion] | None = None
        self._channels: dict[str, str] = {}
        self._answers: dict[str, CarriedAnswer] = {}
        self._memo: dict[tuple[bytes, int], str | None] = {}
        self.revision = 0

    # -- read from disk ----------------------------------------------------------------

    @property
    def _state(self) -> dict[str, KnownRegion]:
        """The map from name to record, read from disk at the first access."""
        if self._regions is None:
            self._regions, self._channels, self._answers = self._load()
        return self._regions

    def _load(
        self,
    ) -> tuple[dict[str, KnownRegion], dict[str, str], dict[str, CarriedAnswer]]:
        """Parse the file, or start empty when the file is missing or corrupt."""
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
        """All the known regions, in the order in which the names are tried for a packet.

        The names that the user chose (a default scope, a channel scope, a typed name)
        come first. Then come the names that only a repeater gave. The reason: the names
        under which our node most probably sends are the names that its own packets will
        carry. Names in the same group keep the order in which they were learned.
        """
        chosen = [r for r in self._state.values() if set(r.sources) - {"repeater"}]
        heard = [r for r in self._state.values() if not set(r.sources) - {"repeater"}]
        return chosen + heard

    def names(self) -> list[str]:
        """The known region names, in resolution order (refer to :meth:`regions`)."""
        return [r.name for r in self.regions()]

    def get(self, name: str) -> KnownRegion | None:
        """The record for one region name, or ``None`` if it is not known."""
        return self._state.get(normalize(name))

    def carriers(self, name: str) -> tuple[str, ...]:
        """The node ids of the repeaters that were heard to carry a region (or empty)."""
        region = self.get(name)
        return region.repeaters if region else ()

    def carried_by(self, node: str) -> list[str]:
        """The regions that a repeater was heard to carry, by its node id (12-hex key prefix)."""
        node = node.lower()[:12]
        return [r.name for r in self.regions() if node in r.repeaters]

    def answer_of(self, node: str) -> CarriedAnswer | None:
        """The last full answer of a repeater (its unscoped flag and its time), or ``None``.

        ``None`` means that MeshTerm never asked the repeater, or that it never answered.
        The node page shows this case as it is. It does not read an empty
        :meth:`carried_by` as "carries nothing".
        """
        self._state  # noqa: B018 - load on first access
        return self._answers.get(node.lower()[:12])

    # -- change it -------------------------------------------------------------------

    def learn(self, name: str, source: str, *, repeater: str | None = None) -> str | None:
        """Store a region name and its source. Write the file only for a real change.

        Args:
            name: The name, bare or with a ``#`` prefix. The function ignores the wildcard
                and the names that the firmware refuses (a repeater that lists ``*``
                teaches no region).
            source: One of :data:`SOURCES`.
            repeater: For ``source="repeater"``, the node id of the repeater that carries
                the region.

        Returns:
            The bare name that was stored, or ``None`` when there was nothing to learn.
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
        """Store what one repeater says that it carries, and replace what it said before.

        The list of a repeater is its full answer. Thus a region that the repeater no
        longer names loses that repeater. If nothing else taught the region, the store
        also removes the region itself. Next to the regions, the store keeps whether the
        repeater listed the wildcard (that is, it relays unscoped floods), and when it
        answered (:meth:`answer_of`).

        Args:
            repeater: The node id of the repeater (a 12-hex key prefix or the full key).
            names: The regions that it listed (the store keeps the wildcard as the
                unscoped flag).

        Returns:
            The bare region names that were learned, in the given order.
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
        """Remove a region name completely, and each channel scope that used it."""
        bare = normalize(name)
        if bare not in self._state and bare not in self._channels.values():
            return
        self._state.pop(bare, None)
        self._channels = {k: v for k, v in self._channels.items() if v != bare}
        self._changed()

    # -- channel scopes ----------------------------------------------------------------

    def channel_scope(self, channel_id: str | None) -> str | None:
        """The send scope of a channel, or ``None`` for the default scope.

        The send scope is the region under which MeshTerm sends the messages of the channel.
        """
        self._state  # noqa: B018 - load on first access
        return self._channels.get(channel_id) if channel_id else None

    def channel_scopes(self) -> dict[str, str]:
        """All the channel scopes, as channel identity -> region (a copy)."""
        self._state  # noqa: B018 - load on first access
        return dict(self._channels)

    def set_channel_scope(self, channel_id: str, name: str | None) -> None:
        """Give a channel a send scope, or clear it to use the default of the device again.

        Args:
            channel_id: The intrinsic identity of the channel.
            name: The region, or ``None``/empty to clear the scope.

        Raises:
            RegionNameError: If ``name`` is a name that the firmware refuses.
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
        """The scope of a received packet, resolved against each known name (cached).

        Args:
            raw: The raw payload of the packet (live, or restored from the history).

        Returns:
            The same as :func:`~meshterm.core.regions.frame_scope`: ``None`` where the
            packet has no scope to show (a direct packet, or a route type that was never
            kept).
        """
        if not isinstance(raw, dict):
            return None
        route = str(raw.get("route_typename") or "").upper()
        if route != "TC_FLOOD":
            return frame_scope(raw, ())
        body = raw_scope_body(raw)
        scope = frame_scope(raw, ())  # parses the code only. The resolution comes below.
        if scope is None or scope.code is None or body is None:
            return scope
        memo_key = (body, int(scope.code, 16))
        if memo_key not in self._memo:
            if len(self._memo) >= _MEMO_CAP:
                self._memo.clear()
            self._memo[memo_key] = (frame_scope(raw, self.names()) or scope).region
        region = self._memo[memo_key]
        return Scope("scoped", region, scope.code) if region else scope

    # -- write to disk ---------------------------------------------------------------

    def _changed(self) -> None:
        """Increase the revision, clear the cache, and write the file."""
        self.revision += 1
        self._memo.clear()
        self._save()

    def _save(self) -> None:
        """Write the full store atomically.

        If a crash occurs during the write, the old file stays.
        """
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
    """Parse one stored answer of a repeater, or return ``None`` if it is malformed."""
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
    """Parse one stored region, or return ``None`` if it is malformed."""
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
