# SPDX-License-Identifier: Apache-2.0
"""P4 chrome contracts: the borderless frame, the slim header, the F-key lane, the dialog gate."""

from __future__ import annotations

import asyncio

from rich.cells import cell_len
from rich.text import Text

from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
from meshterm.ui.tui import frame
from meshterm.ui.tui.fkeys import (
    DEFAULT_LANE,
    PICOCALC_LYRA_DECK,
    FPair,
    default_lane,
    strip_lane_atoms,
)
from meshterm.ui.tui.screen import ScrollScreen
from tests.conftest import plain as _plain


def _screen(lines: int = 40) -> ScrollScreen:
    body = Text("\n".join(f"row {i}" for i in range(lines)))
    screen = ScrollScreen(body, title="Chrome probe", floating=False)
    return screen


def _slot_style(row: Text, index: int) -> str:
    """The style that the lane painted on the chip of slot ``index``.

    A slot is 9 cells and a gap of 2 cells.
    """
    start = index * 11
    return next(str(span.style) for span in row.spans if span.start <= start < span.end)


def test_borderless_frame_swaps_the_panel_for_a_title_bar() -> None:
    """On the PicoCalc there is no Panel. A title bar of one row replaces the whole border.

    The bar has the rule, the title, and the clip arrow. The frame uses no column for a
    border, so the body starts at column 0 and has all 53 cells.
    """
    set_platform(PICOCALC_LYRA)
    composed = frame.compose_base(Text("hdr"), _screen(), "hint", 53, 26).split("\n")
    assert len(composed) == 26
    plain = [_plain(line) for line in composed]
    # Row 1 (under the header of one line) is the title bar: rule, title, and clip arrow.
    assert "Chrome probe" in plain[1] and "─" in plain[1]
    assert "↓" in plain[1]  # 40 rows in a viewport of 23 rows: more below
    # There are no Panel borders, and the body has the full width.
    assert not any("│" in line or "╭" in line for line in plain)
    assert all(cell_len(line) <= 53 for line in plain)
    # The first row of the body starts at column 0 of row 2 (no panel padding).
    assert plain[2].startswith("row 0")


def _bar_text(
    title: str, hint: str, cols: int = 53, *, above: bool = False, below: bool = True
) -> Text:
    """The borderless title bar for a screen with ``title`` and ``hint``, as styled text."""
    screen = ScrollScreen(Text("x"), title=title, floating=False)
    screen._footer_hint = hint
    return frame._title_bar(screen, cols, above, below)


def _bar(title: str, hint: str, cols: int = 53, *, below: bool = True) -> str:
    """The plain text of the borderless title bar for a screen with ``title`` and ``hint``."""
    return _bar_text(title, hint, cols, below=below).plain


def _arrow_styles(bar: Text) -> tuple[str, str]:
    """The styles that the bar painted on its two clip arrows, ``(up, down)``."""
    return tuple(
        next(
            str(span.style)
            for span in bar.spans
            if span.start <= index < span.end and span.end - span.start == 1
        )
        for index in (0, 1)
    )


def test_the_title_bar_says_how_to_leave_the_screen() -> None:
    """This platform has no footer hint line, so the way out is at the end of the bar.

    The F-key lane is where the footer would be, and it shows only what its slots do. Thus
    the Esc key had no hint at all. Each screen answers to Esc, and this is the reason that
    no screen has a Back row (JP, 2026-08-30). The way out is last, after the title, and the
    clip arrows are at the other end of the row.
    """
    bar = _bar("Chrome probe", "↑↓ move · Enter open · Esc back")

    assert bar.startswith("↑↓ ")
    assert bar.rstrip().endswith("Esc back")
    assert "Chrome probe" in bar


def test_the_clip_arrows_are_always_drawn_and_say_it_in_colour() -> None:
    """Both arrows are fixed at the left edge of the row, and the colour shows the scroll.

    The arrows once appeared and disappeared. A pair that was half shown left a blank cell
    for the missing arrow, and the user had to compare the row with a memory of the row
    (JP, 2026-08-31). Now the pair never moves and never changes width. An arrow for a
    direction with more content has the accent colour of the border. An arrow for a
    direction with no more content is muted. This is the live and dim language of the F-key
    lane, one row up.
    """
    hint = "↑↓ move · Esc back"
    assert _arrow_styles(_bar_text("Contacts", hint, above=True, below=True)) == (
        "accent",
        "accent",
    )
    assert _arrow_styles(_bar_text("Contacts", hint, above=False, below=True)) == (
        "muted",
        "accent",
    )
    assert _arrow_styles(_bar_text("Contacts", hint, above=True, below=False)) == (
        "accent",
        "muted",
    )
    # A body that fits keeps the pair, and both are dim. Nothing appears or disappears.
    assert _arrow_styles(_bar_text("Contacts", hint, above=False, below=False)) == (
        "muted",
        "muted",
    )

    widths = {
        _bar_text("Contacts", hint, above=a, below=b).plain
        for a in (False, True)
        for b in (False, True)
    }
    assert len(widths) == 1, "the bar's text must not shift as the body scrolls"


def test_the_bar_speaks_the_screen_s_own_esc_verb() -> None:
    """The bar takes the Esc verb from the footer hint of the screen.

    Thus each surface keeps its true verb.
    """
    assert _bar("Contacts", "↑↓ move · Enter open · Esc back").endswith("Esc back")
    assert _bar("Path width", "↑↓ move · Enter set · Esc keep").endswith("Esc keep")
    # A hint with no way out shows no way out. This is the rule of the lane, one row up.
    assert "Esc" not in _bar("Working", "↑↓ move")


def test_the_main_menu_s_way_out_is_the_quit_chord_until_a_filter_stands() -> None:
    """Esc does nothing at the menu, so the bar has ^Q there, and ``Esc clear`` replaces it.

    ``Esc clear`` replaces ``^Q quit?`` while a filter is typed.

    The hint of the menu ends with ``^Q quit?`` instead of an Esc clause (issue #22). The
    end of the bar takes the way out with which the hint ends. Thus ``Esc clear`` of a typed
    filter takes the place of ``^Q quit?`` while the filter is there, because Esc does
    something then.
    """
    title = "What would you like to do?"
    idle = "↑↓ move · type to filter · Enter select · ^Q quit?"
    assert _bar(title, idle).endswith(" ^Q quit?")
    assert _bar(title, f"{idle} · Esc clear").endswith(" Esc clear")
    # If the bar is full, the atom gives way as Esc does: its bare key stays first, and there is
    # never an "Esc" that is alone.
    verbless = _bar("Trace — Hilltop-Repeater over a spec", "↑↓ move · ^Q quit?")
    assert verbless.rstrip().endswith(" ^Q") and "Esc" not in verbless


def test_the_esc_hint_gives_way_before_it_crowds_the_title() -> None:
    """The bar is compact by design: the verb goes first, then the atom, and the title stays.

    There are two steps, in this order. A long title keeps the key without its verb. A
    longer title takes all the cells of the atom. The title is what the user needs.
    """
    verbless = _bar("Trace — Hilltop-Repeater over a spec", "↑↓ move · Esc back")
    assert verbless.rstrip().endswith(" Esc")  # the verb went, but the key stayed
    assert "Esc back" not in verbless

    crowded = _bar("Trace — Hilltop-Repeater over a longer spec", "↑↓ move · Esc back")
    assert "Esc" not in crowded

    for bar in (verbless, crowded):
        assert "Hilltop-Repeater" in bar and cell_len(bar) <= 53


def test_bordered_frame_is_unchanged_on_regular() -> None:
    """Regular keeps its Panel. The borderless frame is the shape of the console, not of the app."""
    set_platform(REGULAR)
    composed = frame.compose_base(Text("hdr"), _screen(), "hint", 72, 24).split("\n")
    plain = [_plain(line) for line in composed]
    assert any("╭" in line or "┌" in line for line in plain), "regular keeps its Panel"


def test_picocalc_header_brands_the_app_not_the_device() -> None:
    """The PicoCalc header brands the app, not the device.

    This is the spec of JP for the ``header_atoms`` wiring. The picocalc-lyra platform shows
    ``MeshTerm vX``. The port of a soldered radio never changes, so the device segment gives
    no information. Regular keeps both.
    """
    from meshterm.ui.menu import _header_segments

    class _Badges:
        def unread_total(self) -> int:
            return 0

        def unacked_count(self) -> int:
            return 0

    class _Ctx:
        mock = True
        chat = _Badges()
        watchtower = _Badges()

    set_platform(PICOCALC_LYRA)
    text = "".join(seg.plain for seg in _header_segments(_Ctx(), {}))
    assert "MeshTerm" in text
    assert "simulator" not in text  # the device atom is not in the header
    assert "pulse" in PICOCALC_LYRA.header_atoms  # the sparkline uses the room that is left
    set_platform(REGULAR)
    text = "".join(seg.plain for seg in _header_segments(_Ctx(), {}))
    assert "MeshTerm" in text and "simulator" in text


def test_fkey_lane_resolution_and_banks() -> None:
    """The shared lane has some slots, and it leaves the other slots free.

    The pager takes F4 and F5. Each jump is on the Shift half of the pager that goes toward
    it. F1 to F3 stay free for the own verbs of the screen. Neither Enter nor Esc takes a
    slot.
    """
    lane = DEFAULT_LANE
    # F4 and F5 (paging, with no physical key at all) carry the pager. Each jump is on the
    # Shift half of the pager that goes toward it: Home behind Page ↑, End behind Page ↓.
    assert (
        PICOCALC_LYRA_DECK.action_for(lane, 4) == "pagedown"
        and PICOCALC_LYRA_DECK.action_for(lane, 9) == "end"
    )
    assert (
        PICOCALC_LYRA_DECK.action_for(lane, 5) == "pageup"
        and PICOCALC_LYRA_DECK.action_for(lane, 10) == "home"
    )
    # F1 to F3 are free on each screen. The shared lane has none of them.
    for number in (1, 2, 3, 6, 7, 8):
        assert PICOCALC_LYRA_DECK.action_for(lane, number) is None
    # Enter and Esc never use a slot, because both keys are already easy to reach.
    assert "enter" not in {PICOCALC_LYRA_DECK.action_for(lane, n) for n in range(1, 11)}
    assert "escape" not in {PICOCALC_LYRA_DECK.action_for(lane, n) for n in range(1, 11)}


def test_fkey_lane_text_fits_and_flips() -> None:
    """The lane fills exactly 53 cells in both banks, and a long label is cut, not wrapped.

    The directional pair rises toward its outer key. A free slot draws as a bare key number
    instead of an empty gap.
    """
    primary = PICOCALC_LYRA_DECK.lane_text(DEFAULT_LANE)
    shifted = PICOCALC_LYRA_DECK.lane_text(DEFAULT_LANE, shifted=True)
    assert cell_len(primary.plain) == 53 and cell_len(shifted.plain) == 53
    # A directional pair rises to the right: down and out on the left, up and in on the
    # right. Each companion keeps the end of the axis of its own slot.
    assert "F4 Page ↓" in primary.plain and "F5 Page ↑" in primary.plain
    assert "F9 Bottom" in shifted.plain and "10 Top" in shifted.plain
    # The free slots render as bare key numbers with no fill, in both banks.
    for number in (1, 2, 3):
        assert f"F{number}    " in primary.plain
        assert f"F{number + 5}    " in shifted.plain
    wide = [FPair("Muchtoolonglabel", "a", "Muchtoolonglabel", "b")] * 5
    assert cell_len(PICOCALC_LYRA_DECK.lane_text(wide).plain) == 53  # cut to the slot budget
    assert cell_len(PICOCALC_LYRA_DECK.lane_text(wide, shifted=True).plain) == 53


def test_fkey_slot_dims_when_its_action_is_unavailable() -> None:
    """A slot that is not available keeps its label and loses its fill.

    It never shows a dead key.
    """
    lane = [FPair("Paths", "paths", "Retry", "retry", enabled=False)] + [None] * 4
    primary = PICOCALC_LYRA_DECK.lane_text(lane)
    shifted = PICOCALC_LYRA_DECK.lane_text(lane, shifted=True)

    # The label stays: the lane still shows what F1 is for, but not that F1 acts now.
    assert "F1 Paths" in primary.plain and _slot_style(primary, 0) == "muted"
    # Its Shift companion does not change. The two banks are gated separately.
    assert "F6 Retry" in shifted.plain and _slot_style(shifted, 0) == "fkey.chip.shift"
    # The row is still exactly the width of the lane, with the dim chips.
    assert cell_len(primary.plain) == 53 and cell_len(shifted.plain) == 53
    # Dimming is only how the slot looks. The key still resolves, and the handler of the
    # screen does nothing.
    assert PICOCALC_LYRA_DECK.action_for(lane, 1) == "paths"


def test_default_lane_dims_every_nav_slot_when_nothing_moves() -> None:
    """A screen with nothing to move gets the shared lane with inactive slots, not no lane."""
    assert default_lane() is DEFAULT_LANE  # the live lane is the constant
    dim = default_lane(nav=False)

    assert [pair.label for pair in dim if pair] == ["Page ↓", "Page ↑"]
    assert not any(pair.enabled or pair.opp_enabled for pair in dim if pair)
    row = PICOCALC_LYRA_DECK.lane_text(dim)
    assert cell_len(row.plain) == 53 and "F4 Page ↓" in row.plain and "F5 Page ↑" in row.plain
    assert {str(span.style) for span in row.spans} == {"muted"}
    # Both banks dim together: the jump behind a pager that is not active is not active too.
    assert {str(span.style) for span in PICOCALC_LYRA_DECK.lane_text(dim, shifted=True).spans} == {
        "muted"
    }


def test_lane_is_built_after_the_body_renders() -> None:
    """The frame builds the lane later, so its gates read the metrics of this paint.

    The gates do not read the metrics of the last paint.
    """
    set_platform(PICOCALC_LYRA)
    screen = _screen()  # 40 body rows in a viewport of 23 rows: it overflows
    seen: dict[str, bool] = {}

    def build() -> Text:
        seen["nav"] = all(pair.enabled for pair in screen.picocalc_lyra_lane if pair)
        return PICOCALC_LYRA_DECK.lane_text(screen.picocalc_lyra_lane)

    composed = frame.compose_base(Text("hdr"), screen, "hint", 53, 26, footer_lane=build)

    assert seen["nav"] is True  # lit on the first paint, with no old paint to lag behind
    assert "F4 Page ↓" in _plain(composed.split("\n")[-1])


def test_scroll_screen_lane_tracks_whether_its_body_overflows() -> None:
    """The navigation slots of the result screen light only when there is more to scroll to."""
    screen = _screen()
    screen.note_metrics(4, 20)  # the whole body fits in the viewport
    assert not any(pair.enabled for pair in screen.picocalc_lyra_lane if pair)
    screen.note_metrics(80, 20)  # taller than the viewport
    assert all(pair.enabled for pair in screen.picocalc_lyra_lane if pair)


def test_select_lane_promotes_the_section_jumps_only_where_there_are_sections() -> None:
    """The keyboard has no PgUp key, so a grouped list puts Ctrl+PgUp/PgDn on the lane."""
    from meshterm.ui.tui.select import Choice, SelectScreen, Separator

    flat = SelectScreen("Flat", [Choice("one", 1), Choice("two", 2)])
    # There are no headings. This list has no sections, so the slots are empty instead of
    # dim, and F1 and F2 resolve to nothing.
    assert flat.picocalc_lyra_lane[0] is None and flat.picocalc_lyra_lane[1] is None
    assert PICOCALC_LYRA_DECK.action_for(flat.picocalc_lyra_lane, 1) is None

    grouped = SelectScreen(
        "Grouped",
        [
            Separator("── Near ──"),
            Choice("alpha", 1),
            Choice("beta", 2),
            Separator("── Far ──"),
            Choice("gamma", 3),
        ],
    )
    lane = grouped.picocalc_lyra_lane
    assert [pair.label for pair in lane[:2]] == ["Sect ↑", "Sect ↓"]
    # A pair rises toward its outer key: on this pair at the left edge, up takes F1.
    assert (
        PICOCALC_LYRA_DECK.action_for(lane, 1) == "ctrl_pageup"
        and PICOCALC_LYRA_DECK.action_for(lane, 2) == "ctrl_pagedown"
    )
    assert all(pair.enabled for pair in lane[:2])

    # A filter that reduces the list to one section keeps the labels and dims them. The list
    # still has sections, but there is no other section to jump to now.
    for ch in "gam":
        grouped.handle("text", ch)
    dim = grouped.picocalc_lyra_lane
    assert [pair.label for pair in dim[:2]] == ["Sect ↑", "Sect ↓"]
    assert not any(pair.enabled for pair in dim[:2])


def test_dialogs_draw_no_lane_at_all() -> None:
    """A prompt has nothing to page, so it shows bare key numbers and not a dim pager."""
    from meshterm.ui.tui.fkeys import EMPTY_LANE
    from meshterm.ui.tui.prompt import ButtonDialog, ConfirmScreen, TextScreen

    for screen in (
        TextScreen("Name", prompt="Call it what?"),
        ConfirmScreen("Sure?"),
        ButtonDialog("Reboot the node?", ["Cancel", "Reboot"]),
    ):
        assert list(screen.picocalc_lyra_lane) == list(EMPTY_LANE)
        row = PICOCALC_LYRA_DECK.lane_text(screen.picocalc_lyra_lane)
        assert cell_len(row.plain) == 53
        assert {str(span.style) for span in row.spans} == {"muted"}


def test_map_locate_is_reachable_from_both_control_keys() -> None:
    """``locate`` is bound as a chord in the whole app.

    Thus both Ctrl keys and F2 do the same action.

    The letter is the mnemonic of the action: ^Y for "you". Thus it is the same chord on each
    screen that can point to our node. It is not the initial of one screen.
    """
    from prompt_toolkit.keys import Keys

    from meshterm.ui.tui.session import _CTRL_LETTER_CHORDS, _KEY_ACTIONS

    assert _CTRL_LETTER_CHORDS["y"] == "locate"  # the half for the right-Ctrl rescue
    assert _KEY_ACTIONS[Keys.ControlY] == "locate"  # the ordinary binding, made from it


def test_host_battery_reads_the_sysfs_supply(tmp_path, monkeypatch) -> None:
    """The PicoCalc battery path gets the true percent and the charging flag from sysfs."""
    from meshterm.services import battery_service

    supply = tmp_path / "picocalc"
    supply.mkdir()
    (supply / "type").write_text("Battery\n")
    (supply / "capacity").write_text("53\n")
    (supply / "status").write_text("Discharging\n")
    (supply / "voltage_now").write_text("4012000\n")
    monkeypatch.setattr(battery_service, "_POWER_SUPPLIES", tmp_path)
    service = battery_service.BatteryService(ctx=None)
    asyncio.run(service._poll_host())
    reading = service.reading()
    assert reading is not None
    assert (reading.percent, reading.charging, reading.millivolts) == (53, False, 4012)

    (supply / "status").write_text("Charging\n")
    asyncio.run(service._poll_host())
    assert service.reading().charging is True

    monkeypatch.setattr(battery_service, "_POWER_SUPPLIES", tmp_path / "gone")
    for _ in range(battery_service._HOST_MISSES_KEPT + 1):
        asyncio.run(service._poll_host())
    assert service.reading() is None  # an unreadable supply is absent, so the header draws nothing


def test_dialog_wrap_shrinks_on_picocalc() -> None:
    """A dialog message wraps to fewer cells on the PicoCalc, because the display is narrower."""
    from meshterm.ui.surface import _dialog_wrap_cells

    set_platform(PICOCALC_LYRA)
    assert _dialog_wrap_cells() == 37  # 53 readable - 4 margin - 12 chrome
    set_platform(REGULAR)
    assert _dialog_wrap_cells() == 54  # 72 readable - 6 margin - 12 chrome


# --- the hint in the border of a dialog, and the lane one row below it -----------------


def _dialog(hint: str) -> ScrollScreen:
    """A floating screen with ``hint``, on the shared pager lane."""
    screen = ScrollScreen(Text("body"), title="Probe")
    screen._footer_hint = hint
    return screen


def test_a_dialog_keeps_its_hint_where_the_lane_is_the_footer() -> None:
    """This platform has no hint line, so the border of a floating box has the keys.

    The desktop platform does not draw the hint in the border, because the footer row below
    has the same words. On this platform the footer row is the lane. It shows only its five
    chips, so there is no other place for Enter, Esc, and the arrows.
    """
    screen = _dialog("↑↓ move · Enter select · Esc back")
    set_platform(REGULAR)
    assert frame._dialog_hint(screen) == ""
    set_platform(PICOCALC_LYRA)
    assert "Esc back" in _plain(frame.compose_dialog(screen, 53, 26))


def test_the_dialog_hint_drops_what_the_lane_already_says() -> None:
    """The hint does not repeat an atom when each key of the atom is a chip on the next row."""
    set_platform(PICOCALC_LYRA)
    screen = _dialog("↑↓ move · PgUp/PgDn scroll · Home/End ends · Enter select · Esc back")
    # The shared lane pages on F4 and F5, and it jumps to either end on their Shift halves.
    assert frame._dialog_hint(screen) == "↑↓ move · Enter select · Esc back"
    assert "PgUp" not in _plain(frame.compose_dialog(screen, 53, 26))


def test_the_dialog_hint_is_resolved_on_every_paint() -> None:
    """The dialog hint is never built one time into the hint of a screen.

    Both halves change while the screen is open. The packet viewer writes its hint again
    when the list that it pages through grows to more than one entry (JP, 2026-08-31). Each
    screen reads its lane again on each paint.
    """
    set_platform(PICOCALC_LYRA)
    screen = _dialog("Esc close")
    assert frame._dialog_hint(screen) == "Esc close"
    screen._footer_hint = "↑↓ newer/older · PgUp/PgDn scroll · Home/End ends · Esc close"
    assert frame._dialog_hint(screen) == "↑↓ newer/older · Esc close"


def test_an_atom_the_lane_only_half_covers_stands() -> None:
    """An atom that the lane covers only in part stays.

    A half truth is worse than the whole atom, so the atom is removed only when each key is
    a chip.
    """
    pager_only = (None, None, None, FPair("Page ↓", "pagedown"), FPair("Page ↑", "pageup"))
    assert strip_lane_atoms("PgUp/PgDn scroll", pager_only) == ""
    # This lane has no Shift bank, so Home/End are not on the lane.
    assert strip_lane_atoms("Home/End ends", pager_only) == "Home/End ends"
    # The atom also describes the arrows, and no slot has them.
    assert strip_lane_atoms("↑↓ PgUp/PgDn scroll", DEFAULT_LANE) == "↑↓ PgUp/PgDn scroll"
    # A second key in the verb half keeps the whole atom (region/you of the map).
    assert strip_lane_atoms("Home/^Y region/you", DEFAULT_LANE) == "Home/^Y region/you"


def test_a_dimmed_chip_still_covers_its_atom() -> None:
    """A dim chip means that the action exists but is not available now.

    The user already knows where the action is, so the chip still covers its atom.
    """
    assert strip_lane_atoms("PgUp/PgDn scroll", default_lane(nav=False)) == ""
