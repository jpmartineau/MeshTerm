# SPDX-License-Identifier: Apache-2.0
"""The channel guide screen: the public channels heard on the mesh, and an add for each.

The guide opens from the "Add a channel" section of the Channels page. It lists each public
channel that MeshTerm heard (refer to :mod:`meshterm.core.channel_guide`), in two sections.
The channels that are not on the device come first, because the user can add them: Enter
adds the highlighted channel to the next free slot at once, as the other add rows do. The
channels that the device already has come after them, each with a ``✓`` in the pointer
column. The highlight does not walk these rows, so the list is complete and Enter never does
nothing. A muted line at the end counts the messages that no name matched.

Each row shows the name, the number of different messages, the age of the last message, and
the activity of the last seven days. The chart is the last lane, and it becomes shorter to
fit the row, as on the Channels page.

The match of the stored packets takes a moment on a large history (one second on a desktop
for 31,000 messages, more on a handheld). Thus it runs in a worker thread under a busy card,
and the result stays for the session while the history and the candidate names are the same.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.text import Text

from ..core.channel_guide import GUIDE_BUCKETS, Guide, HeardChannel, build_guide, mention_names
from ..core.channel_probe import ChannelSlot
from .braillechart import activity_peak, activity_sparkline
from .channels import _pick_free_slot, write_channel
from .menus import Lane, column_header, fit_cells, section_heading
from .tui import CANCEL, Choice, SelectScreen, Separator
from .widgets import age_seconds, channel_glyph, format_age

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.connection import Device

#: The footer of the guide: navigation, the filter, the action, and Esc last.
_GUIDE_HINT = "↑↓ move · type to filter · Enter add · Esc back"

#: The widest name lane. A longer name ends in an ellipsis, as on the Channels page.
_NAME_WIDTH_MAX = 20
#: The width of the message-count lane, which is aligned to the right.
_COUNT_WIDTH = 5
#: The width of the age lane, which is aligned to the right. It fits ``never``.
_AGE_WIDTH = 5
#: The cells of the activity chart at full width: two buckets of six hours for each cell.
_CHART_CELLS = GUIDE_BUCKETS // 2
#: The lower limit of the shared chart peak, so that one message stays a small mark.
_ACTIVITY_FLOOR = 2.0

#: The last guide that was built, and what it was built from. Refer to :func:`read_guide`.
_cache: tuple[tuple, Guide] | None = None


async def channel_guide(ctx: AppContext, device: Device, capacity: int) -> int:
    """Show the guide until the user leaves it. Return the number of channels added.

    Args:
        ctx: The shared application context.
        device: The connected device, to add a channel to.
        capacity: The number of channel slots of the device.

    Returns:
        The number of channels that the user added from the guide.
    """
    async with ctx.ui.busy_dialog("reading the channels heard…", title="Channel guide"):
        slots = list(await ctx.devstate.channel_slots())
        guide = await read_guide(ctx, slots)
    by_identity = {channel.identity: channel for channel in guide.channels}
    added = 0

    async def add(choice: object) -> bool:
        """Add the selected channel. Return whether the device changed."""
        nonlocal slots, added
        channel = by_identity.get(choice) if isinstance(choice, str) else None
        if channel is None:
            return False
        idx = await _pick_free_slot(ctx, device, slots, capacity)
        if idx is None:
            return False
        await write_channel(ctx, device, idx, channel.name, channel.write_secret)
        slots = list(await ctx.devstate.channel_slots())
        added += 1
        return True

    title, items = guide_items(guide, slots)
    session = getattr(ctx.ui, "session", None)
    if session is None:  # a UI with no stack of screens: one round at a time
        while True:
            choice = await ctx.ui.select(title, items)
            if choice is None:
                return added
            if await add(choice):
                title, items = guide_items(guide, slots)
    screen = SelectScreen(title, items, footer_hint=_GUIDE_HINT)
    async with session.stay(screen) as visit:
        while True:
            choice = await visit.result()
            if choice is CANCEL:
                return added
            if await add(choice):
                # The rows are data, so the list changes in place: the added channel moves
                # to the second section, and the filter stays.
                title, items = guide_items(guide, slots)
                screen.replace_items(items, title=title)


async def read_guide(ctx: AppContext, slots: list[ChannelSlot]) -> Guide:
    """Build the guide from the stored packets, or return the guide that is still correct.

    The two reads of the database run here, on the thread of the database. Only the match,
    which reads no database, goes to a worker thread. The guide stays for the session while
    its inputs are the same: the same number of stored messages with the same latest time,
    the same candidate names, and the same channels on the device.
    """
    global _cache
    packets = ctx.repo.channel_packets()
    regions = ctx.region_store.names() if ctx.region_store is not None else []
    guesses = mention_names(ctx.repo.message_texts()) | {f"#{name}" for name in regions}
    known = [(slot.name, slot.secret) for slot in slots]
    latest = max((row[4] or "" for row in packets), default="")
    key = (len(packets), latest, frozenset(guesses), tuple(slot.identity for slot in slots))
    if _cache is not None and _cache[0] == key:
        return _cache[1]
    now = datetime.now(timezone.utc)
    guide = await asyncio.to_thread(build_guide, packets, known, guesses, now=now)
    _cache = (key, guide)
    return guide


def guide_items(guide: Guide, slots: list[ChannelSlot]) -> tuple[str, list]:
    """The title and the rows of the guide, for the channels that are on the device now.

    Args:
        guide: What the guide found.
        slots: The channels of the device.

    Returns:
        ``(title, items)`` for a :class:`SelectScreen`.
    """
    on_device = {slot.identity for slot in slots}
    absent = [c for c in guide.channels if c.identity not in on_device]
    present = [c for c in guide.channels if c.identity in on_device]
    title = f"Channel guide · {len(guide.channels)} heard"
    items: list = []
    if not guide.channels:
        items.append(Separator("  no public channel heard yet — keep MeshTerm listening"))
    else:
        name_w = min(
            _NAME_WIDTH_MAX, max(len("CHANNEL"), *(cell_len(c.name) for c in guide.channels))
        )
        peak = activity_peak(*(c.histogram for c in guide.channels), floor=_ACTIVITY_FLOOR)
        items.append(Separator(lambda w: _header(name_w, w), heading=True))
        items.append(section_heading("Not on this device"))
        if not absent:
            items.append(Separator("  each public channel heard is on this device"))
        for channel in absent:
            row = _row(channel, name_w, peak)
            items.append(Choice(title=row, value=channel.identity, fitted=True))
        if present:
            items.append(section_heading("On this device"))
            for channel in present:
                items.append(Separator(_row(channel, name_w, peak, mark="✓")))
    if guide.unnamed:
        count = f"{guide.unnamed} message" if guide.unnamed == 1 else f"{guide.unnamed} messages"
        items.append(Separator(" "))
        items.append(Separator(f"  {count} on channels with no known name"))
    return title, items


def _header(name_w: int, width: int) -> str:
    """The column headers over the lanes of the guide (refer to :func:`_row_text`)."""
    return column_header(
        [
            Lane("CHANNEL", cell_len(channel_glyph("#x", None)) + 1 + name_w + 2),
            Lane(f"{'MSGS':>{_COUNT_WIDTH}}", _COUNT_WIDTH + 2),
            Lane(f"{'LAST':>{_AGE_WIDTH}}", _AGE_WIDTH + 2),
            Lane(("ACTIVITY", "ACT", "")),  # it goes with its chart
        ],
        width,
    )


def _row(
    channel: HeardChannel, name_w: int, peak: float, *, mark: str = ""
) -> Callable[[int], Text]:
    """A row title that the screen renders again at each paint, at the width of the row."""
    return lambda width: _row_text(channel, name_w, peak, width, mark=mark)


def _row_text(channel: HeardChannel, name_w: int, peak: float, width: int, *, mark: str) -> Text:
    """Build one row of the guide, ``width`` cells at most.

    A row that the highlight walks starts at the name, after the pointer column that the
    screen draws. A row of a channel that is on the device draws that column itself: two
    cells, with ``mark`` (``✓``) in the first one. Thus the two kinds of row align.
    """
    text = Text(no_wrap=True, overflow="ellipsis")
    if mark:
        text.append(mark, style="ok")
        text.append(" " * (2 - cell_len(mark)))
    text.append(f"{channel_glyph(channel.name, channel.key)} ")
    text.append(fit_cells(channel.name, name_w), style="muted" if mark else "")
    text.append("  ")
    text.append(f"{min(channel.messages, 99999):>{_COUNT_WIDTH}}")
    text.append("  ")
    age = format_age(age_seconds(channel.last_heard)) if channel.last_heard else "never"
    text.append(f"{age:>{_AGE_WIDTH}}", style="muted")
    cells = min(_CHART_CELLS, width - text.cell_len - 2)
    if cells > 0:
        text.append("  ")
        text.append_text(activity_sparkline(channel.histogram, 2 * cells, peak=peak))
    return text
