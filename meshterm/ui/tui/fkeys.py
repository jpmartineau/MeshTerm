# SPDX-License-Identifier: Apache-2.0
"""The F-key lane: a handheld's fixed footer row of coloured chips, one per function key.

**Each platform deals its own lane** (JP, 2026-09-30). What a lane *is* — slots of
:class:`FPair`, dimmed or live, stripped from a dialog's hint — is shared; everything that
belongs to one keyboard is a :class:`LaneDeck`, named by ``Platform.lane_deck``: which
keycodes drive the slots, where the chips sit on the row, how they are drawn, and which of
a screen's lane definitions it reads. A screen states its lane per deck —
``Screen.picocalc_lyra_lane``, ``Screen.cardputer_zero_lane`` — and the Cardputer's follows the
PicoCalc's until a screen says otherwise. Both decks having five slots is a coincidence of
two keyboards, not a rule, and nothing here assumes it: a deck's slot count is its keys.

The rest of this docstring is the PicoCalc deck's design, which the Cardputer's deals for
now. The PicoCalc keyboard has five dedicated function keys; its MCU translates Shift+F1..F5
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

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from operator import attrgetter
from typing import Any

from rich.text import Text

from ...platforms import Platform, on_platform

#: A chip's label budget: every lane label is written to fit six cells.
_DESC_WIDTH = 6


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


@dataclass(frozen=True, slots=True)
class LaneDeck:
    """One platform's F-key lane: the keys that drive it, where it draws, whose lanes it reads.

    The slots are left to right, and every per-slot tuple here has one entry per slot.

    Attributes:
        name: What ``Platform.lane_deck`` calls this deck.
        keys: The F-key number each slot's plain key arrives as.
        shift_keys: The F-key number each slot's Shift companion arrives as.
        captions: How each slot's plain key is named on the row: leading a captioned
            chip, and standing alone, muted, in a slot with nothing assigned.
        shift_captions: The same, for the Shift bank.
        columns: The column each chip starts at.
        chip_width: Cells per chip.
        captioned: Whether a live chip leads with its key's caption (``F1 Zoom +``) or
            is its label alone, centred — for a keyboard whose key sits right under the
            chip, where naming it again only costs the label cells.
        fill: The theme style of a live chip in the plain bank.
        shift_fill: The same, while the Shift watcher reports Shift held.
        read_lane: Reads a screen's lane for this deck.
    """

    name: str
    keys: tuple[int, ...]
    shift_keys: tuple[int, ...]
    captions: tuple[str, ...]
    shift_captions: tuple[str, ...]
    columns: tuple[int, ...]
    chip_width: int
    captioned: bool
    fill: str
    shift_fill: str
    read_lane: Callable[[Any], Lane]

    def is_shift_key(self, number: int) -> bool:
        """Whether F-key ``number`` is one of this deck's Shift-bank keys."""
        return number in self.shift_keys

    def action_for(self, lane: Lane, number: int) -> str | None:
        """The action F-key ``number`` resolves to on this lane, or ``None``.

        A plain key takes its slot's action, a Shift-bank key its companion — or nothing,
        where the slot has none (a lone slot, or an unassigned one), and where the key is
        not one of this deck's at all. A *dimmed* slot still resolves: dimming is how the
        lane draws an action the screen would no-op anyway, and resolving it regardless
        keeps a lane built from stale metrics from ever swallowing a key that works (see
        the module docstring).
        """
        if number in self.keys:
            index, shifted = self.keys.index(number), False
        elif number in self.shift_keys:
            index, shifted = self.shift_keys.index(number), True
        else:
            return None
        pair = lane[index] if index < len(lane) else None
        if pair is None:
            return None
        if shifted:
            return pair.opp_action or None
        return pair.action

    def lane_text(self, lane: Lane, *, shifted: bool = False) -> Text:
        """Render the lane as this deck's chips: coloured fill, white text, each at its column.

        Args:
            lane: The slots; any past this deck's last are not drawn.
            shifted: Show the Shift bank, in :attr:`shift_fill`; the plain bank renders
                in :attr:`fill`.

        Returns:
            A one-line :class:`Text`, ending with the last chip.
        """
        fill = self.shift_fill if shifted else self.fill
        captions = self.shift_captions if shifted else self.captions
        row = Text()
        for index, column in enumerate(self.columns):
            row.append(" " * (column - row.cell_len))
            pair = lane[index] if index < len(lane) else None
            label = (pair.opp_label if shifted else pair.label) if pair else ""
            if not label:
                # Unassigned (no slot, or a lone slot's empty Shift bank): the bare key
                # caption, unfilled — nothing to press here.
                row.append(self._body(captions[index]), style="muted")
                continue
            live = pair.opp_enabled if shifted else pair.enabled
            body = f"{captions[index]} {label[:_DESC_WIDTH]:<{_DESC_WIDTH}}"
            # A dimmed slot drops the fill and keeps the label, landing in the same muted
            # "nothing to press" class as an unassigned one — while still saying what the
            # key is for once it becomes available.
            row.append(
                self._body(body if self.captioned else label[:_DESC_WIDTH]),
                style=fill if live else "muted",
            )
        return row

    def _body(self, text: str) -> str:
        """``text`` padded to exactly one chip: left-aligned when captioned, else centred."""
        if self.captioned:
            return f"{text:<{self.chip_width}}"
        return f"{text:^{self.chip_width}}"


#: The PicoCalc's five dedicated function keys. Its keyboard MCU sends Shift+F1..F5 as
#: plain F6-F10, so the Shift bank is keys of its own. Every chip is a 2-cell key caption,
#: a space and a 6-cell label; five chips and four 2-cell gaps sum to exactly 53, the
#: console's width, with nothing left over.
PICOCALC_LYRA_DECK = LaneDeck(
    name="picocalc-lyra",
    keys=(1, 2, 3, 4, 5),
    shift_keys=(6, 7, 8, 9, 10),
    captions=("F1", "F2", "F3", "F4", "F5"),
    shift_captions=("F6", "F7", "F8", "F9", "10"),
    columns=(0, 11, 22, 33, 44),
    chip_width=9,
    captioned=True,
    fill="fkey.chip",
    shift_fill="fkey.chip.shift",
    read_lane=attrgetter("picocalc_lyra_lane"),
)

#: The Cardputer Zero's number keys 4-8, which sit right under the display: Fn+4..8 are
#: F4-F8, never the bare digits, since a digit must stay a digit on a screen that takes
#: typing (JP, 2026-09-30). The keyboard driver sends Shift as a key of its own, and the
#: console host encodes Shift+Fn+4..8 the way xterm encodes Shift+F4..F8, which
#: prompt_toolkit reads as F16-F20 — so a desktop terminal with ``--platform cardputer-zero``
#: drives the same bank. The chips are laid out exactly as the PicoCalc's — the key's
#: caption leading a 6-cell label, nine cells a chip, two between (JP, 2026-09-30) — and
#: the Shift bank keeps the same captions, since it is the same keys with Shift held; its
#: blue fill is what says so. It reads the PicoCalc's lanes for now (see
#: :meth:`~meshterm.ui.tui.screen.Screen.cardputer_zero_lane`).
CARDPUTER_ZERO_DECK = LaneDeck(
    name="cardputer-zero",
    keys=(4, 5, 6, 7, 8),
    shift_keys=(16, 17, 18, 19, 20),
    captions=("F4", "F5", "F6", "F7", "F8"),
    shift_captions=("F4", "F5", "F6", "F7", "F8"),
    columns=PICOCALC_LYRA_DECK.columns,
    chip_width=PICOCALC_LYRA_DECK.chip_width,
    captioned=True,
    fill="fkey.chip.cardputer_zero",
    shift_fill="fkey.chip.cardputer_zero.shift",
    read_lane=attrgetter("cardputer_zero_lane"),
)

#: Every deck, by the name ``Platform.lane_deck`` gives it.
DECKS: dict[str, LaneDeck] = {deck.name: deck for deck in (PICOCALC_LYRA_DECK, CARDPUTER_ZERO_DECK)}

_DECK: LaneDeck | None = None


def active_deck() -> LaneDeck | None:
    """The active platform's deck, or ``None`` where its footer is a hint line instead."""
    return _DECK


@on_platform
def _bind(platform: Platform) -> None:
    """Deal the platform's deck (see :attr:`~meshterm.platforms.Platform.lane_deck`)."""
    global _DECK
    _DECK = DECKS[platform.lane_deck] if platform.lane_deck else None


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
