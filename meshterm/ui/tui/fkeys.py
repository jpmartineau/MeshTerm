# SPDX-License-Identifier: Apache-2.0
"""The F-key lane: a handheld's fixed footer row of coloured chips, one for each function key.

**Each platform has its own lane** (JP, 2026-09-30). The parts of the lane concept are
shared: the :class:`FPair` slots, the dim and live states, and the removal of lane atoms
from the hint of a dialog. All that belongs to one keyboard is a :class:`LaneDeck`, named
by ``Platform.lane_deck``:

* which keycodes drive the slots,
* where the chips are on the row,
* how the chips are drawn, and
* which of the lane definitions of a screen the deck reads.

A screen gives its lane for each deck (``Screen.picocalc_lyra_lane``,
``Screen.cardputer_zero_lane``). The Cardputer's lane follows the PicoCalc's lane until a
screen gives a different one. Both decks have five slots, but that is a coincidence of two
keyboards, not a rule, and nothing here assumes it: the slot count of a deck is the number
of its keys.

The rest of this docstring is the design of the PicoCalc deck, and the Cardputer's deck
uses the same design for now. The PicoCalc keyboard has five dedicated function keys. Its
MCU translates Shift+F1..F5 into plain F6-F10 keycodes (measured in P0). Thus
"Shift+key = the opposite action" is literal hardware behaviour, and the app binds all ten
keys.

By JP's spec (2026-08-01), a lane slot is for a function that is otherwise hard to reach.
It is not a shortcut to a keyboard key that is already easy to reach. Enter and Esc are
directly on the keyboard, and they are already the easiest keyboard keys to press. Thus
they never use a slot. Paging has no physical keyboard key, so a screen that scrolls gets
the best pair, F4/F5, for it. Ctrl+letter chords (Ctrl+P for message paths, Ctrl+arrows
for a sort column, …) are difficult to hold on this keyboard. Thus a screen moves its own
chords onto the free F1-F3 bank.

The five plain keys have the actions of a screen that are used most and are otherwise
difficult to reach. The user can reach them with no Shift. The Shift bank (F6-F10) holds
the *opposite number* of each slot, not unrelated overflow. A lane can be different on
each screen, and it is.

**Second-grade keys go on the Shift half of their own pair** (JP, 2026-08-08). Home and
End are on this keyboard, so a jump to an end of a body was never hard to reach, as paging
is. The jump got a chip only to be consistent with the pager that it belongs to. Thus the
jump goes on that pager: Shift+F5 (``F10``) is Top because F5 is Page ↑, and Shift+F4
(``F9``) is Bottom because F4 is Page ↓. One axis uses one pair of slots, and the jump is
behind the page on the same keyboard key. Thus **F1-F3 stay free on each screen** for the
verbs that the screen has to offer, which is where the interesting work goes.

**A chip names an action, never a keyboard key** (JP, 2026-08-07). ``PgUp``, ``End``, and
similar names are the names of keyboard keys that this keyboard does not have. The slot is
for what it does on this screen: ``Page ↑`` through a body, ``Latest`` on a transcript,
``Reset`` on the map. (On the map, the Home key fits the viewport again, and the paging
keys zoom.) A screen that uses a shared action for a different purpose gives it a new
label. A screen on which the action has no meaning leaves the slot **empty**, and does not
fill the lane.

Empty and dim are different claims. Empty says "not a thing here" (Retry in a channel,
where nothing is ever acknowledged). Dim says "a thing here, but not now" (Retry in a
direct chat where all messages are delivered).

A screen gives its lane as five :class:`FPair` slots (``None`` = unassigned). The session
resolves a pressed F-key to the action string of the slot, and dispatches it through the
normal action funnel. Thus screens get F-key support with no key handling of their own.
An :class:`FPair` does not have to have a Shift-bank action. A "lone" slot
(``opp_label=""``) renders blank in the shifted bank, and F(n+5) does nothing.

**A slot never advertises a keyboard key that does nothing.** The lane is the full footer
of the PicoCalc. It replaces the hint line that the other platform draws, and that line
always removed an atom whose keyboard key does nothing (refer to
:attr:`~meshterm.ui.tui.screen.Screen.content_overflows`). Thus a screen builds its lane
again at each paint. It clears :attr:`FPair.enabled` / :attr:`FPair.opp_enabled` on each
action that is not available now: the chat's *Paths* when no message is picked, its
*Retry* when no message is unacknowledged, and the shared nav slots on a screen with
nothing to move (:func:`default_lane`).

A dim slot keeps its label (muted and with no fill, as an unassigned slot). Thus the
keyboard key still shows what it will do, but it is clearly not live. The dimming is only
presentational. The screen's own ``handle`` stays the authority on what an action does
(it does nothing). Thus a lane that was built from metrics one paint old can never
block a key press that works.
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
    """One F-key slot: the action of the plain key, and its optional Shift-bank companion.

    Attributes:
        label: A verb-phrase hint for the plain key, for example ``"Zoom +"``: what the
            keyboard key does on this screen, never the name of the keyboard key
            (``"PgUp"``). Clipped or padded to 6 cells.
        action: The screen action that the plain key dispatches (``"home"``, ``"enter"``,
            …).
        opp_label: The hint for the Shift-bank companion, or ``""`` for a lone slot with
            no companion. Then the shifted bank renders that chip blank, and the related
            F(n+5) does nothing.
        opp_action: The action that the Shift-bank companion dispatches, or ``""``.
        enabled: Whether the action of the plain key is available now. ``False`` draws
            the chip dim (the label stays, and the fill is removed), and does not pretend
            (refer to the module docstring).
        opp_enabled: The same, for the Shift-bank companion.
    """

    label: str
    action: str
    opp_label: str = ""
    opp_action: str = ""
    enabled: bool = True
    opp_enabled: bool = True


#: A lane is five slots, F1..F5 from left to right. ``None`` leaves a slot unassigned.
Lane = Sequence[FPair | None]

#: The default lane, in the vocabulary of a screen that is one body that scrolls. Move
#: through the body one page at a time (F4/F5), or jump to the end that each pager goes to
#: (F9/F10, behind Shift, on the same two keyboard keys). There is no Enter or Esc slot,
#: because both keyboard keys are already directly on the keyboard. F1-F3 stay free for
#: the verbs of the screen. A screen that has no verbs leaves them blank, and does not
#: fill the lane. A screen whose body is not one column that scrolls gives its lane in its
#: own words (refer to :class:`ChatScreen`, ``MapScreen``).
#:
#: **A directional pair rises toward its outer key** (JP, 2026-08-08). When two adjacent
#: chips are opposite ends of one axis, the *up · in · more* end takes the slot that is
#: nearer to the edge of the lane. On the right-hand F4/F5 pair, that is F5: the pager
#: here, or the map's zoom (``Zoom -`` then ``Zoom +``, a rocker). On a left-hand F1/F2
#: pair, it is F1, so a select list reads ``Sect ↑`` then ``Sect ↓``. The reason is
#: ergonomic: the hand stays on the edge of the lane and rocks inward, on the side of the
#: pair.
#: The Shift companion of a slot follows the same rule along its slot: the jump behind
#: ``Page ↑`` is the end that Page ↑ goes to, so F10 is Top and F9 is Bottom. A screen
#: that gives a pair new labels keeps its order (a chat's ``Latest``/``Oldest``).
DEFAULT_LANE: tuple[FPair | None, ...] = (
    None,
    None,
    None,
    FPair("Page ↓", "pagedown", "Bottom", "end"),
    FPair("Page ↑", "pageup", "Top", "home"),
)

#: The lane of a screen with no lane: a confirm dialog, a busy spinner, a text prompt.
#: Nothing there pages or jumps. Thus the shared pager is not "dim but real". It is "not a
#: thing here", which is the difference that the module docstring explains. Each slot
#: renders as a bare key number with no fill.
EMPTY_LANE: tuple[FPair | None, ...] = (None, None, None, None, None)


def default_lane(*, nav: bool = True) -> tuple[FPair | None, ...]:
    """:data:`DEFAULT_LANE`, with its pager slots dim unless ``nav``.

    Both shared slots move something: the page, and the jump behind it. Thus on a screen
    with nothing to move (a body that fits in its visible area, a transcript with no
    messages), both banks do nothing and show it. Screens that use the nav actions for
    something that is always live (the map's zoom) build from :data:`DEFAULT_LANE`
    instead, and give new labels.

    Args:
        nav: Whether the nav keys can do something on this paint. This is usually
            :attr:`~meshterm.ui.tui.screen.Screen.content_overflows`.

    Returns:
        The five slots, ready for a screen to overwrite the slots that it claims.
    """
    if nav:
        return DEFAULT_LANE
    return tuple(
        None if pair is None else replace(pair, enabled=False, opp_enabled=False)
        for pair in DEFAULT_LANE
    )


@dataclass(frozen=True, slots=True)
class LaneDeck:
    """One platform's F-key lane: its keyboard keys, where it draws, and whose lanes it reads.

    The slots are from left to right, and each per-slot tuple here has one entry for each
    slot.

    Attributes:
        name: What ``Platform.lane_deck`` calls this deck.
        keys: The F-key number that the plain key of each slot arrives as.
        shift_keys: The F-key number that the Shift companion of each slot arrives as.
        captions: How the row names the plain key of each slot. The caption starts a
            captioned chip, and it stands alone, muted, in a slot with nothing assigned.
        shift_captions: The same, for the Shift bank.
        columns: The column at which each chip starts.
        chip_width: The number of cells in each chip.
        captioned: Whether a live chip starts with the caption of its keyboard key
            (``F1 Zoom +``), or is only its label, at the centre. The second form is for
            a keyboard whose keyboard key is directly under the chip. There, the name of
            the keyboard key again only uses cells of the label.
        fill: The theme style of a live chip in the plain bank.
        shift_fill: The same, while the Shift watcher reports that Shift is held.
        read_lane: Reads the lane of a screen for this deck.
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
        """The action that F-key ``number`` resolves to on this lane, or ``None``.

        A plain key takes the action of its slot, and a Shift-bank key takes the action of
        its companion. The result is ``None`` where the slot has no action (a lone slot, or
        an unassigned slot), and where the keyboard key is not a key of this deck. A dim
        slot still resolves. Dimming is how the lane draws an action on which the screen
        does nothing anyway. Because the action resolves in all cases, a lane that was
        built from stale metrics can never block a key press that works (refer to the
        module docstring).
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
            lane: The slots. The slots after the last slot of this deck are not drawn.
            shifted: Show the Shift bank, in :attr:`shift_fill`. The plain bank renders
                in :attr:`fill`.

        Returns:
            A one-line :class:`Text` that ends with the last chip.
        """
        fill = self.shift_fill if shifted else self.fill
        captions = self.shift_captions if shifted else self.captions
        row = Text()
        for index, column in enumerate(self.columns):
            row.append(" " * (column - row.cell_len))
            pair = lane[index] if index < len(lane) else None
            label = (pair.opp_label if shifted else pair.label) if pair else ""
            if not label:
                # Unassigned (no slot, or the empty Shift bank of a lone slot): the bare
                # key caption with no fill. There is nothing to press here.
                row.append(self._body(captions[index]), style="muted")
                continue
            live = pair.opp_enabled if shifted else pair.enabled
            body = f"{captions[index]} {label[:_DESC_WIDTH]:<{_DESC_WIDTH}}"
            # A dim slot removes the fill and keeps the label. Thus it is in the same muted
            # "nothing to press" class as an unassigned slot. But it still says what the
            # keyboard key is for, when the action becomes available.
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
#: plain F6-F10, so the Shift bank has keyboard keys of its own. Each chip is a 2-cell key
#: caption, a space, and a 6-cell label. Five chips and four 2-cell gaps add up to exactly
#: 53, the width of the console, with nothing left over.
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

#: The Cardputer Zero's number keys 4-8, which are directly under the display. Fn+4..8 are
#: F4-F8, never the bare digits, because a digit must stay a digit on a screen that takes
#: typing (JP, 2026-09-30). The keyboard driver sends Shift as a key of its own. The
#: emulator encodes Shift+Fn+4..8 as xterm encodes Shift+F4..F8, which prompt_toolkit
#: reads as F16-F20. Thus a desktop terminal with ``--platform cardputer-zero`` drives the
#: same bank. The chips have exactly the same layout as the PicoCalc's: the caption of the
#: keyboard key before a 6-cell label, nine cells for each chip, and two cells between
#: chips (JP, 2026-09-30). The Shift bank keeps the same captions, because it is the same
#: keyboard keys with Shift held. Its blue fill shows that Shift is held. For now, the
#: deck reads the lanes of the PicoCalc (refer to
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

#: All the decks, by the name that ``Platform.lane_deck`` gives each one.
DECKS: dict[str, LaneDeck] = {deck.name: deck for deck in (PICOCALC_LYRA_DECK, CARDPUTER_ZERO_DECK)}

_DECK: LaneDeck | None = None


def active_deck() -> LaneDeck | None:
    """The active platform's deck, or ``None`` where its footer is a hint line instead."""
    return _DECK


@on_platform
def _bind(platform: Platform) -> None:
    """Set the deck of the platform (refer to :attr:`~meshterm.platforms.Platform.lane_deck`)."""
    global _DECK
    _DECK = DECKS[platform.lane_deck] if platform.lane_deck else None


#: The screen action that each key notation in a footer hint dispatches. This is the
#: vocabulary that the hint line and the lane have in common, and the only place where the
#: two can say the same thing two times. Arrows, Enter, Esc, and the letter chords are not
#: here, on purpose. No lane slot ever claims them (Enter and Esc can never have a slot).
#: Thus an atom that names one of them can never be a duplicate of a chip.
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

#: Marks a word as a key notation of some type, also where this module cannot map it: a
#: chord (``^Y``), a shifted key (``⇧Tab``), or a bare glyph key (``⌫``). If the verb half
#: of an atom has one of these marks, the atom names more than the keys at its start. Thus
#: the atom is never removed because of the keys that it did name (the map's
#: ``Home/^Y region/you``).
_KEYISH = ("^", "⇧", "⌫", "↑", "↓", "←", "→")


def _hint_keys(atom: str) -> tuple[str, ...]:
    """The key notations that ``atom`` opens with, or ``()`` if it also names something else.

    A hint atom is keys, then a verb: ``PgUp/PgDn scroll``, ``Home/End ends``. Thus the
    keys are the leading run of words in which this module knows each ``/``-separated part.
    The run stops at the verb. The answer is empty (which means "keep this atom") in two
    cases:

    * when the atom opens with a key that we cannot map (``↑↓ PgUp/PgDn scroll`` also
      documents the arrows), and
    * when a word after the run still looks like a key notation (:data:`_KEYISH`).

    If such an atom is removed, it takes away a key that the lane does not represent.
    """
    keys: list[str] = []
    verb = False  # after the leading key run: all words from here are the verb half
    for word in atom.split():
        parts = word.split("/")
        mappable = all(part in _HINT_KEY_ACTIONS for part in parts)
        if not verb and mappable:
            keys.extend(parts)
            continue
        verb = True
        if mappable or any(mark in word for mark in _KEYISH):
            return ()  # a second key is in the verb half. The atom says more than these keys.
    return tuple(keys)


def strip_lane_atoms(hint: str, lane: Lane) -> str:
    """``hint`` with each atom that the lane already advertises removed.

    Sometimes a hint line and the lane are drawn at the same time: for example, a floating
    dialog on the PicoCalc. Its box has the hint in its border, while the footer row of
    the frame below it has the lane of the frame. Then an atom whose keys are all chips is
    the same statement two times, in two notations. The dialog can use those cells for
    what the lane does not say. An atom stays unless **each** key that it names is the
    action of a slot. If a lane covers only half of ``Home/End ends``, the full atom
    stays, and does not tell half of a truth.

    Dim slots also cover their keys. A dim chip keeps its label and means "a thing here,
    but not now" (refer to the module docstring). In both cases, the user knows where the
    action is, and that was the only purpose of the hint atom.

    This function runs at each paint, never one time at construction. A screen writes its
    hint again when its content changes (on a single packet, the packet viewer changes to
    a bare ``Esc close``). It also reads its lane again on each frame. Thus the overlap
    between the two is a property of this paint.

    Args:
        hint: The footer hint of the screen, in the standard ``a · b · Esc x`` grammar.
        lane: The lane that is drawn on the same frame.

    Returns:
        The atoms that stay, joined again in order. ``""`` when the lane says all of it.
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
