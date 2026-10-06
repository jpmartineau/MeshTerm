# SPDX-License-Identifier: Apache-2.0
"""The device-state cache of the session: read the device one time, use the facts everywhere.

Before this cache, each screen read the same stable facts from the companion again when it
opened: the contacts table, the self-info of our node, the path-hash routing width, the
configured channel slots, and the channel-slot capacity. Over Bluetooth, each of these
reads is a full request→reply round trip, and the channel-slot reads are the slowest.
``get_contacts`` on a busy node (hundreds of contacts), the slot probe
(:func:`~meshterm.core.channel_probe.read_channel_slots`), and the capacity probe
(:meth:`~meshterm.core.connection.Device.channel_capacity`) each read the slot table one
index at a time. On firmware that never rejects an index that is out of range, each of
these reads takes seconds. MeshTerm did these reads at each navigation, and that is why
the move between screens seemed to stop for a time.

None of these facts changes while the user moves between screens:

* **self-info** changes when the config editor writes it. Otherwise, only its position
  changes: a node with a GPS that operates changes its own position when it moves.
* **path-hash mode** changes only when the config editor writes it.
* **channel slots** change only when the channel editor saves a slot.
* **channel-slot capacity** is a constant of the firmware build. It never changes during a
  connection. Thus the service keeps it for the session and removes it only on a reconnect.
* **contacts** become more when nodes on the mesh send adverts. But a list that is one
  minute old does no harm, because the app also finds names from the stored history.

Thus this service reads each fact one time and keeps it for the session. It gives the
cached copy to the screens immediately. Two rules keep the cache correct:

* **Contacts** use *stale-while-revalidate*. When the cached list is older than
  :data:`_CONTACTS_TTL_S`, a read returns the cached list immediately and refreshes it in
  the background. Thus navigation never waits for the slow call. But a contact that was
  heard for the first time still shows on the next screen within one TTL.
  **Self-info** does the same after :data:`_SELF_INFO_TTL_S`. Thus a screen that opens
  after the node moved shows the current position of the node.
* The service keeps all the other facts until a write in the app **invalidates** them
  (refer to :meth:`invalidate_self_info`, :meth:`invalidate_channels`, and the other
  ``invalidate_*`` methods). Only the writer can change these facts. Thus only the writer
  must remove them from the cache.

The cache does not depend on the transport, because it is above
:meth:`~meshterm.context.AppContext.device`. Thus a serial session gets the same benefit.
The benefit is smaller, because serial round trips are faster. The ``--mock`` simulator is
always fast. A reconnect clears all of the cache (refer to :meth:`reset`), because a new
link must read the true values again.
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

#: The age (seconds) after which a read of the cached contacts list also starts a refresh
#: in the background. The list of contacts only becomes longer when nodes send adverts. The
#: app uses the list to name and address nodes, and it also gets names from the stored
#: history. Thus a copy that is a little old costs nothing. But a new read of the slow
#: table each time that a screen opened cost seconds. After this age, a read still returns
#: the cached list immediately, and the refresh occurs in the background.
_CONTACTS_TTL_S = 90.0

#: The age (seconds) after which a read of the self-info of our node also refreshes it in
#: the background, after the answer. Only the position changes when the app does not write
#: it: a GPS changes the position when the node moves. One small protocol frame each minute
#: keeps the position correct on each screen that opens after a move. Navigation does not
#: wait for it.
_SELF_INFO_TTL_S = 60.0


def _aware(when: datetime | None) -> datetime | None:
    """Read a stored timestamp as UTC, so that two timestamps can be compared.

    Timestamps are stored as UTC, and are rendered as local time only at the last step. But
    a row that an older build wrote can come back without a tzinfo. A comparison of such a
    timestamp with an aware timestamp raises an error. The storage contract says UTC, thus a
    naive timestamp is UTC.
    """
    if when is None or when.tzinfo is not None:
        return when
    return when.replace(tzinfo=timezone.utc)


class DeviceState:
    """A cache, for the session, of the stable facts that screens read from the companion.

    The :class:`~meshterm.context.AppContext` holds it as ``ctx.devstate``. It is empty when
    it is created. At its first use, each getter reads from the device (and opens the
    connection through :meth:`AppContext.device` if necessary). After that, the getter
    returns the cached value. The getters follow the contract of the related
    :class:`~meshterm.core.connection.Device` methods. :meth:`contacts` and
    :meth:`self_info` raise an error if the first read fails (their callers handle the error
    or let it go up). :meth:`path_hash_mode` also raises, so that its callers, for which the
    read is optional, can use a fallback. Thus a call site can replace ``device.get_x()``
    with ``ctx.devstate.x()``, and its failure handling does not change.
    """

    def __init__(self, ctx: AppContext) -> None:
        """Initialize an empty cache that is bound to an application context.

        Args:
            ctx: The shared application context. The cache uses it to get the device and to
                write to the log. The cache opens no device before the first call to a
                getter.
        """
        self._ctx = ctx
        self._contacts: list[Contact] | None = None
        self._contacts_at: float = 0.0
        self._self_info: dict | None = None
        self._self_info_at: float = 0.0
        self._path_hash_mode: int | None = None
        self._channels: list[ChannelSlot] | None = None
        self._channels_epoch = 0
        self._channel_capacity: int | None = None
        #: The default flood scope of the companion after the first read (refer to
        #: :meth:`default_scope`), or ``""`` for none. ``None`` before the first read.
        self._default_scope: str | None = None
        # One lock for each slow read. Thus callers that make their first access at the same
        # time (two screens that open quickly, one after the other) share one round trip,
        # instead of one round trip for each caller.
        self._contacts_lock = asyncio.Lock()
        self._self_info_lock = asyncio.Lock()
        self._path_hash_lock = asyncio.Lock()
        self._channels_lock = asyncio.Lock()
        self._capacity_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    # -- contacts (stale-while-revalidate) --------------------------------------

    async def contacts(self, *, force: bool = False) -> list[Contact]:
        """Return the contacts of the device, cached for the session and refreshed when old.

        The first call reads the table from the device (a slow round trip on a busy node) and
        keeps it. Later calls return the cached list immediately. When the list is older than
        :data:`_CONTACTS_TTL_S`, a read also starts a refresh in the background. Thus the list
        stays current, and navigation never waits for the slow call.

        Args:
            force: Read from the device again now, wait for the result, and do not use the
                cache. This is for the rare caller that must have the most recent list.

        Returns:
            The contacts of the device. If the first read fails, this method raises the same
            error as :meth:`~meshterm.core.connection.Device.get_contacts`. If a refresh in
            the background fails, the error is ignored, and the last good list stays.
        """
        if force:
            return await self._fetch_contacts(force=True)
        if self._contacts is None:
            return await self._fetch_contacts()
        if time.monotonic() - self._contacts_at > _CONTACTS_TTL_S:
            self._spawn(self._refresh_contacts_quietly())
        return self._contacts

    def peek_contacts(self) -> list[Contact] | None:
        """The contacts that the cache already has (possibly old), or ``None``.

        This method never reads from the device. It is for a screen that uses the contacts
        only as context and must not wait for them: for example, the nodes near the location
        picker. A companion can refuse the contacts read for some time
        (``ERR_CODE_BAD_STATE``). Then :meth:`contacts` waits through all the retries (more
        than twenty seconds) before it stops.
        """
        return self._contacts

    async def _fetch_contacts(self, *, force: bool = False) -> list[Contact]:
        """Read the contacts table from the device and cache it. The caller waits for the read.

        Concurrent calls share one read.

        Args:
            force: Read always. When ``False``, another caller can refresh the cache while
                this caller waits for the lock. Then this caller uses that new result, and
                does not send a second, identical round trip.
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
        """Store the contacts that were read, then add the contacts that the device forgot.

        A radio bridge without firmware loses its contact table when it starts again. Thus
        MeshTerm stores each contact that it reads (under the public key of the device) and
        adds the missing contacts back into the list. A device with firmware reports its full
        table, so its list does not change (refer to :mod:`meshterm.core.contact_store`).
        This step is best effort: if the store or the self-info read has a problem, the
        method returns the live list with no change. It never blocks the read.
        """
        store = getattr(self._ctx, "contact_store", None)
        if store is None:
            return fetched
        from ..core.contact_store import merge_contacts

        try:
            pubkey = str((await self.self_info()).get("public_key") or "")
        except Exception as exc:  # noqa: BLE001 - the store must never block a contacts read
            self._ctx.log.debug("devstate: contact remember/merge skipped: %s", exc)
            return fetched
        if not pubkey:
            return fetched
        store.remember_all(pubkey, fetched)
        return merge_contacts(store, pubkey, fetched)

    def _merge_heard(self, contacts: list[Contact]) -> list[Contact]:
        """Give each contact the later time of the device advert time and our own receptions.

        The ``last_seen`` value of a contact comes from the ``last_advert`` value of the
        firmware. The node that sent the advert put that time on it with its own clock, so
        the time is not first-hand. (:func:`~meshterm.core.models.advert_time` can refuse
        only a time in the future. A node whose clock is days late reports a time that looks
        correct and never becomes current.) Our own history has first-hand evidence instead,
        with the time at which we received something:

        * adverts and telemetry that we heard
          (:meth:`~meshterm.persistence.repository.Repository.last_heard_by_node`), and
        * direct messages that the node sent to us
          (:meth:`~meshterm.persistence.repository.Repository.last_message_by_peer`).

        The messages count, because in the lexicon of this app "heard" means "received from",
        and a message is received from its sender.

        Thus this method merges the three times and takes the latest. It does this one time,
        here, at the only point through which each screen gets contacts. Thus the heard lanes,
        the recency sorts, the silence alerts, and the archive ladder all see the same correct
        value.

        The method takes the latest time, and does not prefer one of the sources. Thus a time
        from the device stays when the firmware received an advert that we did not store
        (monitoring off, or the app not running). We replace that time only with proof of a
        later reception. A contact that we never heard stays ``None`` and shows ``never``.
        This step is best effort: if a history read fails, the method returns the list
        without the merge. It never blocks the read.
        """
        try:
            heard = self._ctx.repo.last_heard_by_node()
            messaged = self._ctx.repo.last_message_by_peer()
        except Exception as exc:  # noqa: BLE001 - the history must never block a contacts read
            self._ctx.log.debug("devstate: last-heard merge skipped: %s", exc)
            return contacts
        merged: list[Contact] = []
        for contact in contacts:
            ident = (contact.public_key or contact.key_prefix or "").lower()
            ident = ident.removeprefix("0x")
            stamps = [contact.last_seen, heard.get(ident[:12])]
            # The peer of a message has the width that the address on the wire used. That
            # width can be different from the width in the contact table. Thus either one can
            # be the shorter one. The live chat matches an incoming sender to its thread in
            # the same way.
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
        """Refresh the contacts in the background. If the refresh fails, keep the old list."""
        try:
            await self._fetch_contacts()
        except Exception as exc:  # noqa: BLE001 - an old list is acceptable. Do not show the error.
            self._ctx.log.debug("devstate: background contacts refresh failed: %s", exc)

    # -- self-info (stale-while-revalidate, and a write in the app invalidates it) ----

    async def self_info(self) -> dict:
        """Return the self-info of our node, cached and refreshed when old, as the contacts are.

        The first call reads it from the device. Later calls return the cached copy
        immediately. After :data:`_SELF_INFO_TTL_S`, a call also reads it again in the
        background, because a GPS can change the position. When the config editor writes,
        it also invalidates the cached copy immediately.

        Returns:
            The self-info payload (identity, radio tuning, coordinates, tx power). If the
            first read fails, this method raises the same error as
            :meth:`~meshterm.core.connection.Device.get_self_info`. The failure is not cached,
            thus the next call tries again.
        """
        if self._self_info is None:
            return await self._fetch_self_info()
        if time.monotonic() - self._self_info_at > _SELF_INFO_TTL_S:
            self._spawn(self._refresh_self_info_quietly())
        return self._self_info

    async def _fetch_self_info(self) -> dict:
        """Read the self-info from the device and cache it.

        The caller waits for the read. Concurrent calls share one read.
        """
        async with self._self_info_lock:
            if (
                self._self_info is not None
                and time.monotonic() - self._self_info_at <= _SELF_INFO_TTL_S
            ):
                return self._self_info
            device = await self._ctx.device()
            self._self_info = dict(await device.get_self_info())
            self._self_info_at = time.monotonic()
            return self._self_info

    async def _refresh_self_info_quietly(self) -> None:
        """Refresh the self-info in the background. If it fails, keep the copy in the cache."""
        try:
            await self._fetch_self_info()
        except Exception as exc:  # noqa: BLE001 - an old copy is acceptable. Do not show the error.
            self._ctx.log.debug("devstate: background self-info refresh failed: %s", exc)

    def peek_self_info(self) -> dict | None:
        """The self-info that the cache already has, or ``None``.

        This method never reads from the device. It is the :meth:`peek_contacts` for our
        node: context for a screen that must not wait.
        """
        return self._self_info

    async def path_hash_mode(self) -> int:
        """Return the path-hash routing mode of the device, cached for the session.

        Returns:
            The path-hash mode as an integer. If the read fails, this method raises the same
            error as :meth:`~meshterm.core.connection.Device.get_path_hash_mode`. Thus the
            callers that put this call in a ``try`` block, because the read is optional for
            them, use their fallback as before.
        """
        if self._path_hash_mode is None:
            # A lock, as for the other first reads: :meth:`prewarm` reads this value in the
            # background at connect. Without the lock, a screen that opens at the same time
            # sends a second, identical round trip, and does not wait for the read that is in
            # progress.
            async with self._path_hash_lock:
                if self._path_hash_mode is None:
                    device = await self._ctx.device()
                    self._path_hash_mode = int(await device.get_path_hash_mode())
        return self._path_hash_mode

    async def routing_prefix_bytes(self) -> int:
        """The path-hash width of the device in bytes, or 0 when it cannot be known.

        A screen that lights the hashes in keys must have this value: the number of bytes at
        the start of a key that the mesh routes on. :func:`~meshterm.ui.widgets.highlighted_hash`
        lights these bytes. This method is best effort, because each caller reads stored
        history and must work with no device at all. If the device cannot be reached (or the
        firmware does not report the mode), no hash is lit, and the screen does not fail.

        Returns:
            The hash width in bytes (1–4), or 0 when the mode could not be read.
        """
        try:
            if not (self._ctx.is_connected or self._ctx.settings.connect_on_start):
                return 0
            mode = await self.path_hash_mode()
        except Exception:  # noqa: BLE001 - an optional read. If it is absent, no hash is lit.
            return 0
        return (mode + 1) if isinstance(mode, int) and 0 <= mode <= 3 else 0

    # -- the default flood scope (kept until a config write invalidates it) ------

    async def default_scope(self) -> str:
        """The default flood scope that the companion keeps, or ``""`` for none. Read one time.

        A plain channel send uses this scope. Thus a channel that has no scope of its own
        stores its messages as sent in this scope. The method does one round trip. Then the
        cache keeps the value until the config editor writes a setting
        (:meth:`invalidate_config`) or the link is made again.

        Returns:
            The bare region name, or ``""`` when no default scope is set.

        Raises:
            Exception: The same errors as
                :meth:`~meshterm.core.connection.Device.get_default_flood_scope`. Firmware
                older than 1.15 has no default scope to read. The failure is not cached, thus
                the next call asks again.
        """
        if self._default_scope is None:
            device = await self._ctx.device()
            self._default_scope = str(await device.get_default_flood_scope() or "")
        return self._default_scope

    def note_default_scope(self, name: str | None) -> None:
        """Keep a default scope that other code read or wrote a short time ago (Device config).

        Args:
            name: The bare name, ``""`` for none, or ``None`` to forget the value and read it
                again.
        """
        self._default_scope = None if name is None else str(name)

    # -- channel slots (kept until the channel editor invalidates them) --------

    async def channel_slots(self) -> list[ChannelSlot]:
        """Return the configured channel slots, cached until a channel edit invalidates them.

        The probe that this method uses (:func:`~meshterm.core.channel_probe.read_channel_slots`)
        reads each slot index on the firmware. That is one of the slowest reads when a screen
        opens, thus one read for the session is a large benefit. The probe is best effort (on
        a firmware that does not support it, it gives an empty list and does not raise). But
        if the probe stopped early because a read failed, this method returns the result and
        does not cache it. Refer to :func:`~meshterm.core.channel_probe.probe_channel_slots`
        for the reason why a short list is not a layout.

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
                    # Return an incomplete probe, but do not keep it. Before, one read that
                    # timed out made the cache wrong for the whole session. The missing slots
                    # do not show themselves: an empty result looks the same as a device with
                    # no channels.
                    return slots
        return self._channels

    async def channel_capacity(self) -> int:
        """Return the channel-slot capacity of the device, found once and kept for the session.

        The capacity is a constant of the firmware build. Thus the method probes it one time
        and uses it again. The probe (:meth:`~meshterm.core.connection.Device.channel_capacity`)
        reads the slots in increasing order until the firmware rejects an index. If the
        firmware never rejects an index, the probe reads up to
        :data:`~meshterm.core.channels.CHANNEL_SLOT_PROBE_CAP` slots. That is one of the
        slowest reads when a screen opens. Before this cache, each open of the channel manager
        paid that cost.

        Unlike the list of configured slots, a series of empty slots cannot safely limit this
        probe, because the probe must reach the real limit of a larger firmware. Thus the
        cache limits the cost: the value stays until :meth:`reset` (a reconnect reads it
        again). A channel edit does not invalidate it, because an edit of a channel never
        changes the number of slots that the hardware has.

        Returns:
            The number of addressable channel slots that the firmware makes available.
        """
        if self._channel_capacity is None:
            async with self._capacity_lock:
                if self._channel_capacity is None:
                    device = await self._ctx.device()
                    self._channel_capacity = await device.channel_capacity()
        return self._channel_capacity

    # -- prewarm (fill the slow caches in the background, off the navigation path) --

    def prewarm(self) -> None:
        """Read all the cached facts in the background after connect, the fastest reads first.

        This method is called one time, when the link of the session is up (refer to
        :func:`meshterm.ui.menu._resume_monitor`). Thus the first screen that reads the facts
        (Contacts, Chat, Trace, the Dashboard, the channel manager) gets them from the cache
        immediately. It does not do the round trips in the navigation path, while the user
        waits for the screen to open. The first reads are necessary, but this method puts them
        into one quiet wait behind the menu, and the first open does not show them.

        **The order is the most important part of the design.** The reads share one link and
        run one after the other. Thus the first open must still wait for a fact that is read
        late. The two slot probes at the end are the slowest reads in the app: each probe
        reads the slot table one index at a time, and takes seconds. Thus the reads go from
        the fastest to the slowest:

        1. the two single round trips that each list screen must have (:meth:`self_info`,
           :meth:`path_hash_mode`),
        2. the contacts table,
        3. the probes for which only the channel manager waits.

        Then, if the user opens Contacts one second after connect, its three facts are
        already in the cache, and it does not wait behind a slot probe. The read of
        ``path_hash_mode`` closed the last such gap. The Contacts and Trace screens read it
        when they open, to find how many bytes of a key to light, and no other code read it
        before this method did.

        The work runs as a tracked background task:

        * **sequential**: the reads are never gathered, because concurrent reads collide on
          the BLE UART.
        * best effort: if a read fails, that fact stays out of the cache, and a normal read on
          demand gets it later.
        * cancelled on :meth:`reset` and :meth:`aclose`.

        If a getter is called before this task finishes, it waits for the same read that is
        in progress (the lock for each read prevents a duplicate). Thus the prewarm never
        reads a fact two times.
        """
        self._spawn(self._prewarm())

    async def _prewarm(self) -> None:
        """Read each cached fact, one after the other, and ignore failures (best effort)."""
        for label, fetch in (
            ("self-info", self.self_info),
            ("path-hash mode", self.path_hash_mode),
            ("contacts", self.contacts),
            ("channel slots", self.channel_slots),
            ("channel capacity", self.channel_capacity),
        ):
            try:
                await fetch()
            except Exception as exc:  # noqa: BLE001 - after a failure, a later read gets the fact
                self._ctx.log.debug("devstate: prewarm of %s failed: %s", label, exc)

    # -- invalidation (called by the code that writes device state) ------------

    def invalidate_contacts(self) -> None:
        """Remove the cached contacts, so that the next read gets them again.

        For example, call this method after a contact was removed.
        """
        self._contacts = None
        self._contacts_at = 0.0

    def invalidate_self_info(self) -> None:
        """Remove the cached self-info.

        Call this method after a write of the name, the coordinates, the radio, the tx power,
        or the tuning.
        """
        self._self_info = None

    def invalidate_path_hash_mode(self) -> None:
        """Remove the cached path-hash mode. Call this method after the config editor writes it."""
        self._path_hash_mode = None

    @property
    def channels_epoch(self) -> int:
        """The number of times that the cache removed the channel layout in this session.

        A caller can want to know if any code wrote a slot again while the caller was busy.
        That caller compares this value before and after, and does not trust a return value.
        A write can change the device and then raise an error on its way back. That write
        changed the device as much as a write that returned with no error. But that write
        never got to increment its count, so its count cannot show the change.
        """
        return self._channels_epoch

    def invalidate_channels(self) -> None:
        """Remove the cached channel slots.

        Call this method after the channel editor saves or clears a slot.
        """
        self._channels = None
        self._channels_epoch += 1

    def invalidate_config(self) -> None:
        """Remove all that the config editor can change, in one call (self-info, routing mode).

        This method helps the apply path of the config editor. That path can write any of
        the self-info fields, the path-hash mode, or both. To remove both costs less than to
        record exactly which settings changed.
        """
        self.invalidate_self_info()
        self.invalidate_path_hash_mode()
        self._default_scope = None

    def reset(self) -> None:
        """Clear all of the cache, and cancel each background refresh that is in progress.

        This method is called on a reconnect: a new link must read the true values again. It
        must not trust facts that were cached on the connection that was lost.
        """
        self.invalidate_contacts()
        self.invalidate_self_info()
        self.invalidate_path_hash_mode()
        self.invalidate_channels()
        self._default_scope = None
        # The capacity is a hardware constant. Channel edits do not change it, thus no write
        # invalidates it. But a reconnect can go to a different device, thus remove it too.
        self._channel_capacity = None
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    async def aclose(self) -> None:
        """Cancel each background refresh at the end of the session (an alias for :meth:`reset`)."""
        self.reset()

    def _spawn(self, coro) -> None:  # type: ignore[no-untyped-def]
        """Run a background refresh as a tracked task, so that a reset can cancel it."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
