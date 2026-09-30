# SPDX-License-Identifier: Apache-2.0
"""Each handheld deals its own F-key lane: its keys, its chips, and its lane definitions."""

from __future__ import annotations

from prompt_toolkit.keys import Keys
from rich.cells import cell_len

from meshterm.platforms import CARDPUTER, PICOCALC, REGULAR, resolve, set_platform
from meshterm.services import modifier_watch
from meshterm.ui.tui import session
from meshterm.ui.tui.fkeys import (
    CARDPUTER_DECK,
    DECKS,
    DEFAULT_LANE,
    EMPTY_LANE,
    PICOCALC_DECK,
    FPair,
    active_deck,
)
from meshterm.ui.tui.screen import ScrollScreen

#: Where M5's own screen-key drawing centres the labels for keys 4-8, in panel pixels.
_KEY_CENTRES_PX = (48, 104, 160, 216, 272)


class _OwnCardputerLane(ScrollScreen):
    """A screen that has decided its Cardputer lane for itself."""

    @property
    def cardputer_lane(self):
        return (FPair("Mine", "mine"), None, None, None, None)


def test_every_handheld_names_a_deck_of_its_own() -> None:
    """A platform with a lane names a deck that exists; the desktop names none."""
    assert PICOCALC.lane_deck == "picocalc" and CARDPUTER.lane_deck == "cardputer"
    assert all(DECKS[p.lane_deck].name == p.lane_deck for p in (PICOCALC, CARDPUTER))
    assert REGULAR.lane_deck == "" and not REGULAR.footer_fkeys
    assert PICOCALC.footer_fkeys and CARDPUTER.footer_fkeys


def test_the_active_deck_follows_the_platform() -> None:
    """The deck is bound at platform-switch time, never looked up per frame."""
    assert active_deck() is None
    set_platform(PICOCALC)
    assert active_deck() is PICOCALC_DECK
    set_platform(CARDPUTER)
    assert active_deck() is CARDPUTER_DECK


def test_the_cardputer_deals_the_picocalc_lanes_until_a_screen_says_otherwise() -> None:
    """JP, 2026-09-30: same entries for now, and each platform reads its own definition."""
    screen = ScrollScreen("body")
    assert screen.cardputer_lane == screen.picocalc_lane

    own = _OwnCardputerLane("body")
    set_platform(CARDPUTER)
    assert own.fkey_lane[0].label == "Mine"
    set_platform(PICOCALC)
    assert own.fkey_lane == own.picocalc_lane  # the PicoCalc never sees the Cardputer's choice
    set_platform(REGULAR)
    assert own.fkey_lane == EMPTY_LANE  # no lane on the desktop


def test_cardputer_keys_are_fn_4_to_8_with_their_shift_bank() -> None:
    """Fn+4..8 arrive as F4-F8, Shift with them as F16-F20; nothing else is the lane's."""
    lane = DEFAULT_LANE
    assert CARDPUTER_DECK.action_for(lane, 7) == "pagedown"
    assert CARDPUTER_DECK.action_for(lane, 8) == "pageup"
    assert CARDPUTER_DECK.action_for(lane, 19) == "end"
    assert CARDPUTER_DECK.action_for(lane, 20) == "home"
    assert all(CARDPUTER_DECK.action_for(lane, n) is None for n in (1, 2, 3, 9, 10, 11, 12))
    assert CARDPUTER_DECK.is_shift_key(16) and not CARDPUTER_DECK.is_shift_key(6)
    # The same lane on the PicoCalc's own keys, untouched by the Cardputer's.
    assert PICOCALC_DECK.action_for(lane, 4) == "pagedown"
    assert PICOCALC_DECK.action_for(lane, 10) == "home"
    assert PICOCALC_DECK.action_for(lane, 16) is None


def test_the_session_binds_every_key_a_deck_can_use() -> None:
    """Which F-keys drive the lane is the deck's business, so all 24 reach _dispatch."""
    for deck in DECKS.values():
        for number in deck.keys + deck.shift_keys:
            assert session._KEY_ACTIONS[getattr(Keys, f"F{number}")] == f"f{number}"


def test_cardputer_chips_sit_over_their_keys() -> None:
    """Each chip is centred on its key's label position, a label alone in eight cells."""
    lane = (FPair("Region", "home"), FPair("You", "locate", enabled=False), None, *DEFAULT_LANE[3:])
    row = CARDPUTER_DECK.lane_text(lane)
    for column, centre_px in zip(CARDPUTER_DECK.columns, _KEY_CENTRES_PX, strict=True):
        chip_centre_px = (column + CARDPUTER_DECK.chip_width / 2) * 6
        assert abs(chip_centre_px - centre_px) <= 2  # within a third of a cell
    starts = CARDPUTER_DECK.columns
    assert all(
        b - a > CARDPUTER_DECK.chip_width for a, b in zip(starts, starts[1:], strict=False)
    ), "a gap"
    assert cell_len(row.plain) <= CARDPUTER.readable_cols
    chips = [row.plain[c : c + CARDPUTER_DECK.chip_width] for c in starts]
    assert chips[0] == " Region " and chips[3] == " Page ↓ "
    assert chips[2].strip() == "F6"  # an unassigned slot shows its key, unfilled
    styles = {row.plain[s.start : s.end].strip(): str(s.style) for s in row.spans}
    assert styles["Region"] == "fkey.chip.cardputer"
    assert styles["You"] == "muted" and styles["F6"] == "muted"


def test_cardputer_shift_bank_draws_in_its_own_fill() -> None:
    """Shifted, the companions show in the Shift fill; a lone slot shows its bare key."""
    row = CARDPUTER_DECK.lane_text(DEFAULT_LANE, shifted=True)
    styles = {row.plain[s.start : s.end].strip(): str(s.style) for s in row.spans}
    assert styles["Bottom"] == "fkey.chip.cardputer.shift"
    assert styles["Top"] == "fkey.chip.cardputer.shift"
    assert styles["F4"] == "muted"


def test_the_picocalc_row_is_unchanged() -> None:
    """Five captioned 9-cell chips with 2-cell gaps: exactly the console's 53 columns."""
    row = PICOCALC_DECK.lane_text(DEFAULT_LANE)
    assert cell_len(row.plain) == PICOCALC.readable_cols
    assert row.plain.startswith("F1       ")
    assert row.plain.endswith("F5 Page ↑")


def test_the_platform_resolves_by_name() -> None:
    """Chosen by flag or environment only: no device-tree model is known for it yet."""
    assert resolve("cardputer").platform is CARDPUTER
    assert CARDPUTER.readable_cols == 53 and CARDPUTER.readable_rows == 14


def test_the_shift_watcher_looks_for_the_platforms_keyboard(tmp_path, monkeypatch) -> None:
    """The watcher finds the keyboard the platform names, by its input device name."""
    for event, name in (("event0", "gpio-keys"), ("event1", "tca8418c")):
        device = tmp_path / event / "device"
        device.mkdir(parents=True)
        (device / "name").write_text(f"{name}\n")
    monkeypatch.setattr(modifier_watch, "_SYS_INPUT", tmp_path)
    assert modifier_watch._find_keyboard(CARDPUTER.modifier_watch).name == "event1"
    assert modifier_watch._find_keyboard(PICOCALC.modifier_watch) is None
