# SPDX-License-Identifier: Apache-2.0
"""The interactive channel manager, rendered in the full-screen session.

This is the channel management that the user sees for the ``channels`` tool. It is a live
list of the channel slots of the device. For each slot, a detail screen shows the QR code
and the key that the user can share. On the detail screen the user can rename the channel,
change its key, open it in chat, mute its notifications, or clear it. There are four ways
to make a new channel: a new private channel (random key), a public ``#`` channel (the key
comes from the name), joining with a key that the user pastes, or importing a scanned
``meshcore://`` link. Each change is written to the device at once, as in a phone app.
Thus the list always shows the state of the radio.

The channel guide (:mod:`meshterm.ui.channel_guide`) is the first row of "Add a channel". It
lists the public channels that MeshTerm heard, and it adds one with Enter.

The section "Export and import" moves all the channels to another device. The export writes
each channel, with its key, its send scope, and its mute, to one TOML file. The import adds
the channels of such a file to the device, in the order of the file, and it never removes a
channel (refer to :mod:`meshterm.core.channel_file`).

Muting is the one setting of a channel that is a local *preference* and not a configuration
of the device. The new messages of a muted channel do not make the unread badge higher.
Its inbound messages do not add to the unread count, and muting sets the count to zero.
But MeshTerm still records the messages in the history. The mute is in
:class:`~meshterm.core.mute_store.MuteStore`. Its key is the intrinsic identity of the
channel, so the mute stays with the channel when the channel moves to another slot. The
list row shows a ``🔕`` in its unread lane (which is then always empty) to mark the mute.

The **send scope** of a channel is the other such setting. It is the region into which the
messages of the channel are flooded, so that only the repeaters that carry that region
relay them. The firmware has no send scope for each channel. The chat sets the session
scope of the companion around each send (refer to
:meth:`~meshterm.core.connection.Device.send_channel_in_scope`). Thus MeshTerm keeps the
send scope, in :class:`~meshterm.core.region_store.RegionStore`, with the same key type as
a mute. The row *Send scope…* of the detail page lets the user select the scope from the
regions that MeshTerm already knows, or type a name.

The list has the same layout as the config editor: fixed lanes, aligned in columns, under
one header line. The lanes are the openness glyph and name, the send scope, the unread
badge, the total messages, the age of the last message, and a braille sparkline of the
traffic of the last two hours. Thus a glance shows which channels exist and also which
channels are active. The openness (other than the glyph) and the hash are in the detail
screens (the title line and Show key). This keeps the list small enough that the activity
lane stays visible on a terminal of 72 columns. The message statistics come from
:meth:`~meshterm.persistence.repository.Repository.channel_stats` (read through a small
TTL cache). The unread counts come from the live chat service. Each row is a callable
title that the code resolves again at each paint. Thus, if a message arrives while the
list is open, its row updates in place. The conversation picker uses the same method. The
menus also follow the pattern of the config editor with a backdrop that stays: the list
stays pushed, and each sub-prompt floats over it as a modal dialog, and does not replace
the screen.

This module is in the UI layer. But, as the config editor and the chat screen do, it can
depend on the context and the services. It has no persistence of its own.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.console import Group
from rich.text import Text

from ..core.channel_file import (
    ChannelFileError,
    ImportPlan,
    apply_preferences,
    entry_from_slot,
    plan_import,
    read_channel_file,
    write_channel_file,
)
from ..core.channel_probe import ChannelSlot, probe_channel_slots
from ..core.channels import (
    DEFAULT_PUBLIC_SECRET,
    MAX_CHANNELS,
    derive_secret,
    normalize_secret,
    parse_share_url,
    random_secret,
    share_url,
)
from ..core.connection import Device
from ..core.regions import RegionNameError
from ..core.regions import validate as validate_region
from ..persistence.repository import ACTIVITY_DRAWN_BUCKETS
from ..platforms import get_platform
from .braillechart import activity_peak, activity_sparkline
from .menus import (
    Lane,
    column_header,
    fit_cells,
    icon_lane,
    marked_label,
    menu_rows,
    run_steps,
    section_heading,
)
from .qr import share_screen
from .theme import glyph
from .tui import CANCEL, Choice, SelectScreen, Separator
from .widgets import age_seconds, channel_glyph, format_age, format_ago

if TYPE_CHECKING:
    from ..context import AppContext
    from ..persistence.repository import ChannelStats

#: The footer sentence of the manager: navigation, then the filter, then the action, and Esc
#: last.
_MANAGER_HINT = "↑↓ move · type to filter · Enter open · Esc back"

# The sentinels of the actions of the top menu. They are different from a plain slot index,
# which selects that channel.
_CREATE = "__create__"
_PUBLIC = "__public__"
_DEFAULT_PUBLIC = "__default_public__"
_JOIN = "__join__"
_IMPORT = "__import__"
_REORDER = "__reorder__"
_EXPORT_FILE = "__export_file__"
_IMPORT_FILE = "__import_file__"
_GUIDE = "__guide__"

# The sentinels of the actions of the channel detail screen.
_QR = "qr"
_KEY = "key"
_CHAT = "chat"
_MUTE = "mute"
_SCOPE = "scope"
_EDIT = "edit"

# The sentinels of the send-scope picker: clear the scope of the channel, or type the name of
# a region.
_NO_SCOPE = "__no_scope__"
_TYPE_REGION = "__type_region__"

#: The footer of the scope picker. It is a value picker over a list that can grow, so it
#: filters.
_SCOPE_HINT = "↑↓ move · type to filter · Enter set · Esc keep"
_CLEAR = "clear"


async def manage_channels(ctx: AppContext) -> int:
    """Run the interactive channel manager until the user leaves it.

    The visit says **nothing** when the user leaves. It once stored a note for each action,
    and then added a line "✓ applied 3 channel changes" after them. The line was shown when
    the manager was already closed. It was an acknowledgement for changes that the user had
    just seen in the list in front of them. It came as a short dialog or as a full-frame
    result screen, and that depended on the number of lines that had collected. An error is the
    exception. The manager shows an error when it occurs (refer to :func:`_import_link`).

    The user gets the wait itself instead. Each device action reports *while it runs*, under
    the modal busy card (:meth:`~meshterm.ui.surface.Ui.busy_dialog`). Refer to
    :func:`write_channel` and ``settle`` below for the places where the cards are drawn, and
    for the reason why the card must be modal.

    Args:
        ctx: The shared application context (it gives the connected device and the UI).

    Returns:
        The number of channels that were created, changed, or cleared. This is the record
        of the run log of what the visit did (``ToolResult.summary``). The user does not
        see it.
    """
    device = await ctx.device()
    # The number of slots of the firmware is fixed for the session, so find it one time
    # (a probe that only reads) and do not assume a fixed 8. The number controls the check
    # for a free slot and the used/total display below. Read it through the session cache.
    # On firmware that never rejects an index that is out of range, the probe walks each
    # slot (slow). The count does not change for a connection, so the code warms it one time
    # (prewarm or the first open) and uses it again at each later open. If a device cannot
    # report any slots, the code uses the standard count. Thus the manager stays usable and
    # does not show zero capacity.
    capacity = await ctx.devstate.channel_capacity() or MAX_CHANNELS
    stats = _LiveStats(ctx)
    changes = 0
    highlight: object | None = None
    slots: list = []

    async def reload() -> tuple[str, list]:
        """Probe the slot layout again and build the title and the rows of the list again.

        This goes through the session cache. The slot probe walks each index, and it is one
        of the slowest reads when a screen opens. Thus a screen that opens after this one
        (or this one after another screen) uses the same probe again. The function keeps a
        local copy, so that the helpers below that change the list can work on a working
        list. Each change makes the cache invalid (refer to ``handle``). Thus this function
        probes the new layout again and does not trust the old copy.
        """
        nonlocal slots
        slots = list(await ctx.devstate.channel_slots())
        return _menu_items(ctx, slots, capacity, stats)

    async def settle(*, moved: bool = False) -> tuple[str, list]:
        """Probe the layout again. If a slot moved, file again what points to it.

        When something changed, this is two round trips to the device, one after the other.
        They are the slot map of the chat service, and the slot probe that ``reload`` calls,
        which is one of the slowest reads that the app does. They report as one wait, and
        not as boxes that flash in a sequence. Also, while the card is up, the user cannot
        type into a list whose rows are replaced under the user.

        Args:
            moved: Whether the occupant of a slot changed during the round that just ended.
                The caller finds this by a comparison of ``devstate.channels_epoch``. It
                does not trust a count (refer to :func:`write_channel`, which drops the
                cache when it writes).
        """
        async with ctx.ui.busy_dialog("reading channels…", title="Channels"):
            title_and_items = await reload()
            if moved:
                # An inbound message has only a slot index. The chat service maps it to the
                # identity of a channel through a cache with the slot as the key. Refresh the
                # cache now. Then a message on a slot that is used again or has a new key is
                # filed under the channel that is there now, and not under the channel that
                # was there before. If not, its transcript is in the wrong chat. This is
                # after ``reload`` on purpose. It reads the same cached probe, so in this
                # order the pair needs one walk of the device and not two.
                await _refresh_chat_channels(ctx)
            return title_and_items

    async def handle(choice: object) -> bool:
        """Do one menu choice (over the list that is still pushed). ``False`` leaves the manager."""
        nonlocal changes, highlight
        if choice is None:  # Esc
            return False
        highlight = choice
        if choice == _CREATE:
            changes += await _create_private(ctx, device, slots, capacity)
        elif choice == _DEFAULT_PUBLIC:
            changes += await _add_default_public(ctx, device, slots, capacity)
        elif choice == _PUBLIC:
            changes += await _add_public(ctx, device, slots, capacity)
        elif choice == _JOIN:
            changes += await _join_with_key(ctx, device, slots, capacity)
        elif choice == _IMPORT:
            changes += await _import_link(ctx, device, slots, capacity)
        elif choice == _REORDER:
            changes += await _reorder_channels(ctx, device, slots)
        elif choice == _EXPORT_FILE:
            await _export_channels(ctx, slots)  # it writes a file, and the device stays the same
        elif choice == _IMPORT_FILE:
            changes += await _import_channels(ctx, device, capacity)
        elif choice == _GUIDE:
            from .channel_guide import channel_guide  # it imports this module

            changes += await channel_guide(ctx, device, capacity)
        else:  # an existing slot index
            slot = next((s for s in slots if s.idx == choice), None)
            if slot is not None:
                changes += await _channel_detail(ctx, device, slot, stats)
        return True

    # The list stays pushed for the whole visit. Each sub-flow floats over it: to create a
    # channel, to import a link, or the detail page of a slot. After each sub-flow, the code
    # probes the rows again and replaces them in place. Thus the highlight (and a typed
    # filter) stays when the layout changed under it. A UI that has no session (the plain
    # CLI, or scripted tests) has no stack to stay on. It runs the same dispatcher, one
    # round at a time.
    title, items = await settle()
    session = getattr(ctx.ui, "session", None)
    if session is None:
        while True:
            epoch = ctx.devstate.channels_epoch
            try:
                keep = await _menu_round(ctx, title, items, default=highlight, handle=handle)
            except BaseException:
                # A write that reached the device and then raised an error changed the
                # device as much as a write that returned with no error. A ^W that unwinds
                # through the QR screen, which opened after the write, does the same. The
                # epoch shows this when the return value of the action did not have the
                # chance to show it. Thus the code files the chat map again before it
                # raises the error again.
                if ctx.devstate.channels_epoch != epoch:
                    await _refresh_chat_channels(ctx)
                raise
            if not keep:
                return changes
            title, items = await settle(moved=ctx.devstate.channels_epoch != epoch)
    # The hint is ``Enter open`` and not the default of the list. Each slot row pushes the
    # detail screen of the channel, and Reorder pushes the reorder screen. The chat picker
    # shows the same channels, one screen over, and it has always said the same.
    menu = SelectScreen(title, items, footer_hint=_MANAGER_HINT)
    async with session.stay(menu) as visit:
        while True:
            choice = await visit.result()
            epoch = ctx.devstate.channels_epoch
            try:
                keep = await handle(None if choice is CANCEL else choice)
            except BaseException:
                if ctx.devstate.channels_epoch != epoch:  # refer to the note on the other loop
                    await _refresh_chat_channels(ctx)
                raise
            if not keep:
                return changes
            title, items = await settle(moved=ctx.devstate.channels_epoch != epoch)
            menu.replace_items(items, title=title)


async def _menu_round(
    ctx: AppContext,
    title: str,
    items: list,
    *,
    handle: Callable[[object], Awaitable],
    default: object = None,
) -> object:
    """Select one time and do the choice. This is what a UI with no stack of screens does.

    In the full-screen session, both channel menus keep one screen for the whole visit
    (``session.stay``, with the rows replaced in place). This is the only way that the
    highlight, the sort, and a typed filter stay after an action. A UI that has no stack
    (the plain CLI, or a scripted test) has nothing to keep. It selects, does the choice,
    and is called again. ``default`` is the only value that it can carry from one round to
    the next. The summary line has no place to be drawn.

    Args:
        ctx: The shared application context.
        title: The heading in the border of the menu.
        items: The :class:`Choice` and :class:`Separator` rows of the menu.
        handle: The async function that is awaited with the selected value (``None`` for
            Esc).
        default: The value of a choice to highlight again, so that the menu opens again
            where the user left it.

    Returns:
        The value that ``handle`` returns.
    """
    return await handle(await ctx.ui.select(title, items, default=default))


async def _refresh_chat_channels(ctx: AppContext) -> None:
    """Build the slot-to-identity cache of the chat service again after a channel change.

    This is a best effort. A short fault in a device read must never stop the channel
    manager. The cache also repairs itself at the next miss, so the code only logs a
    failure.
    """
    try:
        await ctx.chat.refresh_channels()
    except Exception as exc:  # noqa: BLE001 - a best effort. It is never a fatal error here
        ctx.log.debug("channels: chat cache refresh failed: %s", exc)


def _next_free_slot(slots: list[ChannelSlot], capacity: int) -> int | None:
    """Return the lowest unused slot index, or ``None`` if each slot is full."""
    used = {s.idx for s in slots}
    return next((i for i in range(capacity) if i not in used), None)


# --- writing -----------------------------------------------------------------


async def write_channel(
    ctx: AppContext, device: Device, idx: int, name: str, secret: bytes | None
) -> None:
    """Write a channel to a device slot, and remember it for a restore.

    The memory lets MeshTerm restore a device that forgets its channels.
    This is the only boundary that each channel change goes through. Create, join, import,
    edit, reorder, and clear all come here. Thus the channel store is a true record of what
    MeshTerm wrote (refer to :mod:`meshterm.core.channel_store`). If the user clears a slot
    (an empty ``name``), the store forgets it. Any other write makes the store remember the
    channel under the device's own public key. For a channel whose key comes from its
    name, the store keeps that derived key, so that it can be sent again as it is. The
    remembering is a best effort. It never blocks the real write to the device, and it never
    makes the write fail.

    Because this is the only boundary, it is also the place where the *wait* is reported.
    The write and the identity probe after it are one round trip to the device each. Before
    the card existed, the list looked idle and fully interactive while they ran. A batch (a
    reorder) is nested in the card of its caller. It does not flash one box for each slot.

    Args:
        ctx: The shared application context (for the channel store and the cached
            self-info).
        device: The connected device to write to.
        idx: The slot to write.
        name: The channel name (an empty name clears the slot).
        secret: The secret of 16 bytes, or ``None`` to let the firmware derive it from the
            name.
    """
    caption = f"clearing slot {idx}…" if not name.strip() else f"saving {name.strip()}…"
    async with ctx.ui.busy_dialog(caption, title="Channels"):
        await _write_and_remember(ctx, device, idx, name, secret)
    # The layout on the device is not what any cache has now. Say so here, and do not leave
    # each caller to remember it. The manager of the menu did remember it, but it lost the
    # call when an action raised an error on its way back. The four CLI subcommands never
    # did it. The bump is also the signal that a caller compares across a round. Refer to
    # :attr:`~meshterm.services.device_state.DeviceState.channels_epoch`.
    ctx.devstate.invalidate_channels()


async def _write_and_remember(
    ctx: AppContext, device: Device, idx: int, name: str, secret: bytes | None
) -> None:
    """Do the write itself and the bookkeeping of the channel store.

    Refer to :func:`write_channel`.
    """
    await device.set_channel(idx, name, secret)
    store = ctx.channel_store
    if store is None:
        return
    try:
        pubkey = (await ctx.devstate.self_info()).get("public_key", "")
    except Exception as exc:  # noqa: BLE001 - the identity probe is a best effort, so skip the store
        ctx.log.debug("channels: could not read self key to remember channel: %s", exc)
        return
    if not pubkey:
        return
    if name.strip():
        store.remember(pubkey, idx, name, secret if secret is not None else derive_secret(name))
    else:
        store.forget(pubkey, idx)


def _slot_label(slot: ChannelSlot) -> str:
    """Format a channel for a compact row (the reorder screen): only its glyph and name.

    The reorder screen is about *position* and not about the vital data. The openness,
    hash, and message lanes of the manager list would only make the dialog wider. Thus
    each row is the channel exactly as the first lane of the manager shows it: glyph, gap,
    name.
    """
    return f"{channel_glyph(slot.name, slot.secret)} {slot.name}"


# --- notifications -------------------------------------------------------------


def _is_muted(ctx: AppContext, slot: ChannelSlot) -> bool:
    """Whether the notifications of new messages of this channel are muted.

    Refer to :class:`MuteStore`.
    """
    return ctx.mute_store.is_muted(slot.identity)


def _toggle_mute(ctx: AppContext, slot: ChannelSlot) -> None:
    """Change the notification mute of a channel. When it is muted, set its unread count to zero.

    Muting is a preference that MeshTerm remembers for each channel. Its key is the
    intrinsic identity of the channel, so it stays with the channel when the channel moves
    to another slot. Muting stops the new messages of the channel from making the unread
    badge higher. Muting also clears the unread count that the channel had in this session.
    Thus the badge goes down at once and does not stay until the next open. If the user
    unmutes the channel, future messages start to count again.
    """
    now_muted = not _is_muted(ctx, slot)
    ctx.mute_store.set_muted(slot.identity, now_muted)
    if now_muted:
        ctx.chat.clear_unread(slot.conversation.key)


# --- message statistics --------------------------------------------------------


#: The lower limit for the shared channel-activity peak. When each channel is quiet, a
#: single message is drawn against at least this number for each bucket. Thus a single
#: message stays a small nub and does not fill its column. The number is low, because
#: channel traffic is sparse from the start.
_ACTIVITY_FLOOR = 2.0


class _LiveStats:
    """A view of the statistics of the stored messages of each channel. It refreshes itself.

    This is the ``_LiveLasts`` pattern of the conversation picker, used for
    :meth:`~meshterm.persistence.repository.Repository.channel_stats`. The list rows read
    through this object at each paint (their titles are callables). Thus, if a message
    arrives while the manager is open, the counts, the age, and the sparkline of that
    channel update in place. But the code queries the repository at most one time for each
    ``ttl`` seconds, and not one time for each row at each paint. Thus a full slot table
    stays cheap at the paint rate of the session, which is approximately 1 Hz. The TTL is
    *higher* than that paint period on purpose. If the TTL was exactly the tick rate,
    each idle frame would still cause one repository query, and the throttle would limit
    nothing.
    """

    def __init__(self, ctx: AppContext, *, ttl: float = 3.0) -> None:
        """Connect to a context. The first read fills the cache."""
        self._ctx = ctx
        self._ttl = ttl
        self._cache: dict[str, ChannelStats] | None = None
        self._peak: float | None = None
        self._at = 0.0

    def _snapshot(self) -> dict[str, ChannelStats]:
        """The statistics of each channel.

        It reads the repository again at most one time for each ``ttl``.
        """
        now = time.monotonic()
        if self._cache is None or now - self._at >= self._ttl:
            try:
                self._cache = self._ctx.repo.channel_stats()
            except Exception:  # noqa: BLE001 - if a read fails, keep the last good snapshot
                self._cache = self._cache or {}
            self._peak = None  # it comes from the snapshot, so calculate it again at the next read
            self._at = now
        return self._cache

    def get(self, channel_id: str) -> ChannelStats | None:
        """Return the statistics for one channel identity. Refresh them when the TTL ends."""
        return self._snapshot().get(channel_id)

    def peak(self) -> float:
        """The sparkline scale that is shared across *each* channel. It is cached for each snapshot.

        This is a steady upper limit over the histograms of all the channels together (refer
        to :func:`~meshterm.ui.braillechart.activity_peak`). Outliers have little effect on
        it, so one busy burst does not flatten the column. It has a lower limit, so a single
        message in a quiet period stays a nub. It is steadier because the pool uses a time window
        that is deeper than the rows draw. The code gives this one value to the
        :func:`~meshterm.ui.braillechart.activity_sparkline` of each row. Thus the whole
        activity column is scaled against the busiest channel on the screen. The bar heights
        of the rows are comparable at a glance, and each row does not scale to its own
        upper limit.

        The method reads live through the same cache and TTL as :meth:`get`. It caches the
        value together with that snapshot, so a full slot table does not calculate the pooled
        peak again for each row.
        """
        snapshot = self._snapshot()
        if self._peak is None:
            self._peak = activity_peak(
                *(st.histogram for st in snapshot.values()),
                floor=_ACTIVITY_FLOOR,
            )
        return self._peak


#: The maximum width of the name lane. Names that are longer are cut with an ellipsis, so
#: that the lanes stay in their places.
_NAME_WIDTH_MAX = 18
#: The number of cells that the name lane and the send-scope lane share. The code draws the
#: scope lane only if at least one channel in the list has a scope, because a column of
#: blanks gives no information. The lane is then only as wide as the longest scope (but not
#: narrower than its ``SCOPE`` label). It takes what the names leave of this budget. If a
#: name has the width :data:`_NAME_WIDTH_MAX`, the scope lane gets eight cells. This is the
#: space that the row of 72 columns had before the lane existed, less the last cell that a
#: row with an ellipsis keeps free. Thus the activity sparkline keeps its full width on the
#: desktop. Shorter names give more cells to a long region name. A region name of 30 bytes
#: gets an ellipsis, and the row does not become wider. On the PicoCalc, with 53 columns,
#: the code already draws the sparkline shorter to fit (refer to :func:`_slot_text`). Thus a
#: scope lane there makes it shorter again. It takes cells from the MSGS count behind it
#: only when the chart has no cells left. The detail page states the scope. The leading
#: SLOT lane also uses cells from this budget (refer to :func:`_menu_items`), for the same
#: reason: the sparkline keeps its cells.
_NAME_SCOPE_BUDGET = _NAME_WIDTH_MAX + 8
#: The minimum width of the slot-index lane, which is aligned to the right. It has two
#: digits, because the default table of MeshCore has more than ten slots. A wider table
#: makes the lane as wide as its highest index.
_SLOT_WIDTH_MIN = 2
#: The width of the unread-badge lane (it fits ``● 999``). It is the same as the width in the
#: conversation picker.
_BADGE_WIDTH = 5
#: The width of the total-messages lane, which is aligned to the right.
_COUNT_WIDTH = 5
#: The width of the last-message-age lane, which is aligned to the right. It fits ages that
#: are as long as ``never``.
_AGE_WIDTH = 5


#: The number of cells that the activity sparkline uses when the row has room for all of it.
#: A braille cell has two buckets of five minutes, so the sparkline shows the last two hours.
_ACTIVITY_CELLS = ACTIVITY_DRAWN_BUCKETS // 2


@lru_cache(maxsize=64)
def _activity_sparkline(
    histogram: tuple[int, ...], peak: float, cells: int = _ACTIVITY_CELLS
) -> Text:
    """The braille activity sparkline of the channel, ``cells`` wide, with now on the right.

    This is the shared :func:`~meshterm.ui.braillechart.activity_sparkline` over the newest
    ``2 × cells`` buckets of five minutes of the histogram of the repository. At the full
    :data:`_ACTIVITY_CELLS` it shows the last two hours. It shows less where the row has
    less room (refer to :func:`_slot_text`). Then it removes the *oldest* buckets, so that
    now stays on the right edge. It is scaled to ``peak``, which is the shared upper limit
    across each channel (refer to :meth:`_LiveStats.peak`). Thus the whole activity column
    has one scale, and the bars of the rows are comparable at a glance. The histogram is
    deeper than the drawn part. The tail after the drawn buckets has an effect on ``peak``,
    but the chart does not show it.

    The function is cached on its inputs, which are hashable. The row callables are built
    again at each paint, but the histogram snapshot changes only one time for each
    :class:`_LiveStats` TTL. Thus between refreshes the sparkline of each slot is a cache
    hit. Callers must treat the returned Text as read-only.
    """
    return activity_sparkline(histogram, 2 * cells, peak=peak)


# --- menus -------------------------------------------------------------------


def _lanes_header(slot_w: int, name_w: int, scope_w: int, width: int) -> str:
    """The column headers over the fixed lanes of the channel list (refer to :func:`_slot_text`).

    The indent covers the pointer column of the select screen (2 cells, drawn on choice
    rows but not on separators). ``SLOT`` is first. It is the index that each ``channels``
    command and the slot references of the chat use. Its lane also covers the glyph lane
    after it, which has no label. The code *measures* the width and does not assume it.
    The glyph of a channel is two cells on the desktop and one on the console. A fixed width
    put each later label one column off there. ``NEW`` is over the lane of the unread
    badge. There is no TYPE lane and no HASH lane. The glyph already shows the openness, and
    the hash is in Show key. This gives the activity sparkline its room on a terminal of 72
    columns. ``SCOPE`` is between the name and the badge. It says where the messages of the
    channel go, which is a fact of the channel itself, and it comes before the lanes that
    count its traffic. The code shows it only while some channel has a scope (otherwise
    ``scope_w`` is 0). ``LAST`` is before ``MSGS``. A glance down the list asks how recently
    a channel spoke, and the total is the detail behind that.

    The code resolves the header against the render width, because the header row is pinned
    and must stay one row. At 53 columns the full line was 54 cells and it wrapped. That
    used one content row of the twenty-six, and the landmark was drawn two times.
    ``ACTIVITY`` gives its cells back first (refer to
    :func:`~meshterm.ui.menus.column_header`), as the chart under it does.
    """
    return column_header(
        [
            Lane("SLOT", slot_w + 2 + cell_len(channel_glyph("Public", None)) + 1),
            Lane("CHANNEL", name_w + 2),
            *([Lane("SCOPE", scope_w + 2)] if scope_w else []),
            Lane("NEW", _BADGE_WIDTH + 2),
            # Aligned to the right, because the values under them are. An age and a count
            # are padded to the right edge of their lane, so a label aligned to the left
            # would be away from the digits that it names.
            Lane(f"{'LAST':>{_AGE_WIDTH}}", _AGE_WIDTH + 2),
            Lane(f"{'MSGS':>{_COUNT_WIDTH}}", _COUNT_WIDTH + 2),
            Lane(("ACTIVITY", "ACT", "")),  # it is removed with its chart (refer to _slot_text)
        ],
        width,
    )


def _slot_row(
    ctx: AppContext, slot: ChannelSlot, stats: _LiveStats, slot_w: int, name_w: int, scope_w: int
) -> Callable[[int], Text]:
    """Return a title callable for a list row. The select screen renders it again at each paint.

    The code reads the unread badge, the counts, the age, and the activity sparkline live
    (refer to :class:`_LiveStats`). Thus, if a message arrives while the list is open, the
    row updates at the next paint. This is exactly the behaviour of the conversation
    picker. The callable takes the render width, so the sparkline can fit itself to the row
    (refer to :func:`_slot_text`).
    """
    return lambda width: _slot_text(ctx, slot, stats, slot_w, name_w, scope_w, width)


def _slot_text(
    ctx: AppContext,
    slot: ChannelSlot,
    stats: _LiveStats,
    slot_w: int,
    name_w: int,
    scope_w: int,
    width: int,
) -> Text:
    """Build the list row of one channel, ``width`` cells at most.

    The row has lanes of fixed width and colours. The alignment makes the row easy to read.
    The slot index (aligned right, muted), the glyph, the name, the send scope, the unread
    badge, the age of the last message, the total messages, and the activity sparkline are
    each in their own lane under the :func:`_lanes_header` line. The colour is light and has
    a purpose. The name is the focus of the row, in the base colour. The descriptive lanes
    are muted. The unread ``●`` badge is red, with its count in warn (the language of the
    conversation picker). The sparkline is drawn in the ok green over a faint flat line.
    A muted channel shows a muted ``🔕`` in the unread lane and not a count. Muting sets
    the unread count to zero and stops it from growing, so that lane is always free to
    show the state. The scope is the region name in the ``scope`` style. It is blank for a
    channel that sends with the default of the device. That is the ordinary case, and a
    word in each row would make it harder to see the other rows. If ``scope_w`` is 0 (no
    channel has a scope), the code does not draw the lane. The row is always a Rich
    :class:`~rich.text.Text`, so those spans stay under the row highlight of the select
    screen.

    **The sparkline is the last lane, and it becomes shorter to fit** (JP, 2026-10-03).
    Where the row is narrower than all the lanes at their full width (the 53 columns of the
    PicoCalc and the Cardputer, a long name, or a scope lane), the code draws the chart in
    the cells that the lanes before it leave. It removes the oldest buckets, so that now
    stays on the right. The ellipsis of the row does not cut the chart in the middle. If no
    cell is left for it, the code does not draw it.
    """
    st = stats.get(slot.identity)
    muted = _is_muted(ctx, slot)
    unread = ctx.chat.unread(slot.conversation.key)
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{slot.idx:>{slot_w}}", style="muted")
    text.append("  ")
    text.append(f"{channel_glyph(slot.name, slot.secret)} ")  # ＃ / 🌐 / 🔒 (2 cells) + gap
    text.append(fit_cells(slot.name, name_w))
    text.append("  ")
    if scope_w:
        text.append(fit_cells(_channel_scope(ctx, slot) or "", scope_w), style="scope")
        text.append("  ")
    if muted:
        mark = glyph("🔕")  # two cells on the desktop, one on the console
        text.append(mark, style="muted")
        text.append(" " * (_BADGE_WIDTH - cell_len(mark)))
    elif unread:
        text.append("●", style="err")
        text.append(f" {unread}".ljust(_BADGE_WIDTH - 1), style="warn")
    else:
        text.append(" " * _BADGE_WIDTH)
    text.append("  ")
    age = format_age(age_seconds(st.last_at)) if st is not None and st.last_at else ""
    text.append(f"{age:>{_AGE_WIDTH}}", style="muted")
    text.append("  ")
    total = st.total if st is not None else 0
    if total:
        # A limit, so that an extreme backlog cannot push the row out of its lanes.
        text.append(f"{min(total, 99999):>{_COUNT_WIDTH}}")
    else:
        # ``○`` is the empty mark of the app. ``·`` had three meanings here: the separator
        # that chains status atoms, the stand-in of the picker for an unknown sender, and,
        # in this same row, what the console folds ``🔕`` to. Thus a muted channel with no
        # messages drew the same glyph two times with different meanings.
        text.append(f"{'○':>{_COUNT_WIDTH}}", style="muted")
    # The cells that the lanes leave after the gap, up to the full width of the chart.
    cells = min(_ACTIVITY_CELLS, width - text.cell_len - 2)
    if cells > 0:
        text.append("  ")
        # The shared peak across all channels, so the sparkline of each row has one scale.
        histogram = st.histogram if st is not None else ()
        text.append_text(_activity_sparkline(histogram, stats.peak(), cells))
    return text


def _menu_items(
    ctx: AppContext, slots: list[ChannelSlot], capacity: int, stats: _LiveStats
) -> tuple[str, list]:
    """Build the title and the rows of the channel manager for the current slot table.

    The function returns the ``(title, items)`` for one :func:`_menu_round`. The items are
    the channel rows in their aligned lanes under a column-header line (the presentation of
    the config editor), and then the sections of actions, Organize and Add a channel. The
    slot usage is in the title, so the header line has only column labels.
    """
    items: list = []
    if slots:
        slot_w = max(_SLOT_WIDTH_MIN, *(len(str(s.idx)) for s in slots))
        name_w = min(_NAME_WIDTH_MAX, max(len("CHANNEL"), *(len(s.name) for s in slots)))
        scopes = [cell_len(_channel_scope(ctx, s) or "") for s in slots]
        # There is no scope lane while no channel has a scope (refer to
        # :data:`_NAME_SCOPE_BUDGET`). The slot lane and its gap come out of the same
        # budget, so the sparkline keeps its room. A scope lane that is drawn is never
        # narrower than its label. A long name gives up cells before the lane would put
        # ``SCOPE`` too close to ``NEW``.
        budget = _NAME_SCOPE_BUDGET - (slot_w + 2)
        scope_w = max(len("SCOPE"), min(budget - name_w, max(*scopes))) if any(scopes) else 0
        name_w = min(name_w, budget - scope_w)
        # The lane names are the only landmark of this block (its section has no
        # ── heading ──). Thus they stay pinned at the top while the slots scroll, and they
        # give way to Organize and Add a channel.
        items.append(Separator(lambda w: _lanes_header(slot_w, name_w, scope_w, w), heading=True))
        for slot in slots:
            # Fitted: the row is complete at any width (its chart gets shorter). Thus the
            # highlight also draws it fitted, and not as a line that ←→ slides
            # (Choice.fitted).
            row = _slot_row(ctx, slot, stats, slot_w, name_w, scope_w)
            items.append(Choice(title=row, value=slot.idx, fitted=True))
    else:
        items.append(Separator("  no channels yet — add one below"))

    # One icon column, which the code measures, for each command row on this screen. Thus
    # the marks of one cell (↕) start their labels in the same column as the marks of two
    # cells (＋ ＃ 🌐 🔑 🔗), and not one column early. The Organize row once corrected this
    # by hand, with two spaces and a comment of four lines. The column is empty where the
    # platform draws no icons, and the labels use the cells.
    lane = icon_lane(("↕", "💾", "📂", "⚡", "🌐", "＋", "＃", "🔑", "🔗"))
    if len(slots) > 1:
        items.append(section_heading("Organize"))
        reorder = marked_label("↕", "Reorder channels", "", lane=lane)
        items.append(Choice(title=reorder, value=_REORDER))

    # Before "Add a channel", because that section ends early when each slot is full, and an
    # import still has work then: it puts the channels in the order of its file.
    transfer = []
    if slots:
        export = marked_label("💾", "Export channels…", "", lane=lane)
        transfer.append((export, "Save them all to a file", _EXPORT_FILE))
    import_file = marked_label("📂", "Import channels…", "", lane=lane)
    transfer.append((import_file, "Add them from a file", _IMPORT_FILE))

    # The guide is first, and it stays when each slot is full: its list of the channels heard
    # is worth a read then too, and an add from it says why it cannot add.
    guide = marked_label("⚡", "Channel guide…", "", lane=lane)
    adds = [(guide, "Public channels heard on the mesh", _GUIDE)]
    free = _next_free_slot(slots, capacity) is not None
    if free:
        # The Public channel of the firmware has a fixed key. It has one well-known secret, so
        # it is the same channel on each slot. Offer to restore it only if no slot has it
        # already.
        if not any(s.secret == DEFAULT_PUBLIC_SECRET for s in slots):
            adds.append(
                (
                    marked_label("🌐", "Standard Public channel", "", lane=lane),
                    "MeshCore's built-in meshwide channel",
                    _DEFAULT_PUBLIC,
                )
            )
        adds.extend(
            [
                (
                    marked_label("＋", "New private channel…", "", lane=lane),
                    "A fresh random key",
                    _CREATE,
                ),
                (
                    marked_label("＃", "Public channel…", "", lane=lane),
                    "Key derived from its name",
                    _PUBLIC,
                ),
                (
                    marked_label("🔑", "Join with a key…", "", lane=lane),
                    "Paste a channel's 32-hex key",
                    _JOIN,
                ),
                (
                    marked_label("🔗", "Import a link…", "", lane=lane),
                    "Paste a meshcore:// share link",
                    _IMPORT,
                ),
            ]
        )
    # One call for the rows of the two sections, so that their descriptions start in the same
    # column. Two calls aligned each section to its own widest label, four cells apart.
    aligned = menu_rows(transfer + adds)
    items.append(section_heading("Export and import"))
    items.extend(aligned[: len(transfer)])
    items.append(section_heading("Add a channel"))
    items.extend(aligned[len(transfer) :])
    if not free:
        # The list already knows that there is no free slot. It says so here, and it does not
        # offer four rows that all end in the same refusal.
        items.append(Separator("  every slot is full — clear one first"))

    # ``·`` chains a status atom. ``—`` introduces a subject, and the slot count is not the
    # subject of this screen (refer to Contacts, which reads "Contacts · 12 known").
    return f"Channels · {len(slots)}/{capacity} slots", items


def _detail_summary(ctx: AppContext, slot: ChannelSlot, stats: _LiveStats) -> str:
    """One line of vital data for the detail screen: what the channel is, then how it was used.

    The openness and the hash are first. They moved here from the title of the screen. The
    title had them in a blob in parentheses, and it was 42 cells wide. That is the whole
    borderless title bar of the PicoCalc, and it left no room for the one place that says
    that Esc leaves.

    The line is one line. The code removes atoms from the right until it fits the readable
    width of the platform. The atoms on the left identify the channel. The atoms on the
    right describe traffic, which the user can also see in the row that the user came from.
    """
    st = stats.get(slot.identity)
    unread = ctx.chat.unread(slot.conversation.key)
    kind = "public" if slot.is_public else "private"
    parts = [kind, f"hash {slot.hash}", f"slot {slot.idx}"]
    # The scope goes with the identity of the channel and not with its traffic. It decides
    # who can hear the next message, so the line keeps it before the counts when it removes
    # atoms.
    scope = _channel_scope(ctx, slot)
    if scope:
        parts.append(f"scope {scope}")
    if st is None or not st.total:
        parts.append("no messages yet")
    else:
        parts.append(f"{st.total} msg{'' if st.total == 1 else 's'}")
        if unread:
            parts.append(f"{unread} unread")
    if _is_muted(ctx, slot):
        parts.append("muted")
    if st is not None and st.last_at is not None:
        parts.append(f"last {format_ago(age_seconds(st.last_at))}")
    width = get_platform().readable_cols
    while len(parts) > 1 and cell_len(" · ".join(parts)) > width:
        parts.pop()
    return " · ".join(parts)


def _detail_items(ctx: AppContext, slot: ChannelSlot) -> list:
    """Build the rows of the channel detail screen: label and description in two aligned lanes.

    This is the Actions presentation (lanes in the style of a menu, with no header line,
    because these are commands and not tabular data). The code pads in display cells, so
    that the emoji of double width do not move the description column. The Open in chat row
    has the live unread badge of the channel. The notifications row is a toggle, and its
    glyph and verb show the current mute state (it is an immediate action, so it has no
    ``…`` at the end). The one destructive row keeps a label with the err tint, so that it
    looks destructive.
    """
    unread = ctx.chat.unread(slot.conversation.key)
    # ✎ and 🗑 are one cell, and 📱 🔑 💬 🔔 🔕 are two cells. Thus the code measures the
    # column one time and pads each mark to it. If not, the edit row and the clear row start
    # their labels one column to the left of the rows above them.
    lane = icon_lane(("📱", "🔑", "💬", "🔔", "🔕", "🔖", "✎", "🗑"))
    chat_label = marked_label("💬", "Open in chat", "", lane=lane)
    if unread:
        chat_label.append("  ●", style="err")
        chat_label.append(f" {unread}", style="warn")
    if _is_muted(ctx, slot):
        mute_row = (
            marked_label("🔔", "Unmute notifications", "", lane=lane),
            "Show new messages here in the unread badge",
            _MUTE,
        )
    else:
        mute_row = (
            marked_label("🔕", "Mute notifications", "", lane=lane),
            "Hide new messages here from the unread badge",
            _MUTE,
        )
    items = menu_rows(
        [
            (
                marked_label("📱", "Show QR code", "", lane=lane),
                "Share this channel as a scannable code",
                _QR,
            ),
            (
                marked_label("🔑", "Show key", "", lane=lane),
                "The name, key, hash, and share link",
                _KEY,
            ),
            (chat_label, "Read and send messages on this channel", _CHAT),
            mute_row,
            _scope_row(ctx, slot, lane),
            (
                marked_label("✎", "Rename / change key…", "", lane=lane),
                "Edit the name or paste a different key",
                _EDIT,
            ),
            (
                # The tint is on the mark and not on the words. It is on the words only where
                # the platform draws no mark (refer to menus.marked_label).
                marked_label("🗑", "Clear this slot…", "err", lane=lane),
                "Remove the channel from this device",
                _CLEAR,
            ),
        ]
    )
    return items


async def _channel_detail(
    ctx: AppContext, device: Device, slot: ChannelSlot, stats: _LiveStats
) -> int:
    """Show the actions of one channel (QR, key, chat, rename, clear). Return the changes made.

    The screen starts with the vital data of the channel (refer to :func:`_detail_summary`)
    above the action rows. As the main list does, it is one screen for the whole visit. It
    stays pushed while the prompts of each action float over it. After the action, the code
    replaces the rows and the vital-data line in place, and does not draw the screen again.
    Both are live (the mute row is a toggle, and the summary counts the unread messages and
    gives the age of the last message). Thus the code reads both again at each round, and
    the highlight stays on the row that the user just used.

    A **clear** closes the page, because the channel that the page was about is gone. A
    **rename or a new key does not close it**. The channel is still there, and this is still
    its page. Thus the code reads the new name and key again into the title, the summary,
    and the rows, and the user stays where the user was. The page once closed in both cases.
    The only reason was that ``slot`` was a snapshot that the rename made old.
    """
    changes = 0

    def title_for() -> str:
        """``Feature — subject``. The openness and the hash are in the summary line instead."""
        return f"Channel — {slot.name}"

    async def reread() -> bool:
        """Point ``slot`` at what occupies its index now. ``False`` if nothing does."""
        nonlocal slot
        fresh = next((s for s in await ctx.devstate.channel_slots() if s.idx == slot.idx), None)
        if fresh is None:
            return False
        slot = fresh
        return True

    async def handle(choice: object) -> int | None:
        """Run one action.

        An int closes the detail screen with that number of changes. ``None`` stays.
        """
        nonlocal changes
        if choice is None:  # Esc
            return changes
        if choice == _QR:
            await _show_share(ctx, slot.name, slot.secret)
        elif choice == _KEY:
            await _show_key(ctx, slot)
        elif choice == _CHAT:
            await _open_chat(ctx, slot)
        elif choice == _MUTE:
            # This is a preference and not a change of the slot config. Change the toggle in
            # place and go back to the detail screen (its rows render again to the new
            # state). Do not count it as a channel change.
            _toggle_mute(ctx, slot)
        elif choice == _SCOPE:
            # MeshTerm keeps it, as it keeps the mute. Nothing is written to the radio until
            # a message is sent, so it is not a channel change either.
            await _pick_scope(ctx, slot)
        elif choice == _EDIT and await _edit(ctx, device, slot):
            changes += 1
            if not await reread():  # pragma: no cover - the slot that we just wrote is there
                return changes
        elif choice == _CLEAR and await _clear(ctx, device, slot):
            return changes + 1  # the channel of this page is gone, and the page is gone too
        return None

    # A UI that has no session (the plain CLI, or scripted tests) has no stack to stay on.
    # It runs the same dispatcher one round at a time, as the manager above does.
    session = getattr(ctx.ui, "session", None)
    if session is None:
        while True:
            result = await _menu_round(ctx, title_for(), _detail_items(ctx, slot), handle=handle)
            if result is not None:
                return result

    menu = SelectScreen(
        title_for(),
        _detail_items(ctx, slot),
        prompt=_detail_summary(ctx, slot, stats),
    )
    async with session.stay(menu) as visit:
        while True:
            choice = await visit.result()
            result = await handle(None if choice is CANCEL else choice)
            if result is not None:
                return result
            menu.replace_items(
                _detail_items(ctx, slot),
                title=title_for(),
                prompt=_detail_summary(ctx, slot, stats),
            )


# --- send scope ----------------------------------------------------------------


def _channel_scope(ctx: AppContext, slot: ChannelSlot) -> str | None:
    """The region for the messages of this channel, or ``None`` for the device default."""
    store = getattr(ctx, "region_store", None)
    return store.channel_scope(slot.identity) if store is not None else None


def _scope_row(ctx: AppContext, slot: ChannelSlot, lane: int) -> tuple:
    """The *Send scope…* row of the detail page. Its description says what is set now."""
    scope = _channel_scope(ctx, slot)
    description = f"Messages flood in {scope} only" if scope else "Keep messages in one region"
    return (marked_label("🔖", "Send scope…", "", lane=lane), description, _SCOPE)


def _scope_items(ctx: AppContext, current: str | None) -> list:
    """The rows of the scope picker: no scope, each known region, then a row to type one.

    A region is shown in the ``scope`` style (a region is not a node, so it has no node
    hue). The row also shows the number of repeaters that were heard to carry the region.
    This number decides if a message that has that scope goes anywhere. The scope that is
    now in use is marked ``current``.
    """
    store = getattr(ctx, "region_store", None)
    none_row = Text("No scope — the device default")
    if not current:
        none_row.append("  · current", style="muted")
    items: list = [Choice(title=none_row, value=_NO_SCOPE)]
    for name in store.names() if store is not None else []:
        row = Text(name, style="scope")
        carriers = len(store.carriers(name))
        if carriers:
            row.append(f"  · {carriers} repeater{'s' if carriers != 1 else ''}", style="muted")
        if name == current:
            row.append("  · current", style="muted")
        items.append(Choice(title=row, value=name))
    items.append(Choice(title=Text("Type a region name…"), value=_TYPE_REGION))
    return items


async def _pick_scope(ctx: AppContext, slot: ChannelSlot) -> bool:
    """Select the region in which the messages of a channel are sent. Return whether it changed.

    This is a value picker that floats over the detail page. It opens on the scope that is
    now in use. To type a new name is a second step on top of the first (the ``run_steps``
    shape). Esc on the name field goes back to the list and does not abandon the selection.
    The code sets a typed name and also learns it (source ``typed``), so that the next
    channel can select it from the list.

    Args:
        ctx: The shared application context.
        slot: The channel for which the user sets the scope.

    Returns:
        ``True`` if the scope of the channel changed.
    """
    store = getattr(ctx, "region_store", None)
    if store is None:  # pragma: no cover - every real context builds one
        return False
    current = store.channel_scope(slot.identity)
    session = getattr(ctx.ui, "session", None)
    while True:
        items = _scope_items(ctx, current)
        title = f"Send scope — {slot.name}"
        prompt = "The region this channel's messages flood into:"
        default = current or _NO_SCOPE
        if session is not None:
            picked = await session.select(
                title, items, prompt=prompt, default=default, footer_hint=_SCOPE_HINT
            )
        else:
            picked = await ctx.ui.select(title, items, prompt=prompt, default=default)
        if picked is None:
            return False
        if picked == _TYPE_REGION:
            typed = await ctx.ui.text(
                "Send scope",
                prompt="Region name, as its repeaters list it:",
                default=current or "",
                validate=_valid_region,
            )
            if typed is None:
                continue  # Esc on the name goes back to the list
            picked = store.learn(typed, "typed") or typed
        wanted = None if picked == _NO_SCOPE else str(picked)
        if wanted == current:
            return False
        store.set_channel_scope(slot.identity, wanted)
        return True


def _valid_region(text: str) -> bool | str:
    """Make sure that the firmware can hold the region name (refer to :func:`regions.validate`)."""
    try:
        validate_region(text)
    except RegionNameError as exc:
        return str(exc)
    return True


# --- create / join flows -----------------------------------------------------


async def _create_private(
    ctx: AppContext, device: Device, slots: list[ChannelSlot], capacity: int
) -> int:
    """Create a private channel with a new random key on the next free slot."""
    idx = await _pick_free_slot(ctx, device, slots, capacity)
    if idx is None:
        return 0
    name = await ctx.ui.text("Channel name:", validate=_nonblank)
    if not name:
        return 0
    secret = random_secret()
    await write_channel(ctx, device, idx, name.strip(), secret)
    await _show_share(ctx, name.strip(), secret)
    return 1


async def _add_default_public(
    ctx: AppContext, device: Device, slots: list[ChannelSlot], capacity: int
) -> int:
    """Add the ``Public`` channel of MeshCore, which has a fixed key, on the next free slot.

    It has one well-known secret. Thus it is the same channel on each slot, and a second
    copy is not a channel but a duplicate row. The menu already removes this action when a
    slot has the channel. This is the same check at the place where the action *runs*. It is
    for a key press that was on its way while the first write was still in progress.
    """
    if any(s.secret == DEFAULT_PUBLIC_SECRET for s in slots):
        return 0
    idx = await _pick_free_slot(ctx, device, slots, capacity)
    if idx is None:
        return 0
    await write_channel(ctx, device, idx, "Public", DEFAULT_PUBLIC_SECRET)
    return 1


async def _add_public(
    ctx: AppContext, device: Device, slots: list[ChannelSlot], capacity: int
) -> int:
    """Create a public channel. The key comes from its name, which starts with ``#``."""
    idx = await _pick_free_slot(ctx, device, slots, capacity)
    if idx is None:
        return 0
    raw = await ctx.ui.text(
        "Public channel name:",
        help_text="A leading # is added automatically; the key is derived from the name",
        validate=_nonblank,
    )
    if not raw:
        return 0
    name = raw.strip()
    if not name.startswith("#"):
        name = f"#{name}"
    secret = derive_secret(name)  # what the firmware will calculate. It is for the QR and share
    await write_channel(ctx, device, idx, name, None)  # None: the firmware derives the key
    await _show_share(ctx, name, secret)
    return 1


async def _join_with_key(
    ctx: AppContext, device: Device, slots: list[ChannelSlot], capacity: int
) -> int:
    """Join an existing private channel. The user enters its name and its key of 16 bytes.

    The two prompts are a stack (:func:`~meshterm.ui.menus.run_steps`). Esc on the key goes
    back to the name, and the text that the user typed is still in the field. The code does
    not remove both answers. A key of 32 hex digits is long, and it is easy to type it
    wrong.
    """
    idx = await _pick_free_slot(ctx, device, slots, capacity)
    if idx is None:
        return 0
    answers = await run_steps(
        [
            lambda vals: ctx.ui.text("Channel name:", default=vals[0] or "", validate=_nonblank),
            lambda vals: ctx.ui.text(
                "Channel key (32 hex characters / 16 bytes):",
                default=vals[1] or "",
                validate=_valid_secret,
            ),
        ]
    )
    if answers is None:
        return 0
    name, key = answers
    secret = normalize_secret(key)
    await write_channel(ctx, device, idx, name.strip(), secret)
    return 1


async def _import_link(
    ctx: AppContext, device: Device, slots: list[ChannelSlot], capacity: int
) -> int:
    """Import a channel from a pasted ``meshcore://channel/add`` link."""
    idx = await _pick_free_slot(ctx, device, slots, capacity)
    if idx is None:
        return 0
    url = await ctx.ui.text("Paste a meshcore:// channel link:", validate=_valid_link)
    if not url:
        return 0
    parsed = parse_share_url(url)
    if parsed is None:  # pragma: no cover - the validator prevents this
        await _say(ctx, "not a valid channel link", "err")
        return 0
    name, secret = parsed
    await write_channel(ctx, device, idx, name, secret)
    return 1


async def _edit(ctx: AppContext, device: Device, slot: ChannelSlot) -> bool:
    """Rename an existing channel, change its key, or do both. Return whether it changed.

    The prompts are the name, then the key, as a stack (:func:`~meshterm.ui.menus.run_steps`).
    Esc on the key goes back to the name, and the code does not remove the rename. Each step
    opens with the last answer that it got. The first time, this is the current value of the
    slot. When the user comes back to a step, this is the text that the user typed. A blank
    key that the user committed is included, because blank means *derive the key from the
    name*.
    """
    answers = await run_steps(
        [
            lambda vals: ctx.ui.text(
                "Channel name:",
                default=slot.name if vals[0] is None else vals[0],
                validate=_nonblank,
            ),
            lambda vals: ctx.ui.text(
                "Channel key (32 hex chars; blank to derive from the name):",
                default=(
                    ("" if slot.is_name_derived else slot.secret.hex())
                    if vals[1] is None
                    else vals[1]
                ),
                validate=_optional_secret,
            ),
        ]
    )
    if answers is None:
        return False
    name, key = answers
    name = name.strip()
    secret = normalize_secret(key) if key.strip() else None
    await write_channel(ctx, device, slot.idx, name, secret)
    return True


async def _clear(ctx: AppContext, device: Device, slot: ChannelSlot) -> bool:
    """Clear a channel slot after a confirmation. Return whether it was cleared.

    The confirmation is a red dialog for a loss of data. Cancel is on the left, and the verb
    is on the right and is the default. It has the ``destructive`` colours, because the
    clear removes the key of the slot. It is not a bare yes/no dialog. Thus it looks like
    each other delete confirm.
    """
    choice = await ctx.ui.dialog(
        f"Clear {slot.name}? This removes the channel from this device.",
        [("Cancel", None), ("Clear", "clear")],
        title="Clear channel",
        default=1,
        destructive=True,
    )
    if choice != "clear":
        return False
    ctx.chat.set_active(slot.conversation.key)  # remove its unread count before the slot is gone
    ctx.chat.set_active(None)
    await write_channel(ctx, device, slot.idx, "", None)  # an empty name: the slot is unused
    return True


# --- reordering --------------------------------------------------------------


async def _write_slot(ctx: AppContext, device: Device, idx: int, slot: ChannelSlot) -> None:
    """Write the contents of ``slot`` into slot ``idx``.

    A channel whose key comes from its name derives the key again.
    """
    secret = None if slot.is_name_derived else slot.secret
    await write_channel(ctx, device, idx, slot.name, secret)


async def _apply_order(
    ctx: AppContext, device: Device, slots: list[ChannelSlot], order: list[int]
) -> int:
    """Write the channel slots again so that they show in ``order``. Return the writes made.

    ``slots`` is the current list in the order of the slot index. ``order`` is a
    permutation of its positions (the reorder screen returns it). The code puts the
    channels again in the same physical slot indices, lowest first. Thus their order on the
    device is the same as the new display order. The code writes only the slots whose
    occupant changes. The reads come from the snapshot in memory. Thus the writes between
    them never overwrite a channel that is not placed yet.
    """
    indices = sorted(s.idx for s in slots)  # the physical slots to fill, ascending
    moves = [
        (target_idx, slots[pos])
        for target_idx, pos in zip(indices, order, strict=True)
        if slots[pos].idx != target_idx  # already in place, so no write is necessary
    ]
    if not moves:
        return 0
    # One card for the whole batch. Its title changes as the batch advances. This is the
    # longest operation of the manager: one write for each channel that moves, and each
    # write is a round trip to the device. It is the one place where a count is useful,
    # because the user can see that the operation advances and is not stuck. The cards of
    # the single writes are nested in this card.
    async with ctx.ui.busy_dialog("reordering channels…", title="Channels") as busy:
        for position, (target_idx, slot) in enumerate(moves, start=1):
            busy.message = f"moving {slot.name} · {position}/{len(moves)}"
            await _write_slot(ctx, device, target_idx, slot)
    return len(moves)


async def _reorder_channels(ctx: AppContext, device: Device, slots: list[ChannelSlot]) -> int:
    """Let the user move channels into a new order with the arrows. Return the writes made."""
    if len(slots) < 2:  # pragma: no cover - the menu offers reorder only with 2 or more channels
        return 0
    labels = [_slot_label(slot) for slot in slots]
    order = await ctx.ui.reorder("Reorder channels", labels)
    if not order or order == list(range(len(slots))):
        return 0  # the user cancelled, or the order did not change
    # The key of the chat history is the intrinsic identity of each channel and not its
    # slot. Thus the history follows the channels automatically, and a reorder needs no
    # migration of the history. The cache from slot to identity, which the chat service uses
    # to resolve inbound messages, is refreshed in one place by manage_channels when any
    # change arrives (refer to :func:`_refresh_chat_channels`).
    try:
        return await _apply_order(ctx, device, slots, order)
    except Exception as exc:  # noqa: BLE001 - reported here, where the half-done state is known
        # A reorder is a sequence of writes with no transaction under it. If the link drops
        # in the middle, the slots are between the two orders. The code must say this
        # plainly and at once. The manager reads again when this round ends, so the user will
        # look at the half-applied layout, and nothing else tells the user that.
        ctx.log.debug("channels: reorder stopped partway: %s", exc)
        await _say(ctx, f"reorder stopped partway — {exc}", "err")
        return 0


# --- export and import ---------------------------------------------------------

#: The file that the export and import prompts offer. The user can type another path.
_CHANNEL_FILE = "meshterm-channels.toml"


async def _export_channels(ctx: AppContext, slots: list[ChannelSlot]) -> None:
    """Write the channels of the device to a file, so that another device can import them.

    Each channel goes into the file with its key, its send scope, and its mute, in slot
    order (refer to :mod:`meshterm.core.channel_file`). The export writes nothing to the
    device, so the list does not change. Thus a dialog says what the export wrote and where,
    because nothing else on the screen shows that the file exists.
    """
    raw = await ctx.ui.path(
        "Export channels", prompt="Write the channels to this file:", default=_CHANNEL_FILE
    )
    if not raw:
        return
    path = Path(raw).expanduser().resolve()
    entries = [
        entry_from_slot(slot, scope=_channel_scope(ctx, slot), muted=_is_muted(ctx, slot))
        for slot in slots
    ]
    try:
        write_channel_file(path, entries)
    except OSError as exc:
        await _say(ctx, f"could not write {path} — {exc.strerror or exc}", "err")
        return
    private = sum(1 for slot in slots if not slot.is_public)
    text = f"{_count(len(entries), 'channel')} written to {path}"
    if private:
        text += f". It holds the keys of {_count(private, 'private channel')}: keep it private."
    await _say(ctx, text, "ok")


async def _import_channels(ctx: AppContext, device: Device, capacity: int) -> int:
    """Add the channels of an exported file to the device, in the order of the file.

    The import reads all the slots again before it plans. The plan writes many slots, and a
    probe that stopped early makes a slot that holds a channel look free (refer to
    :func:`_pick_free_slot` for the same reason on a single add). Then a dialog shows the
    plan, and nothing is written until the user selects Import. A file that has nothing new
    for the device still gives its send scopes and mutes.

    Returns:
        The number of slot writes that the import made.
    """
    raw = await ctx.ui.path(
        "Import channels", prompt="Read the channels from this file:", default=_CHANNEL_FILE
    )
    if not raw:
        return 0
    path = Path(raw).expanduser()
    try:
        entries = read_channel_file(path)
    except ChannelFileError as exc:
        await _say(ctx, str(exc), "err")
        return 0
    async with ctx.ui.busy_dialog("reading channels…", title="Channels"):
        slots, complete = await probe_channel_slots(device)
    if not complete:
        await _say(ctx, "could not read every slot — nothing was written", "err")
        return 0
    plan = plan_import(entries, slots, capacity)
    kept = list(plan.present) + [entry for _, entry in plan.added]
    if not plan.changes_device:
        apply_preferences(kept, ctx.region_store, ctx.mute_store)
        if plan.skipped:
            await _say(ctx, "every slot is full — clear a channel, then import again", "warn")
        else:
            text = f"this device already has the {_count(len(entries), 'channel')} of {path.name}"
            await _say(ctx, text, "ok")
        return 0
    choice = await ctx.ui.dialog(
        _plan_text(path, entries, plan),
        [("Cancel", None), ("Import", "import")],
        title="Import channels",
        default=1,
    )
    if choice != "import":
        return 0
    done = 0
    try:
        # One card for the whole import, as for a reorder. The card of each write is nested
        # in it, and the count shows that the import advances.
        async with ctx.ui.busy_dialog("importing channels…", title="Channels") as busy:
            for position, (idx, name, secret) in enumerate(plan.writes, start=1):
                what = f"saving {name}" if name else f"clearing slot {idx}"
                busy.message = f"{what} · {position}/{len(plan.writes)}"
                await write_channel(ctx, device, idx, name, secret)
                done += 1
    except Exception as exc:  # noqa: BLE001 - reported here, where the half-done state is known
        # The same case as a reorder that stops: the writes have no transaction, so the
        # slots are between the two layouts. Say so at once. The list reads the slots again
        # when this round ends.
        ctx.log.debug("channels: import stopped partway: %s", exc)
        await _say(ctx, f"import stopped partway — {exc}", "err")
        return done
    apply_preferences(kept, ctx.region_store, ctx.mute_store)
    if plan.skipped:
        names = ", ".join(entry.name for entry in plan.skipped)
        await _say(ctx, f"no free slot for {names} — clear a channel, then import again", "warn")
    return done


def _plan_text(path: Path, entries: list, plan: ImportPlan) -> Text:
    """The body of the import dialog: what the import adds, moves, and cannot add.

    The last line says that nothing is removed. A user who imports onto a device that has
    channels of its own must know that before Import, and not find it after.
    """
    width = max((cell_len(entry.name) for _, entry in plan.added), default=0)
    text = Text(f"{path.name} has {_count(len(entries), 'channel')}.\n")
    for slot, entry in plan.added:
        text.append(f"  + {channel_glyph(entry.name, entry.key)} ")
        text.append(entry.name.ljust(width), style="brand")
        text.append(f"  into slot {slot}\n", style="muted")
    if plan.moved:
        moved = _count(plan.moved, "channel")
        text.append(f"  ↕ {moved} on this device change slot, to match the file\n")
    if plan.skipped:
        names = ", ".join(entry.name for entry in plan.skipped)
        text.append(f"  ⚠ no free slot for {names}\n", style="warn")
    if plan.present:
        text.append(f"  {_count(len(plan.present), 'channel')} already on this device\n")
    text.append("Nothing is removed from the device.", style="muted")
    return text


def _count(n: int, noun: str) -> str:
    """``n`` and the noun, plural when ``n`` is not 1: ``1 channel``, ``3 channels``."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


# --- shared views ------------------------------------------------------------


async def _show_share(ctx: AppContext, name: str, secret: bytes) -> None:
    """Show the QR code of the channel and its share URL, in the full frame.

    Refer to ``share_screen``.
    """
    await share_screen(ctx, name=name, url=share_url(name, secret))


async def _show_key(ctx: AppContext, slot: ChannelSlot) -> None:
    """Show the name, type, hash, key, and share link of a channel as blocks with labels.

    This is not a table, on purpose. A grid with borders in a dialog is visual noise, and it
    wastes the columns that the values need. Each field is a muted label in the style of a
    column header, and its value is on the line under it. The long values (the hash, the
    key, and the link are for the user to copy completely) *wrap* to the width of the
    dialog (``overflow="fold"``, because they are single words that cannot break). They are
    not cut at an ellipsis. The first byte of the hash keeps the brand highlight. It is the
    fingerprint of two characters that the MeshCore companion app reports.
    """
    if slot.is_name_derived:
        kind = "public (key from name)"
    elif slot.is_public:
        kind = "public (default channel)"
    else:
        kind = "private"
    hash_text = Text(overflow="fold")
    hash_text.append(slot.full_hash[:2], style="brand")
    hash_text.append(slot.full_hash[2:], style="muted")
    fields: list[tuple[str, Text]] = [
        ("NAME", Text(slot.name, style="brand", overflow="fold")),
        ("TYPE", Text(kind)),
        ("HASH", hash_text),
        ("KEY", Text(slot.secret.hex(), style="warn", overflow="fold")),
        ("LINK", Text(share_url(slot.name, slot.secret), style="accent", overflow="fold")),
    ]
    blocks: list[Text] = []
    for label, value in fields:
        if blocks:
            blocks.append(Text())
        blocks.append(Text(label, style="muted"))
        blocks.append(value)
    await ctx.ui.view(Group(*blocks), title=f"Key — {slot.name}", footer_hint="Esc close")


async def _open_chat(ctx: AppContext, slot: ChannelSlot) -> None:
    """Open this channel on the live chat screen."""
    from .chat import open_chat

    await open_chat(ctx, slot.conversation)


async def _pick_free_slot(
    ctx: AppContext, device: Device, slots: list[ChannelSlot], capacity: int
) -> int | None:
    """Return a slot that is *confirmed* empty. If there is none, say why and return ``None``.

    The list that this function selects from is a snapshot, and a snapshot can be different
    from the truth. A slot probe that failed in the middle returns the slots that it could
    read. Then a slot that has a channel that the probe did not reach looks free. Each add
    flow comes here, and an unconditional write follows. Thus the function reads the slot one
    more time before it gives the slot to the caller. The code must not overwrite a channel
    because of a list that can have a missing row.

    Args:
        ctx: The shared application context (for the message dialogs).
        device: The connected device, for the confirming read.
        slots: The slots as they were last read.
        capacity: The number of slots of the device.

    Returns:
        The index of a free slot, or ``None`` if there is no slot to give.
    """
    idx = _next_free_slot(slots, capacity)
    if idx is None:
        await _say(ctx, "all channel slots are full — clear one first, then try again", "warn")
        return None
    try:
        async with ctx.ui.busy_dialog(f"checking slot {idx}…", title="Channels"):
            occupant = await device.get_channel(idx)
    except Exception as exc:  # noqa: BLE001 - a slot that the code cannot read is not proved free
        ctx.log.debug("channels: could not confirm slot %s is free: %s", idx, exc)
        await _say(ctx, f"could not read slot {idx} — nothing was written", "err")
        return None
    if occupant and occupant.get("channel_name"):
        await _say(ctx, f"slot {idx} is in use — reopen Channels to see what is there", "err")
        return None
    return idx


async def _say(ctx: AppContext, text: str, style: str) -> None:
    """Tell the user something *now*, in a dialog over the manager where it happened.

    An outcome during the visit is not a tool result. It belongs to the action in front of
    the user. Thus it goes to a dialog and not into the buffer that the menu empties when the
    whole tool has finished. A failed import once stayed in that buffer.
    """
    mark = {"err": "✗", "warn": "⚠", "ok": "✓"}.get(style, "")
    body = Text(f"{mark} {text}" if mark else text, style=style)
    session = getattr(ctx.ui, "session", None)
    if session is None:  # the scripted CLI has no dialogs, so print the text
        ctx.ui.note(f"[{style}]{body.plain}[/{style}]")
        return
    await session.message_dialog(body, title="Channels")


# --- validators --------------------------------------------------------------


def _nonblank(text: str) -> bool | str:
    """Make sure that the name is not empty."""
    return bool(text.strip()) or "Enter a channel name."


def _valid_secret(text: str) -> bool | str:
    """Make sure that the text is a valid key of 16 bytes in hex."""
    try:
        normalize_secret(text)
        return True
    except ValueError as exc:
        return str(exc)


def _optional_secret(text: str) -> bool | str:
    """Accept a blank key (derive it from the name) or a valid key of 16 bytes in hex."""
    return True if not text.strip() else _valid_secret(text)


def _valid_link(text: str) -> bool | str:
    """Make sure that the text is a ``meshcore://channel/add`` link that the code can parse."""
    return parse_share_url(text) is not None or "Not a valid meshcore:// channel link."
