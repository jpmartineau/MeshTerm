# SPDX-License-Identifier: Apache-2.0
"""The Contacts sweep: rank the full contact table, and archive the weakest off the device.

The contact table of a companion has a fixed size. A busy mesh fills it with the adverts
that arrive, until the table has no space left to discover a new node. Most of these
adverts come from nodes that were heard only one time, in passing, four hops away. This
flow gets that free space back, in three steps over the pushed Contacts list:

1. **Select a target**, from a ladder in two sections. The "By standing" rungs keep the
   strongest part of the table and archive the remainder. The "Long silent" rungs sweep by
   last-heard age. That is the plain, predictable operation that a ranking cannot express
   ("everything I haven't heard in a year"). Each rung of the two types shows the real
   number of contacts that it archives. MeshTerm calculates this number from the actual
   ranking, not from arithmetic on the table size, because a protected contact is never a
   victim.
2. **Read the list, and edit it.** The list shows each contact that the sweep will take,
   weakest first. Lanes beside each contact show the evidence: how recently it was heard,
   what it sent, whether you ever sent a message to it, and how far away it is. The age
   rungs show this list too. An age threshold cannot check itself, and to see what the
   contacts that it caught did is exactly that check. ``Delete`` removes a row from the
   list. Thus the user can spare one contact, and does not have to cancel the full sweep
   and select a less aggressive rung. ``←→`` scroll a row that is too wide for the
   terminal. Enter on a contact opens its detail page, only to examine the contact. The
   page does not show its own verbs that manage contacts (refer to
   :func:`~meshterm.ui.node_detail_screen.open_node_detail`). A screen where the user
   chooses what to archive must not also offer an archive or a delete of one contact from
   inside its own list of candidates. The list ends with the Apply/Back pair that
   :func:`~meshterm.ui.menus.exit_rows` draws for staged changes, because this list is
   that type of choice: a choice with a cost on each side, not an exit.
3. **Confirm.** An amber Cancel/Archive dialog, not the red typed gate of a deletion,
   because this operation is reversible. Each contact keeps its key, its reception history,
   and its transcripts. The Archived list is one row away on the Contacts screen, and a
   restore is one write. We keep the red for what cannot be undone. That is why the red
   continues to have a meaning.

**The preview shows the evidence, never the verdict.** It does not show the score. A
weighted sum has no meaning without the distribution that it came from, and it needs a
legend each time that the weights change. It does not show the percentile either. The
percentile was the first lane of the row for one round. But a rank is the score's own
reading of itself, so a lane of percentiles repeated the order of the list and used five
cells to say "trust me". An audit needs the measurements, each in a lane of its own type.
Each lane that the Contacts list also has gets the same header as on that list. Thus a
column means one thing from top to bottom, and the user can check the sweep against what
they know, instead of against its arithmetic. Refer to :mod:`~meshterm.core.contact_score`
for the scoring itself. That module is pure, it is in core, and it still ranks the list.

**Archived, not deleted.** The sweep removes each contact from the device, which frees its
slot (that is the purpose of the sweep). It stores the contact in the cross-session store
with an archive stamp (refer to :meth:`~meshterm.core.contact_store.ContactStore.archive`).
Nothing else changes. The reception history of the node, its position on the map, its Time
Machine record, and its direct-message transcript are stored by node id, not by contact
row, so they stay as they are. The store keeps the full public key, so one write puts a
contact back. The chat screen already offers this write when the device rejects a send
because it does not hold the recipient
(:class:`~meshterm.core.connection.ContactNotOnDeviceError`). Thus, if you send a message
to an archived contact, MeshTerm restores the contact as part of the send, and the send
does not fail.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.text import Text

from ..core.connection import ContactNotOnDeviceError
from ..core.contact_score import (
    ScoredContact,
    rank_contacts,
    sweep_candidates,
)
from ..core.models import Contact
from .menus import Lane, column_header, fit_cells, section_heading
from .theme import name_style
from .tui import Choice, Separator
from .tui.screen import CANCEL
from .widgets import DEFAULT_GLYPH, NODE_GLYPHS, _recency_style, format_age

if TYPE_CHECKING:
    from ..context import AppContext

#: The rungs that keep the strongest contacts, as the target ladder offers them. Each value
#: is a percentage of the sweepable contacts (the unprotected contacts), not of all the
#: contacts. We use percentages instead of counts, because only a percentage has the same
#: meaning on a table of thirty and on a table of three hundred. The ranking below it is a
#: rank, so a count would need a new value for each mesh, but a percentage never does. The
#: steps are large at the aggressive end: from 90% to 75% is a small clean-up, but from 50%
#: to 25% is a decision.
KEEP_RUNGS: tuple[int, ...] = (90, 75, 60, 50, 25)

#: The sentinel values of the two last rows of the preview. No :class:`ScoredContact` is
#: equal to them.
_APPLY = ("apply",)
_BACK = ("back",)

#: One day in seconds, for the age rungs.
_DAY = 86400

#: The last-heard rungs, as ``(label, min_age_seconds)``. A contact matches when its age is
#: equal to that value or more. These rungs do not include a contact that was never heard.
#: Only :data:`_NEVER` sweeps such a contact, so that an age rung does not silently take a
#: contact that did not broadcast an advert yet. This is the same ladder that the age-only
#: sweep offered before this flow included it.
AGE_RUNGS: tuple[tuple[str, int], ...] = (
    ("Not heard in 1 week", 7 * _DAY),
    ("Not heard in 1 month", 30 * _DAY),
    ("Not heard in 3 months", 90 * _DAY),
    ("Not heard in 6 months", 180 * _DAY),
    ("Not heard in 1 year", 365 * _DAY),
)

#: The value of the age rung for the contacts that were never heard (the contacts with no
#: advert time at all).
_NEVER = -1

#: The gap between two lanes, in cells. This space keeps a right-aligned value apart from
#: the value before it. The declared width of a lane is the width of its content plus this
#: gap.
_GAP = 2

#: The width of the name lane in the preview, with its gap. It is wide enough for the
#: approximately 20 bytes of a MeshCore advert name. It is also narrow enough that the
#: evidence lanes stay readable at the 53 cells of the PicoCalc.
_NAME_W = 22

#: The type-mark lane before the name: the glyph of the node and a space. The Contacts list
#: starts each row in the same way (refer to :func:`~meshterm.ui.contactlist._lane`).
_MARK_W = 2


def _count_cell(tally: int | None) -> tuple[str, str]:
    """A tally lane's value: the number in muted style, or a faint ``—`` if there is none.

    This is exactly how the Contacts list reads ``count`` (refer to
    :func:`~meshterm.ui.contactlist._lane_cell`), with the same clamp, so a very large tally
    does not make a column wider. The user came here from that list, and must not have to
    learn a second format for the same fact.
    """
    if not tally:
        return "—", "faint"
    return f"{min(tally, 99999)}", "muted"


def _heard_cell(scored: ScoredContact) -> tuple[str, str]:
    """The last-heard lane: the column age in its recency heat, or a cold ``never``.

    The lane uses :func:`~meshterm.ui.widgets.format_age` and the heat that the Contacts list
    uses for its own ``HEARD`` lane. Thus one fact has one format, on each screen that shows
    it.
    """
    secs = age_seconds(scored)
    return format_age(secs), _recency_style(secs)


def _hops_cell(scored: ScoredContact) -> tuple[str, str]:
    """The distance lane: ``direct`` for a neighbour, or else the mean hop count of its packets."""
    hops = scored.signals.hops
    if hops is None:
        return "—", "faint"
    return ("direct" if hops < 0.5 else f"{hops:g}"), "muted"


@dataclass(frozen=True)
class _Evidence:
    """One evidence lane: the word above it, and how to read the value of a contact for it.

    This is a declaration, not a branch, the same as
    :class:`~meshterm.ui.contactlist.ContactLane` in the nearby module. The header builder,
    the row builder, and the width arithmetic all go through the same tuple. Thus you add a
    lane, or change the order of the lanes, in one place.

    Attributes:
        label: The header word. It is right-aligned above its lane, where its digits will be.
        read: The value of the contact for this lane, as ``(text, style)``.
    """

    label: str
    read: Callable[[ScoredContact], tuple[str, str]]


#: The evidence lanes of the preview, from left to right. ``HEARD`` and ``PKTS`` are first,
#: in the order of the Contacts list and drawn in its format. The user came from that list,
#: and these are the two lanes that the user already read there. ``MSGS`` and ``HOPS`` come
#: next. The sweep adds these two lanes, and they are almost always empty in a list of
#: candidates (a contact that you sent messages to is almost never weak enough for the
#: sweep). Thus a terminal that is too narrow for the full row cuts the least useful end
#: first, and ``←→`` brings it back.
_EVIDENCE: tuple[_Evidence, ...] = (
    _Evidence("HEARD", _heard_cell),
    _Evidence("PKTS", lambda scored: _count_cell(scored.signals.packets)),
    _Evidence("MSGS", lambda scored: _count_cell(scored.signals.dm_total)),
    _Evidence("HOPS", _hops_cell),
)


def _lane_widths(victims: list[ScoredContact]) -> tuple[int, ...]:
    """The width of each evidence lane: its header word, or the widest value below it.

    The function measures the widths from the list, not from fixed values. Thus a lane of
    single digits is one cell of digits, not five cells of empty space. The widths are
    measured again each time that a spared row leaves the list, and that can only make a
    lane narrower.
    """
    return tuple(
        max([cell_len(lane.label), *(cell_len(lane.read(v)[0]) for v in victims)])
        for lane in _EVIDENCE
    )


def _preview_header(widths: tuple[int, ...], width: int) -> str:
    """The preview's pinned column header: the name lane, then one word for each evidence lane.

    Each word is right-aligned in its lane, above values that are also right-aligned. Thus a
    header is where its digits will be, not to the left of them. The Contacts list puts the
    headers of the same two lanes in the same way.

    One word for each lane is the purpose of this layout. Before, one ``WHY IT RANKS LOW``
    header was above three lanes, and it could not name any of them. A lane held the *n*-th
    weakest reason of a contact. Thus a message count, an age, and a hop count moved between
    columns from row to row, and no column had the same meaning on two rows. Now each lane
    has one fixed type of value. That makes the lanes comparable from the top to the bottom
    of the list. It also lets the user see that the contact that they were not sure about is
    the contact that never transmitted a packet.
    """
    lanes = [Lane("NAME", _NAME_W)]
    last = len(_EVIDENCE) - 1
    for index, (lane, lane_w) in enumerate(zip(_EVIDENCE, widths, strict=True)):
        lanes.append(Lane(f"{lane.label:>{lane_w}}", 0 if index == last else lane_w + _GAP))
    # Start after the pointer and the type mark, so ``NAME`` is above the name, not the glyph.
    return column_header(lanes, width, indent=2 + _MARK_W)


def _victim_row(scored: ScoredContact, widths: tuple[int, ...]) -> Text:
    """One preview line: the contact's type mark and name, then its evidence, lane by lane.

    The row starts in the same way as a row of the Contacts list: first the shared type mark
    in its own colour (``▲`` repeater, ``■`` room server, ``◉`` sensor, ``●`` companion), then
    the name. Thus the contact looks the same here as on the list that the user came from.
    Also, a repeater that MeshTerm is about to archive shows that it is a repeater before the
    user reads its name.

    The name keeps the colour that comes from its key, the same as everywhere else in the
    app. This is a list of nodes. A user who looks for one node in thirty rows must not have
    to read each name letter by letter only because this screen is about archives. The name
    goes through :func:`~meshterm.ui.menus.fit_cells`. Thus a name that is too long ends with
    an ellipsis that shows that it was cut, the lane is measured in cells, and a name with an
    emoji is cut between glyphs, not through one glyph. A name is whatever the radio of a
    stranger broadcast in its advert. It is the only value on this screen that can be
    anything.

    Each other lane is right-aligned in its measured width and has its own style: the heard
    age has its recency heat, a tally is muted, and an absent value is a faint ``—``. The row
    starts as an unstyled :class:`~rich.text.Text`, not with the style of the name, because
    Rich merges a base style into each span. On a base style in the hue of a node, a faint
    ``—`` is faint, and in the colour of that node.
    """
    contact = scored.contact
    name = contact.name or contact.key_prefix or "?"
    mark, mark_style = NODE_GLYPHS.get(contact.node_type, DEFAULT_GLYPH)
    row = Text()
    row.append(mark, style=mark_style)
    row.append(" " * (_MARK_W - cell_len(mark)))
    row.append(
        fit_cells(name, _NAME_W - _GAP) + " " * _GAP,
        style=name_style(name, contact.public_key or contact.key_prefix),
    )
    last = len(_EVIDENCE) - 1
    for index, (lane, lane_w) in enumerate(zip(_EVIDENCE, widths, strict=True)):
        value, style = lane.read(scored)
        row.append(fit_cells(value, lane_w, align="right"), style=style)
        if index != last:
            row.append(" " * _GAP)
    return row


#: The headers of the two count lanes of the ladder. Each lane has the same width as its
#: word, and the count is right-aligned in it. Thus a number ends below the last letter of
#: its header.
_KEEPS = "KEEPS"
_ARCHIVES = "ARCHIVES"


def _ladder_header(label_w: int, width: int) -> str:
    """The pinned column header of the target ladder, above the rung lane and its two counts."""
    return column_header(
        [
            Lane("TARGET", label_w + 2),
            Lane(_KEEPS, len(_KEEPS) + 2),
            Lane(_ARCHIVES),
        ],
        width,
    )


def _rung_row(label: str, kept: int, archived: int, label_w: int) -> Text:
    """One rung: its label, then how many contacts it keeps and how many it archives.

    **The kept count is first**, not the archived count. The contact table of the device has
    limited space, and that limit is the only reason for a sweep. Thus the useful number is
    the number that must fit in the table. The number of archived contacts is the remainder
    of the arithmetic, and it comes second. A percentile cutoff was first here for one
    round, and it was the wrong unit for two reasons: it answered a question that nobody
    asked at this step, and it did not fit in 53 cells.

    The two counts are columns, not a phrase (``keeps 45 · archives 12``). Thus the ladder
    is a table from the top to the bottom. Each count is right-aligned below its own header.
    To compare two rungs, the user looks down one lane, and does not have to find the number
    in each sentence. A rung that archives no contacts shows its ``0`` muted. Thus the rungs
    that do something are easy to see among the rungs that do nothing.

    Args:
        label: The label of the rung.
        kept: The contacts that stay on the device, with the protected contacts.
        archived: The contacts that the rung archives.
        label_w: The width of the rung lane in cells: the widest label in the two sections.
    """
    row = Text(label)
    row.append(" " * (label_w - cell_len(label) + 2))
    row.append(f"{kept:>{len(_KEEPS)}}  ")
    row.append(f"{archived:>{len(_ARCHIVES)}}", style=None if archived else "muted")
    return row


async def _rank(ctx: AppContext, contacts: list[Contact], self_key: str) -> list[ScoredContact]:
    """Collect all the signals of these contacts, and rank the contacts, strongest first.

    The function does five grouped scans of the history
    (:meth:`~meshterm.persistence.repository.Repository.contact_signals`) and one pass over
    the channel transcript to attribute names. It also reads the three stores that hold the
    explicit protections: the contacts that you locked, the stars of the Watchtower, and the
    remembered admin logins. The position of our node comes from the self-info of the
    device. On a device that advertises no position, the position is absent, and the
    distance term reads it as unknown for all contacts.

    Args:
        ctx: The shared application context.
        contacts: The contacts of the device (our node is not one of them).
        self_key: The public key of the device (hex). The locks are stored for this key.

    Returns:
        The full ranking, strongest first.
    """
    from ..core.contact_score import node_id

    nodes = [node_id(c) for c in contacts]
    signals = ctx.repo.contact_signals(nodes)

    # MeshTerm attributes a channel post by the name of its sender, because a channel packet
    # has no sender key. Thus a name that two contacts share is attributed to neither of
    # them. If we counted it for both, one node would get the standing of another. If we
    # counted it for the first, the list order would decide. Both contacts stay
    # unattributed. The score reads that as unknown, not as silence, and the median fill
    # puts them where an average contact is.
    posts = ctx.repo.channel_post_counts()
    seen: dict[str, int] = {}
    for contact in contacts:
        folded = (contact.name or "").strip().casefold()
        if folded:
            seen[folded] = seen.get(folded, 0) + 1

    store = getattr(ctx, "contact_store", None)
    locked = store.locked_keys(self_key) if store is not None and self_key else frozenset()
    watch = getattr(ctx, "watch_store", None)
    admin = getattr(ctx, "admin_store", None)
    for contact, node in zip(contacts, nodes, strict=True):
        found = signals.get(node)
        if found is None:
            continue
        folded = (contact.name or "").strip().casefold()
        unique = bool(folded) and seen.get(folded) == 1
        signals[node] = replace(
            found,
            channel_posts=posts.get(folded, 0) if unique else 0,
            channel_attributed=unique,
            watched=bool(watch is not None and watch.is_watched(node)),
            has_admin=bool(admin is not None and admin.get(contact)),
            locked=(contact.public_key or "").lower().removeprefix("0x") in locked,
        )

    self_lat = self_lon = None
    try:
        info = await ctx.devstate.self_info()
        lat, lon = info.get("adv_lat"), info.get("adv_lon")
        # A device that never had a position set advertises 0/0. That is a point in the
        # Atlantic, not a real location. The node detail page reads it in the same way.
        if lat and lon:
            self_lat, self_lon = float(lat), float(lon)
    except Exception as exc:  # noqa: BLE001 - a device with no position loses only one weak term
        ctx.log.debug("archive: no self position (%s); distance term stays unknown", exc)

    return rank_contacts(contacts, signals, self_lat=self_lat, self_lon=self_lon)


async def archive_contacts(ctx: AppContext, self_key: str) -> int:
    """Run the full sweep: rank, select a target, preview, confirm, archive. Return the count.

    The sweep runs over the pushed Contacts list as its backdrop. Thus each step floats, and
    each Esc goes back one step. Each step back is a real step: Esc on the preview returns
    to the ladder, where the user can select a different rung. The flow does not stop, and
    the user does not have to start again.

    The ranking is calculated **one time**, before the ladder is drawn. All the rungs, of the
    two types, use it again. The ranking costs five scans of the history. Also, the count of
    a rung would be false if it came from a different pass than the preview that the rung
    opens.

    Args:
        ctx: The shared application context (interactive menu).
        self_key: The public key of the device (hex). The contact store keeps the
            remembered contacts of this device under this key.

    Returns:
        How many contacts were archived (``0`` if the user cancelled or no contact matched).
    """
    from .surface import TuiUi

    assert isinstance(ctx.ui, TuiUi)  # the Contacts screen makes sure of this
    session = ctx.ui.session

    async with ctx.ui.busy_overlay("Rating contacts"):
        contacts = await ctx.devstate.contacts()
        ranked = await _rank(ctx, contacts, self_key)

    sweepable = [scored for scored in ranked if not scored.protected]
    if not sweepable:
        ctx.ui.note("[muted]every contact is protected — nothing to sweep[/muted]")
        return 0

    while True:
        picked = await session.run_screen(_target_screen(ranked, sweepable))
        if picked is CANCEL or picked is None:
            return 0
        victims = victims_for(ranked, sweepable, picked)
        if not victims:
            ctx.ui.note("[muted]nothing matches that — no contacts to archive[/muted]")
            continue
        if await _preview(ctx, victims):
            return await _sweep(ctx, self_key, victims)


def age_seconds(scored: ScoredContact) -> float | None:
    """A scored contact's last-heard age in seconds, or ``None`` if it was never heard."""
    days = scored.signals.heard_age_days
    return None if days is None else days * _DAY


def victims_for(
    ranked: list[ScoredContact],
    sweepable: list[ScoredContact],
    picked: tuple,
) -> list[ScoredContact]:
    """The contacts that one rung archives, weakest first.

    The order is weakest first for **both** types of rung, also for the age rungs. The user
    reads the preview from the top down. A user who reads only its first screen must see the
    contacts that they are least likely to want back. They must not see the contacts that
    the age filter put first by chance.

    Args:
        ranked: The full ranking, strongest first.
        sweepable: The unprotected contacts of the ranking, in the same order.
        picked: The value of the rung: ``("keep", share)`` or ``("age", seconds)``.

    Returns:
        The victims, weakest first.
    """
    route, value = picked
    if route == "keep":
        # Round up: "keep the strongest 90%" of five contacts keeps five. You cannot keep
        # four and a half, and when you keep contacts, more is the safe direction. Python's
        # ``round`` is banker's rounding. On a table of five contacts, it silently made the
        # 90% and 75% rungs the same, because it changed 4.5 and 3.75 to the same 4.
        return sweep_candidates(ranked, keep=math.ceil(len(sweepable) * int(value) / 100))
    matched = []
    for scored in sweepable:
        age = age_seconds(scored)
        if value == _NEVER:
            if age is None:
                matched.append(scored)
        elif age is not None and age >= value:
            matched.append(scored)
    return list(reversed(matched))


def _target_screen(ranked: list[ScoredContact], sweepable: list[ScoredContact]):
    """Build the ladder of two sections. Each rung shows its own real archive count.

    The sections are grouped with :func:`~meshterm.ui.menus.section_heading`, not drawn as
    two lists. Thus the headings pin while the ladder scrolls, and the section jumps go from
    one heading to the next. Also, the two types of rung look like two ways to answer one
    question, not like two separate features that share a screen by chance.
    """
    from .tui import SelectScreen

    protected = len(ranked) - len(sweepable)
    sections: list[tuple[str, list[tuple[str, tuple]]]] = [
        (
            "By standing",
            [(f"Keep the strongest {share}%", ("keep", share)) for share in KEEP_RUNGS],
        ),
        (
            "Long silent",
            [(label, ("age", secs)) for label, secs in AGE_RUNGS]
            + [("Never heard at all", ("age", _NEVER))],
        ),
    ]
    # One rung lane for the two sections. Thus the counts are in two columns from the top to
    # the bottom of the ladder, and they do not start again below each heading.
    label_w = max(cell_len(label) for _, rungs in sections for label, _ in rungs)

    # Pinned like the header of the editors (menus.lane_header). Thus, when the ladder
    # scrolls, its column words stay at the top, with the heading of its current section.
    items: list = [Separator(lambda w: _ladder_header(label_w, w), pinned=True)]
    for heading, rungs in sections:
        items.append(section_heading(heading))
        for label, value in rungs:
            victims = victims_for(ranked, sweepable, value)
            # The contacts that stay on the device: all the protected contacts, and the
            # sweepable contacts that this rung does not take. We count them from the
            # ranking, not from the percentage of the rung, because a protected contact is
            # never a victim. Thus the two counts would not agree.
            kept = len(ranked) - len(victims)
            items.append(Choice(title=_rung_row(label, kept, len(victims), label_w), value=value))
    held = f" · {protected} protected" if protected else ""
    return SelectScreen(
        f"Archive contacts — {len(ranked)} known{held}",
        items,
        prompt=(
            "Archives the weakest off the device, keeping them here. Ranked on messages, "
            "how lately and often heard, hops and distance."
        ),
        footer_hint="↑↓ move · Enter select · Esc back",
        filterable=False,
    )


def _preview_items(victims: list[ScoredContact]) -> list:
    """The preview's rows: a pinned column header, one line for each victim, then Apply/Back.

    Each contact row is ``deletable``, so ``Delete`` removes it from the sweep. Each row also
    keeps its type mark and its **name** out of the ``←→`` scroll (``hscroll_from``). A
    narrow terminal cuts the evidence lanes. If the name moved away when the user scrolls to
    read those lanes, the row would lose the one thing that tells which contact it is.

    This function measures the lanes across all the victims. That is also why a spared row
    makes this function build all the rows again, instead of a removal of one row: the
    value that left may have set a width.
    """
    widths = _lane_widths(victims)
    return [
        Separator(lambda w: _preview_header(widths, w), pinned=True),
        *(
            Choice(
                title=_victim_row(v, widths),
                value=v,
                deletable=True,
                hscroll_from=_MARK_W + _NAME_W,
            )
            for v in victims
        ),
        # The same shape that ``exit_rows`` draws, in the words of this flow. Apply has no
        # keyboard key of its own, so it must have a visible partner row that tells the cost
        # of the other way out. This is not ``exit_rows`` itself, because these rows are not
        # staged changes to a set of values, and the pair must say what it archives.
        Separator(" "),
        Choice(
            title=Text.assemble(("✓ ", "ok"), f"Archive {_count_desc(len(victims))}"),
            value=_APPLY,
        ),
        Choice(title=Text.assemble(("✗ ", "err"), "Back — keep them all"), value=_BACK),
    ]


def _preview_screen(victims: list[ScoredContact]):
    """The preview screen for a victim list, with a size and words that agree with its rows."""
    from .tui import SelectScreen

    return SelectScreen(
        f"Archive contacts — {_count_desc(len(victims))} to archive",
        _preview_items(victims),
        prompt="Archived contacts leave the device but stay here, with their history.",
        # The base hint does not include the scroll atom or the keep atom. The select list
        # adds each atom only when its keyboard key can act: ``←→`` only while the
        # highlighted row is too wide, and ``Del keep`` only on a row that the user can
        # spare. All the footer obeys this same rule ("advertise a key only where it
        # acts"). The rule also keeps all three atoms in 72 cells at the same time.
        footer_hint="↑↓ move · type to filter · Enter select · Esc back",
        delete_hint="Del keep",
        default=_APPLY,
        hscroll=True,
        hscroll_hint="←→ scroll",
    )


async def _preview(ctx: AppContext, victims: list[ScoredContact]) -> bool:
    """Show exactly which contacts will go, weakest first. Return whether the user agreed.

    This is the audit step, and the reason why it is safe to offer the sweep. If MeshTerm
    archives several dozen contacts because of a number that nobody can check, the user must
    examine that operation one time first. The list has a filter, like all other lists. A
    user who wants to know whether one specific node is in it must be able to type its name.

    The list is also **editable**. ``Delete`` removes a contact from the sweep. The
    alternative was to go back and select a less aggressive rung. That spares the one contact
    that you recognized, but it also spares forty contacts that were not important to you.
    The list of rows is data here, so it refreshes through
    :meth:`~meshterm.ui.tui.select.SelectScreen.replace_items`: the highlight follows its
    value, and the typed filter stays. The screen is not built again, because that would put
    the user back at the top of a list that they were halfway down.

    Args:
        ctx: The shared application context.
        victims: The candidates, weakest first. This function **changes** the list when the
            user removes rows, so the list of the caller is the list that the sweep archives.

    Returns:
        Whether the user selected the archive of the contacts that remain after the edits.
    """
    from .node_detail_screen import open_node_detail
    from .tui.select import DeleteRequest

    screen = _preview_screen(victims)
    # A hub: it owns a loop, so it is pushed one time and stays for the full visit. That is
    # why a spared row can refresh the rows below the user's highlight. It is also why a
    # detail page can nest above this screen, and this screen keeps its place.
    async with ctx.ui.session.stay(screen) as visit:
        while True:
            chosen = await visit.result()
            if chosen is CANCEL or chosen is None or chosen == _BACK:
                return False
            if chosen == _APPLY:
                return bool(victims)
            if isinstance(chosen, DeleteRequest):
                spared = chosen.value
                if spared in victims:
                    victims.remove(spared)
                if not victims:
                    # The user spared all the candidates. Thus there is no sweep left to
                    # preview. An empty list with an "Archive no contacts" row would be a
                    # screen that asks the user to confirm nothing.
                    return False
                # The title shows the count of victims, so it is content too, and it
                # changes with them.
                screen.replace_items(
                    _preview_items(victims),
                    title=f"Archive contacts — {_count_desc(len(victims))} to archive",
                )
                continue
            # Each other row is a contact. Enter opens its node detail page, so a user who
            # does not recognize a name can examine the node before MeshTerm archives it. The
            # page opens with ``manage=False``, because this screen is already the place where
            # the user decides what to archive. A second archive or delete of one contact,
            # from inside the list of candidates, would give two answers to one question. The
            # preview stays pushed below, so Esc on the page goes back to the row that opened
            # it.
            await open_node_detail(ctx, chosen.contact, manage=False)


def _count_desc(n: int) -> str:
    """``no contacts`` / ``1 contact`` / ``n contacts``: the phrase that the sweep counts with."""
    if n == 0:
        return "no contacts"
    return f"{n} contact{'' if n == 1 else 's'}"


async def _sweep(ctx: AppContext, self_key: str, victims: list[ScoredContact]) -> int:
    """Confirm, then remove each victim from the device and archive it here. Return the count.

    The confirm is **amber, and the user types nothing**. A bulk deletion has a red typed
    gate. The two tiers are for two different costs, and this cost can be recovered: each
    contact keeps its key, its reception history, and its transcripts, and a restore is one
    write from the Archived list. A red typed confirm here would use the strongest warning of
    the app on its most reversible bulk action. Then a warning has no meaning.

    The removal is one command for each contact, local to the companion, with no LoRa
    traffic. Thus it runs from start to end under the progress dialog, and it is not paced.
    The store write comes after each successful removal, not in one batch at the end. Thus,
    if the sweep is interrupted, the state stays consistent: each contact that is off the
    device is stored as archived.
    """
    if not await ctx.ui.dialog(
        f"Archive {_count_desc(len(victims))}? They come off this device's contact list, "
        "freeing space for new ones. MeshTerm keeps them — with their keys, reception "
        "history, and messages — under Archived contacts, and any of them can be restored.",
        [("Cancel", False), ("Archive", True)],
        title="Archive contacts",
        default=1,
        danger=True,
    ):
        return 0

    device = await ctx.device()
    dev_pub = (self_key or "").lower().removeprefix("0x")
    store = ctx.contact_store
    stamp = int(time.time())
    archived = failed = 0
    with ctx.ui.progress("Archive contacts") as progress:
        task = progress.add_task("Archiving", total=len(victims))
        for scored in victims:
            contact = scored.contact
            try:
                await device.remove_contact(contact)
            except ContactNotOnDeviceError:
                # The contact is already absent from the device. Thus the work of the sweep
                # on the device is done, and the archive here is what removes the row. This
                # is not a failure.
                ctx.log.debug("archive: %s was not on the device; archiving ours", contact.name)
            except Exception as exc:  # noqa: BLE001 - one bad removal must not stop the sweep
                failed += 1
                ctx.log.debug("archive: could not remove %s: %s", contact.name, exc)
                continue
            finally:
                progress.advance(task)
            if store is not None and dev_pub:
                store.archive(dev_pub, contact, when=stamp)
            archived += 1
    ctx.devstate.invalidate_contacts()

    if archived and not failed:
        outcome = Text(f"✓ archived {_count_desc(archived)}", style="ok")
    elif archived:
        outcome = Text(
            f"archived {_count_desc(archived)}; {failed} could not be removed", style="warn"
        )
    else:
        outcome = Text("no contacts could be archived", style="err")
    await ctx.ui.session.message_dialog(outcome, title="Archive contacts")
    return archived
