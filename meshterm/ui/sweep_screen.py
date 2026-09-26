# SPDX-License-Identifier: Apache-2.0
"""The Contacts sweep: rank the whole contact table, archive the weakest off the device.

A companion's contact table is finite and a busy mesh fills it with whatever adverts
arrived — mostly nodes heard once, in passing, four hops out — until there is no room left
to discover anyone new. This is the flow that gets that headroom back, in three steps over
the pushed Contacts list:

1. **Pick a target**, from a ladder in two sections. *By standing* rungs keep the
   strongest share of the table and archive the rest; *long silent* rungs sweep by
   last-heard age — the plain, predictable operation a ranking can't express ("everything I
   haven't heard in a year"). Both carry the real number of contacts they would archive,
   computed from the actual ranking rather than from arithmetic on the table size, because
   protected contacts are never victims.
2. **Read the list, and edit it.** Every contact the sweep would take, weakest first, with
   the evidence beside it in lanes — how lately it was heard, what it has sent, whether you
   ever messaged it, how far out it sits. For the age rungs too, since seeing what the
   contacts an age threshold caught have actually *done* is exactly the check that threshold
   cannot do for itself. ``Delete`` drops a row from the list, so a
   contact the reader wants to keep is spared without abandoning the whole sweep and picking
   a shallower rung; ``←→`` scroll a row too wide for the terminal. Enter on a contact opens
   its detail page to *look* at, with the page's own contact-management verbs withheld (see
   :func:`~meshterm.ui.node_detail_screen.open_node_detail`) — a screen for choosing what to
   archive should not also hand out a singular archive or delete from inside its own
   candidate list. It ends in the Apply/Back pair that
   :func:`~meshterm.ui.menus.exit_rows` draws for staged changes, because that is what this
   is: a choice with a cost on both sides, not an exit.
3. **Confirm.** An amber Cancel/Archive dialog, not the red typed gate a deletion gets: this
   is reversible. Every contact keeps its key, its reception history and its transcripts,
   the Archived list is one row away on the Contacts screen, and a restore is a single
   write. Saving the red for what cannot be undone is what keeps the red meaning anything.

**The preview shows the evidence, never the verdict.** Not the score — a weighted sum means
nothing without the distribution it came from, and would need a legend the moment the weights
were retuned. Not the percentile either, which led the row for one round: a rank is the
score's own reading of itself, so a lane of them restated the order the list was already in
and spent five columns saying "trust me". What an audit needs is the measurements, each in a
lane of its own kind — headed like the Contacts list heads the two they share — so a column
means one thing all the way down and the reader can check the sweep against what they know
rather than against its arithmetic. See :mod:`~meshterm.core.contact_score` for the scoring
itself, which is pure, lives in core, and still ranks the list.

**Archived, not deleted.** The sweep removes each contact from the *device* — freeing the
slot, which is the whole point — and records it in the cross-session store with an archive
stamp (see :meth:`~meshterm.core.contact_store.ContactStore.archive`). Nothing else moves:
the node's reception history, its position on the map, its Time Machine record and its
direct-message transcript are all keyed by node id rather than by a contact row and are
untouched. Because the store keeps the full public key, one write puts a contact back —
which is exactly what the chat screen already offers when a send is rejected for a
recipient the device no longer holds
(:class:`~meshterm.core.connection.ContactNotOnDeviceError`), so an archived contact you
message anyway is restored in passing rather than being a dead end.
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

#: The keep-the-strongest rungs the target picker offers, as a share of the *sweepable*
#: contacts. Percentages rather than absolute counts because a share is the one form the
#: ladder can state once and have mean the same thing on a table of thirty and a table of
#: three hundred — the ranking underneath is a rank, so a count would have to be retuned
#: for every mesh and a share never does. Coarse at the aggressive end — the difference
#: between keeping 90% and 75% is a tidy-up, between 50% and 25% a decision.
KEEP_RUNGS: tuple[int, ...] = (90, 75, 60, 50, 25)

#: The sweep's own tail sentinels, distinct from any :class:`ScoredContact`.
_APPLY = ("apply",)
_BACK = ("back",)

#: A day in seconds, for the age ladder.
_DAY = 86400

#: The last-heard rungs, ``(label, min_age_seconds)``. A contact qualifies when its age is
#: *at least* that long; a never-heard contact is excluded here and swept only by
#: :data:`_NEVER`, so an age rung can't quietly take a contact that simply hasn't adverted
#: yet. The same ladder the age-only sweep offered before this flow absorbed it.
AGE_RUNGS: tuple[tuple[str, int], ...] = (
    ("Not heard in 1 week", 7 * _DAY),
    ("Not heard in 1 month", 30 * _DAY),
    ("Not heard in 3 months", 90 * _DAY),
    ("Not heard in 6 months", 180 * _DAY),
    ("Not heard in 1 year", 365 * _DAY),
)

#: The age-rung value meaning the never-heard bucket (contacts with no advert time at all).
_NEVER = -1

#: The gap between two lanes, in cells — the air that keeps a right-aligned value clear of
#: the one before it, and what a lane's declared width carries on top of its content.
_GAP = 2

#: Name lane width in the preview, plus its gap. Wide enough for the 20-odd bytes a
#: MeshCore advert name runs to, and narrow enough to leave the evidence lanes readable at
#: the PicoCalc's 53 columns.
_NAME_W = 22

#: The type-mark lane ahead of the name: the node's glyph and its space, which is how the
#: Contacts list leads every row (see :func:`~meshterm.ui.contactlist._lane`).
_MARK_W = 2


def _count_cell(tally: int | None) -> tuple[str, str]:
    """A tally lane's value: the number muted, or a faint ``—`` where there is nothing.

    The Contacts list's ``count`` reading exactly (see
    :func:`~meshterm.ui.contactlist._lane_cell`), clamp included — a runaway tally widens no
    column — because the reader arrived here from that list and must not have to learn a
    second grammar for the same fact.
    """
    if not tally:
        return "—", "faint"
    return f"{min(tally, 99999)}", "muted"


def _heard_cell(scored: ScoredContact) -> tuple[str, str]:
    """The last-heard lane: the column age under recency heat, or a cold ``never``.

    :func:`~meshterm.ui.widgets.format_age` and the heat the Contacts list colours its own
    ``HEARD`` lane with — one grammar for one fact, whichever screen is asking it.
    """
    secs = age_seconds(scored)
    return format_age(secs), _recency_style(secs)


def _hops_cell(scored: ScoredContact) -> tuple[str, str]:
    """The distance lane: ``direct`` for a neighbour, else the mean hop count it arrives by."""
    hops = scored.signals.hops
    if hops is None:
        return "—", "faint"
    return ("direct" if hops < 0.5 else f"{hops:g}"), "muted"


@dataclass(frozen=True)
class _Evidence:
    """One evidence lane: the word over it, and how to read a contact's value for it.

    A declaration rather than a branch, like :class:`~meshterm.ui.contactlist.ContactLane`
    next door: the header builder, the row builder and the width arithmetic all walk the
    same tuple, so a lane is added — or reordered — in one place.

    Attributes:
        label: The header word. Right-aligned over its lane, where its digits will land.
        read: The contact's value for this lane, as ``(text, style)``.
    """

    label: str
    read: Callable[[ScoredContact], tuple[str, str]]


#: The preview's evidence lanes, left to right. ``HEARD`` and ``PKTS`` lead, in the Contacts
#: list's own order and drawn in its own grammar: the reader has just come from that list,
#: and these are the two lanes they were already reading. ``MSGS`` and ``HOPS`` follow — the
#: two the sweep adds, and the two nearly always empty in a list of candidates (a contact you
#: have messaged is rarely weak enough to be swept), so a terminal too narrow for the whole
#: row cuts the least informative end first and ``←→`` brings it back.
_EVIDENCE: tuple[_Evidence, ...] = (
    _Evidence("HEARD", _heard_cell),
    _Evidence("PKTS", lambda scored: _count_cell(scored.signals.packets)),
    _Evidence("MSGS", lambda scored: _count_cell(scored.signals.dm_total)),
    _Evidence("HOPS", _hops_cell),
)


def _lane_widths(victims: list[ScoredContact]) -> tuple[int, ...]:
    """Each evidence lane's width: its header word, or the widest value under it.

    Measured against the list rather than fixed, so a lane of single digits is one cell of
    numbers instead of five of air — and remeasured whenever a spared row leaves, which can
    only tighten it.
    """
    return tuple(
        max([cell_len(lane.label), *(cell_len(lane.read(v)[0]) for v in victims)])
        for lane in _EVIDENCE
    )


def _preview_header(widths: tuple[int, ...], width: int) -> str:
    """The preview's pinned column header: the name lane, then one word per evidence lane.

    Each word is right-aligned inside its own lane, over values right-aligned too, so a
    header sits where its digits will land rather than off to the left of them — how the
    Contacts list heads the same two lanes.

    One word per lane is the whole point of the layout. A single ``WHY IT RANKS LOW`` used to
    head three lanes at once, and could not name any of them: a lane held a contact's *n*-th
    weakest reason, so a message count, an age and a hop count moved between columns from row
    to row and nothing in a column meant the same thing twice. The lanes are fixed by *kind*
    now, which is what makes them comparable down the list — and what lets a reader see that
    the contact they were unsure about is the one that has never sent a packet.
    """
    lanes = [Lane("NAME", _NAME_W)]
    last = len(_EVIDENCE) - 1
    for index, (lane, lane_w) in enumerate(zip(_EVIDENCE, widths, strict=True)):
        lanes.append(Lane(f"{lane.label:>{lane_w}}", 0 if index == last else lane_w + _GAP))
    # Past the pointer and the type mark, so ``NAME`` sits over the name, not the glyph.
    return column_header(lanes, width, indent=2 + _MARK_W)


def _victim_row(scored: ScoredContact, widths: tuple[int, ...]) -> Text:
    """One preview line: the contact's type mark and name, then its evidence, lane by lane.

    The row leads the way the Contacts list's does — the shared type mark in its own colour
    (``▲`` repeater, ``■`` room, ``◉`` sensor, ``●`` plain node), then the name — so the
    contact reads as the same row here as on the list the reader came from, and a repeater
    about to be archived says so before its name is read.

    The name keeps its key-derived colour like everywhere else in the app — this is a list of
    nodes, and a reader picking one out of thirty rows should not have to read it letter by
    letter because the screen it is on happens to be about archiving things. It goes through
    :func:`~meshterm.ui.menus.fit_cells`, so an over-long name ends on an ellipsis that says
    it was shortened, the lane is measured in display cells, and a name carrying an emoji is
    cut between glyphs rather than through one — a name is whatever a stranger's radio
    advertised, and it is the one value on this screen that can be anything at all.

    Every other lane is right-aligned into its measured width and carries its own style: the
    heard age its recency heat, a tally muted, an absent one a faint ``—``. The row is built
    on an unstyled :class:`~rich.text.Text` rather than under the name's own style, because
    Rich merges a base style into every span — a faint ``—`` under a name-hue base would come
    out faint *in that node's colour*.
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


#: The ladder's two count lanes, headed by these words. Each lane is exactly as wide as its
#: word, and the count right-aligns in it, so a number ends under its header's last letter.
_KEEPS = "KEEPS"
_ARCHIVES = "ARCHIVES"


def _ladder_header(label_w: int, width: int) -> str:
    """The target ladder's pinned column header, over the rung lane and its two counts."""
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

    **Keeps leads**, not archives. The device's contact table is the scarce thing and the
    whole reason to sweep at all, so the useful number is the one that has to fit in it —
    how many are archived is the arithmetic left over, and it follows. A percentile cutoff
    led here for one round and was the wrong unit twice over: it answered a question nobody
    asked at this step, and at 53 columns it did not fit anyway.

    Both are columns rather than a phrase (``keeps 45 · archives 12``), so the ladder reads
    down as a table: every count sits right-aligned under its own header, and comparing two
    rungs is a glance down a lane rather than finding the number inside each sentence. A
    rung that archives nothing shows its ``0`` muted, so the rungs that do something stand
    out from the ones that don't.

    Args:
        label: The rung's own label.
        kept: Contacts left on the device, protected ones included.
        archived: Contacts the rung would archive.
        label_w: The rung lane's width in cells — the widest label across both sections.
    """
    row = Text(label)
    row.append(" " * (label_w - cell_len(label) + 2))
    row.append(f"{kept:>{len(_KEEPS)}}  ")
    row.append(f"{archived:>{len(_ARCHIVES)}}", style=None if archived else "muted")
    return row


async def _rank(ctx: AppContext, contacts: list[Contact], self_key: str) -> list[ScoredContact]:
    """Gather every signal these contacts have and rank them, strongest first.

    Five grouped scans of the history (:meth:`
    ~meshterm.persistence.repository.Repository.contact_signals`), one pass over the channel
    transcript for name attribution, and the three stores that carry the explicit protections
    — the contacts you locked, the Watchtower's stars and the remembered admin logins. Our
    own position comes from the device's self-info, and is simply absent on a device that
    advertises none, which the distance term reads as unknown for everybody.

    Args:
        ctx: Shared application context.
        contacts: The device's contacts (our own node is not among them).
        self_key: The device's own public key (hex), which scopes the locks.

    Returns:
        The full ranking, strongest first.
    """
    from ..core.contact_score import node_id

    nodes = [node_id(c) for c in contacts]
    signals = ctx.repo.contact_signals(nodes)

    # Channel attribution is by display name — a channel frame carries no sender key — so a
    # name two contacts share attributes to neither of them. Counting it for both would
    # award one node's standing to another; counting it for the first would pick by list
    # order. Both stay *unattributed*, which the score reads as unknown rather than as
    # silence, and the median fill puts them where an average contact sits.
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
        # A device that has never had a position set advertises 0/0, which is a point in the
        # Atlantic rather than a location — the same reading the node detail page takes.
        if lat and lon:
            self_lat, self_lon = float(lat), float(lon)
    except Exception as exc:  # noqa: BLE001 - an unplaced device just loses one weak term
        ctx.log.debug("archive: no self position (%s); distance term stays unknown", exc)

    return rank_contacts(contacts, signals, self_lat=self_lat, self_lon=self_lon)


async def archive_contacts(ctx: AppContext, self_key: str) -> int:
    """Run the whole sweep — rank, pick a target, preview, confirm, archive. Returns the count.

    Runs over the pushed Contacts list as its backdrop, so every step floats and Esc walks
    back out one frame at a time. Each step is a real step back: Esc from the preview
    returns to the ladder with the reader's rung still highlighted, rather than abandoning
    the flow and making them start over.

    The ranking is computed **once**, before the ladder is drawn, and reused for every rung
    and both routes — it is five scans of the history, and a rung's count would be a lie if
    it were measured against a different pass than the preview it opens.

    Args:
        ctx: Shared application context (interactive menu).
        self_key: The device's own public key (hex) — how the contact store scopes this
            device's remembered contacts.

    Returns:
        How many contacts were archived (``0`` if cancelled or nothing matched).
    """
    from .surface import TuiUi

    assert isinstance(ctx.ui, TuiUi)  # guaranteed by the Contacts screen
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
    """The contacts one rung would archive, weakest first.

    Weakest first on **both** routes, the age ones included: the preview is read top-down,
    and a reader who skims only its first screen should be seeing the contacts they are
    least likely to want back — not whichever ones the age filter happened to list first.

    Args:
        ranked: The full ranking, strongest first.
        sweepable: Its unprotected subset, in the same order.
        picked: The rung's value — ``("keep", share)`` or ``("age", seconds)``.

    Returns:
        The victims, weakest first.
    """
    route, value = picked
    if route == "keep":
        # Rounded *up*: "keep the strongest 90%" of five contacts keeps five, because you
        # cannot keep four and a half and the safe direction on a keep is more. Python's
        # ``round`` is banker's rounding, which quietly made the 90% and 75% rungs identical
        # on a five-contact table by sending 4.5 and 3.75 to the same 4.
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
    """Build the two-section ladder, every rung carrying its own real archive count.

    Grouped through :func:`~meshterm.ui.menus.section_heading` rather than drawn as two
    lists, so the headings pin as the ladder scrolls and the section jumps step by them —
    and so the two routes read as two ways of answering one question rather than as two
    separate features that happen to share a screen.
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
    # One rung lane across both sections, so the counts stand in two columns down the whole
    # ladder rather than restarting under each heading.
    label_w = max(cell_len(label) for _, rungs in sections for label, _ in rungs)

    # Pinned like the editors' header (menus.lane_header), so a scrolled ladder keeps its
    # column words overhead along with the section it is in.
    items: list = [Separator(lambda w: _ladder_header(label_w, w), pinned=True)]
    for heading, rungs in sections:
        items.append(section_heading(heading))
        for label, value in rungs:
            victims = victims_for(ranked, sweepable, value)
            # What remains on the device: every protected contact, plus the sweepable ones
            # this rung doesn't take. Counted from the ranking rather than from the rung's
            # own percentage, because a protection is never a victim and the two would
            # disagree.
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
    """The preview's rows: a pinned column header, one line per victim, then Apply/Back.

    Each contact row is ``deletable``, so ``Delete`` lifts it out of the sweep, and pins its
    type mark and **name** out of the ``←→`` scroll (``hscroll_from``): the evidence lanes
    are what a narrow terminal cuts, and sliding the name away to read them would cost the
    row the one thing that says which contact is being read.

    The lanes are measured here, across every victim, which is also why a spared row rebuilds
    the rows through this function rather than dropping one: the widths it leaves behind may
    have been set by the value that just left.
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
        # The same shape ``exit_rows`` draws, in this flow's own words: Apply has no key of
        # its own, so it needs a visible counterpart naming what the other way out costs.
        # Not ``exit_rows`` itself — these are not *staged changes* to a set of values, and
        # the pair should say what it actually archives.
        Separator(" "),
        Choice(
            title=Text.assemble(("✓ ", "ok"), f"Archive {_count_desc(len(victims))}"),
            value=_APPLY,
        ),
        Choice(title=Text.assemble(("✗ ", "err"), "Back — keep them all"), value=_BACK),
    ]


def _preview_screen(victims: list[ScoredContact]):
    """The preview screen over a victim list, sized and worded to what it currently holds."""
    from .tui import SelectScreen

    return SelectScreen(
        f"Archive contacts — {_count_desc(len(victims))} to archive",
        _preview_items(victims),
        prompt="Archived contacts leave the device but stay here, with their history.",
        # Neither the scroll nor the keep atom is written into the base hint: the select
        # list splices each in exactly when its key would act — ``←→`` only while the
        # highlighted row actually overflows, ``Del keep`` only on a row that can be spared
        # — which is the same "advertise a key only where it acts" rule the whole footer
        # follows, and what keeps all three atoms inside 72 cells at once.
        footer_hint="↑↓ move · type to filter · Enter select · Esc back",
        delete_hint="Del keep",
        default=_APPLY,
        hscroll=True,
        hscroll_hint="←→ scroll",
    )


async def _preview(ctx: AppContext, victims: list[ScoredContact]) -> bool:
    """Show exactly who would go, weakest first; return whether the reader committed.

    The audit step, and the reason the sweep is safe to offer at all: archiving several
    dozen contacts on a number nobody can check is precisely the operation that deserves to
    be looked at once. The list is filterable like any other — a reader who wants to know
    whether one particular node is in it should be able to type its name.

    It is also **editable**. ``Delete`` drops a contact out of the sweep, because the
    alternative was to back out and pick a shallower rung, which spares the one contact you
    recognised by also sparing forty you didn't care about. The row list is *data* here, so
    it refreshes through
    :meth:`~meshterm.ui.tui.select.SelectScreen.replace_items` — the highlight follows by
    value and the typed filter holds — rather than by rebuilding the screen and dropping the
    reader back at the top of a list they were halfway down.

    Args:
        ctx: Shared application context.
        victims: The candidates, weakest first. **Mutated** when the reader drops rows, so
            the caller's list is what actually gets swept.

    Returns:
        Whether the reader committed to archiving whatever survived their edits.
    """
    from .node_detail_screen import open_node_detail
    from .tui.select import DeleteRequest

    screen = _preview_screen(victims)
    # A hub: it owns a loop, so it is pushed once and stays for the whole visit. That is what
    # lets a spared row refresh the rows underneath the reader's cursor, and a detail page
    # nest above without this screen losing its place.
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
                    # The reader spared every candidate: there is no sweep left to preview,
                    # and an empty list under an "Archive no contacts" row would be a screen
                    # asking them to confirm nothing.
                    return False
                # The title counts the victims, so it is content too and moves with them.
                screen.replace_items(
                    _preview_items(victims),
                    title=f"Archive contacts — {_count_desc(len(victims))} to archive",
                )
                continue
            # Any other row is a contact: Enter opens its node detail page, so a reader who
            # doesn't recognise a name can go and look before archiving it. The page is
            # opened with ``manage=False`` — this screen is already where archiving is being
            # decided, and a second, singular archive or delete reachable from inside its own
            # candidate list would be two answers to one question. The preview stays pushed
            # underneath, so Esc from the page lands back on the row it was opened from.
            await open_node_detail(ctx, chosen.contact, manage=False)


def _count_desc(n: int) -> str:
    """``no contacts`` / ``1 contact`` / ``n contacts`` — the sweep's counting phrase."""
    if n == 0:
        return "no contacts"
    return f"{n} contact{'' if n == 1 else 's'}"


async def _sweep(ctx: AppContext, self_key: str, victims: list[ScoredContact]) -> int:
    """Confirm, then remove each victim from the device and archive it here. Returns the count.

    The confirm is **amber and takes no typing**, unlike the red typed gate a bulk deletion
    wears. The two tiers exist for two different costs and this one is recoverable — every
    contact keeps its key, its reception history and its transcripts, and a restore is one
    write from the Archived list. A red typed confirm here would have spent the app's
    loudest warning on its most reversible bulk action, which is how a warning stops meaning
    anything.

    The removal is a companion-local command per contact — no LoRa traffic — so it runs
    straight through under the progress dialog rather than being paced. The store write
    follows each successful removal rather than being batched at the end, so an interrupted
    sweep leaves a consistent state: every contact off the device is recorded as archived.
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
                # Already absent from the radio: the sweep's device-side work is done, and
                # archiving it here is what makes the row go away. Not a failure.
                ctx.log.debug("archive: %s was not on the device; archiving ours", contact.name)
            except Exception as exc:  # noqa: BLE001 - one bad removal mustn't abort the sweep
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
