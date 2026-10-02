# SPDX-License-Identifier: Apache-2.0
"""The PicoCalc F-key lane: a fixed footer row of five coloured chips, F1-F5.

The PicoCalc keyboard has five dedicated function keys; its MCU translates Shift+F1..F5
into plain F6-F10 keycodes (measured in P0), so "Shift+key = the opposite action" is
literal hardware behaviour and the app simply binds all ten.

By JP's spec (2026-08-01): a lane slot is for functionality that would otherwise be
*hard to reach* — not a shortcut to a key that is already close at hand. Enter and Esc
sit right on the keyboard and are already the easiest keys to hit, so they never occupy
a slot. Paging has no physical key at all, so a screen that scrolls earns it the prime
F4/F5 pair. Ctrl+letter chords (Ctrl+P for message paths, Ctrl+arrows for a sort column,
…) are fiddly to hold on this keyboard, so a screen promotes its own onto the free
F1-F3 bank. The five *plain* keys carry a screen's most-used, otherwise-awkward actions
— reachable with no Shift at all — and the Shift bank (F6-F10) holds each slot's
*opposite number*, not unrelated overflow. A lane can, and does, differ from screen to
screen.

**Second-grade keys ride their own pair's Shift half** (JP, 2026-08-08). Home and End
*are* on this keyboard, so jumping to either end of a body was never the unreachable
thing paging is — it only ever wanted a chip for consistency with the pager it belongs
to. So it rides that pager: Shift+F5 (``F10``) is Top because F5 is Page ↑, Shift+F4
(``F9``) is Bottom because F4 is Page ↓. One axis, one pair of slots, the jump sitting
behind the page on the very same key. That leaves **F1-F3 free on every screen** for the
verbs a screen actually has to offer, which is where the interesting work goes.

**A chip names an action, never a key** (JP, 2026-08-07). ``PgUp``, ``End`` and their
kin are the names of keys this keyboard doesn't even have; what the slot is for is the
thing it does *here* — ``Page ↑`` through a body, ``Latest`` on a transcript, ``Reset``
on the map, whose Home key refits the view and whose paging keys zoom. A screen that
repurposes a shared action relabels it; a screen the action means nothing on leaves the
slot **empty** rather than filling the lane out. Empty and dim are different claims:
empty says *not a thing here* (Retry in a channel, where nothing is ever acknowledged),
dim says *a thing here, just not right now* (Retry in a direct chat with everything
delivered).

A screen describes its lane as five :class:`FPair` slots (``None`` = unassigned); the
session resolves a pressed F-key to the slot's action string and dispatches it through
the normal action funnel, so screens gain F-key support without any key handling of
their own. An :class:`FPair` need not carry a Shift-bank action at all — a "lone" slot
(``opp_label=""``) simply renders blank in the shifted bank and F(n+5) does nothing.

**A slot never advertises a key that would do nothing.** The lane is the PicoCalc's whole
footer — it stands in for the hint line the other platform draws, and that line has always
dropped an atom whose key is inert (see :attr:`~meshterm.ui.tui.screen.Screen.content_overflows`).
So a screen rebuilds its lane each paint and clears :attr:`FPair.enabled` /
:attr:`FPair.opp_enabled` on whatever is out of reach right now: the chat's *Paths* with
no message picked, its *Retry* with nothing unacknowledged, the shared nav slots on a
screen with nothing to move (:func:`default_lane`). A dimmed slot keeps its label — muted,
unfilled, like an unassigned slot — so the key still reads as *what it would do*, it just
plainly isn't live. The dimming is presentational: the screen's own ``handle`` stays the
authority on what an action does (it no-ops), so a lane built from one-paint-stale metrics
can never swallow a key that would have worked.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from rich.text import Text

#: Every chip is exactly this many cells: a 2-cell key label, a space, a 6-cell
#: description. Five chips plus four 2-cell gaps sum to exactly 53 — the PicoCalc's
#: readable width — with nothing left over.
_CHIP_WIDTH = 9
_DESC_WIDTH = 6
_GAP = "  "


@dataclass(frozen=True, slots=True)
class FPair:
    """One F-key slot: the plain-key action and its optional Shift-bank companion.

    Attributes:
        label: Verb-phrase hint for the plain key, e.g. ``"Zoom +"`` — what the key
            *does* on this screen, never the key's own name (``"PgUp"``). Clipped/padded
            to 6 cells.
        action: The screen action the plain key dispatches (``"home"``, ``"enter"``, …).
        opp_label: Hint for the Shift-bank companion, or ``""`` for a lone slot with no
            companion — the shifted bank then renders that chip blank and the
            corresponding F(n+5) does nothing.
        opp_action: The action the Shift-bank companion dispatches, or ``""``.
        enabled: Whether the plain key's action is available *right now*. ``False`` draws
            the chip dimmed — label kept, fill dropped — rather than pretending (see the
            module docstring).
        opp_enabled: The same, for the Shift-bank companion.
    """

    label: str
    action: str
    opp_label: str = ""
    opp_action: str = ""
    enabled: bool = True
    opp_enabled: bool = True


#: A lane is five slots, F1..F5 left to right; ``None`` leaves a slot unassigned.
Lane = Sequence[FPair | None]

#: The default lane, in the vocabulary of a screen that is one scrolling body: move
#: through it a page at a time (F4/F5), or — behind Shift, on those same two keys — jump
#: to the end each pager heads for (F9/F10). No Enter or Esc slot: both keys sit right on
#: the keyboard already. F1-F3 stay free for the screen's own verbs; a screen that has
#: none leaves them blank rather than filling the lane out. A screen whose body is not one
#: scrolling column says so in its own words (see :class:`ChatScreen`, ``MapScreen``).
#:
#: **A directional pair rises toward its outer key** (JP, 2026-08-08): wherever two
#: adjacent chips are opposite ends of one axis, the *up · in · more* end takes the slot
#: nearer the lane's own edge. On the right-hand F4/F5 pair that is F5 — the pager here,
#: the map's zoom, ``Zoom -`` then ``Zoom +``, a rocker — and on a left-hand F1/F2 pair it
#: is F1, so a select list reads ``Sect ↑`` then ``Sect ↓``: ergonomically the hand
#: anchors on the lane's edge and rocks inward, whichever side the pair lives on. A slot's
#: own Shift companion follows the same rule *along its slot*: the jump behind ``Page ↑``
#: is the one Page ↑ is heading for, so F10 is Top and F9 Bottom. A screen relabelling a
#: pair keeps its order (a chat's ``Latest``/``Oldest``).
DEFAULT_LANE: tuple[FPair | None, ...] = (
    None,
    None,
    None,
    FPair("Page ↓", "pagedown", "Bottom", "end"),
    FPair("Page ↑", "pageup", "Top", "home"),
)

#: The lane of a screen with no lane at all: a confirm dialog, a busy spinner, a text
#: prompt. Nothing there pages or jumps, so the shared pager is not dim-but-real, it is
#: simply *not a thing here* — the distinction the module docstring draws — and every slot
#: renders as a bare, unfilled key number.
EMPTY_LANE: tuple[FPair | None, ...] = (None, None, None, None, None)


def default_lane(*, nav: bool = True) -> tuple[FPair | None, ...]:
    """:data:`DEFAULT_LANE`, with its pager slots dimmed unless ``nav``.

    Both shared slots *move* something — the page, and the jump behind it — so on a screen
    with nothing to move (a body that fits its viewport, a transcript with no messages)
    both banks are inert and say so. Screens that repurpose the nav actions for something
    always live (the map's zoom) build from :data:`DEFAULT_LANE` instead, relabelling as
    they go.

    Args:
        nav: Whether the nav keys would do anything on this paint — typically
            :attr:`~meshterm.ui.tui.screen.Screen.content_overflows`.

    Returns:
        The five slots, ready for a screen to overwrite the ones it claims.
    """
    if nav:
        return DEFAULT_LANE
    return tuple(
        None if pair is None else replace(pair, enabled=False, opp_enabled=False)
        for pair in DEFAULT_LANE
    )


def action_for(lane: Lane, number: int) -> str | None:
    """The action F-key ``number`` (1-10) resolves to on this lane, or ``None``.

    F1-F5 take the slot's plain-key action, F6-F10 (the physical Shift bank) take its
    companion — or resolve to nothing when the slot has none (a lone slot, or an
    unassigned one). A *dimmed* slot still resolves: dimming is how the lane draws an
    action the screen would no-op anyway, and resolving it regardless keeps a lane built
    from stale metrics from ever swallowing a key that works (see the module docstring).
    """
    index = (number - 1) % 5
    if index >= len(lane) or lane[index] is None:
        return None
    pair = lane[index]
    if number > 5:
        return pair.opp_action or None
    return pair.action


def _chip(number: int, label: str) -> str:
    """The exact 9-cell chip body: ``F1 Zoom +`` (``10`` stands in for ``F10``)."""
    key = f"F{number}" if number < 10 else "10"
    return f"{key} {label[:_DESC_WIDTH]:<{_DESC_WIDTH}}"


def lane_text(lane: Lane, *, shifted: bool = False) -> Text:
    """Render the lane as five 9-cell chips: coloured fill, white text, 2-cell gaps.

    Args:
        lane: The five slots.
        shifted: Show the Shift-bank chips (F6-F10) in the green fill; the plain bank
            (F1-F5) renders in the gray fill.

    Returns:
        A one-line :class:`Text`, exactly 53 cells: five 9-cell chips and four 2-cell
        gaps.
    """
    fill = "fkey.chip.shift" if shifted else "fkey.chip"
    row = Text()
    for index in range(5):
        if index:
            row.append(_GAP)
        pair = lane[index] if index < len(lane) else None
        number = index + 6 if shifted else index + 1
        label = (pair.opp_label if shifted else pair.label) if pair else ""
        if not label:
            # Unassigned (no slot, or a lone slot's empty Shift bank): the bare key
            # number, unfilled — nothing to press here.
            key = f"F{number}" if number < 10 else "10"
            row.append(f"{key}{' ' * (_CHIP_WIDTH - len(key))}", style="muted")
            continue
        live = pair.opp_enabled if shifted else pair.enabled
        # A dimmed slot drops the fill and keeps the label, landing in the same muted
        # "nothing to press" class as an unassigned one — while still saying what the
        # key is for once it becomes available.
        row.append(_chip(number, label), style=fill if live else "muted")
    return row


#: The screen action each key notation in a footer hint dispatches — the vocabulary the
#: hint line and the lane have in common, and the only place the two can say the same
#: thing twice. Arrows, Enter, Esc and the letter chords are absent deliberately: no lane
#: slot ever claims them (Enter and Esc are barred outright), so an atom naming one can
#: never be a duplicate of a chip.
_HINT_KEY_ACTIONS: dict[str, str] = {
    "PgUp": "pageup",
    "PgDn": "pagedown",
    "Home": "home",
    "End": "end",
    "^PgUp": "ctrl_pageup",
    "^PgDn": "ctrl_pagedown",
    "^Home": "ctrl_home",
    "^End": "ctrl_end",
    "Tab": "tab",
}

#: Marks a word as *some* key notation even where it isn't one this module can map: a
#: chord (``^Y``), a shifted key (``⇧Tab``), or a bare glyph key (``⌫``). An atom whose
#: verb half carries one of these names more than the keys it opened with, so it is never
#: dropped on the strength of the ones it did name (the map's ``Home/^Y region/you``).
_KEYISH = ("^", "⇧", "⌫", "↑", "↓", "←", "→")


def _hint_keys(atom: str) -> tuple[str, ...]:
    """The key notations ``atom`` opens with, or ``()`` where it names something else too.

    A hint atom is *keys then verb* — ``PgUp/PgDn scroll``, ``Home/End ends`` — so the
    keys are the leading run of words whose every ``/``-separated part this module knows.
    The run stops at the verb, and the answer is empty (meaning "keep this atom") both
    when the atom opens with a key we can't map (``↑↓ PgUp/PgDn scroll`` documents the
    arrows too) and when anything after the run still looks like a key notation
    (:data:`_KEYISH`) — dropping such an atom would take an unrepresented key with it.
    """
    keys: list[str] = []
    verb = False  # past the leading key run: everything from here is the atom's verb half
    for word in atom.split():
        parts = word.split("/")
        mappable = all(part in _HINT_KEY_ACTIONS for part in parts)
        if not verb and mappable:
            keys.extend(parts)
            continue
        verb = True
        if mappable or any(mark in word for mark in _KEYISH):
            return ()  # a second key rides in the verb half — the atom says more than these
    return tuple(keys)


def strip_lane_atoms(hint: str, lane: Lane) -> str:
    """``hint`` with every atom the lane already advertises removed.

    Where both a hint line and the lane are drawn at once — a floating dialog on the
    PicoCalc, whose box carries the hint in its border while the frame's footer row below
    carries *its* lane — an atom whose keys are all chips is the same statement twice, in
    two notations, and the cells are the dialog's to spend on what the lane doesn't say.
    An atom survives unless **every** key it names is a slot's action: a lane covering
    only half of ``Home/End ends`` leaves the whole atom standing rather than telling half
    a truth.

    Dimmed slots count as covering. A dim chip keeps its label and means *a thing here,
    just not right now* (see the module docstring) — the reader has been told where the
    action lives either way, which is all the hint atom was for.

    Runs per paint, never once at construction: a screen rewrites its hint as its content
    changes (the packet viewer drops to a bare ``Esc close`` on a single packet) and reads
    its lane fresh on every frame, so the overlap between the two is a property of *this*
    paint.

    Args:
        hint: The screen's footer hint, in the standard ``a · b · Esc x`` grammar.
        lane: The lane drawn on the same frame.

    Returns:
        The kept atoms, rejoined in order; ``""`` when the lane says all of it.
    """
    covered = {
        action
        for pair in lane
        if pair is not None
        for action in (pair.action, pair.opp_action)
        if action
    }
    kept = []
    for raw in hint.split("·"):
        atom = raw.strip()
        if not atom:
            continue
        keys = _hint_keys(atom)
        if keys and all(_HINT_KEY_ACTIONS[key] in covered for key in keys):
            continue
        kept.append(atom)
    return " · ".join(kept)
