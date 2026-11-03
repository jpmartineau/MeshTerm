# SPDX-License-Identifier: Apache-2.0
"""The Clock drift page and the ``clocks`` command: the histogram, the sections, the rows."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

from meshterm.core.admin_store import AdminStore
from meshterm.core.models import NODE_TYPE_REPEATER, ChatMessage, Contact, Message, utcnow
from meshterm.core.preferences import Preferences
from meshterm.persistence.repository import Repository
from meshterm.services.clock_offsets import ClockOffset
from meshterm.tools.clocks import _listing, clock_rows
from meshterm.ui.clocks_screen import (
    ClockRow,
    buckets,
    clock_drift_screen,
    histogram,
    own_clock_note,
)
from tests.conftest import plain

TOLERANCE = 300


def _row(name: str, offset_s: float, *, managed: bool = False, keyed: bool = True) -> ClockRow:
    key = (name.encode().hex() * 32)[:64]
    who = key[:12] if keyed else name
    offset = ClockOffset(who, keyed, offset_s, 3, utcnow() - timedelta(minutes=5))
    contact = Contact(name=name, public_key=key) if keyed else None
    return ClockRow(offset, name, key if keyed else None, None, managed, contact)


def test_the_steps_run_from_behind_through_in_sync_to_ahead() -> None:
    """The centre step is the tolerance. Each step past it is an hour, a day, a year."""
    labels = [b.label for b in buckets(TOLERANCE)]
    assert labels == [
        "> 1y behind",
        "1d-1y behind",
        "1h-1d behind",
        "5m-1h behind",
        "in sync ≤5m",
        "5m-1h ahead",
        "1h-1d ahead",
        "1d-1y ahead",
        "> 1y ahead",
    ]
    # A tolerance of two hours leaves no step between it and the hour.
    wide = [b.label for b in buckets(7200)]
    assert wide[2:5] == ["2h-1d behind", "in sync ≤2h", "2h-1d ahead"]


def test_each_offset_falls_in_exactly_one_step() -> None:
    """The edges belong to one step each, so no sender counts twice or not at all."""
    rows = [_row(f"n{i}", s) for i, s in enumerate((-300, 300, -3600, 3600, 0, -4e8, 4e8))]
    counted = dict((b.label, n) for b, n in histogram(rows, TOLERANCE))
    assert sum(counted.values()) == len(rows)
    assert counted["in sync ≤5m"] == 3  # -300, 0, and 300
    assert counted["5m-1h behind"] == 1 and counted["5m-1h ahead"] == 1
    assert counted["> 1y behind"] == 1 and counted["> 1y ahead"] == 1


def test_most_clocks_ahead_blame_our_own_clock() -> None:
    """When most keyed clocks agree against ours, the fault is probably ours."""
    agree = [_row(f"n{i}", 2 * 3600 + i) for i in range(6)]
    note = own_clock_note(agree, TOLERANCE)
    assert note is not None and "2h ahead of this computer" in note
    mixed = [_row(f"n{i}", (-1) ** i * 7200) for i in range(6)]
    assert own_clock_note(mixed, TOLERANCE) is None
    assert own_clock_note(agree[:3], TOLERANCE) is None  # too few to judge


def test_the_page_lists_managed_nodes_first_then_the_clocks_out_of_sync() -> None:
    """A managed node is listed also in sync. An other node is listed only out of sync."""
    rows = [
        _row("Hilltop", 10, managed=True),
        _row("Fine", 20),
        _row("Slow", -3 * 3600),
        _row("Worse", -9 * 86400),
        _row("Bob", -900, keyed=False),
    ]
    screen = clock_drift_screen(rows, tolerance_s=TOLERANCE)
    assert screen.title == "Clock drift · 5 nodes · 3 out of sync"
    text = plain("\n".join(screen.render_body(72)))
    order = [text.index(s) for s in ("Managed nodes", "Hilltop", "Out of sync", "Worse", "Slow")]
    assert order == sorted(order), "managed first, then the largest offset first"
    assert "Fine" not in text
    assert text.index("Senders with no key") < text.index("Bob")
    assert "15m behind" in text


def test_managed_nodes_come_first_with_a_header_and_no_sample_rows() -> None:
    """The histogram is on top, the managed nodes are the first list, and each is listed.

    A managed node with no sample is listed too. Each table has a header line under its
    heading. An older offset of a managed node does not count in the histogram or the
    title, because it is not from the window.
    """
    from meshterm.services.clock_offsets import ClockOffset

    old = ClockOffset("ab" * 6, True, -7200, 2, utcnow() - timedelta(days=30))
    stale = ClockRow(
        old, "Old", "ab" * 32, None, True, Contact(name="Old", public_key="ab" * 32), current=False
    )
    never = ClockRow(None, "New", "cd" * 32, None, True, Contact(name="New", public_key="cd" * 32))
    screen = clock_drift_screen([stale, never, _row("Slow", -3 * 3600)], tolerance_s=TOLERANCE)
    assert screen.title == "Clock drift · 1 nodes · 1 out of sync"
    lines = plain("\n".join(screen.render_body(72))).splitlines()
    assert lines[0].strip() == "── All clocks ──"
    assert lines[1].split() == ["OFFSET", "#"]
    managed_at = next(i for i, line in enumerate(lines) if line.strip() == "── Managed nodes ──")
    assert lines[managed_at + 1].split() == ["NAME", "OFFSET", "LAST"]
    assert any("New" in line and "no sample" in line for line in lines)
    assert any("Old" in line and "2h behind" in line and "4w" in line for line in lines)
    text = "\n".join(lines)
    assert text.index("All clocks") < text.index("Managed nodes") < text.index("Out of sync")
    counted = dict((b.label, n) for b, n in histogram([stale, never], TOLERANCE))
    assert sum(counted.values()) == 0


def test_a_page_with_nothing_out_of_sync_says_so() -> None:
    """The Out of sync section always has its heading, and an empty state when it is empty."""
    screen = clock_drift_screen([_row("Fine", 20)], tolerance_s=TOLERANCE)
    assert "no other clock is out of sync" in plain("\n".join(screen.render_body(72)))


def test_the_cli_listing_gives_signed_seconds_and_words() -> None:
    """The plain face says how far and which way. The document gives signed seconds."""
    listing = _listing([_row("Fine", 20), _row("Slow", -3 * 3600, managed=True)], TOLERANCE)
    first, second = listing.rows
    assert first["offset_s"] == -10800 and first["out_of_sync"] and first["managed"]
    assert second["offset_s"] == 20 and not second["out_of_sync"]
    lane = next(c for c in listing.columns if c.key == "offset_s").lanes[0]
    assert lane.render(first["offset_s"]) == "3h behind"
    assert lane.render(second["offset_s"]) == "in sync"


class _DevState:
    def __init__(self, contacts: list[Contact]) -> None:
        self._contacts = contacts

    async def contacts(self) -> list[Contact]:
        return self._contacts

    async def self_info(self) -> dict:
        return {"public_key": "99" * 32}


class _Ctx:
    def __init__(self, repo: Repository, contacts: list[Contact], admin: AdminStore) -> None:
        self.repo = repo
        self.devstate = _DevState(contacts)
        self.preferences = Preferences()
        self.admin_store = admin
        self.log = logging.getLogger("test.clocks")


def test_clock_rows_name_their_nodes_and_mark_the_managed_ones(tmp_path: Path) -> None:
    """A DM sender that is a contact gets its contact. A managed one is marked."""
    repo = Repository(tmp_path / "clocks.db")
    now = utcnow()
    repeater = Contact(name="Hilltop", public_key="ab" * 32, node_type=NODE_TYPE_REPEATER)
    for sender in (repeater.public_key[:12], "cd" * 6):
        repo.record_chat_message(
            ChatMessage.from_message(
                Message(
                    text="hi",
                    sender=sender,
                    sender_timestamp=now - timedelta(hours=2),
                    received_at=now,
                )
            )
        )
    quiet = Contact(name="Quiet", public_key="ef" * 32, node_type=NODE_TYPE_REPEATER)
    admin = AdminStore(tmp_path / "admin.json")
    admin.remember(repeater, "secret")
    admin.remember(quiet, "secret")
    rows = asyncio.run(clock_rows(_Ctx(repo, [repeater, quiet], admin)))
    by_name = {r.label: r for r in rows}
    assert by_name["Hilltop"].managed and by_name["Hilltop"].contact is repeater
    stranger = by_name["cd" * 6]
    assert not stranger.managed and stranger.contact is not None  # a page to open
    assert stranger.contact.key_prefix == "cd" * 6
    # A managed node that never sent a sample still has its row, with no offset.
    assert by_name["Quiet"].managed and by_name["Quiet"].offset is None
    assert not by_name["Quiet"].current
    listing = _listing(rows, TOLERANCE)
    assert listing.rows[-1]["offset_s"] is None and listing.rows[-1]["managed"]
    repo.close()


# -- the bulk clock sync ----------------------------------------------------------------------


def _managed(name: str, offset_s: float | None, node_type: int | None = NODE_TYPE_REPEATER):  # noqa: ANN202
    key = (name.encode().hex() * 32)[:64]
    contact = Contact(name=name, public_key=key, node_type=node_type)
    offset = None if offset_s is None else ClockOffset(key[:12], True, offset_s, 3, utcnow())
    return ClockRow(offset, name, key, node_type, True, contact)


def test_the_bulk_row_ends_the_managed_section_for_each_repeater_and_room() -> None:
    """Each managed repeater and room server is due, also with a correct clock.

    A repeater with no battery loses its time at each power cycle, and the page learns it
    only at its next advert. A managed companion has no ``clock sync`` command, so it is
    never due, and a page with only managed companions has no bulk row.
    """
    from meshterm.ui.clocks_screen import SYNC_ALL, clocks_to_sync

    rows = [
        _managed("Fine", 10),
        _managed("Behind", -7200),
        _managed("Unknown", None),
        _managed("Phone", -7200, node_type=None),
    ]
    assert [c.name for c in clocks_to_sync(rows)] == ["Behind", "Fine", "Unknown"]
    screen = clock_drift_screen(rows, tolerance_s=TOLERANCE)
    text = plain("\n".join(screen.render_body(72)))
    assert "Sync their clocks…  ·  3 nodes" in text
    assert text.index("Sync their clocks") < text.index("Out of sync")
    assert any(getattr(item, "value", None) == SYNC_ALL for item in screen._items)

    in_sync = clock_drift_screen([_managed("Fine", 10)], tolerance_s=TOLERANCE)
    assert "Sync their clocks…  ·  1 node" in plain("\n".join(in_sync.render_body(72)))
    phones = clock_drift_screen([_managed("Phone", -7200, node_type=None)], tolerance_s=TOLERANCE)
    assert "Sync their clocks" not in plain("\n".join(phones.render_body(72)))


class _Ui:
    """The UI of the bulk sync: the confirm, then the dialog of the run.

    ``run_screen`` waits until the run is done, as a user who reads the dialog does, keeps
    what the dialog shows, then closes it.
    """

    def __init__(self) -> None:
        self.shown = ""
        self.session = self

    async def dialog(self, prompt, buttons, **kw):  # noqa: ANN001, ANN003, ANN201
        return "go"

    def invalidate(self) -> None:
        pass

    async def run_screen(self, dialog):  # noqa: ANN001, ANN201
        while not dialog.done:
            await asyncio.sleep(0)
        self.shown = plain("\n".join(dialog.render_body(72)))
        dialog.handle("enter")


class _Device:
    """Two nodes: one accepts the login and the command, one never answers the login."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def admin_login(self, node, password):  # noqa: ANN001, ANN201
        from meshterm.core.models import LoginResult

        return LoginResult.ACCEPTED if node.name == "Behind" else LoginResult.NO_REPLY

    async def send_remote_command(self, node, command, timeout):  # noqa: ANN001, ANN201
        self.commands.append(f"{node.name}: {command}")
        return "OK - clock set"


def test_the_bulk_sync_sets_each_clock_and_shows_each_result(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """One confirm, then each node in turn. The dialog shows the result of each line."""
    from meshterm.core import transmit_gate
    from meshterm.ui.repeater_admin import sync_clocks

    monkeypatch.setattr(transmit_gate, "remaining", lambda **kw: 0.0)  # no countdown here
    behind, silent = _managed("Behind", -7200).contact, _managed("Silent", None).contact
    admin = AdminStore(tmp_path / "admin.json")
    admin.remember(behind, "secret")
    admin.remember(silent, "secret")
    device = _Device()
    ui = _Ui()

    async def get_device():  # noqa: ANN202
        return device

    ctx = type("Ctx", (), {"ui": ui, "admin_store": admin, "device": staticmethod(get_device)})
    asyncio.run(sync_clocks(ctx, [behind, silent]))
    assert device.commands == ["Behind: clock sync"]  # no command after a silent login
    assert "✓ Behind  ·  OK - clock set" in ui.shown
    assert "✗ Silent  ·  no reply to the login" in ui.shown
    assert "OK" in ui.shown.splitlines()[-1]  # the run is done: the chip closes it
    assert admin.get(silent) == "secret"  # a silence keeps the password


def test_the_dialog_lists_each_node_and_closes_only_when_done() -> None:
    """Each node waits, then works, then ends. Esc stops a run, and only a done run closes."""
    from meshterm.ui.clock_sync_dialog import ACTIVE, FAILED, OK, ClockSyncDialog
    from meshterm.ui.tui.spinner import Spinner

    nodes = [_managed(name, None).contact for name in ("One", "Two", "Three")]
    dialog = ClockSyncDialog(nodes, spinner=Spinner())
    stops: list[int] = []
    dialog.on_stop = lambda: stops.append(1)
    text = plain("\n".join(dialog.render_body(72)))
    assert text.count("○") == 3 and "waiting" in text and "Stop" in text
    assert dialog.footer_hint == "Esc stop"

    dialog.update(0, OK, "clock set")
    dialog.update(1, FAILED, "no reply to the login")
    dialog.update(2, ACTIVE, "next in 4s")
    text = plain("\n".join(dialog.render_body(72)))
    assert "✓ One  ·  clock set" in text and "✗ Two  ·  no reply to the login" in text
    assert "Three  ·  next in 4s" in text

    dialog.handle("escape")
    assert stops == [1] and not dialog.done  # a stop asks the owner, and does not close
    dialog.finish()
    text = plain("\n".join(dialog.render_body(72)))
    assert "Three  ·  not done" in text and "OK" in text.splitlines()[-1]
    assert dialog.footer_hint == "Enter OK · Esc close"
