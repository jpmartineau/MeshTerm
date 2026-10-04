# SPDX-License-Identifier: Apache-2.0
"""Each handheld deals its own F-key lane: its keys, its chips, and its lane definitions."""

from __future__ import annotations

from prompt_toolkit.keys import Keys
from rich.cells import cell_len

from meshterm.platforms import CARDPUTER_ZERO, PICOCALC_LYRA, REGULAR, resolve, set_platform
from meshterm.services import modifier_watch
from meshterm.ui.tui import session
from meshterm.ui.tui.fkeys import (
    CARDPUTER_ZERO_DECK,
    DECKS,
    DEFAULT_LANE,
    EMPTY_LANE,
    PICOCALC_LYRA_DECK,
    FPair,
    active_deck,
)
from meshterm.ui.tui.screen import ScrollScreen


class _OwnCardputerLane(ScrollScreen):
    """A screen that has decided its Cardputer lane for itself."""

    @property
    def cardputer_zero_lane(self):
        return (FPair("Mine", "mine"), None, None, None, None)


def test_every_handheld_names_a_deck_of_its_own() -> None:
    """A platform with a lane names a deck that exists; the desktop names none."""
    assert (
        PICOCALC_LYRA.lane_deck == "picocalc-lyra" and CARDPUTER_ZERO.lane_deck == "cardputer-zero"
    )
    assert all(DECKS[p.lane_deck].name == p.lane_deck for p in (PICOCALC_LYRA, CARDPUTER_ZERO))
    assert REGULAR.lane_deck == "" and not REGULAR.footer_fkeys
    assert PICOCALC_LYRA.footer_fkeys and CARDPUTER_ZERO.footer_fkeys


def test_the_active_deck_follows_the_platform() -> None:
    """The deck is bound at platform-switch time, never looked up per frame."""
    assert active_deck() is None
    set_platform(PICOCALC_LYRA)
    assert active_deck() is PICOCALC_LYRA_DECK
    set_platform(CARDPUTER_ZERO)
    assert active_deck() is CARDPUTER_ZERO_DECK


def test_the_cardputer_deals_the_picocalc_lanes_until_a_screen_says_otherwise() -> None:
    """JP, 2026-09-30: same entries for now, and each platform reads its own definition."""
    screen = ScrollScreen("body")
    assert screen.cardputer_zero_lane == screen.picocalc_lyra_lane

    own = _OwnCardputerLane("body")
    set_platform(CARDPUTER_ZERO)
    assert own.fkey_lane[0].label == "Mine"
    set_platform(PICOCALC_LYRA)
    assert own.fkey_lane == own.picocalc_lyra_lane  # the PicoCalc never sees the Cardputer's choice
    set_platform(REGULAR)
    assert own.fkey_lane == EMPTY_LANE  # no lane on the desktop


def test_cardputer_keys_are_fn_4_to_8_with_their_shift_bank() -> None:
    """Fn+4..8 arrive as F4-F8, Shift with them as F16-F20; nothing else is the lane's."""
    lane = DEFAULT_LANE
    assert CARDPUTER_ZERO_DECK.action_for(lane, 7) == "pagedown"
    assert CARDPUTER_ZERO_DECK.action_for(lane, 8) == "pageup"
    assert CARDPUTER_ZERO_DECK.action_for(lane, 19) == "end"
    assert CARDPUTER_ZERO_DECK.action_for(lane, 20) == "home"
    assert all(CARDPUTER_ZERO_DECK.action_for(lane, n) is None for n in (1, 2, 3, 9, 10, 11, 12))
    assert CARDPUTER_ZERO_DECK.is_shift_key(16) and not CARDPUTER_ZERO_DECK.is_shift_key(6)
    # The same lane on the PicoCalc's own keys, untouched by the Cardputer's.
    assert PICOCALC_LYRA_DECK.action_for(lane, 4) == "pagedown"
    assert PICOCALC_LYRA_DECK.action_for(lane, 10) == "home"
    assert PICOCALC_LYRA_DECK.action_for(lane, 16) is None


def test_the_session_binds_every_key_a_deck_can_use() -> None:
    """Which F-keys drive the lane is the deck's business, so all 24 reach _dispatch."""
    for deck in DECKS.values():
        for number in deck.keys + deck.shift_keys:
            assert session._KEY_ACTIONS[getattr(Keys, f"F{number}")] == f"f{number}"


def test_cardputer_chips_are_laid_out_as_the_picocalcs() -> None:
    """The PicoCalc's chips — caption, 9-cell width, 2-cell gaps — under the Cardputer's keys."""
    assert CARDPUTER_ZERO_DECK.columns == PICOCALC_LYRA_DECK.columns
    assert CARDPUTER_ZERO_DECK.chip_width == PICOCALC_LYRA_DECK.chip_width
    lane = (FPair("Region", "home"), FPair("You", "locate", enabled=False), None, *DEFAULT_LANE[3:])
    row = CARDPUTER_ZERO_DECK.lane_text(lane)
    assert cell_len(row.plain) == CARDPUTER_ZERO.readable_cols
    chips = [row.plain[c : c + CARDPUTER_ZERO_DECK.chip_width] for c in CARDPUTER_ZERO_DECK.columns]
    assert chips == ["F4 Region", "F5 You   ", "F6       ", "F7 Page ↓", "F8 Page ↑"]
    styles = {row.plain[s.start : s.end].strip(): str(s.style) for s in row.spans}
    assert styles["F4 Region"] == "fkey.chip.cardputer_zero"
    assert styles["F5 You"] == "muted" and styles["F6"] == "muted"


def test_cardputer_shift_bank_draws_in_its_own_fill() -> None:
    """Shifted, the companions show in the Shift fill under the same keys' captions."""
    row = CARDPUTER_ZERO_DECK.lane_text(DEFAULT_LANE, shifted=True)
    styles = {row.plain[s.start : s.end].strip(): str(s.style) for s in row.spans}
    assert styles["F7 Bottom"] == "fkey.chip.cardputer_zero.shift"
    assert styles["F8 Top"] == "fkey.chip.cardputer_zero.shift"
    assert styles["F4"] == "muted"


def test_the_picocalc_row_is_unchanged() -> None:
    """Five captioned 9-cell chips with 2-cell gaps: exactly the console's 53 columns."""
    row = PICOCALC_LYRA_DECK.lane_text(DEFAULT_LANE)
    assert cell_len(row.plain) == PICOCALC_LYRA.readable_cols
    assert row.plain.startswith("F1       ")
    assert row.plain.endswith("F5 Page ↑")


def test_the_platform_resolves_by_name() -> None:
    """Chosen by flag or environment only: no device-tree model is known for it yet."""
    assert resolve("cardputer-zero").platform is CARDPUTER_ZERO
    assert CARDPUTER_ZERO.readable_cols == 53 and CARDPUTER_ZERO.readable_rows == 14


def test_the_shift_watcher_looks_for_the_platforms_keyboard(tmp_path, monkeypatch) -> None:
    """The watcher finds the keyboard the platform names, by its input device name."""
    for event, name in (("event0", "gpio-keys"), ("event1", "tca8418c")):
        device = tmp_path / event / "device"
        device.mkdir(parents=True)
        (device / "name").write_text(f"{name}\n")
    monkeypatch.setattr(modifier_watch, "_SYS_INPUT", tmp_path)
    assert modifier_watch._find_keyboard(CARDPUTER_ZERO.modifier_watch).name == "event1"
    assert modifier_watch._find_keyboard(PICOCALC_LYRA.modifier_watch) is None
