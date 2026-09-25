# SPDX-License-Identifier: Apache-2.0
"""The session's device-state cache: read the radio once, reuse it everywhere.

Opening a screen used to re-read the same stable facts from the companion every time —
the contacts table, the node's own self-info, the path-hash routing width, the configured
channel slots, the channel-slot capacity. Over Bluetooth each of those is a full
request→reply round-trip, and the channel-slot reads dominate: ``get_contacts`` on a busy
node (hundreds of contacts), the slot probe
(:func:`~meshterm.core.channel_probe.read_channel_slots`) and the capacity probe
(:meth:`~meshterm.core.connection.Device.channel_capacity`) each walk
the slot table one index at a time and are measured in *seconds* on firmware that never
rejects an out-of-range index. Firing them on every navigation is what made moving between
screens feel like it stalled.

None of that data actually changes mid-navigation:

* **self-info** and **path-hash mode** change only when the config editor writes them;
* **channel slots** change only when the channel editor saves one;
* **channel-slot capacity** is a fixed firmware build constant — it never changes at all
  within a connection, so it is simply held for the session and only dropped on a reconnect;
* **contacts** grow as the mesh advertises, but a minute-stale list is harmless — the app
  already resolves names from recorded history too.

So this service reads each fact once, holds it for the session, and hands screens the cached
copy instantly. The two rules that keep the cache honest:

* **Contacts** use *stale-while-revalidate*: a read past :data:`_CONTACTS_TTL_S` returns the
  cached list immediately and refreshes it in the background, so navigation is never blocked
  on the slow call, yet newly-heard contacts still appear within a TTL of the next screen.
* Everything else is held until an in-app write **invalidates** it (see
  :meth:`invalidate_self_info`, :meth:`invalidate_channels`, …); the writer is the only thing
  that can change it, so it is also the only thing that needs to drop the cache.

The cache is transport-agnostic — it sits above :meth:`~meshterm.context.AppContext.device`,
so serial sessions get the same win (smaller, since serial round-trips are faster) and the
``--mock`` simulator is simply always fast. It is cleared wholesale on a reconnect (see
:meth:`reset`), since a fresh link should re-read the truth.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..core.channel_probe import ChannelSlot, probe_channel_slots

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.models import Contact

#: How long a cached contacts list is served before a read triggers a background refresh
#: (seconds). Contacts only grow as the mesh advertises, and the list is a naming/addressing
#: convenience the app also fills from recorded history — so a slightly stale copy costs
#: nothing, while re-reading the (slow) table on every screen open cost seconds. A read past
#: this age still returns instantly from cache; the refresh happens behind it.
_CONTACTS_TTL_S = 90.0


def _aware(when: datetime | None) -> datetime | None:
    """Read a stored timestamp as UTC, so two of them can be compared.

    Timestamps are stored as UTC (rendered local only at the very edge), but a row written
    by an older build may come back without a tzinfo — and comparing one of those against an
    aware one raises. Since the storage contract says UTC, a naive stamp simply *is* UTC.
    """
    if when is None or when.tzinfo is not None:
        return when
    return when.replace(tzinfo=timezone.utc)


class DeviceState:
    """Session-scoped cache of the stable facts screens read from the companion on open.

    Held on the :class:`~meshterm.context.AppContext` as ``ctx.devstate`` and created idle;
    each getter fetches from the device on first use (opening the connection via
    :meth:`AppContext.device` if needed) and serves the cached value thereafter. The getters
    mirror the contract of the underlying :class:`~meshterm.core.connection.Device` methods —
    :meth:`contacts` and :meth:`self_info` raise on a failed first fetch (their callers either
    handle it or let it surface), :meth:`path_hash_mode` likewise so its optional-read callers
    can fall back — so a call site can swap ``device.get_x()`` for ``ctx.devstate.x()`` without
    changing how it handles failure.
    """

    def __init__(self, ctx: AppContext) -> None:
        """Initialize an empty cache bound to an application context.

        Args:
            ctx: The shared application context, used to reach the device and to log. No
                device is opened until a getter is first called.
        """
        self._ctx = ctx
        self._contacts: list[Contact] | None = None
        self._contacts_at: float = 0.0
        self._self_info: dict | None = None
        self._path_hash_mode: int | None = None
        self._channels: list[ChannelSlot] | None = None
        self._channels_epoch = 0
        self._channel_capacity: int | None = None
        #: The companion's default flood scope — ``""`` for none — once read (see
        #: :meth:`default_scope`); ``None`` while unread.
        self._default_scope: str | None = None
        # One lock per slow fetch so overlapping first-access callers (two screens opened in
        # quick succession) collapse onto a single round-trip instead of each firing their own.
        self._contacts_lock = asyncio.Lock()
        self._self_info_lock = asyncio.Lock()
        self._path_hash_lock = asyncio.Lock()
        self._channels_lock = asyncio.Lock()
        self._capacity_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    # -- contacts (stale-while-revalidate) --------------------------------------

    async def contacts(self, *, force: bool = False) -> list[Contact]:
        """Return the device's contacts, cached for the session and refreshed lazily.

        The first call reads the table from the radio (a slow round-trip on a busy node) and
        holds it. Later calls return the cached list *immediately*; once it is older than
        :data:`_CONTACTS_TTL_S` a read also kicks off a background refresh, so the list stays
        current without ever blocking navigation on the slow call.

        Args:
            force: Re-read from the device now (blocking), bypassing the cache — for the rare
                caller that needs the freshest possible list.

        Returns:
            The device's contacts. Raises like
            :meth:`~meshterm.core.connection.Device.get_contacts` if the *first* fetch fails
            (a background refresh failure is swallowed, leaving the last good list in place).
        """
        if force:
            return await self._fetch_contacts(force=True)
        if self._contacts is None:
            return await self._fetch_contacts()
        if time.monotonic() - self._contacts_at > _CONTACTS_TTL_S:
            self._spawn(self._refresh_contacts_quietly())
        return self._contacts

    def peek_contacts(self) -> list[Contact] | None:
        """The contacts already in hand — possibly stale — or ``None``, never a radio read.

        For a surface that only wants them as *context* and must not wait for them: the
        location picker's surrounding nodes. A companion can refuse the contacts read for a
        stretch (``ERR_CODE_BAD_STATE``), and :meth:`contacts` then blocks through every retry
        of it — twenty-odd seconds — before giving up.
        """
        return self._contacts

    async def _fetch_contacts(self, *, force: bool = False) -> list[Contact]:
        """Read the contacts table from the device and cache it (blocking, deduplicated).

        Args:
            force: Fetch unconditionally. When ``False``, a caller that refreshed the cache
                while this one waited for the lock reuses that fresh result rather than issuing
                a second identical round-trip.
        """
        async with self._contacts_lock:
            if (
                not force
                and self._contacts is not None
                and (time.monotonic() - self._contacts_at <= _CONTACTS_TTL_S)
            ):
                return self._contacts
            device = await self._ctx.device()
            merged = await self._remember_and_merge(await device.get_contacts())
            self._contacts = self._merge_heard(merged)
            self._contacts_at = time.monotonic()
            return self._contacts

    async def _remember_and_merge(self, fetched: list[Contact]) -> list[Contact]:
        """Record the contacts just read, then union in any the device has since forgotten.

        A firmware-less radio bridge loses its contact table on restart, so MeshTerm remembers
        every contact it reads (keyed by the device's own public key) and merges the missing ones
        back into the list — leaving a firmware radio, which reports its whole table, untouched
        (see :mod:`meshterm.core.contact_store`). Best-effort: a store or self-info hiccup just
        returns the live list unchanged, never blocking the read.
        """
        store = getattr(self._ctx, "contact_store", None)
        if store is None:
            return fetched
        from ..core.contact_store import merge_contacts

        try:
            pubkey = str((await self.self_info()).get("public_key") or "")
        except Exception as exc:  # noqa: BLE001 - never block a contacts read on the store
            self._ctx.log.debug("devstate: contact remember/merge skipped: %s", exc)
            return fetched
        if not pubkey:
            return fetched
        store.remember_all(pubkey, fetched)
        return merge_contacts(store, pubkey, fetched)

    def _merge_heard(self, contacts: list[Contact]) -> list[Contact]:
        """Give each contact the *later* of the device's advert time and our own receptions.

        A contact's ``last_seen`` arrives as the firmware's ``last_advert``, which the
        advertising node stamped with its own clock — hearsay
        (:func:`~meshterm.core.models.advert_time` can only refuse a *future* stamp; a node
        whose clock runs days behind reports a plausible-looking time that never catches
        up). Our own history holds first-hand evidence instead, stamped when we received
        something: overheard adverts and telemetry
        (:meth:`~meshterm.persistence.repository.Repository.last_heard_by_node`) and direct
        messages the node sent us
        (:meth:`~meshterm.persistence.repository.Repository.last_message_by_peer`) — which
        count, because in this app's lexicon "heard" means received from, and a message is
        received from its sender.

        So the three are merged by taking the latest, here — once, at the one point every
        screen fetches contacts through, so heard lanes, recency sorts, silence alerts and
        the archive ladder all see the same honest value. Taking the *latest* rather than
        preferring either side means a device time still stands whenever the firmware caught
        an advert we did not record (monitoring off, app not running); we only override it
        with proof of a later reception. A contact we have truly never heard stays ``None``
        and reads ``never``. Best-effort: a history read failure just returns the list
        unmerged, never blocking the fetch.
        """
        try:
            heard = self._ctx.repo.last_heard_by_node()
            messaged = self._ctx.repo.last_message_by_peer()
        except Exception as exc:  # noqa: BLE001 - never block a contacts read on history
            self._ctx.log.debug("devstate: last-heard merge skipped: %s", exc)
            return contacts
        merged: list[Contact] = []
        for contact in contacts:
            ident = (contact.public_key or contact.key_prefix or "").lower()
            ident = ident.removeprefix("0x")
            stamps = [contact.last_seen, heard.get(ident[:12])]
            # A message's peer is whatever width the wire addressed, which need not match
            # the contact table's — so either may be the shorter, exactly as the live chat
            # matches an inbound sender to its thread.
            stamps += [
                when
                for peer, when in messaged.items()
                if ident and peer and (ident.startswith(peer) or peer.startswith(ident))
            ]
            known = [a for a in (_aware(s) for s in stamps) if a is not None]
            latest = max(known) if known else None
            if latest is not None and latest != contact.last_seen:
                contact = replace(contact, last_seen=latest)
            merged.append(contact)
        return merged

    async def _refresh_contacts_quietly(self) -> None:
        """Background contacts refresh: update the cache, swallow a failure (keep the old list)."""
        try:
            await self._fetch_contacts()
        except Exception as exc:  # noqa: BLE001 - a stale list is fine; never surface here
            self._ctx.log.debug("devstate: background contacts refresh failed: %s", exc)

    # -- self-info (held until an in-app write invalidates it) ------------------

    async def self_info(self) -> dict:
        """Return the node's own self-info, cached until the config editor invalidates it.

        Returns:
            The self-info payload (identity, radio tuning, coordinates, tx power). Raises like
            :meth:`~meshterm.core.connection.Device.get_self_info` if the fetch fails; the
            failure is not cached, so the next call retries.
        """
        if self._self_info is None:
            async with self._self_info_lock:
                if self._self_info is None:
                    device = await self._ctx.device()
                    self._self_info = dict(await device.get_self_info())
        return self._self_info

    def peek_self_info(self) -> dict | None:
        """The self-info already in hand, or ``None`` — never a radio read.

        The :meth:`peek_contacts` of our own node: context for a surface that must not wait.
        """
        return self._self_info

    async def path_hash_mode(self) -> int:
        """Return the device's path-hash routing mode, cached for the session.

        Returns:
            The path-hash mode integer. Raises like
            :meth:`~meshterm.core.connection.Device.get_path_hash_mode` if the read fails, so
            the optional-read callers that wrap this in ``try`` fall back exactly as before.
        """
        if self._path_hash_mode is None:
            # Locked like the other first fetches: :meth:`prewarm` reads this in the background
            # at connect, so a screen opening in that same beat would otherwise issue a second,
            # identical round-trip alongside it rather than awaiting the one in flight.
            async with self._path_hash_lock:
                if self._path_hash_mode is None:
                    device = await self._ctx.device()
                    self._path_hash_mode = int(await device.get_path_hash_mode())
        return self._path_hash_mode

    async def routing_prefix_bytes(self) -> int:
        """The device's path-hash width in bytes, or 0 when unknowable.

        What a surface that *highlights* hashes actually wants: how many leading bytes of a
        key the mesh routes on, which is what :func:`~meshterm.ui.widgets.highlighted_hash`
        lights. Best-effort, because every caller reads stored history and must work with no
        radio at all — an unreachable device (or firmware that doesn't report the mode) just
        leaves every hash un-highlighted rather than failing the screen.

        Returns:
            The hash width in bytes (1–4), or 0 when the mode could not be read.
        """
        try:
            if not (self._ctx.is_connected or self._ctx.settings.connect_on_start):
                return 0
            mode = await self.path_hash_mode()
        except Exception:  # noqa: BLE001 - optional read; absence just skips highlighting
            return 0
        return (mode + 1) if isinstance(mode, int) and 0 <= mode <= 3 else 0

    # -- the default flood scope (held until a config write invalidates it) ------

    async def default_scope(self) -> str:
        """The companion's persisted default flood scope, ``""`` for none, read once.

        What a plain channel send goes out under, so it is what a channel with no scope of
        its own records its messages as sent under. One round trip, then held until the
        config editor writes a setting (:meth:`invalidate_config`) or the link is rebuilt.

        Returns:
            The bare region name, or ``""`` when no default scope is set.

        Raises:
            Exception: Like :meth:`~meshterm.core.connection.Device.get_default_flood_scope`
                — firmware older than 1.15 has no default scope to read. Not cached, so the
                next call asks again.
        """
        if self._default_scope is None:
            device = await self._ctx.device()
            self._default_scope = str(await device.get_default_flood_scope() or "")
        return self._default_scope

    def note_default_scope(self, name: str | None) -> None:
        """Record a default scope just read or written elsewhere (Device config).

        Args:
            name: The bare name, ``""`` for none, or ``None`` to forget it and re-read.
        """
        self._default_scope = None if name is None else str(name)

    # -- channel slots (held until the channel editor invalidates them) --------

    async def channel_slots(self) -> list[ChannelSlot]:
        """Return the configured channel slots, cached until a channel edit invalidates them.

        The underlying probe (:func:`~meshterm.core.channel_probe.read_channel_slots`) walks
        every slot index on the firmware, which is one of the slowest reads on a screen open — so it
        is well worth reading once. It is best-effort itself (an unsupported firmware yields an
        empty list rather than raising), but a probe that ended early because a *read failed*
        is answered and then dropped rather than cached — see
        :func:`~meshterm.core.channel_probe.probe_channel_slots` for why a short list is not a
        layout.

        Returns:
            One :class:`~meshterm.core.channel_probe.ChannelSlot` per slot, in index order.
        """
        if self._channels is None:
            async with self._channels_lock:
                if self._channels is None:
                    device = await self._ctx.device()
                    slots, complete = await probe_channel_slots(device)
                    if complete:
                        self._channels = slots
                    # An incomplete probe is answered but not kept: one timed-out read used
                    # to poison the whole session, and the shortfall does not announce
                    # itself — an empty result reads exactly like a device with no channels.
                    return slots
        return self._channels

    async def channel_capacity(self) -> int:
        """Return the device's channel-slot capacity, discovered once and held for the session.

        The capacity is a fixed firmware build constant, so it is probed once and reused. The
        probe (:meth:`~meshterm.core.connection.Device.channel_capacity`) reads slots upward
        until the firmware rejects an index; on firmware that *never* rejects one it walks up
        to :data:`~meshterm.core.channels.CHANNEL_SLOT_PROBE_CAP` slots — one of the slowest
        reads on a screen open, and paid on *every* open of the channel manager before this
        cache. Unlike the configured-slot list it cannot be safely bounded by a run of empty
        slots (it must reach a larger firmware's real ceiling), so it is bounded by caching:
        held until :meth:`reset` (a reconnect re-reads it) rather than invalidated on a channel
        edit, since editing a channel never changes how many slots the hardware has.

        Returns:
            The number of addressable channel slots the firmware exposes.
        """
        if self._channel_capacity is None:
            async with self._capacity_lock:
                if self._channel_capacity is None:
                    device = await self._ctx.device()
                    self._channel_capacity = await device.channel_capacity()
        return self._channel_capacity

    # -- prewarm (fill the slow caches in the background, off the navigation path) --

    def prewarm(self) -> None:
        """Warm *every* cached fact in the background after connect, cheapest reads first.

        Called once the session's link is up (see :func:`meshterm.ui.menu._resume_monitor`) so
        the first screen that reads them — Contacts, Chat, Trace, the Dashboard, the channel
        manager — is served from cache instantly, rather than paying the round-trips in the
        navigation path where the user is waiting on the screen to open. It folds the
        unavoidable first reads into one quiet wait behind the menu instead of surfacing them
        on the first open.

        **Order is the whole design.** The reads share one link and run in sequence, so a fact
        warmed late is a fact the first open still queues behind — and the two slot probes at
        the end are the slowest reads in the app (each walks the slot table one index at a
        time, measured in seconds). So the warm runs cheapest-first: the two single round-trips
        every list screen needs (:meth:`self_info`, :meth:`path_hash_mode`), then the contacts
        table, then the probes only the channel manager waits on. Opening Contacts a second
        after connect then finds its three facts already in hand instead of joining the queue
        behind a slot walk. Warming ``path_hash_mode`` is what closed the last such gap: it is
        read on the Contacts and Trace open paths to size the key-hash highlight, and nothing
        else warmed it.

        The work runs as a tracked background task: **sequential** (never gathered — concurrent
        reads collide on the BLE UART; see the module note), best-effort (a failure just leaves
        the cache cold for a normal lazy fetch later), and cancelled on :meth:`reset` /
        :meth:`aclose`. If a getter is reached before this finishes, it awaits the *same*
        in-flight fetch — the per-fetch locks dedupe — so prewarming never doubles a read.
        """
        self._spawn(self._prewarm())

    async def _prewarm(self) -> None:
        """Fetch every cached fact one after another, swallowing failures (best-effort warm)."""
        for label, fetch in (
            ("self-info", self.self_info),
            ("path-hash mode", self.path_hash_mode),
            ("contacts", self.contacts),
            ("channel slots", self.channel_slots),
            ("channel capacity", self.channel_capacity),
        ):
            try:
                await fetch()
            except Exception as exc:  # noqa: BLE001 - a warm miss just falls back to a lazy fetch
                self._ctx.log.debug("devstate: prewarm of %s failed: %s", label, exc)

    # -- invalidation (called by the code that writes device state) ------------

    def invalidate_contacts(self) -> None:
        """Drop the cached contacts so the next read re-fetches (e.g. a contact was removed)."""
        self._contacts = None
        self._contacts_at = 0.0

    def invalidate_self_info(self) -> None:
        """Drop the cached self-info — call after writing name/coords/radio/tx power/tuning."""
        self._self_info = None

    def invalidate_path_hash_mode(self) -> None:
        """Drop the cached path-hash mode — call after writing it in the config editor."""
        self._path_hash_mode = None

    @property
    def channels_epoch(self) -> int:
        """How many times the channel layout has been dropped this session.

        A caller that wants to know whether *anything* rewrote a slot while it was busy
        compares this across the gap rather than trusting a return value: a write that
        landed and then raised on its way back moved the device just as much as one that
        returned cleanly, and the count it never got to increment cannot say so.
        """
        return self._channels_epoch

    def invalidate_channels(self) -> None:
        """Drop the cached channel slots — call after the channel editor saves or clears one."""
        self._channels = None
        self._channels_epoch += 1

    def invalidate_config(self) -> None:
        """Drop everything the config editor can change in one call (self-info + routing mode).

        A convenience for the config editor's apply path, which may have written any of the
        self-info fields and/or the path-hash mode; dropping both is cheaper than tracking
        exactly which settings changed.
        """
        self.invalidate_self_info()
        self.invalidate_path_hash_mode()
        self._default_scope = None

    def reset(self) -> None:
        """Clear the whole cache and cancel any in-flight background refresh.

        Called on a reconnect: a fresh link should re-read the truth rather than trust facts
        cached against the connection that just dropped.
        """
        self.invalidate_contacts()
        self.invalidate_self_info()
        self.invalidate_path_hash_mode()
        self.invalidate_channels()
        self._default_scope = None
        # Capacity is a hardware constant (not touched by channel edits, so it has no per-write
        # invalidator), but a reconnect may be to a different device — drop it too.
        self._channel_capacity = None
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    async def aclose(self) -> None:
        """Cancel any background refresh at session end (an alias for :meth:`reset`)."""
        self.reset()

    def _spawn(self, coro) -> None:  # type: ignore[no-untyped-def]
        """Run a background refresh as a tracked task so it can be cancelled on reset."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
