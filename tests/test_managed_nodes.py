# SPDX-License-Identifier: Apache-2.0
"""Managed nodes and refused adverts: the marks, the section, and the advert gate repair.

A managed node is a node whose admin password MeshTerm stores. It has the ``🔑`` mark in
each list of nodes. A contact whose stored advert time is in the future is a node that the
device no longer hears, and the node page can lower that time.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.cells import cell_len

from meshterm.core.admin_store import AdminStore
from meshterm.core.connection import ContactNotOnDeviceError, MeshCoreDevice, MockDevice
from meshterm.core.models import (
    CLOCK_TOLERANCE_S,
    NODE_TYPE_REPEATER,
    Contact,
    advert_gate,
    utcnow,
)
from meshterm.services.clock_offsets import ClockOffset
from meshterm.ui.clocks_screen import ClockRow, clock_drift_screen
from meshterm.ui.contactlist import ContactRow, _lock_cell, _lock_lane_width
from meshterm.ui.marks import LOCK_ICON, MANAGED_ICON
from meshterm.ui.theme import glyph
from tests.conftest import plain

KEY = "ab" * 32


def _stamp(delta: timedelta) -> int:
    return int((utcnow() + delta).timestamp())


# -- the advert gate ------------------------------------------------------------------------


def test_only_a_stored_time_past_the_tolerance_closes_the_gate() -> None:
    """A time far in the future is a closed gate. A near or past time, or none, is not."""
    far = advert_gate(_stamp(timedelta(days=90)))
    assert far is not None and far > utcnow() + timedelta(days=89)
    assert advert_gate(_stamp(timedelta(seconds=CLOCK_TOLERANCE_S // 2))) is None
    assert advert_gate(_stamp(timedelta(days=-1))) is None
    assert advert_gate(0) is None
    assert advert_gate("garbage") is None


def test_the_simulator_opens_the_gate_once() -> None:
    """The first call lowers the time. A second has nothing to do. An unknown key raises."""
    device = MockDevice()
    gated = Contact(
        name="Gated",
        public_key=KEY,
        key_prefix=KEY[:12],
        adverts_refused_until=utcnow() + timedelta(days=30),
    )
    asyncio.run(device.add_contact(gated))
    assert asyncio.run(device.accept_adverts_again(gated)) is True
    assert asyncio.run(device.accept_adverts_again(gated)) is False
    with pytest.raises(ContactNotOnDeviceError):
        asyncio.run(device.accept_adverts_again(Contact(name="Nobody", public_key="cd" * 32)))


class _Commands:
    """A stand-in for the command set of the library: one stored record, and the writes."""

    def __init__(self, record: dict) -> None:
        self.record = record
        self.written: list[dict] = []

    async def get_contact_by_key(self, pubkey: bytes):  # noqa: ANN201
        assert pubkey.hex() == self.record["public_key"]
        return SimpleNamespace(payload=dict(self.record), is_error=lambda: False)

    async def add_contact(self, record: dict):  # noqa: ANN201
        self.written.append(record)
        return SimpleNamespace(payload={}, is_error=lambda: False)


def test_the_device_writes_back_only_a_lower_advert_time() -> None:
    """The record goes back as the device has it, with only a lower advert time.

    The flags, the path, the name, and the position do not change. The time goes to our
    clock less the tolerance, and never to zero.
    """
    record = {
        "public_key": KEY,
        "type": NODE_TYPE_REPEATER,
        "flags": 1,
        "out_path_len": 2,
        "out_path_hash_mode": 0,
        "out_path": "3d27",
        "adv_name": "Hilltop",
        "last_advert": _stamp(timedelta(days=200)),
        "adv_lat": 45.5,
        "adv_lon": -73.6,
    }
    commands = _Commands(record)
    device = MeshCoreDevice(port="mock")
    device._mc = SimpleNamespace(commands=commands)
    assert asyncio.run(device.accept_adverts_again(Contact(name="Hilltop", public_key=KEY)))
    (written,) = commands.written
    now = int(utcnow().timestamp())
    assert now - CLOCK_TOLERANCE_S - 5 <= written["last_advert"] <= now - CLOCK_TOLERANCE_S + 5
    assert {k: v for k, v in written.items() if k != "last_advert"} == {
        k: v for k, v in record.items() if k != "last_advert"
    }

    # A stored time that is already in the past changes nothing.
    commands.record = {**record, "last_advert": now - 3600}
    commands.written.clear()
    assert (
        asyncio.run(device.accept_adverts_again(Contact(name="Hilltop", public_key=KEY))) is False
    )
    assert commands.written == []


# -- the marks ------------------------------------------------------------------------------


def test_the_mark_column_holds_the_padlock_and_the_key() -> None:
    """A managed node has the key in the mark column. A locked and managed one has both."""
    rows = [
        ContactRow(value=1, name="Plain"),
        ContactRow(value=2, name="Managed", managed=True),
        ContactRow(value=3, name="Both", locked=True, managed=True),
    ]
    width = _lock_lane_width(rows)
    assert _lock_cell(rows[0], width).strip() == ""
    assert _lock_cell(rows[1], width).strip() == glyph(MANAGED_ICON)
    assert _lock_cell(rows[2], width).strip() == glyph(LOCK_ICON) + glyph(MANAGED_ICON)
    assert _lock_lane_width(rows[:1]) == 0  # no marks, no cells


def test_the_admin_picker_lists_managed_nodes_first_with_the_key(tmp_path: Path) -> None:
    """The first section of the picker is "Managed nodes", and its rows have the key."""
    from meshterm.ui.admin_picker import admin_picker_rows
    from meshterm.ui.tui import Choice

    store = AdminStore(tmp_path / "admin.json")
    managed = Contact(name="Hilltop", public_key=KEY, node_type=NODE_TYPE_REPEATER)
    other = Contact(name="Valley", public_key="cd" * 32, node_type=NODE_TYPE_REPEATER)
    store.remember(managed, "secret")
    items, _ = admin_picker_rows(SimpleNamespace(admin_store=store), [other, managed])
    lines = [str(item.text(80) if isinstance(item, Choice) else item.text(80)) for item in items]
    text = "\n".join(lines)
    assert text.index("Managed nodes") < text.index("Hilltop") < text.index("Valley")
    hilltop = next(line for line in lines if "Hilltop" in line)
    valley = next(line for line in lines if "Valley" in line)
    assert hilltop.startswith(glyph(MANAGED_ICON))
    # One column for the names, in cells: the key is one character but two cells wide.
    assert cell_len(valley[: valley.index("Valley")]) == cell_len(
        hilltop[: hilltop.index("Hilltop")]
    )


def test_only_a_managed_row_of_the_picker_takes_delete(tmp_path: Path) -> None:
    """Delete forgets a stored password, so it acts only where a password is stored."""
    from meshterm.ui.admin_picker import admin_picker_rows
    from meshterm.ui.tui import Choice

    store = AdminStore(tmp_path / "admin.json")
    managed = Contact(name="Hilltop", public_key=KEY, node_type=NODE_TYPE_REPEATER)
    other = Contact(name="Valley", public_key="cd" * 32, node_type=NODE_TYPE_REPEATER)
    store.remember(managed, "secret")
    items, _ = admin_picker_rows(SimpleNamespace(admin_store=store), [other, managed])

    deletable = {row.value: row.deletable for row in items if isinstance(row, Choice)}
    assert deletable == {"Hilltop": True, "Valley": False}


async def _picker_top(session, title: str):  # noqa: ANN001, ANN202
    """The picker once it is the top screen, or ``None``."""
    from meshterm.ui.tui import SelectScreen

    for _ in range(200):
        top = session.top
        if isinstance(top, SelectScreen) and top.title == title:
            return top
        await asyncio.sleep(0)
    return None


async def _dialog_top(session):  # noqa: ANN001, ANN202
    """The button dialog once it is the top screen, or ``None``."""
    from meshterm.ui.tui.prompt import ButtonDialog

    for _ in range(200):
        if isinstance(session.top, ButtonDialog):
            return session.top
        await asyncio.sleep(0)
    return None


@pytest.mark.parametrize("confirmed", [True, False])
async def test_delete_in_the_admin_picker_forgets_the_password(
    tmp_path: Path, confirmed: bool
) -> None:
    """GitHub issue 28: the user changed a password and had no way to clear the stored one.

    Delete on a managed node asks first (amber, Cancel or Forget). Forget removes the
    password, and the same list refreshes in place: the node leaves the managed section,
    the highlight stays on it, and the list stays open. Cancel changes nothing.
    """
    from meshterm.ui.admin_picker import FORGET_HINT, pick_admin_node
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.tui import CANCEL
    from meshterm.ui.tui.session import TuiSession

    store = AdminStore(tmp_path / "admin.json")
    managed = Contact(name="Hilltop", public_key=KEY, node_type=NODE_TYPE_REPEATER)
    other = Contact(name="Valley", public_key="cd" * 32, node_type=NODE_TYPE_REPEATER)
    store.remember(managed, "secret")
    ctx = SimpleNamespace(ui=TuiUi(TuiSession()), admin_store=store)
    session = ctx.ui.session

    task = asyncio.ensure_future(pick_admin_node(ctx, [other, managed], title="Pick", prompt=""))
    try:
        box = await _picker_top(session, "Pick")
        assert box is not None
        assert box._current_choice().value == "Hilltop"
        assert FORGET_HINT in box.footer_hint
        box.handle("delete")

        confirm = await _dialog_top(session)
        assert confirm is not None, "Delete asks before it forgets"
        assert [label for label, _ in confirm._buttons] == ["Cancel", "Forget"]
        assert confirm.title == "Forget password — Hilltop"
        confirm.resolve(confirmed)

        again = await _picker_top(session, "Pick")
        assert again is box, "the same list, refreshed in place"
        current = box._current_choice()
        assert current.value == "Hilltop", "the highlight follows the node"
        assert current.deletable is (not confirmed)
        box.resolve(CANCEL)
        assert await task is None
    finally:
        if not task.done():
            task.cancel()

    assert (store.get(managed) is None) is confirmed
    assert session._stack == []


def test_forget_all_empties_the_store_and_returns_what_it_held(tmp_path: Path) -> None:
    """It also forgets a password whose node the device does not know any more."""
    store = AdminStore(tmp_path / "admin.json")
    assert store.forget_all() == []
    assert not (tmp_path / "admin.json").exists(), "nothing to forget writes nothing"

    store.remember(Contact(name="Hilltop", public_key=KEY), "one")
    store.remember(Contact(name="Valley", public_key="cd" * 32), "two")
    forgotten = store.forget_all()

    assert sorted(c.label for c in forgotten) == ["Hilltop", "Valley"]
    assert store.credentials() == []


async def _typed_top(session):  # noqa: ANN001, ANN202
    """The typed confirm once it is the top screen, or ``None``."""
    from meshterm.ui.tui.prompt import TypedConfirmDialog

    for _ in range(200):
        if isinstance(session.top, TypedConfirmDialog):
            return session.top
        await asyncio.sleep(0)
    return None


@pytest.mark.parametrize("typed", [True, False])
async def test_forget_passwords_asks_for_the_typed_word_and_names_the_nodes(
    tmp_path: Path, typed: bool
) -> None:
    """GitHub issue 28: one step forgets all the passwords, behind the red typed confirm.

    The confirm names each node, also a node that is not a contact any more, because no
    list shows that node. Esc keeps all the passwords.
    """
    from meshterm.tools.forget_passwords import ForgetPasswordsTool
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.tui import CANCEL
    from meshterm.ui.tui.session import TuiSession

    store = AdminStore(tmp_path / "admin.json")
    for name, key in (("Hilltop", KEY), ("Valley", "cd" * 32), ("Ridge", "ef" * 32)):
        store.remember(Contact(name=name, public_key=key), "secret")
    ctx = SimpleNamespace(ui=TuiUi(TuiSession()), admin_store=store)
    tool = ForgetPasswordsTool()

    task = asyncio.ensure_future(tool.prompt_params(ctx))
    try:
        confirm = await _typed_top(ctx.ui.session)
        assert confirm is not None, "a bulk deletion asks for the typed word"
        assert confirm.title == tool.title
        assert "3 managed nodes: Hilltop, Valley, and Ridge" in confirm._warning
        confirm.resolve(True if typed else CANCEL)
        params = await task
    finally:
        if not task.done():
            task.cancel()

    if not typed:
        assert params is None
        assert len(store.credentials()) == 3
        return
    result = await tool.run(ctx, params)
    assert store.credentials() == []
    assert result.summary == {"forgotten": 3}
    assert "forgot" in result.message
    assert result.report is None, "the menu path has no report"


async def test_forget_passwords_with_nothing_stored_says_so_and_asks_nothing(
    tmp_path: Path,
) -> None:
    """An empty store gets the empty state in a message dialog, and no typed confirm."""
    from meshterm.tools.forget_passwords import ForgetPasswordsTool
    from meshterm.ui.surface import TuiUi
    from meshterm.ui.tui.session import TuiSession

    ctx = SimpleNamespace(ui=TuiUi(TuiSession()), admin_store=AdminStore(tmp_path / "a.json"))
    task = asyncio.ensure_future(ForgetPasswordsTool().prompt_params(ctx))
    try:
        note = await _dialog_top(ctx.ui.session)
        assert note is not None
        assert "no saved admin passwords" in note._prompt.plain
        assert [label for label, _ in note._buttons] == ["OK"]
        note.resolve("ok")
        assert await task is None
    finally:
        if not task.done():
            task.cancel()


def test_forget_passwords_is_a_managed_nodes_row_that_keeps_the_title_column() -> None:
    """The row floats its dialogs over the menu, and its title is no wider than the widest.

    A wider title moves the description of each row of the menu to the right.
    """
    from meshterm.tools import all_tools, get_tool

    tool = get_tool("forget-admin-passwords")
    assert tool is not None and tool.menu_visible and tool.popup
    assert tool.category == "Managed nodes"
    others = [t for t in all_tools() if t.menu_visible and t is not tool]
    assert len(tool.title) <= max(len(t.title or t.name) for t in others)


# -- the Clock drift page -------------------------------------------------------------------


def test_the_page_lists_the_contacts_whose_adverts_are_refused() -> None:
    """A section after the managed nodes names each one, with the date in warn."""
    until = utcnow() + timedelta(days=60)
    gated = Contact(name="Gated", public_key=KEY, adverts_refused_until=until)
    offset = ClockOffset(KEY[:12], True, 2.0, 3, utcnow())
    screen = clock_drift_screen(
        [ClockRow(offset, "Gated", KEY, None, False, gated)], tolerance_s=CLOCK_TOLERANCE_S
    )
    text = plain("\n".join(screen.render_body(72)))
    assert "Adverts refused" in text
    assert f"{until.astimezone():%Y-%m-%d}" in text
    assert text.index("Out of sync") < text.index("Adverts refused")  # the last section
