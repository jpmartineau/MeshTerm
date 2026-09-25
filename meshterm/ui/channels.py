# SPDX-License-Identifier: Apache-2.0
"""The interactive channel manager, rendered in the full-screen session.

This is the user-facing channel-management experience for the ``channels`` tool: a live
list of the device's channel slots with, for each, a detail view that shows the sharable
QR code and key, renames or re-keys it, opens it in chat, mutes its notifications, or clears
it. New channels are created four ways — a fresh private channel (random key), a public ``#``
channel (key derived from the name), joining by pasting a key, or importing a scanned
``meshcore://`` link. Every change is written to the device immediately (like a phone app), so
the list you see always reflects the radio.

Muting is the one per-channel setting that is a local *preference* rather than device
configuration: a muted channel's new messages stop raising the unread badge (its inbound
messages no longer accrue unread, and muting zeros whatever it had), while still being
recorded to history. The mute lives in :class:`~meshterm.core.mute_store.MuteStore`, keyed by
the channel's intrinsic identity so it follows the channel across slot moves, and the list
row shows a ``🔕`` in its (then always-empty) unread lane to mark it.

A channel's **send scope** is the other: the region its messages are flooded into, so only
the repeaters carrying that region relay them. The firmware has no such thing per channel —
the chat sets the companion's session scope around each send instead (see
:meth:`~meshterm.core.connection.Device.send_channel_in_scope`) — so it is kept by MeshTerm,
in :class:`~meshterm.core.region_store.RegionStore`, keyed the same way a mute is. The
detail page's *Send scope…* row picks it from the regions already known here, or takes a
typed name.

The list is laid out like the config editor: fixed, column-aligned lanes under one header
line — the openness glyph and name, send scope, unread badge, total messages, last-message
age, and a braille sparkline of the trailing two hours' traffic — so a glance shows not
just *which* channels exist but which ones are alive. Openness beyond the glyph, and the
hash, live in the detail views (the title line and Show key), keeping the list lean enough that the
activity lane survives a 72-column terminal. The message statistics come from
:meth:`~meshterm.persistence.repository.Repository.channel_stats` (read through a small
TTL cache) and the unread counts from the live chat service, and each row is a callable
title re-resolved on repaint, so a message arriving while the list sits open updates its
row in place — the same trick the conversation picker uses. The menus also follow the
config editor's persistent-backdrop pattern: the list stays pushed while every sub-prompt
floats over it as a modal popup, rather than replacing the screen.

The module sits in the UI layer but, like the config editor and chat screen, is allowed to
depend on the context and services; it owns no persistence of its own.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.console import Group
from rich.text import Text

from ..core.channel_probe import ChannelSlot
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

#: The manager's footer sentence: navigation, then the filter, then the action, Esc last.
_MANAGER_HINT = "↑↓ move · type to filter · Enter open · Esc back"

# Top-menu action sentinels (distinct from a plain slot index, which selects that channel).
_CREATE = "__create__"
_PUBLIC = "__public__"
_DEFAULT_PUBLIC = "__default_public__"
_JOIN = "__join__"
_IMPORT = "__import__"
_REORDER = "__reorder__"

# Channel-detail action sentinels.
_QR = "qr"
_KEY = "key"
_CHAT = "chat"
_MUTE = "mute"
_SCOPE = "scope"
_EDIT = "edit"

# Send-scope picker sentinels: clear the channel's scope, or type a region by name.
_NO_SCOPE = "__no_scope__"
_TYPE_REGION = "__type_region__"

#: The scope picker's footer: a value picker over a list that can grow, so it filters.
_SCOPE_HINT = "↑↓ move · type to filter · Enter set · Esc keep"
_CLEAR = "clear"


async def manage_channels(ctx: AppContext) -> int:
    """Run the interactive channel manager until the user backs out.

    The visit says **nothing** on the way out. It used to bank a note per action and then an
    "✓ applied 3 channel changes" line on top of those, shown once the manager had already
    closed — an acknowledgement for changes the reader had just watched land in the list in
    front of them, arriving as a tidy popup or as a full-frame window depending on how many
    lines had piled up. An error is the exception, and is shown when it happens (see
    :func:`_import_link`).

    What the reader gets instead is the wait itself: every device action reports *while it
    runs*, under the modal busy card (:meth:`~meshterm.ui.surface.Ui.busy_dialog`) — see
    :func:`write_channel` and ``settle`` below for where those are drawn, and why the card
    has to be modal.

    Args:
        ctx: Shared application context (provides the connected device and UI surface).

    Returns:
        How many channels were created, changed, or cleared — the run log's record of what
        the visit did (``ToolResult.summary``), not anything the reader is shown.
    """
    device = await ctx.device()
    # The firmware's slot count is fixed for the session, so discover it once (a read-only
    # probe) rather than assuming a hard-coded 8; it drives the free-slot check and the
    # used/total display below. Read through the session cache: on firmware that never
    # rejects an out-of-range index the probe walks every slot (slow), and the count never
    # changes for a connection, so it is warmed once (prewarm/first open) and reused on every
    # later open. A device that can't report any slots falls back to the standard count so the
    # manager stays usable instead of showing zero capacity.
    capacity = await ctx.devstate.channel_capacity() or MAX_CHANNELS
    stats = _LiveStats(ctx)
    changes = 0
    highlight: object | None = None
    slots: list = []

    async def reload() -> tuple[str, list]:
        """Re-probe the slot layout and rebuild the list's title and rows.

        Through the session cache — the slot probe walks every index and is one of the
        slowest reads on a screen open, so a screen opened after this one (or this one after
        another) reuses the one probe. A local copy so the mutation helpers below can work a
        working list; each mutation invalidates the cache (see ``handle``), so this re-probes
        the fresh layout rather than trusting the stale copy.
        """
        nonlocal slots
        slots = list(await ctx.devstate.channel_slots())
        return _menu_items(ctx, slots, capacity, stats)

    async def settle(*, moved: bool = False) -> tuple[str, list]:
        """Re-probe the layout — and, when a slot actually moved, re-file what points at it.

        Two device round-trips back to back when something changed (the chat service's slot
        map and the slot probe ``reload`` calls one of the slowest reads the app makes), so
        they report as a single wait rather than boxes flashing in sequence — and while the
        card is up the reader cannot type into a list whose rows are being replaced
        underneath them.

        Args:
            moved: Whether a slot's occupant changed during the round just finished. The
                caller decides that by comparing ``devstate.channels_epoch``, not by trusting
                a count (see :func:`write_channel`, which drops the cache as it writes).
        """
        async with ctx.ui.busy_dialog("reading channels…", title="Channels"):
            title_and_items = await reload()
            if moved:
                # Inbound messages carry only a slot index, which the chat service maps to a
                # channel identity through a cache keyed by slot; refresh it now so a message
                # on a reused/re-keyed slot is filed under the channel that's actually there
                # and not the one that used to be — otherwise its transcript surfaces in the
                # wrong chat. After ``reload``, deliberately: it reads the same cached probe,
                # so this way round the pair costs one walk of the device instead of two.
                await _refresh_chat_channels(ctx)
            return title_and_items

    async def handle(choice: object) -> bool:
        """Dispatch one menu choice (over the still-pushed list); ``False`` exits."""
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
        else:  # an existing slot index
            slot = next((s for s in slots if s.idx == choice), None)
            if slot is not None:
                changes += await _channel_detail(ctx, device, slot, stats)
        return True

    # The list stays pushed for the whole visit: every sub-flow — creating a channel,
    # importing a link, a slot's detail page — floats over it, and the rows are re-probed and
    # swapped in place afterwards, so the highlight (and any typed filter) survives a layout
    # that changed under it. A surface with no session — the plain CLI, scripted tests — has
    # no stack to stay on and runs the same dispatcher a round at a time.
    title, items = await settle()
    session = getattr(ctx.ui, "session", None)
    if session is None:
        while True:
            epoch = ctx.devstate.channels_epoch
            try:
                keep = await _menu_round(ctx, title, items, default=highlight, handle=handle)
            except BaseException:
                # A write that landed and then raised — or a ^W unwinding out through the QR
                # view opened after it — moved the device just as much as one that returned
                # cleanly. The epoch says so where the action's return value never got the
                # chance to, so the chat map is re-filed on the way past.
                if ctx.devstate.channels_epoch != epoch:
                    await _refresh_chat_channels(ctx)
                raise
            if not keep:
                return changes
            title, items = await settle(moved=ctx.devstate.channels_epoch != epoch)
    # ``Enter open``, not the list default: every slot row pushes the channel's detail
    # screen and Reorder pushes the reorder screen, which is what the chat picker — the same
    # channels, one screen over — has always said.
    menu = SelectScreen(title, items, footer_hint=_MANAGER_HINT)
    async with session.stay(menu) as visit:
        while True:
            choice = await visit.result()
            epoch = ctx.devstate.channels_epoch
            try:
                keep = await handle(None if choice is CANCEL else choice)
            except BaseException:
                if ctx.devstate.channels_epoch != epoch:  # see the note on the other loop
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
    """Select once and dispatch the choice — what a surface with no screen stack does.

    Both channel menus keep one screen for the whole visit in the full-screen session
    (``session.stay``, rows swapped in place), which is the only way the cursor, the sort
    and a typed filter survive an action. A surface without a stack — the plain CLI, a
    scripted test — has nothing to keep, so it selects, dispatches, and is called again;
    ``default`` is all it can carry between rounds, and the summary line has nowhere to be
    drawn at all.

    Args:
        ctx: Shared application context.
        title: The menu's border heading.
        items: The menu's :class:`Choice`/:class:`Separator` rows.
        handle: Async dispatcher awaited with the chosen value (``None`` for Esc).
        default: A choice value to re-highlight, so the menu reopens where it was left.

    Returns:
        Whatever ``handle`` returns.
    """
    return await handle(await ctx.ui.select(title, items, default=default))


async def _refresh_chat_channels(ctx: AppContext) -> None:
    """Rebuild the chat service's slot→identity cache after a channel mutation.

    Best-effort: a device read hiccup here must never break the channel manager, and the
    cache also self-heals on the next miss, so a failure is only logged.
    """
    try:
        await ctx.chat.refresh_channels()
    except Exception as exc:  # noqa: BLE001 - refresh is best-effort; never fatal here
        ctx.log.debug("channels: chat cache refresh failed: %s", exc)


def _next_free_slot(slots: list[ChannelSlot], capacity: int) -> int | None:
    """Return the lowest unused slot index, or ``None`` when every slot is full."""
    used = {s.idx for s in slots}
    return next((i for i in range(capacity) if i not in used), None)


# --- writing -----------------------------------------------------------------


async def write_channel(
    ctx: AppContext, device: Device, idx: int, name: str, secret: bytes | None
) -> None:
    """Write a channel to a device slot, remembering it so a forgetful device can be restored.

    The single boundary every channel mutation goes through — create, join, import, edit,
    reorder, and clear all land here — so the channel store stays a faithful record of what
    MeshTerm wrote (see :mod:`meshterm.core.channel_store`). Clearing a slot (an empty ``name``)
    forgets it; any other write remembers the channel under the device's own public key, storing
    a name-derived channel's derived key so it can later be replayed as-is. Remembering is
    best-effort: it never blocks or fails the actual device write.

    Being the one boundary, it is also where the *wait* is reported: the write and the
    identity probe behind it are a device round-trip each, and until the card went up the
    list sat there looking idle and fully interactive while they ran. A batch (a reorder)
    nests inside its caller's card rather than flashing one box per slot.

    Args:
        ctx: Shared application context (for the channel store and cached self-info).
        device: The connected device to write to.
        idx: The slot to write.
        name: The channel name (empty clears the slot).
        secret: The 16-byte secret, or ``None`` to let the firmware derive it from the name.
    """
    caption = f"clearing slot {idx}…" if not name.strip() else f"saving {name.strip()}…"
    async with ctx.ui.busy_dialog(caption, title="Channels"):
        await _write_and_remember(ctx, device, idx, name, secret)
    # The layout on the device is no longer what anything cached, so say so here rather
    # than leaving each caller to remember: the menu's manager did (and lost it whenever an
    # action raised on its way back), and the four CLI subcommands never did at all. The
    # bump also *is* the signal a caller compares across a round — see
    # :attr:`~meshterm.services.device_state.DeviceState.channels_epoch`.
    ctx.devstate.invalidate_channels()


async def _write_and_remember(
    ctx: AppContext, device: Device, idx: int, name: str, secret: bytes | None
) -> None:
    """Do the write itself and the channel-store bookkeeping (see :func:`write_channel`)."""
    await device.set_channel(idx, name, secret)
    store = ctx.channel_store
    if store is None:
        return
    try:
        pubkey = (await ctx.devstate.self_info()).get("public_key", "")
    except Exception as exc:  # noqa: BLE001 - identity probe is best-effort; skip remembering
        ctx.log.debug("channels: could not read self key to remember channel: %s", exc)
        return
    if not pubkey:
        return
    if name.strip():
        store.remember(pubkey, idx, name, secret if secret is not None else derive_secret(name))
    else:
        store.forget(pubkey, idx)


def _slot_label(slot: ChannelSlot) -> str:
    """Format a channel for a compact row (the reorder screen): just its glyph and name.

    The reorder screen is about *position*, not vitals — the openness, hash, and message
    lanes of the manager list would only widen the popup — so each row is the channel
    exactly as the manager's first lane shows it: glyph, gap, name.
    """
    return f"{channel_glyph(slot.name, slot.secret)} {slot.name}"


# --- notifications -------------------------------------------------------------


def _is_muted(ctx: AppContext, slot: ChannelSlot) -> bool:
    """Whether this channel's new-message notifications are muted (see :class:`MuteStore`)."""
    return ctx.mute_store.is_muted(slot.identity)


def _toggle_mute(ctx: AppContext, slot: ChannelSlot) -> None:
    """Flip a channel's notification mute, zeroing its unread the moment it is muted.

    Muting is a remembered per-channel preference (keyed by the channel's intrinsic
    identity, so it follows the channel across slot moves) that stops its new messages from
    raising the unread badge. Muting also clears whatever unread it had accrued this session,
    so the badge drops immediately rather than lingering until the next open — the reverse
    (unmuting) simply lets future messages start counting again.
    """
    now_muted = not _is_muted(ctx, slot)
    ctx.mute_store.set_muted(slot.identity, now_muted)
    if now_muted:
        ctx.chat.clear_unread(slot.conversation.key)


# --- message statistics --------------------------------------------------------


#: Floor for the shared channel-activity peak: with every channel quiet, a lone message
#: draws against at least this many per bucket, so a single stray stays a small nub
#: rather than filling its column. Low, since channel chatter is sparse to begin with.
_ACTIVITY_FLOOR = 2.0


class _LiveStats:
    """A self-refreshing view of every channel's stored-message statistics.

    The conversation picker's ``_LiveLasts`` pattern applied to
    :meth:`~meshterm.persistence.repository.Repository.channel_stats`: the list rows read
    through this on every repaint (their titles are callables), so a message arriving while
    the manager sits open updates that channel's counts, age, and sparkline in place —
    but the repository is re-queried at most once per ``ttl`` seconds rather than once per
    row per repaint, so a full slot table stays cheap at the session's ~1 Hz repaint.
    The TTL sits *above* that repaint period on purpose: at exactly the tick rate every
    idle frame would still land one repository query — the throttle would gate nothing.
    """

    def __init__(self, ctx: AppContext, *, ttl: float = 3.0) -> None:
        """Bind to a context; the first read populates the cache."""
        self._ctx = ctx
        self._ttl = ttl
        self._cache: dict[str, ChannelStats] | None = None
        self._peak: float | None = None
        self._at = 0.0

    def _snapshot(self) -> dict[str, ChannelStats]:
        """Every channel's stats, re-reading the repository at most once per ``ttl``."""
        now = time.monotonic()
        if self._cache is None or now - self._at >= self._ttl:
            try:
                self._cache = self._ctx.repo.channel_stats()
            except Exception:  # noqa: BLE001 - keep the last good snapshot on a read error
                self._cache = self._cache or {}
            self._peak = None  # derived from the snapshot; recompute on the next read
            self._at = now
        return self._cache

    def get(self, channel_id: str) -> ChannelStats | None:
        """Return the stats for one channel identity, refreshing once the TTL lapses."""
        return self._snapshot().get(channel_id)

    def peak(self) -> float:
        """The shared sparkline scale across *every* channel, cached per snapshot.

        A steady ceiling over the pooled per-channel histograms (see
        :func:`~meshterm.ui.braillechart.activity_peak`): outlier-robust so one busy
        burst doesn't flatten the column, floored so a lull's stray message stays a nub,
        and steadied by pooling a window deeper than the rows draw. Handing this one
        value to every row's :func:`~meshterm.ui.braillechart.activity_sparkline` scales the whole
        activity column against the busiest channel on screen, so the rows' bar heights
        are comparable at a glance instead of each self-scaling to its own ceiling.

        Read live through the same cache/TTL as :meth:`get`, and memoised alongside that
        snapshot so a full slot table doesn't recompute the pooled peak once per row.
        """
        snapshot = self._snapshot()
        if self._peak is None:
            self._peak = activity_peak(
                *(st.histogram for st in snapshot.values()),
                floor=_ACTIVITY_FLOOR,
            )
        return self._peak


#: Widest the name lane grows (longer names are ellipsized so the lanes stay put).
_NAME_WIDTH_MAX = 18
#: Cells the name and send-scope lanes share. The scope lane is only as wide as the longest
#: scope on the list (never narrower than its ``SCOPE`` label), and takes what the names
#: leave of this budget: with a name at :data:`_NAME_WIDTH_MAX` it gets eight cells — what
#: the 72-column row had spare before the lane existed, less the last cell an ellipsizing
#: row keeps back — so the activity sparkline is never cut to make room; shorter names give
#: a long region name more of its own. A 30-byte region name is ellipsized rather than let
#: widen the row. On the PicoCalc's 53 the row was already cut inside the activity lane, so
#: the scope lane there costs the LAST ages behind it; the detail page still states both.
_NAME_SCOPE_BUDGET = _NAME_WIDTH_MAX + 8
#: Width of the unread-badge lane (fits ``● 999``), matching the conversation picker's.
_BADGE_WIDTH = 5
#: Width of the right-aligned total-messages lane.
_COUNT_WIDTH = 5
#: Width of the right-aligned last-message-age lane (fits ``never``-length ages).
_AGE_WIDTH = 5


@lru_cache(maxsize=32)
def _activity_sparkline(histogram: tuple[int, ...], peak: float) -> Text:
    """The channel's braille activity sparkline over the trailing two hours, now at the right.

    The shared :func:`~meshterm.ui.braillechart.activity_sparkline` over the newest
    :data:`ACTIVITY_DRAWN_BUCKETS` five-minute buckets of the repository histogram,
    scaled to ``peak`` — the shared ceiling across every channel (see
    :meth:`_LiveStats.peak`) — so the whole activity column shares one scale and the
    rows' bars are comparable at a glance. The histogram runs deeper than is drawn; the
    tail past the drawn buckets shapes ``peak`` but isn't charted.

    Memoized on its (hashable) inputs: the row callables rebuild every repaint, but the
    histogram snapshot only moves once per :class:`_LiveStats` TTL, so between refreshes
    every slot's sparkline is a cache hit. Callers treat the returned Text as read-only.
    """
    return activity_sparkline(histogram, ACTIVITY_DRAWN_BUCKETS, peak=peak)


# --- menus -------------------------------------------------------------------


def _lanes_header(name_w: int, scope_w: int, width: int) -> str:
    """Column headers over the channel list's fixed lanes (see :func:`_slot_text`).

    The indent covers the select screen's pointer column (2 cells, drawn on choice rows but
    not separators) plus the glyph lane, *measured* rather than assumed: a channel's glyph
    is two cells on the desktop and one on the console, so a hard-coded indent put every
    label a column off there. ``UNREAD`` borrows its lane's trailing gap — the badge lane
    itself is one cell too narrow for the word — which still leaves a space before the
    message count. (No TYPE or HASH lane: the glyph already carries the openness and the
    hash lives in Show key, which buys the activity sparkline its room on a 72-column
    terminal.) ``SCOPE`` sits between the name and the badge: it says where the channel's
    messages go, which is the channel's own fact, before the lanes that count its traffic.

    Resolved against the render width, because the header row is pinned and must stay one
    row: at 53 columns the full line ran to 54 and wrapped, costing a content row out of
    twenty-six and leaving the landmark drawn twice over. ``ACTIVITY`` gives its cells back
    first (see :func:`~meshterm.ui.menus.column_header`).
    """
    return column_header(
        [
            Lane("CHANNEL", name_w + 2),
            Lane("SCOPE", scope_w + 2),
            Lane("UNREAD", _BADGE_WIDTH + 2),
            # Right-aligned, because the values under them are: a count and an age are
            # padded to the right edge of their lane, so a left-aligned label would sit
            # off the digits it names.
            Lane(f"{'MSGS':>{_COUNT_WIDTH}}", _COUNT_WIDTH + 2),
            Lane(f"{'LAST':>{_AGE_WIDTH}}", _AGE_WIDTH + 2),
            Lane(("ACTIVITY", "ACT")),
        ],
        width,
        indent=2 + cell_len(channel_glyph("Public", None)) + 1,
    )


def _slot_row(
    ctx: AppContext, slot: ChannelSlot, stats: _LiveStats, name_w: int, scope_w: int
) -> Callable[[], Text]:
    """Return a list-row title *callable* the select screen re-renders on each repaint.

    The unread badge, counts, age, and activity sparkline are all read live (see
    :class:`_LiveStats`), so a message arriving while the list sits open updates the row on
    the next repaint — exactly the conversation picker's behavior.
    """
    return lambda: _slot_text(ctx, slot, stats, name_w, scope_w)


def _slot_text(
    ctx: AppContext, slot: ChannelSlot, stats: _LiveStats, name_w: int, scope_w: int
) -> Text:
    """Build one channel's list row as fixed-width, colour-coded lanes.

    Alignment carries the readability — glyph, name, send scope, unread badge, total
    messages, last-message age, and the activity sparkline each sit in their own lane under the
    :func:`_lanes_header` line. Colour stays light and purposeful: the name is the row's
    focus in the base colour, the descriptive lanes are muted, the unread ``●`` badge is
    red with its count in warn (the conversation picker's language), and the sparkline
    draws in the ok green over a faint flatline. A muted channel shows a muted ``🔕`` in
    the unread lane instead of a count — muting zeros its unread and stops it accruing, so
    that lane is always free to carry the state. The scope is the region name in the
    ``scope`` style, and blank for a channel that sends under the device default — the
    ordinary case, which a word in every row would only make harder to see past. The row
    is always a Rich
    :class:`~rich.text.Text` so those spans survive under the select screen's row highlight.
    """
    st = stats.get(slot.identity)
    muted = _is_muted(ctx, slot)
    unread = ctx.chat.unread(slot.conversation.key)
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{channel_glyph(slot.name, slot.secret)} ")  # ＃ / 🌐 / 🔒 (2 cells) + gap
    text.append(fit_cells(slot.name, name_w))
    text.append("  ")
    scope = _channel_scope(ctx, slot) or ""
    text.append(fit_cells(scope, scope_w), style="scope")
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
    total = st.total if st is not None else 0
    if total:
        # Clamped so a pathological backlog can't push the row out of its lanes.
        text.append(f"{min(total, 99999):>{_COUNT_WIDTH}}")
    else:
        # ``○`` is the app's empty mark. ``·`` here was three things at once: the separator
        # that chains status atoms, the picker's unknown-sender stand-in, and — two cells
        # away in this very row — what the console folds ``🔕`` to, so a muted channel with
        # no messages drew the same glyph twice meaning different things.
        text.append(f"{'○':>{_COUNT_WIDTH}}", style="muted")
    text.append("  ")
    age = format_age(age_seconds(st.last_at)) if st is not None and st.last_at else ""
    text.append(f"{age:>{_AGE_WIDTH}}", style="muted")
    text.append("  ")
    # The shared peak across all channels, so every row's sparkline uses one scale.
    text.append_text(_activity_sparkline(st.histogram if st is not None else (), stats.peak()))
    return text


def _menu_items(
    ctx: AppContext, slots: list[ChannelSlot], capacity: int, stats: _LiveStats
) -> tuple[str, list]:
    """Build the channel manager's title and rows for the current slot table.

    Returns the ``(title, items)`` for one :func:`_menu_round`: the channel rows in their
    aligned lanes under a column-header line (the config editor's presentation), then the
    Organize and Add-a-channel action sections. The slot usage lives in the title, so the
    header line is free to be pure column labels.
    """
    items: list = []
    if slots:
        name_w = min(_NAME_WIDTH_MAX, max(len("CHANNEL"), *(len(s.name) for s in slots)))
        scopes = [cell_len(_channel_scope(ctx, s) or "") for s in slots]
        scope_w = min(_NAME_SCOPE_BUDGET - name_w, max(len("SCOPE"), *scopes))
        # The lane names are this block's only landmark (its section carries no ── heading ──),
        # so they pin overhead while the slots scroll and give way to Organize/Add a channel.
        items.append(Separator(lambda w: _lanes_header(name_w, scope_w, w), heading=True))
        for slot in slots:
            items.append(Choice(title=_slot_row(ctx, slot, stats, name_w, scope_w), value=slot.idx))
    else:
        items.append(Separator("  no channels yet — add one below"))

    # One measured icon column for every command row on this screen, so the one-cell marks
    # (↕) start their labels in the same column as the two-cell ones (＋ ＃ 🌐 🔑 🔗) instead
    # of a column early — which the Organize row used to fix by hand, with two spaces and a
    # four-line comment. Empty where the platform draws no icons, and the labels take the
    # cells back.
    lane = icon_lane(("↕", "🌐", "＋", "＃", "🔑", "🔗"))
    if len(slots) > 1:
        items.append(section_heading("Organize"))
        reorder = marked_label("↕", "Reorder channels", "", lane=lane)
        items.append(Choice(title=reorder, value=_REORDER))

    items.append(section_heading("Add a channel"))
    if _next_free_slot(slots, capacity) is None:
        # The list already knows there is nowhere to put one, so it says so here rather than
        # offering four rows that all dead-end in the same refusal.
        items.append(Separator("  every slot is full — clear one first"))
        return f"Channels · {len(slots)}/{capacity} slots", items
    rows = []
    # The firmware's built-in fixed-key Public channel has one well-known secret, so it's the
    # same channel on every slot — offer to restore it only while no slot already holds it.
    if not any(s.secret == DEFAULT_PUBLIC_SECRET for s in slots):
        rows.append(
            (
                marked_label("🌐", "Standard Public channel", "", lane=lane),
                "MeshCore's built-in meshwide channel",
                _DEFAULT_PUBLIC,
            )
        )
    rows.extend(
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
    items.extend(menu_rows(rows))

    # ``·`` chains a status atom; ``—`` would introduce a subject, and the slot count is not
    # what this screen is about (see Contacts, which reads "Contacts · 12 known").
    return f"Channels · {len(slots)}/{capacity} slots", items


def _detail_summary(ctx: AppContext, slot: ChannelSlot, stats: _LiveStats) -> str:
    """One line of vital signs for the detail screen: what it is, then how it has been used.

    The openness and the hash lead, having moved here out of the screen's title, which was
    carrying them in a parenthesised blob and running to 42 cells — the whole of the
    PicoCalc's borderless title bar, leaving no room for the one place that says Esc leaves.

    The line is one line. Atoms are shed from the right until it fits the platform's
    readable width, because the ones on the left identify the channel and the ones on the
    right describe traffic the reader can also see in the row they came from.
    """
    st = stats.get(slot.identity)
    unread = ctx.chat.unread(slot.conversation.key)
    kind = "public" if slot.is_public else "private"
    parts = [kind, f"hash {slot.hash}", f"slot {slot.idx}"]
    # The scope rides with the channel's identity rather than its traffic: it decides who
    # can hear the next message, so it is kept ahead of the counts when the line sheds.
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
    """Build the channel-detail rows: label and description in two aligned lanes.

    The Actions presentation (menu-style lanes, no header line — these are commands,
    not tabular data), padded in display cells so the double-width emoji can't skew the
    description column. Open-in-chat carries the channel's live unread badge, the
    notifications row reads as a toggle whose glyph and verb reflect the current mute state
    (an immediate action, so no trailing ``…``), and the one destructive row keeps an
    err-tinted label so it reads as such.
    """
    unread = ctx.chat.unread(slot.conversation.key)
    # ✎ and 🗑 are one cell where 📱 🔑 💬 🔔 🔕 are two, so the column is measured once and
    # every mark padded out to it — otherwise the edit and clear rows start their labels a
    # column left of the rows above them.
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
                # The tint goes on the mark, not the words — and falls back to the words only
                # where the platform draws no mark at all (see menus.marked_label).
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
    """Show one channel's actions (QR, key, chat, rename, clear); return changes made.

    The screen leads with the channel's vital signs (see :func:`_detail_summary`) above the
    action rows, and — like the main list — is one screen for the whole visit: it stays
    pushed while each action's prompts float over it, and the rows and the vital-signs line
    are swapped in place afterwards rather than the screen being drawn again. Both are live
    (the mute row is a toggle; the summary counts unread and ages the last message), so both
    are re-read each round, and the highlight stays on the row that was just used.

    A **clear** closes the page, because the channel it was about is gone. A **rename or
    re-key does not**: the channel is still there and this is still its page, so the new name
    and key are read back into the title, the summary and the rows, and the reader stays
    where they were. The page used to close on both, for no better reason than ``slot`` being
    a snapshot that the rename had made stale.
    """
    changes = 0

    def title_for() -> str:
        """``Feature — subject``: the openness and the hash lead the summary line instead."""
        return f"Channel — {slot.name}"

    async def reread() -> bool:
        """Re-point ``slot`` at what now occupies its index; ``False`` if nothing does."""
        nonlocal slot
        fresh = next((s for s in await ctx.devstate.channel_slots() if s.idx == slot.idx), None)
        if fresh is None:
            return False
        slot = fresh
        return True

    async def handle(choice: object) -> int | None:
        """Run one action; an int closes the detail with that many changes, ``None`` stays."""
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
            # A preference, not a slot-config change: toggle in place and loop back to the
            # detail (whose rows re-render to the new state) without counting a channel change.
            _toggle_mute(ctx, slot)
        elif choice == _SCOPE:
            # MeshTerm's to keep, like the mute: nothing is written to the radio until a
            # message is sent, so it is not a channel change either.
            await _pick_scope(ctx, slot)
        elif choice == _EDIT and await _edit(ctx, device, slot):
            changes += 1
            if not await reread():  # pragma: no cover - the slot we just wrote is there
                return changes
        elif choice == _CLEAR and await _clear(ctx, device, slot):
            return changes + 1  # the channel this page is about is gone; so is the page
        return None

    # A surface with no session (the plain CLI, scripted tests) has no stack to stay on and
    # runs the same dispatcher a round at a time, as the manager above does.
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
    """The region this channel's messages are sent under, or ``None`` for the device default."""
    store = getattr(ctx, "region_store", None)
    return store.channel_scope(slot.identity) if store is not None else None


def _scope_row(ctx: AppContext, slot: ChannelSlot, lane: int) -> tuple:
    """The detail page's *Send scope…* row, its description saying what is set now."""
    scope = _channel_scope(ctx, slot)
    description = f"Messages flood in {scope} only" if scope else "Keep messages in one region"
    return (marked_label("🔖", "Send scope…", "", lane=lane), description, _SCOPE)


def _scope_items(ctx: AppContext, current: str | None) -> list:
    """The scope picker's rows: no scope, every region known here, then typing one in.

    A region reads in the ``scope`` style (a region is not a node, so no node hue), with
    how many repeaters were heard to carry it — the thing that decides whether a message
    scoped to it goes anywhere at all. The one in force is marked ``current``.
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
    """Pick the region a channel's messages are sent under; return whether it changed.

    A value picker floated over the detail page, opening on the scope in force. Typing a
    new name is a second step stacked on the first (the ``run_steps`` shape): Esc on the
    name field comes back to the list rather than abandoning the pick. A typed name is
    learned (source ``typed``) as well as set, so the next channel can pick it from the
    list.

    Args:
        ctx: Shared application context.
        slot: The channel whose scope is being set.

    Returns:
        ``True`` when the channel's scope changed.
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
                continue  # Esc on the name steps back to the list
            picked = store.learn(typed, "typed") or typed
        wanted = None if picked == _NO_SCOPE else str(picked)
        if wanted == current:
            return False
        store.set_channel_scope(slot.identity, wanted)
        return True


def _valid_region(text: str) -> bool | str:
    """Require a region name the firmware could hold (see :func:`regions.validate`)."""
    try:
        validate_region(text)
    except RegionNameError as exc:
        return str(exc)
    return True


# --- create / join flows -----------------------------------------------------


async def _create_private(
    ctx: AppContext, device: Device, slots: list[ChannelSlot], capacity: int
) -> int:
    """Create a private channel with a fresh random key on the next free slot."""
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
    """Add MeshCore's built-in fixed-key ``Public`` channel on the next free slot.

    One well-known secret means it is the same channel on every slot, so a second copy is
    not a channel but a duplicate row. The menu already drops this action once a slot holds
    it; this is the same check where the action *runs*, for a press that was on its way while
    the first one was still being written.
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
    """Create a public channel whose key is derived from its (``#``-prefixed) name."""
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
    secret = derive_secret(name)  # what the firmware will compute; kept for the QR/share
    await write_channel(ctx, device, idx, name, None)  # None => firmware derives the key from name
    await _show_share(ctx, name, secret)
    return 1


async def _join_with_key(
    ctx: AppContext, device: Device, slots: list[ChannelSlot], capacity: int
) -> int:
    """Join an existing private channel by entering its name and 16-byte key.

    The two prompts are a stack (:func:`~meshterm.ui.menus.run_steps`): Esc on the key
    steps back to the name with what was typed still in the field, rather than throwing
    both away — a 32-hex key is a long thing to mistype.
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
    if parsed is None:  # pragma: no cover - guarded by the validator
        await _say(ctx, "not a valid channel link", "err")
        return 0
    name, secret = parsed
    await write_channel(ctx, device, idx, name, secret)
    return 1


async def _edit(ctx: AppContext, device: Device, slot: ChannelSlot) -> bool:
    """Rename and/or re-key an existing channel; return whether it changed.

    Name then key, as a stack (:func:`~meshterm.ui.menus.run_steps`): Esc on the key steps
    back to the name rather than dropping the rename with it. Each step opens on what it
    was last given — the slot's current value the first time through, whatever was typed
    when it is come back to (a committed blank key included, since blank means *derive it
    from the name*).
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
    """Clear a channel slot after confirmation; return whether it was cleared.

    A red data-loss dialog (Cancel left, the verb right and default, ``destructive``
    theming — clearing a slot drops its key) rather than a bare yes/no, so it reads
    like every other delete confirm.
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
    ctx.chat.set_active(slot.conversation.key)  # drop its unread before the slot goes away
    ctx.chat.set_active(None)
    await write_channel(ctx, device, slot.idx, "", None)  # empty name => the slot reads as unused
    return True


# --- reordering --------------------------------------------------------------


async def _write_slot(ctx: AppContext, device: Device, idx: int, slot: ChannelSlot) -> None:
    """Write ``slot``'s contents into slot ``idx`` (name-derived channels re-derive their key)."""
    secret = None if slot.is_name_derived else slot.secret
    await write_channel(ctx, device, idx, slot.name, secret)


async def _apply_order(
    ctx: AppContext, device: Device, slots: list[ChannelSlot], order: list[int]
) -> int:
    """Rewrite the channel slots so they display in ``order``; return the writes made.

    ``slots`` is the current list in slot-index order and ``order`` is a permutation of its
    positions (as returned by the reorder screen). The channels are re-laid across the same
    physical slot indices, lowest first, so their on-device order matches the new display
    order. Only slots whose occupant actually changes are written. Reads come from the
    in-memory snapshot, so the interleaved writes never clobber a not-yet-placed channel.
    """
    indices = sorted(s.idx for s in slots)  # the physical slots to fill, ascending
    moves = [
        (target_idx, slots[pos])
        for target_idx, pos in zip(indices, order, strict=True)
        if slots[pos].idx != target_idx  # already in place; no write needed
    ]
    if not moves:
        return 0
    # One card for the whole batch, retitled as it walks: this is the longest thing the
    # manager does — a write per moved channel, each a device round-trip — and the one place
    # where a count is worth showing, because the reader can see it is progressing rather
    # than stuck. The per-write cards inside nest into this one.
    async with ctx.ui.busy_dialog("reordering channels…", title="Channels") as busy:
        for position, (target_idx, slot) in enumerate(moves, start=1):
            busy.message = f"moving {slot.name} · {position}/{len(moves)}"
            await _write_slot(ctx, device, target_idx, slot)
    return len(moves)


async def _reorder_channels(ctx: AppContext, device: Device, slots: list[ChannelSlot]) -> int:
    """Let the user drag channels into a new order with the arrows; return writes made."""
    if len(slots) < 2:  # pragma: no cover - the menu only offers reorder with 2+ channels
        return 0
    labels = [_slot_label(slot) for slot in slots]
    order = await ctx.ui.reorder("Reorder channels", labels)
    if not order or order == list(range(len(slots))):
        return 0  # cancelled or left unchanged
    # Chat history is keyed by each channel's intrinsic identity, not its slot, so it follows
    # the channels automatically — reordering needs no history migration. The slot→identity
    # cache the chat service resolves inbound messages through is refreshed centrally by
    # manage_channels once any change lands (see :func:`_refresh_chat_channels`).
    try:
        return await _apply_order(ctx, device, slots, order)
    except Exception as exc:  # noqa: BLE001 - reported here, where the half-done state is
        # A relay is a sequence of writes with no transaction under it, so a link that drops
        # partway leaves the slots between the two orders. That is worth saying plainly and
        # at once: the manager re-reads on the way out of this round, so what the reader is
        # about to look at *is* the half-applied layout, and nothing else would tell them so.
        ctx.log.debug("channels: reorder stopped partway: %s", exc)
        await _say(ctx, f"reorder stopped partway — {exc}", "err")
        return 0


# --- shared views ------------------------------------------------------------


async def _show_share(ctx: AppContext, name: str, secret: bytes) -> None:
    """Show the channel's QR code and its share URL, full-frame (see ``share_screen``)."""
    await share_screen(ctx, name=name, url=share_url(name, secret))


async def _show_key(ctx: AppContext, slot: ChannelSlot) -> None:
    """Show a channel's name, type, hash, key, and share link as labelled blocks.

    Deliberately not a table: a bordered grid inside a popup is visual noise and wastes
    the very columns the values need. Each field is instead a muted column-header-style
    label with its value on the line beneath, and the long values — the hash, key, and
    link exist to be copied out whole — *wrap* to the popup's width (``overflow="fold"``,
    since they are single unbreakable words) rather than being chopped at an ellipsis.
    The hash's first byte keeps the brand highlight: it is the two-character fingerprint
    the MeshCore companion app reports.
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
    """Open this channel in the live chat screen."""
    from .chat import open_chat

    await open_chat(ctx, slot.conversation)


async def _pick_free_slot(
    ctx: AppContext, device: Device, slots: list[ChannelSlot], capacity: int
) -> int | None:
    """Return a slot that is *confirmed* empty, or ``None`` (having said why) if there is none.

    The list this picks from is a snapshot, and a snapshot can be short of the truth: a slot
    probe that failed partway returns the slots it managed to read, so a slot holding a
    channel the probe never reached looks free. Every add flow lands here, and what follows
    it is an unconditional write — so the slot is read back one more time before it is handed
    out. A channel is not something to overwrite on the strength of a list that might be
    missing a row.

    Args:
        ctx: Shared application context (for the message surfaces).
        device: The connected device, for the confirming read.
        slots: The slots as last read.
        capacity: The device's slot count.

    Returns:
        A free slot index, or ``None`` when there is none to give.
    """
    idx = _next_free_slot(slots, capacity)
    if idx is None:
        await _say(ctx, "all channel slots are full — clear one first, then try again", "warn")
        return None
    try:
        async with ctx.ui.busy_dialog(f"checking slot {idx}…", title="Channels"):
            occupant = await device.get_channel(idx)
    except Exception as exc:  # noqa: BLE001 - unreadable is not provably free
        ctx.log.debug("channels: could not confirm slot %s is free: %s", idx, exc)
        await _say(ctx, f"could not read slot {idx} — nothing was written", "err")
        return None
    if occupant and occupant.get("channel_name"):
        await _say(ctx, f"slot {idx} is in use — reopen Channels to see what is there", "err")
        return None
    return idx


async def _say(ctx: AppContext, text: str, style: str) -> None:
    """Tell the reader something *now*, in a popup over the manager it happened in.

    An in-visit outcome is not a tool result: it belongs to the action in front of the
    reader, so it goes to the dialog surface rather than into the buffer the menu drains
    once the whole tool has finished (which is where a failed import used to leave it).
    """
    mark = {"err": "✗", "warn": "⚠"}.get(style, "")
    body = Text(f"{mark} {text}" if mark else text, style=style)
    session = getattr(ctx.ui, "session", None)
    if session is None:  # the scripted CLI has no dialog surface; print it
        ctx.ui.note(f"[{style}]{body.plain}[/{style}]")
        return
    await session.message_dialog(body, title="Channels")


# --- validators --------------------------------------------------------------


def _nonblank(text: str) -> bool | str:
    """Require a non-empty name."""
    return bool(text.strip()) or "Enter a channel name."


def _valid_secret(text: str) -> bool | str:
    """Require a valid 16-byte hex key."""
    try:
        normalize_secret(text)
        return True
    except ValueError as exc:
        return str(exc)


def _optional_secret(text: str) -> bool | str:
    """Accept a blank key (derive from name) or a valid 16-byte hex key."""
    return True if not text.strip() else _valid_secret(text)


def _valid_link(text: str) -> bool | str:
    """Require a parseable ``meshcore://channel/add`` link."""
    return parse_share_url(text) is not None or "Not a valid meshcore:// channel link."
