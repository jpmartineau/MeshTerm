# SPDX-License-Identifier: Apache-2.0
"""P4 chrome contracts: borderless frame, slim header, the F-key lane, dialog gate."""

from __future__ import annotations

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
    """The style the lane painted onto slot ``index``'s chip (slots are 9 cells + 2 gap)."""
    start = index * 11
    return next(str(span.style) for span in row.spans if span.start <= start < span.end)


def test_borderless_frame_swaps_the_panel_for_a_title_bar() -> None:
    """On the PicoCalc there is no Panel; a one-row title bar stands in for the whole border.

    It carries the rule, the title, and the clip arrow. No border column is spent, so the
    body starts at column 0 and owns all 53 cells.
    """
    set_platform(PICOCALC_LYRA)
    composed = frame.compose_base(Text("hdr"), _screen(), "hint", 53, 26).split("\n")
    assert len(composed) == 26
    plain = [_plain(line) for line in composed]
    # Row 1 (under the one-line header) is the title bar: rule + title + clip arrow.
    assert "Chrome probe" in plain[1] and "─" in plain[1]
    assert "↓" in plain[1]  # 40 rows into a 23-row viewport: more below
    # No Panel borders anywhere — the body owns the full width.
    assert not any("│" in line or "╭" in line for line in plain)
    assert all(cell_len(line) <= 53 for line in plain)
    # The body's first row starts at column 0 of row 2 (no panel padding).
    assert plain[2].startswith("row 0")


def _bar_text(
    title: str, hint: str, cols: int = 53, *, above: bool = False, below: bool = True
) -> Text:
    """The borderless title bar for a screen with ``title`` and ``hint``, as styled Text."""
    screen = ScrollScreen(Text("x"), title=title, floating=False)
    screen._footer_hint = hint
    return frame._title_bar(screen, cols, above, below)


def _bar(title: str, hint: str, cols: int = 53, *, below: bool = True) -> str:
    """The borderless title bar's plain text for a screen with ``title`` and ``hint``."""
    return _bar_text(title, hint, cols, below=below).plain


def _arrow_styles(bar: Text) -> tuple[str, str]:
    """The styles the bar painted on its two clip arrows, ``(up, down)``."""
    return tuple(
        next(
            str(span.style)
            for span in bar.spans
            if span.start <= index < span.end and span.end - span.start == 1
        )
        for index in (0, 1)
    )


def test_the_title_bar_says_how_to_leave_the_screen() -> None:
    """This platform has no footer hint line, so the way out rides the bar's tail.

    The F-key lane stands where the footer would be and only advertises what its slots do,
    which left Esc — the key every screen answers to, and the reason no screen carries a
    *Back* row — completely unadvertised (JP, 2026-08-30). It lands last, past the title,
    with the clip arrows holding the row's other end.
    """
    bar = _bar("Chrome probe", "↑↓ move · Enter open · Esc back")

    assert bar.startswith("↑↓ ")
    assert bar.rstrip().endswith("Esc back")
    assert "Chrome probe" in bar


def test_the_clip_arrows_are_always_drawn_and_say_it_in_colour() -> None:
    """Both arrows are fixed furniture at the row's left edge; colour reads the scroll.

    They used to appear and vanish, and a half-shown pair left a blank cell standing in
    for the missing one — the reader had to compare the row against a memory of itself
    (JP, 2026-08-31). Now the pair never moves and never changes width, and an arrow
    whose direction has more takes the border's own accent while one with nothing that way
    drops to muted: the F-key lane's live/dim language, one row up.
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
    # A body that fits whole keeps the pair, both dim — nothing appears or disappears.
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
    """Lifted off the screen's own footer hint, so each surface keeps its true verb."""
    assert _bar("Contacts", "↑↓ move · Enter open · Esc back").endswith("Esc back")
    assert _bar("Path width", "↑↓ move · Enter set · Esc keep").endswith("Esc keep")
    # A hint that names no way out advertises none — the lane's rule, one row up.
    assert "Esc" not in _bar("Working", "↑↓ move")


def test_the_main_menu_s_way_out_is_the_quit_chord_until_a_filter_stands() -> None:
    """Esc is inert at the menu, so the bar carries ^Q there — and Esc clear over it.

    The menu's hint ends on ``^Q quit?`` rather than an Esc clause (issue #22); the tail
    lifts whichever way out the hint ends on, so a typed filter's ``Esc clear`` wins while
    it stands, because that is the key that does something then.
    """
    title = "What would you like to do?"
    idle = "↑↓ move · type to filter · Enter select · ^Q quit?"
    assert _bar(title, idle).endswith(" ^Q quit?")
    assert _bar(title, f"{idle} · Esc clear").endswith(" Esc clear")
    # Crowded, it gives way the way Esc does: its bare key first, never a stray "Esc".
    verbless = _bar("Trace — Hilltop-Repeater over a spec", "↑↓ move · ^Q quit?")
    assert verbless.rstrip().endswith(" ^Q") and "Esc" not in verbless


def test_the_esc_hint_gives_way_before_it_crowds_the_title() -> None:
    """Compact by construction: the verb goes first, then the atom, and the title stays.

    Two rungs down, in order — a long title keeps the key without its verb, a longer one
    takes the cells back altogether. The title is what the reader came for.
    """
    verbless = _bar("Trace — Hilltop-Repeater over a spec", "↑↓ move · Esc back")
    assert verbless.rstrip().endswith(" Esc")  # the verb went, the key stayed
    assert "Esc back" not in verbless

    crowded = _bar("Trace — Hilltop-Repeater over a longer spec", "↑↓ move · Esc back")
    assert "Esc" not in crowded

    for bar in (verbless, crowded):
        assert "Hilltop-Repeater" in bar and cell_len(bar) <= 53


def test_bordered_frame_is_unchanged_on_regular() -> None:
    """Regular keeps its Panel — the borderless frame is the console's shape, not the app's."""
    set_platform(REGULAR)
    composed = frame.compose_base(Text("hdr"), _screen(), "hint", 72, 24).split("\n")
    plain = [_plain(line) for line in composed]
    assert any("╭" in line or "┌" in line for line in plain), "regular keeps its Panel"


def test_picocalc_header_brands_the_app_not_the_device() -> None:
    """The PicoCalc header brands the app, not the device.

    Per JP's spec for the ``header_atoms`` wiring: picocalc-lyra shows ``MeshTerm vX``,
    because a soldered radio's port never changes and so the device segment earns
    nothing. Regular keeps both.
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
    assert "simulator" not in text  # the device atom is composed out
    assert "pulse" in PICOCALC_LYRA.header_atoms  # the sparkline takes the remaining room
    set_platform(REGULAR)
    text = "".join(seg.plain for seg in _header_segments(_Ctx(), {}))
    assert "MeshTerm" in text and "simulator" in text


def test_fkey_lane_resolution_and_banks() -> None:
    """What the shared lane claims, and what it leaves alone.

    The pager takes F4/F5, each jump rides the Shift half of the pager heading for it,
    F1-F3 stay free for the screen's own verbs, and neither Enter nor Esc takes a slot.
    """
    lane = DEFAULT_LANE
    # F4/F5 (paging — no physical key at all) carry the pager, and each jump rides the
    # Shift half of the very pager heading for it: Home behind Page ↑, End behind Page ↓.
    assert (
        PICOCALC_LYRA_DECK.action_for(lane, 4) == "pagedown"
        and PICOCALC_LYRA_DECK.action_for(lane, 9) == "end"
    )
    assert (
        PICOCALC_LYRA_DECK.action_for(lane, 5) == "pageup"
        and PICOCALC_LYRA_DECK.action_for(lane, 10) == "home"
    )
    # F1-F3 are free on every screen — the shared lane claims none of them.
    for number in (1, 2, 3, 6, 7, 8):
        assert PICOCALC_LYRA_DECK.action_for(lane, number) is None
    # Enter/Esc never occupy a slot — both keys are already close at hand.
    assert "enter" not in {PICOCALC_LYRA_DECK.action_for(lane, n) for n in range(1, 11)}
    assert "escape" not in {PICOCALC_LYRA_DECK.action_for(lane, n) for n in range(1, 11)}


def test_fkey_lane_text_fits_and_flips() -> None:
    """The lane fills exactly 53 cells in both banks, and a long label is clipped, not wrapped.

    The directional pair rises toward its outer key, and a free slot draws as a bare
    key number rather than an empty gap.
    """
    primary = PICOCALC_LYRA_DECK.lane_text(DEFAULT_LANE)
    shifted = PICOCALC_LYRA_DECK.lane_text(DEFAULT_LANE, shifted=True)
    assert cell_len(primary.plain) == 53 and cell_len(shifted.plain) == 53
    # A directional pair rises to the right: down/out left, up/in right — and each
    # companion keeps its slot's end of that axis.
    assert "F4 Page ↓" in primary.plain and "F5 Page ↑" in primary.plain
    assert "F9 Bottom" in shifted.plain and "10 Top" in shifted.plain
    # The free bank renders as bare, unfilled key numbers in both banks.
    for number in (1, 2, 3):
        assert f"F{number}    " in primary.plain
        assert f"F{number + 5}    " in shifted.plain
    wide = [FPair("Muchtoolonglabel", "a", "Muchtoolonglabel", "b")] * 5
    assert cell_len(PICOCALC_LYRA_DECK.lane_text(wide).plain) == 53  # clipped to the slot budget
    assert cell_len(PICOCALC_LYRA_DECK.lane_text(wide, shifted=True).plain) == 53


def test_fkey_slot_dims_when_its_action_is_unavailable() -> None:
    """An unavailable slot keeps its label and drops its fill — it never offers a dead key."""
    lane = [FPair("Paths", "paths", "Retry", "retry", enabled=False)] + [None] * 4
    primary = PICOCALC_LYRA_DECK.lane_text(lane)
    shifted = PICOCALC_LYRA_DECK.lane_text(lane, shifted=True)

    # The label survives: the lane still says what F1 is for, just not that it acts now.
    assert "F1 Paths" in primary.plain and _slot_style(primary, 0) == "muted"
    # Its Shift companion is untouched — the two banks gate independently.
    assert "F6 Retry" in shifted.plain and _slot_style(shifted, 0) == "fkey.chip.shift"
    # The row is still exactly the lane's width, dim chips and all.
    assert cell_len(primary.plain) == 53 and cell_len(shifted.plain) == 53
    # Dimming is presentational: the key still resolves, and the screen's handler no-ops.
    assert PICOCALC_LYRA_DECK.action_for(lane, 1) == "paths"


def test_default_lane_dims_every_nav_slot_when_nothing_moves() -> None:
    """A screen with nothing to move gets the shared lane back inert, not absent."""
    assert default_lane() is DEFAULT_LANE  # the live lane is the constant itself
    dim = default_lane(nav=False)

    assert [pair.label for pair in dim if pair] == ["Page ↓", "Page ↑"]
    assert not any(pair.enabled or pair.opp_enabled for pair in dim if pair)
    row = PICOCALC_LYRA_DECK.lane_text(dim)
    assert cell_len(row.plain) == 53 and "F4 Page ↓" in row.plain and "F5 Page ↑" in row.plain
    assert {str(span.style) for span in row.spans} == {"muted"}
    # Both banks dim together: the jump behind a dead pager is just as dead.
    assert {str(span.style) for span in PICOCALC_LYRA_DECK.lane_text(dim, shifted=True).spans} == {
        "muted"
    }


def test_lane_is_built_after_the_body_renders() -> None:
    """The frame defers the lane, so its gates read this paint's metrics, not the last one's."""
    set_platform(PICOCALC_LYRA)
    screen = _screen()  # 40 body rows into a 23-row viewport: it overflows
    seen: dict[str, bool] = {}

    def build() -> Text:
        seen["nav"] = all(pair.enabled for pair in screen.picocalc_lyra_lane if pair)
        return PICOCALC_LYRA_DECK.lane_text(screen.picocalc_lyra_lane)

    composed = frame.compose_base(Text("hdr"), screen, "hint", 53, 26, footer_lane=build)

    assert seen["nav"] is True  # lit on the very first paint, with no stale frame to lag
    assert "F4 Page ↓" in _plain(composed.split("\n")[-1])


def test_scroll_screen_lane_tracks_whether_its_body_overflows() -> None:
    """The result window's nav slots light only once there is something to scroll to."""
    screen = _screen()
    screen.note_metrics(4, 20)  # the whole body fits the viewport
    assert not any(pair.enabled for pair in screen.picocalc_lyra_lane if pair)
    screen.note_metrics(80, 20)  # taller than the viewport
    assert all(pair.enabled for pair in screen.picocalc_lyra_lane if pair)


def test_select_lane_promotes_the_section_jumps_only_where_there_are_sections() -> None:
    """Ctrl+PgUp/PgDn is unreachable on a keyboard with no PgUp, so a grouped list lanes it."""
    from meshterm.ui.tui.select import Choice, SelectScreen, Separator

    flat = SelectScreen("Flat", [Choice("one", 1), Choice("two", 2)])
    # No headings anywhere: sections are not a thing on this list, so the slots are empty
    # rather than dim — and F1/F2 resolve to nothing at all.
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
    # A pair rises toward its outer key: on this left-edge pair, up takes F1.
    assert (
        PICOCALC_LYRA_DECK.action_for(lane, 1) == "ctrl_pageup"
        and PICOCALC_LYRA_DECK.action_for(lane, 2) == "ctrl_pagedown"
    )
    assert all(pair.enabled for pair in lane[:2])

    # A filter that collapses the list onto one section keeps the labels and dims them:
    # the sections are still a thing here, they just have nowhere to jump right now.
    for ch in "gam":
        grouped.handle("text", ch)
    dim = grouped.picocalc_lyra_lane
    assert [pair.label for pair in dim[:2]] == ["Sect ↑", "Sect ↓"]
    assert not any(pair.enabled for pair in dim[:2])


def test_dialogs_draw_no_lane_at_all() -> None:
    """A prompt has nothing to page, so it shows bare key numbers, not a dim pager."""
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
    """``locate`` is bound as a chord app-wide, so both Ctrl keys and F2 reach the same action.

    The letter is the mnemonic of the action — ^Y for *you* — so it is the same chord on
    every screen that can point at our own node, not one screen's initial.
    """
    from prompt_toolkit.keys import Keys

    from meshterm.ui.tui.session import _CTRL_LETTER_CHORDS, _KEY_ACTIONS

    assert _CTRL_LETTER_CHORDS["y"] == "locate"  # the right-Ctrl rescue's half
    assert _KEY_ACTIONS[Keys.ControlY] == "locate"  # the ordinary binding, generated from it


def test_host_battery_reads_the_sysfs_supply(tmp_path, monkeypatch) -> None:
    """The PicoCalc battery path: a true percent and charging flag straight from sysfs."""
    from meshterm.services import battery_service

    (tmp_path / "capacity").write_text("53\n")
    (tmp_path / "status").write_text("Discharging\n")
    (tmp_path / "voltage_now").write_text("4012000\n")
    monkeypatch.setattr(battery_service, "_HOST_SUPPLY", tmp_path)
    service = battery_service.BatteryService(ctx=None)
    service._poll_host()
    reading = service.reading()
    assert reading is not None
    assert (reading.percent, reading.charging, reading.millivolts) == (53, False, 4012)

    (tmp_path / "status").write_text("Charging\n")
    service._poll_host()
    assert service.reading().charging is True

    monkeypatch.setattr(battery_service, "_HOST_SUPPLY", tmp_path / "gone")
    service._poll_host()
    assert service.reading() is None  # unreadable supply = absent, header draws nothing


def test_dialog_wrap_shrinks_on_picocalc() -> None:
    """A dialog message wraps to fewer cells on the PicoCalc, the screen being narrower."""
    from meshterm.ui.surface import _dialog_wrap_cells

    set_platform(PICOCALC_LYRA)
    assert _dialog_wrap_cells() == 37  # 53 readable - 4 margin - 12 chrome
    set_platform(REGULAR)
    assert _dialog_wrap_cells() == 54  # 72 readable - 6 margin - 12 chrome


# --- the dialog border's hint, against the lane one row below it ------------------------


def _dialog(hint: str) -> ScrollScreen:
    """A floating screen carrying ``hint``, on the shared pager lane."""
    screen = ScrollScreen(Text("body"), title="Probe")
    screen._footer_hint = hint
    return screen


def test_a_dialog_keeps_its_hint_where_the_lane_is_the_footer() -> None:
    """There is no hint line here, so a floating box's border is the only place keys read.

    The desktop drops the border hint entirely (the footer row below repeats it word for
    word); this platform's footer row is the lane, which speaks only for its five chips,
    so Enter, Esc and the arrows have nowhere else to be said.
    """
    screen = _dialog("↑↓ move · Enter select · Esc back")
    set_platform(REGULAR)
    assert frame._dialog_hint(screen) == ""
    set_platform(PICOCALC_LYRA)
    assert "Esc back" in _plain(frame.compose_dialog(screen, 53, 26))


def test_the_dialog_hint_drops_what_the_lane_already_says() -> None:
    """An atom whose every key is a chip on the very next row is the same claim twice."""
    set_platform(PICOCALC_LYRA)
    screen = _dialog("↑↓ move · PgUp/PgDn scroll · Home/End ends · Enter select · Esc back")
    # The shared lane pages on F4/F5 and jumps to either end behind their Shift halves.
    assert frame._dialog_hint(screen) == "↑↓ move · Enter select · Esc back"
    assert "PgUp" not in _plain(frame.compose_dialog(screen, 53, 26))


def test_the_dialog_hint_is_resolved_on_every_paint() -> None:
    """Never folded into a screen's hint once: both halves move while the screen is up.

    The packet viewer rewrites its own hint as the list it pages through grows past one
    entry (JP, 2026-08-31), and every screen reads its lane fresh each frame.
    """
    set_platform(PICOCALC_LYRA)
    screen = _dialog("Esc close")
    assert frame._dialog_hint(screen) == "Esc close"
    screen._footer_hint = "↑↓ newer/older · PgUp/PgDn scroll · Home/End ends · Esc close"
    assert frame._dialog_hint(screen) == "↑↓ newer/older · Esc close"


def test_an_atom_the_lane_only_half_covers_stands() -> None:
    """Half a truth is worse than the whole atom: it survives unless every key is a chip."""
    pager_only = (None, None, None, FPair("Page ↓", "pagedown"), FPair("Page ↑", "pageup"))
    assert strip_lane_atoms("PgUp/PgDn scroll", pager_only) == ""
    # No Shift bank here, so Home/End are nowhere on the lane.
    assert strip_lane_atoms("Home/End ends", pager_only) == "Home/End ends"
    # The atom documents the arrows too, and no slot ever claims those.
    assert strip_lane_atoms("↑↓ PgUp/PgDn scroll", DEFAULT_LANE) == "↑↓ PgUp/PgDn scroll"
    # A second key riding in the verb half keeps the atom whole (the map's region/you).
    assert strip_lane_atoms("Home/^Y region/you", DEFAULT_LANE) == "Home/^Y region/you"


def test_a_dimmed_chip_still_covers_its_atom() -> None:
    """Dim says *a thing here, just not right now* — the reader has been told where it is."""
    assert strip_lane_atoms("PgUp/PgDn scroll", default_lane(nav=False)) == ""
