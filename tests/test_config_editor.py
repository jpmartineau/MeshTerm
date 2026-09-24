# SPDX-License-Identifier: Apache-2.0
"""Tests for the overhauled config editor: dialogs, staging, and the location picker.

The pure pieces (the contact share URL, coordinate parsing, the typed-confirmation
dialog, the map picker screen) are exercised directly; the editor's flows run end-to-end
against the :class:`MockDevice` simulator through a scripted fake UI surface that answers
each prompt from a queue.
"""

from __future__ import annotations

import io
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core.admin_store import AdminStore
from meshterm.core.config import Settings
from meshterm.core.device_store import DeviceStore
from meshterm.persistence.repository import Repository
from meshterm.platforms import PICOCALC, REGULAR, set_platform
from meshterm.ui.config_editor import (
    _ConfigMenu,
    _menu_items,
    config_table,
    contact_share_url,
    edit_config,
    has_pin,
    send_advert,
)
from meshterm.ui.device_info_screen import REVEAL_KEY, DeviceInfoScreen
from meshterm.ui.marks import MASK_MARK
from meshterm.ui.theme import active_theme
from meshterm.ui.tui.prompt import TypedConfirmDialog
from meshterm.ui.tui.screen import CANCEL
from tests.conftest import plain as _plain  # THE strip-and-join screen reader

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class _Fut:
    """A minimal future stand-in so screens can resolve without an event loop."""

    def __init__(self) -> None:
        self.value: Any = None
        self._done = False

    def done(self) -> bool:
        return self._done

    def set_result(self, value: Any) -> None:
        self.value = value
        self._done = True


# -- the config table's column allocation ---------------------------------------

#: A snapshot carrying one of every field the table draws, including the values that push
#: each lane widest: the longest setting label and the longest formatted value.
_SNAPSHOT = {
    "name": "Homestead-Hub",
    "adv_lat": 45.5017,
    "adv_lon": -73.5673,
    "ble_pin": 123456,
    "radio_freq": 869525,
    "radio_bw": 250,
    "radio_sf": 11,
    "radio_cr": 5,
    "tx_power": 22,
    "max_tx_power": 30,
    "airtime_factor": 1.0,
    "rx_delay": 0.0,
    "manual_add_contacts": 0,
    "autoadd_config": 0,
    "flood_scope": "",
    "adv_loc_policy": 1,
    "multi_acks": 0,
    "telemetry_mode_base": 1,
    "telemetry_mode_loc": 0,
    "telemetry_mode_env": 0,
    "path_hash_mode": 1,
}


def _table_lines(width: int, **kwargs: Any) -> list[str]:
    """The config table's rendered lines at ``width`` columns."""
    console = Console(width=width, file=io.StringIO(), theme=active_theme(), legacy_windows=False)
    with console.capture() as capture:
        console.print(config_table(_SNAPSHOT, {}, **kwargs))
    return _plain(capture.get()).rstrip("\n").split("\n")


def test_config_table_gives_the_description_lane_the_widest_share() -> None:
    """DESCRIPTION takes the widest lane, because it carries the widest content.

    Sizing the other two lanes to their content and leaving DESCRIPTION the remainder
    gave it 19 of the 72 cells, and every explanation wrapped three deep.
    """
    lines = _table_lines(72)
    header = next(line for line in lines if line.lstrip().startswith("SETTING"))
    setting = header.index("CURRENT")
    current = header.index("DESCRIPTION") - setting
    description = 72 - header.index("DESCRIPTION")
    assert description >= setting > current
    assert description >= 26


def test_config_table_keeps_every_row_within_three_lines() -> None:
    """No setting's explanation sprawls further than three lines at the readable width.

    That is the whole point of the reallocation.
    """
    lines = _table_lines(72)
    body = [line for line in lines if line.strip() and not line.startswith("──")]
    runs, run = [], 0
    for line in body:
        if line.startswith("  ") and not line.startswith("    "):
            runs.append(run)
            run = 1
        else:
            run += 1
    runs.append(run)
    assert max(runs) <= 3


def test_config_table_drops_the_description_lane_where_it_cannot_fit() -> None:
    """At 53 columns the description lane is dropped rather than squeezed.

    The setting and its value are all that fit, and with nothing competing for the
    cells the labels keep their natural width.
    """
    set_platform(PICOCALC)
    try:
        lines = _table_lines(PICOCALC.readable_cols)
    finally:
        set_platform(REGULAR)
    header = next(line for line in lines if line.lstrip().startswith("SETTING"))
    assert "DESCRIPTION" not in header
    assert any("Telemetry mode (environment)" in line for line in lines)
    assert max(len(line.rstrip()) for line in lines) <= PICOCALC.readable_cols


def test_config_table_drops_the_description_lane_beside_a_staged_column() -> None:
    """A staged column costs the description lane too: three narrow lanes leave no room."""
    lines = _table_lines(72, pending={"tx_power": 14})
    header = next(line for line in lines if line.lstrip().startswith("SETTING"))
    assert "STAGED" in header and "DESCRIPTION" not in header


# -- the concealed pairing PIN --------------------------------------------------


class _StubSession:
    """A minimal session: the screen only ever asks it to repaint."""

    def __init__(self) -> None:
        self.repaints = 0

    def invalidate(self) -> None:
        self.repaints += 1


def _pin_row(rendered) -> str:  # noqa: ANN001 - lines or an already-joined render
    """The Device PIN row out of a rendered table or screen body."""
    return next(line for line in _plain(rendered).split("\n") if "Device PIN" in line)


def _info_screen(snapshot: dict) -> DeviceInfoScreen:
    return DeviceInfoScreen(
        _StubSession(),
        lambda reveal: config_table(snapshot, {}, reveal_pin=reveal),
        title="Device info",
        conceals=has_pin(snapshot),
    )


def test_the_pin_is_bullets_until_it_is_asked_for() -> None:
    """The PIN reads as bullets until something asks for it.

    One bullet per digit, in the app's own mask glyph — the one a password prompt types.
    """
    masked = _pin_row(_table_lines(72))
    assert f"{MASK_MARK * 6}" in masked
    assert "123456" not in masked
    assert "123456" in _pin_row(_table_lines(72, reveal_pin=True))


def test_the_mask_shows_the_field_not_the_pins_length() -> None:
    """The mask draws the field's width, not the PIN's.

    A one-digit PIN behind a single bullet would tell you how many digits to guess —
    and would read as a stray dot besides.
    """
    short = {**_SNAPSHOT, "ble_pin": 0}
    console = Console(width=72, file=io.StringIO(), theme=active_theme(), legacy_windows=False)
    with console.capture() as capture:
        console.print(config_table(short, {}))
    assert MASK_MARK * 6 in _pin_row(capture.get())


def test_a_device_with_no_pin_has_nothing_to_conceal() -> None:
    """A device with no PIN shows ``?`` unmasked.

    An absence is not a secret, and masking it would claim there is something behind it.
    """
    snapshot = {k: v for k, v in _SNAPSHOT.items() if k != "ble_pin"}
    console = Console(width=72, file=io.StringIO(), theme=active_theme(), legacy_windows=False)
    with console.capture() as capture:
        console.print(config_table(snapshot, {}))
    assert "?" in _pin_row(_plain(capture.get()).split("\n"))
    assert not has_pin(snapshot)


def test_the_chord_uncovers_the_pin_and_puts_it_back() -> None:
    """The reveal chord uncovers the PIN, and the same key puts it away again."""
    screen = _info_screen(_SNAPSHOT)
    assert MASK_MARK in _pin_row(screen.render_body(72))

    screen.handle("reveal")
    assert "123456" in _pin_row(screen.render_body(72))

    screen.handle("reveal")  # the same key puts it away again
    assert MASK_MARK in _pin_row(screen.render_body(72))


def test_the_lane_chip_and_the_key_are_one_behaviour() -> None:
    """The PicoCalc's lane chip runs the action the letter runs, and relabels with it.

    One behaviour reached two ways, not a second copy of it.
    """
    screen = _info_screen(_SNAPSHOT)
    assert screen.fkey_lane[2].label == "Reveal"

    screen.handle("reveal")
    assert "123456" in _pin_row(screen.render_body(72))
    assert screen.fkey_lane[2].label == "Hide"


def test_the_footer_names_what_the_press_would_do_now() -> None:
    """The footer says what the key would do now: show while hidden, hide while shown."""
    screen = _info_screen(_SNAPSHOT)
    assert f"{REVEAL_KEY} show PIN" in screen.footer_hint
    screen.handle("reveal")
    assert f"{REVEAL_KEY} hide PIN" in screen.footer_hint


def test_a_page_with_no_pin_advertises_no_key_for_it() -> None:
    """A page with no PIN advertises no key for it, in the footer or on the lane.

    The standing rule: a footer never names a key that would do nothing, and a lane
    slot for an action this screen does not have stays empty rather than dim.
    """
    screen = _info_screen({k: v for k, v in _SNAPSHOT.items() if k != "ble_pin"})
    assert "PIN" not in screen.footer_hint
    assert screen.fkey_lane[2] is None
    screen.handle("reveal")
    assert screen.fkey_lane[2] is None


def test_uncovering_the_pin_keeps_the_reader_where_they_were() -> None:
    """Uncovering the PIN leaves the scroll where the reader put it.

    Nothing reflows — six cells become six cells — so nothing should move.
    """
    screen = _info_screen(_SNAPSHOT)
    screen.note_viewport(10)
    screen.render_body(72)
    screen.scroll_lines(8)
    where = screen.scroll
    screen.handle("reveal")
    assert screen.scroll == where
    assert len(screen.render_body(72)) == len(_table_lines(72))


# -- the editor's copy of the same secret ---------------------------------------


def _editor(snapshot: dict, pending: dict | None = None) -> _ConfigMenu:
    pending = {} if pending is None else pending
    return _ConfigMenu(
        _StubSession(),
        lambda reveal: _menu_items(snapshot, pending, len(pending), reveal),
        conceals=has_pin(snapshot),
        footer_hint="↑↓ move · type to filter · Enter select · Esc back",
    )


def test_the_editor_masks_the_pin_row_too() -> None:
    """The editor conceals the same value on the same terms as the page next door.

    Otherwise the reader only has to walk one menu over to undo it.
    """
    menu = _editor(_SNAPSHOT)
    assert MASK_MARK in _pin_row(menu.render_body(72))
    assert "123456" not in _pin_row(menu.render_body(72))


def test_a_staged_pin_is_concealed_as_well_as_the_saved_one() -> None:
    """A PIN typed a moment ago is concealed too, staged and saved alike.

    A row that uncovered itself the instant it was edited would leave the secret up
    for the rest of the staging session.
    """
    menu = _editor(_SNAPSHOT, {"device_pin": 424242})
    row = _pin_row(menu.render_body(72))
    assert f"{MASK_MARK * 6} → {MASK_MARK * 6}" in row
    menu.handle("reveal")
    assert "123456 → 424242" in _pin_row(menu.render_body(72))


def test_the_editor_reveals_on_the_same_chord_as_the_info_page() -> None:
    """The editor reveals on the same chord as the info page: one key for the concept.

    It has to be a chord rather than a bare letter, because the editor's letters are
    find-as-you-type.
    """
    menu = _editor(_SNAPSHOT)
    assert f"{REVEAL_KEY} show PIN" in menu.footer_hint
    assert menu.fkey_lane[2].label == "Reveal"

    menu.handle("reveal")
    assert "123456" in _pin_row(menu.render_body(72))
    assert f"{REVEAL_KEY} hide PIN" in menu.footer_hint
    assert menu.fkey_lane[2].label == "Hide"


def test_revealing_keeps_the_filter_and_the_row_the_reader_was_on() -> None:
    """Revealing keeps the typed filter and the row the reader had picked.

    The rows are data, so the toggle refreshes them in place rather than rebuilding
    the screen under a reader who had narrowed it.
    """
    menu = _editor(_SNAPSHOT)
    for ch in "telemetry":
        menu.handle("text", ch)
    menu.handle("down")
    before = menu._current_choice()

    menu.handle("reveal")
    assert menu._filter == "telemetry"
    assert menu._current_choice().value == before.value


def test_an_editor_over_a_device_with_no_pin_offers_no_reveal() -> None:
    """An editor over a device with no PIN offers no reveal either, footer or lane."""
    menu = _editor({k: v for k, v in _SNAPSHOT.items() if k != "ble_pin"})
    assert "PIN" not in menu.footer_hint
    assert menu.fkey_lane[2] is None


# -- contact share URL ----------------------------------------------------------


def test_contact_share_url_matches_meshcore_format() -> None:
    """The contact card URL carries the encoded name, hex key, and node type."""
    url = contact_share_url("Base Camp", "AB" * 32, node_type=2)
    assert url.startswith("meshcore://contact/add?")
    assert "name=Base%20Camp" in url
    assert f"public_key={'ab' * 32}" in url  # key is normalized to lowercase
    assert url.endswith("&type=2")


def test_contact_share_url_defaults_to_companion_type() -> None:
    """With no explicit type the card marks the node as a companion (type 1)."""
    assert contact_share_url("n", "00" * 32).endswith("&type=1")


async def test_show_contact_card_is_the_qr_over_the_link_on_a_bare_frame() -> None:
    """The contact card is a QR code over its link, and it takes the whole screen.

    A scannable code above the raw ``meshcore://`` link, on a bare frame — no header,
    footer, title or box competes with the code for a camera's attention.
    """
    from types import SimpleNamespace

    from meshterm.ui.config_editor import show_contact_card
    from meshterm.ui.qr import QrScreen
    from meshterm.ui.surface import TuiUi

    pushed: list = []

    async def run_screen(screen: Any) -> None:
        pushed.append(screen)

    ui = TuiUi(SimpleNamespace(run_screen=run_screen))
    await show_contact_card(SimpleNamespace(ui=ui), "Hub", "AB" * 32, node_type=2)
    (screen,) = pushed
    assert isinstance(screen, QrScreen)
    assert screen.title == "Share Hub"
    assert screen.bare and not screen.floating, "a QR code is full-screen, never a popup"
    screen.note_viewport(60)
    out = _plain(screen.render_body(200))  # wide: the link never wraps
    assert f"meshcore://contact/add?name=Hub&public_key={'ab' * 32}&type=2" in out
    assert "█" in out  # the QR actually drew its modules


# -- the map pick's opening spot --------------------------------------------------


def test_the_map_opens_on_a_stored_position_and_on_the_mesh_without_one() -> None:
    """Floats or CLI strings open the picker there; no fix (0, 0) or no value frames the mesh."""
    from meshterm.ui.map_screen import coords_or_none

    assert coords_or_none(45.5, -73.6) == (45.5, -73.6)
    assert coords_or_none("45.5", "-73.6") == (45.5, -73.6)
    assert coords_or_none(0.0, 0.0) is None
    assert coords_or_none("0", "0") is None
    assert coords_or_none(None, -73.6) is None
    assert coords_or_none("here", "there") is None


# -- the typed-confirmation dialog ------------------------------------------------


def test_typed_confirm_requires_the_exact_word() -> None:
    """Enter only commits once the confirmation word has been typed."""
    dialog = TypedConfirmDialog("This erases everything.", "RESET")
    dialog.future = _Fut()

    dialog.handle("enter")  # empty field: nudge, no resolve
    assert not dialog.future.done()
    assert "doesn't match" in _plain(dialog.render_body(60))

    for ch in "nope":
        dialog.handle("text", ch)
    dialog.handle("enter")
    assert not dialog.future.done()

    for _ in "nope":
        dialog.handle("backspace")
    for ch in "RESET":
        dialog.handle("text", ch)
    dialog.handle("enter")
    assert dialog.future.done() and dialog.future.value is True


def test_typed_confirm_is_case_insensitive_and_cancellable() -> None:
    """A lowercase match still confirms (the friction is the word); Esc cancels."""
    dialog = TypedConfirmDialog("Danger.", "IMPORT")
    dialog.future = _Fut()
    for ch in "import":
        dialog.handle("text", ch)
    dialog.handle("enter")
    assert dialog.future.value is True

    cancelled = TypedConfirmDialog("Danger.", "IMPORT")
    cancelled.future = _Fut()
    cancelled.handle("escape")
    assert cancelled.future.value is CANCEL


def test_typed_confirm_renders_warning_and_instruction() -> None:
    """The dialog shows the consequence and names the word to type."""
    dialog = TypedConfirmDialog("This erases ALL data.", "RESET", title="⚠ Factory reset")
    body = _plain(dialog.render_body(60))
    assert "This erases ALL data." in body
    assert "Type RESET to confirm" in body
    assert dialog.border_style == "err"


# -- the map location picker -------------------------------------------------------


class _StubSession:
    """Minimal stand-in for :class:`TuiSession` for driving the picker screen."""

    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        self._cols, self._rows = cols, rows

    def base_body_size(self) -> tuple[int, int]:
        return self._cols, self._rows

    def invalidate(self) -> None:
        pass


class _StubSource:
    """An always-offline tile source, so the picker renders nodes only (no network)."""

    available = False
    max_zoom = 14

    def load_tile(self, z: int, x: int, y: int):
        return None

    def answered_empty(self, z: int, x: int, y: int) -> bool:
        return False  # offline is silence, never the source saying "nothing there"


def _picker(markers=None, initial=None):
    from meshterm.ui.map_screen import LocationPickScreen

    screen = LocationPickScreen(_StubSession(), markers or [], _StubSource(), 14, initial=initial)
    screen.future = _Fut()
    return screen


def test_location_picker_opens_on_the_initial_spot_and_commits_the_centre() -> None:
    """The picker centres on the node's current location and Enter resolves it."""
    screen = _picker(initial=(45.5, -73.6))
    screen.render_body(80)
    assert screen._viewport.center_lat == pytest.approx(45.5)
    assert screen._viewport.center_lon == pytest.approx(-73.6)
    assert screen._viewport.zoom == 13

    screen.handle("enter")
    lat, lon = screen.future.value
    assert lat == pytest.approx(45.5) and lon == pytest.approx(-73.6)


def test_location_picker_moves_the_crosshair_and_resets_to_initial() -> None:
    """Panning moves the committed point; ``Home`` returns to the starting location."""
    screen = _picker(initial=(45.5, -73.6))
    screen.render_body(80)
    screen.handle("right")  # pan east
    screen.handle("down")  # pan south
    screen.handle("enter")
    lat, lon = screen.future.value
    assert lon > -73.6 and lat < 45.5

    screen.handle("home")  # back to the initial spot
    assert screen._viewport.center_lat == pytest.approx(45.5)
    assert screen._viewport.center_lon == pytest.approx(-73.6)

    # Typing does nothing here — the picker has no find filter to feed.
    view = screen._viewport
    screen.handle("text", "d")
    assert screen._viewport is view and screen._filter == ""


def test_location_picker_draws_a_live_crosshair_without_keeping_it() -> None:
    """The centre crosshair (with live coordinates) renders each frame but is transient."""
    from meshterm.ui.map_render import MapMarker

    markers = [MapMarker("Repeater", 45.51, -73.61, is_repeater=True)]
    screen = _picker(markers=markers, initial=(45.5, -73.6))
    body = _plain(screen.render_body(80))
    assert "⌖" in body and "45.5" in body  # crosshair + its live coordinates
    assert screen._markers == markers  # not accumulated across frames
    assert "Set location" in screen._title(screen._viewport)


async def test_location_picker_keeps_the_crosshair_when_the_basemap_lands(
    monkeypatch,  # noqa: ANN001
) -> None:
    """The finished ground raster must carry the crosshair, not paint over it.

    The crosshair lives for exactly one ``render_body`` call, while the real raster runs on
    a thread and lands much later. Reading the marker list back at draw time found it gone,
    so every basemap frame arrived crosshair-less and erased the pick the moment the
    streets appeared (JP, 2026-08-13).
    """
    from meshterm.ui import map_screen as ms

    monkeypatch.setattr(ms, "_loop_running", lambda: True)
    coros: list = []
    monkeypatch.setattr(ms.asyncio, "ensure_future", coros.append)

    screen = _picker(initial=(45.5, -73.6))
    screen.render_body(80)  # paints now, schedules the ground raster
    assert coros, "no background raster was scheduled"

    await coros[-1]  # the raster finishes — long after the crosshair was taken back
    # Every other coroutine the paint scheduled (the tile ``_load``) is collected, never
    # awaited, so close them here: an un-awaited coroutine warns at collection time, and a
    # RuntimeWarning from a passing test is noise the next real one hides behind.
    for pending in coros[:-1]:
        pending.close()
    assert screen._frame is not None
    assert "⌖" in _plain(screen._frame), "the basemap landed over the crosshair"
    # And the finished frame is what the next paint serves, crosshair intact.
    assert "⌖" in _plain(screen.render_body(80))
    assert screen._markers == [], "the crosshair leaked into the marker list"


def test_location_picker_without_nodes_or_initial_shows_the_world() -> None:
    """With nothing to frame the picker opens on a world view rather than crashing."""
    screen = _picker()
    screen.render_body(80)
    assert screen._viewport.zoom == 2
    screen.handle("home")  # reset with nothing to fit stays on the world view
    assert screen._viewport.zoom == 2
    screen.handle("escape")
    assert screen.future.value is None


def test_nodes_that_arrive_late_join_the_open_map() -> None:
    """``set_markers`` replaces the plotted nodes and redraws, without moving the view."""
    from meshterm.ui.map_render import MapMarker

    screen = _picker(initial=(45.5, -73.6))
    screen.render_body(80)
    view = screen._viewport
    screen.set_markers([MapMarker("Repeater", 45.51, -73.61, is_repeater=True)])
    screen.render_body(80)
    assert screen._viewport is view
    assert screen._frame_key[3] == 2  # redrawn for the new node, crosshair included


async def test_markers_without_waiting_never_ask_the_radio(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The picker's markers come from what is in hand; the radio is never asked for them."""
    from meshterm.services.markers import gather_markers

    device = await ctx.device()

    async def _refuse(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("the radio was asked")

    monkeypatch.setattr(device, "get_contacts", _refuse)
    monkeypatch.setattr(device, "get_self_info", _refuse)
    assert await gather_markers(ctx, wait=False) == []  # nothing cached, nothing heard yet


async def test_the_picker_opens_while_the_radio_is_still_reading_contacts(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contacts read that hasn't answered doesn't hold the map closed; its nodes join later.

    Regression: a companion refusing the contacts read (``ERR_CODE_BAD_STATE``) kept the
    picker from opening through every retry of it — twenty-odd seconds.
    """
    import asyncio

    from meshterm.core.models import Contact
    from meshterm.ui import map_screen

    device = await ctx.device()
    answer = asyncio.Event()

    async def _slow_contacts() -> list:
        await answer.wait()
        return [
            Contact(
                name="Hilltop", public_key="ab" * 32, key_prefix="abababab", lat=45.51, lon=-73.61
            )
        ]

    monkeypatch.setattr(device, "get_contacts", _slow_contacts)

    class _Session:
        def base_body_size(self) -> tuple[int, int]:
            return (72, 20)

        def invalidate(self) -> None:
            pass

        def request_full_repaint(self) -> None:
            pass

        async def run_screen(self, screen: Any) -> Any:
            self.opened_with = len(screen._markers)  # the map is up; the radio hasn't answered
            answer.set()
            for _ in range(200):
                await asyncio.sleep(0)
                if len(screen._markers) > self.opened_with:
                    break
            self.later = len(screen._markers)
            return (45.5, -73.6)

    class _Ui:
        def __init__(self, session: _Session) -> None:
            self.session = session

    session = _Session()
    monkeypatch.setattr("meshterm.ui.surface.TuiUi", _Ui)
    monkeypatch.setattr(map_screen, "basemap_source", lambda _ctx: _StubSource())
    ctx.ui = _Ui(session)  # type: ignore[assignment]
    assert await map_screen.pick_location(ctx) == (45.5, -73.6)
    assert session.opened_with == 0
    assert session.later == 1


# -- the editor's flows against the simulator ---------------------------------------


class _FakeSession:
    """A minimal stand-in for the TUI session behind the editor's persistent menu.

    The editor keeps its menu *pushed on the session stack* for the whole visit (so its
    sub-prompts float over it as modal popups), driving it one round at a time through
    ``stay``/``Visit``. Here each round answers straight from the script's next ``select``
    entry — the same FIFO the sub-prompts draw from — so the visit's control flow is
    exercised without a real full-screen session.
    """

    def __init__(self, ui: _ScriptedUi) -> None:
        self.ui = ui
        self.pushed: list = []

    def push(self, screen: Any) -> None:
        from meshterm.ui.tui.screen import CANCEL

        self.pushed.append(screen)
        value = self.ui._answer("select")
        screen.resolve(CANCEL if value is None else value)

    def pop(self, screen: Any = None) -> None:
        pass

    @asynccontextmanager
    async def stay(self, screen: Any) -> Any:
        """Keep ``screen`` pushed and hand out a visit answering from the script."""
        self.pushed.append(screen)
        try:
            yield _FakeVisit(self, screen)
        finally:
            pass


class _FakeVisit:
    """One round of a visited screen: the script's next ``select`` answer."""

    def __init__(self, session: _FakeSession, screen: Any) -> None:
        self._session = session
        self.screen = screen

    async def result(self) -> Any:
        from meshterm.ui.tui.screen import CANCEL

        value = self._session.ui._answer("select")
        return CANCEL if value is None else value


class _ScriptedUi:
    """A fake UI surface answering every prompt from a FIFO script.

    Each entry is ``(method, answer)``; the method name is asserted so a flow that asks
    an unexpected question fails loudly instead of silently mis-answering. It exposes a
    :class:`_FakeSession` so the editor's push-a-persistent-menu path works.
    """

    def __init__(self, script: list[tuple[str, Any]]) -> None:
        self.script = list(script)
        self.notes: list[str] = []
        self.views: list[str] = []
        self.presented: list[str] = []
        self.session = _FakeSession(self)

    def _answer(self, method: str) -> Any:
        assert self.script, f"unexpected prompt: {method}"
        expected, value = self.script.pop(0)
        assert expected == method, f"expected {expected} prompt, got {method}"
        return value

    async def select(self, title: str, items: list, **kwargs: Any) -> Any:
        return self._answer("select")

    async def dialog(self, prompt: str, buttons: list, **kwargs: Any) -> Any:
        return self._answer("dialog")

    async def typed_confirm(self, warning: str, word: str, **kwargs: Any) -> bool:
        return self._answer("typed_confirm")

    async def text(self, title: str, **kwargs: Any) -> str | None:
        return self._answer("text")

    async def path(self, title: str, **kwargs: Any) -> str | None:
        return self._answer("path")

    async def autocomplete(self, title: str, choices: list, **kwargs: Any) -> str | None:
        return self._answer("autocomplete")

    async def confirm(self, title: str, **kwargs: Any) -> bool | None:
        return self._answer("confirm")

    async def view(self, renderable: Any, **kwargs: Any) -> None:
        self.views.append(str(kwargs.get("title", "")))

    def note(self, markup: str) -> None:
        self.notes.append(markup)

    def ack(self, markup: str) -> None:
        # The menu's answer: an acknowledgement is a note. (The scripted CLI drops it.)
        self.note(markup)

    def show(self, *renderables: Any) -> None:
        pass

    async def present(self, *, title: str = "") -> None:
        self.presented.append(title)

    def discard(self) -> None:
        pass

    @asynccontextmanager
    async def busy_overlay(self) -> Any:
        yield


@pytest.fixture()
def ctx(tmp_path: Path) -> AppContext:
    """A mock-backed application context whose UI is swapped per test."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "cfg.db")
    context = AppContext(
        console=Console(file=__import__("io").StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    yield context
    context.repo.close()


def _install(ctx: AppContext, script: list[tuple[str, Any]]) -> _ScriptedUi:
    """Install a scripted UI on the context and return it."""
    ui = _ScriptedUi(script)
    ctx.ui = ui  # type: ignore[assignment]
    return ui


@pytest.fixture
def applied_ops(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Every op the page's Apply hands the executor, recorded on its way to the device."""
    from meshterm.tools import config as config_tool

    recorded: list[tuple] = []
    real = config_tool.apply_ops

    async def _recording(ctx, device, snapshot, ops):  # noqa: ANN001
        recorded.extend(ops)
        return await real(ctx, device, snapshot, ops)

    monkeypatch.setattr(config_tool, "apply_ops", _recording)
    return recorded


async def test_editor_stages_a_setting_and_applies_it_in_place(
    ctx: AppContext, applied_ops: list[tuple]
) -> None:
    """Editing a value stages it; Apply sends it, and the page stays up for the next round."""
    ui = _install(
        ctx,
        [
            ("select", "name"),
            ("text", "NewName"),
            ("select", "__apply__"),
            ("select", None),  # still on the page after Apply; nothing staged, so Esc leaves
        ],
    )
    assert await edit_config(ctx) == {"applied": 1}
    assert applied_ops == [("set", "name", "NewName")]
    assert (await (await ctx.device()).get_self_info())["name"] == "NewName"
    assert ui.presented == ["Apply"]


async def test_editor_restaging_the_current_value_clears_the_stage(ctx: AppContext) -> None:
    """Typing the device's existing value back un-stages the row (nothing to apply)."""
    device = await ctx.device()
    current = (await device.get_self_info())["name"]
    _install(
        ctx,
        [
            ("select", "name"),
            ("text", "Changed"),
            ("select", "name"),
            ("text", str(current)),  # back to what the device already has
            ("select", None),  # nothing staged now — Esc closes without a discard dialog
        ],
    )
    assert await edit_config(ctx) is None


async def test_editor_asks_before_discarding_staged_changes(
    ctx: AppContext, applied_ops: list[tuple]
) -> None:
    """Cancelling with staged changes confirms; Keep editing returns to the page."""
    _install(
        ctx,
        [
            ("select", "name"),
            ("text", "NewName"),
            ("select", "__cancel__"),
            ("dialog", "keep"),  # changed my mind — keep editing
            ("select", "__apply__"),
            ("select", None),
        ],
    )
    await edit_config(ctx)
    assert applied_ops == [("set", "name", "NewName")]


async def test_editor_discards_staged_changes_when_confirmed(ctx: AppContext) -> None:
    """Cancelling with staged changes and confirming the discard applies nothing."""
    _install(
        ctx,
        [
            ("select", "name"),
            ("text", "NewName"),
            ("select", None),  # Esc from the main menu
            ("dialog", "discard"),
        ],
    )
    assert await edit_config(ctx) is None


async def test_editor_clean_close_needs_no_confirmation(ctx: AppContext) -> None:
    """With nothing staged, Esc leaves immediately — no dialog in the script."""
    _install(ctx, [("select", None)])
    assert await edit_config(ctx) is None


async def test_editor_menu_pins_the_column_header_over_the_category(ctx: AppContext) -> None:
    """Scrolled deep, the lane names stay overhead with the category heading under them."""
    from meshterm.ui.tui import SelectScreen, frame

    ui = _install(ctx, [("select", None)])
    assert await edit_config(ctx) is None
    menu = ui.session.pushed[0]
    # Re-open the same rows highlighting a row in the last category, so the list scrolls
    # past both the column header and the earlier headings.
    deep = SelectScreen(menu.title, menu._items, default="__custom__")
    visible, above, _below = frame._visible_slice(deep, deep.render_body(100), 6)
    top = [_ANSI.sub("", row).strip() for row in visible[:2]]
    assert top[0].startswith("SETTING") and top[0].endswith("DESCRIPTION")
    # Whichever section the window's top row fell in, its heading pins under the lanes.
    assert top[1].startswith("── ") and top[1].endswith(" ──")
    assert above is True
    assert any("Set by name" in _ANSI.sub("", row) for row in visible)


async def test_editor_latitude_and_longitude_are_rows_of_their_own(
    ctx: AppContext, applied_ops: list[tuple]
) -> None:
    """Each coordinate is typed on its own row, as on the repeater admin page."""
    _install(
        ctx,
        [
            ("select", "adv_lat"),
            ("text", "45.5"),
            ("select", "adv_lon"),
            ("text", "-73.6"),
            ("select", "__apply__"),
            ("select", None),
        ],
    )
    await edit_config(ctx)
    assert applied_ops == [("set", "adv_lat", 45.5), ("set", "adv_lon", -73.6)]
    info = await (await ctx.device()).get_self_info()
    assert (info["adv_lat"], info["adv_lon"]) == (45.5, -73.6)


async def test_editor_location_map_pick_stages_rounded_coordinates(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch, applied_ops: list[tuple]
) -> None:
    """Picking on the map stages the chosen point, rounded to advert precision."""

    async def _fake_pick(_ctx: AppContext, *, initial=None):
        assert initial is None  # the mock device has no fix (0, 0)
        return (45.51234567, -73.65432109)

    monkeypatch.setattr("meshterm.ui.map_screen.pick_location", _fake_pick)
    _install(
        ctx,
        [
            ("select", "__location__"),  # straight onto the map — no dialog in between
            ("select", "__apply__"),
            ("select", None),
        ],
    )
    await edit_config(ctx)
    assert applied_ops == [("set", "adv_lat", 45.512346), ("set", "adv_lon", -73.654321)]


async def test_editor_map_pick_opens_on_the_staged_position_and_can_be_backed_out_of(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The map opens where the typed coordinates put the node; Esc on the map stages nothing."""
    opened: list = []

    async def _fake_pick(_ctx: AppContext, *, initial=None):
        opened.append(initial)
        return None  # Esc on the map

    monkeypatch.setattr("meshterm.ui.map_screen.pick_location", _fake_pick)
    _install(
        ctx,
        [
            ("select", "adv_lat"),
            ("text", "45.5"),
            ("select", "adv_lon"),
            ("text", "-73.6"),
            ("select", "__location__"),
            ("select", None),  # the typed pair is still staged, so leaving asks
            ("dialog", "discard"),
        ],
    )
    assert await edit_config(ctx) is None
    assert opened == [(45.5, -73.6)]


def test_the_map_pick_row_heads_the_coordinates_it_sets() -> None:
    """``Pick location on map…`` sits directly above Latitude and Longitude."""
    _title, items = _menu_items(_SNAPSHOT, {}, 0)
    rows = _row_texts(items)
    at = next(i for i, row in enumerate(rows) if row.startswith("Pick location on map…"))
    assert rows[at + 1].startswith("Latitude")
    assert rows[at + 2].startswith("Longitude")
    assert not any(row.startswith("Location") for row in rows)


async def test_send_advert_sends_zero_hop_and_flood_immediately(ctx: AppContext) -> None:
    """The advert popup sends immediately: zero-hop by default, flood when chosen."""
    device = await ctx.device()
    sent: list[bool] = []

    async def _record(flood: bool = False) -> None:
        sent.append(flood)

    device.send_advert = _record  # type: ignore[method-assign]
    ui = _install(ctx, [("select", "zero")])
    await send_advert(ctx)
    ui = _install(ctx, [("select", "flood")])
    await send_advert(ctx)
    assert sent == [False, True]
    assert any("flood" in n for n in ui.notes)


def test_the_page_leaves_sending_an_advert_to_its_own_menu_entry() -> None:
    """The box's actions moved onto the page; sending an advert stayed a main-menu popup."""
    _title, items = _menu_items(_SNAPSHOT, {}, 0)
    assert not any("Send advert" in row for row in _row_texts(items))


async def test_send_advert_standalone_shares_the_contact_card(ctx: AppContext) -> None:
    """The standalone flow's share option opens the same QR/URI contact-card view."""
    ui = _install(ctx, [("select", "share")])
    await send_advert(ctx)
    assert any(v.startswith("Share ") for v in ui.views)


async def test_send_advert_standalone_backs_out_cleanly(ctx: AppContext) -> None:
    """Backing out of the advert select sends nothing and returns to the menu."""
    device = await ctx.device()

    async def _boom(flood: bool = False) -> None:  # pragma: no cover - must not run
        raise AssertionError("no advert should be sent on Back")

    device.send_advert = _boom  # type: ignore[method-assign]
    _install(ctx, [("select", None)])
    await send_advert(ctx)


async def test_actions_factory_reset_gates_on_typed_confirmation(ctx: AppContext) -> None:
    """Factory reset only runs behind the typed dialog."""
    device = await ctx.device()
    await device.set_custom_var("mode", "test")

    # Declined: the device is untouched.
    _install(
        ctx,
        [
            ("select", "__reset__"),
            ("typed_confirm", False),
            ("select", None),
        ],
    )
    await edit_config(ctx)
    assert await device.get_custom_vars() == {"mode": "test"}

    # Confirmed: the reset runs and the device comes back empty.
    _install(
        ctx,
        [
            ("select", "__reset__"),
            ("typed_confirm", True),
            ("select", None),
        ],
    )
    await edit_config(ctx)
    assert await device.get_custom_vars() == {}


async def test_actions_import_key_gates_on_typed_confirmation(ctx: AppContext) -> None:
    """Importing an identity key requires the typed IMPORT confirmation."""
    device = await ctx.device()
    new_key = "ab" * 32
    _install(
        ctx,
        [
            ("select", "__identity_key__"),
            ("select", "import"),
            ("text", new_key),
            ("typed_confirm", True),
            ("select", None),
        ],
    )
    await edit_config(ctx)
    assert await device.export_private_key() == new_key


async def test_actions_reboot_on_simulator_stays_on_the_screen(ctx: AppContext) -> None:
    """The simulator has no link to drop, so a confirmed reboot just notes and returns."""
    ui = _install(
        ctx,
        [
            ("select", "__reboot__"),
            ("dialog", "reboot"),
            ("select", None),
        ],
    )
    await edit_config(ctx)
    assert ctx.reboot_in_progress is False
    assert any("rebooting" in n for n in ui.notes)


async def test_actions_sync_clock_corrects_the_device_time(ctx: AppContext) -> None:
    """Sync clock shows the drift, and confirming writes the host time to the device."""
    import time

    device = await ctx.device()
    assert (await device.get_time()) < int(time.time())  # the mock boots with drift
    ui = _install(
        ctx,
        [
            ("select", "__sync_clock__"),
            ("dialog", "sync"),
            ("select", None),
        ],
    )
    await edit_config(ctx)
    assert abs((await device.get_time()) - int(time.time())) <= 2
    assert any("clock set to" in n for n in ui.notes)


async def test_actions_sync_clock_cancel_leaves_the_clock_alone(ctx: AppContext) -> None:
    """Backing out of the sync dialog changes nothing."""
    device = await ctx.device()
    before = await device.get_time()
    _install(
        ctx,
        [
            ("select", "__sync_clock__"),
            ("dialog", None),
            ("select", None),
        ],
    )
    await edit_config(ctx)
    assert abs((await device.get_time()) - before) <= 2  # still ticking on the old drift


async def test_editor_stages_flood_scope(ctx: AppContext, applied_ops: list[tuple]) -> None:
    """The flood scope is a first-class staged setting, hashtag-normalized on apply.

    The device reads it back as ``#alpha``, not the ``alpha`` that was staged — and the row
    must still leave the stage, because the device *took* it: what unstages a value is its
    acceptance, never a string comparison.
    """
    _install(
        ctx,
        [
            ("select", "flood_scope"),
            ("text", "alpha"),
            ("select", "__apply__"),
            ("select", None),  # nothing left staged: Esc leaves without a discard dialog
        ],
    )
    await edit_config(ctx)
    assert applied_ops == [("set", "flood_scope", "alpha")]
    assert await (await ctx.device()).get_default_flood_scope() == "#alpha"


async def test_actions_backup_writes_immediately(ctx: AppContext, tmp_path: Path) -> None:
    """Backup runs at once: the TOML lands on disk before the screen closes."""
    target = tmp_path / "out" / "backup.toml"
    ui = _install(
        ctx,
        [
            ("select", "__backup__"),
            ("path", str(target)),
            ("select", None),
        ],
    )
    await edit_config(ctx)
    assert target.exists()
    assert any("wrote" in n for n in ui.notes)


async def test_editor_bool_prompt_uses_the_button_dialog(
    ctx: AppContext, applied_ops: list[tuple]
) -> None:
    """A boolean setting is toggled through the On/Off dialog and staged."""
    _install(
        ctx,
        [
            ("select", "manual_add_contacts"),
            ("dialog", True),
            ("select", "__apply__"),
            ("select", None),
        ],
    )
    await edit_config(ctx)
    assert applied_ops == [("set", "manual_add_contacts", True)]


async def test_editor_custom_var_suggests_known_names(
    ctx: AppContext, applied_ops: list[tuple]
) -> None:
    """With existing custom vars the name prompt offers them as suggestions."""
    device = await ctx.device()
    await device.set_custom_var("existing", "1")
    _install(
        ctx,
        [
            ("select", "__custom__"),
            ("autocomplete", "existing"),
            ("text", "2"),
            ("select", "__apply__"),
            ("select", None),
        ],
    )
    await edit_config(ctx)
    assert applied_ops == [("set_custom", "existing", "2")]
    assert (await device.get_custom_vars())["existing"] == "2"


# -- the page's shape: the repeater-admin page's, for the radio in your hand --------------


def _row_texts(items: list) -> list[str]:
    """The plain text of every selectable row on the page."""
    from meshterm.ui.tui import Choice

    return [
        getattr(item.title, "plain", str(item.title)) for item in items if isinstance(item, Choice)
    ]


def test_the_page_is_full_screen_like_repeater_admin() -> None:
    """The page fills the frame; only its prompts, confirms and results float over it."""
    assert _editor(_SNAPSHOT).floating is False


def test_the_page_ends_long_rows_at_the_edge_despite_its_actions() -> None:
    """Rows are cut with the ellipsis and ←→ stay inert, Actions rows and all.

    Those rows pin a head block, which on its own turns a list's ←→ scrolling on; the
    page keeps the handling it always had, and the repeater page now shares it.
    """
    menu = _editor(_SNAPSHOT)
    before = list(menu.render_body(40))
    assert "←→" not in menu.footer_hint
    menu.handle("right")
    assert list(menu.render_body(40)) == before


def test_the_title_names_the_device_and_counts_what_is_staged() -> None:
    """``Feature — subject · status``: the node's name, then the staged count as an atom."""
    assert _menu_items(_SNAPSHOT, {}, 0)[0] == "Device config — Homestead-Hub"
    staged_title, _items = _menu_items(_SNAPSHOT, {"tx_power": 14}, 1)
    assert staged_title == "Device config — Homestead-Hub · 1 staged"


def test_the_actions_close_the_page_every_label_in_the_same_cell() -> None:
    """The box's operations sit last, one icon column wide, as on the repeater page.

    ``↻`` and ``⚠`` are one cell among two-cell siblings; a row that wrote ``icon + " "``
    would start those two labels a column early.
    """
    from rich.cells import cell_len

    _title, items = _menu_items(_SNAPSHOT, {}, 0)
    rows = _row_texts(items)
    labels = (
        "Read settings",
        "Sync clock…",
        "Back up config…",
        "Restore config…",
        "Identity key…",
        "Reboot device…",
        "Factory reset…",
    )
    starts = {
        label: cell_len(row[: row.index(label)]) for row in rows for label in labels if label in row
    }
    assert set(starts) == set(labels)
    assert len(set(starts.values())) == 1, f"a label starts a column early: {starts}"
    assert "Factory reset…" in rows[-1]  # nothing staged: the last verb ends the list


def test_custom_variables_are_rows_of_their_own() -> None:
    """Each reported variable is a row like a setting; the firmware's own read in words."""
    _title, items = _menu_items(
        _SNAPSHOT,
        {},
        1,
        custom={"gps": "1", "probe": ""},
        custom_pending={"gps": "0"},
    )
    rows = _row_texts(items)
    assert "on → off" in next(row for row in rows if row.startswith("GPS "))
    assert "empty" in next(row for row in rows if row.startswith("probe"))
    assert any(row.startswith("Set by name…") for row in rows)


async def test_a_listed_custom_variable_stages_and_applies(
    ctx: AppContext, applied_ops: list[tuple]
) -> None:
    """The firmware's GPS switch is an On/Off pick, applied like any other staged value."""
    device = await ctx.device()
    await device.set_custom_var("gps", "1")
    _install(
        ctx,
        [
            ("select", "__custom_var__:gps"),
            ("dialog", "0"),
            ("select", "__apply__"),
            ("select", None),
        ],
    )
    await edit_config(ctx)
    assert applied_ops == [("set_custom", "gps", "0")]
    assert (await device.get_custom_vars())["gps"] == "0"


async def test_a_refused_value_stays_staged_while_the_rest_apply(
    ctx: AppContext, applied_ops: list[tuple]
) -> None:
    """The repeater page's rule: a refusal is shown and kept staged, never silently dropped."""
    device = await ctx.device()

    async def _refuse(name: str) -> None:
        raise RuntimeError("device rejected the command: illegal argument")

    device.set_name = _refuse  # type: ignore[method-assign]
    ui = _install(
        ctx,
        [
            ("select", "name"),
            ("text", "Refused"),
            ("select", "flood_scope"),
            ("text", "alpha"),
            ("select", "__apply__"),
            ("select", None),  # the refused name is still staged, so leaving asks first
            ("dialog", "discard"),
        ],
    )
    assert await edit_config(ctx) == {"applied": 1}
    assert [op[1] for op in applied_ops] == ["name", "flood_scope"]
    assert await device.get_default_flood_scope() == "#alpha"
    assert any("still staged" in note for note in ui.notes)
    assert any("1[/brand] of 2" in note for note in ui.notes)


async def test_repeat_is_refused_off_the_frequencies_the_firmware_allows(ctx: AppContext) -> None:
    """Switching relaying on at a frequency the firmware refuses says so, and stages nothing."""
    ui = _install(ctx, [("select", "client_repeat"), ("dialog", True), ("select", None)])
    assert await edit_config(ctx) is None
    assert any("relays only on" in note for note in ui.notes)
    assert ui.presented == ["Repeat"]


async def test_repeat_goes_on_with_the_frequency_staged_beside_it(
    ctx: AppContext, applied_ops: list[tuple]
) -> None:
    """Checked against the radio as staged, and sent after it whatever the staging order."""
    _install(
        ctx,
        [
            ("select", "client_repeat"),
            ("dialog", True),  # refused: the device still sits on 869.618
            ("select", "radio_freq"),
            ("text", "869.495"),
            ("select", "client_repeat"),
            ("dialog", True),  # allowed now: checked against the staged frequency
            ("select", "__apply__"),
            ("select", None),
        ],
    )
    await edit_config(ctx)
    assert [op[1] for op in applied_ops] == ["radio_freq", "client_repeat"]
    device = await ctx.device()
    assert (await device.get_device_info())["repeat"] is True
    assert (await device.get_self_info())["radio_freq"] == 869.495
